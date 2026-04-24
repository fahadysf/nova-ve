# nova-ve

A clean-room network virtualization environment inspired by legacy platform, built with modern tooling.

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

### Local Development

1. **Start PostgreSQL**
   ```bash
   docker compose up -d db
   ```

2. **Install backend dependencies**
   ```bash
   cd backend
   python -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```

3. **Run backend**
   ```bash
   uvicorn app.main:app --reload --port 8000
   ```

4. **Install frontend dependencies**
   ```bash
   cd frontend
   npm install
   ```

5. **Run frontend**
   ```bash
   npm run dev
   ```

6. **Open** `http://localhost:5173`

### Local macOS + Rancher Desktop

For Docker-backed local lab testing on macOS with Rancher Desktop:

1. **Prepare the local runtime**
   ```bash
   ./deploy/scripts/setup-local-rancher.sh
   ```

2. **Run the backend**
   ```bash
   NOVA_VE_ENV_FILE=.omx/local/backend.env ./deploy/scripts/run-local-backend.sh
   ```

3. **Run the frontend**
   ```bash
   NOVA_VE_BACKEND_ORIGIN=http://127.0.0.1:8000 NOVA_VE_HTML5_ORIGIN=http://127.0.0.1:8081 NOVA_VE_FRONTEND_PORT=5174 ./deploy/scripts/run-local-frontend.sh
   ```

4. **Open** `http://127.0.0.1:5174` (or the next free port if `5174` is already in use)

This path prepares a repo-local env file, starts PostgreSQL and Guacamole via Docker, builds the Alpine demo image, and uses Rancher Desktop's Docker socket for local lab node runtime.

If you proxy the frontend to a non-local backend or Guacamole host during development, set both `NOVA_VE_BACKEND_ORIGIN` and `NOVA_VE_HTML5_ORIGIN` explicitly.

### Docker Compose (all services)

```bash
docker compose up --build
```

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

## Research

Detailed architecture studies of legacy platform are in `./research/`:
- `01_overview.md` — legacy platform architecture overview
- `12_runtime_api_observations.md` — Live API behavior documentation
- `13_clean_room_design.md` — Design decisions for this project

## License

Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>.

This repository is licensed under `PolyForm Noncommercial License 1.0.0`.
See [LICENSE](LICENSE) for the full text.

Commercial use is not granted under the public repository license.
Commercial licensing requires a separate agreement from the copyright holder.
