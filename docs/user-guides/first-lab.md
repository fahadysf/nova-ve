# Creating your first lab

A lab in nova-ve is a JSON file under `/var/lib/nova-ve/labs/` that describes a topology — nodes, networks, and links between them. The lab editor renders that JSON as a visual canvas you can edit.

## Create a lab

1. Open `http://<host>/` and log in.
2. Click **Labs** in the top nav, then **New lab**.
3. Give the lab a name (this becomes the filename — `<name>.json`).
4. The editor opens with an empty canvas.

The lab JSON is created server-side at `/var/lib/nova-ve/labs/<name>.json`. It is owned by root with mode `0600`; reading it directly from the host requires `sudo cat`.

## What you can do next

- **Add nodes** — Docker containers or QEMU VMs from built-in or imported templates. See [Adding nodes](adding-nodes.md).
- **Add networks** — Linux bridges (or OVS bridges) that act as L2 hubs between nodes. The canvas calls these "Network" pills with a count badge.
- **Wire them together** — drag from a node port to a network or another node port. See [Connecting nodes](connecting-nodes.md).
- **Start nodes** — right-click a node → Start, or use the lab-level Start All control.
- **Open consoles** — once a node is running, click its console icon to open a Guacamole HTML5 console. See [Console access](console-access.md).

## Anatomy of the editor

| Region | What it does |
|---|---|
| Top bar | Lab name + path + metric pills (Nodes / Running / Networks / vCPU) and console-layout selector |
| Canvas | The main drag-pannable Svelte Flow canvas with nodes, networks, and links |
| Palette (left) | Add new nodes / networks; opens a modal for templated nodes |
| Info panels (top-left, floating) | Up to 4 draggable panels with per-node or per-network metadata |
| Console workspace | A tabbed (or floating) console pane that hosts Guacamole sessions |

For a guided tour of canvas interactions (info panels, top-bar pills, pinned port labels, link hover), see [The lab canvas](canvas.md).
