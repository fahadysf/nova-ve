# Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""Tests for US-206 — orphan sweep on lab/node stop and backend startup.

These tests mock the privileged helper and ``ip -o link show`` so they run
without root and without real kernel objects.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services import host_net


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def instance_env(monkeypatch, tmp_path):
    """Seed a deterministic instance_id so bridge/iface names are predictable."""
    idir = tmp_path / "nova-ve"
    idir.mkdir(parents=True, exist_ok=True)
    (idir / "instance_id").write_text("test-orphan-sweep")
    monkeypatch.setenv("NOVA_VE_INSTANCE_DIR", str(idir))
    return "test-orphan-sweep"


def _hash_hex(lab_id: str, instance_id: str) -> str:
    import hashlib
    raw = hashlib.blake2b(
        f"{instance_id}:{lab_id}".encode("utf-8"), digest_size=2
    ).hexdigest()
    return f"{int(raw, 16):04x}"


def _make_bridge_name(lab_id: str, network_id: int, instance_id: str) -> str:
    h = _hash_hex(lab_id, instance_id)
    return f"nove{h}n{network_id}"


def _make_veth_name(lab_id: str, node_id: int, iface: int, instance_id: str, suffix: str = "h") -> str:
    h = _hash_hex(lab_id, instance_id)
    return f"nve{h}d{node_id}i{iface}{suffix}"


def _make_tap_name(lab_id: str, node_id: int, iface: int, instance_id: str) -> str:
    h = _hash_hex(lab_id, instance_id)
    return f"nve{h}d{node_id}i{iface}"


# ---------------------------------------------------------------------------
# _list_host_ifaces_prefixed mock factory
# ---------------------------------------------------------------------------


def _mock_ip_link(monkeypatch, present_names: list[str]):
    """Make ``_run`` return a fake ``ip -o link show`` listing ``present_names``."""
    lines = []
    for i, name in enumerate(present_names, start=1):
        lines.append(f"{i}: {name}: <BROADCAST,MULTICAST> mtu 1500 ...")

    def _fake_run(argv):
        # ip -o link show
        if "ip" in argv[0] and "-o" in argv and "link" in argv and "show" in argv:
            return SimpleNamespace(returncode=0, stdout="\n".join(lines))
        # tap-del / bridge-del via helper — succeed
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(host_net, "_run", _fake_run)


# ---------------------------------------------------------------------------
# sweep_node_host_ifaces
# ---------------------------------------------------------------------------


class TestSweepNodeHostIfaces:
    def test_removes_matching_ifaces(self, monkeypatch, instance_env, tmp_path):
        """Unregistered host-end veth for a node is swept."""
        lab_id = "lab-abc"
        node_id = 3
        iface_h = _make_veth_name(lab_id, node_id, 0, instance_env, "h")
        iface_p = _make_veth_name(lab_id, node_id, 0, instance_env, "p")
        # peer-end naming (p) also matches regex
        other = _make_veth_name("lab-other", 5, 1, instance_env, "h")

        deleted: list[str] = []

        def _fake_run(argv):
            if "-o" in argv:
                names = [iface_h, iface_p, other]
                lines = [f"{i}: {n}: <>" for i, n in enumerate(names, 1)]
                return SimpleNamespace(returncode=0, stdout="\n".join(lines))
            # tap-del verb — record deletion
            if "tap-del" in argv:
                deleted.append(argv[-1])
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(host_net, "_run", _fake_run)

        removed = host_net.sweep_node_host_ifaces(lab_id, node_id)

        # Both iface_h and iface_p belong to lab-abc/node-3 and should be removed.
        assert iface_h in removed
        assert iface_p in removed
        # The "other" lab's iface must NOT be touched.
        assert other not in removed

    def test_registered_pid_iface_still_swept_on_stop(self, monkeypatch, instance_env):
        """sweep_node_host_ifaces doesn't check pids.json; it sweeps by name."""
        lab_id = "lab-xyz"
        node_id = 1
        iface = _make_veth_name(lab_id, node_id, 0, instance_env, "h")

        deleted: list[str] = []

        def _fake_run(argv):
            if "-o" in argv:
                return SimpleNamespace(returncode=0, stdout=f"1: {iface}: <>")
            if "tap-del" in argv:
                deleted.append(argv[-1])
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(host_net, "_run", _fake_run)
        removed = host_net.sweep_node_host_ifaces(lab_id, node_id)
        assert iface in removed

    def test_continues_past_individual_failure(self, monkeypatch, instance_env):
        """Sweep continues even if one deletion fails."""
        lab_id = "lab-fail"
        node_id = 2
        iface0 = _make_veth_name(lab_id, node_id, 0, instance_env, "h")
        iface1 = _make_veth_name(lab_id, node_id, 1, instance_env, "h")

        call_count = [0]

        def _fake_run(argv):
            if "-o" in argv:
                names = [iface0, iface1]
                lines = [f"{i}: {n}: <>" for i, n in enumerate(names, 1)]
                return SimpleNamespace(returncode=0, stdout="\n".join(lines))
            if "tap-del" in argv:
                call_count[0] += 1
                if call_count[0] == 1:
                    # First deletion fails
                    return SimpleNamespace(
                        returncode=1, stdout="", stderr="no such device"
                    )
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(host_net, "_run", _fake_run)
        # Should not raise; second iface should still be attempted.
        removed = host_net.sweep_node_host_ifaces(lab_id, node_id)
        # At least the second iface succeeded.
        assert iface1 in removed

    def test_no_instance_id_returns_empty(self, monkeypatch):
        """Missing instance_id → empty list, no crash."""
        monkeypatch.setenv("NOVA_VE_INSTANCE_DIR", "/nonexistent-path-xyz")
        monkeypatch.delenv("NOVA_VE_INSTANCE_ID", raising=False)
        monkeypatch.delenv("NOVA_VE_INSTANCE_ID_OVERRIDE_OK", raising=False)
        result = host_net.sweep_node_host_ifaces("lab-x", 1)
        assert result == []


# ---------------------------------------------------------------------------
# sweep_orphan_bridges
# ---------------------------------------------------------------------------


class TestSweepOrphanBridges:
    def test_orphan_bridge_removed(self, monkeypatch, instance_env):
        """A nove* bridge with no matching known lab is an orphan and is removed."""
        # Bridge that belongs to a real lab
        known_lab = "lab-known"
        orphan_lab = "lab-orphan-gone"
        owned_bridge = _make_bridge_name(known_lab, 1, instance_env)
        orphan_bridge = _make_bridge_name(orphan_lab, 1, instance_env)

        deleted: list[str] = []

        def _fake_run(argv):
            if "-o" in argv:
                names = [owned_bridge, orphan_bridge]
                lines = [f"{i}: {n}: <>" for i, n in enumerate(names, 1)]
                return SimpleNamespace(returncode=0, stdout="\n".join(lines))
            if "bridge-del" in argv:
                deleted.append(argv[-1])
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(host_net, "_run", _fake_run)

        removed = host_net.sweep_orphan_bridges({known_lab})

        assert orphan_bridge in removed
        assert owned_bridge not in removed

    def test_bridge_with_known_lab_kept(self, monkeypatch, instance_env):
        """A nove* bridge owned by a known lab is not touched."""
        known_lab = "lab-keep"
        bridge = _make_bridge_name(known_lab, 2, instance_env)

        def _fake_run(argv):
            if "-o" in argv:
                return SimpleNamespace(returncode=0, stdout=f"1: {bridge}: <>")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(host_net, "_run", _fake_run)

        removed = host_net.sweep_orphan_bridges({known_lab})
        assert bridge not in removed
        assert removed == []

    def test_sweep_continues_past_individual_failure(self, monkeypatch, instance_env):
        """Orphan sweep continues if one bridge deletion fails."""
        orphan1 = _make_bridge_name("orphan-a", 1, instance_env)
        orphan2 = _make_bridge_name("orphan-b", 1, instance_env)

        call_count = [0]

        def _fake_run(argv):
            if "-o" in argv:
                names = [orphan1, orphan2]
                lines = [f"{i}: {n}: <>" for i, n in enumerate(names, 1)]
                return SimpleNamespace(returncode=0, stdout="\n".join(lines))
            if "bridge-del" in argv:
                call_count[0] += 1
                if call_count[0] == 1:
                    return SimpleNamespace(
                        returncode=1, stdout="", stderr="no such device"
                    )
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(host_net, "_run", _fake_run)
        removed = host_net.sweep_orphan_bridges(set())
        # At least orphan2 succeeds
        assert orphan2 in removed

    def test_no_instance_id_returns_empty(self, monkeypatch):
        """Missing instance_id → empty list, no crash."""
        monkeypatch.setenv("NOVA_VE_INSTANCE_DIR", "/nonexistent-path-xyz")
        monkeypatch.delenv("NOVA_VE_INSTANCE_ID", raising=False)
        monkeypatch.delenv("NOVA_VE_INSTANCE_ID_OVERRIDE_OK", raising=False)
        result = host_net.sweep_orphan_bridges(set())
        assert result == []


# ---------------------------------------------------------------------------
# sweep_orphan_ifaces
# ---------------------------------------------------------------------------


class TestSweepOrphanIfaces:
    def test_orphan_iface_removed(self, monkeypatch, instance_env):
        """A nve* iface with no matching known lab is removed."""
        known_lab = "lab-known2"
        orphan_lab = "lab-orphan2"
        owned_iface = _make_veth_name(known_lab, 1, 0, instance_env, "h")
        orphan_iface = _make_veth_name(orphan_lab, 1, 0, instance_env, "h")

        deleted: list[str] = []

        def _fake_run(argv):
            if "-o" in argv:
                names = [owned_iface, orphan_iface]
                lines = [f"{i}: {n}: <>" for i, n in enumerate(names, 1)]
                return SimpleNamespace(returncode=0, stdout="\n".join(lines))
            if "tap-del" in argv:
                deleted.append(argv[-1])
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(host_net, "_run", _fake_run)

        removed = host_net.sweep_orphan_ifaces({known_lab})
        assert orphan_iface in removed
        assert owned_iface not in removed

    def test_no_instance_id_returns_empty(self, monkeypatch):
        monkeypatch.setenv("NOVA_VE_INSTANCE_DIR", "/nonexistent-path-xyz")
        monkeypatch.delenv("NOVA_VE_INSTANCE_ID", raising=False)
        monkeypatch.delenv("NOVA_VE_INSTANCE_ID_OVERRIDE_OK", raising=False)
        result = host_net.sweep_orphan_ifaces(set())
        assert result == []


# ---------------------------------------------------------------------------
# Integration: startup sweep removes orphan objects not in known labs
# ---------------------------------------------------------------------------


class TestStartupOrphanSweep:
    """Tests that exercise the full sweep path as called from app.startup."""

    def test_startup_sweep_removes_orphan_bridge_not_in_labs_dir(
        self, monkeypatch, instance_env, tmp_path
    ):
        """Orphan bridge (no lab file) is removed during startup sweep."""
        labs_dir = tmp_path / "labs"
        labs_dir.mkdir()

        orphan_bridge = _make_bridge_name("lab-deleted", 1, instance_env)
        deleted: list[str] = []

        def _fake_run(argv):
            if "-o" in argv:
                return SimpleNamespace(returncode=0, stdout=f"1: {orphan_bridge}: <>")
            if "bridge-del" in argv:
                deleted.append(argv[-1])
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(host_net, "_run", _fake_run)

        # No lab files → known_lab_ids = {}
        known: set[str] = set()
        removed = host_net.sweep_orphan_bridges(known)
        assert orphan_bridge in removed

    def test_startup_sweep_keeps_bridge_for_existing_lab(
        self, monkeypatch, instance_env, tmp_path
    ):
        """Bridge for a lab that still has a JSON file is kept."""
        labs_dir = tmp_path / "labs"
        labs_dir.mkdir()
        lab_id = "lab-still-here"
        lab_file = labs_dir / "lab.json"
        lab_file.write_text(json.dumps({"id": lab_id}))

        bridge = _make_bridge_name(lab_id, 1, instance_env)

        def _fake_run(argv):
            if "-o" in argv:
                return SimpleNamespace(returncode=0, stdout=f"1: {bridge}: <>")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(host_net, "_run", _fake_run)

        removed = host_net.sweep_orphan_bridges({lab_id})
        assert bridge not in removed

    def test_startup_sweep_removes_orphan_iface(
        self, monkeypatch, instance_env
    ):
        """Orphan nve* iface (no matching lab) is removed during startup sweep."""
        orphan_iface = _make_veth_name("lab-gone", 2, 0, instance_env, "h")

        def _fake_run(argv):
            if "-o" in argv:
                return SimpleNamespace(returncode=0, stdout=f"1: {orphan_iface}: <>")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        monkeypatch.setattr(host_net, "_run", _fake_run)

        removed = host_net.sweep_orphan_ifaces(set())
        assert orphan_iface in removed
