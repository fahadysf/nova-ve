# Backend

Async FastAPI app under `backend/app/`. Driven by Pydantic schemas, talks to Postgres via SQLAlchemy async, and persists lab topology to JSON files on disk.

## Module layout

```
backend/app/
├── main.py             # FastAPI application + router wiring
├── config.py           # Settings (Pydantic BaseSettings)
├── database.py         # Async engine + session factory
├── dependencies.py     # FastAPI dependency injectors (auth, lab lookup, etc.)
├── core/               # Cross-cutting concerns (logging, error handlers)
├── models/             # SQLAlchemy models — users, sessions, audit
├── schemas/            # Pydantic request/response models
├── routers/            # HTTP + WebSocket route handlers
└── services/           # Business logic — pure Python, no FastAPI coupling
```

## Routers

Each module under `routers/` mounts an `APIRouter` at a path prefix:

| Router | Prefix | Surface |
|---|---|---|
| `auth.py` | `/api/auth` | Session login / logout / current-user. |
| `users.py` | `/api/users` | User CRUD (admin-only). |
| `folders.py` | `/api/folders` | Lab folder hierarchy. Listing entries carry an `owner`: a top-level `/<user>` folder is owned by that user (children inherit), `/Shared` by all users (`*`), `Running`/`Users` by `system`; labs report their stored owner. |
| `labs.py` | `/api/labs` | Lab CRUD, node start/stop, runtime control, external-console endpoints. |
| `links.py` | `/api/labs/{lab}/links` | Link create / delete / list. |
| `networks.py` | `/api/labs/{lab}/networks` | Network create / patch / delete + runtime bridge reconciliation. |
| `listing.py` | `/api/list` | Vendor template + image listings. |
| `system.py` | `/api/system` | Health, status, editable settings, environment info. |
| `dynamips.py` | `/api/dynamips` | Idle-PC calibration API. |
| `docker_images.py` | `/api/docker/images` | Local Docker image inspection for node templates. |
| `library.py` | `/api/library` | User image library management. |
| `manage.py` | `/api/manage` | Lab + node management views. |
| `ws.py` | `/ws` | WebSocket fan-out for runtime state. |

## Services

Each service owns one concern. Routers stay thin and delegate everywhere:

| Service | What it owns |
|---|---|
| `auth_service` | Password hashing, session token issuance, role checks. |
| `lab_service` | Lab JSON read/write, validation, schema upgrades. |
| `lab_lock` | Cooperative file lock for lab JSON writes. |
| `link_service` | Link create / delete with cascading runtime detach. |
| `link_utils` | Pure helpers for link endpoint shape. |
| `network_service` | Bridge create / delete / rename, display-name uniqueness, NAT-Cloud + Bridge-Cloud runtime state. |
| `node_runtime_service` | Start / stop nodes, attach / detach interfaces at runtime, console ports and proxies. |
| `runtime/` | Per-kind launchers (`iol`, `dynamips`) used by the runtime service. |
| `runtime_mutex` | Per-(lab, node, iface) async locks gating runtime mutations. |
| `runtime_pids` | PID tracking and exit-watcher per running node. |
| `host_net` | Shell-out to the privileged helper for bridge / veth / TAP ops. |
| `mac_registry` | Live-MAC reader + planned-MAC comparator. |
| `template_service` | Template discovery and instantiation. |
| `qemu_versions` | Side-by-side QEMU version resolution (#278). |
| `docker_image_service` | Local Docker image listing/inspection. |
| `image_library_service` | User image library. |
| `bridge_cloud_service` | Host Bridge-Cloud (`br-eth*`) listing. |
| `cloud_inventory_service` | NAT-Cloud egress inventory. |
| `iourc_service` | IOURC license storage/status for IOL nodes. |
| `folder_service` | Lab folder hierarchy. |
| `guacamole_db_service` | Wire console connections into the Guacamole DB. |
| `html5_service` | HTML5 console URL builder. |
| `ws_hub` | WebSocket fan-out — subscribe-by-lab, broadcast runtime events. |

## EVE-NG importer

The EVE-NG importer is **not** part of the running backend. It lives at `backend/scripts/import_eveng/` and runs as an offline CLI:

```
backend/scripts/import_eveng/
├── cli.py              # argparse entry; resolves modes, walks, plans, migrates
├── walker.py           # KIND_QEMU / KIND_DYNAMIPS / KIND_IOL / KIND_DOCKER discovery
├── copy_engine.py      # Idempotent move+symlink / copy + sha256 verify (#271)
├── idempotency.py      # Per-file skip decisions
├── migrate.py          # Top-level run orchestrator (step reporter wiring)
├── manifest.py         # Manifest schema + writer
├── permissions.py      # chown / chmod after migration
├── _app_owner.py       # Service-user resolution (NOVA_VE_SERVICE_USER → NOVA_VE_OWNER → nova-ve)
├── _hash.py            # sha256 helper
├── logging_setup.py    # Step reporter + rotating run-log config (#271)
├── template_schema.py  # Result schema for adapter convert()
├── parsers/            # PHP / YAML parsers for EVE-NG source templates
└── adapters/           # One module per vendor (arista_veos, cisco_iol, …)
```

See [CLI — `import-eveng-templates`](../reference/cli-import-eveng.md) for the user-facing flags and [Vendor coverage](../importing-eveng/vendor-coverage.md) for the adapter list.

## Testing

```bash
# Full suite
cd backend && .venv/bin/python -m pytest tests/

# A subset
.venv/bin/python -m pytest tests/test_networks_api.py -k "test_create_network_duplicate"

# Importer-only
.venv/bin/python -m pytest tests/scripts/test_import_eveng/
```

Integration tests use a real Postgres via the host package; unit tests for services run without it.
