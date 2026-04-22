#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
DRY_RUN=0
APP_OWNER="${SUDO_USER:-$(id -un)}"
APP_GROUP="$(id -gn "${APP_OWNER}")"
APP_ROOT="${REPO_ROOT}"
ENV_DIR="/etc/nova-ve"
ENV_FILE="${ENV_DIR}/backend.env"
FRONTEND_ROOT="${NOVA_VE_FRONTEND_ROOT:-/var/lib/nova-ve/www}"
CADDY_TEMPLATE="${REPO_ROOT}/deploy/templates/Caddyfile.tpl"
BACKEND_SERVICE_TEMPLATE="${REPO_ROOT}/deploy/templates/nova-ve-backend.service.tpl"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run)
      DRY_RUN=1
      shift
      ;;
    *)
      echo "Unknown argument: $1" >&2
      exit 1
      ;;
  esac
done

run() {
  if [[ "${DRY_RUN}" -eq 1 ]]; then
    printf '+ %q' "$1"
    shift
    for arg in "$@"; do
      printf ' %q' "${arg}"
    done
    printf '\n'
    return 0
  fi
  "$@"
}

require_target_host() {
  if [[ "${DRY_RUN}" -eq 1 ]]; then
    return 0
  fi
  if [[ "$(uname -m)" != "x86_64" ]]; then
    echo "This provisioning lane only supports x86_64 hosts." >&2
    exit 1
  fi
  # shellcheck disable=SC1091
  . /etc/os-release
  if [[ "${ID}" != "ubuntu" || "${VERSION_ID}" != "26.04" ]]; then
    echo "This provisioning lane only supports Ubuntu 26.04." >&2
    exit 1
  fi
  if [[ "${EUID}" -ne 0 ]]; then
    echo "Run this script as root." >&2
    exit 1
  fi
}

render_template() {
  local template="$1"
  local destination="$2"
  if [[ "${DRY_RUN}" -eq 1 ]]; then
    echo "+ render ${template} -> ${destination}"
    return 0
  fi
  sed \
    -e "s|__APP_ROOT__|${APP_ROOT}|g" \
    -e "s|__APP_OWNER__|${APP_OWNER}|g" \
    -e "s|__APP_GROUP__|${APP_GROUP}|g" \
    -e "s|__FRONTEND_ROOT__|${FRONTEND_ROOT}|g" \
    "${template}" > "${destination}"
}

ensure_database() {
  if [[ "${DRY_RUN}" -eq 1 ]]; then
    echo "+ ensure PostgreSQL role nova exists"
    echo "+ ensure PostgreSQL database novadb exists"
    return 0
  fi

  sudo -u postgres psql -v ON_ERROR_STOP=1 -c "DO \$\$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'nova') THEN CREATE ROLE nova LOGIN PASSWORD 'nova'; END IF; END \$\$;"

  if ! sudo -u postgres psql -Atqc "SELECT 1 FROM pg_database WHERE datname='novadb'" | grep -q '^1$'; then
    sudo -u postgres createdb -O nova novadb
  fi
}

ensure_backend_env() {
  if [[ "${DRY_RUN}" -eq 1 ]]; then
    echo "+ ensure ${ENV_FILE} exists"
    return 0
  fi

  install -d -m 0700 "${ENV_DIR}"
  if [[ ! -f "${ENV_FILE}" ]]; then
    local secret
    local admin_password
    secret="$(python3.12 - <<'PY'
import secrets
print(secrets.token_urlsafe(48))
PY
)"
    admin_password="$(python3.12 - <<'PY'
import secrets
print(secrets.token_urlsafe(24))
PY
)"
    install -m 0600 /dev/null "${ENV_FILE}"
    sed \
      -e "s|^SECRET_KEY=.*|SECRET_KEY=${secret}|" \
      -e "s|^NOVA_VE_ADMIN_PASSWORD=.*|NOVA_VE_ADMIN_PASSWORD=${admin_password}|" \
      "${REPO_ROOT}/deploy/env/backend.env.example" > "${ENV_FILE}"
  fi
  chmod 0600 "${ENV_FILE}"
}

require_target_host

run apt-get update
run apt-get install -y \
  ca-certificates \
  curl \
  jq \
  build-essential \
  libpq-dev \
  postgresql \
  postgresql-contrib \
  caddy \
  python3.12 \
  python3.12-venv \
  nodejs \
  npm

run install -d -m 0755 /var/lib/nova-ve/labs /var/lib/nova-ve/images /var/lib/nova-ve/tmp "${FRONTEND_ROOT}"
ensure_backend_env

ensure_database

run python3.12 -m venv "${REPO_ROOT}/backend/.venv"
run "${REPO_ROOT}/backend/.venv/bin/pip" install --upgrade pip
run "${REPO_ROOT}/backend/.venv/bin/pip" install -r "${REPO_ROOT}/backend/requirements.txt"

run "${REPO_ROOT}/deploy/scripts/run-migrations.sh"
run "${REPO_ROOT}/deploy/scripts/run-seed.sh"
run "${REPO_ROOT}/deploy/scripts/build-frontend.sh"

render_template "${CADDY_TEMPLATE}" /etc/caddy/Caddyfile
render_template "${BACKEND_SERVICE_TEMPLATE}" /etc/systemd/system/nova-ve-backend.service
run caddy validate --config /etc/caddy/Caddyfile

run systemctl daemon-reload
run systemctl enable postgresql
run systemctl enable caddy
run systemctl enable nova-ve-backend
run systemctl restart postgresql
run systemctl restart nova-ve-backend
run systemctl restart caddy

run "${REPO_ROOT}/deploy/scripts/smoke-check.sh"

if [[ "${DRY_RUN}" -eq 0 ]]; then
  echo "Bootstrap admin credentials are stored in ${ENV_FILE}."
fi
