#!/usr/bin/env bash
# Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
# SPDX-License-Identifier: Apache-2.0
#
# Builds the bundled demo images and marks them for lab use.
#
# Images built/pulled:
#   - alpine:3.22                    (base image, also offered as a generic
#                                     lab option once marked)
#   - nova-ve/alpine-telnet:latest   (telnet console demo, see #181)
#   - nova-ve/alpine-vnc:latest      (VNC desktop demo)
#
# Marker convention: every image listed above also gets a
# ``nova-ve-lab/<sanitized>:<tag>`` reverse tag. The backend's image catalog
# only surfaces images carrying that marker, so guacamole/postgres/etc. stay
# out of the add-node modal even though they live on the same docker host.
#
# Honoured env vars:
#   NOVA_VE_SKIP_DEMO_IMAGES=1  Skip every build/pull cleanly. Provisioner
#                               uses this for re-deploys where the operator
#                               does not need the demo images rebuilt.
#
# Idempotency: external base images are pulled only when missing. Bundled demo
# images are rebuilt from the checked-out repo so stale local tags do not mask
# entrypoint/package fixes. Marker tags are refreshed when their source image
# id changes.
#
# Failure semantics: this script exits non-zero on docker-build/pull failure.
# The provisioner wraps the call so a build failure does not abort install.sh;
# it is recorded in nova-ve-install-summary.md instead.

set -euo pipefail

if [[ "${NOVA_VE_SKIP_DEMO_IMAGES:-0}" == "1" ]]; then
  echo "build-demo-images: NOVA_VE_SKIP_DEMO_IMAGES=1; skipping all builds." >&2
  exit 0
fi

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

# Marker namespace must match app.services.docker_image_service.MARKER_NAMESPACE.
MARKER_NS="nova-ve-lab"

image_present() {
  [[ -n "$(docker images -q "$1" 2>/dev/null)" ]]
}

image_id() {
  docker images -q "$1" 2>/dev/null | head -n1
}

# Mirror app.services.docker_image_service._sanitize_repo_for_marker so the
# marker tag matches what the backend would generate on a manual "Mark".
sanitize_repo() {
  local repo="$1"
  # lowercase, slashes/colons → underscores, then drop anything not in the
  # docker reference grammar.
  local lowered="${repo,,}"
  local flat="${lowered//\//_}"
  flat="${flat//:/_}"
  flat="$(echo "$flat" | sed -E 's/[^a-z0-9_.\-]+/_/g; s/^_+//; s/_+$//')"
  [[ -z "$flat" ]] && flat="untitled"
  echo "$flat"
}

marker_for() {
  local reference="$1"
  local repo="${reference%%:*}"
  local tag="${reference#*:}"
  [[ "$tag" == "$reference" ]] && tag="latest"
  echo "${MARKER_NS}/$(sanitize_repo "$repo"):${tag}"
}

mark_for_lab() {
  local reference="$1"
  local marker
  marker="$(marker_for "$reference")"
  if image_present "$marker"; then
    if [[ "$(image_id "$marker")" == "$(image_id "$reference")" ]]; then
      echo "build-demo-images: marker ${marker} already current; skipping." >&2
      return 0
    fi
    echo "build-demo-images: refreshing marker ${marker} -> ${reference}" >&2
  else
    echo "build-demo-images: tagging ${reference} -> ${marker}" >&2
  fi
  if ! image_present "$reference"; then
    echo "build-demo-images: source image ${reference} missing; cannot mark." >&2
    return 1
  fi
  docker tag "$reference" "$marker"
}

pull_if_missing() {
  local reference="$1"
  if image_present "$reference"; then
    echo "build-demo-images: ${reference} already present; skipping pull." >&2
    return 0
  fi
  echo "build-demo-images: pulling ${reference}" >&2
  docker pull "$reference"
}

build_demo_image() {
  local tag="$1"
  local context="$2"
  echo "build-demo-images: building ${tag} from ${context}" >&2
  docker build -t "$tag" "$context"
}

# 1. Base image (also the FROM for the two demos below).
pull_if_missing "alpine:3.22"

# 2. Demo images.
build_demo_image "nova-ve/alpine-telnet:latest" "${REPO_ROOT}/deploy/demo-images/alpine-telnet"
build_demo_image "nova-ve/alpine-vnc:latest"    "${REPO_ROOT}/deploy/demo-images/alpine-vnc"

# 3. Mark everything for lab availability so the add-node modal sees them by
# default. Operators can ``Unmark`` from the admin UI if they want a cleaner
# list.
mark_for_lab "alpine:3.22"
mark_for_lab "nova-ve/alpine-telnet:latest"
mark_for_lab "nova-ve/alpine-vnc:latest"

echo "build-demo-images: done." >&2
