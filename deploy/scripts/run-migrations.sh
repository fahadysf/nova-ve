#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

if [[ -f /etc/nova-ve/backend.env ]]; then
  # shellcheck disable=SC1091
  set -a && source /etc/nova-ve/backend.env && set +a
fi

cd "${REPO_ROOT}/backend"
exec ./.venv/bin/alembic upgrade head
