# Connecting nodes

A **link** in nova-ve attaches one node interface to a network (bridge) or to another node directly. The canvas surfaces this as a line between two endpoints.

## The two shapes of a link

| Shape | Endpoints | When to use |
|---|---|---|
| Node → Network | One node interface + one network (bridge) | The common case — you want a shared L2 segment that multiple nodes can join. |
| Node → Node (peer) | Two node interfaces | A point-to-point link with no shared bridge. Slightly leaner; cannot host more than two endpoints. |

## How networks work

A **network** is a Linux bridge (or OVS bridge) named `nove<lab_hash:04x>n<network_id>` on the host. Every node interface attached to that network gets a veth pair (Docker) or TAP device (QEMU) plumbed into the bridge.

In the canvas, networks are rendered as blue pill-shaped nodes with a connection count badge. Each network exposes **N + 1** slots: one slot per existing link plus one always-open slot for the next connection. Drag from a node port into the open slot to attach a new link.

!!! note "Display names are unique"
    nova-ve rejects creating two networks with the same display name (returns HTTP 409). The smart default-name picker in the canvas auto-skips conflicts (`net-1`, `net-2`, …). Backing bridges remain unique regardless of display name (each network has its own `network_id`).

## Drawing a link

1. Hover over a node — port handles light up around its perimeter.
2. Click + drag from a port handle.
3. Drop on:
    - a free port on another node — produces a peer link.
    - the open slot on a network — produces a node-to-network link.
4. The canvas commits the link to the lab JSON and (if the node is running) hot-attaches the interface at runtime.

## Pinned port labels

Connected ports display the interface name as a small rounded pill next to the port handle. The pill is visible by default so you can glance at the topology and read which interface is bound to which link, without hovering. Open ports (not yet linked) stay label-less.

The hover tooltip still works on a port — it shows the full planned MAC and live MAC (with mismatch warning) on top of the pinned label.

## Hovering over a link

Mouse near a link line and it boldens within ~3 px of the path. The wider hit area also makes the link easier to click — useful for deleting a single link or opening its config.

## Deleting a link

- Click the link line to select it.
- Press **Delete** or right-click → **Delete link**.

The canvas cascades: the link is removed from the lab JSON, the interface is detached from the bridge at runtime, and any orphan runtime attachment is auto-healed on the next link change (so you do not have to chase ghost attachments by hand).
