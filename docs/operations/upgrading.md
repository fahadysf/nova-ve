# Upgrading

`install.sh` is idempotent. To upgrade to the latest `main`, re-run it on the host:

```bash
curl -fsSL https://raw.githubusercontent.com/fahadysf/nova-ve/main/install.sh | sudo bash
```

What re-running does:

1. Creates/reuses the dedicated `nova-ve` service user, then fast-forwards the cloned repo at `${NOVA_VE_REPO_DIR}` (default `/var/lib/nova-ve/nova-ve-git`; legacy `~${SUDO_USER}/nova-ve-git` checkouts are reused during migration when present).
2. Re-runs `provision-ubuntu-2604.sh` to top up OS-level packages and migrate `/var/lib/nova-ve` ownership to the service user.
3. Reapplies the backend venv (`pip install -r requirements.txt`) and Alembic migrations.
4. Rebuilds the frontend (`npm ci && vite build`) and rsyncs it into `/var/lib/nova-ve/www`.
5. Reinstalls the privileged network helper and sudoers fragment, including NAT-Cloud DHCP, NAT, and forwarding verbs.
6. Restarts Docker, `nova-ve-backend`, and `caddy`.
7. Reconciles existing NAT-Cloud bridges from lab JSON files, re-applying IPv4 forwarding, NAT, DOCKER-USER forwarding rules, and dnsmasq leases where the bridge already exists.

It does **not** rotate existing secrets, drop the database, or touch lab JSON files. The upgraded backend also reconciles deterministic Docker node names at start time: a running same-name nova-ve container is adopted into runtime state, while a stopped stale same-name container is removed before `docker run`.

When upgrading an older host that ran `nova-ve-backend` as the installer user, the installer migrates the unit, repo, and `/var/lib/nova-ve` ownership to the dedicated service user. Already-running QEMU nodes may still be owned by the old user until they are stopped; the privileged helper includes a registry-authorized QEMU signal path so the new backend can still stop those migrated runtimes.

The smoke check at the end exits non-zero if anything broke.

## Pinning to a specific release

Set `NOVA_VE_REPO_REF` before piping the installer:

```bash
NOVA_VE_REPO_REF=v0.4.0 curl -fsSL ... | sudo bash
```

`NOVA_VE_REPO_REF` accepts a branch, tag, or SHA.

## Manual upgrade (without the installer)

If you prefer to drive the upgrade yourself:

```bash
cd /var/lib/nova-ve/nova-ve-git
git fetch origin
git checkout v0.4.0          # or main, or a specific SHA

# Backend
cd backend
.venv/bin/pip install -r requirements.txt
.venv/bin/alembic upgrade head

# Frontend
cd ../frontend
npm ci
npm run build
sudo rsync -a --delete build/ /var/lib/nova-ve/www/

# Restart
sudo systemctl restart nova-ve-backend
```

The manual path above is enough for application code, migrations, and the static frontend. It does not reinstall `/opt/nova-ve/bin/nova-ve-net.py`, refresh `/etc/sudoers.d/nova-ve`, or run the installer-side NAT-Cloud bridge reconciliation. Use the full installer path when upgrading across host-networking changes such as NAT-Cloud DHCP/NAT/forwarding or Docker runtime reconciliation.

The Caddy unit reloads itself on file changes and does not need restarting after a frontend rebuild.

## What can go wrong

- **Alembic migration fails** — investigate with `journalctl -u nova-ve-backend -n 100`. Common cause: out-of-order migration heads after a long detour. The contract is that `nova-ve-backend` does **not** self-migrate; you must `alembic upgrade head` manually before restarting.
- **Frontend build fails** — usually a Node version drift. The provisioner installs Node 22.x from NodeSource; if you bumped Node manually, ensure it still satisfies `package.json`'s `engines` field.
- **Caddy refuses to start** — port 80 / 443 already in use. `sudo ss -ltnp | grep -E ':(80|443) '` finds the offender.
- **NAT-Cloud clients get DHCP but no internet** — re-run the installer so the current helper is installed and NAT-Cloud reconciliation reapplies IPv4 forwarding, masquerade NAT, Docker `DOCKER-USER` forwarding rules, and `dnsmasq` state for existing bridges.
- **Docker node start fails with a container-name conflict** — make sure the backend is upgraded and restarted. The current runtime path adopts a running same-name nova-ve container or removes a stopped stale same-name container before starting the node.

When in doubt, [Troubleshooting](troubleshooting.md) has the recipes.
