#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

ENV_FILE="${NOVA_VE_ENV_FILE:-/etc/nova-ve/backend.env}"

if [[ -f "${ENV_FILE}" ]]; then
  # shellcheck disable=SC1091
  set -a && source "${ENV_FILE}" && set +a
fi

cd "${REPO_ROOT}/backend"
exec ./.venv/bin/alembic upgrade head
