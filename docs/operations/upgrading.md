# Upgrading

`install.sh` is idempotent. To upgrade to the latest `main`, re-run it on the host:

```bash
curl -fsSL https://raw.githubusercontent.com/fahadysf/nova-ve/main/install.sh | sudo bash
```

What re-running does:

1. Fast-forwards the cloned repo at `${NOVA_VE_REPO_DIR}` (default `~${SUDO_USER}/nova-ve-git`).
2. Re-runs `provision-ubuntu-2604.sh` to top up OS-level packages.
3. Reapplies the backend venv (`pip install -r requirements.txt`) and Alembic migrations.
4. Rebuilds the frontend (`npm ci && vite build`) and rsyncs it into `/var/lib/nova-ve/www`.
5. Restarts `nova-ve-backend` and `caddy`.

It does **not** rotate existing secrets, drop the database, or touch lab JSON files. The smoke check at the end exits non-zero if anything broke.

## Pinning to a specific release

Set `NOVA_VE_REPO_REF` before piping the installer:

```bash
NOVA_VE_REPO_REF=v0.4.0 curl -fsSL ... | sudo bash
```

`NOVA_VE_REPO_REF` accepts a branch, tag, or SHA.

## Manual upgrade (without the installer)

If you prefer to drive the upgrade yourself:

```bash
cd ~ubuntu/nova-ve-git
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

The Caddy unit reloads itself on file changes and does not need restarting after a frontend rebuild.

## What can go wrong

- **Alembic migration fails** — investigate with `journalctl -u nova-ve-backend -n 100`. Common cause: out-of-order migration heads after a long detour. The contract is that `nova-ve-backend` does **not** self-migrate; you must `alembic upgrade head` manually before restarting.
- **Frontend build fails** — usually a Node version drift. The provisioner installs Node 22.x from NodeSource; if you bumped Node manually, ensure it still satisfies `package.json`'s `engines` field.
- **Caddy refuses to start** — port 80 / 443 already in use. `sudo ss -ltnp | grep -E ':(80|443) '` finds the offender.

When in doubt, [Troubleshooting](troubleshooting.md) has the recipes.
