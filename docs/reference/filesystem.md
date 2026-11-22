# Filesystem layout

Every path nova-ve writes to or reads from, in one table.

## Application state

| Path | Owner | Mode | Purpose |
|---|---|---|---|
| `/var/lib/nova-ve/labs/` | `nova-ve` service user | 0755 | Lab JSON files. |
| `/var/lib/nova-ve/labs/<lab>.json` | `nova-ve` service user | 0644 (umask) | A single lab's topology. |
| `/var/lib/nova-ve/labs/<lab>.json.lock` | `nova-ve` service user | 0644 (umask) | Cooperative write lock. |
| `/var/lib/nova-ve/tmp/<lab>/<node-id>/` | `nova-ve` service user | 0755 | Per-node runtime state — qcow2 overlays, stdout/stderr logs, QMP sockets. |
| `/var/lib/nova-ve/tmp/node-runtime/` | `nova-ve` service user | 0755 | Persisted per-node runtime records (`<lab>-<node>.json`). |
| `/var/lib/nova-ve/runtime/pids.json` | `nova-ve` service user | 0644 | Registered process PIDs (survive backend restarts for re-adoption). |
| `/var/lib/nova-ve/system-settings.json` | `nova-ve` service user | 0644 (umask) | Persisted editable settings (apply at backend startup). |
| `/var/lib/nova-ve/iourc/` | `nova-ve` service user | 0700 | System IOURC license directory for IOL nodes. |
| `/var/lib/nova-ve/guacamole/` | root | 0755 | Guacamole compose stack state. |
| `/var/lib/nova-ve/images/` | `nova-ve` service user | 0755 | Vendor base images. |
| `/var/lib/nova-ve/images/<kind>/<key>/` | `nova-ve` service user | 0755 | Per-image bundle (qemu / dynamips / iol / docker). |
| `/var/lib/nova-ve/templates/` | `nova-ve` service user | 0755 | Node templates (importer output + hand-authored). |
| `/var/lib/nova-ve/templates/<vendor>/<key>.json` | `nova-ve` service user | 0644 | One template. |
| `/var/lib/nova-ve/www/` | `nova-ve` service user | 0755 | SvelteKit static build, served by Caddy. |
| `/var/lib/nova-ve/import-manifest.json` | root | 0644 | Last EVE-NG importer run summary. |

## Configuration

| Path | Owner | Mode | Purpose |
|---|---|---|---|
| `/etc/nova-ve/backend.env` | root | 0600 | Backend env file — DB DSN, secrets, admin bootstrap. |
| `/etc/sudoers.d/nova-ve` | root | 0440 | Visudo-validated `NOPASSWD:` sudoers fragment for the service user's privileged network helper calls. |
| `/etc/caddy/Caddyfile` (or fragment) | root | 0644 | Caddy reverse-proxy config for the SPA + WS. |
| `/etc/systemd/system/nova-ve-backend.service` | root | 0644 | Backend systemd unit. |

## Source repo

| Path | Purpose |
|---|---|
| `/var/lib/nova-ve/nova-ve-git/` | Clone of this repo; existing legacy `~${SUDO_USER}/nova-ve-git/` checkouts are reused during migration when present. |
| `/var/lib/nova-ve/nova-ve-git/backend/.venv/` | Backend Python venv. |
| `/var/lib/nova-ve/nova-ve-git/frontend/build/` | Frontend Vite build output (rsync'd into `/var/lib/nova-ve/www/`). |
| `/var/lib/nova-ve/nova-ve-git/deploy/scripts/` | Bootstrap / deploy / smoke-check scripts. |

## Privileged helper

| Path | Owner | Mode | Purpose |
|---|---|---|---|
| `/opt/nova-ve/bin/` | root | 0755 | Privileged helper binaries. |
| `/opt/nova-ve/bin/nova-ve-net.py` | root | 0755 | Bridge / veth / TAP / NAT / dnsmasq operations. Invoked via sudo from the backend. |
| `/opt/nova-ve/bin/nova-ve-console-proxy.py` | root | 0755 | Console TCP forwarder for Docker / Dynamips consoles. |
| `/opt/nova-ve/bin/nova-ve-backup.py` | root | 0755 | Netplan snapshot / restore used by Bridge-Cloud provisioning. |
| `/opt/nova-ve/qemu/<version>/` | root | 0755 | Side-by-side QEMU builds for per-node QEMU version selection (installed by `install.sh` from the `nova-ve-qemu` pack; optional). |

## What you can safely move

- `/var/lib/nova-ve/labs/` and `/var/lib/nova-ve/templates/` — both can be symlinked elsewhere, e.g. onto NFS, with no app changes.
- `/var/lib/nova-ve/images/` — symlinking is fine; the images directory is large and a separate-volume target is common.

## What you should *not* move

- `/etc/nova-ve/backend.env` — referenced by an absolute path in the systemd unit.
- `/opt/nova-ve/bin/` — the sudoers fragment matches this exact prefix.
- `/var/lib/nova-ve/www/` — Caddy is configured to serve this exact path.
