#!/usr/bin/env bash
# Copyright (c) 2026 Fahad Yousuf <fahadysuf@gmail.com>
# SPDX-License-Identifier: Apache-2.0
#
# Quality-control smoke check for the platform containers (#288).
#
# Usage:
#   deploy/scripts/verify-platform-images.sh mgmt-server <container-name>
#   deploy/scripts/verify-platform-images.sh browser <container-name>
#   deploy/scripts/verify-platform-images.sh desktop <container-name>
#
# Runs against a STARTED container (docker exec) and asserts the version
# floors declared for each image plus the console services the templates
# rely on (telnetd on 23, VNC on 5900, RDP on 3389, sshd on 22). Exits non-zero on the
# first failed assertion so build pipelines and operator checks stop loudly.

set -euo pipefail

kind="${1:-}"
container="${2:-}"

if [[ -z "$container" || ( "$kind" != "mgmt-server" && "$kind" != "browser" && "$kind" != "desktop" ) ]]; then
  echo "usage: $0 <mgmt-server|browser|desktop> <container-name>" >&2
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

verify_rdp() {
  listener_up 3389 || fail "RDP console is not listening on 3389"
  run 'test -s /run/nova-ve/rdp-password && [ "$(stat -c "%u:%a" /run/nova-ve/rdp-password)" = "0:600" ]' \
    || fail "RDP credential is missing or has incorrect permissions"
  run 'test -s /etc/xrdp/cert.pem && test -x /usr/libexec/Xorg && test -x /etc/xrdp/startwm.sh' \
    || fail "xRDP TLS or Xorg startup files are missing"
  say "RDP listener, TLS, credentials and Xorg startup files ready"
}

verify_wallpaper() {
  run 'test -s /usr/local/share/nova-ve/wallpaper.png' || fail "wallpaper asset missing"
  say "wallpaper asset present"
}

verify_telnet_shell() {
  listener_up 23 || fail "telnet second console is not listening on 23"

  # A quote inside the marker prevents the input echo from matching.
  local console_ok
  console_ok="$(docker exec -i "$container" /bin/bash -s <<'EOF' 2>/dev/null || echo no
exec 3<>/dev/tcp/127.0.0.1/23
printf 'echo nova-ve-console-"ok"\nexit\n' >&3
for _ in {1..10}; do
  IFS= read -r -t 1 line <&3 || continue
  [[ "${line}" == *nova-ve-console-ok* ]] && { echo yes; exit 0; }
done
echo no
EOF
  )"
  [[ "${console_ok}" == yes ]] || fail "telnet second console does not offer a shell"
  say "telnet second console offers a shell"
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

    verify_telnet_shell
    verify_rdp

    if run '[ "${NOVA_VE_BROWSER_VNC:-1}" = 0 ]'; then
      if listener_up 5900 || run 'pgrep -x Xvfb >/dev/null'; then
        fail "RDP-only browser started VNC or Xvfb"
      fi
      say "RDP-only browser has no VNC display"
    else
      listener_up 5900 || fail "VNC console is not listening on 5900"
      docker exec "$container" /bin/bash -c 'exec 3<>/dev/tcp/127.0.0.1/5900; read -t 5 -r banner <&3 && [[ "$banner" == "RFB 003."* ]]' \
        || fail "VNC console did not send an RFB greeting"
      say "VNC console sent an RFB greeting (port 5900)"

      run 'pgrep -x Xvfb >/dev/null' || fail "Xvfb is not running"
      say "Xvfb running"

      run 'pgrep -x openbox >/dev/null' || fail "window manager is not running"
      say "window manager running"

      run 'pgrep -x tint2 >/dev/null' || fail "taskbar (tint2) is not running"
      say "taskbar (tint2) running"
    fi

    verify_wallpaper
    ;;
  desktop)
    verify_rdp
    verify_telnet_shell
    verify_wallpaper

    for tool in mate-session marco caja mate-terminal engrampa eom pluma zathura firefox chromium \
                wireshark tshark dumpcap filezilla xfreerdp3 remmina vlc java dig snmpget iperf iperf3 mtr \
                traceroute tcpdump git ssh sshd lftp tftp in.tftpd dhcpcd dnsmasq \
                vsftpd httpd php85 vim asciidoctor dconf-editor xmllint xsltproc; do
      run "command -v $tool >/dev/null" || fail "$tool missing"
    done
    run '/opt/nova-ve/venv/bin/python -c "import ansible, napalm, netmiko"' \
      || fail "Python automation imports failed"
    say "desktop, network and automation tools present"
    ;;
esac

say "all checks passed"
