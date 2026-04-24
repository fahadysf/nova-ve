#!/usr/bin/env bash
# Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
# SPDX-License-Identifier: Apache-2.0

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
ENV_FILE="${NOVA_VE_ENV_FILE:-/etc/nova-ve/backend.env}"
COMPOSE_FILE="${NOVA_VE_GUAC_COMPOSE_FILE:-${REPO_ROOT}/deploy/compose/guacamole-compose.yml}"
GUAC_PATCH_JS="${REPO_ROOT}/deploy/guacamole/nova-ve-guac-patch.js"
GUAC_FONT_DIR="${REPO_ROOT}/deploy/guacamole/fonts"

if [[ ! -f "${ENV_FILE}" ]]; then
  echo "Missing ${ENV_FILE}" >&2
  exit 1
fi

# shellcheck disable=SC1091
set -a && source "${ENV_FILE}" && set +a

if [[ -z "${GUACAMOLE_DB_PASSWORD:-}" ]]; then
  echo "GUACAMOLE_DB_PASSWORD is not set in ${ENV_FILE}" >&2
  exit 1
fi

state_dir="${GUACAMOLE_STATE_DIR:-/var/lib/nova-ve/guacamole}"
mkdir -p "${state_dir}"

wait_for_container() {
  local service="$1"
  local timeout="${2:-30}"
  local start_ts
  start_ts="$(date +%s)"

  while true; do
    local container_id
    container_id="$(docker compose -f "${COMPOSE_FILE}" ps -q "${service}")"
    if [[ -n "${container_id}" ]]; then
      printf '%s\n' "${container_id}"
      return 0
    fi
    if (( "$(date +%s)" - start_ts >= timeout )); then
      return 1
    fi
    sleep 1
  done
}

wait_for_postgres() {
  local container_id="$1"
  local timeout="${2:-120}"
  local start_ts
  start_ts="$(date +%s)"

  while true; do
    if docker exec "${container_id}" pg_isready -U guacuser -d guacdb >/dev/null 2>&1; then
      return 0
    fi
    if (( "$(date +%s)" - start_ts >= timeout )); then
      return 1
    fi
    sleep 1
  done
}

patch_guacamole_webapp() {
  local container_id="$1"
  local html5_root
  local start_ts
  start_ts="$(date +%s)"

  while true; do
    html5_root="$(docker exec "${container_id}" sh -lc 'find /tmp -type d -path "*/webapps/html5" | head -n1')"
    if [[ -n "${html5_root}" ]] && docker exec "${container_id}" test -f "${html5_root}/index.html" >/dev/null 2>&1; then
      break
    fi
    if (( "$(date +%s)" - start_ts >= 60 )); then
      break
    fi
    sleep 1
  done

  if [[ -z "${html5_root}" ]]; then
    echo "Unable to locate Guacamole webapp root inside ${container_id}" >&2
    return 1
  fi

  docker cp "${GUAC_PATCH_JS}" "${container_id}:${html5_root}/nova-ve-guac-patch.js"
  docker exec "${container_id}" sh -lc "
    sed -i 's/,target-densitydpi=medium-dpi//g' '${html5_root}/index.html'
    grep -q 'nova-ve-guac-patch.js' '${html5_root}/index.html' || \
      sed -i 's#</body>#<script src=\"nova-ve-guac-patch.js\"></script></body>#' '${html5_root}/index.html'
  "
}

install_guacd_fonts() {
  local container_id="$1"
  local target_dir="/usr/local/share/fonts/nova-ve"

  if [[ ! -d "${GUAC_FONT_DIR}" ]]; then
    echo "Missing ${GUAC_FONT_DIR}" >&2
    return 1
  fi

  docker exec -u 0 "${container_id}" sh -lc "mkdir -p '${target_dir}'"
  docker cp "${GUAC_FONT_DIR}/." "${container_id}:${target_dir}/"
  docker exec -u 0 "${container_id}" sh -lc "fc-cache -f '${target_dir}' >/dev/null 2>&1 || fc-cache -f >/dev/null 2>&1"
}

docker compose -f "${COMPOSE_FILE}" up -d --pull missing guacdb
guacdb_container="$(wait_for_container guacdb 30)"
schema_file="$(mktemp)"
trap 'rm -f "${schema_file}"' EXIT
if ! wait_for_postgres "${guacdb_container}" 120; then
  echo "guacdb did not become ready in time" >&2
  exit 1
fi

if [[ "$(docker exec "${guacdb_container}" psql -U guacuser -d guacdb -Atqc "select to_regclass('public.guacamole_entity')")" != "guacamole_entity" ]]; then
  docker run --rm guacamole/guacamole:1.6.0 /opt/guacamole/bin/initdb.sh --postgresql > "${schema_file}"
  docker cp "${schema_file}" "${guacdb_container}":/tmp/guacdb.sql
  docker exec -i "${guacdb_container}" \
    psql -U guacuser -d guacdb -v ON_ERROR_STOP=1 -f /tmp/guacdb.sql >/dev/null
fi

docker compose -f "${COMPOSE_FILE}" up -d --pull missing
guacd_container="$(wait_for_container guacd 30)"
install_guacd_fonts "${guacd_container}"
guacamole_container="$(wait_for_container guacamole 30)"
patch_guacamole_webapp "${guacamole_container}"
