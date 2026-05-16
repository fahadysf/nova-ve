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

# Xvfb owns display :0; trailing & lets us layer the wm/term on top.
Xvfb :0 -screen 0 "${GEOM}" -nolisten tcp &
XVFB_PID=$!

# Give Xvfb a beat to bind before clients connect; a fixed sleep is fine
# because the alternative (xdpyinfo polling) needs another package.
sleep 1

fluxbox >/dev/null 2>&1 &
xterm -geometry 100x30 -fa Monospace -fs 11 -bg black -fg green -e /bin/bash -li &

# Foreground x11vnc so it's PID-meaningful to docker stop.
exec x11vnc \
  -display :0 \
  -rfbport "${PORT}" \
  -forever \
  -shared \
  -nopw \
  -quiet
