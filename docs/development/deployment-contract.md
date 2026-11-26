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
- `COOKIE_SECURE=False` is permitted for an initial isolated control-plane validation lane because the initial contract does not require TLS termination.
- When the deployment is moved behind trusted HTTPS with a stable hostname, `COOKIE_SECURE` should be flipped to `True`.

## Database Lifecycle Ownership

- Migrations run as an explicit provisioning action as the service user. Operators can use `deploy/scripts/run-migrations.sh` for manual application deployments.
- The backend service must **not** self-migrate on startup.
- Initial seed runs as a separate explicit provisioning action as the service user.
- Seed is safe to rerun because it skips creation when the configured admin user already exists.
- Guacamole schema bootstrap runs as an explicit provisioning/deploy action inside `deploy/scripts/run-guacamole.sh`.
- Guacamole auth/storage uses PostgreSQL-backed datasource `postgresql`, not the legacy JSON-auth-only path, for the target host lane.
- Installer success must not depend on undocumented manual Alembic or seed commands.

## Filesystem Layout

- App checkout root: the service-owned application directory. The installer defaults to `/var/lib/nova-ve/nova-ve-git`.
- Privileged provisioning source: a temporary root-owned checkout at the same Git revision as the application. The installer removes it after provisioning.
- **Instance ID file: `/etc/nova-ve/instance_id`** — written once by `provision-ubuntu-2604.sh`, never overwritten; authoritative source for bridge/TAP name derivation
- Static frontend root: `/var/lib/nova-ve/www`
- Labs: `/var/lib/nova-ve/labs`
- Images: `/var/lib/nova-ve/images`
- Temp: `/var/lib/nova-ve/tmp`
- Guacamole support state: `/var/lib/nova-ve/guacamole`

## Instance ID and Multi-Host Constraints

nova-ve generates collision-resistant Linux bridge and TAP interface names using a per-deployment instance ID.

### What it is

`/etc/nova-ve/instance_id` holds a UUID (generated via `/proc/sys/kernel/random/uuid` at first provisioning).
The backend derives a 16-bit `lab_hash = blake2b(instance_id + ":" + lab_id, digest_size=2)` used in:
- Bridge names: `nove{lab_hash:04x}n{network_id}` (≤14 chars)
- TAP names: `nve{lab_hash:04x}d{node_id}i{iface}` (≤14 chars)

### Rules

- `/etc/nova-ve/instance_id` **MUST NOT live on a shared filesystem** (NFS, CIFS, etc.).
  Each physical or virtual host must have a unique value on its local filesystem.
- If a host VM is cloned from an image, `install.sh` must be re-run on the
  clone so a new UUID is generated. The script is idempotent only within a single host
  (it never overwrites an existing non-empty file). Clones share the original UUID unless
  the file is explicitly regenerated.
- The backend startup will **FAIL HARD** with a clear error message if the file is missing
  or empty. There is no silent fallback — this is intentional to prevent bridge name
  collisions across hosts.

### Temporary relocation (non-production only)

There is no env-var override for the instance UUID value itself — startup fails
hard without the file. On a dev machine you can point the backend at a different
file location instead:
```
NOVA_VE_INSTANCE_ID_FILE=/path/to/instance_id     # default /etc/nova-ve/instance_id
NOVA_VE_INSTANCE_DIR=/path/to/dir                 # dir to auto-create the file in
```
This is **not suitable for production** — provisioning writes the canonical file.

## Toolchain Expectations

- Python runtime for the backend is the host-native Ubuntu `26.04` `python3` toolchain
- Node and npm are required only to build the static frontend assets during provisioning
- Docker Engine and Compose are required for Docker node runtime plus Guacamole support services
- `dnsmasq` and `nftables` are required for NAT-Cloud networks
- The provisioning lane must not assume a separately packaged legacy Python version on Ubuntu `26.04`

## Runtime Tunables

The backend reads optional environment variables from its environment file
to tune long-running reconciliation behavior. All values have safe defaults; set
them only when an operator has a specific reason to deviate.

| Variable | Default | Range | Effect |
| --- | --- | --- | --- |
| `NOVA_VE_DISCOVERY_CADENCE_SECONDS` | `30` | `[5, 300]` | Period of the discovery loop that walks each lab network's Linux bridge (`bridge link show master <bridge>`) plus container netns and cross-references against `links[]`. Discrepancies emit a `discovered_link` WS event consumed by the canvas overlay. Out-of-range values fail backend startup. Also editable in the UI (System → Settings); edits persist to `system-settings.json` and apply at the next backend restart (`systemctl restart nova-ve-backend.service` — the unit has no reload action). |
| `NOVA_VE_NAT_CLOUD_POOL` | `10.255.0.0/16` | IPv4 CIDR, prefix `/0`-`/24` | Root pool for auto-allocating one `/24` per NAT-Cloud network. |
| `NOVA_VE_KSM_PAGES_TO_SCAN` | `1250` | positive int | KSM scanner pages per wake (see below). |
| `NOVA_VE_KSM_SLEEP_MS` | `10` | positive int | KSM scanner sleep between wakes, in milliseconds (see below). |
| `NOVA_VE_KSM_RUN` | `1` | `0` or `1` | Master KSM switch (see below). |

## Kernel Samepage Merging (KSM)

Running labs are the ideal case for KSM: many guests boot the same images, so
identical guest pages can be deduplicated instead of each guest paying a
private copy. Stock Ubuntu ships the daemon off, so provisioning turns it on.

- QEMU marks guest RAM mergeable itself; no node or template change is needed.
  Guests started before KSM was enabled deduplicate once the daemon runs, so
  no restart is required for existing nodes to benefit.
- `provision-ubuntu-2604.sh` persists `KSM_ENABLED=1` in
  `/etc/default/qemu-kvm` (otherwise the stock boot task forces `run=0` again
  at every boot), installs `/opt/nova-ve/bin/nova-ve-ksm.sh` with the
  `nova-ve-ksm.service` boot unit (tuned scanner rates, `run` set last,
  `ksmtuned` masked so nothing else toggles KSM), and enables the unit after
  `qemu-kvm.service` and before `nova-ve-backend.service`.
- The `NOVA_VE_KSM_*` overrides above are read from
  `/etc/nova-ve/nova-ve.env` by the tuner script. Remove the overrides (or set
  `NOVA_VE_KSM_RUN=0`) and restart `nova-ve-ksm.service` to turn KSM back off.
- `/api/status` reports the live state in `ksm` plus `ksm_facts`
  (`enabled`, `pages_sharing`/`pages_shared`, `dedup_ratio`, `saved_gb`).
  `smoke-check.sh` fails when the KSM interface exists but the daemon is not
  scanning.

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
