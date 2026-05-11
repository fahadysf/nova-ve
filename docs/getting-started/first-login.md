# First login

When `install.sh` finishes it prints the bootstrapped admin credentials and the path to `nova-ve-install-summary.md`. The summary file is written into the working directory you launched the installer from, with permission mode `0600` (owner read/write only).

## Finding the admin password

```bash
# In the directory where you ran install.sh:
ls -la nova-ve-install-summary.md
sudo cat nova-ve-install-summary.md | grep -A 1 'Admin'
```

The file is owned by root with mode `0600`, so `sudo` is required to read it. The bootstrapped password sits in plain text inside — rotate it after first login if the host is shared.

## Logging in

1. Point your browser at `http://<host-ip>/` (or `https://<host-ip>/` if you have wired Caddy with a TLS cert).
2. Use the admin username + password from the install summary.
3. The session lands on the labs index. Click "New lab" to start, or import an existing lab JSON.

If the UI does not load, jump straight to [Troubleshooting](../operations/troubleshooting.md) — the most common cause is the backend not coming up cleanly.

## The install summary

`nova-ve-install-summary.md` is the single source of truth for everything `install.sh` set up on the host. Sections typically include:

- **Admin credentials** — bootstrapped username + password.
- **Critical config files** — `/etc/nova-ve/backend.env`, sudoers fragments, Caddy site config, systemd units.
- **Service map** — what systemd unit runs what, and how to restart each.
- **Demo images** — status of the bundled `nova-ve-alpine-telnet:latest` build.
- **Env var reference** — every variable in `/etc/nova-ve/backend.env` with its purpose.

Treat this file as a runbook. If you ever forget where something lives, the summary almost certainly tells you.

## Next steps

- [Create your first lab →](../user-guides/first-lab.md)
- [Migrating from EVE-NG → import images & templates](../importing-eveng/index.md)
