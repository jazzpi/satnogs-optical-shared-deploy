# Plan: split SatNOGS Optical deployment

Run a SatNOGS Optical station split across two hosts, with a NAS as the only
thing they share:

- **Acquisition host** — a Raspberry Pi (Ubuntu) with the camera. Runs only
  `optical-acquire`, natively. Writes FITS straight to the NAS.
- **Processing host** — a VM. Runs everything else (process, agent, web UI,
  claim page, retention) in Podman containers managed by systemd (Quadlet).

The upstream client and its SD-card image assume one host. This repo is a
deployment of the **unmodified** client; everything that bridges the two hosts
lives here.

Two repos:

| Repo | Contents | Public |
|---|---|---|
| `shared-deploy` (this one, Ansible collection `jazzpi.satnogs_optical_split`) | roles, playbooks, sync script, docs, examples | yes |
| `optical-deploy-config` (private) | inventory, variables, vault-encrypted secrets, pinned collection version | no |

## Decisions

| Topic | Decision | Why |
|---|---|---|
| Camera | Native `optical-acquire` on the Pi, `libcamera` driver via Ubuntu's `python3-picamera2` | A containerised libcamera has to match the host kernel/firmware; native avoids that drift |
| Pi OS | Ubuntu (not Raspberry Pi OS) | Already used for stvid with picamera2 on a Pi 5 |
| FITS | Written directly to the NAS, never to the SD card | SD wear: ~3.3 GB/hour of stacks |
| Pi store | The Pi's own SQLite store stays on the SD card | Writes are tiny (rows only); a reboot loses nothing |
| Clock | chrony against the local NTP server | Acquire validates the clock itself every cycle; there is no supported way to skip it for a real camera |
| Database | SQLite on each host, no shared DB, no Postgres | SQLite WAL is unsafe on network filesystems; Postgres would mean forking the client's store layer |
| Frame path | `files` (the client's default); not pinned | The shared-memory ring only works with processing on the same host. Setting it to `ring` in the web UI stops FITS arriving, which is visible and revertible |
| Config from the web UI | Passed to the Pi unfiltered, including `acquire.source` and `acquire.frame_path` | A bad setting shows up as no FITS arriving and is reverted in the UI; filtering would add complexity for little gain |
| VM runtime | Podman + Quadlet, host systemd timers | Keeps restart/ordering/timers/journald and the `Type=notify` watchdog without systemd inside containers |
| Config management | Ansible, generic collection + private config repo | Re-runnable restore; shareable without our IPs/tokens |

## NAS layout

Mounted at the same path on both hosts (default `/srv/satnogs-optical`), so
stored paths mean the same thing on both and no rewriting is needed by
default.

```
/srv/satnogs-optical/
  raw/                     FITS stacks + .planes.fits sidecars (Pi writes, VM retention deletes)
  exchange/
    outbox/                JSON batches of stack rows, Pi -> VM (VM deletes after ingest)
    outbox/rejected/       batches that could not be ingested, kept for a human
    config.json            newest VM configuration snapshot, VM -> Pi
```

The service user must have the **same uid/gid on both hosts** (default 1990)
so files written by the Pi can be read and deleted by the VM over NFS.

## Data flow

```
Pi                                   NAS                         VM
optical-acquire --FITS-------------> raw/ <---------------------- optical-process (reads FITS)
   | stack + raw_artifact rows                                    optical-retention (deletes old FITS)
   v
Pi store (SD) --optical-sync-publish--> exchange/outbox/ --optical-sync-ingest--> VM store
                                                                    |
Pi store <--optical-config-pull-- exchange/config.json <--optical-config-export--+
   |                                                         (web UI edits land in VM store)
   +--> restart request --> optical-acquire-restart.path (root) --> restart optical-acquire
```

## The sync script (`optical_sync.py`)

One stdlib-only script (plus the client's own `Store` API for writes), four
subcommands, each a long-running loop (`--once` for one pass):

| Command | Host | Does |
|---|---|---|
| `publish` | Pi | Every 10 s: new `stack` and `raw_artifact` rows (by id cursor, kept in a small local state file) -> one JSON batch in `outbox/`, written atomically |
| `ingest` | VM | Every 10 s: each batch, oldest first -> `insert_stack` / `insert_raw_artifact` (idempotent on `client_uuid` / `path`), optional path-prefix rewrite, then delete the batch |
| `config-export` | VM | Every 10 s: newest config snapshot -> `config.json` if it changed |
| `config-pull` | Pi | Every 30 s: `config.json` -> `put_config_if_changed`; if a new version was stored, write a restart-request file |

Why an outbox of small files rather than copying the Pi's database:
a full copy grows forever (rows are never deleted on the Pi) and would be
rewritten every 10 s; batches carry only new rows, and if the VM is down they
simply wait on the NAS.

Details that matter:

- **Crash safety.** Batches and `config.json` are written to a temp file and
  renamed. The publish cursor advances only after the batch is renamed into
  place; a crash in between re-publishes rows, which ingest absorbs.
- **Ordering.** An artifact is never published before its stack: the artifact
  scan stops at the first artifact whose stack is not yet published.
- **`config_version`.** Version numbers differ between the two stores. Each
  batch carries the Pi config snapshots its stacks reference; ingest maps
  each to the VM version with an identical snapshot, else `NULL`. For a
  `NULL`, processing attributes the measurements to the VM's current config
  version.
- **Poison batches** are moved to `outbox/rejected/` instead of blocking the
  queue.
- **Restarting acquire.** `config-pull` runs as the service user and cannot
  call `systemctl`; it writes `/run/satnogs-optical/acquire-restart` and a
  root `.path` unit performs the restart.
- **No filtering.** `config-pull` stores the VM's snapshot as is. The Pi sets
  no `SATNOGS_OPTICAL_SOURCE`, so `acquire.source` from the web UI applies
  (default `auto`, which resolves to `libcamera` on the Pi).

## Collection layout (`jazzpi.satnogs_optical_split`)

```
shared-deploy/
  galaxy.yml, meta/runtime.yml, README.md, LICENSE (AGPL-3.0-or-later)
  playbooks/
    site.yml               all roles, by inventory group
    backup_secrets.yml     fetch identity/token files into the private repo, vault-encrypt them
  roles/
    common/                service user (fixed uid/gid), optical_sync.py
    nas_mount/             NFS/SMB mount, raw/ and exchange/ directories
    acquire/               Pi: apt packages, venv (--system-site-packages) with the client
                           at a pinned commit, chrony, station.toml, units:
                           optical-acquire, optical-sync-publish, optical-config-pull,
                           optical-acquire-restart.{path,service}
    processing/            VM: podman, image build (Containerfile), Hipparcos catalogue,
                           station.toml/station.env/admin-token, Quadlet units:
                           optical-process, optical-agent, optical-web (+ .path),
                           optical-setup, optical-sync-ingest, optical-config-export,
                           optical-retention (+ host timer)
  tests/
    test_optical_sync.py   round-trips against the client's real Store
  examples/
    inventory.yml, group_vars/  placeholder values for a private repo
```

Inventory groups: `optical_acquisition` (the Pi) and `optical_processing`
(the VM).

### Acquisition role specifics

- Client installed into `/opt/satnogs-optical/venv` with
  `--system-site-packages`, so apt's `python3-picamera2`, `python3-numpy`,
  `python3-astropy`, `python3-sgp4` are used; pip installs only the client and
  the `satnogs-optical-schema` contract library from git at pinned commits.
- `optical-acquire.service` adapted from upstream: no `Requires=optical-gate`
  (acquire checks the clock itself), `RequiresMountsFor=` the NAS,
  `--raw-dir` on the NAS, the venv's python.
- Not deployed: gate, process, agent, web, setup, watchdog, retention, site.

### Processing role specifics

- Image: `python:3.11-slim-bookworm` + schema library + client
  `[pipeline,web]` at the pinned commit + `numpy<2`, Pillow,
  `cedar-solve==0.5.1 --no-deps` (same recipe as the upstream sandbox) +
  `hip_main.dat` (sha256-pinned) for generating the cedar database.
- Containers run as the service uid; `/etc/satnogs-optical`,
  `/var/lib/satnogs-optical` and the NAS mount are bind-mounted at the same
  paths; `/run/satnogs-optical` is a tmpfs.
- Web and setup listen on 8080 inside the container (non-root cannot bind 80),
  published on a configurable host port. Setup runs only while unclaimed
  (`ConditionPathExists=!.../identity/claimed.json`); `optical-web.path`
  starts the web UI once the claim lands.
- Agent: the upstream unit runs `elements`, `telemetry`, `tasks`,
  `config-pull` as `ExecStartPre` and a one-shot `upload`, restarted every
  60 s. The container runs the same sequence in a loop, re-reading
  `station.env` each cycle so a token written by the claim is picked up.
- Not deployed: gate (processing does not timestamp), watchdog (it supervises
  `optical-acquire`, which is not on this host), power, site.

### Station identity and secrets

- The claim (via the setup page on the VM) writes `identity/` (key, record,
  `claimed.json`), the token into `station.env`, and the id/name into
  `station.toml`.
- `station.toml` is templated by Ansible from `optical_station_id`. To keep a
  re-run from silently reverting a claim, the role fails if the file on disk
  carries a different non-zero id, and says which value to set.
- `station.env` is edited line by line (URL, DB); the token line is left alone.
- `backup_secrets.yml` fetches the identity files, `station.env` and
  `admin-token` into the private repo and vault-encrypts them; the processing
  role restores them when present.
- Not in git: the VM's store (measurements, upload cursor). Back it up
  separately (e.g. nightly SQLite backup to the NAS).

## Private repo (`optical-deploy-config`)

```
ansible.cfg
requirements.yml           jazzpi.satnogs_optical_split from GitHub, pinned
inventory.yml              our hosts
group_vars/all/main.yml    NAS export, NTP server, site, client commit, ports
group_vars/all/vault.yml   encrypted
secrets/                   vault-encrypted identity files (from backup_secrets.yml)
site.yml                   import_playbook: jazzpi.satnogs_optical_split.site
backup.yml                 import_playbook: jazzpi.satnogs_optical_split.backup_secrets
```

## Order of operations for a new setup

1. NAS export created, writable by the service uid.
2. `ansible-playbook site.yml --limit optical_processing`
3. Claim the station on the VM's setup page; set `optical_station_id`.
4. `ansible-playbook backup.yml` and commit the encrypted secrets.
5. `ansible-playbook site.yml --limit optical_acquisition`

## Status

- [x] `optical-acquire` verified to run standalone (synthetic source: no gate,
      agent, web or process running; creates its own store)
- [x] `optical_sync.py` and its tests (`PYTHONPATH=../client pytest tests`)
- [x] Collection skeleton (`galaxy.yml`, `meta/runtime.yml`, README, LICENSE)
- [x] `common` role
- [x] `nas_mount` role
- [x] `acquire` role
- [x] `processing` role
- [x] Processing image builds (Docker) and runs end to end: synthetic acquire ->
      publish -> ingest -> `optical-process` consumed the ingested stacks;
      cedar solver constructed, agent selftest OK
- [x] Playbooks (`site.yml`, `backup_secrets.yml`)
- [x] `examples/`
- [x] `git init` + remote + identity for `shared-deploy`
- [x] Private repo skeleton, `git init` + remote + identity (values are TODO)
- [x] Lint (`ansible-lint` production profile, `--syntax-check`) and every
      template rendered with the example variables

## Unverified / open

- The real `libcamera` driver against the HQ camera on Ubuntu (expected to
  work: same `picamera2` path as the stvid fork).
- Whether `optical-process` solves and measures ingested **sky** stacks
  (the end-to-end run used the synthetic source, which is not sky, so every
  stack was skipped for "no plate solution").
- `Notify=true` / `WatchdogSec=` through Quadlet on the VM's Podman version.
- Port publishing and the claim flow for the web and setup containers under
  Podman (they ran only as plain processes in the Docker test).
- Client pin: `v0.5.0` predates `frame_path`, so the default pin is a `main`
  commit (`d057e80`) until a newer release is tagged.
- The playbooks have only been linted, syntax-checked and template-rendered;
  they have not run against a real Pi or VM yet.
- Directories on the NAS are created as the service user (so `root_squash`
  is fine), which needs the export's top directory writable by that uid.
- `requirements.yml` in the private repo points at the GitHub repo's `main`,
  which exists only once `shared-deploy` is pushed.
- The web UI's focus-mode live view cannot work across hosts (it reads the
  Pi's tmpfs); stack previews from the FITS should.
