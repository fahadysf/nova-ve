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

Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>. All rights reserved.
