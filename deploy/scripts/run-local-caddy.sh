#!/usr/bin/env bash
# Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
# SPDX-License-Identifier: Apache-2.0

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
BUILD_DIR="${REPO_ROOT}/frontend/build"
PORT="${NOVA_VE_FRONTDOOR_PORT:-5174}"
BACKEND_ORIGIN="${NOVA_VE_BACKEND_ORIGIN:-http://127.0.0.1:8000}"
HTML5_ORIGIN="${NOVA_VE_HTML5_ORIGIN:-http://127.0.0.1:18081}"
CADDYFILE="${REPO_ROOT}/.omx/local/Caddyfile"

mkdir -p "${REPO_ROOT}/.omx/local"

cat > "${CADDYFILE}" <<EOF
:${PORT} {
  root * ${BUILD_DIR}
  encode zstd gzip

  handle /api/* {
    reverse_proxy ${BACKEND_ORIGIN}
  }

  handle /VERSION {
    reverse_proxy ${BACKEND_ORIGIN}
  }

  handle /html5/* {
    reverse_proxy ${HTML5_ORIGIN}
  }

  handle {
    try_files {path} /index.html
    file_server
  }
}
EOF

exec /opt/homebrew/bin/caddy run --config "${CADDYFILE}"
