# Troubleshooting

## First-touch diagnostics

```bash
# Is the backend running?
sudo systemctl status nova-ve-backend

# Recent backend log:
sudo journalctl -u nova-ve-backend -n 200 --no-pager

# Is Caddy serving?
sudo systemctl status caddy

# Is Postgres up?
sudo systemctl status postgresql

# Is the Guacamole stack up?
sudo docker ps | grep -E 'guacd|guacamole|guacdb'

# Live API health check:
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8000/api/health   # → 200

# Frontend reachable through Caddy?
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1/                   # → 200
```

If the in-tree smoke check exists in the repo on the host, it bundles all of the above into one command:

```bash
sudo bash ~ubuntu/nova-ve-git/deploy/scripts/smoke-check.sh
```

## Symptom → first step

| Symptom | Most likely cause | First step |
|---|---|---|
| UI does not load (`Caddy: bad gateway`) | `nova-ve-backend` is down or returning 5xx. | `journalctl -u nova-ve-backend -n 100` |
| UI loads but every API call returns 401 | Session cookie expired or Postgres `novadb` is unreachable. | `sudo -u postgres psql -d novadb -c '\l'` |
| Node will not start (`docker: conflicting options`) | An older lab JSON specifies both `--rm` and `--restart`. | Already fixed on `main`; if still seen, pull the latest commit. |
| Console blank / disconnects | Guacamole stack flapping, or QEMU display set to `none`. | `sudo docker logs guacamole \| tail -100` and check the node's VNC config. |
| Link create fails with `interface_index=0 already attached to network N` | Orphan runtime attachment (a previous detach failure). | nova-ve auto-heals on the next link change; if it persists, restart the node. |
| Network name collision (HTTP 409 on create) | A network with the same display name already exists in the lab. | Pick a different name — the canvas's smart default auto-skips collisions. |
| Lab editor hangs while loading | Backend WebSocket failed to upgrade. | Check Caddy is forwarding `/ws/*` and that `nova-ve-backend` is reachable on `127.0.0.1:8000`. |

## Lab JSON files

```bash
# Read a lab JSON (root-owned, mode 0600):
sudo cat /var/lib/nova-ve/labs/<lab-name>.json | jq .

# Show every node in a lab:
sudo cat /var/lib/nova-ve/labs/<lab-name>.json | jq '.nodes | map({id, name, type})'

# Show every link:
sudo cat /var/lib/nova-ve/labs/<lab-name>.json | jq '.links'
```

If a lab JSON gets corrupt (e.g. partial write during a crash), the backend refuses to load it and emits a structured error in the log. There is no auto-repair — restore the JSON from your latest [backup](backups.md).

## Reset everything

This nukes labs, images, templates, and the database. Do not run on a production host.

```bash
# Stop services
sudo systemctl stop nova-ve-backend
sudo docker compose -f /home/ubuntu/nova-ve-git/docker-compose.yml down

# Wipe state
sudo rm -rf /var/lib/nova-ve/{labs,images,templates}
sudo install -d -o ubuntu -g ubuntu /var/lib/nova-ve/{labs,images,templates}

# Drop + recreate the database
sudo -u postgres psql -c 'DROP DATABASE IF EXISTS novadb;'
sudo -u postgres psql -c 'CREATE DATABASE novadb OWNER nova;'

# Re-run the installer to re-seed
curl -fsSL https://raw.githubusercontent.com/fahadysf/nova-ve/main/install.sh | sudo bash
```
