#!/usr/bin/env bash
set -euo pipefail

HOST="${NOVA_VE_SMOKE_HOST:-127.0.0.1}"
SKIP_OS_CHECK="${NOVA_VE_SKIP_OS_CHECK:-0}"

retry() {
  local attempts="$1"
  shift
  local delay="$1"
  shift

  local i
  for i in $(seq 1 "${attempts}"); do
    if "$@"; then
      return 0
    fi
    if [[ "${i}" -lt "${attempts}" ]]; then
      sleep "${delay}"
    fi
  done
  return 1
}

if [[ "${SKIP_OS_CHECK}" != "1" ]]; then
  test "$(uname -m)" = "x86_64"
  # shellcheck disable=SC1091
  . /etc/os-release
  test "${ID}" = "ubuntu"
  test "${VERSION_ID}" = "26.04"
fi

if [[ -f /etc/nova-ve/backend.env ]]; then
  # shellcheck disable=SC1091
  set -a && source /etc/nova-ve/backend.env && set +a
fi

PSQL_URL="${DATABASE_URL:-postgresql+asyncpg://nova:nova@127.0.0.1:5432/novadb}"
PSQL_URL="${PSQL_URL/postgresql+asyncpg:/postgresql:}"
ADMIN_USERNAME="${NOVA_VE_ADMIN_USERNAME:-admin}"
if [[ -z "${NOVA_VE_ADMIN_PASSWORD:-}" ]]; then
  echo "NOVA_VE_ADMIN_PASSWORD must be set for smoke-check.sh" >&2
  exit 1
fi

if command -v systemctl >/dev/null 2>&1; then
  systemctl is-active --quiet docker
  systemctl is-active --quiet postgresql
  systemctl is-active --quiet caddy
  systemctl is-active --quiet nova-ve-backend
fi

pg_isready -h 127.0.0.1 -p 5432 -d novadb -U nova >/dev/null

if command -v psql >/dev/null 2>&1; then
  psql "${PSQL_URL}" -Atc "select username from users where username='${ADMIN_USERNAME}'" | grep -qx "${ADMIN_USERNAME}"
fi

retry 20 2 bash -lc "curl -fsS 'http://127.0.0.1:8000/api/health' | grep -q '\"database\":\"ok\"'"
retry 20 2 bash -lc "curl -fsS 'http://${HOST}/api/health' | grep -q '\"database\":\"ok\"'"
retry 20 2 bash -lc "curl -fsS 'http://${HOST}/' | grep -qi '<!doctype html'"
retry 20 2 bash -lc "curl -fsS 'http://${HOST}/html5/' | grep -qi 'guacamole'"
retry 10 2 curl -fsS -c /tmp/nova-ve-smoke.cookies \
  -H 'content-type: application/json' \
  -d "{\"username\":\"${ADMIN_USERNAME}\",\"password\":\"${NOVA_VE_ADMIN_PASSWORD}\"}" \
  "http://${HOST}/api/auth/login" >/dev/null

echo "nova-ve smoke checks passed"
