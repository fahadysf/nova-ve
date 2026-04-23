#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
ENV_FILE="${NOVA_VE_ENV_FILE:-${REPO_ROOT}/.omx/local/backend.env}"

if [[ ! -f "${ENV_FILE}" ]]; then
  echo "Missing ${ENV_FILE}. Run deploy/scripts/setup-local-rancher.sh first." >&2
  exit 1
fi

# shellcheck disable=SC1090
set -a && source "${ENV_FILE}" && set +a

cd "${REPO_ROOT}/backend"
exec ./.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000
