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
  # by any restart. Patch the WAR so each deployment includes the client fixes.
  local container_id="$1"
  local war_path="/opt/guacamole/webapp/guacamole.war"

  local workdir
  workdir="$(mktemp -d)" || return 1
  trap 'rm -rf "${workdir}"' RETURN

  docker cp "${container_id}:${war_path}" "${workdir}/guacamole.war" || return 1

  local patch_rc=0
  python3 - "$workdir" "${GUAC_PATCH_JS}" <<'PYEOF' || patch_rc=$?
import sys
import zipfile
from pathlib import Path

workdir, patch_js = sys.argv[1], sys.argv[2]
patch_name = "nova-ve-guac-patch.js"
tunnel_asset = "guacamole-common-js/all.min.js"
original_socket = b'new WebSocket(b+"?"+f,"guacamole")'
patched_socket = b'new WebSocket(b+"?"+f)'

with zipfile.ZipFile(f"{workdir}/guacamole.war") as source:
    index = source.read("index.html")
    patched_index = index.replace(b",target-densitydpi=medium-dpi", b"")
    patched_index = patched_index.replace(
        f'src="{tunnel_asset}"'.encode(),
        f'src="{tunnel_asset}?v=nova-ve-websocket"'.encode(),
    )
    if patch_name.encode() not in patched_index:
        patched_index = patched_index.replace(
            b"</body>", f'<script src="{patch_name}"></script></body>'.encode()
        )

    # Guacamole accepts the tunnel without subprotocol negotiation. Some
    # network paths stall when the browser offers the named subprotocol.
    tunnel_js = source.read(tunnel_asset)
    if tunnel_js.count(original_socket) == 1:
        patched_tunnel = tunnel_js.replace(original_socket, patched_socket)
    elif original_socket not in tunnel_js and tunnel_js.count(patched_socket) == 1:
        patched_tunnel = tunnel_js
    else:
        sys.exit("Unexpected Guacamole WebSocket client in WAR")

    has_patch_script = patch_name in source.namelist()
    if patched_index == index and patched_tunnel == tunnel_js and has_patch_script:
        sys.exit(10)

    replacements = {"index.html": patched_index, tunnel_asset: patched_tunnel}
    with zipfile.ZipFile(f"{workdir}/guacamole-patched.war", "w", zipfile.ZIP_DEFLATED) as output:
        for entry in source.infolist():
            name = entry.filename
            data = replacements[name] if name in replacements else source.read(name)
            output.writestr(entry, data)
        if not has_patch_script:
            output.writestr(patch_name, Path(patch_js).read_bytes())
PYEOF
  if [[ "${patch_rc}" -eq 10 ]]; then
    return 0
  fi
  if [[ "${patch_rc}" -ne 0 ]]; then
    return 1
  fi

  docker cp "${workdir}/guacamole-patched.war" "${container_id}:${war_path}" || return 1
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
elif [[ "${war_rc}" -ne 0 ]]; then
  echo "guacamole: failed to patch the webapp" >&2
  exit "${war_rc}"
fi
