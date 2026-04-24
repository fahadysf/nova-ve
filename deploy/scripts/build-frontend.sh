#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
FRONTEND_ROOT="${NOVA_VE_FRONTEND_ROOT:-/var/lib/nova-ve/www}"

cd "${REPO_ROOT}/frontend"
if [[ -f package-lock.json ]]; then
  npm ci
else
  npm install
fi
npm run build

install -d "${FRONTEND_ROOT}"
find "${FRONTEND_ROOT}" -mindepth 1 -maxdepth 1 -exec rm -rf {} +
cp -a build/. "${FRONTEND_ROOT}/"
