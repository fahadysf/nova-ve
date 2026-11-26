# Isolated bridge DHCP

A `linux_bridge` network may carry an informational CIDR and, for `/22`
through `/29`, run its own DHCP server with no router and no DNS option. The
user-facing rules are in [Bridge subnets and DHCP](../user-guides/bridge-dhcp.md).

## Data model

`Network.config` holds intent:

| Field | Meaning |
| --- | --- |
| `cidr` | IPv4 network, normalized (`10.40.1.0/24`). Optional; immutable once set. |
| `dhcp` | `true` only with a `/22`–`/29` CIDR. Defaults to `false`. |
| `dhcp_server` | Host address on the bridge. Default: broadcast − 1. |
| `dhcp_start` / `dhcp_end` | Scope. Default: network + size/2 through broadcast − 2. |

The three DHCP fields are stored only for a CIDR that allows DHCP, and are
kept when `dhcp` goes back to `false` so that turning it on again reuses them.
A record counts as running DHCP only when `dhcp` is `true` and all of `cidr`,
`dhcp_server`, `dhcp_start` and `dhcp_end` are set
(`cloud_inventory_service.bridge_dhcp_enabled`); a bare `dhcp: true` from
before this feature is never applied, and an edit switches it on only when it
sends `dhcp` itself. Members of a shared bridge never run a
server.

`Network.runtime` records what was applied: `dhcp`, `dhcp_server`,
`dhcp_start`, `dhcp_end` and `dhcp_pid`.

## API

- `POST /api/labs/{lab}/networks` and `PATCH .../networks/{id}` accept the
  fields above. A PATCH sends only changed keys; a blank server or scope
  field means the default.
- `POST /api/labs/{lab}/networks/validate-config` with
  `{type, config, network_id?}` returns `{"data": {"config": ...}}`, the
  normalized settings a create or edit would store. It changes nothing and
  needs access to the lab. `type: "nat_cloud"` behaves like
  `validate-nat-cloud`.
- `GET .../networks/{id}/info` reports `ip.dhcp` for a bridge whose CIDR
  allows DHCP, in the same shape as for a NAT-Cloud: `enabled`, `server`,
  `start`, `end`, `size`, `lease_time`, `running`, `pid`, `leases`,
  `lease_count`. It is `null` for other CIDRs.

Errors use the network error body with `field` naming the input
(`config.cidr`, `config.dhcp`, `config.dhcp_server`, `config.dhcp_start`,
`config.dhcp_end`, `type`). Subnet conflicts are `409` and add
`conflicting_cidr` and `source`; `source` does not name labs the caller cannot
open.

## Host lifecycle

Turning DHCP on runs, in order: `bridge-guard-apply`, `bridge-addr-add`
(`server/prefix`), `link-up`, `dnsmasq-start ... --no-router`. Turning it off
runs `dnsmasq-stop`, `bridge-addr-del`, `bridge-guard-remove`. The guard is in
place before the address exists and stays until it is gone: if removing the
address fails, the guard stays too. A failing step
puts the previous state back and leaves `lab.json` unchanged.

Toggling DHCP never recreates the bridge or detaches links. Delete and lab
cleanup remove the server, address and guard before deleting the bridge,
because the server and the nftables rules match the bridge by name and would
outlive it. Lab open (`ensure_lab_bridges`) re-applies the guard and address
and restarts the server only when it is not running or its settings changed;
a bridge with DHCP off is cleaned only when the runtime shows something
applied. Opening a lab that joined a shared bridge restores the owner's
server too.

Subnet choices are serialized host-wide with `nat_cloud_ipam_lock`, shared
with NAT-Cloud allocation.

## Guard rules

`bridge-guard-apply <bridge>` writes one nft transaction into table
`inet nova_ve_bridge_guard`, with two base chains per bridge at priority `-10`
(before Docker's chains at `0`):

```
chain in_<bridge> { type filter hook input priority -10; policy accept;
  iifname "<bridge>" meta nfproto ipv4 udp sport 68 udp dport 67 accept
  iifname "<bridge>" icmp type echo-reply accept
  iifname "<bridge>" drop
}
chain fwd_<bridge> { type filter hook forward priority -10; policy accept;
  iifname "<bridge>" oifname "<bridge>" accept
  iifname "<bridge>" drop
  oifname "<bridge>" drop
}
```

Input from the bridge is DHCP only, plus ICMP echo replies so that dnsmasq's
check that an address is free before offering it works; IPv6 is dropped with
everything else. Apply also sets `net.ipv4.conf.<bridge>.arp_ignore=1`, so
the host answers ARP on the bridge only for the server address instead of
every local address; remove resets it to `0`. A
drop is final in nftables whatever later chains decide, so routed traffic is
blocked both ways regardless of the host's forward policy. Frames bridged
between nodes on the same bridge reach the forward hook only when
`br_netfilter` is loaded, and then with the bridge as both devices; the first
forward rule lets them continue unchanged. There are no NAT rules.

`bridge-guard-remove <bridge>` deletes exactly that bridge's two chains, and
the table once no chain is left. Both verbs are idempotent and serialized by a
file lock (`/run/nova-ve-bridge-guard.lock`, opened without following
links). The helper writes `dnsmasq.conf` through a fresh temporary file and
a rename, and refuses symlinked directories under its own root: a second
line of defence behind the root-owned location described below.

## dnsmasq

The server shares the NAT-Cloud state directory
`/run/nova-ve/dnsmasq/<bridge>/` (configuration, pid file, leases), so the
info panel reads leases and the live process the same way for both. The tree
belongs to root, because dnsmasq runs as root and must not read its
configuration, or write its leases, where the service account can replace
files. The helper writes `dnsmasq.conf` and the one-time copy of any legacy
leases; dnsmasq writes the pid and leases. `/run` is tmpfs, so the helper
recreates the tree on start and leases there do not survive a reboot.

On upgrade from the legacy `<data dir>/nat-cloud/<bridge>/` layout, the
helper finds servers in either layout, removes the legacy pid file when
stopping a server, and copies eligible legacy leases on the first start.
After the new server is confirmed running, it renames the legacy `leases`
to `leases.pre-334`. The backend and helper still read the legacy directory
as a fallback when needed.

In `--no-router` mode the configuration has `port=0` (no DNS service),
`listen-address=<server>`, and empty `dhcp-option=3` and `dhcp-option=6`,
which tell dnsmasq to send no router and no DNS server. It is not
authoritative, so it does not NAK clients of another server on the segment.

## Deploys and upgrades

Upgrades from the legacy layout require a maintenance window for DHCP:

1. Stop the old backend and prevent older backend instances or helpers from
   starting more legacy servers. Install the new privileged helper before
   restarting the backend.
2. Inspect running dnsmasq command lines for each legacy
   `--conf-file=<data dir>/nat-cloud/<bridge>/dnsmasq.conf`. Stop each such
   server using the new helper's `dnsmasq-stop <bridge>` command, or migrate
   it with the new helper using its saved DHCP settings. Stopping preserves
   its legacy leases for the next start. This interrupts DHCP service.
3. Verify that no legacy dnsmasq command line remains, then start the new
   backend and open the affected labs to reconcile their DHCP servers.

The backend checks the installed helper's protected-layout capability before
serving requests. It must also have visibility of the host process list; the
startup check cannot detect processes hidden by the host's procfs
configuration. It refuses startup if it finds a legacy server or encounters
an error inspecting the process list. Refusing startup does not stop an
existing legacy server: it continues to read a service-owned configuration
until it is stopped or
migrated, so the upgrade is incomplete until the verification succeeds.
The check is a startup snapshot; keep older backends and helpers from
starting legacy servers after it. A new helper with an old backend can make
the old backend report DHCP stopped or restart it on lab open. An old helper
with a new backend can recreate legacy servers and is unsupported.
