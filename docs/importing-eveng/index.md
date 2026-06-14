# Importing from EVE-NG / UNetLab / PNETLab

This section walks an operator through migrating an existing EVE-NG (or UNetLab / PNETLab) host's images and templates to a fresh nova-ve install.

The end state: every supported addon directory under `/opt/unetlab/addons/...` is materialised under `/var/lib/nova-ve/images/<kind>/<key>/`, every priority-vendor template is converted to a nova-ve template under `/var/lib/nova-ve/templates/`, and the operator can instantiate nodes from each migrated template through the nova-ve UI.

Use the one-shot shell wrapper for normal migrations:

```bash
cd "${NOVA_VE_REPO_DIR:-/var/lib/nova-ve/nova-ve-git}"
./deploy/scripts/import-eveng-templates.sh --dry-run
sudo ./deploy/scripts/import-eveng-templates.sh
```

The wrapper sources the installed nova-ve environment, finds the backend virtual environment, copies addon assets into `/var/lib/nova-ve/images`, writes generated templates under `/var/lib/nova-ve/templates`, and records `/var/lib/nova-ve/import-manifest.json`. The Python module (`backend/scripts/import_eveng`) is the implementation detail behind that wrapper.

## Pages in this section

- [Overview](overview.md) — one-shot wrapper usage, prerequisites, and run order (`--dry-run` → real → optional `--delete-source`).
- [Importing images](images.md) — per-kind source → destination mapping for qemu / dynamips / iol / docker, plus boot-disk precedence and verification recipes.
- [Vendor coverage](vendor-coverage.md) — which vendor adapters can emit a nova-ve template natively.
- [Reading the manifest](manifest.md) — the manifest JSON shape, handling `needs-manual-review`, and rollback.

!!! tip "Non-destructive by default"
    Running `import-eveng-templates.sh` with no flags is non-destructive. Sources under `/opt/unetlab/addons/...` are copied + sha256-verified at the destination; the source files are left untouched unless you pass `--delete-source` (or the unsafe `--move`).
