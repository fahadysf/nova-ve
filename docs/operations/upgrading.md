# Upgrading

`install.sh` is idempotent. To upgrade to the latest `main`, re-run it on the host:

```bash
curl -fsSL https://raw.githubusercontent.com/fahadysf/nova-ve/main/install.sh | sudo bash
```

What re-running does:

1. Stages the selected Git revision in a temporary root-owned directory. The installer updates the dedicated service user's application checkout at `${NOVA_VE_REPO_DIR}` to the same commit. The default checkout is `/var/lib/nova-ve/nova-ve-git`, and the installer can reuse a legacy checkout.
2. Runs privileged provisioning from the root-owned source and migrates `/var/lib/nova-ve` ownership to the service user.
3. Updates the backend virtual environment and runs Alembic migrations as the service user.
4. Rebuilds and publishes the frontend to `/var/lib/nova-ve/www` as the service user.
5. Reinstalls the privileged network helper and sudoers fragment, including NAT-Cloud DHCP, NAT, and forwarding verbs.
6. Restarts Docker, `nova-ve-backend`, and `caddy`.
7. Reconciles existing NAT-Cloud bridges from lab JSON files, re-applying IPv4 forwarding, NAT, DOCKER-USER forwarding rules, and dnsmasq leases where the bridge already exists.

It does **not** rotate existing secrets, drop the database, or touch lab JSON files. The upgraded backend also reconciles deterministic Docker node names at start time: a running same-name nova-ve container is adopted into runtime state, while a stopped stale same-name container is removed before `docker run`.

When upgrading an older host that ran `nova-ve-backend` as the installer user, the installer migrates the unit, repo, and `/var/lib/nova-ve` ownership to the dedicated service user. Already-running QEMU nodes may still be owned by the old user until they are stopped; the privileged helper includes a registry-authorized QEMU signal path so the new backend can still stop those migrated runtimes.

The smoke check at the end exits non-zero if anything broke.

If the service checkout has tracked edits or cannot fast-forward to the selected revision, the installer preserves those files and stops before provisioning. Set aside local changes or choose a clean application checkout before retrying. If the upstream ref changes during preparation, retry so the application and privileged inputs come from the same commit.

## Pinning to a specific release

Set `release_ref` to your chosen revision, then pass it to the installer:

```bash
curl -fsSL https://raw.githubusercontent.com/fahadysf/nova-ve/main/install.sh | sudo NOVA_VE_REPO_REF="${release_ref}" bash
```

`NOVA_VE_REPO_REF` accepts a branch, tag, or SHA. The selected revision must support separate provisioning and application checkouts. The installer checks this before updating the application.

## Manual upgrade (without the installer)

Run the Git and application commands as the service user for a manual upgrade:

```bash
cd /var/lib/nova-ve/nova-ve-git
git fetch origin
git checkout --detach origin/main  # or a fetched tag or commit SHA

# Backend
cd backend
.venv/bin/pip install -r requirements.txt
.venv/bin/alembic upgrade head

# Frontend
cd ../frontend
npm ci
npm run build
rsync -a --delete build/ /var/lib/nova-ve/www/
```

Restart the backend from the administrator account after the application update:

```bash
sudo systemctl restart nova-ve-backend
```

The manual path covers application code, migrations, and the static frontend. Use the installer to update privileged helpers, sudoers rules, or installer-managed networking. It stages privileged inputs separately from the application checkout.

Caddy serves `/var/lib/nova-ve/www` from disk on each request, so a frontend rebuild needs no Caddy restart or reload.

## What can go wrong

- **Alembic migration fails** — investigate with `journalctl -u nova-ve-backend -n 100`. Common cause: out-of-order migration heads after a long detour. The contract is that `nova-ve-backend` does **not** self-migrate; you must `alembic upgrade head` manually before restarting.
- **Frontend build fails** — usually a Node version drift. The provisioner installs the Ubuntu distro Node.js/npm packages; if you upgraded Node manually, verify `vite build` still runs clean.
- **Caddy refuses to start** — port 80 / 443 already in use. `sudo ss -ltnp | grep -E ':(80|443) '` finds the offender.
- **NAT-Cloud clients get DHCP but no internet** — re-run the installer so the current helper is installed and NAT-Cloud reconciliation reapplies IPv4 forwarding, masquerade NAT, Docker `DOCKER-USER` forwarding rules, and `dnsmasq` state for existing bridges.
- **Docker node start fails with a container-name conflict** — make sure the backend is upgraded and restarted. The current runtime path adopts a running same-name nova-ve container or removes a stopped stale same-name container before starting the node.

When in doubt, [Troubleshooting](troubleshooting.md) has the recipes.
