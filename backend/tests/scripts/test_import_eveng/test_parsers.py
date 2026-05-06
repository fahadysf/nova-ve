"""Tests for the EVE-NG template parsers (#186)."""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.import_eveng.parsers import ParserError, parse_php, parse_wrapper, parse_yaml


# ---- yaml_parser -------------------------------------------------------


def test_yaml_parser_returns_normalised_dict(tmp_path: Path) -> None:
    p = tmp_path / "vyos.yml"
    p.write_text(
        """\
name: VyOS 1.4
type: qemu
cpu: 2
ram: 1024
ethernet: 4
console: telnet
qemu_nic: virtio-net-pci
"""
    )
    raw = parse_yaml(p)
    assert raw["name"] == "VyOS 1.4"
    assert raw["type"] == "qemu"
    assert raw["cpu"] == 2
    assert raw["ram"] == 1024
    assert raw["ethernet"] == 4
    assert raw["console"] == "telnet"
    assert raw["qemu_nic"] == "virtio-net-pci"
    assert "_eveng_raw" in raw
    assert raw["_eveng_raw"]["yaml"]["name"] == "VyOS 1.4"


def test_yaml_parser_empty_file_yields_empty_raw(tmp_path: Path) -> None:
    p = tmp_path / "empty.yml"
    p.write_text("")
    raw = parse_yaml(p)
    assert raw == {"_eveng_raw": {"yaml": {}}}


def test_yaml_parser_rejects_non_mapping_root(tmp_path: Path) -> None:
    p = tmp_path / "bad.yml"
    p.write_text("- 1\n- 2\n")
    with pytest.raises(ParserError):
        parse_yaml(p)


def test_yaml_parser_raises_on_malformed_yaml(tmp_path: Path) -> None:
    p = tmp_path / "broken.yml"
    p.write_text("key: [unbalanced\n")
    with pytest.raises(ParserError):
        parse_yaml(p)


# ---- php_parser --------------------------------------------------------


def test_php_parser_extracts_named_fields(tmp_path: Path) -> None:
    p = tmp_path / "vyos.php"
    p.write_text(
        """<?php
$name = "VyOS";
$type = "qemu";
$cpu = 2;
$ram = 1024;
$ethernet = 4;
$qemu_arch = "x86_64";
$qemu_nic = "virtio-net-pci";
$qemu_options = "-nographic -enable-kvm";
$icon = "Router.png";
$console = "telnet";
?>
"""
    )
    raw = parse_php(p)
    assert raw["name"] == "VyOS"
    assert raw["type"] == "qemu"
    assert raw["cpu"] == 2
    assert raw["ram"] == 1024
    assert raw["ethernet"] == 4
    assert raw["qemu_arch"] == "x86_64"
    assert raw["qemu_nic"] == "virtio-net-pci"
    assert raw["qemu_options"] == "-nographic -enable-kvm"
    assert raw["icon"] == "Router.png"
    assert raw["console"] == "telnet"
    assert "<?php" in raw["_eveng_raw"]["php_residual"]


def test_php_parser_omits_missing_fields_without_raising(tmp_path: Path) -> None:
    """Per the per-field fail-graceful contract: missing fields are silently omitted."""
    p = tmp_path / "minimal.php"
    p.write_text('<?php $name = "Minimal"; $cpu = 1; ?>\n')
    raw = parse_php(p)
    assert raw["name"] == "Minimal"
    assert raw["cpu"] == 1
    assert "ram" not in raw
    assert "qemu_options" not in raw
    assert "console" not in raw


def test_php_parser_omits_malformed_int_field(tmp_path: Path) -> None:
    """A non-numeric $cpu is silently omitted (does not match the int regex)."""
    p = tmp_path / "bad-int.php"
    p.write_text('<?php $name = "Bad"; $cpu = "two"; ?>\n')
    raw = parse_php(p)
    assert raw["name"] == "Bad"
    assert "cpu" not in raw  # the int pattern requires \d+, "two" does not match


def test_php_parser_handles_single_quoted_strings(tmp_path: Path) -> None:
    p = tmp_path / "single.php"
    p.write_text("<?php $name = 'Single'; $type = 'qemu'; ?>\n")
    raw = parse_php(p)
    assert raw["name"] == "Single"
    assert raw["type"] == "qemu"


def test_php_parser_residual_preserves_unrecognised_content(tmp_path: Path) -> None:
    """Unknown PHP code (functions, conditionals, etc.) is kept in the residual."""
    p = tmp_path / "with-extras.php"
    body = """<?php
$name = "X";
function some_helper($x) { return $x + 1; }
if (file_exists('/etc/foo')) { /* arbitrary PHP */ }
?>"""
    p.write_text(body)
    raw = parse_php(p)
    assert raw["name"] == "X"
    assert raw["_eveng_raw"]["php_residual"] == body


# ---- wrapper_parser ----------------------------------------------------


def test_wrapper_parser_extracts_qemu_flags(tmp_path: Path) -> None:
    """Bash-style EVE-NG wrappers (the more common shape) tokenise cleanly via shlex."""
    p = tmp_path / "iosv_wrapper"
    p.write_text(
        """\
#!/usr/bin/env bash
exec qemu-system-x86_64 -nographic -enable-kvm -m 1024 -nic user
"""
    )
    raw = parse_wrapper(p)
    assert "-nographic" in raw["qemu_flags"]
    assert "-enable-kvm" in raw["qemu_flags"]
    assert "-m" in raw["qemu_flags"]
    assert "1024" in raw["qemu_flags"]
    assert "-nic" in raw["qemu_flags"]
    assert "user" in raw["qemu_flags"]


def test_wrapper_parser_handles_no_qemu_invocation(tmp_path: Path) -> None:
    p = tmp_path / "noop_wrapper.py"
    p.write_text("#!/usr/bin/env python3\nprint('hello')\n")
    raw = parse_wrapper(p)
    assert raw["qemu_flags"] == []
