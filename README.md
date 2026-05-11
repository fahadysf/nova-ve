# nova-ve

> [!WARNING]
> Pre-Alpha Development Version: this project is not fit for actual lab use yet.
> Expect incomplete features, broken workflows, and data/runtime instability.

A clean-room network virtualization environment built with modern tooling.

## Stack

| Layer | Technology |
|-------|-----------|
| Frontend | Svelte 5 + SvelteKit + Svelte Flow + Tailwind CSS |
| Backend | FastAPI + Pydantic + SQLAlchemy (async) |
| Database | PostgreSQL (app data) + JSON files (lab topology) |
| Virtualization | QEMU/KVM + Docker |
| Console | Apache Guacamole (HTML5) |
| Reverse Proxy | Caddy |
| Base OS | Ubuntu 26.04 LTS |

## Quick Start

On a fresh Ubuntu 26.04 LTS x86_64 host with sudo access:

```bash
curl -fsSL https://raw.githubusercontent.com/fahadysf/nova-ve/main/install.sh | sudo bash
```

> **Full documentation** lives at <https://docs.nova-ve.com/> (built from `docs/`).
> A GitHub Pages mirror remains available at <https://fahadysf.github.io/nova-ve/>.
>
> **Migrating from EVE-NG / UNetLab / PNETLab?** See the [Importing from EVE-NG](docs/importing-eveng/index.md) section for the importer walk-through (default mode is non-destructive — sources preserved unless `--delete-source` is passed).

That single command:

1. Installs git, Docker, PostgreSQL, Caddy, Node.js, Python 3 + venv, build tooling, and other OS deps.
2. Clones this repo to `~${SUDO_USER}/nova-ve-git`.
3. Bootstraps the host PostgreSQL role/db (`nova` / `novadb`) and the dockerized Guacamole stack (`guacdb` / `guacd` / `guacamole`).
4. Builds the FastAPI backend (Python venv + Alembic migrations + initial seed).
5. Builds the SvelteKit frontend (`npm ci` + `vite build`) into `/var/lib/nova-ve/www`.
6. Installs the privileged network helper at `/opt/nova-ve/bin/` plus its `visudo`-validated sudoers fragment.
7. Drops systemd units (`nova-ve-backend`, `caddy`) and starts them.
8. Runs the in-tree smoke check.
9. Writes `nova-ve-install-summary.md` (mode `0600`) into the directory you launched the installer from — it contains the bootstrapped admin password, the location of every critical config file, and a map of the env vars in `/etc/nova-ve/backend.env`.

The script is idempotent: re-running it fast-forwards the repo, reapplies templates, and restarts services without rotating existing secrets.

Override knobs (env vars):

| Var | Default | Purpose |
|-----|---------|---------|
| `NOVA_VE_REPO_URL` | `https://github.com/fahadysf/nova-ve.git` | Source repo |
| `NOVA_VE_REPO_REF` | `main` | Branch / tag / SHA to check out |
| `NOVA_VE_REPO_DIR` | `~${SUDO_USER}/nova-ve-git` | Clone destination |
| `NOVA_VE_OWNER` | `${SUDO_USER:-ubuntu}` | UNIX user that owns the repo and runs the backend |

After install, open `http://<host>/` and log in with the admin credentials printed at the end of the run (also written to `nova-ve-install-summary.md`).

For development workflows (running the backend with `--reload`, the frontend dev server, or a Rancher Desktop loop on macOS), see the [Deployment contract](docs/development/deployment-contract.md) and the helper scripts in [`deploy/scripts/`](deploy/scripts/).

## Project Structure

```
nova-ve/
├── backend/
│   ├── app/
│   │   ├── models/          # SQLAlchemy models
│   │   ├── routers/         # FastAPI route handlers
│   │   ├── schemas/         # Pydantic models
│   │   ├── services/        # Business logic
│   │   ├── config.py        # Settings
│   │   ├── database.py      # Async DB engine
│   │   └── main.py          # App entrypoint
│   ├── labs/                # Sample lab JSON files
│   ├── alembic/             # Database migrations
│   └── requirements.txt
├── frontend/
│   ├── src/
│   │   ├── lib/
│   │   │   ├── components/  # Svelte components
│   │   │   ├── stores/      # Svelte stores (auth, etc.)
│   │   │   └── types/       # TypeScript types
│   │   └── routes/          # SvelteKit pages
│   └── package.json
├── docker-compose.yml
└── README.md
```

## License

Copyright (c) 2026 Fahad Yousuf <fahadysf [at] gmail.com>.

This repository is licensed under the `Apache License 2.0`.
See [LICENSE](LICENSE) and [NOTICE](NOTICE).
