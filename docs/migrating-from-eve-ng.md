# Migrating from EVE-NG

nova-ve includes an importer for EVE-NG, UNetLab, and PNETLab addon trees. It copies supported image assets into the nova-ve image layout, generates templates for supported vendors, and records a manifest for audit and follow-up.

The default import mode is **non-destructive**. Source files are preserved unless you explicitly choose a destructive mode.

## Plan First

Run a dry run before the first real import:

```bash
sudo /var/lib/nova-ve/nova-ve-git/deploy/scripts/import-eveng-templates.sh \
  --source /opt/unetlab \
  --dest /var/lib/nova-ve/images \
  --manifest /var/lib/nova-ve/import-manifest.json \
  --dry-run
```

`--dry-run` walks the source tree and prints the planned summary without writing images, templates, or `import-manifest.json`.

## Import

When the plan looks correct, run the importer without `--dry-run`:

```bash
sudo /var/lib/nova-ve/nova-ve-git/deploy/scripts/import-eveng-templates.sh \
  --source /opt/unetlab \
  --dest /var/lib/nova-ve/images \
  --manifest /var/lib/nova-ve/import-manifest.json
```

By default, the importer copies files, verifies sha256 at the destination, and keeps the source tree intact. Re-running the same command is idempotent: files that already match are skipped and recorded in the manifest.

## Destructive Opt-In

Use `--delete-source` only when you intentionally want source files removed after a successful verified copy:

```bash
sudo /var/lib/nova-ve/nova-ve-git/deploy/scripts/import-eveng-templates.sh \
  --source /opt/unetlab \
  --dest /var/lib/nova-ve/images \
  --manifest /var/lib/nova-ve/import-manifest.json \
  --delete-source
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
