# Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
# SPDX-License-Identifier: Apache-2.0

"""US-202b — Tests for scripts/migrate_runtime_network.py.

Covers:
  - no-op on already-migrated lab (all networks have runtime.bridge_name)
  - fills missing bridge_name correctly (uses host_net.bridge_name formula)
  - idempotency (second run is a no-op)
  - dry-run does not write any files or touch host state
  - running-node precondition: exits 1 and lists offending labs
  - per-lab rollback on bridge ownership verification failure
  - abort on docker network inspect failure (no docker network rm called)
  - rollback restores old Docker network from captured inspect JSON
  - stamps node.machine_override='pc' on pre-Wave-7 QEMU nodes
    (compat discriminator — see plan :397-400)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

# Make scripts/ importable.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPTS_DIR = _REPO_ROOT / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import migrate_runtime_network as mig  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures & helpers
# ---------------------------------------------------------------------------


@pytest.fixture()
def instance_id(monkeypatch, tmp_path):
    """Seed a deterministic instance_id."""
    instance_dir = tmp_path / "nova-ve-instance"
    instance_dir.mkdir()
    value = "test-instance-uuid-202b"
    (instance_dir / "instance_id").write_text(value)
    monkeypatch.setenv("NOVA_VE_INSTANCE_DIR", str(instance_dir))
    return value


def _make_lab(
    labs_dir: Path,
    name: str = "lab.json",
    lab_id: str = "lab-aaa",
    networks: dict[str, Any] | None = None,
    nodes: dict[str, Any] | None = None,
) -> Path:
    payload: dict[str, Any] = {
        "schema": 2,
        "id": lab_id,
        "meta": {"name": name},
        "viewport": {"x": 0, "y": 0, "zoom": 1.0},
        "nodes": nodes or {},
        "networks": networks or {},
        "links": [],
        "defaults": {"link_style": "orthogonal"},
    }
    lab_path = labs_dir / name
    lab_path.write_text(json.dumps(payload), encoding="utf-8")
    return lab_path


def _net(net_id: int, *, bridge_name: str | None = None) -> dict[str, Any]:
    """Build a minimal network record."""
    n: dict[str, Any] = {
        "id": net_id,
        "name": f"Net{net_id}",
        "type": "linux_bridge",
        "left": 0,
        "top": 0,
        "visibility": True,
        "implicit": False,
        "runtime": {},
    }
    if bridge_name is not None:
        n["runtime"]["bridge_name"] = bridge_name
    return n


def _missing_network_inspect_result(name: str) -> SimpleNamespace:
    """Simulate Docker's 'network absent' inspect failure."""
    return SimpleNamespace(
        returncode=1,
        stdout="",
        stderr=f"Error: No such network: {name}",
    )


# ---------------------------------------------------------------------------
# No-op on already-migrated lab
# ---------------------------------------------------------------------------


def test_already_migrated_is_noop(instance_id, tmp_path):
    """If all networks have runtime.bridge_name, process_lab returns 0 backfilled."""
    from app.services import host_net

    labs_dir = tmp_path / "labs"
    labs_dir.mkdir()
    backup_dir = tmp_path / "backup"
    bridge = host_net.bridge_name("lab-aaa", 1)

    lab_path = _make_lab(
        labs_dir,
        lab_id="lab-aaa",
        networks={"1": _net(1, bridge_name=bridge)},
    )

    checked, backfilled, nodes_stamped = mig.process_lab(
        lab_path, labs_dir, backup_dir, dry_run=False
    )

    assert checked == 1
    assert backfilled == 0
    assert nodes_stamped == 0
    # File is untouched (no backup taken since nothing changed).
    assert not backup_dir.exists() or not any(backup_dir.iterdir())


# ---------------------------------------------------------------------------
# Fills missing bridge_name correctly
# ---------------------------------------------------------------------------


def test_fills_missing_bridge_name(instance_id, tmp_path, monkeypatch):
    """Missing runtime.bridge_name is backfilled with host_net.bridge_name value."""
    from app.services import host_net

    labs_dir = tmp_path / "labs"
    labs_dir.mkdir()
    backup_dir = tmp_path / "backup"

    lab_path = _make_lab(
        labs_dir,
        lab_id="lab-fill",
        networks={"1": _net(1), "2": _net(2)},
    )

    # No Docker networks exist (inspect returns non-zero → treated as absent).
    monkeypatch.setattr(
        mig,
        "_docker_network_inspect",
        _missing_network_inspect_result,
    )
    monkeypatch.setattr("app.services.host_net.bridge_exists", lambda name: False)

    checked, backfilled, nodes_stamped = mig.process_lab(
        lab_path, labs_dir, backup_dir, dry_run=False
    )

    assert checked == 2
    assert backfilled == 2
    assert nodes_stamped == 0

    saved = json.loads(lab_path.read_text())
    expected_1 = host_net.bridge_name("lab-fill", 1)
    expected_2 = host_net.bridge_name("lab-fill", 2)
    assert saved["networks"]["1"]["runtime"]["bridge_name"] == expected_1
    assert saved["networks"]["2"]["runtime"]["bridge_name"] == expected_2
    assert host_net.bridge_fingerprint_check(expected_1, "lab-fill", 1) == "absent"
    assert host_net.bridge_fingerprint_check(expected_2, "lab-fill", 2) == "absent"


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


def test_idempotent_second_run(instance_id, tmp_path, monkeypatch):
    """Running process_lab twice is a no-op on the second run."""
    labs_dir = tmp_path / "labs"
    labs_dir.mkdir()
    backup_dir = tmp_path / "backup"

    lab_path = _make_lab(
        labs_dir,
        lab_id="lab-idem",
        networks={"1": _net(1)},
    )
    monkeypatch.setattr(
        mig,
        "_docker_network_inspect",
        _missing_network_inspect_result,
    )
    monkeypatch.setattr("app.services.host_net.bridge_add", lambda name: None)
    monkeypatch.setattr("app.services.host_net.bridge_exists", lambda name: False)

    _, backfilled_1, _ = mig.process_lab(
        lab_path, labs_dir, backup_dir, dry_run=False
    )
    assert backfilled_1 == 1

    # Second run — should be zero.
    _, backfilled_2, _ = mig.process_lab(
        lab_path, labs_dir, backup_dir, dry_run=False
    )
    assert backfilled_2 == 0


# ---------------------------------------------------------------------------
# Dry-run does not write
# ---------------------------------------------------------------------------


def test_dry_run_does_not_write(instance_id, tmp_path, monkeypatch):
    """dry_run=True reports what would change but does not modify any files."""
    labs_dir = tmp_path / "labs"
    labs_dir.mkdir()
    backup_dir = tmp_path / "backup"

    lab_path = _make_lab(
        labs_dir,
        lab_id="lab-dry",
        networks={"1": _net(1)},
    )
    original_text = lab_path.read_text()

    bridge_add_calls: list[str] = []
    monkeypatch.setattr(
        mig,
        "_docker_network_inspect",
        _missing_network_inspect_result,
    )
    monkeypatch.setattr(
        "app.services.host_net.bridge_add",
        lambda name: bridge_add_calls.append(name),
    )
    monkeypatch.setattr("app.services.host_net.bridge_exists", lambda name: False)

    checked, backfilled, nodes_stamped = mig.process_lab(
        lab_path, labs_dir, backup_dir, dry_run=True
    )

    assert backfilled == 1
    assert nodes_stamped == 0
    # File must be unchanged.
    assert lab_path.read_text() == original_text
    # No host changes.
    assert bridge_add_calls == []
    # No backup directory created.
    assert not backup_dir.exists()


def test_main_dry_run_does_not_write(instance_id, tmp_path, monkeypatch):
    """main() --dry-run leaves all lab files untouched."""
    labs_dir = tmp_path / "labs"
    labs_dir.mkdir()

    lab_path = _make_lab(
        labs_dir,
        lab_id="lab-main-dry",
        networks={"1": _net(1)},
    )
    original_text = lab_path.read_text()

    monkeypatch.setattr(
        mig,
        "_docker_network_inspect",
        _missing_network_inspect_result,
    )
    monkeypatch.setattr("app.services.host_net.bridge_add", lambda name: None)
    monkeypatch.setattr("app.services.host_net.bridge_exists", lambda name: False)

    rc = mig.main(["--labs-dir", str(labs_dir), "--dry-run"])

    assert rc == 0
    assert lab_path.read_text() == original_text


# ---------------------------------------------------------------------------
# Running-node precondition
# ---------------------------------------------------------------------------


def test_precondition_running_nodes_exits_1(instance_id, tmp_path):
    """main() exits 1 when any lab has a running node (status=2)."""
    labs_dir = tmp_path / "labs"
    labs_dir.mkdir()

    _make_lab(
        labs_dir,
        name="lab_running.json",
        lab_id="lab-running",
        nodes={"1": {"id": 1, "name": "node-a", "status": 2}},
        networks={"1": _net(1)},
    )

    rc = mig.main(["--labs-dir", str(labs_dir)])
    assert rc == 1


def test_precondition_stopped_node_passes(instance_id, tmp_path, monkeypatch):
    """A lab with all nodes stopped (status=0) passes the precondition check."""
    labs_dir = tmp_path / "labs"
    labs_dir.mkdir()

    _make_lab(
        labs_dir,
        name="lab_stopped.json",
        lab_id="lab-stopped",
        nodes={"1": {"id": 1, "name": "node-a", "status": 0}},
        networks={"1": _net(1)},
    )
    monkeypatch.setattr(
        mig,
        "_docker_network_inspect",
        _missing_network_inspect_result,
    )
    monkeypatch.setattr("app.services.host_net.bridge_add", lambda name: None)
    monkeypatch.setattr("app.services.host_net.bridge_exists", lambda name: False)

    rc = mig.main(["--labs-dir", str(labs_dir)])
    assert rc == 0


# ---------------------------------------------------------------------------
# Per-lab abort on bridge ownership collision
# ---------------------------------------------------------------------------


def test_migrate_aborts_on_unfingerprinted_bridge(instance_id, tmp_path, monkeypatch):
    """An existing unfingerprinted bridge aborts the lab migration fail-closed."""
    from app.services import host_net

    labs_dir = tmp_path / "labs"
    labs_dir.mkdir()
    backup_dir = tmp_path / "backup"

    lab_path = _make_lab(
        labs_dir,
        lab_id="lab-fail",
        networks={"1": _net(1)},
    )
    original_text = lab_path.read_text()

    # Simulate a Docker network NOT existing (no docker cleanup path).
    monkeypatch.setattr(
        mig,
        "_docker_network_inspect",
        _missing_network_inspect_result,
    )
    bridge = host_net.bridge_name("lab-fail", 1)
    monkeypatch.setattr("app.services.host_net.bridge_exists", lambda name: name == bridge)
    monkeypatch.setattr("app.services.host_net.bridge_del", lambda name: None)

    with pytest.raises(RuntimeError, match="ownership check failed"):
        mig.process_lab(lab_path, labs_dir, backup_dir, dry_run=False)

    assert lab_path.read_text() == original_text


def test_migrate_aborts_on_mismatched_fingerprint(instance_id, tmp_path, monkeypatch):
    """A mismatched fingerprint aborts and restores the removed Docker network."""
    from app.services import host_net

    labs_dir = tmp_path / "labs"
    labs_dir.mkdir()
    backup_dir = tmp_path / "backup"

    # A network with a docker_network config pointing to a named Docker network.
    net = _net(1)
    net["config"] = {"docker_network": "nova-ve-lab-fail2-Net1"}
    lab_path = _make_lab(
        labs_dir,
        lab_id="lab-fail2",
        networks={"1": net},
    )

    inspect_payload = [
        {
            "Name": "nova-ve-lab-fail2-Net1",
            "Driver": "bridge",
            "Options": {},
            "IPAM": {"Config": [{"Subnet": "172.20.0.0/16"}]},
            "Containers": {},
        }
    ]

    monkeypatch.setattr(
        mig,
        "_docker_network_inspect",
        lambda name: SimpleNamespace(
            returncode=0,
            stdout=json.dumps(inspect_payload),
            stderr="",
        ),
    )

    rm_calls: list[str] = []
    monkeypatch.setattr(
        mig,
        "_docker_network_rm",
        lambda name: rm_calls.append(name) or SimpleNamespace(returncode=0, stdout="", stderr=""),
    )

    create_calls: list[dict] = []

    def fake_create(entry: dict) -> SimpleNamespace:
        create_calls.append(entry)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(mig, "_docker_network_create_from_inspect", fake_create)
    bridge = host_net.bridge_name("lab-fail2", 1)
    monkeypatch.setattr("app.services.host_net.bridge_exists", lambda name: name == bridge)
    monkeypatch.setattr("app.services.host_net.bridge_del", lambda name: None)
    host_net.bridge_fingerprint_write(bridge, "other-lab", 1)

    with pytest.raises(RuntimeError, match="ownership check failed"):
        mig.process_lab(lab_path, labs_dir, backup_dir, dry_run=False)

    # Docker network was removed.
    assert rm_calls == ["nova-ve-lab-fail2-Net1"]
    # Docker network was recreated from captured inspect JSON on rollback.
    assert len(create_calls) == 1
    assert create_calls[0]["Name"] == "nova-ve-lab-fail2-Net1"


# ---------------------------------------------------------------------------
# Abort on docker network inspect failure (no docker network rm)
# ---------------------------------------------------------------------------


def test_migrate_runtime_network_aborts_on_inspect_failure(
    instance_id, tmp_path, monkeypatch
):
    """If docker network inspect fails, migration aborts without calling docker network rm."""
    labs_dir = tmp_path / "labs"
    labs_dir.mkdir()
    backup_dir = tmp_path / "backup"

    net = _net(1)
    net["config"] = {"docker_network": "nova-ve-lab-inspect-fail-Net1"}
    lab_path = _make_lab(
        labs_dir,
        lab_id="lab-inspect-fail",
        networks={"1": net},
    )

    # Inspect returns exit 0 but invalid JSON → should abort.
    monkeypatch.setattr(
        mig,
        "_docker_network_inspect",
        lambda name: SimpleNamespace(returncode=0, stdout="NOT JSON", stderr=""),
    )

    rm_calls: list[str] = []
    monkeypatch.setattr(
        mig,
        "_docker_network_rm",
        lambda name: rm_calls.append(name) or SimpleNamespace(returncode=0, stdout="", stderr=""),
    )
    monkeypatch.setattr("app.services.host_net.bridge_add", lambda name: None)
    monkeypatch.setattr("app.services.host_net.bridge_exists", lambda name: False)
    monkeypatch.setattr("app.services.host_net.bridge_del", lambda name: None)

    with pytest.raises(RuntimeError, match="migration failed"):
        mig.process_lab(lab_path, labs_dir, backup_dir, dry_run=False)

    # docker network rm must NOT have been called.
    assert rm_calls == []


def test_migrate_aborts_on_inspect_daemon_failure(
    instance_id, tmp_path, monkeypatch, capsys
):
    """Non-absence inspect failures abort the lab and return a non-zero exit."""
    labs_dir = tmp_path / "labs"
    labs_dir.mkdir()

    net = _net(1)
    net["config"] = {"docker_network": "nova-ve-lab-daemon-fail-Net1"}
    _make_lab(
        labs_dir,
        lab_id="lab-daemon-fail",
        networks={"1": net},
    )

    monkeypatch.setattr(
        mig,
        "_docker_network_inspect",
        lambda name: SimpleNamespace(
            returncode=125,
            stdout="",
            stderr="Cannot connect to the Docker daemon at unix:///var/run/docker.sock. Is the docker daemon running?",
        ),
    )

    rm_calls: list[str] = []
    monkeypatch.setattr(
        mig,
        "_docker_network_rm",
        lambda name: rm_calls.append(name) or SimpleNamespace(returncode=0, stdout="", stderr=""),
    )
    monkeypatch.setattr("app.services.host_net.bridge_add", lambda name: None)
    monkeypatch.setattr("app.services.host_net.bridge_exists", lambda name: False)
    monkeypatch.setattr("app.services.host_net.bridge_del", lambda name: None)

    rc = mig.main(["--labs-dir", str(labs_dir)])

    captured = capsys.readouterr()
    assert rc == 2
    assert rm_calls == []
    assert "could not capture network state for rollback; refusing to proceed" in captured.err
    assert (
        "Cannot connect to the Docker daemon at unix:///var/run/docker.sock. "
        "Is the docker daemon running?"
    ) in captured.err


def test_migrate_continues_on_no_such_network_inspect_error(
    instance_id, tmp_path, monkeypatch
):
    """Exit 1 with Docker's 'No such network' stderr is treated as absent."""
    from app.services import host_net

    labs_dir = tmp_path / "labs"
    labs_dir.mkdir()
    backup_dir = tmp_path / "backup"

    lab_path = _make_lab(
        labs_dir,
        lab_id="lab-no-such-network",
        networks={"1": _net(1)},
    )

    monkeypatch.setattr(
        mig,
        "_docker_network_inspect",
        _missing_network_inspect_result,
    )

    bridge_add_calls: list[str] = []
    monkeypatch.setattr(
        "app.services.host_net.bridge_add",
        lambda name: bridge_add_calls.append(name),
    )
    monkeypatch.setattr("app.services.host_net.bridge_exists", lambda name: False)

    checked, backfilled, nodes_stamped = mig.process_lab(
        lab_path, labs_dir, backup_dir, dry_run=False
    )

    saved = json.loads(lab_path.read_text())
    expected = host_net.bridge_name("lab-no-such-network", 1)
    assert checked == 1
    assert backfilled == 1
    assert nodes_stamped == 0
    assert saved["networks"]["1"]["runtime"]["bridge_name"] == expected
    assert bridge_add_calls == [expected]


# ---------------------------------------------------------------------------
# Non-zero exit from main() when errors occur
# ---------------------------------------------------------------------------


def test_main_returns_2_on_error(instance_id, tmp_path, monkeypatch):
    """main() returns 2 when any lab fails migration."""
    from app.services import host_net

    labs_dir = tmp_path / "labs"
    labs_dir.mkdir()

    _make_lab(
        labs_dir,
        lab_id="lab-err",
        networks={"1": _net(1)},
    )

    monkeypatch.setattr(
        mig,
        "_docker_network_inspect",
        _missing_network_inspect_result,
    )
    bridge = host_net.bridge_name("lab-err", 1)
    monkeypatch.setattr("app.services.host_net.bridge_exists", lambda name: name == bridge)
    monkeypatch.setattr("app.services.host_net.bridge_del", lambda name: None)

    rc = mig.main(["--labs-dir", str(labs_dir)])
    assert rc == 2


# ---------------------------------------------------------------------------
# main() nonexistent labs_dir
# ---------------------------------------------------------------------------


def test_main_nonexistent_labs_dir(tmp_path):
    """main() exits 1 when labs_dir does not exist."""
    rc = mig.main(["--labs-dir", str(tmp_path / "nonexistent")])
    assert rc == 1


# ---------------------------------------------------------------------------
# node.machine_override='pc' backfill (compat discriminator — plan :397-400)
# ---------------------------------------------------------------------------


def test_migrate_stamps_machine_override(instance_id, tmp_path, monkeypatch):
    """Pre-Wave-7 QEMU nodes get machine_override='pc' stamped.

    Acceptance criteria from .omc/plans/network-runtime-wiring.md:397-400:
      (a) QEMU nodes lacking machine_override get 'pc' stamped on every one.
      (b) docker/iol/dynamips nodes are untouched.
      (c) QEMU nodes already carrying machine_override (e.g. user-set 'q35')
          are untouched (idempotency contract).
      (d) Re-running the migration on an already-stamped lab is a no-op.
    """
    labs_dir = tmp_path / "labs"
    labs_dir.mkdir()
    backup_dir = tmp_path / "backup"

    # Mix of node types covering all four acceptance branches.
    nodes = {
        # (a) pre-Wave-7 QEMU nodes — no machine_override key at all.
        "1": {"id": 1, "name": "vyos-1", "type": "qemu", "status": 0},
        "2": {"id": 2, "name": "csr-1", "type": "qemu", "status": 0},
        # (b) non-QEMU nodes must NOT be touched, even if their type lacks the
        # field (machine_override is QEMU-specific in the spec).
        "3": {"id": 3, "name": "alpine-a", "type": "docker", "status": 0},
        "4": {"id": 4, "name": "iol-1", "type": "iol", "status": 0},
        "5": {"id": 5, "name": "dyn-1", "type": "dynamips", "status": 0},
        # (c) user-set machine_override='q35' must be preserved.
        "6": {
            "id": 6,
            "name": "vyos-q35",
            "type": "qemu",
            "status": 0,
            "machine_override": "q35",
        },
        # An already-'pc' QEMU node — must remain 'pc' (no double-stamp).
        "7": {
            "id": 7,
            "name": "vyos-pc",
            "type": "qemu",
            "status": 0,
            "machine_override": "pc",
        },
    }
    lab_path = _make_lab(
        labs_dir,
        lab_id="lab-mach",
        nodes=nodes,
        networks={"1": _net(1)},
    )

    monkeypatch.setattr(
        mig,
        "_docker_network_inspect",
        _missing_network_inspect_result,
    )
    monkeypatch.setattr("app.services.host_net.bridge_add", lambda name: None)
    monkeypatch.setattr("app.services.host_net.bridge_exists", lambda name: False)

    checked, backfilled, nodes_stamped = mig.process_lab(
        lab_path, labs_dir, backup_dir, dry_run=False
    )

    assert checked == 1
    assert backfilled == 1
    # Two pre-Wave-7 QEMU nodes (ids 1 and 2) should have been stamped.
    assert nodes_stamped == 2

    saved = json.loads(lab_path.read_text())
    saved_nodes = saved["nodes"]

    # (a) Pre-Wave-7 QEMU nodes — stamped 'pc'.
    assert saved_nodes["1"]["machine_override"] == "pc"
    assert saved_nodes["2"]["machine_override"] == "pc"

    # (b) Docker / iol / dynamips nodes — untouched, no machine_override added.
    assert "machine_override" not in saved_nodes["3"]
    assert "machine_override" not in saved_nodes["4"]
    assert "machine_override" not in saved_nodes["5"]

    # (c) User-set values preserved.
    assert saved_nodes["6"]["machine_override"] == "q35"
    assert saved_nodes["7"]["machine_override"] == "pc"

    # (d) Re-running the migration is a no-op for nodes (already stamped) AND
    # for networks (already migrated above).
    text_after_first = lab_path.read_text()
    checked2, backfilled2, nodes_stamped2 = mig.process_lab(
        lab_path, labs_dir, backup_dir, dry_run=False
    )
    assert checked2 == 1
    assert backfilled2 == 0
    assert nodes_stamped2 == 0
    # File contents must be identical on a second pass (no spurious rewrite).
    assert lab_path.read_text() == text_after_first
