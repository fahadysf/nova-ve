# Shared networks

A shared network lets nodes in different labs connect through one network. The
network stays in the lab that created it. Other labs join it and attach their
own nodes, and every attached node is on the same layer 2 segment.

## Share a network

1. Right-click the network and choose **Make shared**. A check mark in the menu
   and a small icon on the network box show that it is shared.
2. Open the other lab, choose **Add → Network → Shared network**, pick the
   network, and give it a name in this lab.
3. Attach node interfaces as usual.

Any signed-in user can join a shared network, so share only what other labs may
reach. **Network Info** lists the labs on the segment and every connected
interface.

Isolated bridges and NAT-Clouds can be shared. A shared NAT-Cloud keeps one
subnet, one DHCP scope, and one pool of static addresses for all labs.
Bridge-Cloud networks already span labs through their host bridge, so the option
is disabled for them.

## Stop sharing

Delete the joined network in each of the other labs, then right-click the
network and clear **Make shared**. Deleting a joined network never affects the
lab that owns it.

While another lab uses a shared network, you cannot stop sharing it, delete it,
or delete its lab or folder. The message names the labs that use it.

## Duplicate MAC addresses

Two nodes with the same MAC address cannot share a segment reliably. New QEMU
nodes get addresses that differ between labs. QEMU nodes created by an earlier
release were numbered per lab, so node 1 in two labs can have the same address.
**Network Info** warns when it finds duplicates. Stop one of the nodes and change
its first MAC address in **Edit Node**.
