#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
LOCAL_ROOT="${REPO_ROOT}/.omx/local"
ENV_FILE="${LOCAL_ROOT}/backend.env"
LABS_DIR="${LOCAL_ROOT}/labs"
IMAGES_DIR="${LOCAL_ROOT}/images"
TMP_DIR="${LOCAL_ROOT}/tmp"
ADMIN_PASSWORD="${NOVA_VE_ADMIN_PASSWORD:-admin}"
SECRET_KEY="${SECRET_KEY:-dev-secret-key-do-not-use-in-production}"

detect_docker_host() {
  if [[ -n "${DOCKER_HOST:-}" ]]; then
    printf '%s\n' "${DOCKER_HOST}"
    return
  fi

  if [[ "$(uname -s)" == "Darwin" && -S "${HOME}/.rd/docker.sock" ]]; then
    printf 'unix://%s/.rd/docker.sock\n' "${HOME}"
    return
  fi

  printf 'unix:///var/run/docker.sock\n'
}

generate_secret() {
  python3 - <<'PY'
import secrets
print(secrets.token_hex(16))
PY
}

DOCKER_HOST_VALUE="$(detect_docker_host)"
GUACAMOLE_SECRET="${GUACAMOLE_JSON_SECRET_KEY:-$(generate_secret)}"

mkdir -p "${LOCAL_ROOT}" "${LABS_DIR}" "${IMAGES_DIR}" "${TMP_DIR}"
cp -f "${REPO_ROOT}/backend/labs/alpine-docker-demo.json" "${LABS_DIR}/alpine-docker-demo.json"

cat > "${ENV_FILE}" <<EOF
DATABASE_URL=postgresql+asyncpg://nova:nova@127.0.0.1:5432/novadb
SECRET_KEY=${SECRET_KEY}
DEBUG=True
COOKIE_SECURE=False
BASE_DATA_DIR=${LOCAL_ROOT}
LABS_DIR=${LABS_DIR}
IMAGES_DIR=${IMAGES_DIR}
TMP_DIR=${TMP_DIR}
DOCKER_HOST=${DOCKER_HOST_VALUE}
GUACAMOLE_PUBLIC_PATH=/html5/
GUACAMOLE_INTERNAL_URL=http://127.0.0.1:8081/html5/
GUACAMOLE_TARGET_HOST=host.docker.internal
GUACAMOLE_JSON_SECRET_KEY=${GUACAMOLE_SECRET}
NOVA_VE_ADMIN_PASSWORD=${ADMIN_PASSWORD}
EOF

set -a
source "${ENV_FILE}"
set +a

cd "${REPO_ROOT}"
docker compose up -d db
NOVA_VE_ENV_FILE="${ENV_FILE}" "${REPO_ROOT}/deploy/scripts/run-migrations.sh"
NOVA_VE_ENV_FILE="${ENV_FILE}" "${REPO_ROOT}/deploy/scripts/run-seed.sh"
"${REPO_ROOT}/deploy/scripts/build-demo-images.sh"
NOVA_VE_ENV_FILE="${ENV_FILE}" "${REPO_ROOT}/deploy/scripts/run-guacamole.sh"

cat <<EOF
Prepared local Rancher Desktop environment.
Env file: ${ENV_FILE}
Labs dir: ${LABS_DIR}
Run backend: NOVA_VE_ENV_FILE=${ENV_FILE} ${REPO_ROOT}/deploy/scripts/run-local-backend.sh
Run frontend: NOVA_VE_BACKEND_ORIGIN=http://127.0.0.1:8000 NOVA_VE_FRONTEND_PORT=5174 ${REPO_ROOT}/deploy/scripts/run-local-frontend.sh
EOF
