"""Shared fixture builders for EVE-NG importer e2e tests (#190).

Provides ``synthetic_eveng_tree`` — builds a complete ``/opt/unetlab/addons/``
tree under ``tmp_path`` populated with a minimal entry per priority vendor
(Cisco IOSv / Juniper vSRX / Arista vEOS / generic Linux). Bytes are
deterministic short payloads; the synthetic-stub policy in
``fixtures/README.md`` applies.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest


@dataclass
class SyntheticEvengTree:
    """A complete fake /opt/unetlab/addons tree on disk."""

    source_root: Path
    """The directory the importer should be pointed at (``--source``)."""

    addons: Path
    """The ``addons`` subdirectory."""


@pytest.fixture()
def synthetic_eveng_tree(tmp_path: Path) -> SyntheticEvengTree:
    """Create a minimal /opt/unetlab/addons tree with one entry per priority vendor.

    Layout (synthetic stubs, not real vendor images):

        addons/qemu/cisco-iosv-l3/vios-adventerprise-15.6.qcow2
        addons/qemu/cisco-iosv-l3/cdrom.iso
        addons/qemu/juniper-vsrx/media-vsrx-vmdisk-15.1X49.qcow2
        addons/qemu/arista-veos/vEOS-lab-4.21.0F.qcow2
        addons/qemu/arista-veos/aboot-veos.iso
        addons/qemu/generic-ubuntu/ubuntu-22.04-cloudimg-amd64.img
        addons/dynamips/c7200/c7200-image.image
        addons/iol/bin/i86bi-linux-l3-15.5.bin
        addons/iol/bin/iourc

    Boot disks for qemu directories are picked per the precedence rules in
    walker.py (cdrom.iso > virtioa.qcow2 > hda.qcow2 > first *.qcow2 lex).
    """
    source_root = tmp_path / "opt-unetlab"
    addons = source_root / "addons"

    # qemu / cisco IOSv L3 (with cdrom.iso to exercise the boot-disk-precedence path).
    cisco_dir = addons / "qemu" / "cisco-iosv-l3"
    cisco_dir.mkdir(parents=True)
    (cisco_dir / "vios-adventerprise-15.6.qcow2").write_bytes(b"synthetic-iosv-l3" * 16)
    (cisco_dir / "cdrom.iso").write_bytes(b"synthetic-iosv-cdrom" * 16)

    # qemu / juniper vSRX
    vsrx_dir = addons / "qemu" / "juniper-vsrx"
    vsrx_dir.mkdir(parents=True)
    (vsrx_dir / "media-vsrx-vmdisk-15.1X49.qcow2").write_bytes(b"synthetic-vsrx" * 16)

    # qemu / arista vEOS (two-disk Aboot+vEOS)
    veos_dir = addons / "qemu" / "arista-veos"
    veos_dir.mkdir(parents=True)
    (veos_dir / "vEOS-lab-4.21.0F.qcow2").write_bytes(b"synthetic-veos" * 16)
    (veos_dir / "aboot-veos.iso").write_bytes(b"synthetic-aboot" * 16)

    # qemu / generic ubuntu (catches generic_linux fallback)
    ubuntu_dir = addons / "qemu" / "generic-ubuntu"
    ubuntu_dir.mkdir(parents=True)
    (ubuntu_dir / "ubuntu-22.04-cloudimg-amd64.img").write_bytes(b"synthetic-ubuntu" * 16)

    # dynamips
    dyn_dir = addons / "dynamips"
    dyn_dir.mkdir(parents=True)
    (dyn_dir / "c7200-image.image").write_bytes(b"synthetic-c7200" * 16)

    # iol (with iourc license)
    iol_dir = addons / "iol" / "bin"
    iol_dir.mkdir(parents=True)
    (iol_dir / "i86bi-linux-l3-15.5.bin").write_bytes(b"synthetic-iol-bin" * 16)
    (iol_dir / "iourc").write_bytes(b"synthetic-iourc-license")

    return SyntheticEvengTree(source_root=source_root, addons=addons)
