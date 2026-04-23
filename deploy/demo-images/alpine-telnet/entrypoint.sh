#!/bin/sh
set -eu

mkdir -p /var/run

exec /usr/sbin/telnetd -F -p 23 -l /usr/local/bin/console-shell
