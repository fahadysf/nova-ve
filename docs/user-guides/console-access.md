# Console access

nova-ve uses Apache Guacamole for HTML5 consoles. Telnet, VNC, and supported RDP backends all funnel through Guacamole so you only need a browser — no native client required.

## Opening a console

1. Start the node (right-click → Start, or use Start All from the lab top bar).
2. Once the status badge flips to **running** (emerald), right-click the node and pick **Console** — or **2nd Console** if the node has a secondary console configured.
3. The Guacamole pane opens in the lab's console workspace.

## Native clients (optional)

With an externally reachable console host configured (System → Settings → Networking → *External console host*), console launches emit `telnet://`, `vnc://`, or `rdp://` links for your OS protocol handler instead of opening the embedded workspace — provided your desktop has a handler registered for the scheme. With no host configured, everything stays in-browser.

Every running node's console also listens directly on the nova-ve host in a fixed per-protocol port range — telnet/serial 15000–20000, VNC 25000–30000, RDP 35000–40000 — so you can dial a console by hand from any client. The advertised links use whatever reachable host name/IP you configured.

## Terminal fonts

nova-ve ships three Nerd Font families for in-browser console sessions (plus Roboto Mono): **JetBrainsMono Nerd Font Mono**, **FiraCode Nerd Font Mono**, and **MesloLGS Nerd Font Mono**. Terminal text is rendered server-side by guacd, so these fonts work in any browser.

- **Default**: pick one in System → Settings → *Terminal font* — applies to new console sessions after the backend restarts.
- **Per session**: open the Guacamole client menu (Ctrl+Shift+Alt) → Terminal behavior → **Font name** — the field offers the shipped fonts as dropdown suggestions, and accepts any font family installed in guacd.

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

The exact console type is template-defined and constrained by node type. A node can also declare a **secondary console** (a second telnet or VNC session, set in the node editor); it takes effect at the node's next start and gets its own **2nd Console** entry in the context menu.

## Closing / minimizing

- **Tabbed mode** — click the × on a tab to close it, or the minimize button to send the workspace to a docked bar. When closing a tab removes keyboard focus, the visible console receives it. Other tabs keep their sessions.
- **Floating mode** — each window has its own header with close / minimize / maximize controls.

Closing a console drops the Guacamole session; it does **not** stop the node. Re-opening reconnects.

## Why is my console blank?

A few common causes:

- **The node is not running yet.** Wait for the emerald running badge.
- **Telnet console: the container has not bound its telnet server yet.** Give it 1–2 seconds after start; the bundled alpine-telnet image takes a moment.
- **VNC console: the guest has not produced display output yet.** Give the appliance or OS installer time to boot; if it stays blank, verify the template is using a VNC console.
- **Docker RDP console: the container is not serving RDP on port `3389`.** Use a VNC/telnet console instead, or use an image that actually starts an RDP service.
- **Guacamole-side issue.** Check `docker logs guacamole` on the host. See [Troubleshooting](../operations/troubleshooting.md).
