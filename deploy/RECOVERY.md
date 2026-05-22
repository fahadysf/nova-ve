# Bridge-Cloud Recovery Guide

When `deploy/scripts/provision-ubuntu-2604.sh` configures the
Bridge-Cloud feature on a host, it runs in two phases separated by a
reboot.  This document walks through every non-`complete` marker state
written to `/etc/nova-ve/bridge-cloud.state` and the exact commands to
recover from each one.

## Marker states

| State | Meaning |
|---|---|
| `absent` | Phase B has not run yet.  Safe re-run. |
| `naming-flipped` | Phase B done (grub edited, reboot scheduled).  Phase C runs on next boot. |
| `complete` | Phase C succeeded.  Host bridges (`br-eth*`) are up. |
| `rollback-applied` | Phase C failed and the original netplan was restored.  No data loss. |
| `rollback-applied-emergency` | Phase C failed AND the restore-apply also timed out.  Host may be unreachable; console access required. |
| `unsupported-renderer` | A `/etc/netplan/*.yaml` declares `renderer: NetworkManager`.  Phase C refused to run; no changes made. |

## How to read the current marker

```bash
sudo cat /etc/nova-ve/bridge-cloud.state
sudo systemctl status nova-ve-postboot.service
sudo journalctl -u nova-ve-postboot --since '10 minutes ago'
```

Each `phase=` line in the journal records exactly what happened.

## Recovery scenarios

### Scenario 1 — `rollback-applied`

Phase C tried to apply the new netplan but the gateway became unreachable
from `br-eth0` (or the apply itself timed out).  The post-boot script has
already restored the previous netplan and re-applied it.  Outbound
connectivity should match the pre-change state.

```bash
# 1. Confirm outbound reachability is back.
ping -c 3 -W 3 "$(ip -4 route show default | awk '/^default/{print $3}')"

# 2. Inspect the journal to see WHY the rollback fired.
sudo journalctl -u nova-ve-postboot --since '30 minutes ago' \
    | grep -E 'phase='

# 3. Address the cause (typical: NIC enumeration changed, gateway moved,
#    upstream LAN gated DHCP, etc.).  Then re-arm Phase C by clearing the
#    marker and re-running the post-boot service.
sudo rm /etc/nova-ve/bridge-cloud.state
sudo systemctl restart nova-ve-postboot.service
```

Do NOT manually edit `/etc/netplan/60-nova-ve-bridge-cloud.yaml`; the
post-boot generator regenerates it from scratch each run.

### Scenario 2 — `rollback-applied-emergency`

Phase C tried to roll back AND `timeout 60s netplan apply` failed during
the restore.  The host may have lost network connectivity entirely.
This is a critical state that requires console access (KVM, IPMI, lab
serial, or hypervisor console).

```bash
# 1. From console: confirm what netplan is currently in effect.
ls -la /etc/netplan/

# 2. Use the latest backup snapshot stored under /etc/nova-ve/.
ls -la /etc/nova-ve/netplan.backup.*/
LATEST=$(ls -1d /etc/nova-ve/netplan.backup.* | sort -V | tail -1)
echo "Restoring from ${LATEST}"

# 3. Stop systemd-networkd briefly so the apply runs cleanly.
sudo systemctl stop systemd-networkd

# 4. Force-restore the snapshotted files and apply.
sudo /opt/nova-ve/bin/nova-ve-backup.py restore "${LATEST}" /etc/netplan
sudo rm -f /etc/netplan/60-nova-ve-bridge-cloud.yaml
sudo systemctl start systemd-networkd
sudo netplan apply

# 5. Clear the marker and disable the post-boot service so it doesn't
#    re-fire on the next reboot.
sudo rm /etc/nova-ve/bridge-cloud.state
sudo systemctl disable nova-ve-postboot.service
```

Once the host is reachable, file an issue with the journal contents
(`journalctl -u nova-ve-postboot > /tmp/postboot.log`).

### Scenario 3 — `unsupported-renderer`

A `/etc/netplan/*.yaml` declares `renderer: NetworkManager`.  Nova-VE
generates netplan with `renderer: networkd`; mixing the two in the same
host triggers undefined behaviour, so Phase C refuses to run.

```bash
# Option A — keep NetworkManager:
#   Bridge-Cloud is not supported on NetworkManager-managed hosts in v1.
#   Use a single, dedicated networkd-managed Ubuntu host instead.

# Option B — migrate the host to systemd-networkd:
#   1. Inventory which yaml files reference NetworkManager.
grep -rE '^\s*renderer:\s*NetworkManager' /etc/netplan/
#   2. Stop and disable NetworkManager.
sudo systemctl stop NetworkManager
sudo systemctl disable NetworkManager
#   3. Edit each yaml file: replace ``renderer: NetworkManager`` with
#      ``renderer: networkd``.  Verify with ``sudo netplan generate``.
#   4. Clear the marker and re-run the install script.
sudo rm /etc/nova-ve/bridge-cloud.state
sudo /path/to/repo/deploy/scripts/provision-ubuntu-2604.sh
```

### Scenario 4 — Aborted Phase B (Ctrl-C during the 30s countdown)

The install script prints a 30-second countdown before rebooting.  If
the operator hits Ctrl-C in that window, the script exits and the
marker is left at `naming-flipped`.  Phase B is complete; only the
reboot is pending.

```bash
# Reboot manually whenever you are ready.  Phase C will run on boot.
sudo reboot
```

Or, if you've decided NOT to migrate this host after all:

```bash
# 1. Remove the grub flip.
sudo sed -i -E 's| net.ifnames=0 biosdevname=0||g' /etc/default/grub
sudo update-grub

# 2. Remove the MAC-pin .link files.
sudo rm -f /etc/systemd/network/10-nova-ve-eth*.link

# 3. Disable the post-boot service and clear the marker.
sudo systemctl disable nova-ve-postboot.service
sudo rm /etc/nova-ve/bridge-cloud.state

# 4. Reboot to pick up the reverted kernel cmdline.
sudo reboot
```

## Backups

Every Phase C apply snapshots the pre-change netplan files to
`/etc/nova-ve/netplan.backup.<unix_ts>/` (mode 0700, root:root, symlink
refusal enforced by `nova-ve-backup.py`).  Backups are not pruned
automatically; rotate them out of band if disk space matters.

## Re-running the install script

The provision script is **always-on**: a re-run on a host with marker
`complete` logs `bridge-cloud: already migrated, skipping` and does
nothing destructive.  Set `NOVA_VE_SKIP_BRIDGE_CLOUD=1` to opt out of
the Bridge-Cloud phase entirely during a re-run.
