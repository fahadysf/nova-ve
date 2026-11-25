# Bridge subnets and DHCP

An isolated bridge (the default **Linux bridge** network type) connects nodes
inside a lab and nothing else. You can give it a subnet and, optionally, let
nova-ve hand out addresses on it. Unlike a [NAT-Cloud](nat-cloud-networks.md),
an isolated bridge never gets a gateway, NAT or a route to the outside.

## Subnet only

Set a **CIDR** such as `10.40.1.0/24` when you create the bridge, or later
from **Configure…**. On its own the CIDR is information for you and for
nova-ve's address bookkeeping: nothing changes on the host, and the subnet may
overlap any other network.

Once set, the CIDR cannot be changed or removed. To use a different subnet,
delete the network and create it again.

## DHCP

DHCP can be turned on when the CIDR is an IPv4 subnet from `/22` to `/29`. It
is off by default. Turn it on in the create dialog, from **Configure…**, or
with **Enable DHCP** in the network's context menu.

nova-ve then runs a DHCP server on the bridge that offers addresses only: it
sends **no router and no DNS server**. Nodes get an address in the subnet and
nothing else, so traffic stays inside the lab unless a lab node routes it.

!!! warning "One DHCP server per segment"
    If a node on the bridge, such as a lab router, already serves DHCP, two
    servers answer the same clients. Leave nova-ve's DHCP off on that segment.

### Server address and range

The server address is the host's own address on the bridge. It must be a usable
address in the subnet and outside the range. Blank fields use these defaults:

| CIDR | Server | Range | Outside the range |
| --- | --- | --- | --- |
| `x.x.x.0/29` | `.6` | `.4` – `.5` | `.1` – `.3` |
| `x.x.x.0/24` | `.254` | `.128` – `.253` | `.1` – `.127` |
| `x.x.0.0/22` | `x.x.3.254` | `x.x.2.0` – `x.x.3.253` | `x.x.0.1` – `x.x.1.255` |

Addresses outside the range are yours for static configuration. Addresses
nova-ve assigns to nodes itself (for example Docker interfaces) start at the
second address of the subnet (`.2`; `.1` is left for a lab gateway) and never
come from the range or the server address. DHCP cannot be turned on while
such an address is already inside the range or on the server address.

### Subnet rules

A bridge with DHCP needs a subnet of its own. nova-ve refuses a CIDR that
overlaps:

- a subnet on a host interface;
- a NAT-Cloud in any lab;
- another bridge with DHCP in any lab.

NAT-Clouds, in turn, never get a subnet that overlaps a bridge with DHCP.

### What the host allows

With DHCP on, the bridge carries a host address, so nova-ve also guards it:

- from the bridge, the host accepts DHCP requests (and replies to its own
  pings) and nothing else; SSH, the web UI and every other host service stay
  unreachable through the bridge, and the host answers ARP there only for the
  server address;
- the host does not forward traffic into or out of the bridge;
- traffic between nodes on the bridge is not affected.

Turning DHCP off, deleting the network or deleting the lab removes the
server, the address and these rules. Nodes keep a leased address until the
lease expires (12 hours) or they renew it.

## Shared bridges

When a bridge is [shared](shared-networks.md), only the lab that owns it can
change its CIDR or DHCP. Labs that join it use the owner's settings.
