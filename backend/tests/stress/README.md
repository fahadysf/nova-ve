# Stress tests (`backend/tests/stress/`)

These tests are **gated behind `RUN_STRESS_TESTS=1`** and require a privileged
Linux host with KVM. They are **skipped in PR CI** and are **required green
before any Wave 6 / Wave 7 release**.

## Why gated

The stress suite drives the real kernel, real Docker daemon (in privileged
mode), and real QEMU/KVM. It cannot run on:

- macOS / Windows hosts (no Linux network namespaces, no KVM).
- Unprivileged Docker (no `--network=none`, no veth manipulation).
- VMs without nested virtualisation enabled (no `/dev/kvm`).

PR CI runners satisfy none of those. Running the suite there would be either
slow (TCG fallback) or impossible (Docker daemon unreachable). Instead, the
tests skip with a precise reason string and the suite must be run by hand on
a privileged host before each release-tagged commit.

## How to run

On a privileged Linux host with KVM:

```bash
# 1. Bring the toolchain up to spec.
sudo apt-get install iproute2 bridge-utils qemu-system-x86 qemu-utils
sudo systemctl enable --now docker
sudo modprobe kvm_intel  # or kvm_amd

# 2. Sanity check (all four must succeed).
docker info
ip -V
bridge -V
[ -c /dev/kvm ] && echo "KVM OK"

# 3. Pre-pull the real images referenced by the test.
docker pull busybox
# Place vyos-1.4 disk image under $LABS_IMAGES_DIR (or override via
# STRESS_VYOS_IMAGE=<image-name>).

# 4. Run the suite.
cd backend
RUN_STRESS_TESTS=1 python -m pytest tests/stress/ -v
```

## What `test_link_thrash.py` exercises

100-iteration tight loop of:

```
POST   /api/labs/<lab>/links            # create link to running endpoint
sleep  50ms
DELETE /api/labs/<lab>/links/<link_id>  # delete the link
sleep  50ms
```

…run twice in series:

1. **Container path** — link to a `--network=none` busybox container started
   via `NodeRuntimeService.start_node` (US-203 architecture).
2. **QEMU path** — link to a running vyos VM started via
   `NodeRuntimeService.start_node` (US-302 / US-303 architecture).

End-of-loop assertions (all of, in a single `finally` block):

- `links[]` is empty.
- `bridge link show master <bridge>` reports no `nve…d…i…` entries for
  either test node.
- `ip link show` reports no veth/TAP entries for either test node.
- QMP `query-pci` (via `scripts/qmp_query.py`) reports no `dev{iface}` for
  the QEMU node.
- No leaked TAP/veth across the entire iteration loop (covered by the two
  scans above).
- `network.runtime.used_ips` is back to its initial baseline (typically
  `[]`) — IPAM leak detection per Critic iter-2 gap-analysis.

Failure of any iteration still triggers the cleanup path:

- The runtime container (busybox) is killed.
- The QEMU VM is stopped via `NodeRuntimeService.stop_node`.
- `host_net.sweep_lab_host_ifaces` removes any straggler host ifaces.
- The test bridge is deleted via `host_net.try_bridge_del`.

## Environment overrides

| Var                    | Default                          | Purpose                          |
|------------------------|----------------------------------|----------------------------------|
| `RUN_STRESS_TESTS`     | (unset → skip)                   | Master gate.                     |
| `KVM_AVAILABLE`        | `1`                              | Set to `0` to force-skip QEMU.   |
| `STRESS_BUSYBOX_IMAGE` | `busybox`                        | Docker image for container path. |
| `STRESS_VYOS_IMAGE`    | `vyos-1.4`                       | Disk image for QEMU path.        |
| `DOCKER_HOST`          | `unix:///var/run/docker.sock`    | Docker daemon socket.            |

## Release-gating policy

Per `.omc/plans/network-runtime-wiring.md`:

> Skipped in PR CI; required green before any Wave 6/7 release.

Before tagging a Wave 6 or Wave 7 release, run the suite end-to-end and
attach the JUnit transcript to the release notes. Any non-skipped failure
blocks the release.
