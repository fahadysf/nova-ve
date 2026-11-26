#!/bin/sh
# Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
# SPDX-License-Identifier: Apache-2.0
#
# Turn on Kernel Samepage Merging and tune it for a lab host.
#
# Labs are the ideal case for KSM: many guests running the same images share
# almost every page of them. The stock defaults (100 pages every 20ms) scan far
# too gently to keep up with a lab being started, so this script enables the
# scanner with lab-measured rates once at boot (via nova-ve-ksm.service).
#
# Adaptation of the reference lab-engine KSM tuning: scanner rates
# pages_to_scan=1250 / sleep_millisecs=10, smart_scan where the kernel offers
# it, run set last. Every knob is optional: they come and go between kernel
# versions, and a missing one must not fail the unit and leave KSM off
# entirely. Overrides via /etc/nova-ve/nova-ve.env (NOVA_VE_KSM_*).
set -eu

KSM=/sys/kernel/mm/ksm
[ -d "$KSM" ] || { echo "no $KSM — this kernel has no KSM support"; exit 0; }

# Overrides: NOVA_VE_KSM_PAGES_TO_SCAN / NOVA_VE_KSM_SLEEP_MS / NOVA_VE_KSM_RUN
[ -r /etc/nova-ve/nova-ve.env ] && . /etc/nova-ve/nova-ve.env

set_knob() {
  # A missing knob is a note, not a failure. Leaving KSM off entirely over one
  # unsupported knob would trade a small optimisation for the whole feature.
  [ -w "$KSM/$1" ] || { echo "  $1: not available on this kernel"; return 0; }
  echo "$2" > "$KSM/$1" 2>/dev/null && echo "  $1 = $2" || echo "  $1: could not set"
}

# Scan harder than stock. 1250 pages every 10ms rather than 100 every 20ms:
# roughly 25x the scan rate, which is what makes dedup keep up with a lab
# being started rather than trailing minutes behind it.
set_knob pages_to_scan "${NOVA_VE_KSM_PAGES_TO_SCAN:-1250}"
set_knob sleep_millisecs "${NOVA_VE_KSM_SLEEP_MS:-10}"

# 6.7+ only. smart_scan skips pages repeatedly found unmergeable, which is
# most of a running VM's memory, so the scanner spends its budget where
# duplicates actually are.
set_knob smart_scan 1

# Last, so a failure above leaves KSM off rather than running untuned.
set_knob run "${NOVA_VE_KSM_RUN:-1}"
