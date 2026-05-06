# Migrating from EVE-NG / UNetLab / PNETLab to nova-ve

This guide walks an operator through migrating an existing EVE-NG (or UNetLab / PNETLab) host's lab images and templates to a fresh nova-ve install. The end state: every supported priority-vendor template under `/opt/unetlab/...` is converted to a nova-ve node template under `/var/lib/nova-ve/templates/`, every image lands at `/var/lib/nova-ve/images/<kind>/<key>/`, and the operator can instantiate nodes from each migrated template through the nova-ve UI.

## TL;DR — non-destructive default

> **Running `nova-ve-import-eveng` (or `import-eveng-templates.sh`) with no flags is non-destructive.**
> Sources under `/opt/unetlab/addons/...` are copied + sha256-verified at the destination; the source files are **left untouched** unless you pass an explicit opt-in flag.

| Flag | Behaviour |
|---|---|
| *(default — no flags)* | copy + sha256 verify; sources preserved |
| `--delete-source` | copy + sha256 verify + delete source after match (destructive opt-in) |
| `--move` | copy + delete source; **skips sha256 verify**; documented as unsafe; never advertised as the recommended flag |
| `--force` | overwrite an existing destination on sha256 mismatch (combinable) |
| `--dry-run` | plan the run without touching the destination filesystem |

This non-destructive default is the inversion of the original spec, made during the safety review for [#184](https://github.com/fahadysf/nova-ve/issues/184) (R-OOB-1 in the consensus plan). It optimises for operator-recoverability over fast-by-default.

## Prerequisites

1. **A reachable nova-ve host.** Use the [curl|sudo bash one-liner](#install-nova-ve) to provision a fresh Ubuntu 26.04 host from scratch. The provisioner creates `/var/lib/nova-ve/{images,templates,labs}` with the right ownership.
2. **Read access to the EVE-NG source tree.** Default source path is `/opt/unetlab`. If your EVE-NG host is on a different machine, `rsync` the addon directory to the nova-ve host first:
   ```bash
   rsync -a -e ssh root@old-eveng-host:/opt/unetlab/ /opt/unetlab/
   ```
3. **Root on the nova-ve host.** The importer needs to chown destination files to the canonical app owner (resolved per `install.sh:76`: `${NOVA_VE_OWNER:-${SUDO_USER:-ubuntu}}`).

## Run order

### Step 1 — `--dry-run` first

Always plan the run before executing it.

```bash
sudo deploy/scripts/import-eveng-templates.sh \
  --source /opt/unetlab \
  --dest /var/lib/nova-ve/images \
  --manifest /var/lib/nova-ve/import-manifest.json \
  --dry-run
```

The dry run walks the source tree, evaluates each file's idempotency check (sha256 vs the existing destination), and emits a manifest describing what *would* happen. No filesystem mutation. The manifest path is logged at the end of the run; on a freshly-provisioned host it is empty (`imported: []`, `skipped: []`, `templates: []`, `errors: []`).

### Step 2 — real run (non-destructive)

Once the dry-run looks correct, drop the `--dry-run` flag:

```bash
sudo deploy/scripts/import-eveng-templates.sh
```

This copies every file, verifies sha256 at the destination, and emits the manifest. **Sources are preserved.** Re-running the same command is idempotent: anything whose destination already matches the source sha256 lands in `manifest.skipped[]` with `reason: "exists, sha256 match"`.

### Step 3 — opt-in to delete sources (optional)

If you have verified the migration and want to reclaim the source disk space:

```bash
sudo deploy/scripts/import-eveng-templates.sh --delete-source
```

`--delete-source` copies + sha256-verifies + deletes the source file *only after* the verify succeeds. If verification fails for any file, the source for that file is **not** deleted; the failure lands in `manifest.errors[]` and the run continues.

### Step 4 — instantiate a node from a migrated template

After the importer finishes, every priority-vendor template that produced a clean conversion lands in `/var/lib/nova-ve/templates/`. Operators see them in the existing `GET /api/list/templates/{template_type}` listing. The picker UI in nova-ve's "Add node" modal iterates types and lists the templates; click "From template" → pick → confirm.

API equivalent:

```bash
# List templates of a given type:
curl -b cookie-jar 'http://localhost/api/list/templates/qemu'

# Instantiate a node from a template:
curl -b cookie-jar -X POST 'http://localhost/api/labs/my-lab.json/nodes/from-template' \
  -H 'Content-Type: application/json' \
  -d '{
    "template_type": "qemu",
    "template_key": "vyos-1.4",
    "name": "vyos-1",
    "image": "vyos-1.4"
  }'
```

## Reading the manifest

The manifest is JSON with four top-level arrays:

| Field | Contents |
|---|---|
| `imported` | one entry per file copied to the destination, with `src` / `dst` / `sha256` / `bytes` / `mode` |
| `templates` | one entry per emitted template, with `name` / `status` (one of `ok`, `needs-manual-review`) and on `needs-manual-review` a `reason` |
| `skipped` | one entry per destination skipped because the bytes already matched (idempotency) |
| `errors` | one entry per failure that did not abort the run; each has `path` and `error` |

### Handling `needs-manual-review` templates

When a vendor adapter cannot fully convert an EVE-NG template (e.g. the PHP file's `qemu_options` references an unrecognised `-device` tree, or an IOL template lacks its `iourc` license file), the importer flags the template with `status: "needs-manual-review"` and stores the parsed-but-unconverted EVE-NG fields under `_eveng_raw` for human inspection.

Workflow:

1. `jq '.templates[] | select(.status == "needs-manual-review")' /var/lib/nova-ve/import-manifest.json` lists every flagged template.
2. For each: read the `reason` field, inspect `_eveng_raw`, and either hand-author a nova-ve template JSON under `/var/lib/nova-ve/templates/<vendor>/<key>.json` or open a follow-up issue if the importer should support that vendor pattern natively.
3. Re-run the importer; the previously-flagged template stays flagged (the importer never overwrites a template you have customised; the check is content-aware sha256, not a marker comment).

## Rollback

Because the default mode is non-destructive, rollback is straightforward:

```bash
# Reset the nova-ve image + template tree:
sudo rm -rf /var/lib/nova-ve/images /var/lib/nova-ve/templates
sudo install -d -o ubuntu -g ubuntu /var/lib/nova-ve/images /var/lib/nova-ve/templates

# Re-run the provisioner (idempotent) to re-create directory perms:
sudo bash deploy/scripts/provision-ubuntu-2604.sh
```

If you ran with `--delete-source` and want to restore from your EVE-NG host, re-`rsync` the source tree.

## Install nova-ve

If you do not yet have a nova-ve host:

```bash
curl -fsSL https://raw.githubusercontent.com/fahadysf/nova-ve/main/install.sh | sudo bash
```

This provisions a fresh Ubuntu 26.04 host, builds the bundled `nova-ve-alpine-telnet:latest` demo image (per [#191](https://github.com/fahadysf/nova-ve/issues/191); skip with `NOVA_VE_SKIP_DEMO_IMAGES=1`), and writes `nova-ve-install-summary.md` with the bootstrapped admin credentials. See the install summary's "Demo images (#191)" section for the alpine-telnet status.

## Tracking issues / follow-ups

- [#182](https://github.com/fahadysf/nova-ve/issues/182) — umbrella tracker for the importer epic.
- `paired-template-picker-fu` — multi-node template picker UI for vMX / vQFX paired-node templates from #188 (filed at #188 merge).
- `legacy-image-layout-removal-fu` — removal of the un-nested `IMAGES_DIR/<image>/` fallback after one release of the structured deprecation log signal (filed at #184 merge).
