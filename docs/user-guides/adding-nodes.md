# Adding nodes

nova-ve nodes come in two flavours:

| Runtime | Backed by | Used for |
|---|---|---|
| **Docker** | A pre-built Docker image (e.g. `nova-ve/alpine-telnet:latest`) | Lightweight Linux endpoints, traffic generators, host emulators |
| **QEMU** | A `.qcow2` boot disk under `/var/lib/nova-ve/images/qemu/` | Full network OSes — vendor routers, switches, firewalls |

Both runtimes can be added from the editor palette.

## From a template

Templates pre-fill all the bits a runtime needs (image path, CPU, RAM, NIC count, boot disk, console settings). Built-in templates plus anything imported from EVE-NG appear in the "Add node" modal.

1. From the lab editor, click the **Add node** palette item.
2. Pick **From template**.
3. Filter by type (Docker, QEMU, IOL, Dynamips) or search by name.
4. Click a template card to instantiate. The node lands on the canvas at the click point.
5. Optionally edit the name / NIC count / per-port naming before clicking **Add**.

## Without a template

Use **Blank node** if you want full control:

- Pick the runtime (Docker or QEMU).
- Pick an image (Docker tag, or a `.qcow2` path under `/var/lib/nova-ve/images/qemu/`).
- Set CPU, RAM, NIC count, console type (telnet for Docker, VNC/SPICE for QEMU).

A blank node never produces a reusable template — if you want one, save it to `/var/lib/nova-ve/templates/<vendor>/<key>.json` by hand. See [Vendor coverage](../importing-eveng/vendor-coverage.md) for the template JSON shape.

## Per-port interface naming

Each node's NICs are named after a naming scheme (e.g. `eth0`, `eth1` for Linux; `Gi0/0`, `Gi0/1` for Cisco IOSv; `ge-0/0/0` for Juniper). The scheme is template-defined for templated nodes; blank nodes default to `eth<n>`.

To override a single port's name, open the Node config modal → **Interface naming** → switch to **Per-port** → enter a comma-separated list (e.g. `mgmt0, lan0, lan1`).

## Where node state lives

| Path | Holds |
|---|---|
| `/var/lib/nova-ve/labs/<lab>.json` | Lab topology — every node, its config, its links |
| `/var/lib/nova-ve/labs/<lab>/<node-id>/` | Per-node runtime state — qcow2 overlays, log files |
| `/var/lib/nova-ve/images/<kind>/<key>/` | Read-only base images (shared across labs) |

When you delete a node from the canvas, the runtime state directory and any links touching that node are cleaned up.
