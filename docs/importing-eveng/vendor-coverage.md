# Vendor adapter coverage

After image copy, the importer attempts to emit a nova-ve template per priority-vendor image. Adapters live under `backend/scripts/import_eveng/adapters/`. The current supported set:

| Vendor | Image kind | Adapter |
|---|---|---|
| Arista vEOS | QEMU | `arista_veos.py` |
| Cisco CSR1000v | QEMU | `cisco_csr1000v.py` |
| Cisco IOL | IOL | `cisco_iol.py` |
| Cisco IOSv L2 | QEMU | `cisco_iosv_l2.py` |
| Cisco IOSv L3 | QEMU | `cisco_iosv_l3.py` |
| Generic Linux | QEMU | `generic_linux.py` (catch-all for unmatched qemu images) |
| Juniper vMX | QEMU (paired VM) | `juniper_vmx.py` |
| Juniper vQFX | QEMU (paired VM) | `juniper_vqfx.py` |
| Juniper vSRX | QEMU | `juniper_vsrx.py` |
| MikroTik CHR | QEMU | `mikrotik_chr.py` |
| VyOS | QEMU | `vyos.py` |

Anything outside this set still has its image copied — only the *template* layer is skipped. The image is available; you can hand-author a template JSON under `/var/lib/nova-ve/templates/<vendor>/<key>.json` and the node modal will pick it up.

## Paired-VM templates

Juniper vMX and vQFX ship as **paired VMs** — a routing-engine (RE) image and a packet-forwarding (PFE) image that must be launched together with a private bridge between them. The dedicated adapters emit a multi-node template that the "Add node" picker presents as a single entry; instantiating it produces the RE + PFE pair correctly wired up. See [#188](https://github.com/fahadysf/nova-ve/issues/188) for the paired-template flow.

## Adding a new adapter

Each adapter inherits from `adapters/base.py` and implements `convert(eveng_template) -> NovaTemplate`. To add a new vendor:

1. Drop a Python file at `backend/scripts/import_eveng/adapters/<vendor>.py`.
2. Implement `convert()` — return a `NovaTemplate` matching the result schema in `template_schema.py`.
3. Register the adapter in `adapters/__init__.py`.
4. Add a unit test under `backend/tests/import_eveng/` exercising a representative EVE-NG fixture.
5. Re-run the importer on a host that has the EVE-NG source tree.

See [Development → Backend](../development/backend.md) for the broader Python module layout.
