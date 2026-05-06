#!/usr/bin/env bash
# Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
# SPDX-License-Identifier: Apache-2.0
#
# Builds the bundled demo images (#191):
#   - nova-ve-alpine-telnet:latest (used by alpine docker-node demos; see #181)
#
# Honoured env vars:
#   NOVA_VE_SKIP_DEMO_IMAGES=1  Skip every build cleanly. Provisioner uses this
#                               for re-deploys where the operator does not need
#                               the demo images rebuilt.
#
# Idempotency: each image is rebuilt only when not already present locally
# (`docker images -q <tag>` is empty). Re-running the provisioner on a host
# that already has the image is a no-op.
#
# Failure semantics: this script exits non-zero on docker-build failure. The
# provisioner wraps the call so a build failure does not abort install.sh; it
# is recorded in nova-ve-install-summary.md instead.

set -euo pipefail

if [[ "${NOVA_VE_SKIP_DEMO_IMAGES:-0}" == "1" ]]; then
  echo "build-demo-images: NOVA_VE_SKIP_DEMO_IMAGES=1; skipping all builds." >&2
  exit 0
fi

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

build_if_missing() {
  local tag="$1"
  local context="$2"
  if [[ -n "$(docker images -q "$tag" 2>/dev/null)" ]]; then
    echo "build-demo-images: ${tag} already present; skipping." >&2
    return 0
  fi
  echo "build-demo-images: building ${tag} from ${context}" >&2
  docker build -t "$tag" "$context"
}

build_if_missing "nova-ve-alpine-telnet:latest" "${REPO_ROOT}/deploy/demo-images/alpine-telnet"
