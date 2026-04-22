# FY Lab Validation Runbook

This runbook is for real x86_64 validation only. The local macOS ARM workstation is not a valid live-test target.

Credentials and host-specific secrets must stay external:

- Obsidian FY Lab notes for host inventory and network context
- `~/.secrets/credentials.yml` for credentials

## Pass A: Standard ESXi VM

Goal: prove control-plane install, reachability, and reboot persistence on Ubuntu 26.04 x86_64.

### VM Shape

- Hypervisor: FY Lab ESXi
- Guest OS: Ubuntu 26.04 x86_64 from `ubuntu-26.04-beta-live-server-amd64.iso`
- Nested virtualization: disabled
- Network: normal management network with outbound package access
- Disk: enough capacity for repo checkout, PostgreSQL, and `/var/lib/nova-ve`

### Procedure

1. Verify ISO checksum before attaching the media.
2. Install Ubuntu 26.04 x86_64.
3. Clone the repo onto the guest, preferably at `/opt/nova-ve`.
4. Run `sudo deploy/scripts/provision-ubuntu-2604.sh`.
5. Run `sudo deploy/scripts/smoke-check.sh`.
6. Reboot the VM.
7. Re-run `sudo deploy/scripts/smoke-check.sh`.

### Evidence To Capture

- VM name, ESXi host, CPU/RAM/disk shape
- ISO filename and checksum
- provisioning log
- the bootstrap admin credential location (`/etc/nova-ve/backend.env`), without copying its contents into notes or logs
- `systemctl status postgresql caddy nova-ve-backend --no-pager`
- `curl http://127.0.0.1/api/health`
- `curl -I http://127.0.0.1/`

## Pass B: Nested-Virt ESXi VM

Goal: prove whether nested virtualization exposes `/dev/kvm` cleanly enough for the next runtime-validation lane.

### VM Shape

- Hypervisor: FY Lab ESXi
- Guest OS: Ubuntu 26.04 x86_64
- Nested virtualization: enabled in the VM CPU configuration
- Network and disk: same baseline as Pass A

### Procedure

1. Start from the Pass A-proven install lane.
2. Record the exact ESXi nested virtualization settings used.
3. Confirm virtualization support inside the guest.
4. Confirm `/dev/kvm` presence and permissions.
5. Capture the result of the first QEMU-focused validation command once that runtime-validation step is ready.

### Evidence To Capture

- ESXi nested virtualization setting screenshots or text notes
- `egrep -c '(vmx|svm)' /proc/cpuinfo`
- `ls -l /dev/kvm`
- any QEMU runtime logs or failure diagnostics

## Proxmox Fallback Appendix

Proxmox is not a primary target in this phase. Only use it if ESXi is unavailable.

If Proxmox is used, add the following to the validation record:

- equivalent CPU virtualization settings
- storage/network differences from ESXi
- any deviations in provisioning or smoke-check behavior
- the same evidence set captured for Pass A or Pass B
