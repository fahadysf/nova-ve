# Glossary

Project-specific terms, in alphabetical order.

**Adapter**
:   A Python class under `backend/scripts/import_eveng/adapters/` that converts one EVE-NG template into one nova-ve template. See [Vendor coverage](../importing-eveng/vendor-coverage.md).

**Bridge**
:   Synonym for **network** in nova-ve. The host-side Linux bridge (or OVS bridge) that joins node interfaces at L2. Bridge name: `nove<lab_hash:04x>n<network_id>`.

**Canvas**
:   The drag-pannable Svelte Flow editor surface in the lab UI.

**Console**
:   An HTML5 terminal pane backed by Apache Guacamole. Tabbed or floating layout, chosen from the top bar.

**Display name**
:   The human-friendly name shown on the canvas (e.g. `mgmt-net`). nova-ve enforces uniqueness on display names within a lab — duplicate creates return HTTP 409.

**Implicit network**
:   A bridge that exists in runtime state but is not declared in the lab JSON (e.g. an auto-created management bridge). Implicit networks are excluded from display-name uniqueness checks.

**Info panel**
:   A floating, draggable card that shows metadata about a node or network. Opened from the `(i)` button on the node or network card. Max 4 open simultaneously; LRU drop.

**Interface index**
:   The 0-based ordinal position of an interface within a node's NIC list. Used as the stable key for (node, interface) tuples in link endpoints and runtime attachment records.

**KVM acceleration**
:   The hardware-virtualization mode that makes QEMU nodes fast. Requires VT-x (Intel) or AMD-V (AMD) plus the `kvm` and `kvm-intel` / `kvm-amd` kernel modules. Verify with `kvm-ok`.

**Lab**
:   A JSON file under `/var/lib/nova-ve/labs/` plus its per-node runtime state directory. Owned by the `nova-ve` service user, mode 0600 / 0700.

**Link**
:   An attachment between two endpoints. One endpoint is always a node interface; the other is either a node interface (peer link) or a network (network link).

**Live MAC**
:   The MAC address currently bound to a running node's interface, as observed by the backend MAC reader. Compared against the planned MAC to flag mismatches in the port tooltip.

**`needs-manual-review`**
:   A template status flag the importer sets when an adapter cannot fully convert an EVE-NG template. The parsed-but-unconverted EVE-NG fields land under `_eveng_raw` for human inspection. See [Reading the manifest](../importing-eveng/manifest.md).

**Open slot**
:   The always-empty connection slot on a network pill, used to draw a new link. A network with N existing links displays N + 1 slots; the last one is the open slot.

**Paired-VM template**
:   A multi-node template that ships as one logical unit but spawns two coordinated VMs (e.g. Juniper vMX RE + PFE, Juniper vQFX RE + PFE).

**Pinned port label**
:   The small rounded interface-name pill that sits next to a connected port. Visible by default while a link is attached, hidden when the link is removed.

**Planned MAC**
:   The MAC the lab JSON declared for a node interface, before runtime. Compared against the live MAC at runtime.

**Port handle**
:   The small circle around a node's perimeter where a link attaches. Click + drag to draw a new link.

**Runtime tracker**
:   The backend in-memory record of every running node — PID, console port, live MAC per interface, interface-to-bridge attachments. Cleared per-node on stop.

**Slot index**
:   The 0-based ordinal slot position on a network pill. Slot 0 is the lexicographically-first attached link; slot N (where N is the link count) is the always-open free slot.

**Template**
:   A JSON file under `/var/lib/nova-ve/templates/<vendor>/<key>.json` that pre-fills the parameters of a new node. Sourced from the EVE-NG importer or hand-authored.

**veth pair**
:   The pair of virtual Ethernet devices used to connect a Docker container to a host bridge. One end lives in the container's network namespace; the other end is enslaved to the bridge.
