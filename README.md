# gos.satnogs_optical

An Ansible collection that deploys a [SatNOGS Optical](https://wiki.satnogs.org/SatNOGS_Optical)
station split across two hosts that share a NAS:

- an **acquisition host** — a Raspberry Pi running Ubuntu, with the camera.
  It runs only `optical-acquire`, natively, and writes FITS stacks straight
  to the NAS. Nothing large is written to its SD card.
- a **processing host** — any x86_64 Linux machine or VM with Podman. It runs
  processing, the network agent, the web UI, the claim page and retention as
  containers managed by systemd (Quadlet).

The upstream client and its SD-card image put all of this on one Pi. This
collection deploys the **unmodified** client; the only extra code is
`optical_sync.py`, which carries stack rows from the Pi to the VM and
configuration from the VM back to the Pi as small files on the NAS (SQLite
must not be shared over a network filesystem).

```
Pi                                 NAS                          VM
optical-acquire --FITS---------->  raw/  <---------------------  optical-process, optical-retention
Pi store --optical-sync-publish--> exchange/outbox/ --ingest-->  VM store
Pi store <--optical-config-pull--  exchange/config.json <------  VM store (web UI edits)
```

## Requirements

- Ansible ≥ 2.15 on the machine you deploy from.
- Acquisition host: Raspberry Pi with Ubuntu, a camera supported by the
  client's `libcamera` driver (e.g. the Raspberry Pi HQ camera), enabled in
  the host's boot config.
- Processing host: x86_64 Ubuntu 24.04+ or Debian 13+ (Podman ≥ 4.4 for
  Quadlet; Debian 12 ships 4.3).
- A NAS export (NFS or SMB) both hosts can mount, writable by the service
  user's uid (same uid on both hosts, default 1990).
- An NTP server both hosts can reach. The Pi will not capture until chrony
  reports a validated clock.

## Usage

Keep your inventory, variables and secrets in a private repository that
installs this collection. `examples/` is a complete starting point: copy it,
then fill in `inventory.yml` and `group_vars/all/main.yml`. Every variable is
documented in the `defaults/main.yml` of the role that uses it.

The two inventory groups are `optical_processing` and `optical_acquisition`.

### First deployment

1. Create the NAS export, writable by uid/gid 1990 (`optical_uid`).
2. Install the collection and deploy the processing host:

   ```bash
   ansible-galaxy collection install -r requirements.yml
   ansible-playbook site.yml --limit optical_processing
   ```

3. Claim the station on the claim page at `http://<processing host>:8080/`.
   The web UI replaces it on the same port once the claim lands.
4. Set `optical_station_id` to the id the claim assigned, and back up the
   identity it created:

   ```bash
   ansible-playbook backup.yml
   git add secrets && git commit
   ```

5. Deploy the acquisition host:

   ```bash
   ansible-playbook site.yml --limit optical_acquisition
   ```

6. In the web UI, set the lens focal length and the camera's pixel size.
   Without them the plate solver does not know the field of view.

### Restoring

Run `site.yml` against the rebuilt host. On the processing host the
identity, token and admin token come back from `secrets/` (only where the
host has none). The processing host's database holds the measurements and
the upload cursor; it is not in git, so back it up separately.

### What runs where

| Host | Unit | Does |
|---|---|---|
| acquisition | `optical-acquire` | camera -> FITS on the NAS, rows in the local store |
| acquisition | `optical-sync-publish` | new rows -> `exchange/outbox/` |
| acquisition | `optical-config-pull` | `exchange/config.json` -> local store, restarts acquisition |
| processing | `optical-process` | detect, plate-solve, link, identify |
| processing | `optical-agent` | elements, telemetry, tasks, config and uploads to the network |
| processing | `optical-web` / `optical-setup` | web UI on `optical_web_port`, or the claim page until claimed |
| processing | `optical-sync-ingest` | `exchange/outbox/` -> local store |
| processing | `optical-config-export` | local store -> `exchange/config.json` |
| processing | `optical-retention.timer` | hourly sweep of old FITS on the NAS |

Not deployed: the clock gate (acquisition checks the clock itself), the
watchdog and power units (they manage a single-host station), and the
focus-mode live view, which cannot work across hosts.

## Development

The sync script's tests run against the client's real store:

```bash
PYTHONPATH=/path/to/satnogs-optical-client pytest tests
```

## License

AGPL-3.0-or-later, the same as the SatNOGS Optical client.
