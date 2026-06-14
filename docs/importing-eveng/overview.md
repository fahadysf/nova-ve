# Overview & run order

## TL;DR — non-destructive default

!!! warning "Sources are preserved by default"
    Running `import-eveng-templates.sh` with no flags is non-destructive. Sources under `/opt/unetlab/addons/...` are copied + sha256-verified at the destination; the source files are **left untouched** unless you pass an explicit opt-in flag.

| Flag | Behaviour |
|---|---|
| *(default — no flags)* | copy + sha256 verify; sources preserved |
| `--copy-only` | alias for the default (accepted for symmetry with `--move`) |
| `--delete-source` | copy + sha256 verify + delete source after match (destructive opt-in) |
| `--move` | copy + delete source; **skips sha256 verify**; unsafe — only use when the source tree is already verified elsewhere |
| `--force` | overwrite an existing destination on sha256 mismatch (combinable) |
| `--dry-run` | plan the run without touching the destination filesystem |
| `--verbose` | DEBUG-level structured logging on stderr |

This non-destructive default is the inversion of the original spec, made during the safety review for [#184](https://github.com/fahadysf/nova-ve/issues/184). It optimises for operator-recoverability over fast-by-default.

## Prerequisites

1. **A reachable nova-ve host.** Use the [quick-start one-liner](../getting-started/quick-start.md) to provision a fresh Ubuntu 26.04 host from scratch. The provisioner creates `/var/lib/nova-ve/{images,templates,labs}` with the right ownership.
2. **Read access to the EVE-NG source tree.** Default source path is `/opt/unetlab`. If your EVE-NG host is on a different machine, `rsync` the addon directory to the nova-ve host first:
   ```bash
   sudo rsync -a --info=progress2 \
     root@old-eveng-host:/opt/unetlab/ \
     /opt/unetlab/
   ```
3. **Root on the nova-ve host.** The importer needs to chown destination files to the canonical service user (resolved from `NOVA_VE_SERVICE_USER`, falling back to the compatibility `NOVA_VE_OWNER`, then `nova-ve`).

## Run order

### One-shot shell wrapper

On a host installed by `install.sh`, use the wrapper from the service checkout:

```bash
sudo /var/lib/nova-ve/nova-ve-git/deploy/scripts/import-eveng-templates.sh --dry-run
sudo /var/lib/nova-ve/nova-ve-git/deploy/scripts/import-eveng-templates.sh
```

The first command is a non-mutating plan. The second command performs the import with the same defaults. Those commands use the defaults most operators want:

| Purpose | Default |
|---|---|
| Source tree | `/opt/unetlab` |
| Image destination | `/var/lib/nova-ve/images` |
| Generated templates | `/var/lib/nova-ve/templates` |
| Manifest | `/var/lib/nova-ve/import-manifest.json` |

The wrapper sources `/etc/nova-ve/backend.env`, changes into the installed backend directory, and runs the importer with the backend virtual environment. You only need the longer `--source`, `--dest`, `--templates-dir`, or `--manifest` flags when you intentionally use non-standard paths.

`--dry-run` and `--help` may be run without sudo. Real imports still require sudo because the script writes `/var/lib/nova-ve/images`, `/var/lib/nova-ve/templates`, and the manifest with service-account ownership.

### Step 1 — `--dry-run` first

Always plan the run before executing it.

```bash
sudo /var/lib/nova-ve/nova-ve-git/deploy/scripts/import-eveng-templates.sh --dry-run
```

The dry run walks the source tree, evaluates each file's idempotency check (sha256 vs the existing destination), and emits a planned manifest on stdout. No filesystem mutation, no manifest written. If `/opt/unetlab` is missing or empty, the planned manifest is empty (`imported: []`, `skipped: []`, `templates: []`, `errors: []`).

`--dry-run` does **not** require root.

### Step 2 — real run (non-destructive)

Once the dry-run looks correct, drop the `--dry-run` flag:

```bash
sudo /var/lib/nova-ve/nova-ve-git/deploy/scripts/import-eveng-templates.sh
```

This copies every file, verifies sha256 at the destination, and writes the manifest to `--manifest` (default `/var/lib/nova-ve/import-manifest.json`). **Sources are preserved.** Re-running the same command is idempotent: anything whose destination already matches the source sha256 lands in `manifest.skipped[]` with `reason: "exists, sha256 match"`.

### Step 3 — opt-in to delete sources (optional)

If you have verified the migration and want to reclaim the source disk space:

```bash
sudo /var/lib/nova-ve/nova-ve-git/deploy/scripts/import-eveng-templates.sh --delete-source
```

`--delete-source` copies + sha256-verifies + deletes the source file *only after* the verify succeeds. If verification fails for any file, the source for that file is **not** deleted; the failure lands in `manifest.errors[]` and the run continues.

### Step 4 — instantiate a node from a migrated template

After the importer finishes, every priority-vendor template that produced a clean conversion lands in `/var/lib/nova-ve/templates/`. Operators see them in the existing `GET /api/list/templates/{template_type}` listing. The picker UI in nova-ve's "Add node" modal iterates types and lists the templates; click "From template" → pick → confirm.

The importer does not migrate saved EVE-NG lab topology files. It imports reusable images and templates; build nova-ve labs from those templates after the import.

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
