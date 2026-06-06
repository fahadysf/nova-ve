# Importing images — layout and per-vendor recipes

The importer is image-aware: it walks the four addon directories EVE-NG ships, materialises each as a `<kind>/<image_key>/` tree under the destination, and emits a structured manifest entry per file.

## Source → destination mapping

| Kind | EVE-NG / PNETLab source | nova-ve destination |
|---|---|---|
| QEMU | `/opt/unetlab/addons/qemu/<vendor-ver>/*.qcow2`, `cdrom.iso`, etc. | `/var/lib/nova-ve/images/qemu/<vendor-ver>/` |
| Dynamips | `/opt/unetlab/addons/dynamips/<image>.image` | `/var/lib/nova-ve/images/dynamips/<image>/<image>.image` |
| IOL | `/opt/unetlab/addons/iol/bin/<image>.bin` (+ `iourc`) | `/var/lib/nova-ve/images/iol/<image>/<image>.bin` (+ `iourc`) |
| Docker | `/opt/unetlab/addons/docker/<image>/Dockerfile` (build context) | `/var/lib/nova-ve/images/docker/<image>/image.txt` (marker) |

Anything outside these four addon trees is ignored. To slim down a long-running import, `rsync` only the addon subdirectories you actually want before invoking the importer.

## QEMU images

Each vendor directory (`/opt/unetlab/addons/qemu/<vendor-ver>/`) is treated as one image. **Every file** inside it is copied to the matching `qemu/<vendor-ver>/` destination — `.qcow2` boot disks, `cdrom.iso` install media, and any vendor-specific helper files. The destination preserves filenames byte-for-byte so a QEMU-launched node sees the same disk geometry it saw under EVE-NG.

**Boot-disk precedence** (per-directory, used by template adapters to know which disk to attach as boot media):

1. `cdrom.iso`
2. `virtioa.qcow2`
3. `hda.qcow2`
4. The lexicographically-first `*.qcow2`

The chosen boot disk is recorded in the manifest as `meta.boot_disk` on the imported entry.

## Dynamips images

Each `*.image` file under `addons/dynamips/` becomes a single-file image: it is copied to `dynamips/<image_stem>/<image>.image`. Non-`.image` files in that directory are skipped.

## IOL images

Each `*.bin` file under `addons/iol/bin/` becomes one image. **The `iourc` license file is copied alongside every IOL image** if it exists in the source bin directory. The importer records `meta.iourc_present` (`true` / `false`) on every IOL entry; entries with `iourc_present: false` will not boot at runtime — re-import or hand-copy the license file before instantiating an IOL node.

## Docker images

Docker is the only kind that does not byte-copy. Each `addons/docker/<image>/` directory containing a `Dockerfile` becomes a build unit: the importer runs `docker build` against the source context and writes a single `image.txt` marker file at `docker/<image>/image.txt` recording the resulting tag (`nova-ve/<image>:latest` by default).

Implications:

- The host running the importer must have a usable Docker daemon. The default `install.sh` provisioner installs Docker, so this just works on a fresh nova-ve host.
- Re-running the importer on an unchanged build context will re-trigger the Docker build (the daemon's layer cache makes this cheap, but it is not skipped at the importer level the way file copies are).

## Verifying what landed

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
