# Quick start

On a fresh Ubuntu 26.04 LTS x86_64 host with sudo access:

```bash
curl -fsSL https://raw.githubusercontent.com/fahadysf/nova-ve/main/install.sh | sudo bash
```

That single command:

1. Installs git, Docker, PostgreSQL, Caddy, Node.js, Python 3 + venv, `dnsmasq`, `nftables`, build tooling, and other OS deps.
2. Creates/reuses the dedicated `nova-ve` service user and clones this repo to `/var/lib/nova-ve/nova-ve-git` by default.
3. Bootstraps the host PostgreSQL role/db (`nova` / `novadb`) and the dockerized Guacamole stack (`guacdb` / `guacd` / `guacamole`).
4. Builds the FastAPI backend (Python venv + Alembic migrations + initial seed).
5. Builds the SvelteKit frontend (`npm ci` + `vite build`) into `/var/lib/nova-ve/www`.
6. Installs the privileged network helper at `/opt/nova-ve/bin/` plus its `visudo`-validated sudoers fragment, including NAT-Cloud DHCP, NAT, and forwarding verbs.
7. Drops systemd units (`nova-ve-backend`, `caddy`) and starts them.
8. Reconciles existing NAT-Cloud bridge state on upgraded hosts.
9. Runs the in-tree smoke check.
10. Writes `nova-ve-install-summary.md` (mode `0600`) into the directory you launched the installer from. It contains the bootstrapped admin password, the location of every critical config file, and a map of the env vars in `/etc/nova-ve/backend.env`.

The script is idempotent: re-running it fast-forwards the repo, reapplies templates, reinstalls the helper, reconciles NAT-Cloud runtime state for existing bridges, and restarts services without rotating existing secrets.

## Override knobs

Set any of these as env vars before piping into `sudo bash`:

| Var | Default | Purpose |
|-----|---------|---------|
| `NOVA_VE_REPO_URL` | `https://github.com/fahadysf/nova-ve.git` | Source repo |
| `NOVA_VE_REPO_REF` | `main` | Branch / tag / SHA to check out |
| `NOVA_VE_REPO_DIR` | `/var/lib/nova-ve/nova-ve-git` | Clone destination; legacy `~${SUDO_USER}/nova-ve-git` checkouts are reused during migration when present |
| `NOVA_VE_SERVICE_USER` | `nova-ve` | Dedicated UNIX service user that owns runtime state and runs the backend |
| `NOVA_VE_OWNER` | (unset) | Compatibility alias for `NOVA_VE_SERVICE_USER` |
| `NOVA_VE_SKIP_DEMO_IMAGES` | (unset) | Set to `1` to skip building the bundled `nova-ve/alpine-telnet:latest` demo image |

## After install

Open `http://<host>/` and log in with the admin credentials printed at the end of the run. The credentials are also written to `nova-ve-install-summary.md` — see [First login](first-login.md).

For development workflows (running the backend with `--reload`, the frontend dev server, or a Rancher Desktop loop on macOS), see [Deployment contract](../development/deployment-contract.md) and the helper scripts in `deploy/scripts/`.
