"""Tests for the Cisco adapters (#187)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from scripts.import_eveng.adapters import (
    CiscoCSR1000vAdapter,
    CiscoIOLAdapter,
    CiscoIOSvL2Adapter,
    CiscoIOSvL3Adapter,
    iter_adapters,
    select_adapter,
)


# ---- IOSv L3 ------------------------------------------------------------


def test_cisco_iosv_l3_matches_canonical_filename() -> None:
    a = CiscoIOSvL3Adapter()
    assert a.match({"image": "vios-adventerprise-15.6.2t.qcow2"}) is True
    assert a.match({"image": "vios-adventerprisek9-m.156-2.T.qcow2"}) is True


def test_cisco_iosv_l3_rejects_other_vendor_images() -> None:
    a = CiscoIOSvL3Adapter()
    assert a.match({"image": "vios_l2-adventerprisek9-m.qcow2"}) is False
    assert a.match({"image": "csr1000v-universalk9.16.09.qcow2"}) is False
    assert a.match({"image": "i86bi-linux-l3-15.5.bin"}) is False
    assert a.match({"image": "vEOS-lab-4.21.0F.qcow2"}) is False


def test_cisco_iosv_l3_convert_snapshot() -> None:
    a = CiscoIOSvL3Adapter()
    raw: dict[str, Any] = {
        "image": "vios-adventerprise-15.6.qcow2",
        "ram": 512,
        "console_type": "telnet",
    }
    out = a.convert(raw, Path("/var/lib/nova-ve/images/qemu/cisco-iosv-l3"))
    assert out == {
        "schema": 1,
        "id": "vios-adventerprise-15.6.qcow2",
        "name": "Cisco IOSv L3",
        "vendor": "cisco",
        "kind": "qemu",
        "image": "vios-adventerprise-15.6.qcow2",
        "cpu": 1,
        "ram": 512,
        "ethernet": 8,
        "console": "telnet",
        "extras": {
            "qemu_nic": "e1000",
            "qemu_options": "",
            "_eveng_raw": raw,
        },
    }


# ---- IOSv L2 ------------------------------------------------------------


def test_cisco_iosv_l2_matches_canonical_filename() -> None:
    a = CiscoIOSvL2Adapter()
    assert a.match({"image": "vios_l2-adventerprisek9-m.SSA.high_iron.qcow2"}) is True


def test_cisco_iosv_l2_rejects_l3() -> None:
    a = CiscoIOSvL2Adapter()
    assert a.match({"image": "vios-adventerprise-15.6.qcow2"}) is False


def test_cisco_iosv_l2_convert_snapshot() -> None:
    a = CiscoIOSvL2Adapter()
    raw = {"image": "vios_l2-adventerprisek9.qcow2", "ram": 512, "console_type": "telnet"}
    out = a.convert(raw, Path("."))
    assert out["vendor"] == "cisco"
    assert out["kind"] == "qemu"
    assert out["ethernet"] == 4
    assert out["extras"]["qemu_nic"] == "e1000"
    assert out["console"] == "telnet"


# ---- CSR1000v / Cat8000v -----------------------------------------------


def test_cisco_csr1000v_matches_both_filenames() -> None:
    """CSR1000v adapter claims both csr1000v-universalk9 AND cat8kv-universalk9."""
    a = CiscoCSR1000vAdapter()
    assert a.match({"image": "csr1000v-universalk9.16.09.04.qcow2"}) is True
    assert a.match({"image": "cat8kv-universalk9.17.10.01a.qcow2"}) is True


def test_cisco_csr1000v_rejects_other_cisco_images() -> None:
    a = CiscoCSR1000vAdapter()
    assert a.match({"image": "vios-adventerprise-15.6.qcow2"}) is False
    assert a.match({"image": "vios_l2-adventerprisek9.qcow2"}) is False
    assert a.match({"image": "i86bi-linux-l3.bin"}) is False


def test_cisco_csr1000v_convert_csr_name() -> None:
    a = CiscoCSR1000vAdapter()
    raw = {"image": "csr1000v-universalk9.16.09.qcow2", "ram": 4096}
    out = a.convert(raw, Path("."))
    assert out["name"] == "Cisco CSR1000v"
    assert out["extras"]["qemu_nic"] == "virtio-net-pci"


def test_cisco_csr1000v_convert_cat8k_name() -> None:
    """cat8kv images get a different default name even though the adapter is shared."""
    a = CiscoCSR1000vAdapter()
    raw = {"image": "cat8kv-universalk9.17.10.qcow2", "ram": 4096}
    out = a.convert(raw, Path("."))
    assert out["name"] == "Cisco Cat8000v"


# ---- IOL ----------------------------------------------------------------


def test_cisco_iol_matches_canonical_filename() -> None:
    a = CiscoIOLAdapter()
    assert a.match({"image": "i86bi-linux-l3-adventerprisek9-15.5.2T.bin"}) is True
    assert a.match({"image": "i86bi-linux-l2-15.7.bin"}) is True


def test_cisco_iol_rejects_qemu_images() -> None:
    a = CiscoIOLAdapter()
    assert a.match({"image": "vios-adventerprise-15.6.qcow2"}) is False
    assert a.match({"image": "csr1000v-universalk9.qcow2"}) is False


def test_cisco_iol_emits_iol_license_path() -> None:
    """IOL adapter must emit extras.iol_license_path; per #187 acceptance criterion 3."""
    a = CiscoIOLAdapter()
    raw = {"image": "i86bi-linux-l3-15.5.bin", "ram": 1024}
    out = a.convert(raw, Path("."))
    assert out["kind"] == "iol"
    assert out["extras"]["iol_license_path"] == "${IMAGES_DIR}/iol/i86bi-linux-l3-15.5/iourc"


# ---- match() tightness across all four adapters -----------------------


def _all_adapters():
    return [
        CiscoIOSvL3Adapter(),
        CiscoIOSvL2Adapter(),
        CiscoCSR1000vAdapter(),
        CiscoIOLAdapter(),
    ]


@pytest.mark.parametrize(
    "image",
    [
        "vios-adventerprise-15.6.qcow2",  # iosv_l3
        "vios_l2-adventerprisek9.qcow2",  # iosv_l2
        "csr1000v-universalk9.16.09.qcow2",  # csr1000v
        "cat8kv-universalk9.17.10.qcow2",  # csr1000v
        "i86bi-linux-l3-15.5.bin",  # iol
    ],
)
def test_exactly_one_cisco_adapter_matches_per_image(image: str) -> None:
    """No two Cisco adapters claim the same image — false-positive bound check."""
    matchers = [a for a in _all_adapters() if a.match({"image": image})]
    assert len(matchers) == 1, f"image {image} matched {[a.name for a in matchers]}"


@pytest.mark.parametrize(
    "image",
    [
        "vEOS-lab-4.21.0F.qcow2",  # arista (out of scope here)
        "chr-7.10.img",  # mikrotik
        "vmx-bundle-22.4R1.S1.qcow2",  # juniper
        "media-vsrx-vmdisk-15.1X49.qcow2",  # juniper
        "ubuntu-22.04-cloudimg-amd64.img",  # generic linux
    ],
)
def test_no_cisco_adapter_claims_other_vendor_images(image: str) -> None:
    """Cisco adapters must not match Juniper / Arista / Mikrotik / generic images."""
    for a in _all_adapters():
        assert a.match({"image": image}) is False, f"{a.name} false-matched {image}"


# ---- registry integration ---------------------------------------------


def test_all_four_cisco_adapters_register_before_generic_linux() -> None:
    """The four Cisco adapters must dispatch before generic_linux."""
    names = [a.name for a in iter_adapters()]
    cisco_names = {"cisco_iosv_l3", "cisco_iosv_l2", "cisco_csr1000v", "cisco_iol"}
    assert cisco_names <= set(names)
    # generic_linux must remain the last entry.
    assert names[-1] == "generic_linux"
    # Every Cisco adapter must precede generic_linux.
    cisco_indices = [i for i, n in enumerate(names) if n in cisco_names]
    assert max(cisco_indices) < names.index("generic_linux")


def test_select_adapter_routes_iosv_l3_to_cisco_not_generic() -> None:
    """A real IOSv L3 image must dispatch to cisco_iosv_l3, not the catch-all."""
    matched = select_adapter({"image": "vios-adventerprise-15.6.qcow2"})
    assert matched is not None and matched.name == "cisco_iosv_l3"


def test_select_adapter_routes_iol_to_cisco_iol() -> None:
    matched = select_adapter({"image": "i86bi-linux-l3-15.5.bin"})
    assert matched is not None and matched.name == "cisco_iol"


def test_select_adapter_falls_through_to_generic_for_unknown_image() -> None:
    """An image no Cisco adapter recognises must fall through to generic_linux."""
    matched = select_adapter({"image": "ubuntu-22.04-cloudimg-amd64.img"})
    assert matched is not None and matched.name == "generic_linux"
