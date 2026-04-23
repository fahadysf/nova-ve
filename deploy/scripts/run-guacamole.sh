#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
ENV_FILE="${NOVA_VE_ENV_FILE:-/etc/nova-ve/backend.env}"
COMPOSE_FILE="${REPO_ROOT}/deploy/compose/guacamole-compose.yml"

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

docker compose -f "${COMPOSE_FILE}" up -d --pull missing guacdb
guacdb_container="$(docker compose -f "${COMPOSE_FILE}" ps -q guacdb)"
for _ in $(seq 1 30); do
  if docker exec "${guacdb_container}" pg_isready -U guacuser -d guacdb >/dev/null 2>&1; then
    break
  fi
  sleep 1
done
schema_file="$(mktemp)"
trap 'rm -f "${schema_file}"' EXIT
if ! docker exec "${guacdb_container}" pg_isready -U guacuser -d guacdb >/dev/null 2>&1; then
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
