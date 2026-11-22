#!/usr/bin/env bash
# Copyright (c) 2026 Fahad Yousuf <fahadysuf@gmail.com>
# SPDX-License-Identifier: Apache-2.0
#
# Builds the nova-ve platform containers (#288) and marks them for lab use.
#
# Images built:
#   - nova-ve/mgmt-server:latest  EVE eve-gui-server analog, no GUI:
#       python 3.13 + ansible/napalm/netmiko (floors in
#       deploy/platform-images/mgmt-server/requirements.txt), sshd, telnet
#       console, iperf3, tcpdump, git.
#   - nova-ve/browser:latest      EVE eve-firefox analog:
#       current-stable Firefox in openbox on Xvfb, VNC console on 5900,
#       telnet debug shell on 23.
#
# These images are intentionally NOT built at install time (the pip/git
# fetches make deploys slow). Build manually after pulling updates:
#   deploy/scripts/build-platform-images.sh
# Optionally verify a started container afterwards:
#   deploy/scripts/verify-platform-images.sh mgmt-server <container>
#   deploy/scripts/verify-platform-images.sh browser <container>
#
# Marker convention: matches deploy/scripts/build-demo-images.sh — every
# image also gets a ``nova-ve-lab/<sanitized>:<tag>`` reverse tag; the
# backend catalog only surfaces marked images.
#
# Honoured env vars:
#   NOVA_VE_SKIP_PLATFORM_IMAGES=1  Skip every build cleanly.
#
# Idempotency: images rebuild from the checked-out repo so stale local tags
# do not mask fixes; marker tags refresh when the source image id changes.

set -euo pipefail

if [[ "${NOVA_VE_SKIP_PLATFORM_IMAGES:-0}" == "1" ]]; then
  echo "build-platform-images: NOVA_VE_SKIP_PLATFORM_IMAGES=1; skipping all builds." >&2
  exit 0
fi

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PLATFORM_DIR="${REPO_ROOT}/deploy/platform-images"

MARKER_NS="nova-ve-lab"

image_id() {
  docker images -q "$1" 2>/dev/null | head -n1
}

sanitize_repo() {
  local repo="$1"
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
  if [[ "$(image_id "$marker" 2>/dev/null || true)" == "$(image_id "$reference")" ]]; then
    echo "build-platform-images: marker ${marker} already current; skipping." >&2
    return 0
  fi
  echo "build-platform-images: tagging ${reference} -> ${marker}" >&2
  docker tag "$reference" "$marker"
}

build_platform_image() {
  local tag="$1"
  local context="$2"
  echo "build-platform-images: building ${tag} from ${context}" >&2
  docker build -t "$tag" "$context"
}

build_platform_image "nova-ve/mgmt-server:latest" "${PLATFORM_DIR}/mgmt-server"
build_platform_image "nova-ve/browser:latest"     "${PLATFORM_DIR}/browser"

mark_for_lab "nova-ve/mgmt-server:latest"
mark_for_lab "nova-ve/browser:latest"

echo "build-platform-images: done." >&2
