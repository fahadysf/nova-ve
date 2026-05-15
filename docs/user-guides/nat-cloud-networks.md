# NAT-Cloud networks

A NAT-Cloud network is the lab network type to use when nodes need outbound
access beyond the isolated lab. It is still a nova-ve bridge, so nodes attach
to it the same way they attach to an isolated bridge, but nova-ve also adds a
host gateway, DHCP, IPv4 forwarding, and outbound NAT through the host
interface that owns the default route.

Use NAT-Cloud for Docker or VM nodes that need package repositories, external
DNS, license checks, or other outbound-only access. Use a regular isolated
bridge when the segment should only connect nodes inside the lab.

## Create one

1. Open a lab in the canvas.
2. Add a network and choose **NAT-Cloud** as the network type.
3. Leave the advanced fields blank for the default behavior.
4. Attach node interfaces to the NAT-Cloud network the same way you attach
   them to an isolated network.
5. Start or restart the attached node so it can request a DHCP lease.

By default nova-ve allocates a `/24` from `10.255.0.0/16`, assigns the bridge
gateway as `.1`, and serves DHCP from `.100` through the last usable address.
For example, a default NAT-Cloud subnet can lease addresses such as
`10.255.0.144` to attached clients.

## What nova-ve configures

When the network is created or reconciled, nova-ve:

- creates the lab bridge if needed;
- assigns the configured gateway address to that bridge;
- enables host IPv4 forwarding;
- resolves the host egress interface from the live default route unless you
  configured an explicit egress interface;
- applies outbound masquerade NAT for the NAT-Cloud subnet;
- applies forwarding allow rules for outbound traffic and established return
  traffic; and
- starts a per-bridge `dnsmasq` DHCP process when DHCP is enabled.

Docker hosts commonly set the `FORWARD` path to drop by default. nova-ve
therefore installs the NAT-Cloud forwarding rules in Docker's `DOCKER-USER`
chain when that chain exists, so Docker's firewall policy does not block the
lab clients.

## Upgrade behavior

Re-running `install.sh` on an older host now deploys the NAT-Cloud helper
verbs and reconciles existing NAT-Cloud bridges after Docker and the backend
restart. The reconciliation is intentionally conservative: it re-applies DHCP,
NAT, and forwarding state only for NAT-Cloud bridges that already exist, and it
does not rewrite lab JSON.

The upgraded backend also reconciles deterministic Docker node container names
at node start. If a same-name nova-ve container is already running, nova-ve
adopts it into runtime state. If the same-name container is stopped, nova-ve
removes it before starting the node so Docker does not fail with a name
conflict.

## Troubleshooting

If a client receives a DHCP address but cannot reach the internet, check these
host-side facts:

```bash
ip route show default
cat /proc/sys/net/ipv4/ip_forward
sudo nft list table ip nova_ve
sudo nft list chain ip filter DOCKER-USER
pgrep -af 'dnsmasq.*nove'
```

Expected state:

- `ip route show default` shows the host egress interface.
- `/proc/sys/net/ipv4/ip_forward` is `1`.
- `table ip nova_ve` contains a masquerade rule for the NAT-Cloud subnet.
- `DOCKER-USER` contains one outbound and one established-return allow rule for
  the active NAT-Cloud bridge.
- `dnsmasq` is running for the bridge when DHCP is enabled.

If those are missing after an upgrade, re-run the installer:

```bash
curl -fsSL https://raw.githubusercontent.com/fahadysf/nova-ve/main/install.sh | sudo bash
```
