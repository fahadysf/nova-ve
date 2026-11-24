#!/usr/bin/env bash
# Copyright (c) 2026 Fahad Yousuf <fahadysuf@gmail.com>
# SPDX-License-Identifier: Apache-2.0
#
# Quality-control smoke check for the platform containers (#288).
#
# Usage:
#   deploy/scripts/verify-platform-images.sh mgmt-server <container-name>
#   deploy/scripts/verify-platform-images.sh browser <container-name>
#
# Runs against a STARTED container (docker exec) and asserts the version
# floors declared for each image plus the console services the templates
# rely on (telnetd on 23, VNC on 5900 / sshd on 22). Exits non-zero on the
# first failed assertion so build pipelines and operator checks stop loudly.

set -euo pipefail

kind="${1:-}"
container="${2:-}"

if [[ -z "$container" || ( "$kind" != "mgmt-server" && "$kind" != "browser" ) ]]; then
  echo "usage: $0 <mgmt-server|browser> <container-name>" >&2
  exit 2
fi

run() { docker exec "$container" /bin/sh -c "$1"; }

say() { echo "verify($kind/$container): $*"; }

fail() {
  echo "verify($kind/$container): FAIL — $*" >&2
  exit 1
}

require_floor() {
  # require_floor <label> <observed-major> <observed-minor> <floor-major> <floor-minor>
  if (( $2 * 100 + $3 < $4 * 100 + $5 )); then
    fail "$1 version $2.$3 below floor $4.$5"
  fi
  say "$1 version $2.$3 >= floor $4.$5"
}

listener_up() {
  # listener_up <port> — busybox netstat is present in every platform image;
  # pgrep -x is NOT reliable across busybox versions (comm matching differs).
  run "netstat -ltn 2>/dev/null | grep -q ':$1 '"
}

case "$kind" in
  mgmt-server)
    python_version="$(run 'python3 -c "import sys; print(f\"{sys.version_info.major} {sys.version_info.minor}\")"')" \
      || fail "python3 not runnable"
    # shellcheck disable=SC2046
    require_floor "python" $python_version 3 13

    ansible_version="$(run 'python3 -c "import ansible; print(ansible.__version__)"')" \
      || fail "ansible import failed"
    ansible_major="${ansible_version%%.*}"
    ansible_minor="$(echo "$ansible_version" | cut -d. -f2)"
    require_floor "ansible-core" "$ansible_major" "$ansible_minor" 2 18

    napalm_version="$(run 'python3 -c "import napalm; print(napalm.__version__)"')" \
      || fail "napalm import failed"
    napalm_major="${napalm_version%%.*}"
    napalm_minor="$(echo "$napalm_version" | cut -d. -f2)"
    require_floor "napalm" "$napalm_major" "$napalm_minor" 5 0

    netmiko_version="$(run 'python3 -c "import netmiko; print(netmiko.__version__)"')" \
      || fail "netmiko import failed"
    netmiko_major="${netmiko_version%%.*}"
    netmiko_minor="$(echo "$netmiko_version" | cut -d. -f2)"
    require_floor "netmiko" "$netmiko_major" "$netmiko_minor" 4 4

    listener_up 22 || fail "sshd is not listening on 22"
    say "sshd listening (port 22)"

    listener_up 23 || fail "telnet console is not listening on 23"
    say "telnet console listening (port 23)"

    for tool in ssh iperf3 tcpdump git curl; do
      run "command -v $tool >/dev/null" || fail "$tool missing"
    done
    say "tools present: ssh iperf3 tcpdump git curl"
    ;;
  browser)
    firefox_version="$(run 'firefox --version')" || fail "firefox not runnable"
    firefox_major="$(echo "$firefox_version" | awk '{print $NF}' | cut -d. -f1)"
    firefox_minor="$(echo "$firefox_version" | awk '{print $NF}' | cut -d. -f2)"
    require_floor "firefox" "$firefox_major" "$firefox_minor" 125 0

    listener_up 5900 || fail "VNC console is not listening on 5900"
    say "VNC console listening (port 5900)"

    listener_up 23 || fail "telnet second console is not listening on 23"
    say "telnet second console listening (port 23)"

    # A listening port is not a working console: the login program has to
    # hand out a shell. The quoted marker can only appear as command output,
    # never as the terminal's echo of the typed line.
    console_ok="$(docker exec -i "$container" /bin/bash -s <<'EOF' 2>/dev/null || echo no
exec 3<>/dev/tcp/127.0.0.1/23
printf 'echo nova-ve-console-"ok"\nexit\n' >&3
for _ in {1..30}; do
  IFS= read -r -t 1 line <&3 || continue
  [[ "${line}" == *nova-ve-console-ok* ]] && { echo yes; exit 0; }
done
echo no
EOF
    )"
    [[ "${console_ok}" == yes ]] || fail "telnet second console does not offer a shell"
    say "telnet second console offers a shell"

    run 'pgrep -x Xvfb >/dev/null' || fail "Xvfb is not running"
    say "Xvfb running"

    run 'pgrep -x openbox >/dev/null' || fail "openbox is not running"
    say "window manager running"

    # Taskbar + wallpaper: the window-switching surface (minimized windows
    # have no other way back on screen) and the branded root window.
    run 'pgrep -x tint2 >/dev/null' || fail "taskbar (tint2) is not running"
    say "taskbar (tint2) running"

    run 'test -f /usr/local/share/nova-ve/wallpaper.png' \
      || fail "wallpaper asset missing"
    say "wallpaper asset present"
    ;;
esac

say "all checks passed"
