#!/bin/sh
# Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
# SPDX-License-Identifier: Apache-2.0
#
# Bring up a minimal VNC desktop suitable for a demo lab node. Xvfb provides
# the in-memory display, fluxbox renders a tiny window manager, an xterm is
# spawned so the operator lands on a shell, and x11vnc exposes display :0 on
# the configured port.
set -eu

GEOM="${NOVA_VE_VNC_GEOMETRY:-1024x768x24}"
PORT="${NOVA_VE_VNC_PORT:-5900}"
DISPLAY_NAME="${DISPLAY:-:0}"
DISPLAY_WAIT_SECS="${NOVA_VE_VNC_DISPLAY_WAIT_SECS:-10}"

cleanup() {
  if [ -n "${XVFB_PID:-}" ] && kill -0 "${XVFB_PID}" 2>/dev/null; then
    kill "${XVFB_PID}" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

# Xvfb owns the display; trailing & lets us layer the wm/term on top.
Xvfb "${DISPLAY_NAME}" -screen 0 "${GEOM}" -nolisten tcp &
XVFB_PID=$!

elapsed=0
while ! xdpyinfo -display "${DISPLAY_NAME}" >/dev/null 2>&1; do
  if ! kill -0 "${XVFB_PID}" 2>/dev/null; then
    echo "alpine-vnc: Xvfb exited before ${DISPLAY_NAME} became ready" >&2
    wait "${XVFB_PID}" || true
    exit 1
  fi
  if [ "${elapsed}" -ge "${DISPLAY_WAIT_SECS}" ]; then
    echo "alpine-vnc: timed out waiting for ${DISPLAY_NAME}" >&2
    exit 1
  fi
  elapsed=$((elapsed + 1))
  sleep 1
done

fluxbox >/dev/null 2>&1 &
xterm -display "${DISPLAY_NAME}" -geometry 100x30 -fa Monospace -fs 11 -bg black -fg green -e /bin/bash -li &

trap - EXIT INT TERM

# Foreground x11vnc so it's PID-meaningful to docker stop.
exec x11vnc \
  -display "${DISPLAY_NAME}" \
  -rfbport "${PORT}" \
  -forever \
  -shared \
  -nopw \
  -quiet
