#!/usr/bin/env bash
# Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
# SPDX-License-Identifier: Apache-2.0
#
# Ubuntu/XFCE VNC lab node. This borrows the upstream accetto
# ubuntu-vnc-xfce-g3 shape (Ubuntu + XFCE + VNC), but keeps VNC passwordless
# for Nova's Guacamole configuration and starts NetworkManager for lab links.
set -Eeuo pipefail

GEOM="${NOVA_VE_VNC_GEOMETRY:-1360x768x24}"
PORT="${NOVA_VE_VNC_PORT:-${VNC_PORT:-5900}}"
DISPLAY_NAME="${DISPLAY:-:0}"
DISPLAY_WAIT_SECS="${NOVA_VE_VNC_DISPLAY_WAIT_SECS:-15}"

PIDS=()

cleanup() {
  for pid in "${PIDS[@]:-}"; do
    if [[ -n "${pid}" ]] && kill -0 "${pid}" 2>/dev/null; then
      kill "${pid}" 2>/dev/null || true
    fi
  done
}
trap cleanup EXIT INT TERM

start_system_dbus() {
  mkdir -p /run/dbus
  if [[ ! -S /run/dbus/system_bus_socket ]]; then
    dbus-daemon --system --fork --nopidfile
  fi
}

start_network_manager() {
  mkdir -p /run/NetworkManager
  NetworkManager --no-daemon >/var/log/nova-ve-networkmanager.log 2>&1 &
  PIDS+=("$!")
}

start_display() {
  Xvfb "${DISPLAY_NAME}" -screen 0 "${GEOM}" -nolisten tcp &
  PIDS+=("$!")

  local elapsed=0
  while ! xdpyinfo -display "${DISPLAY_NAME}" >/dev/null 2>&1; do
    if ! kill -0 "${PIDS[-1]}" 2>/dev/null; then
      echo "ubuntu-2604-xfce-vnc: Xvfb exited before ${DISPLAY_NAME} became ready" >&2
      exit 1
    fi
    if [[ "${elapsed}" -ge "${DISPLAY_WAIT_SECS}" ]]; then
      echo "ubuntu-2604-xfce-vnc: timed out waiting for ${DISPLAY_NAME}" >&2
      exit 1
    fi
    elapsed=$((elapsed + 1))
    sleep 1
  done
}

start_desktop() {
  dbus-launch --exit-with-session startxfce4 >/var/log/nova-ve-xfce.log 2>&1 &
  PIDS+=("$!")
  nm-applet >/var/log/nova-ve-nm-applet.log 2>&1 &
  PIDS+=("$!")
  xterm -display "${DISPLAY_NAME}" -geometry 120x34 -fa Monospace -fs 11 -bg black -fg green -e /bin/bash -li &
  PIDS+=("$!")
}

start_system_dbus
start_network_manager
start_display
start_desktop

trap - EXIT INT TERM

exec x11vnc \
  -display "${DISPLAY_NAME}" \
  -rfbport "${PORT}" \
  -forever \
  -shared \
  -nopw \
  -quiet
