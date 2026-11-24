# Migrating from EVE-NG

nova-ve includes an importer for EVE-NG, UNetLab, and PNETLab addon trees. It migrates supported image assets into the nova-ve image layout, generates templates for supported vendors, and records a manifest for audit and follow-up.

The default import mode is **move + symlink back**: each image file is renamed into the nova-ve image store and a symlink is left at its original EVE-NG path. This is **non-destructive to your data** — no file bytes are deleted, duplicated, or rewritten, the disk image exists exactly once, and EVE-NG keeps booting its nodes from the same paths. Use `--copy-only` if you want the source files left fully untouched instead.

## One-Shot Import Script

On a normal installed nova-ve host, use the one-shot wrapper installed with the git checkout. With the default paths, you only need two commands after the EVE-NG tree is available at `/opt/unetlab`:

```bash
cd "${NOVA_VE_REPO_DIR:-/var/lib/nova-ve/nova-ve-git}"
./deploy/scripts/import-eveng-templates.sh --dry-run
sudo ./deploy/scripts/import-eveng-templates.sh
```

The first command is a dry-run plan; the second performs the import. The wrapper reads the backend environment file, finds the backend virtual environment, and uses these defaults:

| Purpose | Default |
|---|---|
| Service checkout | `${NOVA_VE_REPO_DIR:-/var/lib/nova-ve/nova-ve-git}` |
| Source EVE-NG tree | `/opt/unetlab` |
| nova-ve image destination | `/var/lib/nova-ve/images` |
| Generated template destination | `/var/lib/nova-ve/templates` |
| Run manifest | `/var/lib/nova-ve/import-manifest.json` |
| Run log | `/var/log/eve-ng-importer.log` |

If the EVE-NG source is on another machine, copy it to the nova-ve host first:

```bash
sudo rsync -a --info=progress2 \
  root@old-eveng-host:/opt/unetlab/ \
  /opt/unetlab/
```

The importer migrates addon assets and templates, not saved EVE-NG lab topologies. After a successful run, imported templates appear in the nova-ve add-node flow alongside built-in templates.

`--dry-run` and `--help` may be run without sudo. The real import requires sudo because it writes nova-ve-owned image, template, and manifest files.

## Plan First

Run a dry run before the first real import:

```bash
cd "${NOVA_VE_REPO_DIR:-/var/lib/nova-ve/nova-ve-git}"
./deploy/scripts/import-eveng-templates.sh --dry-run
```

`--dry-run` walks the source tree and prints the planned summary without writing images, templates, or `import-manifest.json`.

## Import

When the plan looks correct, run the importer without `--dry-run`:

```bash
cd "${NOVA_VE_REPO_DIR:-/var/lib/nova-ve/nova-ve-git}"
sudo ./deploy/scripts/import-eveng-templates.sh
```

The run prints one human-readable step per file so you can see exactly what is happening, for example:

```
[1/12] qemu/vyos-1.4 — moving hda.qcow2 (2.1 GiB) ... done (0.3s, symlinked back to /opt/unetlab/addons/qemu/vyos-1.4/hda.qcow2)
[2/12] qemu/vyos-1.4 — generating template YAML ... done (adapter=vyos)
[3/12] qemu/csr1000v — hda.qcow2 already in place (sha256 match), skipped
```

Every run also appends its steps and the final summary to `/var/log/eve-ng-importer.log` (override with `--log-file`; rotation is size-capped). Scripts that want the old machine-readable event stream can pass `--json`.

By default, image files are moved and symlinked back as described above. If the source and destination are on different filesystems, the importer copies the file, verifies its SHA-256, and creates the symlink. On reruns, the importer skips source paths already linked to their destinations. For other existing destinations, equal file sizes and matching first and last 64 KiB skip a full SHA-256 comparison. Changes confined to the middle of a large file can pass this check.

## Conservative Opt-Out

Use `--copy-only` for the pre-#271 behaviour — copy + sha256 verify with the source files fully preserved:

```bash
cd "${NOVA_VE_REPO_DIR:-/var/lib/nova-ve/nova-ve-git}"
sudo ./deploy/scripts/import-eveng-templates.sh --copy-only
```

## Destructive Opt-In

Use `--delete-source` only when you intentionally want source files removed after a successful verified copy:

```bash
cd "${NOVA_VE_REPO_DIR:-/var/lib/nova-ve/nova-ve-git}"
sudo ./deploy/scripts/import-eveng-templates.sh --delete-source
```

`--delete-source` is an explicit opt-in. Do not use it until you have a backup or have verified that the imported destination is the authoritative copy.

## Manifest Review

Every non-dry-run import writes `import-manifest.json` to the path passed with `--manifest`. Review it after each run:

```bash
jq '.summary, .imported, .skipped, .errors, .templates' /var/lib/nova-ve/import-manifest.json
```

Entries marked `needs-manual-review` mean the importer preserved the image assets but could not produce a complete, ready-to-use template. For those entries, inspect the manifest details and hand-author or adjust the template under `/var/lib/nova-ve/templates/` before using it in production labs.

## Next Steps

See the importing guide for details on supported image kinds, manifest fields, and vendor template coverage:

- [Image import details](importing-eveng/images.md)
- [Manifest reference](importing-eveng/manifest.md)
- [Vendor coverage](importing-eveng/vendor-coverage.md)
