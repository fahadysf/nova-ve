# VMware Deployment Requirements

When nova-ve runs as a guest on a VMware vSphere host (ESXi 7.0+ /
vSphere Distributed Switch), the Bridge-Cloud network type bridges
containers onto the same L2 segment as the host's vNIC. That path
crosses the VDS twice per packet, so a few portgroup settings must be
in place for containers to be reliably reachable from the rest of the
physical network.

This page applies only to VMware deployments. KVM, bare-metal, and
other hypervisors have no equivalent requirement.

## Required vSphere Distributed Switch portgroup settings

The portgroup the nova-ve VM's vNIC is attached to must have:

| Setting | Value | Why |
|---|---|---|
| **MAC Learning** | `True` | Lets the VDS forward unicast to the guest based on learned MAC table entries instead of full-portgroup flooding. |
| **Unknown Unicast Flooding** | `True` | When a learned MAC ages out of the VDS table, inbound unicast is flooded to all ports on the portgroup rather than silently dropped. Required so containers are reachable when they have no recent outbound traffic. |
| **MAC Address Changes** | `True` (Accept) | The Linux bridge inside the guest presents container MACs alongside the vNIC's burnt-in MAC. |
| **Forged Transmits** | `True` (Accept) | Containers source traffic with their own MAC, not the vNIC's. |
| **Promiscuous Mode** | `False` (Reject) | Not required and explicitly discouraged. With promiscuous on, the VDS reflects outbound broadcasts back to the source port, which can poison the in-guest bridge FDB and cause split-horizon drops. |

These are dvPortgroup settings; on a vSphere Standard Switch the
equivalent live under "Security" and "MAC Management" on each
portgroup.

The default for `Unknown Unicast Flooding` on a fresh portgroup is
`False`. **You will need to change this.**

## Why the bridge MAC is pinned

nova-ve's Bridge-Cloud Phase C renders a netplan stanza that pins
`br-ethN`'s MAC to the parent `ethN`'s MAC. Without this, Linux
assigns the bridge a random locally-administered MAC, which:

- changes whenever the lowest-MAC slave port changes (e.g. a new
  container veth with a lower numeric MAC joins);
- is unknown to the VDS at boot, so until enough outbound traffic
  refreshes the MAC table the bridge's host IP is unreachable from
  any peer with Unknown Unicast Flooding off;
- is unknown to anti-spoofing upstream (e.g. Dynamic ARP Inspection
  on the gateway router), which may have an existing IP-to-MAC
  binding for the host built when the vNIC's burnt-in MAC was on the
  network before the bridge existed. ARP from the random MAC
  claiming the host's IP is then treated as a spoof and dropped.

The pinning is automatic on new installs and idempotent on
re-provisions. No operator action is required.

## Detecting a misconfigured portgroup

The Phase C installer emits a single advisory line in the journal when
it detects a `vmxnet3` vNIC (PCI vendor `0x15ad`):

```
advisory iface=eth0 driver=vmxnet3 — confirm VDS portgroup has 'Unknown Unicast Flooding=True' and 'MAC Learning=True'
```

Symptoms of a misconfigured portgroup:

- The host can ping some peers on the same subnet but not others
  (the working peers are the ones with no anti-spoofing and a warm
  ARP cache for the host).
- The host can be reached from peers on the same VDS portgroup but
  not from anything routed through the upstream gateway.
- SSH from outside the L2 segment silently times out (no
  `Connection refused`) — TCP SYNs reach the host but SYN-ACK back
  is dropped at the VDS.

Recovery is just flipping the portgroup setting; no reboot or service
restart required on the nova-ve host.

## Non-VMware deployments

On KVM/QEMU, Linux bridge, OVS, or bare-metal hosts none of the
above applies. Bridge-Cloud's MAC pinning is harmless on those
platforms (the bridge MAC was going to be deterministic anyway) and
the advisory log line is suppressed because no `vmxnet3` interface
is present.
