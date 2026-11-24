# Adding nodes

nova-ve nodes come in four flavours:

| Runtime | Backed by | Used for |
|---|---|---|
| **Docker** | A pre-built Docker image (e.g. `nova-ve/alpine-telnet:latest`) | Lightweight Linux endpoints, traffic generators, host emulators |
| **QEMU** | A `.qcow2` boot disk under `/var/lib/nova-ve/images/qemu/` | Full network OSes — vendor routers, switches, firewalls |
| **IOL** | Cisco IOS-on-Linux images under `/var/lib/nova-ve/images/iol/` | Cisco router/switch images that run as Linux processes |
| **Dynamips** | Cisco IOS `.bin` images under `/var/lib/nova-ve/images/dynamips/` | Emulated classic Cisco hardware (c3725, c7200) — see [Dynamips nodes](dynamips-nodes.md) |

All runtimes are added from the canvas's add-element menu.

## From a template

Templates pre-fill all the bits a runtime needs (image path, CPU, RAM, NIC count, boot disk, console settings). Built-in templates plus anything imported from EVE-NG appear in the "Add node" modal. Every node is instantiated from a template.

1. From the lab editor, click the round **+** button at the bottom-left of the canvas ("Open add element menu").
2. Pick **Node** — the "Add node" modal opens with type tabs (QEMU / Docker / IOL / Dynamips).
3. Pick a template from the dropdown, then set the name prefix, how many nodes to add, and the placement grid.
4. Confirm — the nodes appear on the canvas.

## Canvas icons

Place custom PNG, JPEG, WebP, or GIF files in the `icons/` subdirectory of
`USER_TEMPLATES_DIR`. On a default install this is
`/var/lib/nova-ve/templates/icons/`. The service must be able to read the files.

To set a default for nodes created from a template, add `icon: edge-router.png`
to that template's YAML. Reopen the lab editor to refresh its icon choices. In
the node settings, select an image under **Icon** to override the template, or
select **Use template default** to reset the override. The canvas tries the
template icon when an override is unavailable, then uses its built-in glyph
if neither image can be displayed.

## Custom templates

Hand-authored template JSON files under `/var/lib/nova-ve/templates/<vendor>/<key>.json` are loaded alongside the built-ins. See [Vendor coverage](../importing-eveng/vendor-coverage.md) for the template JSON shape.

## Per-port interface naming

Each node's NICs are named after a naming scheme (e.g. `eth0`, `eth1` for Linux; `Gi0/0`, `Gi0/1` for Cisco IOSv; `ge-0/0/0` for Juniper). The scheme is template-defined; presets plus a **Custom…** entry are available in the node editor.

To use your own scheme, open the node editor → **Interface naming** → choose **Custom…** → enter a comma-separated list (e.g. `mgmt0, lan0, lan1`). A node keeps the scheme it was created with, so a change to a template's labels applies to nodes added afterwards. Custom schemes are used exactly as typed.

### Label abbreviations

A port label carries at most four letters before its first digit, slash or dot, so that it stays readable on a node card. Numbering is never changed — only the words are shortened. Built-in templates use the short form the platform itself prints where there is one, and a chosen short form otherwise:

| Platform | Full interface type | Label |
|---|---|---|
| Cisco IOS, IOS-XE | `FastEthernet`, `GigabitEthernet`, `TwoGigabitEthernet`, `FiveGigabitEthernet` | `Fa`, `Gi`, `Tw`, `Fi` |
| Cisco IOS, IOS-XE | `TenGigabitEthernet`, `TwentyFiveGigE`, `FortyGigabitEthernet`, `FiftyGigabitEthernet` | `Te`, `Twe`, `Fo`, `Fif` |
| Cisco IOS, IOS-XE | `HundredGigE`, `TwoHundredGigE`, `FourHundredGigE` | `Hu`, `Two`, `Fou` |
| Cisco IOS, IOS-XE | `Loopback`, `Tunnel`, `Port-channel`, `Vlan`, `Serial`, `Dialer` | `Lo`, `Tu`, `Po`, `Vl`, `Se`, `Di` |
| Cisco ASAv, FTD | `Management0/0`, `Diagnostic0/0` | `Ma0/0`, `Diag0/0` |
| Cisco NX-OS | `Ethernet`, `Loopback`, `Port-channel` | `Eth`, `Lo`, `Po` |
| Cisco IOS-XR | `MgmtEth`, `GigabitEthernet`, `Bundle-Ether` | `Mg`, `Gi`, `BE` |
| Arista EOS | `Ethernet`, `Management`, `Port-Channel`, `Loopback`, `Vlan`, `Tunnel` | `Et`, `Ma`, `Po`, `Lo`, `Vl`, `Tu` |
| Palo Alto PAN-OS | `ethernet1/1`, `loopback`, `tunnel`, `vlan` | `e1/1`, `lo`, `tu`, `vl` |
| Dell OS10 | `ethernet1/1/1` | `eth1/1/1` |
| Aruba, Huawei, HPE Comware | `GigabitEthernet` | `GE` |
| Huawei VRP | `LoopBack`, `Vlanif`, `Tunnel` | `Lo`, `Vl`, `Tu` |
| HPE Comware | `Ten-GigabitEthernet`, `Bridge-Aggregation`, `Route-Aggregation` | `XGE`, `BAGG`, `RAGG` |
| Windows | `Ethernet` | `Eth` |
| MikroTik RouterOS | `ether1` | `e1` |
| VMware ESXi | `vmnic0` | `vmn0` |
| FreeBSD, pfSense, OPNsense | `vtnet0` | `vtn0` |

Juniper (`ge-0/0/0`), Nokia (`1/1/c1/1`), F5 (`1.1`), Fortinet (`port1`) and the Linux family (`eth0`, `swp1`) are already short and are used unchanged.

## Where node state lives

| Path | Holds |
|---|---|
| `/var/lib/nova-ve/labs/<lab>.json` | Lab topology — every node, its config, its links |
| `/var/lib/nova-ve/labs/<lab>/<node-id>/` | Per-node runtime state — qcow2 overlays, log files |
| `/var/lib/nova-ve/images/<kind>/<key>/` | Read-only base images (shared across labs) |

When you delete a node from the canvas, the runtime state directory and any links touching that node are cleaned up.
