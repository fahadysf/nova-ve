#!/bin/sh
# Copyright (c) 2026 Fahad Yousuf <fahadysuf@gmail.com>
# SPDX-License-Identifier: Apache-2.0
#
# nova-ve-mgmt-server entrypoint (#288): start sshd for out-of-band
# management (reachable over the lab mgmt0 attachment when the node opts
# in), then run the busybox telnet console in the foreground so docker stop
# semantics work.
#
# Console ports on lab containers are deliberately unauthenticated lab
# services; the SSH root password below is a lab convenience default (the
# EVE eve-gui-server image ships root/eve), NOT a security boundary.
# Override it per node via the extras "environment" list:
#   NOVA_VE_MGMT_PASSWORD=<value>
set -eu

# Fresh SSH host keys per container start.
ssh-keygen -A >/dev/null 2>&1 || true

# Lab-default credentials: root / nova (overridable via environment).
printf 'root:%s' "${NOVA_VE_MGMT_PASSWORD:-nova}" | chpasswd

# Minimal sshd config — alpine's default file is replaced wholesale so the
# effective policy is exactly what this image declares.
cat > /etc/ssh/sshd_config <<'EOF'
Port 22
HostKey /etc/ssh/ssh_host_ed25519_key
HostKey /etc/ssh/ssh_host_rsa_key
PermitRootLogin yes
PasswordAuthentication yes
PermitEmptyPasswords no
X11Forwarding no
PrintMotd no
EOF

/usr/sbin/sshd

# Foreground telnet console (port 23) — PID 1 keeps container stop clean.
exec /usr/sbin/telnetd -F -p 23 -l /usr/local/bin/console-shell
