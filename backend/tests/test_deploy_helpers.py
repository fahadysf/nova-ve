# Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the Bridge-Cloud deploy helpers.

Covers:
- ``deploy/scripts/nova-ve-netplan-gen.py`` — YAML shape + safe-load
  round-trip (plan AC3-ci).
- ``deploy/scripts/nova-ve-backup.py`` — snapshot/restore symlink refusal
  (plan §4.6 / §6.2 T3).
- ``deploy/scripts/nova-ve-marker.sh`` — atomic write + 0600 perms
  (plan §4.1).
"""

from __future__ import annotations

import importlib.util
import os
import pathlib
import shutil
import stat
import subprocess
import sys
import textwrap

import pytest


REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
DEPLOY_SCRIPTS = REPO_ROOT / "deploy" / "scripts"
NETPLAN_GEN = DEPLOY_SCRIPTS / "nova-ve-netplan-gen.py"
BACKUP_PY = DEPLOY_SCRIPTS / "nova-ve-backup.py"
MARKER_SH = DEPLOY_SCRIPTS / "nova-ve-marker.sh"


def _load_module(name: str, path: pathlib.Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


# ---------------------------------------------------------------------------
# nova-ve-netplan-gen.py
# ---------------------------------------------------------------------------


def test_netplan_gen_produces_expected_shape(tmp_path):
    out = tmp_path / "60-nova-ve-bridge-cloud.yaml"
    rc = subprocess.run(
        [sys.executable, str(NETPLAN_GEN), "eth0", "eth1", "--out", str(out)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert rc.returncode == 0, rc.stderr

    import yaml

    parsed = yaml.safe_load(out.read_text())
    assert parsed["network"]["version"] == 2
    assert parsed["network"]["renderer"] == "networkd"
    assert parsed["network"]["ethernets"]["eth0"] == {"dhcp4": False, "dhcp6": False}
    assert parsed["network"]["ethernets"]["eth1"] == {"dhcp4": False, "dhcp6": False}
    assert parsed["network"]["bridges"]["br-eth0"]["dhcp4"] is True
    assert parsed["network"]["bridges"]["br-eth0"]["dhcp6"] is False
    assert parsed["network"]["bridges"]["br-eth0"]["interfaces"] == ["eth0"]
    assert parsed["network"]["bridges"]["br-eth0"]["parameters"]["stp"] is False
    assert parsed["network"]["bridges"]["br-eth1"]["interfaces"] == ["eth1"]


def test_netplan_gen_round_trips_safe_load_dump(tmp_path):
    out = tmp_path / "cfg.yaml"
    subprocess.run(
        [sys.executable, str(NETPLAN_GEN), "eth0", "--out", str(out)],
        check=True,
    )
    import yaml

    body = out.read_text()
    redumped = yaml.safe_dump(
        yaml.safe_load(body), sort_keys=False, default_flow_style=False
    )
    assert redumped == body


def test_netplan_gen_rejects_invalid_iface_names(tmp_path):
    out = tmp_path / "cfg.yaml"
    rc = subprocess.run(
        [sys.executable, str(NETPLAN_GEN), "eth0", "br-eth0", "--out", str(out)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert rc.returncode != 0
    assert "invalid iface name" in rc.stderr.lower()


def test_netplan_gen_stdout_mode_writes_to_stdout():
    rc = subprocess.run(
        [sys.executable, str(NETPLAN_GEN), "eth0", "--out", "-"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert "bridges:" in rc.stdout
    assert "br-eth0:" in rc.stdout


# ---------------------------------------------------------------------------
# nova-ve-backup.py
# ---------------------------------------------------------------------------


def test_backup_snapshot_refuses_symlink_in_src(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.yaml").write_text("hello\n")
    # Drop a symlink among the *.yaml files.
    (src / "evil.yaml").symlink_to(tmp_path / "external")
    dst = tmp_path / "dst"
    rc = subprocess.run(
        [sys.executable, str(BACKUP_PY), "snapshot", str(src), str(dst)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert rc.returncode != 0
    assert "refusing symlink" in rc.stderr


def test_backup_restore_refuses_to_overwrite_symlink(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.yaml").write_text("ok\n")
    dst = tmp_path / "dst"
    dst.mkdir()
    # Place a symlink at the destination path that restore would clobber.
    (dst / "a.yaml").symlink_to(tmp_path / "external")
    rc = subprocess.run(
        [sys.executable, str(BACKUP_PY), "restore", str(src), str(dst)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert rc.returncode != 0
    assert "refusing to restore over symlink" in rc.stderr


def test_backup_snapshot_writes_0600_files(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.yaml").write_text("ok\n")
    (src / "b.yaml").write_text("two\n")
    dst = tmp_path / "dst"
    rc = subprocess.run(
        [sys.executable, str(BACKUP_PY), "snapshot", str(src), str(dst)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert rc.returncode == 0, rc.stderr
    for name in ("a.yaml", "b.yaml"):
        mode = stat.S_IMODE((dst / name).stat().st_mode)
        assert mode == 0o600, f"{name} mode {oct(mode)} != 0600"


# ---------------------------------------------------------------------------
# nova-ve-marker.sh
# ---------------------------------------------------------------------------


def test_marker_helper_writes_atomically(tmp_path):
    # Override _NOVA_VE_MARKER_DIR via env-style script wrapper so the
    # helper writes inside the tmpdir instead of /etc/nova-ve.
    script = tmp_path / "wrap.sh"
    script.write_text(
        textwrap.dedent(
            f"""\
            #!/bin/bash
            _NOVA_VE_MARKER_DIR={tmp_path}/etc-nova-ve
            _NOVA_VE_MARKER_PATH="$_NOVA_VE_MARKER_DIR/bridge-cloud.state"
            mkdir -p "$_NOVA_VE_MARKER_DIR"
            source {MARKER_SH}
            _write_marker_atomic complete || exit 1
            cat "$_NOVA_VE_MARKER_PATH"
            stat -f '%Sp' "$_NOVA_VE_MARKER_PATH" 2>/dev/null \\
                || stat -c '%a' "$_NOVA_VE_MARKER_PATH"
            """
        )
    )
    script.chmod(0o755)
    proc = subprocess.run(
        ["bash", str(script)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    lines = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
    assert lines[0] == "complete"
    # Last line is the mode.  Accept either ``-rw-------`` (BSD/macOS) or
    # ``600`` (Linux GNU coreutils).
    mode_line = lines[-1]
    assert mode_line in ("-rw-------", "600"), f"unexpected mode: {mode_line}"


def test_marker_helper_overwrites_existing_marker(tmp_path):
    script = tmp_path / "wrap.sh"
    script.write_text(
        textwrap.dedent(
            f"""\
            #!/bin/bash
            _NOVA_VE_MARKER_DIR={tmp_path}/etc-nova-ve
            _NOVA_VE_MARKER_PATH="$_NOVA_VE_MARKER_DIR/bridge-cloud.state"
            mkdir -p "$_NOVA_VE_MARKER_DIR"
            source {MARKER_SH}
            _write_marker_atomic naming-flipped
            _write_marker_atomic complete
            cat "$_NOVA_VE_MARKER_PATH"
            """
        )
    )
    script.chmod(0o755)
    proc = subprocess.run(
        ["bash", str(script)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "complete"


def test_marker_helper_no_leftover_temp_files(tmp_path):
    script = tmp_path / "wrap.sh"
    script.write_text(
        textwrap.dedent(
            f"""\
            #!/bin/bash
            _NOVA_VE_MARKER_DIR={tmp_path}/etc-nova-ve
            _NOVA_VE_MARKER_PATH="$_NOVA_VE_MARKER_DIR/bridge-cloud.state"
            mkdir -p "$_NOVA_VE_MARKER_DIR"
            source {MARKER_SH}
            _write_marker_atomic complete
            ls -1a "$_NOVA_VE_MARKER_DIR"
            """
        )
    )
    script.chmod(0o755)
    proc = subprocess.run(
        ["bash", str(script)],
        check=False,
        capture_output=True,
        text=True,
    )
    listing = set(line.strip() for line in proc.stdout.splitlines() if line.strip())
    # No leftover ``.bridge-cloud.state.*`` temp files.
    assert not any(name.startswith(".bridge-cloud.state.") for name in listing), listing


# ---------------------------------------------------------------------------
# provision-ubuntu-2604.sh — bridge_cloud_predictable_ifaces_sorted
# ---------------------------------------------------------------------------


PROVISION_SH = REPO_ROOT / "deploy" / "scripts" / "provision-ubuntu-2604.sh"


def test_predictable_ifaces_sorted_handles_ens_enp_names(tmp_path):
    """Behavioral test for the MAC-pin enumerator on hosts with
    predictable interface names (no ``eth*`` exists yet).

    Builds a fake ``/sys/class/net`` tree containing ``ens3`` and
    ``enp4s0`` (plus ``lo`` and a synthetic veth), then calls the bash
    function via an override that points its ``/sys/class/net`` glob at
    the fake tree.  Verifies the function:

    1. Skips ``lo`` and interfaces without ``device`` symlinks.
    2. Emits ``<pci-path>\t<iface>\t<mac>`` per physical iface.
    3. Sorts by PCI path so the order is deterministic.
    """
    fake_sys_net = tmp_path / "sys-class-net"
    fake_pci = tmp_path / "pci"
    fake_sys_net.mkdir()
    fake_pci.mkdir()

    # ens3 — PCI bus 0000:04:00.0, MAC 52:54:00:aa:bb:cc
    (fake_pci / "0000:04:00.0").mkdir()
    (fake_sys_net / "ens3").mkdir()
    (fake_sys_net / "ens3" / "device").symlink_to(fake_pci / "0000:04:00.0")
    (fake_sys_net / "ens3" / "address").write_text("52:54:00:aa:bb:cc\n")

    # enp1s0 — PCI bus 0000:01:00.0, MAC 52:54:00:11:22:33  (sorts FIRST by PCI)
    (fake_pci / "0000:01:00.0").mkdir()
    (fake_sys_net / "enp1s0").mkdir()
    (fake_sys_net / "enp1s0" / "device").symlink_to(fake_pci / "0000:01:00.0")
    (fake_sys_net / "enp1s0" / "address").write_text("52:54:00:11:22:33\n")

    # lo — no device, must be skipped.
    (fake_sys_net / "lo").mkdir()
    (fake_sys_net / "lo" / "address").write_text("00:00:00:00:00:00\n")

    # veth pair (virtual — no device dir) — must be skipped.
    (fake_sys_net / "veth0").mkdir()
    (fake_sys_net / "veth0" / "address").write_text("aa:aa:aa:aa:aa:aa\n")

    # Drive the function with the fake sysfs root.
    harness = tmp_path / "harness.sh"
    harness.write_text(
        textwrap.dedent(
            f"""\
            #!/usr/bin/env bash
            set -euo pipefail
            # Source the provision script's function definitions only.  We
            # extract just the function bodies to avoid running the install
            # flow (which would touch the real /etc and /opt).
            awk '/^bridge_cloud_predictable_ifaces_sorted\\(\\) {{/,/^}}/' \\
              {PROVISION_SH} > "{tmp_path}/_func.sh"
            # Override the /sys/class/net glob target.
            sed -i.bak 's|/sys/class/net/\\*|{fake_sys_net}/*|' "{tmp_path}/_func.sh"
            # shellcheck disable=SC1091
            source "{tmp_path}/_func.sh"
            bridge_cloud_predictable_ifaces_sorted
            """
        )
    )
    harness.chmod(0o755)

    proc = subprocess.run(
        ["bash", str(harness)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    lines = [line for line in proc.stdout.splitlines() if line.strip()]

    # Two physical ifaces (ens3 + enp1s0), sorted by PCI path:
    # enp1s0 (0000:01:00.0) FIRST, ens3 (0000:04:00.0) SECOND.
    assert len(lines) == 2, lines
    parsed = [line.split("\t") for line in lines]
    assert parsed[0][1] == "enp1s0", parsed
    assert parsed[0][2] == "52:54:00:11:22:33"
    assert parsed[1][1] == "ens3", parsed
    assert parsed[1][2] == "52:54:00:aa:bb:cc"
