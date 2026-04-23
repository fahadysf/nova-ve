#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
ENV_FILE="/etc/nova-ve/backend.env"
COMPOSE_FILE="${REPO_ROOT}/deploy/compose/guacamole-compose.yml"

if [[ ! -f "${ENV_FILE}" ]]; then
  echo "Missing ${ENV_FILE}" >&2
  exit 1
fi

# shellcheck disable=SC1091
set -a && source "${ENV_FILE}" && set +a

if [[ -z "${GUACAMOLE_JSON_SECRET_KEY:-}" ]]; then
  echo "GUACAMOLE_JSON_SECRET_KEY is not set in ${ENV_FILE}" >&2
  exit 1
fi

docker compose -f "${COMPOSE_FILE}" up -d --pull missing
