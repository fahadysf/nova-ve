# Console access

nova-ve uses Apache Guacamole for HTML5 consoles. Telnet, VNC, and supported RDP backends all funnel through Guacamole so you only need a browser — no native client required.

## Opening a console

1. Start the node (right-click → Start, or use Start All from the lab top bar).
2. Once the status badge flips to **running** (emerald), click the console icon on the node card.
3. The Guacamole pane opens in the lab's console workspace.

## Layout modes

The top bar has a **Console layout** selector with two modes:

| Mode | Behaviour |
|---|---|
| **Tabbed** | Open consoles stack as tabs inside a single resizable pane docked to the canvas. The tab count appears as a pill in the top bar (`{n} consoles`). |
| **Floating** | Each console opens as its own draggable, resizable window. Useful when you want side-by-side consoles. |

You can flip modes any time — existing consoles re-flow into the new layout.

## Console types per runtime

| Runtime | Default console | Notes |
|---|---|---|
| Docker | Telnet, VNC, or RDP | Container must run a matching service on the configured container-side port. VNC images can override the port with `vnc_port`; RDP defaults to container port `3389`. |
| QEMU/KVM | VNC or telnet | VNC is the expected graphical console for desktop/server guests such as Windows. QEMU RDP is not advertised because RDP runs inside the guest OS and needs guest IP and credential handling. |
| IOL / Dynamips | Telnet | Vendor IOL and Dynamips images speak telnet on a per-node port. |

The exact console type is template-defined and constrained by node type. Blank nodes default to the safest supported console for their runtime.

## Closing / minimizing

- **Tabbed mode** — click the × on a tab to close, or the minimize button to send the entire workspace to a docked bar.
- **Floating mode** — each window has its own header with close / minimize / maximize controls.

Closing a console drops the Guacamole session; it does **not** stop the node. Re-opening reconnects.

## Why is my console blank?

A few common causes:

- **The node is not running yet.** Wait for the emerald running badge.
- **Telnet console: the container has not bound its telnet server yet.** Give it 1–2 seconds after start; the bundled alpine-telnet image takes a moment.
- **VNC console: the guest has not produced display output yet.** Give the appliance or OS installer time to boot; if it stays blank, verify the template is using a VNC console.
- **Docker RDP console: the container is not serving RDP on port `3389`.** Use a VNC/telnet console instead, or use an image that actually starts an RDP service.
- **Guacamole-side issue.** Check `docker logs guacamole` on the host. See [Troubleshooting](../operations/troubleshooting.md).
