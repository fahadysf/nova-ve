# Dynamips nodes (Cisco IOS routers)

Dynamips emulates Cisco router hardware by running an unpacked IOS `.bin`
image under a MIPS/PowerPC instruction translator. nova-ve Phase 1 supports
two platforms:

- **c3725** — 2 NM slots (slot 0 = built-in `GT96100-FE` 2-port
  FastEthernet, slot 1 user-configurable). Lightweight; IOS 12.4 fits.
- **c7200** — 7 PA slots, configurable NPE / midplane. Beefier; more
  interface types.

Additional platforms (c1700 / c2600 / c2691 / c3620 / c3640 / c3745) are
recognised by the EVE-NG importer but currently flagged
`needs-manual-review` — the runtime adapter for them lands in a
follow-up.

!!! warning "You supply your own IOS images"
    nova-ve does **not** ship Cisco IOS binaries. You must legally
    obtain unpacked `.bin` images for the platforms you want to run and
    place them under `/var/lib/nova-ve/images/dynamips/`. The standard
    file-naming convention (`c3725-adventerprisek9-mz.124-15.T14.bin`)
    is what the importer matches on.

## Where images live

```
/var/lib/nova-ve/images/dynamips/
├── c3725-adventerprisek9-mz.124-15.T14.bin
└── c7200-adventerprisek9-mz.124-24.T5.bin
```

If you imported an EVE-NG host with `import-eveng-templates`, images are
copied automatically from `/opt/unetlab/addons/dynamips/`. See
[Importing from EVE-NG](../importing-eveng/index.md).

## Idle-PC — why it matters

Without a correct idle-PC value, a single Dynamips instance pegs one CPU
core at 100 % permanently. The value is image-specific (per IOS build),
not platform-specific. nova-ve resolves it in this order:

1. **Template override.** If the template (or per-node extras) sets
   `idlepc`, it wins unconditionally.
2. **Per-image cache.** nova-ve maintains a SHA-256-keyed cache at
   `/var/lib/nova-ve/runtime/idle_pc_cache.json`. Once a value has been
   determined for an image, every subsequent node using that image reads
   it from the cache.
3. **No value available — fail fast.** A Dynamips node with no idle-PC
   refuses to start. The runtime surfaces a clear error pointing here
   rather than silently boot-looping at 100 % CPU.

To populate the cache for an image you imported from EVE-NG: the EVE-NG
template usually carries the idle-PC in its YAML; the importer's
vendor adapter copies that value across into the generated nova-ve
template. For images you supplied yourself, set `idlepc` on the
template JSON before instantiation.

## Restart required on link changes

Dynamips supports online insertion/removal in theory, but the behaviour
depends on the specific IOS image and port-adapter combination — a wrong
guess silently fails (the topology updates but the node does not see
the change). Phase 1 deliberately keeps this off:

> Adding, removing, or rewiring a link on a Dynamips node requires
> stopping the node and starting it again.

The canvas surfaces this as a "restart required" indicator on the
affected node after a link change. The same rule applies to `iol`
nodes today and is intentional — see issue #180 for the broader
hot-attach roadmap.

## Authoring a template by hand

When you have an image but no EVE-NG template to import, drop a JSON
file at `/var/lib/nova-ve/templates/dynamips/<your-key>.json`:

```json
{
  "schema": 1,
  "type": "dynamips",
  "kind": "dynamips",
  "id": "my-c3725",
  "name": "Cisco c3725 (IOS 12.4)",
  "vendor": "cisco",
  "image": "c3725-adventerprisek9-mz.124-15.T14.bin",
  "cpu": 1,
  "ram": 256,
  "ethernet": 2,
  "console": "telnet",
  "extras": {
    "platform": "c3725",
    "slot0": "GT96100-FE",
    "slot1": "NM-16ESW",
    "nvram": 128,
    "idlepc": "0x60c09320"
  }
}
```

The frontend's node-edit modal will let you override `idlepc`,
`platform`, NVRAM, and slot bindings per node.

## Troubleshooting

| Symptom | Where to look |
|---|---|
| Node refuses to start with `no idle-PC value...` | Add `idlepc` to the template, or set it under the node's extras |
| One Dynamips node pegs a CPU core | Idle-PC is set but wrong for this IOS build — extract it once via the Dynamips hypervisor `vm extract_idle_pc` command and update the template |
| `dynamips` binary not found | `apt-get install dynamips` (Ubuntu 26.04 universe). The installer adds this automatically; reinstall the host package if you removed it manually |
| Imported EVE-NG c2691 / c2600 template shows `needs-manual-review` | Only c3725 and c7200 are runtime-supported in Phase 1; other chassis are matched but disabled until their runtime path lands |

## Phase 1 limitations recap

- **Platforms:** c3725 + c7200 only.
- **Hot-attach:** not supported. Link changes require node restart.
- **Calibration CLI:** the planned `nova-ve dynamips calibrate <image>`
  CLI subcommand is Phase 1B. For now: rely on EVE-NG-imported idle-PCs
  or set them manually.

## What's next

Phase 2 adds IOL (Cisco "IOS on Linux" binaries — Cisco-internal
binaries that you must supply along with a valid `iourc` license file).
Phase 3 aliases the `iou` node type onto the same IOL backend. See
the project plan for details.
