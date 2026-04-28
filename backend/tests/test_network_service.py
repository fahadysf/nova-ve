# Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
# SPDX-License-Identifier: Apache-2.0

"""US-202 — ``network_service.create_network`` provisions a real Linux bridge.

Asserts the contract specified in
``.omc/plans/network-runtime-wiring.md`` § US-202:

  * ``host_net.bridge_add`` is invoked with the
    ``nove{lab_hash:04x}n{network_id}`` name derived from the lab UUID and
    instance ID.
  * The same lab_id with different instance_ids yields different bridge
    names (collision-resistant cross-host).
  * The bridge name is persisted on the network record under
    ``runtime.bridge_name`` (US-401 schema field, hoisted by US-202).
  * ``create_network`` is idempotent only when a pre-existing bridge has
    a matching ownership fingerprint; otherwise the typed ownership
    exception is raised fail-closed.
  * On ``bridge_add`` failure, the JSON write is rolled back (no leaked
    network record) and a typed ``NetworkServiceError`` with status 409
    is raised.
  * ``delete_network`` invokes ``bridge_del`` with the persisted name.

The privileged helper is MOCKED throughout — no real ``ip link add``
calls are spawned (CI is un-privileged and would corrupt host network
state otherwise).
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services import host_net, network_service as network_service_mod
from app.services.network_service import (
    NetworkService,
    NetworkServiceError,
)


@pytest.fixture()
def labs_dir(tmp_path: Path) -> Path:
    d = tmp_path / "labs"
    d.mkdir()
    return d


@pytest.fixture()
def instance_id(monkeypatch, tmp_path: Path) -> str:
    """Seed a deterministic instance_id for bridge_name() derivation."""
    instance_dir = tmp_path / "nova-ve-instance"
    instance_dir.mkdir()
    value = "test-instance-uuid-202"
    (instance_dir / "instance_id").write_text(value)
    monkeypatch.setenv("NOVA_VE_INSTANCE_DIR", str(instance_dir))
    return value


@pytest.fixture()
def settings(monkeypatch, labs_dir):
    s = SimpleNamespace(LABS_DIR=labs_dir)
    monkeypatch.setattr(network_service_mod, "get_settings", lambda: s)
    monkeypatch.setattr("app.services.lab_service.get_settings", lambda: s)
    monkeypatch.setattr("app.services.link_service.get_settings", lambda: s)
    return s


@pytest.fixture()
def stub_publish(monkeypatch):
    captured: list[tuple] = []

    async def fake_publish(lab_id, event_type, payload, rev=""):
        captured.append((lab_id, event_type, payload))
        return SimpleNamespace(seq=len(captured), type=event_type, rev=rev, payload=payload)

    monkeypatch.setattr("app.services.ws_hub.ws_hub.publish", fake_publish)
    return captured


@pytest.fixture()
def helper_mocks(monkeypatch):
    """Capture every host_net call and disable real subprocess invocation."""
    calls = {"bridge_add": [], "bridge_del": [], "bridge_exists": []}

    def fake_add(name: str) -> None:
        calls["bridge_add"].append(name)

    def fake_del(name: str) -> None:
        calls["bridge_del"].append(name)

    def fake_exists(name: str) -> bool:
        calls["bridge_exists"].append(name)
        return False

    monkeypatch.setattr(host_net, "bridge_add", fake_add)
    monkeypatch.setattr(host_net, "bridge_del", fake_del)
    monkeypatch.setattr(host_net, "bridge_exists", fake_exists)
    return calls


def _seed_lab(labs_dir: Path, lab_id: str = "lab-uuid-aaa") -> str:
    name = "lab.json"
    (labs_dir / name).write_text(
        json.dumps(
            {
                "schema": 2,
                "id": lab_id,
                "meta": {"name": name},
                "viewport": {"x": 0, "y": 0, "zoom": 1.0},
                "nodes": {},
                "networks": {},
                "links": [],
                "defaults": {"link_style": "orthogonal"},
            }
        )
    )
    return name


# ---------------------------------------------------------------------------
# Happy path: bridge is provisioned with the canonical name
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_network_provisions_bridge(
    instance_id, settings, helper_mocks, stub_publish, labs_dir
):
    lab_name = _seed_lab(labs_dir, lab_id="lab-uuid-aaa")
    expected_bridge = host_net.bridge_name("lab-uuid-aaa", 1)

    payload = await NetworkService().create_network(lab_name, {"name": "lan"})

    # Bridge_add was invoked exactly once with the canonical name.
    assert helper_mocks["bridge_add"] == [expected_bridge]
    # Idempotency probe was performed.
    assert helper_mocks["bridge_exists"] == [expected_bridge]
    # runtime.bridge_name persisted on the response payload.
    assert payload["runtime"]["bridge_name"] == expected_bridge
    assert host_net.bridge_fingerprint_check(expected_bridge, "lab-uuid-aaa", 1) == "match"
    # And on disk.
    saved = json.loads((labs_dir / lab_name).read_text())
    assert saved["networks"]["1"]["runtime"]["bridge_name"] == expected_bridge


@pytest.mark.asyncio
async def test_create_network_bridge_name_uses_helper_format(
    instance_id, settings, helper_mocks, stub_publish, labs_dir
):
    """The bridge name is exactly ``host_net.bridge_name(lab_id, network_id)``.

    Pinned via the public helper so a future tweak to the naming scheme
    only has to change one place.
    """
    lab_name = _seed_lab(labs_dir, lab_id="another-lab")
    await NetworkService().create_network(lab_name, {"name": "n1"})
    [name] = helper_mocks["bridge_add"]
    assert name == host_net.bridge_name("another-lab", 1)
    # IFNAMSIZ-safe: ≤14 chars.
    assert len(name) <= 14
    assert name.startswith("nove")


# ---------------------------------------------------------------------------
# Cross-host collision resistance
# ---------------------------------------------------------------------------


def test_bridge_name_differs_across_instance_ids(monkeypatch, tmp_path):
    """Same lab_id on two hosts must produce different bridge names."""
    dir_a = tmp_path / "a"
    dir_a.mkdir()
    (dir_a / "instance_id").write_text("host-a-uuid")
    monkeypatch.setenv("NOVA_VE_INSTANCE_DIR", str(dir_a))
    name_a = host_net.bridge_name("shared-lab-id", 1)

    dir_b = tmp_path / "b"
    dir_b.mkdir()
    (dir_b / "instance_id").write_text("host-b-uuid")
    monkeypatch.setenv("NOVA_VE_INSTANCE_DIR", str(dir_b))
    name_b = host_net.bridge_name("shared-lab-id", 1)

    assert name_a != name_b, (
        "lab_hash collapsed across hosts — bridge names would collide!"
    )


# ---------------------------------------------------------------------------
# Idempotency: pre-existing bridge → no-op
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_network_idempotent_when_bridge_exists(
    instance_id, settings, monkeypatch, stub_publish, labs_dir
):
    lab_name = _seed_lab(labs_dir, lab_id="idem-lab")
    add_calls: list[str] = []
    bridge = host_net.bridge_name("idem-lab", 1)
    host_net.bridge_fingerprint_write(bridge, "idem-lab", 1)
    monkeypatch.setattr(host_net, "bridge_exists", lambda n: True)
    monkeypatch.setattr(
        host_net, "bridge_add", lambda n: add_calls.append(n)
    )
    monkeypatch.setattr(host_net, "bridge_del", lambda n: None)

    payload = await NetworkService().create_network(lab_name, {"name": "lan"})

    # Pre-existing bridge → bridge_add MUST NOT run.
    assert add_calls == []
    # The runtime.bridge_name is still stamped — caller must be able to
    # tear down the bridge later.
    assert payload["runtime"]["bridge_name"] == bridge


@pytest.mark.asyncio
async def test_create_network_raises_on_bridge_ownership_mismatch(
    instance_id, settings, monkeypatch, stub_publish, labs_dir
):
    lab_name = _seed_lab(labs_dir, lab_id="mismatch-lab")
    bridge = host_net.bridge_name("mismatch-lab", 1)
    host_net.bridge_fingerprint_write(bridge, "other-lab", 1)

    monkeypatch.setattr(host_net, "bridge_exists", lambda n: True)
    monkeypatch.setattr(host_net, "bridge_add", lambda n: None)
    monkeypatch.setattr(host_net, "bridge_del", lambda n: None)

    with pytest.raises(host_net.HostNetBridgeOwnershipError) as excinfo:
        await NetworkService().create_network(lab_name, {"name": "lan"})

    assert bridge in str(excinfo.value)
    assert "mismatch-lab" in str(excinfo.value)
    assert "other-lab" in str(excinfo.value)
    saved = json.loads((labs_dir / lab_name).read_text())
    assert saved["networks"] == {}


# ---------------------------------------------------------------------------
# Failure rollback: bridge_add raises → JSON record removed, 409 raised
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_network_rolls_back_on_bridge_add_failure(
    instance_id, settings, monkeypatch, stub_publish, labs_dir
):
    lab_name = _seed_lab(labs_dir, lab_id="fail-lab")

    def boom(name: str) -> None:
        raise host_net.HostNetEEXIST(
            "RTNETLINK answers: File exists", returncode=1, stderr="exists"
        )

    monkeypatch.setattr(host_net, "bridge_exists", lambda n: False)
    monkeypatch.setattr(host_net, "bridge_add", boom)
    monkeypatch.setattr(host_net, "bridge_del", lambda n: None)

    with pytest.raises(NetworkServiceError) as excinfo:
        await NetworkService().create_network(lab_name, {"name": "lan"})

    assert excinfo.value.code == 409
    # The lab.json was rolled back — no orphaned network record left over.
    saved = json.loads((labs_dir / lab_name).read_text())
    assert saved["networks"] == {}, "lab.json was not rolled back on failure!"


@pytest.mark.asyncio
async def test_create_network_rollback_on_validation_error(
    instance_id, settings, monkeypatch, stub_publish, labs_dir
):
    """A regex-rejection from the helper also triggers full rollback."""
    lab_name = _seed_lab(labs_dir, lab_id="val-lab")

    def reject(name: str) -> None:
        raise host_net.HostNetValidationError(
            "argument failed validation: bridge_name",
            returncode=2,
            stderr="argument failed validation: bridge_name",
        )

    monkeypatch.setattr(host_net, "bridge_exists", lambda n: False)
    monkeypatch.setattr(host_net, "bridge_add", reject)
    monkeypatch.setattr(host_net, "bridge_del", lambda n: None)

    with pytest.raises(NetworkServiceError) as excinfo:
        await NetworkService().create_network(lab_name, {"name": "lan"})

    assert excinfo.value.code == 409
    saved = json.loads((labs_dir / lab_name).read_text())
    assert saved["networks"] == {}


# ---------------------------------------------------------------------------
# delete_network tears the bridge down
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_network_removes_bridge(
    instance_id, settings, helper_mocks, stub_publish, labs_dir
):
    """``delete_network`` reads the persisted runtime.bridge_name and
    invokes ``bridge_del`` with that exact value."""
    lab_name = _seed_lab(labs_dir, lab_id="del-lab")
    await NetworkService().create_network(lab_name, {"name": "lan"})
    expected_bridge = host_net.bridge_name("del-lab", 1)

    helper_mocks["bridge_del"].clear()
    await NetworkService().delete_network(lab_name, 1)

    assert helper_mocks["bridge_del"] == [expected_bridge]
    saved = json.loads((labs_dir / lab_name).read_text())
    assert saved["networks"] == {}


@pytest.mark.asyncio
async def test_delete_network_tolerates_already_absent_bridge(
    instance_id, settings, monkeypatch, stub_publish, labs_dir
):
    """If the bridge has already been swept (US-206), delete_network
    completes successfully — the JSON record is the source of truth for
    the API response, the kernel state is best-effort cleanup."""
    lab_name = _seed_lab(labs_dir, lab_id="absent-lab")
    monkeypatch.setattr(host_net, "bridge_exists", lambda n: False)
    monkeypatch.setattr(host_net, "bridge_add", lambda n: None)

    await NetworkService().create_network(lab_name, {"name": "lan"})

    def gone(name: str) -> None:
        raise host_net.HostNetEINVAL(
            "Cannot find device", returncode=1, stderr="does not exist"
        )

    monkeypatch.setattr(host_net, "bridge_del", gone)

    # No exception — the network record is gone either way.
    removed = await NetworkService().delete_network(lab_name, 1)
    assert removed["id"] == 1
