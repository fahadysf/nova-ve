# Nova-ve Deployment Contract

## Scope

This contract defines the first supported host-native deployment lane for `nova-ve`.

- Target OS: `Ubuntu 26.04 x86_64`
- Frontdoor: `Caddy`
- Public origin shape: same-origin frontend and API on one host
- Mutable data root: `/var/lib/nova-ve`
- This lane is for control-plane bring-up first, not full appliance packaging

Out of scope for this phase:

- `.deb` packaging or apt repository publication
- OVA/golden-image distribution
- HA/cluster behavior
- non-Ubuntu hosts
- authoritative live testing on the local macOS ARM workstation

## Production Topology

The normative frontend topology for this phase is:

- Caddy serves the built static frontend from `/var/lib/nova-ve/www`
- Caddy reverse-proxies `/api/*` to FastAPI on `127.0.0.1:8000`
- Caddy reverse-proxies `/html5/*` to the Guacamole web application on `127.0.0.1:8081`
- The frontend uses same-origin relative `/api` requests and browser-managed cookies

This lane intentionally does **not** run a separate long-lived frontend Node service.

## Process Model

Long-running processes:

- `postgresql` from Ubuntu packages
- `caddy` from Ubuntu packages
- `nova-ve-backend.service` for FastAPI/Uvicorn on `127.0.0.1:8000`
- Docker-managed `guacdb` + `guacd` + Guacamole webapp containers for HTML5 console access

No `nova-ve-frontend.service` exists in this lane because the frontend is served as static assets by Caddy.

## Ports And Route Ownership

- Public HTTP port: `80`
- Backend bind: `127.0.0.1:8000`
- Database bind: local host only via PostgreSQL defaults
- Guacamole webapp bind: `127.0.0.1:8081`
- Guacamole database bind: `127.0.0.1:5433`
- `guacd`: Docker-internal port `4822`

Route ownership:

- `/` and browser routes such as `/login`, `/labs`, `/labs/...`: static SPA from Caddy with history fallback to `/index.html`
- `/api/*`: FastAPI
- `/html5/*`: Guacamole HTML5 console UI and tunnel endpoints

## Cookie And CORS Expectations

- Browser/API traffic is same-origin in this lane, so cross-origin CORS is not required for production traffic.
- Auth cookies remain scoped to `/api/`.
- `COOKIE_SECURE=False` is permitted for the first FY Lab control-plane lane because the initial contract does not require TLS termination.
- When the deployment is moved behind trusted HTTPS with a stable hostname, `COOKIE_SECURE` should be flipped to `True`.

## Database Lifecycle Ownership

- Migrations run as an explicit provisioning/deploy action via `deploy/scripts/run-migrations.sh`.
- The backend service must **not** self-migrate on startup.
- Initial seed runs as a separate explicit provisioning/deploy action via `deploy/scripts/run-seed.sh`.
- Seed is safe to rerun because it skips creation when the configured admin user already exists.
- Guacamole schema bootstrap runs as an explicit provisioning/deploy action inside `deploy/scripts/run-guacamole.sh`.
- Guacamole auth/storage uses PostgreSQL-backed datasource `postgresql`, not the legacy JSON-auth-only path, for the target host lane.
- Installer success must not depend on undocumented manual Alembic or seed commands.

## Filesystem Layout

- App checkout root: the absolute repo path where `deploy/scripts/provision-ubuntu-2604.sh` is executed; recommended location is `/opt/nova-ve`
- Backend env file: `/etc/nova-ve/backend.env`
- Static frontend root: `/var/lib/nova-ve/www`
- Labs: `/var/lib/nova-ve/labs`
- Images: `/var/lib/nova-ve/images`
- Temp: `/var/lib/nova-ve/tmp`
- Guacamole support state: `/var/lib/nova-ve/guacamole`
- Guacamole DB credentials and backend access URL are carried in `/etc/nova-ve/backend.env`

## Toolchain Expectations

- Python runtime for the backend is the host-native Ubuntu `26.04` `python3` toolchain
- Node and npm are required only to build the static frontend assets during provisioning
- Docker Engine and Compose are required for Docker node runtime plus Guacamole support services
- The provisioning lane must not assume a separately packaged legacy Python version on Ubuntu `26.04`

## Health And Smoke Checks

Minimum checks for Pass A:

- PostgreSQL service is enabled, active, and reachable
- Alembic current revision equals head
- Seeded admin user exists
- `nova-ve-backend.service` is enabled and active
- `curl http://127.0.0.1:8000/api/health` succeeds
- `curl http://127.0.0.1/api/health` succeeds through Caddy
- `curl http://127.0.0.1/` returns application HTML
- `curl http://127.0.0.1/html5/` returns the Guacamole HTML shell through Caddy
- Services remain healthy after reboot

## Deferred Items

- TLS issuance and `COOKIE_SECURE=True` as the default
- nested virtualization and QEMU runtime validation beyond Pass B runbook execution
- non-ESXi primary runbooks
