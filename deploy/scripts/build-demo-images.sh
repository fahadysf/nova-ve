#!/usr/bin/env bash
# Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
# SPDX-License-Identifier: Apache-2.0

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

docker build \
  -t nova-ve-alpine-telnet:latest \
  "${REPO_ROOT}/deploy/demo-images/alpine-telnet"
