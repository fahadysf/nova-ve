# Migrating from EVE-NG / UNetLab / PNETLab to nova-ve

This guide walks an operator through migrating an existing EVE-NG (or UNetLab / PNETLab) host's images and templates to a fresh nova-ve install. The end state: every supported addon directory under `/opt/unetlab/addons/...` is materialised under `/var/lib/nova-ve/images/<kind>/<key>/`, every priority-vendor template is converted to a nova-ve template under `/var/lib/nova-ve/templates/`, and the operator can instantiate nodes from each migrated template through the nova-ve UI.

The importer ships as a venv'd Python module (`backend/scripts/import_eveng`) and a root-asserter shell wrapper at `deploy/scripts/import-eveng-templates.sh`.

## TL;DR — non-destructive default

> **Running `import-eveng-templates.sh` with no flags is non-destructive.** Sources under `/opt/unetlab/addons/...` are copied + sha256-verified at the destination; the source files are **left untouched** unless you pass an explicit opt-in flag.

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

1. **A reachable nova-ve host.** Use the [curl|sudo bash one-liner](#install-nova-ve) to provision a fresh Ubuntu 26.04 host from scratch. The provisioner creates `/var/lib/nova-ve/{images,templates,labs}` with the right ownership.
2. **Read access to the EVE-NG source tree.** Default source path is `/opt/unetlab`. If your EVE-NG host is on a different machine, `rsync` the addon directory to the nova-ve host first:
   ```bash
   rsync -a -e ssh root@old-eveng-host:/opt/unetlab/ /opt/unetlab/
   ```
3. **Root on the nova-ve host.** The importer needs to chown destination files to the canonical app owner (resolved from `NOVA_VE_OWNER`, falling back to `SUDO_USER` then `ubuntu`).

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

The dry run walks the source tree, evaluates each file's idempotency check (sha256 vs the existing destination), and emits a planned manifest on stdout. No filesystem mutation, no manifest written. On a freshly-provisioned host the planned manifest is empty (`imported: []`, `skipped: []`, `templates: []`, `errors: []`).

`--dry-run` does **not** require root.

### Step 2 — real run (non-destructive)

Once the dry-run looks correct, drop the `--dry-run` flag:

```bash
sudo deploy/scripts/import-eveng-templates.sh
```

This copies every file, verifies sha256 at the destination, and writes the manifest to `--manifest` (default `/var/lib/nova-ve/import-manifest.json`). **Sources are preserved.** Re-running the same command is idempotent: anything whose destination already matches the source sha256 lands in `manifest.skipped[]` with `reason: "exists, sha256 match"`.

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

## Importing images: layout and per-vendor recipes

The importer is image-aware: it walks the four addon directories EVE-NG ships, materialises each as a `<kind>/<image_key>/` tree under the destination, and emits a structured manifest entry per file.

### Source → destination mapping

| Kind | EVE-NG / PNETLab source | nova-ve destination |
|---|---|---|
| QEMU | `/opt/unetlab/addons/qemu/<vendor-ver>/*.qcow2`, `cdrom.iso`, etc. | `/var/lib/nova-ve/images/qemu/<vendor-ver>/` |
| Dynamips | `/opt/unetlab/addons/dynamips/<image>.image` | `/var/lib/nova-ve/images/dynamips/<image>/<image>.image` |
| IOL | `/opt/unetlab/addons/iol/bin/<image>.bin` (+ `iourc`) | `/var/lib/nova-ve/images/iol/<image>/<image>.bin` (+ `iourc`) |
| Docker | `/opt/unetlab/addons/docker/<image>/Dockerfile` (build context) | `/var/lib/nova-ve/images/docker/<image>/image.txt` (marker) |

Anything outside these four addon trees is ignored. To slim down a long-running import, `rsync` only the addon subdirectories you actually want before invoking the importer.

### QEMU images

Each vendor directory (`/opt/unetlab/addons/qemu/<vendor-ver>/`) is treated as one image. **Every file** inside it is copied to the matching `qemu/<vendor-ver>/` destination — `.qcow2` boot disks, `cdrom.iso` install media, and any vendor-specific helper files. The destination preserves filenames byte-for-byte so a QEMU-launched node sees the same disk geometry it saw under EVE-NG.

**Boot-disk precedence** (per-directory, used by template adapters to know which disk to attach as boot media):

1. `cdrom.iso`
2. `virtioa.qcow2`
3. `hda.qcow2`
4. The lexicographically-first `*.qcow2`

The chosen boot disk is recorded in the manifest as `meta.boot_disk` on the imported entry.

### Dynamips images

Each `*.image` file under `addons/dynamips/` becomes a single-file image: it is copied to `dynamips/<image_stem>/<image>.image`. Non-`.image` files in that directory are skipped.

### IOL images

Each `*.bin` file under `addons/iol/bin/` becomes one image. **The `iourc` license file is copied alongside every IOL image** if it exists in the source bin directory. The importer records `meta.iourc_present` (`true` / `false`) on every IOL entry; entries with `iourc_present: false` will not boot at runtime — re-import or hand-copy the license file before instantiating an IOL node.

### Docker images

Docker is the only kind that does not byte-copy. Each `addons/docker/<image>/` directory containing a `Dockerfile` becomes a build unit: the importer runs `docker build` against the source context and writes a single `image.txt` marker file at `docker/<image>/image.txt` recording the resulting tag (`nova-ve-<image>:latest` by default).

Implications:

- The host running the importer must have a usable Docker daemon. The default `install.sh` provisioner installs Docker, so this just works on a fresh nova-ve host.
- Re-running the importer on an unchanged build context will re-trigger the Docker build (the daemon's layer cache makes this cheap, but it is not skipped at the importer level the way file copies are).

### Verifying what landed

```bash
# Tree-view per kind:
tree -L 2 /var/lib/nova-ve/images/

# Just the QEMU images, by key:
ls /var/lib/nova-ve/images/qemu/

# Show manifest entries for one vendor-version:
jq '.imported[] | select(.dst | contains("/qemu/vyos-1.4/"))' \
  /var/lib/nova-ve/import-manifest.json

# Show every IOL image that landed without a license file:
jq '.imported[] | select(.dst | contains("/iol/")) | select(.meta.iourc_present == false)' \
  /var/lib/nova-ve/import-manifest.json
```

If a destination already matches the source sha256, the entry lands in `manifest.skipped[]` instead. Use `--force` to overwrite mismatched destinations.

### Vendor template adapter coverage

After image copy, the importer attempts to emit a nova-ve template per priority-vendor image. Adapters live under `backend/scripts/import_eveng/adapters/`. As of this writing, the supported set is:

- Arista vEOS
- Cisco CSR1000v
- Cisco IOL (`cisco_iol.py`)
- Cisco IOSv L2 / L3
- Generic Linux (catch-all for qemu images that don't match a more-specific adapter)
- Juniper vMX, vQFX, vSRX
- MikroTik CHR
- VyOS

Anything outside this set still has its image copied — only the *template* layer is skipped. The image is available; you can hand-author a template JSON under `/var/lib/nova-ve/templates/<vendor>/<key>.json` and the node modal will pick it up.

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

This provisions a fresh Ubuntu 26.04 host, builds the bundled `nova-ve-alpine-telnet:latest` demo image (skip with `NOVA_VE_SKIP_DEMO_IMAGES=1`), and writes `nova-ve-install-summary.md` with the bootstrapped admin credentials. See the install summary's "Demo images" section for the alpine-telnet status.

## Tracking

- [#182](https://github.com/fahadysf/nova-ve/issues/182) — umbrella tracker for the importer epic.
- [#184](https://github.com/fahadysf/nova-ve/issues/184) — non-destructive copy contract.
- [#188](https://github.com/fahadysf/nova-ve/issues/188) — paired-VM Juniper templates (vMX / vQFX).
