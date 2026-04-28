#!/usr/bin/env bash
# Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
# SPDX-License-Identifier: Apache-2.0

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
PYTHON_BIN="${NOVA_VE_PYTHON_BIN:-python3}"
GUACAMOLE_COMPOSE_SCRIPT="${REPO_ROOT}/deploy/scripts/run-guacamole.sh"

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
  if ! command -v "${PYTHON_BIN}" >/dev/null 2>&1; then
    echo "Required Python runtime ${PYTHON_BIN} is not installed." >&2
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

  local guacamole_secret
  local guacamole_db_password
  local guacamole_database_url
  local guacamole_data_source
  local guacamole_state_dir
  local public_path
  local target_host
  local expire_seconds
  install -d -m 0700 "${ENV_DIR}"
  if [[ ! -f "${ENV_FILE}" ]]; then
    local secret
    local admin_password
    secret="$("${PYTHON_BIN}" - <<'PY'
import secrets
print(secrets.token_urlsafe(48))
PY
)"
    admin_password="$("${PYTHON_BIN}" - <<'PY'
import secrets
print(secrets.token_urlsafe(24))
PY
)"
    guacamole_secret="$("${PYTHON_BIN}" - <<'PY'
import secrets
print(secrets.token_hex(16))
PY
)"
    guacamole_db_password="$("${PYTHON_BIN}" - <<'PY'
import secrets
print(secrets.token_urlsafe(24))
PY
)"
    install -m 0600 /dev/null "${ENV_FILE}"
    sed \
      -e "s|^SECRET_KEY=.*|SECRET_KEY=${secret}|" \
      -e "s|^NOVA_VE_ADMIN_PASSWORD=.*|NOVA_VE_ADMIN_PASSWORD=${admin_password}|" \
      -e "s|^GUACAMOLE_DB_PASSWORD=.*|GUACAMOLE_DB_PASSWORD=${guacamole_db_password}|" \
      -e "s|^GUACAMOLE_DATABASE_URL=.*|GUACAMOLE_DATABASE_URL=postgresql+asyncpg://guacuser:${guacamole_db_password}@127.0.0.1:5433/guacdb|" \
      -e "s|^GUACAMOLE_JSON_SECRET_KEY=.*|GUACAMOLE_JSON_SECRET_KEY=${guacamole_secret}|" \
      "${REPO_ROOT}/deploy/env/backend.env.example" > "${ENV_FILE}"
  fi

  public_path="$(awk -F= '/^GUACAMOLE_PUBLIC_PATH=/{print $2}' "${ENV_FILE}" | tail -n1)"
  target_host="$(awk -F= '/^GUACAMOLE_TARGET_HOST=/{print $2}' "${ENV_FILE}" | tail -n1)"
  expire_seconds="$(awk -F= '/^GUACAMOLE_JSON_EXPIRE_SECONDS=/{print $2}' "${ENV_FILE}" | tail -n1)"
  guacamole_secret="$(awk -F= '/^GUACAMOLE_JSON_SECRET_KEY=/{print $2}' "${ENV_FILE}" | tail -n1)"
  guacamole_db_password="$(awk -F= '/^GUACAMOLE_DB_PASSWORD=/{print $2}' "${ENV_FILE}" | tail -n1)"
  guacamole_database_url="$(awk -F= '/^GUACAMOLE_DATABASE_URL=/{print $2}' "${ENV_FILE}" | tail -n1)"
  guacamole_data_source="$(awk -F= '/^GUACAMOLE_DATA_SOURCE=/{print $2}' "${ENV_FILE}" | tail -n1)"
  guacamole_state_dir="$(awk -F= '/^GUACAMOLE_STATE_DIR=/{print $2}' "${ENV_FILE}" | tail -n1)"

  if [[ -z "${public_path}" ]]; then
    printf '\nGUACAMOLE_PUBLIC_PATH=/html5/\n' >> "${ENV_FILE}"
  fi
  if [[ -z "${target_host}" ]]; then
    printf 'GUACAMOLE_TARGET_HOST=host.docker.internal\n' >> "${ENV_FILE}"
  fi
  if [[ -z "${guacamole_state_dir}" ]]; then
    printf 'GUACAMOLE_STATE_DIR=/var/lib/nova-ve/guacamole\n' >> "${ENV_FILE}"
  fi
  if [[ -z "${guacamole_data_source}" ]]; then
    printf 'GUACAMOLE_DATA_SOURCE=postgresql\n' >> "${ENV_FILE}"
  fi
  if [[ -z "${expire_seconds}" ]]; then
    printf 'GUACAMOLE_JSON_EXPIRE_SECONDS=300\n' >> "${ENV_FILE}"
  fi
  if [[ -z "${guacamole_secret}" ]]; then
    guacamole_secret="$("${PYTHON_BIN}" - <<'PY'
import secrets
print(secrets.token_hex(16))
PY
)"
    printf 'GUACAMOLE_JSON_SECRET_KEY=%s\n' "${guacamole_secret}" >> "${ENV_FILE}"
  fi
  if [[ -z "${guacamole_db_password}" ]]; then
    guacamole_db_password="$("${PYTHON_BIN}" - <<'PY'
import secrets
print(secrets.token_urlsafe(24))
PY
)"
    printf 'GUACAMOLE_DB_PASSWORD=%s\n' "${guacamole_db_password}" >> "${ENV_FILE}"
  fi
  if [[ -z "${guacamole_database_url}" ]]; then
    printf 'GUACAMOLE_DATABASE_URL=postgresql+asyncpg://guacuser:%s@127.0.0.1:5433/guacdb\n' "${guacamole_db_password}" >> "${ENV_FILE}"
  fi
  chmod 0600 "${ENV_FILE}"
}

install_nova_ve_net_helper() {
  # US-201: install the privileged network helper at /opt/nova-ve/bin and
  # drop the sudoers fragment into /etc/sudoers.d after a visudo dry-run.
  local helper_src="${REPO_ROOT}/deploy/nova-ve-net.py"
  local sudoers_src="${REPO_ROOT}/deploy/nova-ve-sudoers"
  local helper_dst="/opt/nova-ve/bin/nova-ve-net.py"
  local sudoers_dst="/etc/sudoers.d/nova-ve"

  if [[ "${DRY_RUN}" -eq 1 ]]; then
    echo "+ install -d -o root -g root -m 0755 /opt/nova-ve/bin"
    echo "+ install -m 0755 -o root -g root ${helper_src} ${helper_dst}"
    echo "+ visudo -cf ${sudoers_src}"
    echo "+ install -m 0440 -o root -g root ${sudoers_src} ${sudoers_dst}"
    return 0
  fi

  install -d -o root -g root -m 0755 /opt/nova-ve /opt/nova-ve/bin
  install -m 0755 -o root -g root "${helper_src}" "${helper_dst}"
  if ! visudo -cf "${sudoers_src}" >/dev/null; then
    echo "visudo rejected ${sudoers_src}; aborting deploy" >&2
    exit 1
  fi
  install -m 0440 -o root -g root "${sudoers_src}" "${sudoers_dst}"
}

ensure_instance_id() {
  local id_dir="/etc/nova-ve"
  local id_file="${id_dir}/instance_id"
  if [[ "${DRY_RUN}" -eq 1 ]]; then
    echo "+ ensure ${id_file} exists (idempotent)"
    return 0
  fi
  install -d -m 0755 -o root -g root "${id_dir}"
  if [[ ! -f "${id_file}" ]] || [[ ! -s "${id_file}" ]]; then
    cat /proc/sys/kernel/random/uuid > "${id_file}"
    chmod 0644 "${id_file}"
    chown root:root "${id_file}"
    echo "Generated instance ID: $(cat "${id_file}")"
  else
    echo "Instance ID already exists: $(cat "${id_file}")"
  fi
}

require_target_host

run apt-get update
run apt-get install -y --no-install-recommends \
  ca-certificates \
  curl \
  jq \
  openssl \
  build-essential \
  libpq-dev \
  python3-dev \
  postgresql \
  postgresql-contrib \
  caddy \
  docker.io \
  docker-compose-v2 \
  python3 \
  python3-venv \
  nodejs \
  npm

run install -d -o "${APP_OWNER}" -g "${APP_GROUP}" -m 0755 /var/lib/nova-ve
run install -d -o "${APP_OWNER}" -g "${APP_GROUP}" -m 0755 /var/lib/nova-ve/labs /var/lib/nova-ve/images /var/lib/nova-ve/tmp /var/lib/nova-ve/guacamole /var/lib/nova-ve/guacamole/db /var/lib/nova-ve/runtime "${FRONTEND_ROOT}"
run chown -R "${APP_OWNER}:${APP_GROUP}" /var/lib/nova-ve
install_nova_ve_net_helper
run systemctl enable docker
run systemctl restart docker
run usermod -aG docker "${APP_OWNER}"
ensure_backend_env
ensure_instance_id

ensure_database

run "${PYTHON_BIN}" -m venv "${REPO_ROOT}/backend/.venv"
run "${REPO_ROOT}/backend/.venv/bin/pip" install --upgrade pip
run "${REPO_ROOT}/backend/.venv/bin/pip" install -r "${REPO_ROOT}/backend/requirements.txt"

run "${REPO_ROOT}/deploy/scripts/run-migrations.sh"
run "${REPO_ROOT}/deploy/scripts/run-seed.sh"
run "${REPO_ROOT}/deploy/scripts/build-frontend.sh"
run "${GUACAMOLE_COMPOSE_SCRIPT}"

render_template "${CADDY_TEMPLATE}" /etc/caddy/Caddyfile
render_template "${BACKEND_SERVICE_TEMPLATE}" /etc/systemd/system/nova-ve-backend.service
run caddy validate --config /etc/caddy/Caddyfile

run systemctl daemon-reload
run systemctl enable docker
run systemctl enable postgresql
run systemctl enable caddy
run systemctl enable nova-ve-backend
run systemctl restart docker
run systemctl restart postgresql
run systemctl restart nova-ve-backend
run systemctl restart caddy

run "${REPO_ROOT}/deploy/scripts/smoke-check.sh"

if [[ "${DRY_RUN}" -eq 0 ]]; then
  echo "Bootstrap admin credentials are stored in ${ENV_FILE}."
fi
