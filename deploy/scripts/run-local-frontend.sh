#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

export NOVA_VE_BACKEND_ORIGIN="${NOVA_VE_BACKEND_ORIGIN:-http://127.0.0.1:8000}"
export NOVA_VE_FRONTEND_PORT="${NOVA_VE_FRONTEND_PORT:-5174}"
export NOVA_VE_HTML5_ORIGIN="${NOVA_VE_HTML5_ORIGIN:-http://127.0.0.1:8081}"

cd "${REPO_ROOT}/frontend"
exec npm run dev -- --host 127.0.0.1 --port "${NOVA_VE_FRONTEND_PORT}"
