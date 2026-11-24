# Overview & run order

## TL;DR — move + symlink back by default

!!! note "Default mode since #271"
    Running `import-eveng-templates.sh` with no flags moves each image file under `/opt/unetlab/addons/...` into the nova-ve image store and leaves a **symlink at the original path** pointing at the moved file. No bytes are deleted, duplicated, or rewritten — the disk image exists exactly once, EVE-NG keeps resolving its original paths, and the import is near-instant even for multi-GiB qcow2 files.

| Flag | Behaviour |
|---|---|
| *(default — no flags)* | move + symlink back (same as `--move-symlink`); cross-filesystem sources fall back to copy + sha256 verify + symlink |
| `--move-symlink` | explicit spelling of the default |
| `--copy-only` | conservative opt-out: copy + sha256 verify, sources preserved (the pre-#271 default) |
| `--delete-source` | copy + sha256 verify + delete source after match (destructive opt-in) |
| `--move` | copy + delete source; **skips sha256 verify**; unsafe — only use when the source tree is already verified elsewhere |
| `--force` | overwrite an existing destination on sha256 mismatch (combinable) |
| `--dry-run` | plan the run without touching the destination filesystem |
| `--json` | one JSON log event per line on stderr instead of the human step-by-step stdout |
| `--log-file` | appending run log (default `/var/log/eve-ng-importer.log`, size-capped rotation) |
| `--verbose` | DEBUG-level logging in the log file (and stderr under `--json`) |

The four copy-strategy flags (`--move-symlink`, `--copy-only`, `--delete-source`, `--move`) are mutually exclusive — they are competing answers to "what happens to the source file".

Every run prints numbered, human-readable step lines (`[3/12] qemu/vyos-1.4 — moving hda.qcow2 (2.1 GiB) ... done (0.3s, symlinked back to /opt/unetlab/addons/qemu/vyos-1.4/hda.qcow2)`) on stdout and appends the same narration plus the final summary to `/var/log/eve-ng-importer.log`.

## Prerequisites

1. **A reachable nova-ve host.** Use the [quick-start one-liner](../getting-started/quick-start.md) to provision a fresh Ubuntu 26.04 host from scratch. The provisioner creates `/var/lib/nova-ve/{images,templates,labs}` with the right ownership.
2. **Write access to the EVE-NG source tree.** Default source path is `/opt/unetlab`. The default move+symlink mode replaces each image file with a symlink at its original path, so the importer needs write permission there (`--copy-only` only needs read). If your EVE-NG host is on a different machine, `rsync` the addon directory to the nova-ve host first:
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
cd "${NOVA_VE_REPO_DIR:-/var/lib/nova-ve/nova-ve-git}"
./deploy/scripts/import-eveng-templates.sh --dry-run
sudo ./deploy/scripts/import-eveng-templates.sh
```

The first command is a non-mutating plan. The second command performs the import with the same defaults. Those commands use the defaults most operators want:

| Purpose | Default |
|---|---|
| Service checkout | `${NOVA_VE_REPO_DIR:-/var/lib/nova-ve/nova-ve-git}` |
| Source tree | `/opt/unetlab` |
| Image destination | `/var/lib/nova-ve/images` |
| Generated templates | `/var/lib/nova-ve/templates` |
| Manifest | `/var/lib/nova-ve/import-manifest.json` |

The wrapper sources the backend environment file, changes into the installed backend directory, and runs the importer with the backend virtual environment. You only need the longer `--source`, `--dest`, `--templates-dir`, or `--manifest` flags when you intentionally use non-standard paths.

`--dry-run` and `--help` may be run without sudo. Real imports still require sudo because the script writes `/var/lib/nova-ve/images`, `/var/lib/nova-ve/templates`, and the manifest with service-account ownership.

### Step 1 — `--dry-run` first

Always plan the run before executing it.

```bash
cd "${NOVA_VE_REPO_DIR:-/var/lib/nova-ve/nova-ve-git}"
./deploy/scripts/import-eveng-templates.sh --dry-run
```

The dry run walks the source tree, evaluates each file's idempotency check (sha256 vs the existing destination), and prints a planned step-by-step narration (`would be moving ...`) plus the planned summary on stdout. No filesystem mutation, no manifest written. If `/opt/unetlab` is missing or empty, the planned manifest is empty (`imported: []`, `skipped: []`, `templates: []`, `errors: []`).

`--dry-run` does **not** require root.

### Step 2 — real run (move + symlink back)

Once the dry-run looks correct, drop the `--dry-run` flag:

```bash
cd "${NOVA_VE_REPO_DIR:-/var/lib/nova-ve/nova-ve-git}"
sudo ./deploy/scripts/import-eveng-templates.sh
```

This moves every image file into the destination (a rename on the same filesystem — no byte copy), symlinks the original EVE-NG path back at the moved file, and writes the manifest to `--manifest` (default `/var/lib/nova-ve/import-manifest.json`). If the source and destination live on different filesystems, the importer falls back to copy + sha256 verify + symlink for those files and says so in the step output and manifest `note`. Re-running the same command is idempotent: a source path that is already a symlink to its destination lands in `manifest.skipped[]` with `reason: "already migrated: source is a symlink to the destination"`.

Prefer the old behaviour? Pass `--copy-only` for the conservative copy + sha256 verify with sources fully preserved.

The shared IOL license file (`iourc`) is always copy-based even in the default mode, because every IOL image references the one file — moving it for one image would break the others.

### Step 3 — opt-in to delete sources (optional)

If you have verified the migration and want to reclaim the source disk space:

```bash
cd "${NOVA_VE_REPO_DIR:-/var/lib/nova-ve/nova-ve-git}"
sudo ./deploy/scripts/import-eveng-templates.sh --delete-source
```

`--delete-source` copies + sha256-verifies + deletes the source file *only after* the verify succeeds. If verification fails for any file, the source for that file is **not** deleted; the failure lands in `manifest.errors[]` and the run continues.

### Step 4 — instantiate a node from a migrated template

After the importer finishes, every priority-vendor template that produced a clean conversion lands in `/var/lib/nova-ve/templates/`. Operators see them in the existing `GET /api/list/templates/{template_type}` listing. In the lab editor, click the round **+** button → **Node** to open the "Add node" modal, pick the type tab, and choose the migrated template from the dropdown.

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
