#!/bin/bash
# Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
# SPDX-License-Identifier: Apache-2.0
#
# Phase C of the Bridge-Cloud install flow — runs once after the reboot
# triggered by Phase B (naming flip).  Generates the netplan, applies
# it under a 60s timeout, verifies outbound reachability of the
# pre-existing default gateway from ``br-eth0``, and on any failure
# restores the netplan backup and writes the appropriate marker state.
#
# See .omc/plans/bridge-cloud-feature.md §6 for the full state machine.

set -u

MARKER=/etc/nova-ve/bridge-cloud.state
BACKUP_ROOT="/etc/nova-ve/netplan.backup.$(date +%s)"
NETPLAN_GEN=/opt/nova-ve/bin/nova-ve-netplan-gen.py
BACKUP_PY=/opt/nova-ve/bin/nova-ve-backup.py

LOG() {
    logger -t nova-ve-postboot -- "$@"
    echo "$@" >&2
}

# shellcheck disable=SC1091
source /opt/nova-ve/bin/nova-ve-marker.sh   # provides _write_marker_atomic

state="$(cat "$MARKER" 2>/dev/null || echo absent)"
if [[ "$state" != "naming-flipped" ]]; then
    LOG "phase=skip state=$state"
    exit 0
fi

# ----- 0. Wait for kernel + udev to settle so all NICs have their final
#         names before enumeration.  Without this we can race the
#         vmxnet3/e1000/etc. driver init on slow boots.
LOG "phase=settle"
udevadm settle --timeout=10 2>/dev/null || true
# Give DHCP/networkd a moment to bring the iface up on the transitional
# netplan (the Phase B file pinned eth0 by MAC); not strictly required
# for enumeration but helps the gateway-ping check later.
for _ in $(seq 1 10); do
    if ip -br link show eth0 2>/dev/null | grep -q UP; then
        break
    fi
    sleep 1
done

# ----- 1. Enumerate physical ethN interfaces -------------------------------
mapfile -t IFACES < <(
    for d in /sys/class/net/*; do
        i="$(basename "$d")"
        [[ "$i" == "lo" ]] && continue
        [[ ! -e "$d/device" ]] && continue
        [[ "$i" =~ ^eth[0-9]+$ ]] && echo "$i"
    done | sort -V
)
if [[ ${#IFACES[@]} -eq 0 ]]; then
    # Pre-enumeration failure: nothing has been touched yet, so leave the
    # marker at ``naming-flipped`` and let the next invocation retry.
    # Setting ``rollback-applied`` here would lock the operator out of an
    # automatic retry even though no rollback was ever performed.
    LOG "phase=fail reason=no-physical-ifaces — leaving marker at naming-flipped for retry"
    LOG "phase=fail current_ifaces=$(ip -br link | awk '{print $1}' | tr '\n' ',' | sed 's/,$//')"
    exit 1
fi
LOG "phase=enumerate ifaces=${IFACES[*]}"

# ----- 2. Renderer detection — refuse NetworkManager-managed hosts ---------
if grep -qrE '^\s*renderer:\s*NetworkManager' /etc/netplan/ 2>/dev/null; then
    LOG "phase=abort reason=unsupported-renderer"
    _write_marker_atomic unsupported-renderer
    exit 1
fi

# ----- 3. Snapshot existing netplan via symlink-refusing helper ------------
mkdir -p "$BACKUP_ROOT"
chmod 0700 "$BACKUP_ROOT"
chown root:root "$BACKUP_ROOT" 2>/dev/null || true
if ! "$BACKUP_PY" snapshot /etc/netplan "$BACKUP_ROOT"; then
    LOG "phase=fail reason=snapshot-refused"
    _write_marker_atomic rollback-applied
    exit 1
fi
ip route show default > "$BACKUP_ROOT/route-pre.txt" 2>/dev/null || true
PRE_GW="$(awk '/^default/{print $3; exit}' "$BACKUP_ROOT/route-pre.txt" 2>/dev/null)"
LOG "phase=snapshot dir=$BACKUP_ROOT pre_gw=${PRE_GW:-<none>}"

# ----- 4. Generate the new netplan -----------------------------------------
# Pass the Phase B source-netplan backup so the bridge inherits the
# original L3 config (addresses / routes / nameservers / dhcp settings).
# Without this, a host with a static IP would be silently downgraded to
# DHCP after the bridge takeover.
GEN_ARGS=()
if [[ -f /etc/nova-ve/.bridge-cloud-source-netplan ]]; then
    SOURCE_DIR="$(cat /etc/nova-ve/.bridge-cloud-source-netplan 2>/dev/null)"
    if [[ -n "${SOURCE_DIR}" && -d "${SOURCE_DIR}" ]]; then
        GEN_ARGS+=("--migrate-from" "${SOURCE_DIR}")
        for i in "${IFACES[@]}"; do
            mac="$(cat "/sys/class/net/${i}/address" 2>/dev/null || true)"
            if [[ -n "${mac}" ]]; then
                GEN_ARGS+=("--iface-mac" "${i}:${mac}")
            fi
        done
        LOG "phase=migrate source=${SOURCE_DIR}"
    fi
fi
if ! "$NETPLAN_GEN" "${GEN_ARGS[@]}" "${IFACES[@]}"; then
    LOG "phase=fail reason=netplan-gen-failed"
    _write_marker_atomic rollback-applied
    exit 1
fi
LOG "phase=netplan-gen out=/etc/netplan/60-nova-ve-bridge-cloud.yaml"

# Remove the Phase B transitional netplan so the bridge file is the
# only source of truth for the parent ethernet stanza.  systemd-networkd
# is non-destructive: leaving the transitional file in place means the
# parent NIC's IPs / routes from Phase B linger after the bridge takes
# over and the host ends up dual-addressed (IP on both eth0 and br-eth0).
if [[ -f /etc/netplan/50-nova-ve-transitional.yaml ]]; then
    rm -f /etc/netplan/50-nova-ve-transitional.yaml
    LOG "phase=cleanup removed=/etc/netplan/50-nova-ve-transitional.yaml"
fi

restore_and_apply() {
    "$BACKUP_PY" restore "$BACKUP_ROOT" /etc/netplan
    rm -f /etc/netplan/60-nova-ve-bridge-cloud.yaml
    if ! timeout 60 netplan apply; then
        LOG "phase=critical reason=rollback-apply-timed-out"
        _write_marker_atomic rollback-applied-emergency
        return 2
    fi
    return 0
}

# ----- 5. Apply with timeout ------------------------------------------------
if ! timeout 60 netplan apply; then
    LOG "phase=fail reason=netplan-apply-timeout"
    restore_and_apply; rc=$?
    if [[ $rc -ne 2 ]]; then
        _write_marker_atomic rollback-applied
    fi
    exit 1
fi
LOG "phase=apply rc=0"

# Post-apply reconciliation: systemd-networkd is non-destructive, so
# any IPv4/IPv6 address that was on the parent NIC before this apply
# stays in the kernel.  Strip everything but link-local from each
# parent so the IP only lives on the bridge.  We deliberately keep
# IPv6 link-local (``fe80::``) — it's auto-generated and harmless.
for i in "${IFACES[@]}"; do
    if [[ -e "/sys/class/net/${i}" ]]; then
        if ip -4 addr show dev "${i}" | grep -q 'inet '; then
            ip -4 addr flush dev "${i}" 2>/dev/null || true
            LOG "phase=cleanup flushed_inet=${i}"
        fi
        if ip -6 addr show dev "${i}" | grep -E 'inet6 (?!fe80)' >/dev/null 2>&1; then
            ip -6 addr show dev "${i}" | awk '/inet6 / && $2 !~ /^fe80/ {print $2}' | \
                while read -r addr; do
                    [[ -z "${addr}" ]] && continue
                    ip -6 addr del "${addr}" dev "${i}" 2>/dev/null || true
                    LOG "phase=cleanup flushed_inet6=${i} addr=${addr}"
                done
        fi
    fi
done

# Force a networkd reconcile so the routes / DNS from the new netplan
# definitely take effect — apply alone can leave routes pending in
# rare cases when the parent NIC was actively transitioning.
networkctl reload 2>/dev/null || true
networkctl reconfigure "${IFACES[@]}" 2>/dev/null || true
for b in "${IFACES[@]}"; do
    networkctl reconfigure "br-${b}" 2>/dev/null || true
done

# ----- 6. Wait for br-eth0 carrier (best-effort, <=30s) ---------------------
for _ in $(seq 1 30); do
    if [[ "$(cat /sys/class/net/br-eth0/carrier 2>/dev/null)" == "1" ]]; then
        break
    fi
    sleep 1
done

# ----- 7. Gateway reachability check (5 × 3s) -------------------------------
if [[ -n "$PRE_GW" ]]; then
    # If the default route didn't land on br-eth0 (rare apply race),
    # install it manually before the ping check.  netplan generated the
    # route in the YAML; this is a defensive belt-and-suspenders.
    if ! ip route show default | grep -q "via $PRE_GW dev br-eth0"; then
        if ip route show default | grep -q "via $PRE_GW"; then
            # Default route exists but on a different device — replace it.
            ip route replace default via "$PRE_GW" dev br-eth0 2>/dev/null || true
            LOG "phase=route-fix action=replace-default gw=$PRE_GW dev=br-eth0"
        else
            # No default route at all — install one.
            ip route add default via "$PRE_GW" dev br-eth0 2>/dev/null || true
            LOG "phase=route-fix action=add-default gw=$PRE_GW dev=br-eth0"
        fi
    fi

    rc=1
    for _ in $(seq 1 5); do
        if ping -c 1 -W 3 -I br-eth0 "$PRE_GW" >/dev/null 2>&1; then
            rc=0
            break
        fi
    done
    if [[ $rc -ne 0 ]]; then
        LOG "phase=rollback reason=gateway-unreachable gw=$PRE_GW"
        restore_and_apply; rrc=$?
        if [[ $rrc -ne 2 ]]; then
            _write_marker_atomic rollback-applied
        fi
        exit 1
    fi
    LOG "phase=gateway-check rc=0 gw=$PRE_GW"
fi

# ----- 7.5. Disable MAC learning on the uplink -----------------------------
# Hypervisor vSwitches (notably VMware vSS in promiscuous mode) reflect
# the bridge's outbound broadcasts back in on the uplink.  With learning
# on, the bridge then learns lab guest MACs on the eth* port instead of
# the guest's veth and silently drops their inbound unicast replies via
# split-horizon.
#
# Two layers of defence:
#   (a) Drop a systemd-networkd Learning=no override on each netplan-
#       generated eth* .network file so networkd applies it natively on
#       every interface bring-up (boot, link flap, manual reconfigure).
#   (b) Run the helper script now and via a boot-time oneshot service
#       to catch cases where the drop-in is missed (e.g. netplan naming
#       changes in a future Ubuntu release).
for i in "${IFACES[@]}"; do
    dropin_dir="/etc/systemd/network/10-netplan-${i}.network.d"
    mkdir -p "${dropin_dir}"
    cat > "${dropin_dir}/99-nova-ve-learning.conf" <<EOF
# Generated by /opt/nova-ve/bin/nova-ve-bridge-cloud.sh
# Disable MAC learning on the Bridge-Cloud uplink to prevent FDB
# poisoning from hypervisor vSwitch broadcast reflection.
[Bridge]
Learning=no
EOF
    chmod 0644 "${dropin_dir}/99-nova-ve-learning.conf"
    chown root:root "${dropin_dir}/99-nova-ve-learning.conf"
    LOG "phase=learning-dropin path=${dropin_dir}/99-nova-ve-learning.conf"
done
networkctl reload 2>/dev/null || true

if [[ -x /opt/nova-ve/bin/nova-ve-bridge-cloud-learning.sh ]]; then
    /opt/nova-ve/bin/nova-ve-bridge-cloud-learning.sh || \
        LOG "WARN learning helper returned non-zero (continuing)"
else
    LOG "WARN /opt/nova-ve/bin/nova-ve-bridge-cloud-learning.sh missing — uplink learning left enabled"
fi

# ----- 8. Success -----------------------------------------------------------
_write_marker_atomic complete
systemctl disable nova-ve-postboot.service 2>/dev/null || true
LOG "phase=complete ifaces=${IFACES[*]}"
exit 0
