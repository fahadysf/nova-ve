#!/usr/bin/env bash
# Copyright (c) 2026 Fahad Yousuf <fahadysuf@gmail.com>
# SPDX-License-Identifier: Apache-2.0
#
# Builds the network management server, Browser Station, and MATE Lab
# Desktop images and marks them for use in labs.
#
# These images are intentionally NOT built at install time (the pip/git
# fetches make deploys slow). Build manually after pulling updates:
#   deploy/scripts/build-platform-images.sh
# Optionally verify a started container afterwards:
#   deploy/scripts/verify-platform-images.sh mgmt-server <container>
#   deploy/scripts/verify-platform-images.sh browser <container>
#   deploy/scripts/verify-platform-images.sh desktop <container>
#
# Marker convention: matches deploy/scripts/build-demo-images.sh — every
# image also gets a ``nova-ve-lab/<sanitized>:<tag>`` reverse tag; the
# backend catalog only surfaces marked images.
#
# GHCR: CI publishes the images automatically on main when the sources
# change (.github/workflows/build-platform-images.yml) as
#   ghcr.io/fahadysf/nova-ve-mgmt-server:latest
#   ghcr.io/fahadysf/nova-ve-browser:latest
#   ghcr.io/fahadysf/nova-ve-desktop:latest
# Container storage/bandwidth on GHCR is currently free, so fresh deploys
# can pull instead of building. Honoured env vars:
#   NOVA_VE_SKIP_PLATFORM_IMAGES=1  Skip every build/pull cleanly.
#   NOVA_VE_PLATFORM_MODE=auto|pull|build
#                                   auto: GHCR-first with local-build fallback
#                                   (what the provisioner uses — offline hosts
#                                   still get images); pull: GHCR only, strict
#                                   (requires registry access); build: build from
#                                   the checkout (default, dev semantics).
#   NOVA_VE_PLATFORM_PULL=1         Legacy alias for MODE=pull.
#   GHCR_PUSH=1                     Also tag+push the images to GHCR
#                                   (requires docker login ghcr.io with
#                                   write:packages).
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

GHCR_OWNER="${GHCR_OWNER:-fahadysf}"
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
  local image_dir="$2"
  echo "build-platform-images: building ${tag}" >&2
  if [[ "$image_dir" == "mgmt-server" ]]; then
    docker build -t "$tag" "${PLATFORM_DIR}/${image_dir}"
  else
    docker build -t "$tag" -f "${PLATFORM_DIR}/${image_dir}/Dockerfile" "$PLATFORM_DIR"
  fi
}

pull_platform_image() {
  local local_tag="$1"
  local ghcr_name="$2"
  echo "build-platform-images: pulling ghcr.io/${GHCR_OWNER}/${ghcr_name}:latest -> ${local_tag}" >&2
  docker pull "ghcr.io/${GHCR_OWNER}/${ghcr_name}:latest"
  docker tag "ghcr.io/${GHCR_OWNER}/${ghcr_name}:latest" "${local_tag}"
}

push_ghcr() {
  local local_tag="$1"
  local ghcr_name="$2"
  local reference="ghcr.io/${GHCR_OWNER}/${ghcr_name}:latest"
  echo "build-platform-images: pushing ${local_tag} -> ${reference}" >&2
  docker tag "${local_tag}" "${reference}"
  docker push "${reference}"
}

# image definitions: <local tag> <ghcr name> <image directory>
IMAGES=(
  "nova-ve/mgmt-server:latest|nova-ve-mgmt-server|mgmt-server"
  "nova-ve/browser:latest|nova-ve-browser|browser"
  "nova-ve/desktop:latest|nova-ve-desktop|desktop"
)

# Mode: build (default — dev semantics, always build from the checkout),
# pull (GHCR only, strict), auto (GHCR-first with local-build fallback —
# what the provisioner uses so offline hosts still get images).
MODE="${NOVA_VE_PLATFORM_MODE:-build}"
if [[ "${NOVA_VE_PLATFORM_PULL:-0}" == "1" ]]; then
  MODE="pull"
fi

ensure_platform_image() {
  local local_tag="$1"
  local ghcr_name="$2"
  local image_dir="$3"
  case "$MODE" in
    pull)
      pull_platform_image "$local_tag" "$ghcr_name"
      ;;
    build)
      build_platform_image "$local_tag" "$image_dir"
      ;;
    auto)
      # GHCR-first: the published images are the distribution source of
      # truth; if the registry is unreachable (offline / air-gapped host,
      # transient failure) fall back to building from the checked-out repo
      # so installs still succeed.
      if pull_platform_image "$local_tag" "$ghcr_name"; then
        return 0
      fi
      echo "build-platform-images: pull failed for ${local_tag}; falling back to local build" >&2
      build_platform_image "$local_tag" "$image_dir"
      ;;
    *)
      echo "build-platform-images: invalid NOVA_VE_PLATFORM_MODE '${MODE}' (auto|pull|build)" >&2
      exit 2
      ;;
  esac
}

for pair in "${IMAGES[@]}"; do
  IFS='|' read -r local_tag ghcr_name image_dir <<< "$pair"
  ensure_platform_image "$local_tag" "$ghcr_name" "$image_dir"
  mark_for_lab "$local_tag"
  if [[ "${GHCR_PUSH:-0}" == "1" ]]; then
    push_ghcr "$local_tag" "$ghcr_name"
  fi
done

echo "build-platform-images: done." >&2
