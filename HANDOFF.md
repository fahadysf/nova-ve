# Agent Handoff — nova-ve Project

> **Date:** 2026-04-21  
> **Last commit:** `d9be8d1` on `main`  
> **Status:** Issues #1, #2, #3, #10 closed. Issues #4–#9 open.  
> **Read this first, then README.md / CONTRIBUTING.md for conventions.**

---

## 1. Project State at a Glance

```
Closed Issues:  #1 (Auth), #2 (DB Migrations), #3 (Lab CRUD), #10 (Folder Mgmt)
Open Issues:    #4 (Node Lifecycle), #5 (Frontend API), #6 (Canvas), #7 (Templates),
                #8 (Console), #9 (Testing)
```

### What's Working Right Now
| Feature | Status | Tested |
|---------|--------|--------|
| PostgreSQL via Docker Compose | ✅ | `docker compose up -d db` |
| Alembic migrations | ✅ | `alembic upgrade head` applied |
| Admin seed (`admin`/`admin`) | ✅ | `python -m scripts.seed` |
| JWT + Cookie dual auth | ✅ | curl verified all endpoints |
| User CRUD (admin RBAC) | ✅ | POST/GET/PATCH/DELETE /api/users |
| Lab CRUD (create/update/delete) | ✅ | POST/PUT/DELETE /api/labs |
| Lab read (DB + filesystem) | ✅ | GET /api/labs/{path} |
| Folder listing | ✅ | GET /api/folders/ + /api/folders/{path} |
| Folder CRUD | ✅ | POST/PUT/DELETE /api/folders |
| Topology/Nodes/Networks read | ✅ | GET /api/labs/{path}/... |
| SvelteKit frontend (dev) | ✅ | `npm run check` passes 0 errors |
| TopologyCanvas (read-only) | ✅ | Renders nodes, edges, networks |

### What's Stubbed / Not Working
| Feature | Status | Location |
|---------|--------|----------|
| Node start/stop/wipe | STUB | `backend/app/routers/labs.py:137-170` |
| Frontend API integration | MOCK | Frontend uses sample data in places |
| Canvas interactivity | READ-ONLY | No drag-create, no connect, no save |
| Template system | MISSING | No `/api/templates` endpoint |
| Console access | MISSING | No telnet/rdp/HTML5 endpoints |
| User quotas | STORED ONLY | Not enforced anywhere |
| Redis | CONFIGURED ONLY | No client code exists |

---

## 2. How We Work — Issue-Based Development

### The Workflow (MANDATORY)

1. **Pick an open GitHub issue** — Start from the lowest number (#4 is next)
2. **Read the issue comment** — Each issue has an implementation notes comment with expected endpoints and design considerations
3. **Read the research docs** — `research/12_runtime_api_observations.md` has the legacy platform API spec; `research/13_clean_room_design.md` has architecture guidance
4. **Read README.md / CONTRIBUTING.md** — Coding conventions, file organization, auth patterns
5. **Implement** — Write code following existing patterns
6. **Test** — Use curl or FastAPI docs. Test both happy path and error cases.
7. **Add implementation notes to the GitHub issue** — Use the template in README.md / CONTRIBUTING.md §12
8. **Commit** — Reference the issue: `feat(scope): description. Closes #N`
9. **Push** — `git push origin main`
10. **Close the issue** — With a detailed comment explaining what was built

### GitHub Issues Quick Reference

| # | Title | Priority | Complexity | Files You'll Touch |
|---|-------|----------|------------|-------------------|
| 4 | Node lifecycle (start/stop/wipe QEMU/KVM) | HIGH | HIGH | `routers/labs.py`, `services/qemu_service.py` (new), `models/` |
| 5 | Frontend API integration | HIGH | MED | `frontend/src/lib/stores/`, `frontend/src/routes/labs/` |
| 6 | Interactive topology canvas | HIGH | MED | `frontend/src/lib/components/canvas/` |
| 7 | Template and image management | MED | MED | `routers/templates.py` (new), `models/template.py` (new) |
| 8 | Console access (telnet/rdp/HTML5) | MED | HIGH | `routers/console.py` (new), Guacamole integration |
| 9 | Testing + CI pipeline | MED | LOW | `backend/tests/`, `.github/workflows/ci.yml` |


### Issue Comments Already Have Implementation Notes

Every open issue (#4–#10) has a comment with:
- Expected endpoints and methods
- Current state of the codebase
- Implementation plan
- Key files to create/modify
- Design considerations

**Check the issue comments before you start coding.**

---

## 3. Environment Setup (Do This First)

### Prerequisites
- Python 3.12 (NOT 3.14 — `pydantic-core` will fail to build)
- Node.js 22+ (for frontend)
- Docker & Docker Compose (for PostgreSQL)

### One-Time Setup
```bash
cd /Users/fahad/code/nova-ve

# 1. Start PostgreSQL
docker compose up -d db

# 2. Backend venv (already exists at backend/.venv)
cd backend
source .venv/bin/activate

# 3. Apply migrations
alembic upgrade head

# 4. Seed admin user
python -m scripts.seed
# Output: "Created admin user: admin / admin"

# 5. Frontend dependencies (already installed)
cd ../frontend
npm install  # if needed
```

### Daily Dev Startup
```bash
# Terminal 1 — Backend
cd backend && source .venv/bin/activate && uvicorn app.main:app --reload

# Terminal 2 — Frontend
cd frontend && npm run dev

# Terminal 3 — PostgreSQL (if not running)
docker compose up -d db
```

### .env File Location
The `.env` file is at `backend/.env` (NOT project root). pydantic-settings reads from the working directory. Current contents:
```
SECRET_KEY=dev-secret-key-do-not-use-in-production
DEBUG=True
DATABASE_URL=postgresql+asyncpg://nova:nova@localhost:5432/novadb
REDIS_URL=redis://localhost:6379/0
BASE_DATA_DIR=./data
LABS_DIR=./labs
IMAGES_DIR=./images
TMP_DIR=./tmp
COOKIE_SECURE=False
COOKIE_SAMESITE=lax
```

**Do NOT move `.env` to project root** — it won't be picked up when running from `backend/`.

---

## 4. Architecture Deep Dive

### Directory Map

```
nova-ve/
├── README.md / CONTRIBUTING.md              # ← READ THIS for conventions
├── HANDOFF.md             # ← YOU ARE HERE
├── docker-compose.yml     # PostgreSQL + backend + frontend services
├── research/              # 13 docs — THE SPEC for legacy platform behavior
│   ├── 12_runtime_api_observations.md  # ← Primary reference for API shapes
│   └── 13_clean_room_design.md         # ← Architecture blueprint
│
├── backend/
│   ├── .venv/             # Python 3.12 virtualenv
│   ├── .env               # Local dev config (pydantic-settings)
│   ├── alembic/           # Migrations
│   │   ├── env.py         # Async alembic config
│   │   └── versions/      # Migration files
│   ├── labs/              # Lab JSON files live here
│   │   └── pa-fw-ipsec-gre-lab.json   # Sample lab
│   ├── app/
│   │   ├── main.py        # FastAPI entrypoint, CORS, router includes
│   │   ├── config.py      # Settings (pydantic-settings, reads .env)
│   │   ├── database.py    # Async engine, sessionmaker, get_db()
│   │   ├── dependencies.py # get_current_user, get_current_admin
│   │   ├── core/          # Security utilities, constants
│   │   │   ├── security.py   # JWT create/verify, Argon2 hash/verify
│   │   │   └── constants.py  # UserRole, ExtAuth enums
│   │   ├── models/        # SQLAlchemy ORM models
│   │   │   ├── __init__.py
│   │   │   ├── user.py
│   │   │   ├── lab.py
│   │   │   ├── pod.py
│   │   │   └── html5_session.py
│   │   ├── schemas/       # Pydantic request/response models
│   │   │   ├── auth.py
│   │   │   ├── user.py
│   │   │   ├── lab.py
│   │   │   ├── node.py
│   │   │   ├── network.py
│   │   │   ├── topology.py
│   │   │   ├── folder.py
│   │   │   └── system.py
│   │   ├── routers/       # FastAPI route handlers
│   │   │   ├── auth.py    # Login, register, logout, me
│   │   │   ├── users.py   # User CRUD with RBAC
│   │   │   ├── labs.py    # Lab CRUD + node/network read + start/stop stubs
│   │   │   ├── folders.py # Folder listing
│   │   │   └── system.py  # Status, settings, poll
│   │   └── services/      # Business logic
│   │       ├── auth_service.py  # AuthService (authenticate, session mgmt)
│   │       └── lab_service.py   # LabService (hybrid DB + file persistence)
│   └── scripts/
│       └── seed.py        # Creates admin/admin user
│
└── frontend/
    ├── package.json
    ├── vite.config.ts     # Dev server + API proxy to localhost:8000
    ├── src/
    │   ├── app.css        # Tailwind directives + dark theme
    │   ├── lib/
    │   │   ├── types/
    │   │   │   └── index.ts    # NodeData, NetworkData, TopologyLink
    │   │   ├── stores/
    │   │   │   └── auth.ts     # authStore, login(), logout(), checkAuth()
    │   │   └── components/
    │   │       └── canvas/
    │   │           ├── TopologyCanvas.svelte
    │   │           ├── CustomNode.svelte
    │   │           └── NetworkNode.svelte
    │   └── routes/
    │       ├── +layout.svelte
    │       ├── login/+page.svelte
    │       ├── labs/+page.svelte
    │       └── labs/[id]/+page.svelte
```

### Data Flow

```
User → SvelteKit Frontend → FastAPI Backend → PostgreSQL (metadata)
                                      ↓
                                  JSON Files (lab topology)
                                      ↓
                              QEMU/KVM/Docker (virtualization)
```

### Auth Flow

```
Browser:
  1. POST /api/auth/login → sets cookies (nova_session, nova_user)
  2. All subsequent requests auto-send cookies (credentials: 'include')
  3. Server validates cookie session against DB

API Client:
  1. POST /api/auth/login → extract access_token from response JSON
  2. Send Authorization: Bearer <token> header on all requests
  3. Server falls back to JWT validation if cookies missing
```

---

## 5. Current Database Schema

Tables managed by Alembic (migration `37fcfbd00d8a`):

### `users`
- PK: `username` (String 64)
- Fields: email, name, role, extauth, password_hash (Argon2)
- Quotas: ram, cpu, sat, expiration, datestart, diskusage
- Session: session_token, session_expires
- Runtime: ip, online, folder, lab, pod, html5

### `labs`
- PK: `id` (UUID)
- FK: `owner` → users.username
- Fields: filename, name, path, author, description, body, version, scripttimeout, countdown, linkwidth, grid, lock

### `pods`
- PK: `id` (Integer)
- FK: `username` → users.username
- FK: `lab_id` → labs.id
- Fields: expiration

### `html5_sessions`
- Composite PK: (`username`, `connection_id`)
- Fields: pod, token, expires_at

---

## 6. Testing Cheat Sheet

### Backend Import Check
```bash
cd backend && source .venv/bin/activate
python -c "from app.main import app; print('OK', len(app.routes), 'routes')"
# Should print: OK 36 routes (or more as you add)
```

### Frontend Type Check
```bash
cd frontend && npm run check
# Must pass: 0 errors, 0 warnings
```

### Manual API Testing (curl)
```bash
# Login and get JWT
TOKEN=$(curl -s -X POST http://localhost:8000/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"admin","password":"admin"}' | jq -r '.access_token')

# Use JWT for authenticated requests
curl -s http://localhost:8000/api/auth/me \
  -H "Authorization: Bearer $TOKEN"

# Or use cookies
curl -s -c cookies.txt -b cookies.txt http://localhost:8000/api/auth
```

### FastAPI Auto-Docs
Open `http://localhost:8000/docs` when backend is running. All endpoints are documented there.

---

## 7. Key Patterns to Follow

### Adding a Protected Route
```python
from fastapi import APIRouter, Depends
from app.dependencies import get_current_user, get_current_admin
from app.schemas.user import UserRead

router = APIRouter(prefix="/api/something", tags=["something"])

@router.get("/")
async def list_things(
    current_user: UserRead = Depends(get_current_user),  # Any authenticated user
):
    return {"code": 200, "status": "success", "message": "...", "data": {}}

@router.post("/")
async def create_thing(
    current_user: UserRead = Depends(get_current_admin),  # Admin only
):
    return {"code": 200, "status": "success", "message": "..."}
```

### Hybrid DB + File Persistence (Lab Pattern)
```python
# For features that need both DB metadata and file storage:
# 1. Create/update/delete DB record via SQLAlchemy
# 2. Create/update/delete file on disk via standard file I/O
# 3. Use transactions carefully — DB can rollback, files cannot
# 4. See LabService for the reference implementation
```

### legacy platform Response Format
```python
# ALWAYS wrap responses like this:
return {
    "code": 200,
    "status": "success",  # or "fail", "unauthorized", "error"
    "message": "Human readable (code).",
    "data": { ... },  # optional
}
```

### Path Sanitization
```python
from pathlib import Path
from app.config import get_settings

settings = get_settings()

def safe_path(filename: str) -> Path:
    labs_dir = settings.LABS_DIR.resolve()
    safe_name = Path(filename).name  # Strip directories
    filepath = (labs_dir / safe_name).resolve()
    if not str(filepath).startswith(str(labs_dir)):
        raise ValueError("Directory traversal detected")
    return filepath
```

---

## 8. Common Pitfalls (Learned the Hard Way)

| Problem | Cause | Fix |
|---------|-------|-----|
| Empty Alembic migration | Model not imported in `alembic/env.py` | Add `from app.models import User, LabMeta, ...` to env.py |
| `ImportError: cannot import Base` | `app/models/__init__.py` missing `Base` export | Ensure `from app.database import Base` is in `__init__.py` |
| Svelte Flow `style` error | Passing JS object instead of CSS string | Use `style="background: #333"` not `style={{ background: '#333' }}` |
| Auth 401 on frontend | Missing `credentials: 'include'` on fetch | Always include it on API calls |
| `.env` not loaded | File in wrong location | Must be at `backend/.env`, not project root |
| Permission denied on `/var/lib` | Default `LABS_DIR` points to system path | Use `backend/.env` with `LABS_DIR=./labs` |
| Python 3.14 build failure | `pydantic-core` uses PyO3 (max 3.13) | Always use Python 3.12 |

---

## 9. What to Work On Next

### Recommended Order

1. **#4 — Node Lifecycle (start/stop/wipe)**
   - This is the highest priority open issue
   - Requires QEMU/KVM subprocess management
   - Will need a new `services/qemu_service.py`
   - See `research/06_virtualization.md` for QEMU invocation patterns

2. **#5 — Frontend API Integration**
   - Wire up the SvelteKit frontend to real backend endpoints
   - Replace mock data with real API calls
   - Add error handling and loading states

3. **#6 — Interactive Canvas**
   - Make TopologyCanvas editable (drag-create, connect, save)
   - Add node/network creation UI
   - Save positions on drag-end

4. **#9 — Testing + CI**
   - Easiest win — adds pytest fixtures and GitHub Actions
   - Will catch regressions as complexity grows

### Quick Wins
- #9 (Testing) — Well-defined scope, no runtime dependencies

### Avoid for Now
- #8 (Console/Guacamole) — Complex, requires external service
- #7 (Templates) — Depends on #4 (node lifecycle) being solid first

---

## 10. External Resources

| Resource | Location | Purpose |
|----------|----------|---------|
| legacy platform API spec | `research/12_runtime_api_observations.md` | Exact request/response shapes |
| Architecture blueprint | `research/13_clean_room_design.md` | Stack choices, roadmap, models |
| Virtualization details | `research/06_virtualization.md` | QEMU args, Docker, networking |
| Agent conventions | `README.md / CONTRIBUTING.md` | Coding standards, workflow, pitfalls |
| GitHub Issues | `gh issue list` | Tracks all work items |

---

## 11. Contact / Context

- **Repo:** `fahadysf/nova-ve` (private GitHub)
- **Branch:** `main` (trunk-based development)
- **Commits:** Push directly to `main` (solo project)
- **DB:** PostgreSQL 16 via Docker Compose on port 5432
- **Backend:** `http://localhost:8000`
- **Frontend:** `http://localhost:5173`
- **FastAPI Docs:** `http://localhost:8000/docs`

---

*End of handoff. Read README.md / CONTRIBUTING.md next, then pick an issue and start building.*
