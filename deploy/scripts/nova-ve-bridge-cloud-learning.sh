#!/bin/bash
# Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
# SPDX-License-Identifier: Apache-2.0
#
# Disables MAC learning on the physical (eth*) uplink of every
# Bridge-Cloud br-eth* bridge.  Hypervisor vSwitches (notably VMware
# vSS in promiscuous mode) reflect the bridge's outbound broadcasts
# back in on the uplink; with learning enabled the bridge then learns
# every guest's MAC on the uplink port instead of the guest's veth and
# silently drops the inbound unicast reply via split-horizon.  See
# .omc/plans/bridge-cloud-feature.md.
#
# Idempotent: safe to re-run.  Only touches eth[0-9]+ slaves of
# br-eth[0-9]+ bridges; container veths and tap interfaces are left
# with learning on so guest MACs are still learned correctly.

set -u

LOG() {
    logger -t nova-ve-bridge-cloud-learning -- "$@"
    echo "$@" >&2
}

touched=0
for bdir in /sys/class/net/br-eth*; do
    [[ -d "${bdir}" ]] || continue
    bridge="$(basename "${bdir}")"
    [[ "${bridge}" =~ ^br-eth[0-9]+$ ]] || continue
    for sdir in "${bdir}/brif"/*; do
        [[ -e "${sdir}" ]] || continue
        slave="$(basename "${sdir}")"
        [[ "${slave}" =~ ^eth[0-9]+$ ]] || continue
        if bridge link set dev "${slave}" learning off 2>/dev/null; then
            LOG "set learning=off bridge=${bridge} slave=${slave}"
            touched=$((touched + 1))
        else
            LOG "WARN failed to set learning=off bridge=${bridge} slave=${slave}"
        fi
    done
done

LOG "applied to ${touched} bridge-cloud uplink port(s)"
exit 0
