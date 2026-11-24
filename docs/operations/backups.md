# Backups

A working nova-ve install carries state in three places. A complete backup captures all three.

## What to back up

| Source | What's in it | Frequency |
|---|---|---|
| `/var/lib/nova-ve/labs/` | Every lab JSON + per-node runtime state directories | Per change (continuous) or daily |
| `/var/lib/nova-ve/templates/` | Vendor-converted templates from the EVE-NG importer + any hand-authored templates | After every importer run; otherwise rarely changes |
| `/var/lib/nova-ve/images/` | Vendor boot disks (qcow2 / .image / .bin / Docker markers) | Once per major import; large but mostly static |
| Postgres database `novadb` | User accounts, session state, audit log, app metadata | Daily |
| Backend configuration | Environment settings and deployment credentials | After installation and after configuration or credential changes |

## The two backups you should automate

The commands below use `rsync`, which is not part of the provisioner's package list — `apt-get install rsync` if it is missing on your host.

**A — Application state (small, fast, frequent):**

```bash
# Lab JSON + templates (skipping large image blobs):
sudo rsync -a --delete \
  /var/lib/nova-ve/labs/ \
  /var/lib/nova-ve/templates/ \
  /etc/nova-ve/ \
  /backup/dest/nova-ve-state/
```

**B — Postgres dump:**

```bash
sudo -u postgres pg_dump -Fc novadb > /backup/dest/novadb-$(date +%F).dump
```

## Vendor images (slow, large, infrequent)

`/var/lib/nova-ve/images/` is reproducible from the source EVE-NG / PNETLab tree by re-running `deploy/scripts/import-eveng-templates.sh` from the nova-ve checkout. The default checkout is `/var/lib/nova-ve/nova-ve-git`, unless `NOVA_VE_REPO_DIR` was set during install. If you have the source tree archived, you do not strictly need to back the images directory up. One caveat: the importer's default mode **moves** source files into the images tree and symlinks the original `/opt/unetlab` paths back at them, so an already-migrated source tree holds symlinks, not real files. If you archive the source tree as-is, re-importing from it will mostly skip (each symlink source is recognised as already migrated); archive a real copy (`rsync -aL` dereferences symlinks) or keep the images backup instead. If you don't back up either:

```bash
sudo rsync -a --delete \
  /var/lib/nova-ve/images/ \
  /backup/dest/nova-ve-images/
```

## Restore

On a freshly-provisioned target host:

1. Run `install.sh` to lay down the runtime, services, and a clean `/var/lib/nova-ve/` tree.
2. `sudo rsync` your state backup back onto `/var/lib/nova-ve/labs/`, `/var/lib/nova-ve/templates/`, and `/etc/nova-ve/`.
3. Re-import the Postgres dump:
   ```bash
   sudo -u postgres pg_restore --clean --if-exists -d novadb /backup/source/novadb-YYYY-MM-DD.dump
   ```
4. Restart the backend so it loads the restored configuration and lab JSON files:
   ```bash
   sudo systemctl restart nova-ve-backend
   ```
5. Re-import images if not part of the backup set:
   ```bash
   cd "${NOVA_VE_REPO_DIR:-/var/lib/nova-ve/nova-ve-git}"
   sudo ./deploy/scripts/import-eveng-templates.sh
   ```

## What is *not* in any backup

- Running node state. nova-ve does not snapshot live VM RAM. After restore, every node is back to "stopped"; the next start re-builds runtime state from the lab JSON.
- Host network configuration (apart from what `install.sh` re-creates).
- TLS certificates for Caddy (if you set up custom cert provisioning outside the bundled flow).
