# Architecture overview

## Big picture

```
                      ┌────────────────────────────┐
   Browser ────────►  │  Caddy (TLS, reverse proxy)│
                      └─────────────┬──────────────┘
                                    │
                  ┌─────────────────┴───────────────┐
                  │                                 │
                  ▼                                 ▼
       ┌──────────────────────┐         ┌────────────────────┐
       │  Static SPA          │         │  FastAPI backend   │
       │  (Svelte 5 + Vite,   │         │  uvicorn :8000     │
       │   built into         │         │  + Pydantic + SQLA │
       │   /var/lib/nova-ve/  │         │                    │
       │   www/)              │         │  + WS              │
       └──────────────────────┘         └─────┬──────────────┘
                                              │
              ┌───────────────────────────────┼─────────────────────────────┐
              ▼                               ▼                             ▼
      ┌───────────────┐            ┌──────────────────────┐    ┌────────────────────┐
      │  PostgreSQL   │            │  Lab JSON files      │    │  Guacamole stack   │
      │  novadb       │            │  /var/lib/nova-ve/   │    │  guacd / guacamole │
      │  (host pkg,   │            │    labs/<lab>.json   │    │  /  guacdb         │
      │   127.0.0.1)  │            │                      │    │  (Docker)          │
      └───────────────┘            └─────────┬────────────┘    └─────────▲──────────┘
                                             │                           │
                                             ▼                           │
                                   ┌─────────────────────────┐           │
                                   │  Privileged net helper  │           │
                                   │  /opt/nova-ve/bin/      │           │
                                   │  (bridges, veth, TAP)   │           │
                                   └─────────┬───────────────┘           │
                                             │                           │
                                             ▼                           │
                                   ┌─────────────────────────┐           │
                                   │  QEMU / Docker runtimes │───────────┘
                                   │  on the host            │  (console connects via Guacamole)
                                   └─────────────────────────┘
```

## Data flow

The FastAPI backend is the only writer to lab JSON. The frontend treats the lab JSON (and a runtime view served via a WebSocket) as the canonical source of truth.

| Concern | Source of truth |
|---|---|
| Lab topology (nodes / networks / links / per-node config) | `<lab>.json` |
| Runtime state (running PIDs, live MACs, interface attachments) | In-memory runtime tracker in the backend |
| Per-node disk overlays | `<lab>/<node-id>/` on the host filesystem |
| User accounts, sessions, audit log | Postgres `novadb` |
| Vendor base images | `/var/lib/nova-ve/images/<kind>/<key>/` |
| Templates | `/var/lib/nova-ve/templates/<vendor>/<key>.json` |

## Process model

| Process | Where | Owner | Privilege |
|---|---|---|---|
| `nova-ve-backend` (uvicorn) | systemd unit | dedicated `nova-ve` service user by default | Unprivileged; shells out to the helper for net ops |
| `caddy` | systemd unit | caddy user | CAP_NET_BIND_SERVICE for ports 80/443 |
| `nova-ve-net` (privileged helper) | invoked per call | root (via sudoers fragment) | Bridge / veth / TAP create-and-attach |
| `guacd` + `guacamole` + `guacdb` | Docker compose | Docker | Console proxy |
| QEMU / Docker children | per-node | service user or root depending on runtime path | Per-process; cleaned up on node stop |

## Key contracts

- **The backend does not self-migrate.** Alembic migrations are operator-driven (`alembic upgrade head` then restart). The contract lets the unit fail fast on missing migrations rather than silently mutate the schema.
- **The privileged helper is the only path to host networking.** The backend never calls `ip link` or `brctl` directly; everything goes through `/opt/nova-ve/bin/nova-ve-net` via the sudoers fragment.
- **Lab JSON is the topology source of truth.** Runtime state is derived, not authoritative. After a restart, the backend re-derives runtime state from the lab JSON, not the other way around.

## Further reading

- [Backend](backend.md) — module layout.
- [Frontend](frontend.md) — Svelte app layout and the canvas.
- [Deployment contract](deployment-contract.md) — the formal version of the contracts above.
