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

patch_guacamole_war() {
  # The exploded webapp lives in an ephemeral /tmp/catalina-base.* that is
  # regenerated on every container start, so patching it in place is wiped
  # by any restart. Patch the WAR itself instead: every future explode then
  # carries the patch script + viewport fix. Idempotent — a WAR that already
  # contains the patch script is left untouched.
  local container_id="$1"
  local war_path="/opt/guacamole/webapp/guacamole.war"

  if docker exec "${container_id}" jar tf "${war_path}" 2>/dev/null | grep -q "nova-ve-guac-patch.js"; then
    return 0
  fi
  # Return codes: 0 = already patched, 3 = repacked (restart needed),
  # non-zero otherwise (set -e aborts before we get here on hard failure).

  local workdir
  workdir="$(mktemp -d)"
  trap 'rm -rf "${workdir}"' RETURN

  docker cp "${container_id}:${war_path}" "${workdir}/guacamole.war"

  python3 - "$workdir" "${GUAC_PATCH_JS}" <<'PYEOF'
import sys, zipfile

workdir, patch_js = sys.argv[1], sys.argv[2]
war = f"{workdir}/guacamole.war"

patch_source = open(patch_js, "rb").read()
index_html = None

with zipfile.ZipFile(war) as zin:
    entries = [(item, zin.read(item.filename)) for item in zin.infolist()]

for info, data in entries:
    if info.filename == "index.html":
        text = data.decode("utf-8")
        text = text.replace(",target-densitydpi=medium-dpi", "")
        if "nova-ve-guac-patch.js" not in text:
            text = text.replace("</body>", '<script src="nova-ve-guac-patch.js"></script></body>')
        index_html = text.encode("utf-8")
        break

if index_html is None:
    sys.exit("index.html not found in WAR")

with zipfile.ZipFile(f"{workdir}/guacamole-patched.war", "w", zipfile.ZIP_DEFLATED) as zout:
    for info, data in entries:
        if info.filename == "index.html":
            data = index_html
        zout.writestr(info.filename, data)
    zout.writestr("nova-ve-guac-patch.js", patch_source)
PYEOF

  docker cp "${workdir}/guacamole-patched.war" "${container_id}:${war_path}"
  return 3
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

war_rc=0
patch_guacamole_war "${guacamole_container}" || war_rc=$?
if [[ "${war_rc}" -eq 3 ]]; then
  # Freshly repacked WAR: restart the webapp so Tomcat re-explodes the
  # patched archive, then wait for it to answer again.
  echo "guacamole: WAR patched, restarting webapp to apply"
  docker compose -f "${COMPOSE_FILE}" restart guacamole >/dev/null
  for _ in $(seq 1 60); do
    if curl -fsS -o /dev/null "http://127.0.0.1:8081/html5/"; then
      break
    fi
    sleep 2
  done
fi
