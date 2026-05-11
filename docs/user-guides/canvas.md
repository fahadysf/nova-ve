# The lab canvas

The lab canvas is a drag-pannable, zoomable view powered by Svelte Flow. It hosts every node, every network pill, and every link in the open lab. This page covers the interactive features that are not always self-evident.

## Top-bar metric pills

A 2 × 2 grid of compact pills sits at the right end of the header, each with an icon and a `Label: value` count:

- :material-server: **Nodes** — total node count in the lab.
- :material-pulse: **Running** — running node count. Flips to an emerald accent when greater than zero.
- :material-network: **Networks** — total network (bridge) count.
- :material-chip: **vCPU** — sum of every node's configured vCPU count.

The pills update in real time as you add / remove / start / stop things.

## Info panels

Every node and every network has a small `(i)` button:

- **Nodes** — bottom-left corner of the node card.
- **Networks** — inline next to the connection count badge.

Click it to open an **info panel** — a floating, draggable panel anchored to the top-left of the canvas. Panels stack vertically with a 12 px gap. You can have **up to 4 panels open at once**; opening a fifth evicts the oldest (LRU drop).

| Panel kind | Fields shown |
|---|---|
| Node | ID, Type, Template, Console, Image, CPU, RAM, NICs, Status, Runtime PID, IPs |
| Network | ID, Type, Links, Visible, Bridge name, Driver, Created at, Position |

Drag a panel by its header. The pointer-capture model keeps the drag alive even when the pointer crosses other elements. Viewport clamping ensures at least 48 px of the panel header stays on-screen so you can always grab it back.

Panels do not persist — they close on page reload.

## Pinned port labels

Connected ports (interfaces bound to a link) display the interface name as a small rounded pill right next to the port handle. The pill appears immediately on connection and disappears when the link is removed. Open (unconnected) ports stay label-less.

Hovering the port still shows the full hover tooltip (planned MAC, live MAC, mismatch warning); the pinned label hides itself while the hover tooltip is open so they do not visually stack.

## Link hover boldening

Mouse within ~3 px of a link line and the line boldens to make it easier to click. The bolden uses a 120 ms CSS transition so the effect feels smooth, not abrupt. The wider invisible hit area (`stroke-width: 8 px`, `pointer-events: stroke`) covers the bolden distance — you can click a link without pixel-precision.

## Loading skeleton

When you first open a lab, the canvas shows a single full-width dashed-bordered placeholder inside a rounded container. It animates with a soft pulse until the lab JSON and runtime state finish loading.

## Zooming and panning

| Gesture | Effect |
|---|---|
| Scroll | Zoom in / out (centered on the cursor) |
| Pinch (trackpad) | Same as scroll |
| Click + drag on empty canvas | Pan |
| Click + drag on a node | Move the node |
| Shift + click + drag on a port | Reposition the port around the node's perimeter |

Port positions are persisted to the lab JSON automatically (with a small debounce window so a continuous drag commits once at the end).
