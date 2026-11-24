#!/bin/sh
set -eu

telnetd -p 23 -l /usr/local/bin/console-shell
exec /usr/local/bin/nova-ve-rdp-server
