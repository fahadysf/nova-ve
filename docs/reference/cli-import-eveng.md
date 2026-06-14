# CLI — `import-eveng-templates`

Wrapper script: `deploy/scripts/import-eveng-templates.sh`
Underlying module: `backend/scripts/import_eveng` (run as `python -m scripts.import_eveng`).

The wrapper is the normal operator entrypoint. It sources `/etc/nova-ve/backend.env` when readable, asserts root for mutating imports, then `exec`s the venv'd Python module with the same argv. The default repo path is `/var/lib/nova-ve/nova-ve-git`; set `NOVA_VE_REPO_DIR` if the checkout lives elsewhere. `--dry-run` and `--help` may be run without sudo.

On a standard installed host, the one-shot flow is:

```bash
sudo /var/lib/nova-ve/nova-ve-git/deploy/scripts/import-eveng-templates.sh --dry-run
sudo /var/lib/nova-ve/nova-ve-git/deploy/scripts/import-eveng-templates.sh
```

## Synopsis

```text
sudo deploy/scripts/import-eveng-templates.sh
    [--source PATH] [--dest PATH] [--manifest PATH]
    [--templates-dir PATH|-]
    [--dry-run] [--force] [--verbose]
    [--copy-only | --move]
    [--delete-source]
```

## Flags

| Flag | Type | Default | Behaviour |
|---|---|---|---|
| `--source` | path | `/opt/unetlab` | EVE-NG / UNetLab install root to walk. |
| `--dest` | path | `/var/lib/nova-ve/images` | nova-ve images destination. |
| `--manifest` | path | `/var/lib/nova-ve/import-manifest.json` | Where the run manifest JSON is written. Not written when `--dry-run`. |
| `--templates-dir` | path or `-` | `/var/lib/nova-ve/templates` | Where generated nova-ve template YAML is written. Pass `-` to skip template generation. |
| `--dry-run` | flag | off | Plan the run without touching the destination filesystem. Does not require root. |
| `--force` | flag | off | Overwrite an existing destination on sha256 mismatch. Combinable with all copy modes. |
| `--verbose` | flag | off | DEBUG-level structured logging on stderr. |
| `--copy-only` | flag (mutually exclusive with `--move`) | implicit default | Copy + sha256 verify; sources preserved. Accepted for symmetry with `--move`. |
| `--move` | flag (mutually exclusive with `--copy-only`) | off | Copy + delete source; **skips sha256 verify**. Unsafe. |
| `--delete-source` | flag | off | After sha256 verification at the destination, delete the source. The explicit opt-in for destructive moves; the default leaves sources intact. |

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
/var/lib/nova-ve/nova-ve-git/deploy/scripts/import-eveng-templates.sh --dry-run

# Real run (non-destructive default):
sudo /var/lib/nova-ve/nova-ve-git/deploy/scripts/import-eveng-templates.sh

# Reclaim source disk after verification succeeds:
sudo /var/lib/nova-ve/nova-ve-git/deploy/scripts/import-eveng-templates.sh --delete-source

# Only QEMU images, by trimming the source tree first:
sudo rsync -a /opt/unetlab-from-eveng/addons/qemu/ /tmp/eveng-qemu-only/addons/qemu/
sudo /var/lib/nova-ve/nova-ve-git/deploy/scripts/import-eveng-templates.sh --source /tmp/eveng-qemu-only

# Copy images only, without generating templates:
sudo /var/lib/nova-ve/nova-ve-git/deploy/scripts/import-eveng-templates.sh --templates-dir -
```

## Related

- [Importing from EVE-NG → overview](../importing-eveng/overview.md)
- [Importing images](../importing-eveng/images.md)
- [Reading the manifest](../importing-eveng/manifest.md)
