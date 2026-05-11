# System requirements

The `install.sh` provisioner enforces a strict environment. The script hard-stops if either of the first two requirements is not met.

## Hard requirements (enforced by `install.sh`)

| Requirement | Detail |
|---|---|
| Operating system | Ubuntu 26.04 LTS (the script reads `/etc/os-release` and aborts otherwise) |
| Architecture | `x86_64` (the script reads `uname -m` and aborts otherwise) |
| Privilege | root (run via `sudo bash` — the script must drop systemd units and create `/var/lib/nova-ve`) |

ARM hosts, Debian, and other Ubuntu LTS versions are not supported by the one-liner. You can still run nova-ve in development by skipping the provisioner and standing up the backend / frontend / Postgres / Guacamole manually, but that path is not documented yet.

## Recommended host sizing

These are guidelines for a lab host that runs ~10 QEMU/KVM-based VMs concurrently:

| Resource | Recommended | Notes |
|---|---|---|
| CPU | 8 cores with VT-x/AMD-V enabled in BIOS | KVM acceleration is required for QEMU nodes; without it, nodes fall back to TCG and run very slowly. Run `kvm-ok` to verify. |
| RAM | 16 GB minimum | Per-node RAM is template-defined; vQFX, vMX, and CSR1000v often need 4 GB+ each. |
| Storage | 100 GB SSD | Vendor images (`/var/lib/nova-ve/images/`) and lab disk overlays sit here. EVE-NG imports add another 20–40 GB depending on what you bring over. |
| Network | One usable NIC | Outbound internet during install for package + Docker image pulls. Inbound 80/443 if you want to access the UI remotely. |

If you are only running Docker-based nodes (Alpine, Linux containers, lightweight network functions), you can comfortably trim the recommended RAM to 4 GB and skip the BIOS virtualization toggle.

## Software the installer brings in

You do not need to pre-install anything beyond a clean Ubuntu 26.04 image. `install.sh` (via `deploy/scripts/provision-ubuntu-2604.sh`) provisions:

- `git`, `curl`, `jq`, `rsync`, OpenSSL build tooling.
- Docker engine + compose plugin.
- PostgreSQL server (host package, not containerized — listens on `127.0.0.1` only).
- Caddy as the reverse proxy.
- Node.js 22.x via NodeSource.
- Python 3 with venv + build deps.
- QEMU/KVM userspace + bridge utilities (pulled in transitively by the privileged network helper).

After the run, `nova-ve-install-summary.md` lists every config file, env var, and systemd unit the installer created.
