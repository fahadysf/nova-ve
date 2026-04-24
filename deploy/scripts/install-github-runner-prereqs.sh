#!/usr/bin/env bash
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run this script as root." >&2
  exit 1
fi

# shellcheck disable=SC1091
. /etc/os-release

if [[ "${ID}" != "ubuntu" || "${VERSION_ID}" != "26.04" ]]; then
  echo "This runner prep script only supports Ubuntu 26.04." >&2
  exit 1
fi

if [[ "$(uname -m)" != "x86_64" ]]; then
  echo "This runner prep script only supports x86_64." >&2
  exit 1
fi

apt-get update
apt-get install -y --no-install-recommends \
  ca-certificates \
  curl \
  git \
  jq \
  build-essential \
  libpq-dev \
  python3-dev \
  docker.io \
  docker-compose-plugin \
  python3 \
  python3-venv \
  nodejs \
  npm

systemctl enable docker
systemctl restart docker

if ! id github-runner >/dev/null 2>&1; then
  useradd --create-home --shell /bin/bash github-runner
fi

usermod -aG docker github-runner
install -d -o github-runner -g github-runner /home/github-runner/actions-runner

echo "Runner prerequisites installed."
echo "Next: register the GitHub Actions runner as user github-runner with labels self-hosted,linux,x64,docker,nova-ve-ci."
