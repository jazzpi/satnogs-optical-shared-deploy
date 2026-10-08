"""optical_sync against the client's real Store.

Needs ``satnogs_optical_client`` importable (e.g. ``PYTHONPATH=../client``);
skipped otherwise.
"""

import importlib.util
import json
import os

import pytest

pytest.importorskip("satnogs_optical_client.store")
from satnogs_optical_client.store import Store  # noqa: E402

SCRIPT = os.path.join(os.path.dirname(__file__), os.pardir,
                      "roles", "common", "files", "optical_sync.py")
_spec = importlib.util.spec_from_file_location("optical_sync", SCRIPT)
sync = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sync)


@pytest.fixture
def hosts(tmp_path):
    outbox = tmp_path / "nas" / "exchange" / "outbox"
    outbox.mkdir(parents=True)
    pi = Store.open(str(tmp_path / "pi.sqlite3"))
    vm = Store.open(str(tmp_path / "vm.sqlite3"))
    yield {
        "pi": pi, "vm": vm, "outbox": str(outbox),
        "state": str(tmp_path / "publish-state.json"),
        "config": str(tmp_path / "nas" / "exchange" / "config.json"),
        "flag": str(tmp_path / "acquire-restart"),
    }
    pi.close()
    vm.close()


def add_stack(store, n, config_version=None, raw="/srv/satnogs-optical/raw"):
    path = "%s/s/stack-%d.fits" % (raw, n)
    stack_id = store.insert_stack(
        client_uuid="uuid-%d" % n, session="s", path=path,
        epoch_utc="2026-10-08T20:00:%02d.000000Z" % n, exposure_s=0.1, nframes=100,
        timing_source="ntp", timing_sync_state="locked", timing_sigma_ms=5.0,
        epoch_refers_to="exposure-mid", config_version=config_version)
    store.insert_raw_artifact(stack_id=stack_id, kind="stack-fits", path=path, bytes=123)
    return stack_id


def publish(h):
    return sync.publish_once(h["pi"].path, h["outbox"], h["state"])


def ingest(h, mapping=()):
    return sync.ingest_once(h["vm"], h["outbox"], sync.parse_path_map(mapping))


def rows(store, sql):
    return [tuple(r) for r in store.connection.execute(sql)]


def test_round_trip_copies_stacks_and_artifacts(hosts):
    add_stack(hosts["pi"], 1)
    add_stack(hosts["pi"], 2)

    assert publish(hosts) == 4
    assert ingest(hosts) == 4

    assert rows(hosts["vm"], "SELECT client_uuid, timing_sigma_ms FROM stack ORDER BY id") == [
        ("uuid-1", 5.0), ("uuid-2", 5.0)]
    assert rows(hosts["vm"], "SELECT s.client_uuid, a.bytes FROM raw_artifact a "
                             "JOIN stack s ON s.id = a.stack_id ORDER BY a.id") == [
        ("uuid-1", 123), ("uuid-2", 123)]
    assert os.listdir(hosts["outbox"]) == []


def test_publish_only_sends_new_rows(hosts):
    add_stack(hosts["pi"], 1)
    publish(hosts)
    assert publish(hosts) == 0

    add_stack(hosts["pi"], 2)
    assert publish(hosts) == 2
    assert len(os.listdir(hosts["outbox"])) == 2


def test_republishing_after_a_lost_cursor_duplicates_nothing(hosts):
    add_stack(hosts["pi"], 1)
    publish(hosts)
    ingest(hosts)

    os.remove(hosts["state"])
    publish(hosts)
    ingest(hosts)

    assert rows(hosts["vm"], "SELECT COUNT(*) FROM stack") == [(1,)]
    assert rows(hosts["vm"], "SELECT COUNT(*) FROM raw_artifact") == [(1,)]


def test_path_map_rewrites_stack_and_artifact_paths(hosts):
    add_stack(hosts["pi"], 1, raw="/mnt/nas/raw")
    publish(hosts)
    ingest(hosts, ["/mnt/nas=/srv/satnogs-optical"])

    assert rows(hosts["vm"], "SELECT path FROM stack") == [
        ("/srv/satnogs-optical/raw/s/stack-1.fits",)]
    assert rows(hosts["vm"], "SELECT path FROM raw_artifact") == [
        ("/srv/satnogs-optical/raw/s/stack-1.fits",)]


def test_map_path_matches_whole_components_only():
    mapping = sync.parse_path_map(["/mnt/nas=/srv/x"])
    assert sync.map_path("/mnt/nas2/a", mapping) == "/mnt/nas2/a"
    assert sync.map_path("/mnt/nas/a", mapping) == "/srv/x/a"


def test_artifact_is_never_published_before_its_stack(hosts, monkeypatch):
    add_stack(hosts["pi"], 1)
    add_stack(hosts["pi"], 2)
    monkeypatch.setattr(sync, "MAX_ROWS_PER_BATCH", 1)

    publish(hosts)
    with open(os.path.join(hosts["outbox"], os.listdir(hosts["outbox"])[0])) as f:
        batch = json.load(f)
    assert [s["client_uuid"] for s in batch["stacks"]] == ["uuid-1"]
    assert [a["stack_uuid"] for a in batch["artifacts"]] == ["uuid-1"]

    while publish(hosts):
        pass
    ingest(hosts)
    assert rows(hosts["vm"], "SELECT COUNT(*) FROM raw_artifact") == [(2,)]


def test_config_version_is_mapped_by_content(hosts):
    for gain in (1, 2, 3):
        hosts["vm"].put_config({"camera": {"gain": gain}}, author="test")
    assert sync.config_export_once(hosts["vm"], hosts["config"]) == 3

    assert sync.config_pull_once(hosts["pi"], hosts["config"], None) == 1
    add_stack(hosts["pi"], 1, config_version=1)
    publish(hosts)
    ingest(hosts)

    assert rows(hosts["vm"], "SELECT config_version FROM stack") == [(3,)]


def test_unknown_config_version_becomes_null(hosts):
    hosts["pi"].put_config({"camera": {"gain": 99}}, author="test")
    add_stack(hosts["pi"], 1, config_version=1)
    publish(hosts)
    ingest(hosts)

    assert rows(hosts["vm"], "SELECT config_version FROM stack") == [(None,)]


def test_config_export_writes_only_on_change(hosts):
    assert sync.config_export_once(hosts["vm"], hosts["config"]) == 0
    hosts["vm"].put_config({"camera": {"gain": 1}}, author="test")
    assert sync.config_export_once(hosts["vm"], hosts["config"]) == 1
    assert sync.config_export_once(hosts["vm"], hosts["config"]) == 0


def test_config_pull_requests_restart_only_for_a_new_snapshot(hosts):
    hosts["vm"].put_config({"camera": {"gain": 1}}, author="test")
    sync.config_export_once(hosts["vm"], hosts["config"])

    assert sync.config_pull_once(hosts["pi"], hosts["config"], hosts["flag"]) == 1
    assert os.path.exists(hosts["flag"])
    os.remove(hosts["flag"])

    assert sync.config_pull_once(hosts["pi"], hosts["config"], hosts["flag"]) == 0
    assert not os.path.exists(hosts["flag"])
    assert hosts["pi"].get_config() == (1, {"camera": {"gain": 1}})


def test_config_pull_without_an_export_does_nothing(hosts):
    assert sync.config_pull_once(hosts["pi"], hosts["config"], hosts["flag"]) == 0


def test_poison_batch_is_set_aside_and_the_queue_moves_on(hosts):
    with open(os.path.join(hosts["outbox"], "00000000000000000001-x.json"), "w") as f:
        f.write("{not json")
    add_stack(hosts["pi"], 1)
    publish(hosts)

    assert ingest(hosts) == 2
    assert os.listdir(os.path.join(hosts["outbox"], "rejected")) == [
        "00000000000000000001-x.json"]
    assert rows(hosts["vm"], "SELECT COUNT(*) FROM stack") == [(1,)]


def test_artifact_for_an_unknown_stack_is_rejected(hosts):
    batch = {"format": sync.FORMAT, "configs": [], "stacks": [], "artifacts": [
        {"stack_uuid": "nope", "kind": "stack-fits", "path": "/x", "bytes": 1,
         "created_at": "2026-10-08T20:00:00Z"}]}
    with open(os.path.join(hosts["outbox"], "00000000000000000001-x.json"), "w") as f:
        json.dump(batch, f)

    assert ingest(hosts) == 0
    assert os.listdir(os.path.join(hosts["outbox"], "rejected")) == [
        "00000000000000000001-x.json"]


def test_publish_without_a_store_yet_does_nothing(tmp_path):
    outbox = tmp_path / "outbox"
    outbox.mkdir()
    assert sync.publish_once(str(tmp_path / "missing.sqlite3"), str(outbox),
                             str(tmp_path / "state.json")) == 0
