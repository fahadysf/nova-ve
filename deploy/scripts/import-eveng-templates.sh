#!/usr/bin/env bash
#
# import-eveng-templates.sh — root-asserter wrapper for the nova-ve EVE-NG importer.
#
# Sources /etc/nova-ve/backend.env (when readable), asserts root for mutating
# imports, then exec's the venv'd Python module with the same argv. The Python
# module (backend/scripts/import_eveng) does the actual work.
#
# Tracking: #183 (umbrella #182).
#
set -euo pipefail

READ_ONLY=0
for arg in "$@"; do
  case "$arg" in
    --dry-run|--help|-h)
      READ_ONLY=1
      ;;
  esac
done

if [ "$EUID" -ne 0 ] && [ "$READ_ONLY" -ne 1 ]; then
  echo "import-eveng-templates.sh: must run as root (use sudo)." >&2
  echo "                          --dry-run and --help may be run without sudo." >&2
  exit 2
fi

ENV_FILE="/etc/nova-ve/backend.env"
if [ -r "$ENV_FILE" ]; then
  # shellcheck disable=SC1090
  set -a
  . "$ENV_FILE"
  set +a
elif [ -f "$ENV_FILE" ] && [ "$EUID" -ne 0 ]; then
  echo "import-eveng-templates.sh: ${ENV_FILE} exists but is not readable; using checkout defaults." >&2
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT_REPO_DIR="$(cd "${SCRIPT_DIR}/../.." && pwd)"
REPO_DIR="${NOVA_VE_REPO_DIR:-${SCRIPT_REPO_DIR}}"
BACKEND_DIR="${REPO_DIR}/backend"
VENV_PYTHON="${BACKEND_DIR}/.venv/bin/python"

if [ ! -x "$VENV_PYTHON" ]; then
  echo "import-eveng-templates.sh: backend venv python not found at ${VENV_PYTHON}." >&2
  echo "                          Set NOVA_VE_REPO_DIR if the repo is checked out elsewhere." >&2
  exit 3
fi

cd "$BACKEND_DIR"
exec "$VENV_PYTHON" -m scripts.import_eveng "$@"
