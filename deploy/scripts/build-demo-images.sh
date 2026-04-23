#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

docker build \
  -t nova-ve-alpine-telnet:latest \
  "${REPO_ROOT}/deploy/demo-images/alpine-telnet"
