#!/usr/bin/env bash
# Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
# SPDX-License-Identifier: Apache-2.0

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
DRY_RUN=0
APP_OWNER="${NOVA_VE_SERVICE_USER:-${NOVA_VE_OWNER:-nova-ve}}"
APP_GROUP="${NOVA_VE_SERVICE_GROUP:-}"
APP_ROOT="${REPO_ROOT}"
ENV_DIR="/etc/nova-ve"
ENV_FILE="${ENV_DIR}/backend.env"
FRONTEND_ROOT="${NOVA_VE_FRONTEND_ROOT:-/var/lib/nova-ve/www}"
CADDY_TEMPLATE="${REPO_ROOT}/deploy/templates/Caddyfile.tpl"
BACKEND_SERVICE_TEMPLATE="${REPO_ROOT}/deploy/templates/nova-ve-backend.service.tpl"
PYTHON_BIN="${NOVA_VE_PYTHON_BIN:-python3}"
GUACAMOLE_COMPOSE_SCRIPT="${REPO_ROOT}/deploy/scripts/run-guacamole.sh"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run)
      DRY_RUN=1
      shift
      ;;
    *)
      echo "Unknown argument: $1" >&2
      exit 1
      ;;
  esac
done

run() {
  if [[ "${DRY_RUN}" -eq 1 ]]; then
    printf '+ %q' "$1"
    shift
    for arg in "$@"; do
      printf ' %q' "${arg}"
    done
    printf '\n'
    return 0
  fi
  "$@"
}

validate_service_user() {
  local user="$1"
  if [[ "${user}" == "root" ]]; then
    echo "NOVA_VE_SERVICE_USER must not be root" >&2
    exit 1
  fi
  if [[ ! "${user}" =~ ^[a-z_][a-z0-9_-]*[$]?$ ]]; then
    echo "Invalid NOVA_VE_SERVICE_USER '${user}'" >&2
    exit 1
  fi
}

ensure_service_account() {
  validate_service_user "${APP_OWNER}"
  if [[ "${DRY_RUN}" -eq 1 ]]; then
    echo "+ ensure service account ${APP_OWNER} exists"
    APP_GROUP="${APP_GROUP:-${APP_OWNER}}"
    return 0
  fi

  local home="${NOVA_VE_SERVICE_HOME:-/var/lib/nova-ve}"
  if id "${APP_OWNER}" >/dev/null 2>&1; then
    echo "Service account ${APP_OWNER} already exists; reusing it."
  else
    install -d -o root -g root -m 0755 "$(dirname "${home}")"
    useradd --system --create-home --home-dir "${home}" \
      --shell /usr/sbin/nologin --comment "nova-ve service account" "${APP_OWNER}"
    echo "Created service account ${APP_OWNER}."
  fi
}

resolve_app_identity() {
  if id "${APP_OWNER}" >/dev/null 2>&1; then
    APP_GROUP="$(id -gn "${APP_OWNER}")"
    return 0
  fi
  if [[ "${DRY_RUN}" -eq 1 ]]; then
    APP_GROUP="${APP_GROUP:-${APP_OWNER}}"
    return 0
  fi
  echo "Service account ${APP_OWNER} does not exist." >&2
  exit 1
}

require_target_host() {
  if [[ "${DRY_RUN}" -eq 1 ]]; then
    return 0
  fi
  if [[ "$(uname -m)" != "x86_64" ]]; then
    echo "This provisioning lane only supports x86_64 hosts." >&2
    exit 1
  fi
  # shellcheck disable=SC1091
  . /etc/os-release
  if [[ "${ID}" != "ubuntu" || "${VERSION_ID}" != "26.04" ]]; then
    echo "This provisioning lane only supports Ubuntu 26.04." >&2
    exit 1
  fi
  if [[ "${EUID}" -ne 0 ]]; then
    echo "Run this script as root." >&2
    exit 1
  fi
  if ! command -v "${PYTHON_BIN}" >/dev/null 2>&1; then
    echo "Required Python runtime ${PYTHON_BIN} is not installed." >&2
    exit 1
  fi
}

render_template() {
  local template="$1"
  local destination="$2"
  if [[ "${DRY_RUN}" -eq 1 ]]; then
    echo "+ render ${template} -> ${destination}"
    return 0
  fi
  sed \
    -e "s|__APP_ROOT__|${APP_ROOT}|g" \
    -e "s|__APP_OWNER__|${APP_OWNER}|g" \
    -e "s|__APP_GROUP__|${APP_GROUP}|g" \
    -e "s|__FRONTEND_ROOT__|${FRONTEND_ROOT}|g" \
    "${template}" > "${destination}"
}

ensure_env_var() {
  local key="$1"
  local value="$2"
  if grep -qE "^${key}=" "${ENV_FILE}"; then
    sed -i -E "s|^${key}=.*|${key}=${value}|" "${ENV_FILE}"
  else
    printf '%s=%s\n' "${key}" "${value}" >> "${ENV_FILE}"
  fi
}

ensure_database() {
  if [[ "${DRY_RUN}" -eq 1 ]]; then
    echo "+ ensure PostgreSQL role nova exists"
    echo "+ ensure PostgreSQL database novadb exists"
    return 0
  fi

  sudo -u postgres psql -v ON_ERROR_STOP=1 -c "DO \$\$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'nova') THEN CREATE ROLE nova LOGIN PASSWORD 'nova'; END IF; END \$\$;"

  if ! sudo -u postgres psql -Atqc "SELECT 1 FROM pg_database WHERE datname='novadb'" | grep -q '^1$'; then
    sudo -u postgres createdb -O nova novadb
  fi
}

ensure_backend_env() {
  if [[ "${DRY_RUN}" -eq 1 ]]; then
    echo "+ ensure ${ENV_FILE} exists"
    return 0
  fi

  local guacamole_secret
  local guacamole_db_password
  local guacamole_database_url
  local guacamole_data_source
  local guacamole_state_dir
  local public_path
  local target_host
  local expire_seconds
  install -d -m 0700 "${ENV_DIR}"
  if [[ ! -f "${ENV_FILE}" ]]; then
    local secret
    local admin_password
    secret="$("${PYTHON_BIN}" - <<'PY'
import secrets
print(secrets.token_urlsafe(48))
PY
)"
    admin_password="$("${PYTHON_BIN}" - <<'PY'
import secrets
print(secrets.token_urlsafe(24))
PY
)"
    guacamole_secret="$("${PYTHON_BIN}" - <<'PY'
import secrets
print(secrets.token_hex(16))
PY
)"
    guacamole_db_password="$("${PYTHON_BIN}" - <<'PY'
import secrets
print(secrets.token_urlsafe(24))
PY
)"
    install -m 0600 /dev/null "${ENV_FILE}"
    sed \
      -e "s|^SECRET_KEY=.*|SECRET_KEY=${secret}|" \
      -e "s|^NOVA_VE_ADMIN_PASSWORD=.*|NOVA_VE_ADMIN_PASSWORD=${admin_password}|" \
      -e "s|^GUACAMOLE_DB_PASSWORD=.*|GUACAMOLE_DB_PASSWORD=${guacamole_db_password}|" \
      -e "s|^GUACAMOLE_DATABASE_URL=.*|GUACAMOLE_DATABASE_URL=postgresql+asyncpg://guacuser:${guacamole_db_password}@127.0.0.1:5433/guacdb|" \
      -e "s|^GUACAMOLE_JSON_SECRET_KEY=.*|GUACAMOLE_JSON_SECRET_KEY=${guacamole_secret}|" \
      "${REPO_ROOT}/deploy/env/backend.env.example" > "${ENV_FILE}"
  fi

  public_path="$(awk -F= '/^GUACAMOLE_PUBLIC_PATH=/{print $2}' "${ENV_FILE}" | tail -n1)"
  target_host="$(awk -F= '/^GUACAMOLE_TARGET_HOST=/{print $2}' "${ENV_FILE}" | tail -n1)"
  expire_seconds="$(awk -F= '/^GUACAMOLE_JSON_EXPIRE_SECONDS=/{print $2}' "${ENV_FILE}" | tail -n1)"
  guacamole_secret="$(awk -F= '/^GUACAMOLE_JSON_SECRET_KEY=/{print $2}' "${ENV_FILE}" | tail -n1)"
  guacamole_db_password="$(awk -F= '/^GUACAMOLE_DB_PASSWORD=/{print $2}' "${ENV_FILE}" | tail -n1)"
  guacamole_database_url="$(awk -F= '/^GUACAMOLE_DATABASE_URL=/{print $2}' "${ENV_FILE}" | tail -n1)"
  guacamole_data_source="$(awk -F= '/^GUACAMOLE_DATA_SOURCE=/{print $2}' "${ENV_FILE}" | tail -n1)"
  guacamole_state_dir="$(awk -F= '/^GUACAMOLE_STATE_DIR=/{print $2}' "${ENV_FILE}" | tail -n1)"

  if [[ -z "${public_path}" ]]; then
    printf '\nGUACAMOLE_PUBLIC_PATH=/html5/\n' >> "${ENV_FILE}"
  fi
  if [[ -z "${target_host}" ]]; then
    printf 'GUACAMOLE_TARGET_HOST=host.docker.internal\n' >> "${ENV_FILE}"
  fi
  if [[ -z "${guacamole_state_dir}" ]]; then
    printf 'GUACAMOLE_STATE_DIR=/var/lib/nova-ve/guacamole\n' >> "${ENV_FILE}"
  fi
  if [[ -z "${guacamole_data_source}" ]]; then
    printf 'GUACAMOLE_DATA_SOURCE=postgresql\n' >> "${ENV_FILE}"
  fi
  if [[ -z "${expire_seconds}" ]]; then
    printf 'GUACAMOLE_JSON_EXPIRE_SECONDS=300\n' >> "${ENV_FILE}"
  fi
  if [[ -z "${guacamole_secret}" ]]; then
    guacamole_secret="$("${PYTHON_BIN}" - <<'PY'
import secrets
print(secrets.token_hex(16))
PY
)"
    printf 'GUACAMOLE_JSON_SECRET_KEY=%s\n' "${guacamole_secret}" >> "${ENV_FILE}"
  fi
  if [[ -z "${guacamole_db_password}" ]]; then
    guacamole_db_password="$("${PYTHON_BIN}" - <<'PY'
import secrets
print(secrets.token_urlsafe(24))
PY
)"
    printf 'GUACAMOLE_DB_PASSWORD=%s\n' "${guacamole_db_password}" >> "${ENV_FILE}"
  fi
  if [[ -z "${guacamole_database_url}" ]]; then
    printf 'GUACAMOLE_DATABASE_URL=postgresql+asyncpg://guacuser:%s@127.0.0.1:5433/guacdb\n' "${guacamole_db_password}" >> "${ENV_FILE}"
  fi
  ensure_env_var "NOVA_VE_SERVICE_USER" "${APP_OWNER}"
  ensure_env_var "NOVA_VE_OWNER" "${APP_OWNER}"
  ensure_env_var "NOVA_VE_REPO_DIR" "${APP_ROOT}"
  chmod 0600 "${ENV_FILE}"
}

install_nova_ve_net_helper() {
  # US-201: install the privileged network helper at /opt/nova-ve/bin and
  # drop the sudoers fragment into /etc/sudoers.d after a visudo dry-run.
  # Also install nova-ve-console-proxy.py: the helper's
  # console-proxy-start verb execs it, so the path must be present.
  local helper_src="${REPO_ROOT}/deploy/nova-ve-net.py"
  local proxy_src="${REPO_ROOT}/deploy/nova-ve-console-proxy.py"
  local sudoers_src="${REPO_ROOT}/deploy/nova-ve-sudoers"
  local helper_dst="/opt/nova-ve/bin/nova-ve-net.py"
  local proxy_dst="/opt/nova-ve/bin/nova-ve-console-proxy.py"
  local sudoers_dst="/etc/sudoers.d/nova-ve"
  local sudoers_tmp

  if [[ "${DRY_RUN}" -eq 1 ]]; then
    echo "+ install -d -o root -g root -m 0755 /opt/nova-ve/bin"
    echo "+ install -m 0755 -o root -g root ${helper_src} ${helper_dst}"
    echo "+ install -m 0755 -o root -g root ${proxy_src} ${proxy_dst}"
    echo "+ render ${sudoers_src} with service user ${APP_OWNER}"
    echo "+ visudo -cf <rendered sudoers>"
    echo "+ install -m 0440 -o root -g root ${sudoers_src} ${sudoers_dst}"
    return 0
  fi

  install -d -o root -g root -m 0755 /opt/nova-ve /opt/nova-ve/bin
  install -m 0755 -o root -g root "${helper_src}" "${helper_dst}"
  install -m 0755 -o root -g root "${proxy_src}" "${proxy_dst}"
  sudoers_tmp="$(mktemp /tmp/nova-ve-sudoers.XXXXXX)"
  render_template "${sudoers_src}" "${sudoers_tmp}"
  if ! visudo -cf "${sudoers_tmp}" >/dev/null; then
    echo "visudo rejected rendered ${sudoers_src}; aborting deploy" >&2
    rm -f "${sudoers_tmp}"
    exit 1
  fi
  install -m 0440 -o root -g root "${sudoers_tmp}" "${sudoers_dst}"
  rm -f "${sudoers_tmp}"
}

reconcile_nat_cloud_runtime() {
  local helper="/opt/nova-ve/bin/nova-ve-net.py"

  if [[ "${DRY_RUN}" -eq 1 ]]; then
    echo "+ reconcile existing NAT-Cloud runtime state from ${ENV_FILE}"
    return 0
  fi

  if [[ ! -x "${helper}" ]]; then
    echo "WARN: ${helper} is not installed; skipping NAT-Cloud runtime reconciliation." >&2
    return 0
  fi
  if [[ ! -f "${ENV_FILE}" ]]; then
    echo "WARN: ${ENV_FILE} is missing; skipping NAT-Cloud runtime reconciliation." >&2
    return 0
  fi

  # shellcheck disable=SC1090
  set -a; source "${ENV_FILE}"; set +a

  local labs_dir="${LABS_DIR:-/var/lib/nova-ve/labs}"
  if [[ ! -d "${labs_dir}" ]]; then
    echo "NAT-Cloud runtime reconciliation: no labs dir at ${labs_dir}; skipping."
    return 0
  fi

  local applied=0
  local skipped=0
  local failed=0
  local lab_file
  while IFS= read -r -d '' lab_file; do
    local rows
    rows="$(
      jq -r '
        .networks // {}
        | to_entries[]
        | select(.value.type == "nat_cloud")
        | [
            (.value.runtime.bridge_name // ""),
            (.value.config.cidr // ""),
            (.value.config.gateway // ""),
            ((.value.config.dhcp // true) | tostring),
            (.value.config.dhcp_start // ""),
            (.value.config.dhcp_end // ""),
            (.value.runtime.egress_interface // .value.config.egress_interface // "")
          ]
        | @tsv
      ' "${lab_file}" 2>/dev/null || true
    )"
    if [[ -z "${rows}" ]]; then
      continue
    fi

    while IFS=$'\t' read -r bridge cidr gateway dhcp_enabled dhcp_start dhcp_end egress; do
      if [[ -z "${bridge}" || -z "${cidr}" ]]; then
        skipped=$((skipped + 1))
        continue
      fi
      if ! ip link show "${bridge}" >/dev/null 2>&1; then
        skipped=$((skipped + 1))
        continue
      fi
      if [[ -z "${egress}" ]]; then
        egress="$("${helper}" default-egress 2>/dev/null || true)"
      fi
      if [[ -z "${egress}" ]]; then
        echo "WARN: no default egress interface found for NAT-Cloud bridge ${bridge}; skipping." >&2
        skipped=$((skipped + 1))
        continue
      fi

      if ! "${helper}" ipv4-forward-enable; then
        echo "WARN: failed to enable IPv4 forwarding for NAT-Cloud bridge ${bridge}." >&2
        failed=$((failed + 1))
        continue
      fi
      if ! "${helper}" nat-apply "${bridge}" "${cidr}" "${egress}"; then
        echo "WARN: failed to apply NAT for NAT-Cloud bridge ${bridge} via ${egress}." >&2
        failed=$((failed + 1))
        continue
      fi
      if ! "${helper}" forward-apply "${bridge}" "${cidr}" "${egress}"; then
        echo "WARN: failed to apply forwarding for NAT-Cloud bridge ${bridge} via ${egress}." >&2
        failed=$((failed + 1))
        continue
      fi
      if [[ "${dhcp_enabled}" == "true" && -n "${gateway}" && -n "${dhcp_start}" && -n "${dhcp_end}" ]]; then
        if ! "${helper}" dnsmasq-start "${bridge}" "${gateway}" "${dhcp_start}" "${dhcp_end}"; then
          echo "WARN: failed to restart dnsmasq for NAT-Cloud bridge ${bridge}." >&2
          failed=$((failed + 1))
          continue
        fi
      fi
      applied=$((applied + 1))
    done <<< "${rows}"
  done < <(find "${labs_dir}" -type f -name '*.json' -print0)

  echo "NAT-Cloud runtime reconciliation: applied=${applied} skipped=${skipped} failed=${failed}."
  return 0
}

ensure_instance_id() {
  local id_dir="/etc/nova-ve"
  local id_file="${id_dir}/instance_id"
  if [[ "${DRY_RUN}" -eq 1 ]]; then
    echo "+ ensure ${id_file} exists (idempotent)"
    return 0
  fi
  install -d -m 0755 -o root -g root "${id_dir}"
  if [[ ! -f "${id_file}" ]] || [[ ! -s "${id_file}" ]]; then
    cat /proc/sys/kernel/random/uuid > "${id_file}"
    chmod 0644 "${id_file}"
    chown root:root "${id_file}"
    echo "Generated instance ID: $(cat "${id_file}")"
  else
    echo "Instance ID already exists: $(cat "${id_file}")"
  fi
}

require_target_host
ensure_service_account
resolve_app_identity

run apt-get update
# NOTE: we do NOT install the apt ``dynamips`` package. Ubuntu's stock
# 0.2.14 (built Dec 2025 for resolute) segfaults on ``vm start`` for
# c3725 in hypervisor mode — confirmed reproducer logged with a core
# dump but no useful stderr. We build the GNS3 community fork (0.2.24,
# many crash fixes) from source instead via ``install_dynamips``.
# ``cmake``, ``libelf-dev``, ``libpcap-dev`` are its build deps.
run apt-get install -y --no-install-recommends \
  ca-certificates \
  curl \
  sudo \
  jq \
  openssl \
  build-essential \
  libpq-dev \
  python3-dev \
  postgresql \
  postgresql-contrib \
  caddy \
  docker.io \
  docker-compose-v2 \
  dnsmasq \
  nftables \
  python3 \
  python3-venv \
  nodejs \
  npm \
  cmake \
  libelf-dev \
  libpcap-dev

# Build + install dynamips 0.2.24 (GNS3 community fork). Idempotent:
# skipped if /usr/local/bin/dynamips is already at that version or
# newer. See note above for why we don't use the apt package.
install_bridge_cloud_helpers() {
  # Bridge-Cloud Phase B/C scripts.  Installed on every provision run so
  # the post-boot service has a stable location regardless of whether the
  # naming flip has already happened.
  local marker_src="${REPO_ROOT}/deploy/scripts/nova-ve-marker.sh"
  local marker_dst="/opt/nova-ve/bin/nova-ve-marker.sh"
  local netplan_src="${REPO_ROOT}/deploy/scripts/nova-ve-netplan-gen.py"
  local netplan_dst="/opt/nova-ve/bin/nova-ve-netplan-gen.py"
  local backup_src="${REPO_ROOT}/deploy/scripts/nova-ve-backup.py"
  local backup_dst="/opt/nova-ve/bin/nova-ve-backup.py"
  local phase_src="${REPO_ROOT}/deploy/scripts/nova-ve-bridge-cloud.sh"
  local phase_dst="/opt/nova-ve/bin/nova-ve-bridge-cloud.sh"
  local unit_src="${REPO_ROOT}/deploy/scripts/nova-ve-postboot.service.tpl"
  local unit_dst="/etc/systemd/system/nova-ve-postboot.service"
  local learning_src="${REPO_ROOT}/deploy/scripts/nova-ve-bridge-cloud-learning.sh"
  local learning_dst="/opt/nova-ve/bin/nova-ve-bridge-cloud-learning.sh"
  local learning_unit_src="${REPO_ROOT}/deploy/scripts/nova-ve-bridge-cloud-learning.service.tpl"
  local learning_unit_dst="/etc/systemd/system/nova-ve-bridge-cloud-learning.service"
  install -d -o root -g root -m 0755 /opt/nova-ve/bin
  install -d -o root -g root -m 0755 /etc/nova-ve
  install -m 0644 -o root -g root "${marker_src}" "${marker_dst}"
  install -m 0755 -o root -g root "${netplan_src}" "${netplan_dst}"
  install -m 0755 -o root -g root "${backup_src}" "${backup_dst}"
  install -m 0750 -o root -g root "${phase_src}" "${phase_dst}"
  install -m 0644 -o root -g root "${unit_src}" "${unit_dst}"
  install -m 0750 -o root -g root "${learning_src}" "${learning_dst}"
  install -m 0644 -o root -g root "${learning_unit_src}" "${learning_unit_dst}"
  systemctl daemon-reload 2>/dev/null || true
  systemctl enable nova-ve-bridge-cloud-learning.service 2>/dev/null || true
}

bridge_cloud_marker_state() {
  local marker=/etc/nova-ve/bridge-cloud.state
  if [[ -f "${marker}" ]]; then
    cat "${marker}" 2>/dev/null || echo absent
  else
    echo absent
  fi
}

bridge_cloud_cleanup_stale_transitional() {
  # Idempotent cleanup of the Phase B transitional netplan if it
  # lingers on a host whose marker is "complete".  Hosts provisioned
  # before the Phase C cleanup landed (commit 5454520) left this file
  # in place, which causes systemd-networkd to re-apply eth0's L3
  # config on every reconfigure — re-creating the dual-IP regression
  # (192.168.100.X on both eth0 AND br-eth0) and a second default
  # route via eth0.
  #
  # Safe to run unconditionally on every provision: only acts when the
  # marker says "complete" AND the file is present.
  #
  # IMPORTANT: this function deliberately does NOT call `netplan apply`.
  # `netplan apply` triggers `systemd-networkd` to issue RTM_DELROUTE
  # on every iface as part of the reconfigure cycle, then attempts to
  # re-add routes once carriers settle.  With a bridge whose slave
  # briefly flickers during reconfigure, the route re-add can silently
  # drop — and the host loses its default route despite the YAML being
  # correct, leaving it unreachable.  We instead make surgical kernel-
  # level changes (flush, route-del) and refresh networkd's view via
  # `netplan generate` + `networkctl reload`, which do not disrupt
  # carriers.
  local transitional=/etc/netplan/50-nova-ve-transitional.yaml
  [[ -f "${transitional}" ]] || return 0
  local state
  state="$(bridge_cloud_marker_state)"
  [[ "${state}" == "complete" ]] || return 0
  echo "bridge-cloud: removing stale Phase B transitional netplan (state=complete)"
  # Capture the current default-route gateway BEFORE any changes so we
  # can restore it if the per-iface route-del incidentally takes out
  # the bridge route.
  local pre_gw
  pre_gw="$(ip route show default 2>/dev/null | awk '/^default/ {print $3; exit}')"

  rm -f "${transitional}"

  # Flush any lingering inet on physical uplinks (the IP belongs only
  # on the bridge now) and drop the duplicate default route the
  # transitional netplan installed via the physical iface.
  local d iface gw
  for d in /sys/class/net/eth*; do
    [[ -d "${d}" ]] || continue
    iface="$(basename "${d}")"
    [[ "${iface}" =~ ^eth[0-9]+$ ]] || continue
    if ip -4 addr show dev "${iface}" 2>/dev/null | grep -q 'inet '; then
      ip -4 addr flush dev "${iface}" 2>/dev/null || true
      echo "bridge-cloud: flushed stale inet on ${iface}"
    fi
    gw="$(ip route show default 2>/dev/null | awk -v ifn="${iface}" '$1=="default" && $NF==ifn {print $3; exit}')"
    if [[ -n "${gw}" ]]; then
      ip route del default via "${gw}" dev "${iface}" 2>/dev/null || true
      echo "bridge-cloud: removed stale default route via ${gw} dev ${iface}"
    fi
  done

  # Refresh networkd's config view without disrupting carriers.  This
  # ensures a later `networkctl reconfigure` won't re-apply the now-
  # deleted transitional address.  `netplan generate` re-renders the
  # /run/systemd/network/ files; `networkctl reload` makes networkd
  # re-read them.  Neither call removes or re-adds kernel routes.
  netplan generate 2>/dev/null || true
  networkctl reload 2>/dev/null || true

  # Belt-and-suspenders: if any of the surgical changes incidentally
  # took out the bridge's default route, restore it.  This shouldn't
  # happen with the per-iface awk filter above, but we treat the host
  # losing reachability as a hard failure to avoid.
  if [[ -n "${pre_gw}" ]] && ! ip route show default 2>/dev/null | grep -q "via ${pre_gw} dev br-eth0"; then
    ip route replace default via "${pre_gw}" dev br-eth0 2>/dev/null || true
    echo "bridge-cloud: restored default route via ${pre_gw} dev br-eth0"
  fi
}

bridge_cloud_renderer_supported() {
  # Returns 0 if no /etc/netplan file declares ``renderer: NetworkManager``.
  if [[ ! -d /etc/netplan ]]; then
    return 0
  fi
  if grep -qrE '^\s*renderer:\s*NetworkManager' /etc/netplan/ 2>/dev/null; then
    return 1
  fi
  return 0
}

bridge_cloud_physical_ifaces() {
  # Phase C view: enumerate already-renamed eth* physical interfaces.
  for d in /sys/class/net/*; do
    local iface
    iface="$(basename "$d")"
    [[ "${iface}" == "lo" ]] && continue
    [[ ! -e "$d/device" ]] && continue
    [[ "${iface}" =~ ^eth[0-9]+$ ]] || continue
    echo "${iface}"
  done | sort -V
}

bridge_cloud_predictable_ifaces_sorted() {
  # Phase B view: enumerate every non-loopback PHYSICAL iface (any name —
  # predictable ``ens*``/``enp*``, biosdevname ``em*``/``p2p1``, or
  # already-renamed ``eth*``) sorted by PCI path so the order is stable
  # across reboots.  Writes ``<pci-path>\t<iface>\t<mac>`` one per line.
  #
  # Used to generate MAC-pin ``.link`` files BEFORE the kernel cmdline
  # flip — at that point eth0/eth1 don't exist yet on a fresh host.
  for d in /sys/class/net/*; do
    local iface
    iface="$(basename "$d")"
    [[ "${iface}" == "lo" ]] && continue
    [[ ! -e "$d/device" ]] && continue
    # Resolve the PCI/USB path so the sort key is iface-name-independent.
    local devpath
    devpath="$(readlink -f "$d/device" 2>/dev/null || echo "")"
    local mac
    mac="$(cat "$d/address" 2>/dev/null || echo "")"
    [[ -z "${mac}" ]] && continue
    printf '%s\t%s\t%s\n' "${devpath}" "${iface}" "${mac}"
  done | sort -k1,1
}

bridge_cloud_install_mac_pin_files() {
  # MAC-pin .link files are required on BOTH bare-metal AND VMs.
  # ``net.ifnames=0 biosdevname=0`` alone does NOT force ``ens*``/``enp*``
  # → ``ethN`` on Ubuntu 26.04 because systemd-networkd's default
  # ``99-default.link`` carries ``NamePolicy=keep kernel database onboard
  # slot path``, which still picks the predictable name from the kernel
  # ID database (e.g. vmxnet3 → ``ens192``).  An explicit ``.link`` file
  # with ``NamePolicy=`` (empty) + ``Name=ethN`` overrides this.
  install -d -o root -g root -m 0755 /etc/systemd/network
  local idx=0
  local devpath iface mac target path
  while IFS=$'\t' read -r devpath iface mac; do
    [[ -z "${mac}" ]] && continue
    target="eth${idx}"
    path="/etc/systemd/network/10-nova-ve-${target}.link"
    cat > "${path}" <<EOF
# Generated by deploy/scripts/provision-ubuntu-2604.sh
# Pin ${iface} (${mac}) → ${target} so the kernel cmdline flip
# (net.ifnames=0 biosdevname=0) maps this physical NIC to the
# expected ethN regardless of PCI enumeration changes.
[Match]
MACAddress=${mac}

[Link]
Name=${target}
NamePolicy=
EOF
    chmod 0644 "${path}"
    chown root:root "${path}"
    idx=$((idx + 1))
  done < <(bridge_cloud_predictable_ifaces_sorted)
  if [[ "${idx}" -eq 0 ]]; then
    echo "bridge-cloud: no physical interfaces with a MAC found; nothing to pin" >&2
  else
    echo "bridge-cloud: wrote ${idx} MAC-pin .link file(s) under /etc/systemd/network/"
  fi
}

bridge_cloud_write_transitional_netplan() {
  # The cloud-init / Ubuntu Server default netplan typically pins the
  # NIC name with ``set-name: ens192`` plus a ``match: macaddress`` block.
  # netplan compiles this into ``/run/udev/rules.d/99-netplan-ens192.rules``
  # which renames the device back to ``ens192`` AFTER our 10-nova-ve-eth0
  # .link file has set it to ``eth0`` — defeating the whole flip.
  #
  # Phase B therefore replaces /etc/netplan with a transitional config
  # whose ethernets entry is named ``eth0`` (matched by MAC, no set-name).
  # On the next boot this generates a udev rule that pins MAC → eth0,
  # consistent with our .link file.  Phase C later snapshots and
  # replaces this transitional config with the full Bridge-Cloud netplan.
  install -d -o root -g root -m 0755 /etc/nova-ve
  local pre_backup
  pre_backup="/etc/nova-ve/netplan.backup.phase-b.$(date +%s)"
  install -d -o root -g root -m 0700 "${pre_backup}"
  shopt -s nullglob
  for f in /etc/netplan/*.yaml; do
    cp -p "$f" "${pre_backup}/"
    rm -f "$f"
  done
  shopt -u nullglob

  # Suppress cloud-init's network config so it doesn't re-generate the
  # ens-pinned netplan on every boot.  This is the documented opt-out
  # mechanism in https://cloudinit.readthedocs.io/en/latest/topics/network-config.html
  install -d -o root -g root -m 0755 /etc/cloud/cloud.cfg.d
  cat > /etc/cloud/cloud.cfg.d/99-nova-ve-disable-network-config.cfg <<'EOF'
# Nova-VE Bridge-Cloud: cloud-init network re-generation is disabled so
# the operator-managed /etc/netplan files persist across reboots.
network: {config: disabled}
EOF
  chmod 0644 /etc/cloud/cloud.cfg.d/99-nova-ve-disable-network-config.cfg

  # Save the source ↔ target mapping for Phase C to migrate L3 config
  # from the original (ens*/enp*) stanzas onto the bridges.
  echo "${pre_backup}" > /etc/nova-ve/.bridge-cloud-source-netplan
  chmod 0600 /etc/nova-ve/.bridge-cloud-source-netplan

  # Build a transitional netplan that preserves the original L3 config
  # (addresses, routes, nameservers, dhcp) by MAC-matched migration.
  # POST-flip names (``eth0``, ``eth1``, …) consistent with the MAC-pin
  # ``.link`` files.  Phase C later replaces this file with the full
  # Bridge-Cloud bridges config.
  local idx=0
  local devpath iface mac
  local -a iface_macs=()
  local -a target_ifaces=()
  while IFS=$'\t' read -r devpath iface mac; do
    [[ -z "${mac}" ]] && continue
    target_ifaces+=("eth${idx}")
    iface_macs+=("--iface-mac" "eth${idx}:${mac}")
    idx=$((idx + 1))
  done < <(bridge_cloud_predictable_ifaces_sorted)

  local out=/etc/netplan/50-nova-ve-transitional.yaml
  if [[ ${#target_ifaces[@]} -gt 0 ]]; then
    /opt/nova-ve/bin/nova-ve-netplan-gen.py \
      --transitional \
      --migrate-from "${pre_backup}" \
      --out "${out}" \
      "${iface_macs[@]}" \
      "${target_ifaces[@]}"
    chmod 0600 "${out}"
    chown root:root "${out}"
    echo "bridge-cloud: wrote transitional netplan ${out} (${idx} iface(s))"
  else
    echo "bridge-cloud: WARN no physical ifaces found for transitional netplan" >&2
  fi
  echo "bridge-cloud: original netplan backed up to ${pre_backup}"
}

bridge_cloud_append_kernel_cmdline() {
  # Idempotent: only append if substring not already present.
  local grub=/etc/default/grub
  if [[ ! -f "${grub}" ]]; then
    echo "bridge-cloud: ${grub} missing; cannot configure kernel cmdline" >&2
    return 1
  fi
  if grep -qE 'GRUB_CMDLINE_LINUX=.*net\.ifnames=0' "${grub}" \
     && grep -qE 'GRUB_CMDLINE_LINUX=.*biosdevname=0' "${grub}"; then
    return 0
  fi
  cp -p "${grub}" "${grub}.nova-ve-bak.$(date +%s)"
  sed -i -E '
    s|^(GRUB_CMDLINE_LINUX=)"([^"]*)"$|\1"\2 net.ifnames=0 biosdevname=0"|
    s/  +/ /g
    s/" /"/
  ' "${grub}"
}

bridge_cloud_flip() {
  # Plan §5 — Phase B "naming flip" + scaffolding install.  Idempotent and
  # destructive (triggers a reboot when the marker is absent).  Re-runs on
  # already-migrated hosts short-circuit.  See
  # .omc/plans/bridge-cloud-feature.md §2 D4 / §4.1.
  if [[ "${NOVA_VE_SKIP_BRIDGE_CLOUD:-0}" == "1" ]]; then
    echo "bridge-cloud: NOVA_VE_SKIP_BRIDGE_CLOUD=1; skipping"
    return 0
  fi
  if [[ "${DRY_RUN}" -eq 1 ]]; then
    echo "+ bridge_cloud_flip (dry-run; no host changes)"
    return 0
  fi

  install_bridge_cloud_helpers
  # Idempotent guard: if a previous provision left the Phase B
  # transitional netplan on disk after Phase C completed, remove it
  # before doing anything else so subsequent networkctl reconfigure
  # calls don't re-create the dual-IP regression on eth0.
  bridge_cloud_cleanup_stale_transitional

  local state
  state="$(bridge_cloud_marker_state)"

  # State-aware re-run: auto-recover when the on-disk state contradicts
  # the marker.  Examples we want to fix without operator intervention:
  #   * marker=complete but ``br-eth0`` missing (e.g. snapshot revert)
  #   * marker=rollback-applied but rename succeeded after recovery
  #     and the host has eth0 ready for Phase C
  #   * marker=naming-flipped but the post-boot service finished
  #     successfully (shouldn't happen; defensive)
  # NOVA_VE_FORCE_RETRY_BRIDGE_CLOUD=1 forces a fresh Phase B regardless
  # of marker — useful when an operator has manually corrected a host
  # and wants the provision script to re-run end-to-end.
  if [[ "${NOVA_VE_FORCE_RETRY_BRIDGE_CLOUD:-0}" == "1" ]]; then
    echo "bridge-cloud: NOVA_VE_FORCE_RETRY_BRIDGE_CLOUD=1; clearing marker (was=${state})"
    rm -f /etc/nova-ve/bridge-cloud.state
    state="absent"
  fi

  case "${state}" in
    complete)
      if [[ -e /sys/class/net/br-eth0 ]]; then
        echo "bridge-cloud: already migrated, state=complete; skipping"
        return 0
      fi
      echo "bridge-cloud: marker=complete but br-eth0 missing — auto-recovery"
      rm -f /etc/nova-ve/bridge-cloud.state
      state="absent"
      ;;
    rollback-applied|rollback-applied-emergency)
      # Phase C ran and rolled back.  If the host is still on a
      # pre-flip iface (ens*/enp*/em*), it's safe to retry from
      # scratch — the rollback brought us back to the same starting
      # state.  If eth0 exists already, restart Phase C only.
      if [[ -e /sys/class/net/br-eth0 ]]; then
        echo "bridge-cloud: marker=${state} but br-eth0 already exists; assuming complete"
        source /opt/nova-ve/bin/nova-ve-marker.sh
        _write_marker_atomic complete
        return 0
      fi
      if [[ -e /sys/class/net/eth0 ]]; then
        echo "bridge-cloud: marker=${state} but eth0 ready — retrying Phase C"
        source /opt/nova-ve/bin/nova-ve-marker.sh
        _write_marker_atomic naming-flipped
        systemctl enable nova-ve-postboot.service 2>/dev/null || true
        systemctl restart nova-ve-postboot.service 2>/dev/null || true
        return 0
      fi
      echo "bridge-cloud: marker=${state}, no eth0/br-eth0 — auto-retrying Phase B"
      rm -f /etc/nova-ve/bridge-cloud.state
      state="absent"
      ;;
    unsupported-renderer)
      # The operator must remove the NetworkManager-managed netplan
      # before re-running.  Don't loop.
      echo "bridge-cloud: marker=unsupported-renderer; manual cleanup required" >&2
      echo "             see deploy/RECOVERY.md for the migration steps" >&2
      return 0
      ;;
    naming-flipped)
      # Post-boot service installed; bridge phase already enabled or done.
      systemctl enable nova-ve-postboot.service 2>/dev/null || true
      # If eth0 already exists, the reboot already happened — kick the
      # post-boot service so Phase C finishes on this same invocation.
      if [[ -e /sys/class/net/eth0 ]]; then
        echo "bridge-cloud: naming-flipped + eth0 present — re-firing Phase C"
        systemctl restart nova-ve-postboot.service 2>/dev/null || true
        return 0
      fi
      echo "bridge-cloud: naming-flipped marker present; post-boot service enabled"
      return 0
      ;;
  esac

  if ! bridge_cloud_renderer_supported; then
    echo "bridge-cloud: NetworkManager renderer detected — refusing naming flip" >&2
    source /opt/nova-ve/bin/nova-ve-marker.sh
    _write_marker_atomic unsupported-renderer
    return 0
  fi

  bridge_cloud_install_mac_pin_files
  bridge_cloud_write_transitional_netplan
  bridge_cloud_append_kernel_cmdline
  update-grub

  # Rebuild initramfs so the MAC-pin ``.link`` files reach the kernel
  # before the network driver loads.  Without this, vmxnet3 (and most
  # early-bound NIC drivers) name the device using the in-initramfs copy
  # of ``/etc/systemd/network/`` — which lacks our pin file — and the
  # post-boot udev rename only fires when the device first appears.
  if command -v update-initramfs >/dev/null 2>&1; then
    update-initramfs -u
  fi

  systemctl daemon-reload
  systemctl enable nova-ve-postboot.service

  # shellcheck disable=SC1091
  source /opt/nova-ve/bin/nova-ve-marker.sh
  _write_marker_atomic naming-flipped

  echo
  echo "==============================================================="
  echo "  bridge-cloud: reboot required to apply the iface naming flip"
  echo "  Press Ctrl-C within 30 seconds to abort."
  echo "==============================================================="
  for i in $(seq 30 -1 1); do
    echo "[bridge-cloud] reboot in ${i}s — Ctrl-C to abort"
    sleep 1
  done
  # ``--ignore-inhibitors`` overrides block-mode inhibitors (e.g. the
  # ``unattended-upgrades-shutdown`` lock that fires during apt cycles).
  # ``--no-block`` returns immediately so systemd-shutdown can proceed.
  systemctl reboot --ignore-inhibitors --no-block || systemctl --force --force reboot
}

install_dynamips() {
  local target=/usr/local/bin/dynamips
  local desired_version="0.2.24"
  if [[ -x "${target}" ]] && "${target}" --version 2>&1 | grep -q "${desired_version}"; then
    echo "dynamips ${desired_version} already installed at ${target}; skipping build."
    return 0
  fi
  local src=/var/lib/nova-ve/.build/dynamips
  run mkdir -p "${src}"
  if [[ ! -d "${src}/.git" ]]; then
    run git clone --depth=1 --branch=v"${desired_version}" \
      https://github.com/GNS3/dynamips.git "${src}"
  fi
  run bash -c "cd '${src}' && mkdir -p build && cd build && cmake .. -DCMAKE_BUILD_TYPE=Release -DBUILD_NVRAM_EXPORT=ON >/dev/null && make -j\$(nproc) >/dev/null"
  run install -m 0755 "${src}/build/stable/dynamips" "${target}"
  if [[ -x "${src}/build/nvram_export/nvram_export" ]]; then
    run install -m 0755 "${src}/build/nvram_export/nvram_export" /usr/local/bin/nvram_export
  fi
  "${target}" --version 2>&1 | sed -n '1p' || true
}
install_dynamips

run install -d -o "${APP_OWNER}" -g "${APP_GROUP}" -m 0755 /var/lib/nova-ve
run install -d -o "${APP_OWNER}" -g "${APP_GROUP}" -m 0755 /var/lib/nova-ve/labs /var/lib/nova-ve/images /var/lib/nova-ve/templates /var/lib/nova-ve/tmp /var/lib/nova-ve/guacamole /var/lib/nova-ve/guacamole/db /var/lib/nova-ve/runtime "${FRONTEND_ROOT}"
run chown -R "${APP_OWNER}:${APP_GROUP}" /var/lib/nova-ve
install_nova_ve_net_helper
run systemctl enable docker
run systemctl restart docker
run usermod -aG docker "${APP_OWNER}"
if getent group kvm >/dev/null 2>&1; then
  run usermod -aG kvm "${APP_OWNER}"
fi

# Build bundled demo images (NOVA_VE_SKIP_DEMO_IMAGES=1 to skip; idempotent —
# only builds images that are not already present locally). Build failure
# does NOT abort the provisioner; the install summary records the status.
DEMO_IMAGES_STATUS_FILE=/var/lib/nova-ve/.demo-images-status
run rm -f "${DEMO_IMAGES_STATUS_FILE}"
DEMO_BUILD_CMD=(sudo -u "${APP_OWNER}" -H bash "${REPO_ROOT}/deploy/scripts/build-demo-images.sh")
if [[ "${NOVA_VE_SKIP_DEMO_IMAGES:-0}" == "1" ]]; then
  DEMO_BUILD_CMD=(env NOVA_VE_SKIP_DEMO_IMAGES=1 "${DEMO_BUILD_CMD[@]}")
fi
if run "${DEMO_BUILD_CMD[@]}"; then
  if [[ "${NOVA_VE_SKIP_DEMO_IMAGES:-0}" == "1" ]]; then
    run bash -c "echo skipped > ${DEMO_IMAGES_STATUS_FILE}"
  else
    run bash -c "echo ok > ${DEMO_IMAGES_STATUS_FILE}"
  fi
else
  run bash -c "echo failed > ${DEMO_IMAGES_STATUS_FILE}"
  echo "WARN: demo-image build failed; alpine:3.22 / nova-ve/alpine-telnet / nova-ve/alpine-vnc may not be available locally for labs." >&2
fi
run chown "${APP_OWNER}:${APP_GROUP}" "${DEMO_IMAGES_STATUS_FILE}" || true

ensure_backend_env
ensure_instance_id

ensure_database

run "${PYTHON_BIN}" -m venv "${REPO_ROOT}/backend/.venv"
run "${REPO_ROOT}/backend/.venv/bin/pip" install --upgrade pip
run "${REPO_ROOT}/backend/.venv/bin/pip" install -r "${REPO_ROOT}/backend/requirements.txt"

run "${REPO_ROOT}/deploy/scripts/run-migrations.sh"
run "${REPO_ROOT}/deploy/scripts/run-seed.sh"
run "${REPO_ROOT}/deploy/scripts/build-frontend.sh"
run "${GUACAMOLE_COMPOSE_SCRIPT}"

render_template "${CADDY_TEMPLATE}" /etc/caddy/Caddyfile
render_template "${BACKEND_SERVICE_TEMPLATE}" /etc/systemd/system/nova-ve-backend.service
run caddy validate --config /etc/caddy/Caddyfile

run systemctl daemon-reload
run systemctl enable docker
run systemctl enable postgresql
run systemctl enable caddy
run systemctl enable nova-ve-backend
run systemctl restart docker
run systemctl restart postgresql
run systemctl restart nova-ve-backend
run systemctl restart caddy

reconcile_nat_cloud_runtime

run "${REPO_ROOT}/deploy/scripts/smoke-check.sh"

if [[ "${DRY_RUN}" -eq 0 ]]; then
  echo "Bootstrap admin credentials are stored in ${ENV_FILE}."
fi

# Bridge-Cloud naming flip + post-boot scaffolding.  Always-on with a
# 30-second Ctrl-C abort window per plan §2 D4.  Re-runs on already-
# migrated hosts log "already migrated" and return without rebooting.
bridge_cloud_flip
