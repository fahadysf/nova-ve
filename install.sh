#!/usr/bin/env bash
# Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
# SPDX-License-Identifier: Apache-2.0
#
# nova-ve one-shot installer for fresh Ubuntu 26.04 LTS x86_64 hosts.
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/fahadysf/nova-ve/main/install.sh | sudo bash
# or:
#   wget -qO install.sh https://raw.githubusercontent.com/fahadysf/nova-ve/main/install.sh
#   sudo bash install.sh
#
# What it does (one-shot, idempotent):
#   1. Installs git + ca-certificates + curl (bootstrap dependencies).
#   2. Clones https://github.com/fahadysf/nova-ve.git into ~${SUDO_USER}/nova-ve-git.
#   3. Runs deploy/scripts/provision-ubuntu-2604.sh which:
#        - installs all OS packages: docker.io, docker-compose-v2, postgresql,
#          caddy, nodejs+npm, python3+venv, build-essential, libpq-dev, jq,
#          openssl, qemu/kvm tooling (pulled in transitively by the helper);
#        - creates /var/lib/nova-ve/{labs,images,tmp,guacamole,runtime,www};
#        - generates random secrets into /etc/nova-ve/backend.env;
#        - bootstraps the host postgres role/db (nova/nova/novadb);
#        - bootstraps the dockerized guacamole stack (guacdb/guacd/guacamole);
#        - builds the FastAPI backend (.venv + alembic upgrade head + seed);
#        - builds the SvelteKit frontend (npm ci + vite build) into
#          /var/lib/nova-ve/www;
#        - installs the privileged network helper at /opt/nova-ve/bin/ and
#          the matching /etc/sudoers.d/nova-ve fragment (visudo-validated);
#        - drops systemd units (nova-ve-backend, caddy) and starts them;
#        - runs deploy/scripts/smoke-check.sh.
#   4. Writes nova-ve-install-summary.md into the launch directory with the
#      bootstrapped admin credentials and a map of every critical file +
#      env var on the host.
#
# Override knobs (env):
#   NOVA_VE_REPO_URL  default https://github.com/fahadysf/nova-ve.git
#   NOVA_VE_REPO_REF  default main
#   NOVA_VE_REPO_DIR  default ~${SUDO_USER}/nova-ve-git
#   NOVA_VE_OWNER     default ${SUDO_USER:-ubuntu}

set -euo pipefail

# When invoked via `curl ... | sudo bash` our source code is being streamed
# through stdin. Child processes that read stdin -- notably `docker exec -i`
# inside deploy/scripts/run-guacamole.sh -- will silently drain that pipe and
# the script terminates partway through. Detect pipe mode and re-exec from a
# tempfile with stdin pointed at /dev/null so child processes can never eat
# our source.
if [[ ! -r "${BASH_SOURCE[0]:-}" ]]; then
  __nv_self="$(mktemp /tmp/nova-ve-install.XXXXXX.sh)"
  cat > "${__nv_self}"
  chmod 0700 "${__nv_self}"
  exec bash "${__nv_self}" "$@" </dev/null
fi

REPO_URL="${NOVA_VE_REPO_URL:-https://github.com/fahadysf/nova-ve.git}"
REPO_REF="${NOVA_VE_REPO_REF:-main}"
LAUNCH_DIR="${NOVA_VE_LAUNCH_DIR:-${PWD}}"

if [[ ${EUID} -ne 0 ]]; then
  echo "install.sh: must run as root. Try: curl -fsSL <url> | sudo bash" >&2
  exit 1
fi

# shellcheck disable=SC1091
. /etc/os-release
if [[ "${ID:-}" != "ubuntu" || "${VERSION_ID:-}" != "26.04" ]]; then
  echo "install.sh: only Ubuntu 26.04 LTS is supported (found ${ID:-?} ${VERSION_ID:-?})" >&2
  exit 1
fi
if [[ "$(uname -m)" != "x86_64" ]]; then
  echo "install.sh: only x86_64 is supported" >&2
  exit 1
fi

APP_OWNER="${NOVA_VE_OWNER:-${SUDO_USER:-ubuntu}}"
if ! id "${APP_OWNER}" >/dev/null 2>&1; then
  echo "install.sh: target user '${APP_OWNER}' does not exist" >&2
  exit 1
fi
APP_GROUP="$(id -gn "${APP_OWNER}")"
APP_HOME="$(getent passwd "${APP_OWNER}" | cut -d: -f6)"
REPO_DIR="${NOVA_VE_REPO_DIR:-${APP_HOME}/nova-ve-git}"

log() { printf '\n[install.sh] %s\n' "$*"; }

log "Target host: $(hostname -f) ($(hostname -I 2>/dev/null | awk '{print $1}'))"
log "Repo destination: ${REPO_DIR} (owner=${APP_OWNER}:${APP_GROUP})"
log "Launch dir for SUMMARY: ${LAUNCH_DIR}"

log "Installing bootstrap packages (git, ca-certificates, curl)..."
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y --no-install-recommends ca-certificates curl git

if [[ -d "${REPO_DIR}/.git" ]]; then
  log "Repo already at ${REPO_DIR}; fetching ${REPO_REF}..."
  sudo -u "${APP_OWNER}" git -C "${REPO_DIR}" fetch --tags --prune origin
  sudo -u "${APP_OWNER}" git -C "${REPO_DIR}" checkout "${REPO_REF}"
  if ! sudo -u "${APP_OWNER}" git -C "${REPO_DIR}" pull --ff-only origin "${REPO_REF}"; then
    log "WARN: fast-forward pull failed (local divergence?); continuing with current HEAD"
  fi
else
  log "Cloning ${REPO_URL} -> ${REPO_DIR} (ref ${REPO_REF})..."
  install -d -o "${APP_OWNER}" -g "${APP_GROUP}" "$(dirname "${REPO_DIR}")"
  sudo -u "${APP_OWNER}" git clone --branch "${REPO_REF}" "${REPO_URL}" "${REPO_DIR}"
fi

PROVISIONER="${REPO_DIR}/deploy/scripts/provision-ubuntu-2604.sh"
if [[ ! -x "${PROVISIONER}" ]]; then
  echo "install.sh: provisioner missing or not executable: ${PROVISIONER}" >&2
  exit 1
fi

log "Running ${PROVISIONER} (this can take 10-20 min on a fresh box)..."
SUDO_USER="${APP_OWNER}" bash "${PROVISIONER}"

log "Generating install summary..."
ENV_FILE="/etc/nova-ve/backend.env"
if [[ ! -r "${ENV_FILE}" ]]; then
  echo "install.sh: ${ENV_FILE} missing after provisioning -- aborting summary" >&2
  exit 1
fi
# shellcheck disable=SC1090
set -a; source "${ENV_FILE}"; set +a

SUMMARY_PATH="${LAUNCH_DIR}/nova-ve-install-summary.md"
PRIMARY_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
INSTANCE_ID="$(cat /etc/nova-ve/instance_id 2>/dev/null || echo '<missing>')"
GIT_COMMIT="$(sudo -u "${APP_OWNER}" git -C "${REPO_DIR}" rev-parse --short HEAD)"
GENERATED_AT="$(date --iso-8601=seconds)"
DEMO_IMAGES_STATUS="$(cat /var/lib/nova-ve/.demo-images-status 2>/dev/null || echo unknown)"

cat > "${SUMMARY_PATH}" <<EOF
# nova-ve install summary

- Generated: ${GENERATED_AT}
- Host: $(hostname -f)
- Primary IPv4: ${PRIMARY_IP}
- Instance ID: ${INSTANCE_ID}
- Repo: ${REPO_DIR} @ ${REPO_REF} (${GIT_COMMIT})

## Bootstrapped admin credentials

- Web UI: http://${PRIMARY_IP}/
- Login API: POST http://${PRIMARY_IP}/api/auth/login
- Username: ${NOVA_VE_ADMIN_USERNAME}
- Password: ${NOVA_VE_ADMIN_PASSWORD}
- Email:    ${NOVA_VE_ADMIN_EMAIL}
- Display:  ${NOVA_VE_ADMIN_NAME}

Full env file (mode 0600, root-owned) lives at \`${ENV_FILE}\`.
This summary is mode 0600, owned by \`${APP_OWNER}\`.

## Demo images (#191)

Status: \`${DEMO_IMAGES_STATUS}\` (one of: \`ok\`, \`skipped\`, \`failed\`, \`unknown\`)

The \`nova-ve-alpine-telnet:latest\` image is built during provisioning so a docker node with
\`image: alpine:latest\` and \`extras.command: "sleep infinity"\` (#181) works out of the box.

Re-build manually:

\`\`\`
cd ${REPO_DIR} && bash deploy/scripts/build-demo-images.sh
\`\`\`

Skip on next install: \`NOVA_VE_SKIP_DEMO_IMAGES=1 curl -fsSL ... | sudo bash\`.

## Critical files

| Path | Purpose |
|------|---------|
| \`${ENV_FILE}\` | All backend env vars: SECRET_KEY, DATABASE_URL, NOVA_VE_ADMIN_*, GUACAMOLE_* |
| \`/etc/nova-ve/instance_id\` | Stable per-host UUID (used in bridge / iface naming) |
| \`/etc/sudoers.d/nova-ve\` | NOPASSWD rule: \`${APP_OWNER} -> /opt/nova-ve/bin/nova-ve-net.py *\` |
| \`/etc/caddy/Caddyfile\` | Front door on :80 (SPA + reverse-proxy /api,/ws -> :8000, /html5 -> :8081) |
| \`/etc/systemd/system/nova-ve-backend.service\` | uvicorn unit (User=${APP_OWNER}, EnvironmentFile=${ENV_FILE}) |
| \`/opt/nova-ve/bin/nova-ve-net.py\` | Privileged net helper (sudo NOPASSWD entry point) |
| \`/opt/nova-ve/bin/nova-ve-console-proxy.py\` | Console-proxy execed by the net helper |
| \`${REPO_DIR}/backend/.venv\` | Python venv (FastAPI / uvicorn / SQLAlchemy / alembic) |
| \`${REPO_DIR}/backend/requirements.txt\` | Backend pip deps |
| \`${REPO_DIR}/backend/alembic.ini\` | DB migrations entry point |
| \`${REPO_DIR}/frontend/build\` | SvelteKit build output (mirrored to /var/lib/nova-ve/www) |
| \`${REPO_DIR}/deploy/compose/guacamole-compose.yml\` | guacdb/guacd/guacamole stack definition |
| \`${REPO_DIR}/deploy/templates/Caddyfile.tpl\` | Source template for /etc/caddy/Caddyfile |
| \`${REPO_DIR}/deploy/templates/nova-ve-backend.service.tpl\` | Source template for the backend unit |
| \`/var/lib/nova-ve/www\` | Caddy webroot (built SPA) |
| \`/var/lib/nova-ve/labs\` | Lab JSON files |
| \`/var/lib/nova-ve/images\` | VM images (qemu/, etc.) |
| \`/var/lib/nova-ve/runtime\` | PID registry, namespace bookkeeping |
| \`/var/lib/nova-ve/guacamole/db\` | Postgres data dir for the dockerized guacamole DB |
| \`/var/lib/nova-ve/tmp\` | Scratch dir |

## Critical env vars (in ${ENV_FILE})

| Var | Value on this host |
|-----|--------------------|
| \`SECRET_KEY\` | (random, see ${ENV_FILE}) |
| \`DATABASE_URL\` | \`${DATABASE_URL}\` |
| \`BASE_DATA_DIR\` | \`${BASE_DATA_DIR}\` |
| \`LABS_DIR\` | \`${LABS_DIR}\` |
| \`IMAGES_DIR\` | \`${IMAGES_DIR}\` |
| \`TMP_DIR\` | \`${TMP_DIR}\` |
| \`COOKIE_SECURE\` | \`${COOKIE_SECURE}\` |
| \`COOKIE_SAMESITE\` | \`${COOKIE_SAMESITE}\` |
| \`NOVA_VE_ADMIN_USERNAME\` | \`${NOVA_VE_ADMIN_USERNAME}\` |
| \`NOVA_VE_ADMIN_PASSWORD\` | (see "Bootstrapped admin credentials" above) |
| \`GUACAMOLE_PUBLIC_PATH\` | \`${GUACAMOLE_PUBLIC_PATH}\` |
| \`GUACAMOLE_TARGET_HOST\` | \`${GUACAMOLE_TARGET_HOST}\` |
| \`GUACAMOLE_STATE_DIR\` | \`${GUACAMOLE_STATE_DIR}\` |
| \`GUACAMOLE_DATA_SOURCE\` | \`${GUACAMOLE_DATA_SOURCE}\` |
| \`GUACAMOLE_DB_PASSWORD\` | (random, see ${ENV_FILE}) |
| \`GUACAMOLE_DATABASE_URL\` | (depends on GUACAMOLE_DB_PASSWORD; see ${ENV_FILE}) |
| \`GUACAMOLE_JSON_SECRET_KEY\` | (random, see ${ENV_FILE}) |
| \`GUACAMOLE_JSON_EXPIRE_SECONDS\` | \`${GUACAMOLE_JSON_EXPIRE_SECONDS}\` |

## Services

| Unit / project | Status command |
|---|---|
| \`nova-ve-backend.service\` | \`systemctl status nova-ve-backend\` |
| \`caddy.service\` | \`systemctl status caddy\` |
| \`postgresql.service\` | \`systemctl status postgresql\` |
| \`docker.service\` | \`systemctl status docker\` |
| guacamole compose stack | \`docker compose -f ${REPO_DIR}/deploy/compose/guacamole-compose.yml ps\` |

## Smoke check

\`\`\`
curl -fsS http://127.0.0.1/api/health
curl -fsS http://127.0.0.1/ | head -1
curl -fsS http://127.0.0.1/html5/ | head -1
\`\`\`
EOF

chown "${APP_OWNER}:${APP_GROUP}" "${SUMMARY_PATH}"
chmod 0600 "${SUMMARY_PATH}"

cat <<EOF

==========================================================================
 nova-ve install complete.
 Summary: ${SUMMARY_PATH}
          (mode 0600 -- contains the bootstrap admin password)
 Web UI:  http://${PRIMARY_IP}/
 Admin:   ${NOVA_VE_ADMIN_USERNAME} / ${NOVA_VE_ADMIN_PASSWORD}
==========================================================================
EOF
