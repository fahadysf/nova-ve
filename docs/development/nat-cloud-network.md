# NAT-Cloud Network

## Purpose

`nat_cloud` is the routed version of a nova-ve network. A regular
`linux_bridge` is an isolated L2 segment for nodes inside one lab. A
NAT-Cloud keeps that bridge attachment model, then adds a host gateway,
DHCP, IPv4 forwarding, and outbound NAT through the host interface that
currently owns the default route.

## Data Model

`Network.type` is `nat_cloud`.

`Network.config` owns operator intent:

| Field | Meaning |
| --- | --- |
| `cidr` | IPv4 subnet for the bridge. If omitted, nova-ve allocates one `/24` from `NOVA_VE_NAT_CLOUD_POOL` (`10.255.0.0/16` by default). |
| `gateway` | Host bridge IP. Defaults to the first usable address in `cidr` (`.1` for `/24`). |
| `dhcp` | Whether to run DHCP for the bridge. Defaults to `true`. |
| `dhcp_start` / `dhcp_end` | DHCP lease range. Defaults to `.100` through the last usable address. |
| `egress_interface` | Optional host interface override. If omitted, nova-ve resolves the live default-route interface during create/reconcile. |

`Network.runtime` records observed host state: `bridge_name`,
`driver="nat_cloud"`, `gateway`, `egress_interface`, `dhcp_*`,
`dhcp_pid`, and `nat="nftables"`.

Address ownership is split deliberately: `.1` is the gateway, `.2-.99`
is reserved for backend/static allocation, and `.100+` is DHCP.

## Host Lifecycle

Create, backend reconcile, and installer upgrade reconcile are idempotent:

1. Ensure the nova-ve bridge exists and has a matching ownership fingerprint.
2. Assign `gateway/prefix` to the bridge and bring it up.
3. Enable IPv4 forwarding.
4. Resolve the egress interface from the live default route unless
   `config.egress_interface` is set.
5. Apply an nftables masquerade rule for `config.cidr` out that interface.
6. Apply forwarding allow rules for outbound NAT-Cloud traffic and
   established return traffic. On Docker hosts, these rules go in
   `DOCKER-USER` because Docker's `FORWARD` chain commonly has policy
   `drop` and evaluates `DOCKER-USER` before Docker-managed bridge rules.
7. Start the per-bridge dnsmasq process when DHCP is enabled, with DNS
   serving disabled so it does not conflict with host DNS, or stop it when
   DHCP is disabled.

Delete removes the lab JSON record first, then best-effort stops dnsmasq,
removes forwarding/NAT nftables state, and deletes the bridge. A cleanup
failure must not resurrect the deleted network record.

## Privilege Boundary

The backend never invokes `ip`, `nft`, `dnsmasq`, or sysctl paths directly.
All host networking goes through `/opt/nova-ve/bin/nova-ve-net.py` fixed
verbs with regex/shape validation:

- `bridge-addr-add`
- `default-egress`
- `ipv4-forward-enable`
- `nat-apply`
- `nat-remove`
- `forward-apply`
- `forward-remove`
- `dnsmasq-start`
- `dnsmasq-stop`

This keeps NAT-Cloud inside the same privilege model as bridges, TAPs, and
veth links.

## UI Contract

The canvas network palette exposes NAT-Cloud as a network type. Its create
modal provides advanced fields for CIDR, gateway, DHCP range, and optional
egress interface. Blank fields use backend defaults so operators can create
a working NAT-Cloud without knowing the host route table.

## Installer Upgrade Contract

`install.sh` must make both fresh hosts and older running hosts converge on
the current NAT-Cloud contract. The provisioner therefore:

1. installs `dnsmasq` and `nftables`;
2. reinstalls `/opt/nova-ve/bin/nova-ve-net.py` and the matching sudoers
   fragment on every run;
3. restarts Docker and the backend before reconciliation; and
4. scans existing lab JSON for `nat_cloud` networks whose bridges already
   exist, then reapplies IPv4 forwarding, NAT, Docker `DOCKER-USER`
   forwarding rules, and per-bridge dnsmasq state.

The installer intentionally does not create missing bridges or rewrite lab
JSON. Bridge lifecycle remains owned by the lab/network runtime, while the
installer handles host package/helper drift and runtime firewall/DHCP state
that can be lost during Docker or service restarts.

Forwarding cleanup uses nft rule handles selected by nova-ve comments. Do not
switch this back to expression deletion: deployment validation showed that
expression-based deletion did not reliably remove duplicate `DOCKER-USER`
rules on the target host.
