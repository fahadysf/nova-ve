# Reading the manifest

Every non-`--dry-run` invocation writes a structured JSON manifest to the path passed via `--manifest` (default `/var/lib/nova-ve/import-manifest.json`). The manifest has four top-level arrays:

| Field | Contents |
|---|---|
| `imported` | one entry per file landed at the destination, with `src` / `dst` / `sha256` / `bytes` / `mode` |
| `templates` | one entry per emitted template, with `name` / `status` (one of `generated`, `converted`, `skipped`, `needs-manual-review`) and on `needs-manual-review` a `reason` |
| `skipped` | one entry per destination skipped because it was already migrated (idempotency) |
| `errors` | one entry per failure that did not abort the run; each has `path` and `error` |

On `imported` entries, `mode` records the copy strategy used for that file (`move-symlink`, `default`, `delete-source`, `move`). Since [#271](https://github.com/fahadysf/nova-ve/issues/271), two optional fields appear on move+symlink outcomes: `symlinked: true` (the original `/opt/unetlab` path is now a symlink pointing at `dst`) and `note` (a human-readable detail, e.g. `symlinked back to /opt/unetlab/addons/qemu/vyos-1.4/hda.qcow2` or `cross-filesystem: copied + sha256 verified`). Older manifests without these fields still parse identically.

The shared IOL `iourc` license is always copy-based even in the default mode, so its entries read `mode: "default"` without `symlinked`.

## Handling `needs-manual-review` templates

When a vendor adapter cannot fully convert an EVE-NG template (e.g. the source config references an unrecognised hardware shape, or an IOL template lacks its `iourc` license file), the importer flags the template with `status: "needs-manual-review"` and a human-readable `reason` on the manifest entry.

Workflow:

1. List every flagged template:
   ```bash
   jq '.templates[] | select(.status == "needs-manual-review")' \
     /var/lib/nova-ve/import-manifest.json
   ```
2. For each: read the `reason` field, then either:
    - hand-author a nova-ve template JSON under `/var/lib/nova-ve/templates/<vendor>/<key>.json`, or
    - open a follow-up issue if the importer should support that vendor pattern natively.

Note: re-running the importer **regenerates adapter-emitted templates** from the source tree — hand-edits made directly to a generated template file are overwritten on the next run. Keep customisations in differently-named template files.

## Rollback

The default move+symlink mode is non-destructive to your data — the bytes were moved, not deleted — but a rollback must also undo the back-symlinks so the EVE-NG tree points at real files again. Two options:

**Option A — move everything back into the EVE-NG tree** (full revert):

```bash
# For every symlink under /opt/unetlab/addons, move the target file back:
sudo find /opt/unetlab/addons -type l -exec sh -c \
  'mv "$(readlink -f "$1")" "$1"' _ {} \;
```

**Option B — discard the nova-ve copy and re-import with `--copy-only`** (leaves the EVE-NG tree untouched, bytes still at `/opt/unetlab`):

```bash
# Reset the nova-ve image + template tree:
sudo rm -rf /var/lib/nova-ve/images /var/lib/nova-ve/templates
sudo install -d -o nova-ve -g nova-ve /var/lib/nova-ve/images /var/lib/nova-ve/templates

# Re-run the provisioner (idempotent) to re-create directory perms:
sudo bash deploy/scripts/provision-ubuntu-2604.sh
```

Option B only makes sense after Option A has restored the source files (otherwise the source paths are symlinks into the tree you just deleted — check with `find /opt/unetlab/addons -type l` first).

If you ran with `--delete-source` and want to restore from your EVE-NG host, re-`rsync` the source tree.

## Tracking issues

- [#182](https://github.com/fahadysf/nova-ve/issues/182) — umbrella tracker for the importer epic.
- [#184](https://github.com/fahadysf/nova-ve/issues/184) — non-destructive copy contract.
- [#188](https://github.com/fahadysf/nova-ve/issues/188) — paired-VM Juniper templates (vMX / vQFX).
