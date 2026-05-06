"""Tests for the Arista vEOS, Mikrotik CHR, and VyOS adapters (#189)."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from scripts.import_eveng.adapters import (
    AristaVEosAdapter,
    GenericLinuxAdapter,
    MikrotikCHRAdapter,
    VyOSAdapter,
    iter_adapters,
    select_adapter,
)
from scripts.import_eveng.adapters import vyos as vyos_module


@pytest.fixture(autouse=True)
def _isolated_vyos_pristine_hashes():
    """Each test gets a fresh pristine-hash registry."""
    vyos_module.clear_pristine_hashes()
    yield
    vyos_module.clear_pristine_hashes()


# ---- Arista vEOS -------------------------------------------------------


def test_arista_veos_matches_qcow2_and_vmdk() -> None:
    a = AristaVEosAdapter()
    assert a.match({"image": "vEOS-lab-4.21.0F.qcow2"}) is True
    assert a.match({"image": "vEOS-lab-4.21.0F.vmdk"}) is True
    assert a.match({"image": "vEOS-Lab-4.21.0F.QCOW2"}) is True  # case-insensitive


def test_arista_veos_rejects_other_vendor_images() -> None:
    a = AristaVEosAdapter()
    assert a.match({"image": "csr1000v-universalk9.qcow2"}) is False
    assert a.match({"image": "chr-7.10.img"}) is False
    assert a.match({"image": "vyos-1.4.qcow2"}) is False
    assert a.match({"image": "vmx-bundle-22.4.qcow2"}) is False


def test_arista_veos_convert_emits_aboot_cdrom_marker() -> None:
    a = AristaVEosAdapter()
    raw = {"image": "vEOS-lab-4.21.0F.qcow2", "ram": 2048}
    out = a.convert(raw, Path("."))
    assert out["vendor"] == "arista"
    assert out["kind"] == "qemu"
    assert out["extras"]["boot_cdrom"] == "aboot-veos.iso"
    assert out["extras"]["qemu_nic"] == "virtio-net-pci"
    assert out["console"] == "serial"


# ---- Mikrotik CHR -----------------------------------------------------


def test_mikrotik_chr_matches_img_and_qcow2() -> None:
    a = MikrotikCHRAdapter()
    assert a.match({"image": "chr-7.10.img"}) is True
    assert a.match({"image": "chr-7.10.qcow2"}) is True


def test_mikrotik_chr_rejects_other_vendor_images() -> None:
    a = MikrotikCHRAdapter()
    assert a.match({"image": "vEOS-lab-4.21.qcow2"}) is False
    assert a.match({"image": "vyos-1.4.qcow2"}) is False
    assert a.match({"image": "csr1000v-universalk9.qcow2"}) is False


def test_mikrotik_chr_forces_e1000_nic() -> None:
    """CHR is sensitive to NIC model — adapter forces e1000 even if raw says virtio."""
    a = MikrotikCHRAdapter()
    raw = {"image": "chr-7.10.img", "ram": 256, "qemu_nic": "virtio-net-pci"}
    out = a.convert(raw, Path("."))
    assert out["vendor"] == "mikrotik"
    assert out["extras"]["qemu_nic"] == "e1000"  # forced; raw's virtio request ignored


# ---- VyOS -------------------------------------------------------------


def test_vyos_matches_qcow2_img_iso() -> None:
    a = VyOSAdapter()
    assert a.match({"image": "vyos-1.4.qcow2"}) is True
    assert a.match({"image": "vyos-rolling.img"}) is True
    assert a.match({"image": "vyos-1.5.iso"}) is True


def test_vyos_rejects_other_vendor_images() -> None:
    a = VyOSAdapter()
    assert a.match({"image": "vEOS-lab-4.21.qcow2"}) is False
    assert a.match({"image": "chr-7.10.img"}) is False


def test_vyos_unknown_status_when_no_template_path(tmp_path: Path) -> None:
    """Without a vyos_template_path, the adapter cannot classify; emits 'unknown'."""
    a = VyOSAdapter()
    out = a.convert({"image": "vyos-1.4.qcow2", "ram": 512}, tmp_path)
    assert out["extras"]["vyos_status"] == "unknown"


def test_vyos_pristine_template_detected_via_known_hash(tmp_path: Path) -> None:
    """Body sha256 in pristine list -> vyos_status='pristine'."""
    body = b"# Pristine VyOS template\nset interfaces ethernet eth0\n"
    body_path = tmp_path / "vyos.tpl"
    body_path.write_bytes(body)
    expected_hash = hashlib.sha256(body).hexdigest()
    vyos_module.register_pristine_hash(expected_hash)

    a = VyOSAdapter()
    out = a.convert(
        {"image": "vyos-1.4.qcow2", "ram": 512, "vyos_template_path": str(body_path)},
        tmp_path,
    )
    assert out["extras"]["vyos_status"] == "pristine"


def test_vyos_customised_template_detected_via_hash_mismatch(tmp_path: Path) -> None:
    """A whitespace edit changes the hash -> vyos_status='customised'."""
    pristine_body = b"# Pristine VyOS template\nset interfaces ethernet eth0\n"
    customised_body = pristine_body + b"# operator hand-edit\n"

    pristine_hash = hashlib.sha256(pristine_body).hexdigest()
    vyos_module.register_pristine_hash(pristine_hash)

    body_path = tmp_path / "vyos.tpl"
    body_path.write_bytes(customised_body)

    a = VyOSAdapter()
    out = a.convert(
        {"image": "vyos-1.4.qcow2", "ram": 512, "vyos_template_path": str(body_path)},
        tmp_path,
    )
    assert out["extras"]["vyos_status"] == "customised"


def test_vyos_marker_check_is_content_aware_not_substring() -> None:
    """Refinement N5 / Architect: marker check is sha256 of body, NOT substring match.

    A customised template that happens to contain the literal word 'pristine' or any
    other token is still classified by hash, not by string presence.
    """
    a_body = b"set marker pristine here\n"  # contains the word 'pristine'
    a_hash = hashlib.sha256(a_body).hexdigest()
    # We do NOT register a_hash; the body should be 'customised' despite
    # containing the word 'pristine'.

    p = Path("/tmp/_vyos_marker_check_substring.tpl")
    try:
        p.write_bytes(a_body)
        assert vyos_module.vyos_status_for_path(p) == "customised"
    finally:
        p.unlink(missing_ok=True)


# ---- registry integration --------------------------------------------


def test_all_three_vendor_adapters_register_before_generic_linux() -> None:
    names = [a.name for a in iter_adapters()]
    expected = {"arista_veos", "mikrotik_chr", "vyos"}
    assert expected <= set(names)
    # generic_linux remains structurally last regardless of vendor adapters added.
    assert names[-1] == "generic_linux"
    for vendor_name in expected:
        assert names.index(vendor_name) < names.index("generic_linux")


def test_select_adapter_routes_veos_to_arista_not_generic() -> None:
    matched = select_adapter({"image": "vEOS-lab-4.21.qcow2"})
    assert matched is not None and matched.name == "arista_veos"


def test_select_adapter_routes_chr_to_mikrotik() -> None:
    matched = select_adapter({"image": "chr-7.10.img"})
    assert matched is not None and matched.name == "mikrotik_chr"


def test_select_adapter_routes_vyos_image() -> None:
    matched = select_adapter({"image": "vyos-1.4.qcow2"})
    assert matched is not None and matched.name == "vyos"


def test_generic_linux_catches_completely_unknown_image() -> None:
    matched = select_adapter({"image": "exotic-vendor-x.qcow2"})
    assert matched is not None and matched.name == "generic_linux"
