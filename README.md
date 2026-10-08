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
- Processing host: Ubuntu/Debian with Podman ≥ 4.4 (Quadlet).
- A NAS export (NFS or SMB) both hosts can mount, writable by the service
  user's uid (same uid on both hosts, default 1990).
- An NTP server both hosts can reach. The Pi will not capture until chrony
  reports a validated clock.

## Usage

Keep your inventory, variables and secrets in a private repository and
install this collection there:

```yaml
# requirements.yml
collections:
  - name: https://github.com/jazzpi/satnogs-optical-shared-deploy.git
    type: git
    version: main
```

```yaml
# site.yml
- ansible.builtin.import_playbook: gos.satnogs_optical.site
```

`examples/` has an inventory and variables to start from. Every variable is
documented in the `defaults/main.yml` of the role that uses it.

```bash
ansible-galaxy collection install -r requirements.yml
ansible-playbook site.yml --ask-vault-pass
```

## Development

The sync script's tests run against the client's real store:

```bash
PYTHONPATH=/path/to/satnogs-optical-client pytest tests
```

## License

AGPL-3.0-or-later, the same as the SatNOGS Optical client.
