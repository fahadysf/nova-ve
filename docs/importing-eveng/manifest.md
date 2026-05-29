# Reading the manifest

Every non-`--dry-run` invocation writes a structured JSON manifest to the path passed via `--manifest` (default `/var/lib/nova-ve/import-manifest.json`). The manifest has four top-level arrays:

| Field | Contents |
|---|---|
| `imported` | one entry per file copied to the destination, with `src` / `dst` / `sha256` / `bytes` / `mode` |
| `templates` | one entry per emitted template, with `name` / `status` (one of `ok`, `needs-manual-review`) and on `needs-manual-review` a `reason` |
| `skipped` | one entry per destination skipped because the bytes already matched (idempotency) |
| `errors` | one entry per failure that did not abort the run; each has `path` and `error` |

## Handling `needs-manual-review` templates

When a vendor adapter cannot fully convert an EVE-NG template (e.g. the PHP file's `qemu_options` references an unrecognised `-device` tree, or an IOL template lacks its `iourc` license file), the importer flags the template with `status: "needs-manual-review"` and stores the parsed-but-unconverted EVE-NG fields under `_eveng_raw` for human inspection.

Workflow:

1. List every flagged template:
   ```bash
   jq '.templates[] | select(.status == "needs-manual-review")' \
     /var/lib/nova-ve/import-manifest.json
   ```
2. For each: read the `reason` field, inspect `_eveng_raw`, and either:
    - hand-author a nova-ve template JSON under `/var/lib/nova-ve/templates/<vendor>/<key>.json`, or
    - open a follow-up issue if the importer should support that vendor pattern natively.
3. Re-run the importer. The previously-flagged template stays flagged — the importer never overwrites a template you have customised; the check is content-aware sha256, not a marker comment.

## Rollback

Because the default mode is non-destructive, rollback is straightforward:

```bash
# Reset the nova-ve image + template tree:
sudo rm -rf /var/lib/nova-ve/images /var/lib/nova-ve/templates
sudo install -d -o nova-ve -g nova-ve /var/lib/nova-ve/images /var/lib/nova-ve/templates

# Re-run the provisioner (idempotent) to re-create directory perms:
sudo bash deploy/scripts/provision-ubuntu-2604.sh
```

If you ran with `--delete-source` and want to restore from your EVE-NG host, re-`rsync` the source tree.

## Tracking issues

- [#182](https://github.com/fahadysf/nova-ve/issues/182) — umbrella tracker for the importer epic.
- [#184](https://github.com/fahadysf/nova-ve/issues/184) — non-destructive copy contract.
- [#188](https://github.com/fahadysf/nova-ve/issues/188) — paired-VM Juniper templates (vMX / vQFX).
