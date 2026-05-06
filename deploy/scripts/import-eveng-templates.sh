#!/usr/bin/env bash
#
# import-eveng-templates.sh — root-asserter wrapper for the nova-ve EVE-NG importer.
#
# Sources /etc/nova-ve/backend.env (if present), asserts root, then exec's the
# venv'd Python module with the same argv. The Python module
# (backend/scripts/import_eveng) does the actual work.
#
# Tracking: #183 (umbrella #182).
#
set -euo pipefail

if [ "$EUID" -ne 0 ]; then
  echo "import-eveng-templates.sh: must run as root (use sudo)." >&2
  exit 2
fi

ENV_FILE="/etc/nova-ve/backend.env"
if [ -f "$ENV_FILE" ]; then
  # shellcheck disable=SC1090
  set -a
  . "$ENV_FILE"
  set +a
fi

REPO_DIR="${NOVA_VE_REPO_DIR:-/home/ubuntu/nova-ve-git}"
BACKEND_DIR="${REPO_DIR}/backend"
VENV_PYTHON="${BACKEND_DIR}/.venv/bin/python"

if [ ! -x "$VENV_PYTHON" ]; then
  echo "import-eveng-templates.sh: backend venv python not found at ${VENV_PYTHON}." >&2
  echo "                          Set NOVA_VE_REPO_DIR if the repo is checked out elsewhere." >&2
  exit 3
fi

cd "$BACKEND_DIR"
exec "$VENV_PYTHON" -m scripts.import_eveng "$@"
