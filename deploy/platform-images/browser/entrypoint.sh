#!/bin/sh
# Copyright (c) 2026 Fahad Yousuf <fahadysuf@gmail.com>
# SPDX-License-Identifier: Apache-2.0
#
# nova-ve-browser entrypoint (#288): Xvfb + openbox + Firefox on the virtual
# display, exposed via x11vnc on the configured port so the in-browser
# Guacamole VNC console presents the browser station. A busybox telnet
# daemon on 23 backs the template's telnet second console (debug shell into
# the station, EVE-style). Foreground x11vnc keeps docker stop semantics
# clean (mirrors the alpine-vnc demo image).
set -eu

GEOM="${NOVA_VE_VNC_GEOMETRY:-1280x800x24}"
PORT="${NOVA_VE_VNC_PORT:-5900}"
DISPLAY_NAME="${DISPLAY:-:0}"
DISPLAY_WAIT_SECS="${NOVA_VE_VNC_DISPLAY_WAIT_SECS:-10}"

cleanup() {
  if [ -n "${XVFB_PID:-}" ] && kill -0 "${XVFB_PID}" 2>/dev/null; then
    kill "${XVFB_PID}" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

Xvfb "${DISPLAY_NAME}" -screen 0 "${GEOM}" -nolisten tcp &
XVFB_PID=$!

elapsed=0
while ! xdpyinfo -display "${DISPLAY_NAME}" >/dev/null 2>&1; do
  if ! kill -0 "${XVFB_PID}" 2>/dev/null; then
    echo "nova-ve-browser: Xvfb exited before ${DISPLAY_NAME} became ready" >&2
    wait "${XVFB_PID}" || true
    exit 1
  fi
  if [ "${elapsed}" -ge "${DISPLAY_WAIT_SECS}" ]; then
    echo "nova-ve-browser: timed out waiting for ${DISPLAY_NAME}" >&2
    exit 1
  fi
  elapsed=$((elapsed + 1))
  sleep 1
done

# Telnet second console (daemonises; console-shell is a login shell).
telnetd -p 23 -l /usr/local/bin/console-shell || true

openbox >/dev/null 2>&1 &

# Firefox in-container flags: allow root (ephemeral lab container), relax
# sandboxes that assume user namespaces, and skip first-run friction via a
# pre-seeded profile.
PROFILE="/tmp/nova-ve-firefox-profile"
mkdir -p "${PROFILE}"
cat > "${PROFILE}/user.js" <<'EOF'
user_pref("browser.shell.checkDefaultBrowser", false);
user_pref("browser.startup.homepage_override.mstone", "ignore");
user_pref("browser.aboutwelcome.enabled", false);
user_pref("browser.sessionstore.resume_from_crash", false);
user_pref("datareporting.policy.dataSubmissionEnabled", false);
user_pref("toolkit.telemetry.enabled", false);
user_pref("browser.aboutConfig.showWarning", false);
EOF

MOZ_ALLOW_RUN_AS_ROOT=1 \
MOZ_DISABLE_CONTENT_SANDBOX=1 \
MOZ_DISABLE_RDD_SANDBOX=1 \
MOZ_DISABLE_GMP_SANDBOX=1 \
firefox --no-remote --profile "${PROFILE}" --width 1240 --height 740 >/dev/null 2>&1 &

# A fallback terminal so the station is never a dead desktop (mirrors the
# alpine-vnc demo image's xterm).
xterm -display "${DISPLAY_NAME}" -geometry 100x30 -fa Monospace -fs 11 -bg black -fg green -e /bin/bash -li &

trap - EXIT INT TERM

exec x11vnc \
  -display "${DISPLAY_NAME}" \
  -rfbport "${PORT}" \
  -forever \
  -shared \
  -nopw \
  -quiet
