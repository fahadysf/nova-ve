# Filesystem layout

Every path nova-ve writes to or reads from, in one table.

## Application state

| Path | Owner | Mode | Purpose |
|---|---|---|---|
| `/var/lib/nova-ve/labs/` | `nova-ve` app owner | 0700 | Lab JSON files + per-node runtime state. |
| `/var/lib/nova-ve/labs/<lab>.json` | root | 0600 | A single lab's topology. |
| `/var/lib/nova-ve/labs/<lab>.json.lock` | root | 0600 | Cooperative write lock. |
| `/var/lib/nova-ve/labs/<lab>/<node-id>/` | root | 0700 | Per-node runtime state (qcow2 overlays, console-log files). |
| `/var/lib/nova-ve/images/` | app owner | 0755 | Vendor base images. |
| `/var/lib/nova-ve/images/<kind>/<key>/` | app owner | 0755 | Per-image bundle (qemu / dynamips / iol / docker). |
| `/var/lib/nova-ve/templates/` | app owner | 0755 | Node templates (importer output + hand-authored). |
| `/var/lib/nova-ve/templates/<vendor>/<key>.json` | app owner | 0644 | One template. |
| `/var/lib/nova-ve/www/` | app owner | 0755 | SvelteKit static build, served by Caddy. |
| `/var/lib/nova-ve/import-manifest.json` | root | 0644 | Last EVE-NG importer run summary. |

## Configuration

| Path | Owner | Mode | Purpose |
|---|---|---|---|
| `/etc/nova-ve/backend.env` | root | 0600 | Backend env file — DB DSN, secrets, admin bootstrap. |
| `/etc/sudoers.d/nova-ve` | root | 0440 | Visudo-validated sudoers fragment for the privileged network helper. |
| `/etc/caddy/Caddyfile` (or fragment) | root | 0644 | Caddy reverse-proxy config for the SPA + WS. |
| `/etc/systemd/system/nova-ve-backend.service` | root | 0644 | Backend systemd unit. |

## Source repo

| Path | Purpose |
|---|---|
| `~${SUDO_USER}/nova-ve-git/` | Clone of this repo (default `~/ubuntu/nova-ve-git/`). |
| `~${SUDO_USER}/nova-ve-git/backend/.venv/` | Backend Python venv. |
| `~${SUDO_USER}/nova-ve-git/frontend/build/` | Frontend Vite build output (rsync'd into `/var/lib/nova-ve/www/`). |
| `~${SUDO_USER}/nova-ve-git/deploy/scripts/` | Bootstrap / deploy / smoke-check scripts. |

## Privileged helper

| Path | Owner | Mode | Purpose |
|---|---|---|---|
| `/opt/nova-ve/bin/` | root | 0755 | Privileged network helper binaries. |
| `/opt/nova-ve/bin/nova-ve-net` | root | 0755 | Bridge / veth / TAP operations. Invoked via sudo from the backend. |

## What you can safely move

- `/var/lib/nova-ve/labs/` and `/var/lib/nova-ve/templates/` — both can be symlinked elsewhere, e.g. onto NFS, with no app changes.
- `/var/lib/nova-ve/images/` — symlinking is fine; the images directory is large and a separate-volume target is common.

## What you should *not* move

- `/etc/nova-ve/backend.env` — referenced by an absolute path in the systemd unit.
- `/opt/nova-ve/bin/` — the sudoers fragment matches this exact prefix.
- `/var/lib/nova-ve/www/` — Caddy is configured to serve this exact path.
