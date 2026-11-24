# Importing images — layout and per-vendor recipes

The importer is image-aware: it walks the four addon directories EVE-NG ships, materialises each as a `<kind>/<image_key>/` tree under the destination, and emits a structured manifest entry per file.

## How files move (default mode)

Since [#271](https://github.com/fahadysf/nova-ve/issues/271), image files are **moved, not copied**: each file is renamed into the nova-ve destination and a symlink is left at its original `/opt/unetlab` path pointing at the moved file. The bytes exist exactly once, EVE-NG keeps resolving its original paths, and a multi-GiB import takes seconds instead of a full disk write. When the source and destination live on different filesystems, those files fall back to copy + sha256 verify + symlink and the step output and manifest `note` say so.

Pass `--copy-only` for the conservative pre-#271 behaviour (copy + sha256 verify, sources left untouched).

## Source → destination mapping

| Kind | EVE-NG / PNETLab source | nova-ve destination |
|---|---|---|
| QEMU | `/opt/unetlab/addons/qemu/<vendor-ver>/*.qcow2`, `cdrom.iso`, etc. | `/var/lib/nova-ve/images/qemu/<vendor-ver>/` |
| Dynamips | `/opt/unetlab/addons/dynamips/<image>.image` | `/var/lib/nova-ve/images/dynamips/<image>/<image>.image` |
| IOL | `/opt/unetlab/addons/iol/bin/<image>.bin` (+ `iourc`) | `/var/lib/nova-ve/images/iol/<image>/<image>.bin` (+ `iourc`) |
| Docker | `/opt/unetlab/addons/docker/<image>/Dockerfile` (build context) | `/var/lib/nova-ve/images/docker/<image>/image.txt` (marker) |

Anything outside these four addon trees is ignored. To slim down a long-running import, `rsync` only the addon subdirectories you actually want before invoking the importer.

## QEMU images

Each vendor directory (`/opt/unetlab/addons/qemu/<vendor-ver>/`) is treated as one image. **Every file** inside it is moved to the matching `qemu/<vendor-ver>/` destination — `.qcow2` boot disks, `cdrom.iso` install media, and any vendor-specific helper files. The destination preserves filenames byte-for-byte so a QEMU-launched node sees the same disk geometry it saw under EVE-NG.

**Boot-disk precedence** (per-directory, used by template adapters to know which disk to attach as boot media):

1. `cdrom.iso`
2. `virtioa.qcow2`
3. `hda.qcow2`
4. The lexicographically-first `*.qcow2`

In the default mode the destination dir keeps the stable `cdrom.iso` handle: a symlink inside the destination pointing at the chosen boot disk.

## Dynamips images

Each `*.image` file under `addons/dynamips/` becomes a single-file image: it is moved to `dynamips/<image_stem>/<image>.image` and symlinked back at its original path. Non-`.image` files in that directory are skipped.

## IOL images

Each `*.bin` file under `addons/iol/bin/` becomes one image: moved like the other kinds. **The `iourc` license file is the exception — it stays copy-based** even in the default mode, because it is one shared license referenced by every IOL image; moving it for the first image would break the others. An IOL image imported with no license file found gets a `needs-manual-review` template entry noting the missing license.

At runtime nova-ve first uses an image-local `iourc` next to the selected IOL image. If that file is absent, it falls back to the system IOURC directory, `/var/lib/nova-ve/iourc/` by default. Upload the license from the IOL node add modal, or place a valid file in that directory manually. The node add modal shows whether the system IOURC license is installed and displays the host identity details needed to prepare a license.

## Docker images

Docker is the only kind that does not byte-move. Each `addons/docker/<image>/` directory containing a `Dockerfile` becomes a build unit: the importer runs `docker build` against the source context and writes a single `image.txt` marker file at `docker/<image>/image.txt` recording the resulting tag (`nova-ve/<image>:latest` by default).

Implications:

- The host running the importer must have a usable Docker daemon. The default `install.sh` provisioner installs Docker, so this just works on a fresh nova-ve host.
- Re-running the importer on an unchanged build context will re-trigger the Docker build (the daemon's layer cache makes this cheap, but it is not skipped at the importer level the way file moves are).

## Verifying what landed

```bash
# Tree-view per kind:
tree -L 2 /var/lib/nova-ve/images/

# Just the QEMU images, by key:
ls /var/lib/nova-ve/images/qemu/

# Show manifest entries for one vendor-version:
jq '.imported[] | select(.dst | contains("/qemu/vyos-1.4/"))' \
  /var/lib/nova-ve/import-manifest.json

# Show which files were moved + symlinked vs copy-based:
jq '.imported[] | select(.symlinked == true) | .dst' \
  /var/lib/nova-ve/import-manifest.json

# Show IOL templates flagged for a missing license file:
jq '.templates[] | select(.status == "needs-manual-review")' \
  /var/lib/nova-ve/import-manifest.json
```

On reruns, the importer skips source paths already linked to their destinations. For other existing destinations, the importer checks the file size and compares the first and last 64 KiB. When those match, it records a skip in the manifest without hashing the full file. Otherwise, it compares SHA-256 digests. Use `--force` to bypass sampling and overwrite destinations whose full SHA-256 differs.
