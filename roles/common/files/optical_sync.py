#!/usr/bin/env python3
"""Carry SatNOGS Optical state between an acquisition host and a processing host.

The acquisition host (a Pi with the camera) and the processing host (a VM)
each keep their own station SQLite store and share only a NAS. SQLite in WAL
mode must not be shared over a network filesystem, so nothing here opens a
database on the NAS; rows travel as small JSON files instead.

    publish        acquisition host: new stack / raw_artifact rows -> outbox
    ingest         processing host:  outbox -> its own store
    config-export  processing host:  newest config snapshot -> config.json
    config-pull    acquisition host: config.json -> its own store, then ask
                                     for optical-acquire to be restarted

Every command loops forever by default (``--once`` runs one pass). Writes to
the NAS are atomic renames, and every insert is idempotent in the client's
store, so killing any of these at any point loses nothing and duplicates
nothing.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sqlite3
import sys
import time

FORMAT = 1

STACK_COLUMNS = (
    "client_uuid", "session", "path", "epoch_utc", "exposure_s", "nframes",
    "timing_source", "timing_sync_state", "timing_sigma_ms", "epoch_refers_to",
    "bias_applied_ms", "config_version", "created_at",
)
ARTIFACT_COLUMNS = ("kind", "path", "bytes", "created_at")

#: A first publish against a store that already holds weeks of stacks would
#: otherwise write one enormous file.
MAX_ROWS_PER_BATCH = 2000


def log(message):
    print(message, file=sys.stderr, flush=True)


def canonical(snapshot):
    return json.dumps(snapshot, sort_keys=True, separators=(",", ":"), default=str)


def atomic_write(path, text):
    directory = os.path.dirname(os.path.abspath(path))
    tmp = os.path.join(directory, ".%s.tmp" % os.path.basename(path))
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def read_json(path, default):
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        return default


def open_store(path):
    from satnogs_optical_client.store import Store

    return Store.open(path)


# -- publish ---------------------------------------------------------------


def collect_batch(conn, cursor):
    """Rows newer than ``cursor``, read in one snapshot, or ``None``."""
    conn.execute("BEGIN")
    try:
        stacks = conn.execute(
            "SELECT id, %s FROM stack WHERE id > ? ORDER BY id LIMIT ?"
            % ", ".join(STACK_COLUMNS),
            (cursor["stack_id"], MAX_ROWS_PER_BATCH),
        ).fetchall()
        last_stack = stacks[-1]["id"] if stacks else cursor["stack_id"]
        artifacts = []
        for row in conn.execute(
            "SELECT a.id, a.stack_id, s.client_uuid AS stack_uuid, %s FROM raw_artifact a "
            "JOIN stack s ON s.id = a.stack_id WHERE a.id > ? ORDER BY a.id LIMIT ?"
            % ", ".join("a.%s" % c for c in ARTIFACT_COLUMNS),
            (cursor["artifact_id"], MAX_ROWS_PER_BATCH),
        ):
            # Stop, rather than skip, at an artifact whose stack is not
            # published yet: the cursor is a single id, so anything skipped
            # here would never be sent.
            if row["stack_id"] > last_stack:
                break
            artifacts.append(row)
        # Sent with every batch that references them rather than tracked by a
        # cursor: ingest maps them to its own versions by content, and needs
        # them in hand for that.
        versions = sorted({row["config_version"] for row in stacks
                           if row["config_version"] is not None})
        configs = [
            {"version": row["version"], "snapshot": json.loads(row["snapshot_json"])}
            for row in conn.execute(
                "SELECT version, snapshot_json FROM config WHERE version IN (%s)"
                % ", ".join("?" * len(versions)), versions)
        ] if versions else []
    finally:
        conn.execute("COMMIT")
    if not stacks and not artifacts:
        return None
    return {
        "format": FORMAT,
        "source": socket.gethostname(),
        "configs": configs,
        "stacks": [{c: row[c] for c in STACK_COLUMNS} for row in stacks],
        "artifacts": [dict({c: row[c] for c in ARTIFACT_COLUMNS},
                           stack_uuid=row["stack_uuid"]) for row in artifacts],
        "last_stack_id": last_stack,
        "last_artifact_id": artifacts[-1]["id"] if artifacts else cursor["artifact_id"],
    }


def publish_once(db, outbox, state_file):
    """Write at most one batch. Returns the number of rows published."""
    if not os.path.exists(db):
        return 0
    cursor = read_json(state_file, {"stack_id": 0, "artifact_id": 0})
    conn = sqlite3.connect(db, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        batch = collect_batch(conn, cursor)
    finally:
        conn.close()
    if batch is None:
        return 0
    name = "%020d-%s.json" % (time.time_ns(), batch["source"])
    atomic_write(os.path.join(outbox, name), json.dumps(batch))
    # A crash before this line re-publishes the same rows next pass, which
    # ingest absorbs: both inserts are idempotent.
    atomic_write(state_file, json.dumps({"stack_id": batch["last_stack_id"],
                                         "artifact_id": batch["last_artifact_id"]}))
    return len(batch["stacks"]) + len(batch["artifacts"])


def cmd_publish(args):
    os.makedirs(args.outbox, exist_ok=True)
    while True:
        count = publish_once(args.db, args.outbox, args.state)
        if count:
            log("publish: %d row(s) -> %s" % (count, args.outbox))
        if args.once:
            return 0
        if count < MAX_ROWS_PER_BATCH:
            time.sleep(args.interval)


# -- ingest ----------------------------------------------------------------


def parse_path_map(pairs):
    mapping = []
    for pair in pairs or ():
        source, sep, target = pair.partition("=")
        if not sep or not source:
            raise SystemExit("--path-map wants FROM=TO, got %r" % pair)
        mapping.append((source.rstrip("/"), target.rstrip("/")))
    # Longest prefix first, so a nested mapping wins over its parent.
    return sorted(mapping, key=lambda item: len(item[0]), reverse=True)


def map_path(path, mapping):
    if path is None:
        return None
    for source, target in mapping:
        if path == source or path.startswith(source + "/"):
            return target + path[len(source):]
    return path


def local_config_versions(store):
    """Canonical snapshot -> newest local version holding it."""
    versions = {}
    for row in store.connection.execute(
            "SELECT version, snapshot_json FROM config ORDER BY version"):
        versions[canonical(json.loads(row[1]))] = int(row[0])
    return versions


def ingest_batch(store, batch, mapping):
    if batch.get("format") != FORMAT:
        raise ValueError("unsupported batch format %r" % (batch.get("format"),))
    local = local_config_versions(store)
    # A remote version whose snapshot this store does not hold maps to NULL:
    # a version number from another store would point at the wrong snapshot.
    remote = {cfg["version"]: local.get(canonical(cfg["snapshot"]))
              for cfg in batch.get("configs", ())}
    with store.transaction():
        for stack in batch.get("stacks", ()):
            fields = {c: stack.get(c) for c in STACK_COLUMNS}
            fields["path"] = map_path(fields["path"], mapping)
            fields["config_version"] = remote.get(fields["config_version"])
            store.insert_stack(**fields)
        for artifact in batch.get("artifacts", ()):
            stack_id = store.connection.execute(
                "SELECT id FROM stack WHERE client_uuid = ?",
                (artifact["stack_uuid"],)).fetchone()
            if stack_id is None:
                raise ValueError("artifact %s names unknown stack %s"
                                 % (artifact["path"], artifact["stack_uuid"]))
            fields = {c: artifact.get(c) for c in ARTIFACT_COLUMNS}
            fields["path"] = map_path(fields["path"], mapping)
            store.insert_raw_artifact(stack_id=stack_id[0], **fields)


def ingest_once(store, outbox, mapping):
    """Ingest every pending batch, oldest first. Returns rows ingested."""
    rejected = os.path.join(outbox, "rejected")
    total = 0
    names = sorted(n for n in os.listdir(outbox)
                   if n.endswith(".json") and not n.startswith("."))
    for name in names:
        path = os.path.join(outbox, name)
        try:
            batch = read_json(path, None)
            if batch is None:
                continue
            ingest_batch(store, batch, mapping)
        except (ValueError, KeyError, TypeError) as exc:
            # A batch that can never be ingested must not block every batch
            # behind it; it is kept for a human rather than deleted.
            os.makedirs(rejected, exist_ok=True)
            os.replace(path, os.path.join(rejected, name))
            log("ingest: REJECTED %s: %s" % (name, exc))
            continue
        os.remove(path)
        total += len(batch.get("stacks", ())) + len(batch.get("artifacts", ()))
    return total


def cmd_ingest(args):
    mapping = parse_path_map(args.path_map)
    os.makedirs(args.outbox, exist_ok=True)
    store = open_store(args.db)
    try:
        while True:
            count = ingest_once(store, args.outbox, mapping)
            if count:
                log("ingest: %d row(s) from %s" % (count, args.outbox))
            if args.once:
                return 0
            time.sleep(args.interval)
    finally:
        store.close()


# -- config ----------------------------------------------------------------


def config_export_once(store, out):
    """Write the newest snapshot to ``out`` if it differs. Returns its version."""
    version, snapshot = store.get_config()
    if not version:
        return 0
    current = read_json(out, None)
    if current is not None and canonical(current.get("snapshot")) == canonical(snapshot):
        return 0
    atomic_write(out, json.dumps({"format": FORMAT, "version": version,
                                  "source": socket.gethostname(),
                                  "snapshot": snapshot}, sort_keys=True))
    return version


def cmd_config_export(args):
    store = open_store(args.db)
    try:
        while True:
            version = config_export_once(store, args.out)
            if version:
                log("config-export: version %d -> %s" % (version, args.out))
            if args.once:
                return 0
            time.sleep(args.interval)
    finally:
        store.close()


def config_pull_once(store, source, restart_flag):
    """Append the exported snapshot if it is new. Returns the new local version."""
    exported = read_json(source, None)
    if exported is None or exported.get("format") != FORMAT:
        return 0
    version, created = store.put_config_if_changed(
        exported["snapshot"],
        author="config-pull (%s v%s)" % (exported.get("source"), exported.get("version")))
    if not created:
        return 0
    if restart_flag:
        atomic_write(restart_flag, "%d\n" % version)
    return version


def cmd_config_pull(args):
    store = open_store(args.db)
    try:
        while True:
            version = config_pull_once(store, args.source, args.restart_flag)
            if version:
                log("config-pull: stored as version %d, restart requested" % version)
            if args.once:
                return 0
            time.sleep(args.interval)
    finally:
        store.close()


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)

    def add(name, func, interval):
        p = sub.add_parser(name)
        p.set_defaults(func=func)
        p.add_argument("--interval", type=float, default=interval, help="seconds between passes")
        p.add_argument("--once", action="store_true", help="one pass, then exit")
        return p

    p = add("publish", cmd_publish, 10)
    p.add_argument("--db", required=True)
    p.add_argument("--outbox", required=True)
    p.add_argument("--state", required=True, help="cursor file, local to this host")

    p = add("ingest", cmd_ingest, 10)
    p.add_argument("--db", required=True)
    p.add_argument("--outbox", required=True)
    p.add_argument("--path-map", action="append", metavar="FROM=TO",
                   help="rewrite a path prefix written by the acquisition host")

    p = add("config-export", cmd_config_export, 10)
    p.add_argument("--db", required=True)
    p.add_argument("--out", required=True)

    p = add("config-pull", cmd_config_pull, 30)
    p.add_argument("--db", required=True)
    p.add_argument("--source", required=True)
    p.add_argument("--restart-flag", help="file to write when optical-acquire should restart")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
