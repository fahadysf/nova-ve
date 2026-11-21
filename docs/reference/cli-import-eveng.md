# CLI — `import-eveng-templates`

Wrapper script: `deploy/scripts/import-eveng-templates.sh`
Underlying module: `backend/scripts/import_eveng` (run as `python -m scripts.import_eveng`).

The wrapper is the normal operator entrypoint. It sources `/etc/nova-ve/backend.env` when readable, asserts root for mutating imports, then `exec`s the venv'd Python module with the same argv. The default repo path is `/var/lib/nova-ve/nova-ve-git`; set `NOVA_VE_REPO_DIR` if the checkout lives elsewhere. `--dry-run` and `--help` may be run without sudo.

On a standard installed host, the one-shot flow is:

```bash
cd "${NOVA_VE_REPO_DIR:-/var/lib/nova-ve/nova-ve-git}"
./deploy/scripts/import-eveng-templates.sh --dry-run
sudo ./deploy/scripts/import-eveng-templates.sh
```

## Synopsis

```text
sudo deploy/scripts/import-eveng-templates.sh
    [--source PATH] [--dest PATH] [--manifest PATH]
    [--templates-dir PATH|-]
    [--move-symlink | --copy-only | --delete-source | --move]
    [--log-file PATH] [--dry-run] [--force] [--json] [--verbose]
```

## Flags

The four copy-strategy flags are mutually exclusive — they are competing answers to "what happens to the source file". Since [#271](https://github.com/fahadysf/nova-ve/issues/271) the default is **move + symlink back**: image files are renamed into the destination and a symlink is left at the original `/opt/unetlab` path, so the bytes exist exactly once and EVE-NG keeps working.

| Flag | Type | Default | Behaviour |
|---|---|---|---|
| *(no mode flag)* | — | `--move-symlink` | Move image files into `--dest` and leave a symlink at the original path. Same-filesystem moves rename (zero bytes copied); cross-filesystem sources fall back to copy + sha256 verify + symlink. |
| `--move-symlink` | mode flag | on | Explicit spelling of the default. |
| `--copy-only` | mode flag | off | Conservative opt-out: copy + sha256 verify; sources preserved (the pre-#271 default). |
| `--delete-source` | mode flag | off | Copy + sha256 verify, then delete the source file. The explicit opt-in for destructive moves. |
| `--move` | mode flag | off | Copy + delete source; **skips sha256 verify**. Unsafe. |
| `--source` | path | `/opt/unetlab` | EVE-NG / UNetLab install root to walk. |
| `--dest` | path | `/var/lib/nova-ve/images` | nova-ve images destination. |
| `--manifest` | path | `/var/lib/nova-ve/import-manifest.json` | Where the run manifest JSON is written. Not written when `--dry-run`. |
| `--templates-dir` | path or `-` | `/var/lib/nova-ve/templates` | Where generated nova-ve template YAML is written. Pass `-` to skip template generation. |
| `--log-file` | path | `/var/log/eve-ng-importer.log` | Appending run log: every step line plus the final summary, with timestamps. Size-capped rotation (5 MiB × 3 backups). |
| `--dry-run` | flag | off | Plan the run without touching the destination filesystem. Narrates each planned step (`would be moving ...`). Does not require root. |
| `--force` | flag | off | Overwrite an existing destination on sha256 mismatch. Combinable with all copy modes. |
| `--json` | flag | off | Machine-readable output: one JSON log event per line on stderr instead of the human step-by-step stdout. |
| `--verbose` | flag | off | DEBUG-level logging in the log file (and on stderr under `--json`). |

## Output and logs

Every run prints numbered, human-readable step lines to stdout — one per file, docker build, template generation, and the final permission fixup pass — each with the size and elapsed time:

```text
[1/8] qemu/vyos-1.4 — moving hda.qcow2 (2.1 GiB) ... done (0.3s, symlinked back to /opt/unetlab/addons/qemu/vyos-1.4/hda.qcow2)
[6/8] iol/i86bi-linux-l2 — copying iourc (8 B) ... done (0.0s)
[8/8] applying ownership + permission fixups ... done (0.1s)
```

The same narration plus the run summary is appended to `/var/log/eve-ng-importer.log` (override with `--log-file`), so post-run debugging never depends on terminal scrollback. The summary references the log path, and any error points at it. Pass `--json` to swap the step lines for the machine-readable stderr event stream.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Success (including dry-run). |
| 2 | Tried to run non-`--dry-run` without root. |
| 3 | Could not resolve the service user for chown. |
| Non-zero (other) | Walker or copy engine raised. The manifest's `errors[]` lists specific files that failed without aborting the run; an entire run only aborts on argv parse failure or unrecoverable I/O. |

## Examples

```bash
# Dry-run plan first. This may be run without sudo:
cd "${NOVA_VE_REPO_DIR:-/var/lib/nova-ve/nova-ve-git}"
./deploy/scripts/import-eveng-templates.sh --dry-run

# Real run (move + symlink back default):
sudo ./deploy/scripts/import-eveng-templates.sh

# Conservative copy instead of move + symlink:
sudo ./deploy/scripts/import-eveng-templates.sh --copy-only

# Reclaim source disk after verification succeeds:
sudo ./deploy/scripts/import-eveng-templates.sh --delete-source

# Only QEMU images, by trimming the source tree first:
sudo rsync -a /opt/unetlab-from-eveng/addons/qemu/ /tmp/eveng-qemu-only/addons/qemu/
sudo ./deploy/scripts/import-eveng-templates.sh --source /tmp/eveng-qemu-only

# Import images only, without generating templates:
sudo ./deploy/scripts/import-eveng-templates.sh --templates-dir -

# Machine-readable event stream for CI / scripts:
sudo ./deploy/scripts/import-eveng-templates.sh --json
```

## Related

- [Importing from EVE-NG → overview](../importing-eveng/overview.md)
- [Importing images](../importing-eveng/images.md)
- [Reading the manifest](../importing-eveng/manifest.md)
