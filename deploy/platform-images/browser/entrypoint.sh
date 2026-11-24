#!/bin/bash
# Copyright (c) 2026 Fahad Yousuf <fahadysuf@gmail.com>
# SPDX-License-Identifier: Apache-2.0
set -euo pipefail

GEOM="${NOVA_VE_VNC_GEOMETRY:-1280x800x24}"
PORT="${NOVA_VE_VNC_PORT:-5900}"
export DISPLAY="${DISPLAY:-:0}"
DISPLAY_WAIT_SECS="${NOVA_VE_VNC_DISPLAY_WAIT_SECS:-10}"

pids=()
cleanup() {
  if (( ${#pids[@]} )); then
    kill "${pids[@]}" 2>/dev/null || true
    wait "${pids[@]}" 2>/dev/null || true
  fi
}
trap cleanup EXIT
trap 'exit 0' INT TERM

start_vnc() {
  local xvfb_pid elapsed=0
  Xvfb "$DISPLAY" -screen 0 "$GEOM" -nolisten tcp &
  xvfb_pid=$!
  pids+=("$xvfb_pid")
  until xdpyinfo >/dev/null 2>&1; do
    if ! kill -0 "$xvfb_pid" 2>/dev/null || (( elapsed >= DISPLAY_WAIT_SECS )); then
      echo "nova-ve-browser: Xvfb failed to start on $DISPLAY" >&2
      exit 1
    fi
    elapsed=$((elapsed + 1))
    sleep 1
  done

  /usr/local/bin/nova-ve-browser-session &
  pids+=("$!")
  x11vnc -display "$DISPLAY" -rfbport "$PORT" -forever -shared -nopw -quiet &
  pids+=("$!")
}

telnetd -p 23 -l /usr/local/bin/console-shell
if [[ "${NOVA_VE_BROWSER_VNC:-1}" != 0 ]]; then
  start_vnc
fi
/usr/local/bin/nova-ve-rdp-server &
pids+=("$!")
wait -n "${pids[@]}"
