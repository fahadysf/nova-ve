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
    monkeypatch.setattr("app.services.cloud_inventory_service.get_settings", lambda: s)
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
    calls = {
        "bridge_add": [],
        "bridge_del": [],
        "bridge_exists": [],
        "bridge_addr_add": [],
        "link_up": [],
        "ipv4_forward_enable": [],
        "nat_apply": [],
        "nat_remove": [],
        "forward_apply": [],
        "forward_remove": [],
        "dnsmasq_start": [],
        "dnsmasq_stop": [],
    }

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
    monkeypatch.setattr(host_net, "bridge_addr_add", lambda name, cidr: calls["bridge_addr_add"].append((name, cidr)))
    monkeypatch.setattr(host_net, "link_up", lambda iface: calls["link_up"].append(iface))
    monkeypatch.setattr(host_net, "default_egress_iface", lambda: "eth0")
    monkeypatch.setattr(host_net, "ipv4_forward_enable", lambda: calls["ipv4_forward_enable"].append(True))
    monkeypatch.setattr(host_net, "nat_apply", lambda bridge, cidr, egress: calls["nat_apply"].append((bridge, cidr, egress)))
    monkeypatch.setattr(host_net, "nat_remove", lambda bridge: calls["nat_remove"].append(bridge))
    monkeypatch.setattr(host_net, "forward_apply", lambda bridge, cidr, egress: calls["forward_apply"].append((bridge, cidr, egress)))
    monkeypatch.setattr(host_net, "forward_remove", lambda bridge, cidr=None, egress_iface=None: calls["forward_remove"].append((bridge, cidr, egress_iface)))
    monkeypatch.setattr(host_net, "dnsmasq_start", lambda bridge, gateway, start, end: calls["dnsmasq_start"].append((bridge, gateway, start, end)) or 1234)
    monkeypatch.setattr(host_net, "dnsmasq_stop", lambda bridge: calls["dnsmasq_stop"].append(bridge))
    return calls


def _seed_lab(labs_dir: Path, lab_id: str = "lab-uuid-aaa", *, name: str = "lab.json") -> str:
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
# US-401 — runtime.{driver, created_at} reconciliation metadata
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_network_stamps_runtime_driver_and_created_at(
    instance_id, settings, helper_mocks, stub_publish, labs_dir
):
    """US-401: ``create_network`` stamps ``runtime.driver`` and
    ``runtime.created_at`` so the reconciliation loop (US-402) and the
    backfill migration (US-202b) have a target to verify against."""
    from datetime import datetime as _dt, timezone as _tz

    lab_name = _seed_lab(labs_dir, lab_id="lab-runtime-meta")

    payload = await NetworkService().create_network(lab_name, {"name": "lan"})

    assert payload["runtime"]["driver"] == "linux_bridge"
    created_at = payload["runtime"]["created_at"]
    assert isinstance(created_at, str) and created_at, "created_at must be a non-empty ISO string"
    parsed = _dt.fromisoformat(created_at)
    assert parsed.tzinfo is not None, "created_at must be timezone-aware"
    # Sanity: must not be in the past beyond a reasonable bound.
    delta = (_dt.now(_tz.utc) - parsed).total_seconds()
    assert -5.0 < delta < 60.0, f"created_at {created_at!r} not close to now"

    # Round-trip: lab.json holds exactly what the API returned.
    saved = json.loads((labs_dir / lab_name).read_text())
    saved_runtime = saved["networks"]["1"]["runtime"]
    assert saved_runtime["driver"] == "linux_bridge"
    assert saved_runtime["created_at"] == created_at


@pytest.mark.asyncio
async def test_create_nat_cloud_allocates_subnet_and_provisions_l3(
    instance_id, settings, helper_mocks, stub_publish, labs_dir
):
    lab_name = _seed_lab(labs_dir, lab_id="lab-nat-cloud")
    expected_bridge = host_net.bridge_name("lab-nat-cloud", 1)

    payload = await NetworkService().create_network(
        lab_name,
        {"name": "internet", "type": "nat_cloud", "config": {}},
    )

    assert payload["type"] == "nat_cloud"
    assert payload["config"] == {
        "cidr": "10.255.0.0/24",
        "gateway": "10.255.0.1",
        "dhcp": True,
        "dhcp_start": "10.255.0.100",
        "dhcp_end": "10.255.0.254",
    }
    assert helper_mocks["bridge_addr_add"] == [(expected_bridge, "10.255.0.1/24")]
    assert helper_mocks["ipv4_forward_enable"] == [True]
    assert helper_mocks["nat_apply"] == [(expected_bridge, "10.255.0.0/24", "eth0")]
    assert helper_mocks["forward_apply"] == [(expected_bridge, "10.255.0.0/24", "eth0")]
    assert helper_mocks["dnsmasq_start"] == [
        (expected_bridge, "10.255.0.1", "10.255.0.100", "10.255.0.254")
    ]
    assert payload["runtime"]["driver"] == "nat_cloud"
    assert payload["runtime"]["egress_interface"] == "eth0"
    assert payload["runtime"]["dhcp_pid"] == 1234

    saved = json.loads((labs_dir / lab_name).read_text())
    assert saved["networks"]["1"]["runtime"]["nat"] == "nftables"


@pytest.mark.asyncio
async def test_create_nat_cloud_skips_overlapping_existing_cidr(
    instance_id, settings, helper_mocks, stub_publish, labs_dir
):
    lab_name = _seed_lab(labs_dir, lab_id="lab-nat-cloud-overlap")
    saved_path = labs_dir / lab_name
    data = json.loads(saved_path.read_text())
    data["networks"] = {
        "1": {
            "id": 1,
            "name": "existing",
            "type": "nat_cloud",
            "config": {"cidr": "10.255.0.0/24"},
            "runtime": {},
            "implicit": False,
            "visibility": True,
        }
    }
    saved_path.write_text(json.dumps(data))

    payload = await NetworkService().create_network(
        lab_name,
        {"name": "internet-2", "type": "nat_cloud", "config": {}},
    )

    assert payload["id"] == 2
    assert payload["config"]["cidr"] == "10.255.1.0/24"


@pytest.mark.asyncio
async def test_create_nat_cloud_skips_host_wide_existing_cidr(
    instance_id, settings, helper_mocks, stub_publish, labs_dir
):
    first_lab = _seed_lab(labs_dir, lab_id="lab-nat-owner-a", name="owner-a.json")
    second_lab = _seed_lab(labs_dir, lab_id="lab-nat-owner-b", name="owner-b.json")

    first = await NetworkService().create_network(
        first_lab,
        {"name": "internet", "type": "nat_cloud", "config": {}},
    )
    second = await NetworkService().create_network(
        second_lab,
        {"name": "internet", "type": "nat_cloud", "config": {}},
    )

    assert first["config"]["cidr"] == "10.255.0.0/24"
    assert second["config"]["cidr"] == "10.255.1.0/24"


@pytest.mark.asyncio
async def test_create_nat_cloud_reference_reuses_owner_bridge_without_provisioning(
    instance_id, settings, helper_mocks, stub_publish, labs_dir
):
    owner_lab = _seed_lab(labs_dir, lab_id="lab-nat-owner", name="owner.json")
    ref_lab = _seed_lab(labs_dir, lab_id="lab-nat-ref", name="ref.json")
    owner = await NetworkService().create_network(
        owner_lab,
        {"name": "internet", "type": "nat_cloud", "config": {}},
    )
    helper_mocks["bridge_add"].clear()
    helper_mocks["nat_apply"].clear()

    shared_cloud_id = "nat-cloud:lab-nat-owner:1"
    ref = await NetworkService().create_network(
        ref_lab,
        {
            "name": "shared-internet",
            "type": "nat_cloud",
            "config": {"shared_cloud_id": shared_cloud_id},
        },
    )

    assert ref["config"]["shared_cloud_id"] == shared_cloud_id
    assert ref["config"]["cidr"] == owner["config"]["cidr"]
    assert ref["runtime"]["bridge_name"] == owner["runtime"]["bridge_name"]
    assert ref["runtime"]["shared_reference"] is True
    assert helper_mocks["bridge_add"] == []
    assert helper_mocks["nat_apply"] == []


@pytest.mark.asyncio
async def test_shared_nat_cloud_allocates_and_releases_on_owner_ipam(
    instance_id, settings, helper_mocks, stub_publish, labs_dir
):
    owner_lab = _seed_lab(labs_dir, lab_id="lab-nat-ipam-owner", name="owner.json")
    ref_lab = _seed_lab(labs_dir, lab_id="lab-nat-ipam-ref", name="ref.json")
    await NetworkService().create_network(
        owner_lab,
        {"name": "internet", "type": "nat_cloud", "config": {}},
    )
    await NetworkService().create_network(
        ref_lab,
        {
            "name": "shared-internet",
            "type": "nat_cloud",
            "config": {"shared_cloud_id": "nat-cloud:lab-nat-ipam-owner:1"},
        },
    )
    svc = NetworkService()

    first = svc._allocate_ip(ref_lab, 1)
    second = svc._allocate_ip(owner_lab, 1)

    assert first == "10.255.0.2"
    assert second == "10.255.0.3"
    owner_saved = json.loads((labs_dir / owner_lab).read_text())
    ref_saved = json.loads((labs_dir / ref_lab).read_text())
    assert owner_saved["networks"]["1"]["runtime"]["used_ips"] == [
        "10.255.0.2",
        "10.255.0.3",
    ]
    assert ref_saved["networks"]["1"]["runtime"]["used_ips"] == []

    assert svc._release_ip(ref_lab, 1, first) is True
    owner_saved = json.loads((labs_dir / owner_lab).read_text())
    assert owner_saved["networks"]["1"]["runtime"]["used_ips"] == ["10.255.0.3"]


@pytest.mark.asyncio
async def test_shared_nat_cloud_release_with_lab_lock_held_same_lab_owner(
    instance_id, settings, helper_mocks, stub_publish, labs_dir
):
    lab_name = _seed_lab(labs_dir, lab_id="lab-nat-same-lab-ref")
    await NetworkService().create_network(
        lab_name,
        {"name": "internet", "type": "nat_cloud", "config": {}},
    )
    await NetworkService().create_network(
        lab_name,
        {
            "name": "shared-internet",
            "type": "nat_cloud",
            "config": {"shared_cloud_id": "nat-cloud:lab-nat-same-lab-ref:1"},
        },
    )
    svc = NetworkService()
    ip = svc._allocate_ip(lab_name, 2)
    assert ip == "10.255.0.2"

    from app.services.lab_lock import lab_lock

    with lab_lock(lab_name, settings.LABS_DIR, timeout_s=2.0):
        removed = svc._release_ip(lab_name, 2, ip, _lab_lock_held=True)

    assert removed is True
    saved = json.loads((labs_dir / lab_name).read_text())
    assert saved["networks"]["1"]["runtime"]["used_ips"] == []


@pytest.mark.asyncio
async def test_delete_nat_cloud_owner_rejects_external_references(
    instance_id, settings, helper_mocks, stub_publish, labs_dir
):
    owner_lab = _seed_lab(labs_dir, lab_id="lab-nat-delete-owner", name="owner.json")
    ref_lab = _seed_lab(labs_dir, lab_id="lab-nat-delete-ref", name="ref.json")
    await NetworkService().create_network(
        owner_lab,
        {"name": "internet", "type": "nat_cloud", "config": {}},
    )
    await NetworkService().create_network(
        ref_lab,
        {
            "name": "shared-internet",
            "type": "nat_cloud",
            "config": {"shared_cloud_id": "nat-cloud:lab-nat-delete-owner:1"},
        },
    )

    with pytest.raises(NetworkServiceError) as exc:
        await NetworkService().delete_network(owner_lab, 1)

    assert exc.value.code == 409
    assert "used by another lab" in exc.value.message


@pytest.mark.asyncio
async def test_create_nat_cloud_rejects_dhcp_overlap_with_static_range(
    instance_id, settings, helper_mocks, stub_publish, labs_dir
):
    lab_name = _seed_lab(labs_dir, lab_id="lab-nat-cloud-bad-dhcp")

    with pytest.raises(NetworkServiceError) as exc:
        await NetworkService().create_network(
            lab_name,
            {
                "name": "internet",
                "type": "nat_cloud",
                "config": {"cidr": "10.44.0.0/24", "dhcp_start": "10.44.0.50"},
            },
        )

    assert exc.value.code == 422
    assert "static range" in exc.value.message


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


# ---------------------------------------------------------------------------
# US-204c — IPAM free-list (used_ips, NOT a counter)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_network_seeds_empty_used_ips_freelist(
    instance_id, settings, helper_mocks, stub_publish, labs_dir
):
    """``runtime.used_ips`` and ``runtime.first_offset`` are persisted on
    create_network. Empty list (NOT a counter) is the seed value per
    plan §US-204c "IPAM data model (free-list, NOT a counter)"."""
    lab_name = _seed_lab(labs_dir, lab_id="ipam-seed-lab")

    payload = await NetworkService().create_network(
        lab_name, {"name": "lan", "config": {"cidr": "10.99.1.0/24"}}
    )

    assert payload["runtime"]["used_ips"] == []
    assert payload["runtime"]["first_offset"] == 2
    saved = json.loads((labs_dir / lab_name).read_text())
    assert saved["networks"]["1"]["runtime"]["used_ips"] == []
    assert saved["networks"]["1"]["runtime"]["first_offset"] == 2
    assert saved["networks"]["1"]["config"]["cidr"] == "10.99.1.0/24"


@pytest.mark.asyncio
async def test_create_network_rejects_invalid_cidr(
    instance_id, settings, helper_mocks, stub_publish, labs_dir
):
    """Invalid CIDR strings raise 422 BEFORE any kernel work happens."""
    lab_name = _seed_lab(labs_dir, lab_id="ipam-bad-cidr")

    with pytest.raises(NetworkServiceError) as excinfo:
        await NetworkService().create_network(
            lab_name, {"name": "lan", "config": {"cidr": "not-a-cidr"}}
        )
    assert excinfo.value.code == 422
    # No bridge work happened — validation gates BEFORE the lab lock.
    assert helper_mocks["bridge_add"] == []
    saved = json.loads((labs_dir / lab_name).read_text())
    assert saved["networks"] == {}


@pytest.mark.asyncio
async def test_create_network_rejects_ipv6_cidr(
    instance_id, settings, helper_mocks, stub_publish, labs_dir
):
    """IPv6 CIDRs raise 422 with the deferred-IPv6 §5 pointer."""
    lab_name = _seed_lab(labs_dir, lab_id="ipam-ipv6")

    with pytest.raises(NetworkServiceError) as excinfo:
        await NetworkService().create_network(
            lab_name, {"name": "lan", "config": {"cidr": "fd00::/64"}}
        )
    assert excinfo.value.code == 422
    assert "IPv6" in excinfo.value.message


@pytest.mark.asyncio
async def test_allocate_ip_returns_lowest_free_address(
    instance_id, settings, helper_mocks, stub_publish, labs_dir
):
    """Allocator picks the lowest-numbered free host IP (skipping .0
    network and .1 reserved per ``first_offset=2``)."""
    lab_name = _seed_lab(labs_dir, lab_id="ipam-low")
    await NetworkService().create_network(
        lab_name, {"name": "lan", "config": {"cidr": "10.99.1.0/24"}}
    )
    svc = NetworkService()

    first = svc._allocate_ip(lab_name, 1)
    second = svc._allocate_ip(lab_name, 1)
    third = svc._allocate_ip(lab_name, 1)

    # first_offset=2 → first allocation is .2, then .3, then .4.
    assert first == "10.99.1.2"
    assert second == "10.99.1.3"
    assert third == "10.99.1.4"

    saved = json.loads((labs_dir / lab_name).read_text())
    assert saved["networks"]["1"]["runtime"]["used_ips"] == [
        "10.99.1.2",
        "10.99.1.3",
        "10.99.1.4",
    ]


@pytest.mark.asyncio
async def test_release_ip_makes_address_reusable(
    instance_id, settings, helper_mocks, stub_publish, labs_dir
):
    """After release, the SAME address is the next allocation result —
    proves the free-list does not leak (the bug a counter would have)."""
    lab_name = _seed_lab(labs_dir, lab_id="ipam-release")
    await NetworkService().create_network(
        lab_name, {"name": "lan", "config": {"cidr": "10.99.1.0/24"}}
    )
    svc = NetworkService()

    a = svc._allocate_ip(lab_name, 1)  # .2
    b = svc._allocate_ip(lab_name, 1)  # .3
    assert a == "10.99.1.2" and b == "10.99.1.3"

    # Release the lower one; next allocation must reuse it.
    assert svc._release_ip(lab_name, 1, a) is True
    next_alloc = svc._allocate_ip(lab_name, 1)
    assert next_alloc == "10.99.1.2"

    # Releasing an absent IP is a no-op (idempotent for detach paths).
    assert svc._release_ip(lab_name, 1, "10.99.1.99") is False


@pytest.mark.asyncio
async def test_allocate_ip_thrashes_freelist_returns_to_baseline(
    instance_id, settings, helper_mocks, stub_publish, labs_dir
):
    """50 attach/detach cycles leave ``used_ips`` empty — the very leak
    a monotonic counter would create over /24 (250 cycles → exhausted)."""
    lab_name = _seed_lab(labs_dir, lab_id="ipam-thrash")
    await NetworkService().create_network(
        lab_name, {"name": "lan", "config": {"cidr": "10.99.1.0/24"}}
    )
    svc = NetworkService()

    for _ in range(50):
        ip = svc._allocate_ip(lab_name, 1)
        assert svc._release_ip(lab_name, 1, ip) is True

    saved = json.loads((labs_dir / lab_name).read_text())
    assert saved["networks"]["1"]["runtime"]["used_ips"] == []


@pytest.mark.asyncio
async def test_allocate_ip_exhausts_small_cidr_with_typed_error(
    instance_id, settings, helper_mocks, stub_publish, labs_dir
):
    """A /30 has hosts {.1, .2}; with first_offset=2 only .2 is usable —
    second allocation raises 409 ``subnet exhausted``."""
    lab_name = _seed_lab(labs_dir, lab_id="ipam-exhaust")
    await NetworkService().create_network(
        lab_name, {"name": "lan", "config": {"cidr": "10.99.2.0/30"}}
    )
    svc = NetworkService()

    only_addr = svc._allocate_ip(lab_name, 1)
    assert only_addr == "10.99.2.2"

    with pytest.raises(NetworkServiceError) as excinfo:
        svc._allocate_ip(lab_name, 1)
    assert excinfo.value.code == 409
    assert "exhausted" in excinfo.value.message.lower()


@pytest.mark.asyncio
async def test_allocate_ip_rejects_network_without_cidr(
    instance_id, settings, helper_mocks, stub_publish, labs_dir
):
    """Networks without ``config.cidr`` are L2-only — calling
    _allocate_ip raises 422 (caller should gate on cidr presence)."""
    lab_name = _seed_lab(labs_dir, lab_id="ipam-no-cidr")
    # No config -> L2-only.
    await NetworkService().create_network(lab_name, {"name": "lan"})
    svc = NetworkService()

    with pytest.raises(NetworkServiceError) as excinfo:
        svc._allocate_ip(lab_name, 1)
    assert excinfo.value.code == 422
    assert "config.cidr" in excinfo.value.message


@pytest.mark.asyncio
async def test_allocate_ip_persists_used_ips_into_lab_json(
    instance_id, settings, helper_mocks, stub_publish, labs_dir
):
    """Each allocation writes ``used_ips`` to lab.json under the lab
    flock — survives backend restart per plan §US-204c reconciliation."""
    lab_name = _seed_lab(labs_dir, lab_id="ipam-persist")
    await NetworkService().create_network(
        lab_name, {"name": "lan", "config": {"cidr": "10.99.3.0/29"}}
    )
    svc = NetworkService()

    svc._allocate_ip(lab_name, 1)
    svc._allocate_ip(lab_name, 1)

    # Re-read directly from disk — no in-memory caching is allowed to
    # mask a missing write.
    saved = json.loads((labs_dir / lab_name).read_text())
    assert saved["networks"]["1"]["runtime"]["used_ips"] == [
        "10.99.3.2",
        "10.99.3.3",
    ]


@pytest.mark.asyncio
async def test_allocate_ip_skips_externally_reserved_addresses(
    instance_id, settings, helper_mocks, stub_publish, labs_dir
):
    """Pre-existing entries in ``used_ips`` are honoured — allocator
    walks past them to the next free address, supporting reservations
    written by reconciliation or import."""
    lab_name = _seed_lab(labs_dir, lab_id="ipam-reserved")
    await NetworkService().create_network(
        lab_name, {"name": "lan", "config": {"cidr": "10.99.4.0/24"}}
    )
    # Manually pre-populate the free-list (e.g. backend-startup
    # reconciliation re-baselining live container IPs).
    raw = json.loads((labs_dir / lab_name).read_text())
    raw["networks"]["1"]["runtime"]["used_ips"] = ["10.99.4.2", "10.99.4.3"]
    (labs_dir / lab_name).write_text(json.dumps(raw))

    svc = NetworkService()
    chosen = svc._allocate_ip(lab_name, 1)

    # First free is .4 — .2 and .3 were both reserved.
    assert chosen == "10.99.4.4"


# ---------------------------------------------------------------------------
# ensure_lab_bridges — lab-load reconciliation
# ---------------------------------------------------------------------------


def _seed_lab_with_networks(
    labs_dir: Path, lab_id: str, networks: dict, *, name: str = "lab.json"
) -> str:
    (labs_dir / name).write_text(
        json.dumps(
            {
                "schema": 2,
                "id": lab_id,
                "meta": {"name": name},
                "viewport": {"x": 0, "y": 0, "zoom": 1.0},
                "nodes": {},
                "networks": networks,
                "links": [],
                "defaults": {"link_style": "orthogonal"},
            }
        )
    )
    return name


def test_ensure_lab_bridges_provisions_missing_bridge(
    instance_id, settings, helper_mocks, labs_dir
):
    """Network in lab.json with no host bridge → bridge_add runs and
    runtime.bridge_name is stamped on the network record."""
    lab_id = "ensure-lab-1"
    expected_bridge = host_net.bridge_name(lab_id, 1)
    lab_name = _seed_lab_with_networks(
        labs_dir,
        lab_id,
        {"1": {"id": 1, "name": "lan", "type": "linux_bridge"}},
    )

    summary = NetworkService().ensure_lab_bridges(lab_name)

    assert summary["created"] == [expected_bridge]
    assert summary["ensured"] == []
    assert summary["skipped"] == []
    assert helper_mocks["bridge_add"] == [expected_bridge]
    saved = json.loads((labs_dir / lab_name).read_text())
    assert saved["networks"]["1"]["runtime"]["bridge_name"] == expected_bridge


def test_ensure_lab_bridges_idempotent_when_bridge_already_exists(
    instance_id, settings, monkeypatch, labs_dir
):
    """Existing bridge with matching fingerprint → no bridge_add, classified
    under 'ensured'. Verifies the lab-open call is safe to fire on every
    page load without thrashing host state."""
    lab_id = "ensure-lab-2"
    bridge = host_net.bridge_name(lab_id, 1)
    host_net.bridge_fingerprint_write(bridge, lab_id, 1)

    add_calls: list[str] = []
    monkeypatch.setattr(host_net, "bridge_exists", lambda n: True)
    monkeypatch.setattr(host_net, "bridge_add", lambda n: add_calls.append(n))
    monkeypatch.setattr(host_net, "bridge_del", lambda n: None)

    lab_name = _seed_lab_with_networks(
        labs_dir,
        lab_id,
        {"1": {"id": 1, "name": "lan", "runtime": {"bridge_name": bridge}}},
    )

    summary = NetworkService().ensure_lab_bridges(lab_name)

    assert summary["ensured"] == [bridge]
    assert summary["created"] == []
    assert add_calls == []


def test_ensure_lab_bridges_skips_on_ownership_mismatch(
    instance_id, settings, monkeypatch, labs_dir
):
    """Existing bridge whose fingerprint maps to a different lab/network →
    refuse to claim it; report the mismatch in 'skipped'."""
    lab_id = "ensure-lab-3"
    bridge = host_net.bridge_name(lab_id, 1)
    host_net.bridge_fingerprint_write(bridge, "other-lab", 99)

    add_calls: list[str] = []
    monkeypatch.setattr(host_net, "bridge_exists", lambda n: True)
    monkeypatch.setattr(host_net, "bridge_add", lambda n: add_calls.append(n))

    lab_name = _seed_lab_with_networks(
        labs_dir,
        lab_id,
        {"1": {"id": 1, "name": "lan"}},
    )

    summary = NetworkService().ensure_lab_bridges(lab_name)

    assert summary["created"] == []
    assert summary["ensured"] == []
    assert len(summary["skipped"]) == 1
    skipped = summary["skipped"][0]
    assert skipped["bridge"] == bridge
    assert "ownership" in skipped["reason"].lower()
    assert add_calls == []


def test_ensure_lab_bridges_walks_multiple_networks(
    instance_id, settings, helper_mocks, labs_dir
):
    """Multiple networks → each gets its bridge provisioned and stamped."""
    lab_id = "ensure-lab-4"
    bridge_a = host_net.bridge_name(lab_id, 1)
    bridge_b = host_net.bridge_name(lab_id, 2)
    lab_name = _seed_lab_with_networks(
        labs_dir,
        lab_id,
        {
            "1": {"id": 1, "name": "lan-a"},
            "2": {"id": 2, "name": "lan-b"},
        },
    )

    summary = NetworkService().ensure_lab_bridges(lab_name)

    assert sorted(summary["created"]) == sorted([bridge_a, bridge_b])
    assert sorted(helper_mocks["bridge_add"]) == sorted([bridge_a, bridge_b])
    saved = json.loads((labs_dir / lab_name).read_text())
    assert saved["networks"]["1"]["runtime"]["bridge_name"] == bridge_a
    assert saved["networks"]["2"]["runtime"]["bridge_name"] == bridge_b


# ---------------------------------------------------------------------------
# ensure_lab_bridges — bridge auto-up when ports are connected
# ---------------------------------------------------------------------------


def _seed_lab_with_links(
    labs_dir: Path,
    lab_id: str,
    *,
    networks: dict,
    links: list,
    nodes: dict | None = None,
    name: str = "lab.json",
) -> str:
    (labs_dir / name).write_text(
        json.dumps(
            {
                "schema": 2,
                "id": lab_id,
                "meta": {"name": name},
                "viewport": {"x": 0, "y": 0, "zoom": 1.0},
                "nodes": nodes or {},
                "networks": networks,
                "links": links,
                "defaults": {"link_style": "orthogonal"},
            }
        )
    )
    return name


def test_ensure_lab_bridges_raises_bridge_with_link(
    instance_id, settings, helper_mocks, monkeypatch, labs_dir
):
    """A bridge whose network has at least one link endpoint is forced UP
    so its slave ports leave ``state disabled``."""
    lab_id = "ensure-lab-up-1"
    bridge = host_net.bridge_name(lab_id, 1)
    link_up_calls: list[str] = []
    monkeypatch.setattr(host_net, "link_up", lambda n: link_up_calls.append(n))
    lab_name = _seed_lab_with_links(
        labs_dir,
        lab_id,
        networks={"1": {"id": 1, "name": "lan"}},
        links=[
            {
                "id": "lnk_001",
                "from": {"node_id": 7, "interface_index": 0},
                "to": {"network_id": 1},
            }
        ],
    )

    summary = NetworkService().ensure_lab_bridges(lab_name)

    assert summary["created"] == [bridge]
    assert summary["raised"] == [bridge]
    assert link_up_calls == [bridge]


def test_ensure_lab_bridges_leaves_unused_bridge_down(
    instance_id, settings, helper_mocks, monkeypatch, labs_dir
):
    """A network with no link → bridge is created but NOT brought up."""
    lab_id = "ensure-lab-up-2"
    bridge = host_net.bridge_name(lab_id, 1)
    link_up_calls: list[str] = []
    monkeypatch.setattr(host_net, "link_up", lambda n: link_up_calls.append(n))
    lab_name = _seed_lab_with_links(
        labs_dir,
        lab_id,
        networks={"1": {"id": 1, "name": "lan"}},
        links=[],
    )

    summary = NetworkService().ensure_lab_bridges(lab_name)

    assert summary["created"] == [bridge]
    assert summary["raised"] == []
    assert link_up_calls == []


def test_ensure_lab_bridges_swallows_link_up_failure(
    instance_id, settings, helper_mocks, monkeypatch, labs_dir
):
    """``link_up`` failure is logged, not raised, so reconcile keeps walking."""
    lab_id = "ensure-lab-up-3"
    bridge = host_net.bridge_name(lab_id, 1)

    def boom(name: str) -> None:
        raise host_net.HostNetEINVAL("fake")

    monkeypatch.setattr(host_net, "link_up", boom)
    lab_name = _seed_lab_with_links(
        labs_dir,
        lab_id,
        networks={"1": {"id": 1, "name": "lan"}},
        links=[
            {
                "id": "lnk_001",
                "from": {"node_id": 1, "interface_index": 0},
                "to": {"network_id": 1},
            }
        ],
    )

    summary = NetworkService().ensure_lab_bridges(lab_name)

    assert summary["created"] == [bridge]
    assert summary["raised"] == []  # link_up failed; bridge not raised


# ---------------------------------------------------------------------------
# ensure_lab_bridges — QEMU NIC link state reconciliation
# ---------------------------------------------------------------------------


def test_ensure_lab_bridges_sets_qemu_nic_link_state_per_lab_links(
    instance_id, settings, helper_mocks, monkeypatch, labs_dir
):
    """Running QEMU node has set_link issued for every NIC index — UP for
    indices that appear in lab.links, DOWN for the rest."""
    from app.services import network_service as ns_mod

    lab_id = "ensure-nic-link-1"
    captured: list[tuple] = []

    class FakeRuntimeService:
        def _runtime_record(self, _lab_id, _node_id, *, include_stopped=False):
            return {"kind": "qemu", "qmp_socket": "/tmp/qmp.sock"}

        def set_qemu_nic_link(self, _lab_id, node_id, interface_index, *, up):
            captured.append((node_id, interface_index, up))
            return True, None

    monkeypatch.setattr(
        "app.services.node_runtime_service.NodeRuntimeService", FakeRuntimeService
    )

    lab_name = _seed_lab_with_links(
        labs_dir,
        lab_id,
        networks={"1": {"id": 1, "name": "lan"}},
        nodes={
            "3": {
                "id": 3,
                "name": "vyos-1",
                "type": "qemu",
                "ethernet": 4,
                "interfaces": [
                    {"name": "Gi1"},
                    {"name": "Gi2"},
                    {"name": "Gi3"},
                    {"name": "Gi4"},
                ],
            }
        },
        # Only Gi3 (interface_index=2) is connected.
        links=[
            {
                "id": "lnk_003",
                "from": {"node_id": 3, "interface_index": 2},
                "to": {"network_id": 1},
            }
        ],
    )

    summary = NetworkService().ensure_lab_bridges(lab_name)

    assert (3, 0, False) in captured
    assert (3, 1, False) in captured
    assert (3, 2, True) in captured
    assert (3, 3, False) in captured
    assert len(captured) == 4
    nic_state = {(e["interface_index"], e["up"]) for e in summary["nic_link_state"]}
    assert nic_state == {(0, False), (1, False), (2, True), (3, False)}


def test_ensure_lab_bridges_skips_qemu_nic_link_state_when_node_stopped(
    instance_id, settings, helper_mocks, monkeypatch, labs_dir
):
    """A QEMU node with no runtime record (stopped) → no QMP set_link calls."""
    from app.services import network_service as ns_mod

    lab_id = "ensure-nic-link-2"
    captured: list[tuple] = []

    class FakeRuntimeService:
        def _runtime_record(self, _lab_id, _node_id, *, include_stopped=False):
            return None

        def set_qemu_nic_link(self, _lab_id, node_id, interface_index, *, up):
            captured.append((node_id, interface_index, up))
            return True, None

    monkeypatch.setattr(
        "app.services.node_runtime_service.NodeRuntimeService", FakeRuntimeService
    )

    lab_name = _seed_lab_with_links(
        labs_dir,
        lab_id,
        networks={"1": {"id": 1, "name": "lan"}},
        nodes={
            "3": {
                "id": 3,
                "name": "vyos-1",
                "type": "qemu",
                "ethernet": 4,
            }
        },
        links=[],
    )

    summary = NetworkService().ensure_lab_bridges(lab_name)
    assert captured == []
    assert summary["nic_link_state"] == []


def test_ensure_lab_bridges_skips_non_qemu_nodes_for_set_link(
    instance_id, settings, helper_mocks, monkeypatch, labs_dir
):
    """Docker nodes use real veth pairs whose carrier already tracks
    attach state — set_link must not be called on them."""
    from app.services import network_service as ns_mod

    lab_id = "ensure-nic-link-3"
    captured: list[tuple] = []

    class FakeRuntimeService:
        def _runtime_record(self, _lab_id, _node_id, *, include_stopped=False):
            return {"kind": "docker"}

        def set_qemu_nic_link(self, _lab_id, node_id, interface_index, *, up):
            captured.append((node_id, interface_index, up))
            return True, None

    monkeypatch.setattr(
        "app.services.node_runtime_service.NodeRuntimeService", FakeRuntimeService
    )

    lab_name = _seed_lab_with_links(
        labs_dir,
        lab_id,
        networks={"1": {"id": 1, "name": "lan"}},
        nodes={
            "4": {
                "id": 4,
                "name": "docker-test-a",
                "type": "docker",
                "ethernet": 1,
            }
        },
        links=[],
    )

    NetworkService().ensure_lab_bridges(lab_name)
    assert captured == []


# ---------------------------------------------------------------------------
# Bridge-Cloud — create_network / delete_network / ensure_lab_bridges branches
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_bridge_cloud_validates_name(
    instance_id, settings, helper_mocks, stub_publish, labs_dir
):
    """AC8: invalid ``host_bridge`` name → NetworkServiceError(400)."""
    lab_name = _seed_lab(labs_dir, lab_id="bc-lab-A")
    with pytest.raises(NetworkServiceError) as ei:
        await NetworkService().create_network(
            lab_name,
            {"name": "bc1", "type": "bridge_cloud", "config": {"host_bridge": "br-foo"}},
        )
    assert ei.value.code == 400
    # Bridge_add MUST NOT have been called.
    assert helper_mocks["bridge_add"] == []


@pytest.mark.asyncio
async def test_create_bridge_cloud_rejects_missing_host_bridge(
    instance_id, settings, helper_mocks, stub_publish, labs_dir
):
    """AC8: missing ``host_bridge`` → NetworkServiceError(400)."""
    lab_name = _seed_lab(labs_dir, lab_id="bc-lab-B")
    with pytest.raises(NetworkServiceError) as ei:
        await NetworkService().create_network(
            lab_name,
            {"name": "bc2", "type": "bridge_cloud", "config": {}},
        )
    assert ei.value.code == 400
    assert helper_mocks["bridge_add"] == []


@pytest.mark.asyncio
async def test_create_bridge_cloud_404_when_host_bridge_missing(
    instance_id, settings, helper_mocks, stub_publish, labs_dir, monkeypatch
):
    """AC8: host bridge not present → NetworkServiceError(404)."""
    # bridge_exists default fixture returns False — perfect for this case.
    lab_name = _seed_lab(labs_dir, lab_id="bc-lab-C")
    with pytest.raises(NetworkServiceError) as ei:
        await NetworkService().create_network(
            lab_name,
            {"name": "bc3", "type": "bridge_cloud", "config": {"host_bridge": "br-eth0"}},
        )
    assert ei.value.code == 404
    assert helper_mocks["bridge_add"] == []


@pytest.mark.asyncio
async def test_create_bridge_cloud_skips_add_fingerprint_nat_dnsmasq(
    instance_id, settings, helper_mocks, stub_publish, labs_dir, monkeypatch
):
    """AC8: successful bridge_cloud create stamps runtime and skips every
    add/fingerprint/NAT/dnsmasq host call."""
    monkeypatch.setattr(host_net, "bridge_exists", lambda name: True)
    fingerprint_calls: list[str] = []
    monkeypatch.setattr(
        host_net,
        "bridge_fingerprint_write",
        lambda *a, **kw: fingerprint_calls.append(a[0]),
    )
    lab_name = _seed_lab(labs_dir, lab_id="bc-lab-D")

    payload = await NetworkService().create_network(
        lab_name,
        {"name": "bc-ok", "type": "bridge_cloud", "config": {"host_bridge": "br-eth0"}},
    )

    assert payload["runtime"]["bridge_name"] == "br-eth0"
    assert payload["runtime"]["driver"] == "bridge_cloud"
    assert "created_at" in payload["runtime"]
    # Negative assertions — no side effects on host bridge state.
    assert helper_mocks["bridge_add"] == []
    assert helper_mocks["nat_apply"] == []
    assert helper_mocks["dnsmasq_start"] == []
    assert fingerprint_calls == []


@pytest.mark.asyncio
async def test_delete_bridge_cloud_returns_409_with_active_attachments(
    instance_id, settings, helper_mocks, stub_publish, labs_dir, monkeypatch
):
    """AC9: refcount>0 → 409 (mirrors existing nat_cloud behavior)."""
    monkeypatch.setattr(host_net, "bridge_exists", lambda name: True)
    lab_name = _seed_lab(labs_dir, lab_id="bc-lab-E")
    await NetworkService().create_network(
        lab_name,
        {"name": "bc-link", "type": "bridge_cloud", "config": {"host_bridge": "br-eth0"}},
    )

    # Inject a fake link so refcount > 0.
    saved = json.loads((labs_dir / lab_name).read_text())
    saved["links"] = [
        {
            "id": "L1",
            "from": {"node_id": 99, "interface_index": 0},
            "to": {"network_id": 1},
        }
    ]
    (labs_dir / lab_name).write_text(json.dumps(saved))

    with pytest.raises(NetworkServiceError) as ei:
        await NetworkService().delete_network(lab_name, 1)
    assert ei.value.code == 409
    # bridge_del MUST NOT have been invoked.
    assert helper_mocks["bridge_del"] == []


@pytest.mark.asyncio
async def test_delete_bridge_cloud_skips_bridge_del_when_refcount_zero(
    instance_id, settings, helper_mocks, stub_publish, labs_dir, monkeypatch
):
    """AC9: clean delete preserves the host-owned bridge."""
    monkeypatch.setattr(host_net, "bridge_exists", lambda name: True)
    lab_name = _seed_lab(labs_dir, lab_id="bc-lab-F")
    await NetworkService().create_network(
        lab_name,
        {"name": "bc-clean", "type": "bridge_cloud", "config": {"host_bridge": "br-eth0"}},
    )
    helper_mocks["bridge_del"].clear()

    await NetworkService().delete_network(lab_name, 1)

    assert helper_mocks["bridge_del"] == []


def test_ensure_lab_bridges_skips_fingerprint_for_bridge_cloud(
    instance_id, settings, helper_mocks, monkeypatch, labs_dir
):
    """AC8: verify-only behavior for bridge_cloud records."""
    monkeypatch.setattr(host_net, "bridge_exists", lambda name: True)
    fingerprint_calls: list[str] = []
    monkeypatch.setattr(
        host_net,
        "bridge_fingerprint_check",
        lambda *a, **kw: fingerprint_calls.append("check") or "match",
    )
    monkeypatch.setattr(
        host_net,
        "bridge_fingerprint_write",
        lambda *a, **kw: fingerprint_calls.append("write"),
    )

    lab_name = _seed_lab_with_links(
        labs_dir,
        "bc-lab-G",
        networks={
            "1": {
                "id": 1,
                "name": "bc-net",
                "type": "bridge_cloud",
                "runtime": {"bridge_name": "br-eth0", "driver": "bridge_cloud"},
            }
        },
        links=[],
    )

    summary = NetworkService().ensure_lab_bridges(lab_name)
    assert "br-eth0" in summary["ensured"]
    assert helper_mocks["bridge_add"] == []
    assert fingerprint_calls == []


def test_ensure_lab_bridges_reports_missing_host_bridge_clearly(
    instance_id, settings, helper_mocks, monkeypatch, labs_dir
):
    """AC6 adjacent: a bridge_cloud network whose host bridge is missing
    appears in ``skipped`` with the provisioning hint."""
    monkeypatch.setattr(host_net, "bridge_exists", lambda name: False)

    lab_name = _seed_lab_with_links(
        labs_dir,
        "bc-lab-H",
        networks={
            "2": {
                "id": 2,
                "name": "bc-net-2",
                "type": "bridge_cloud",
                "runtime": {"bridge_name": "br-eth1", "driver": "bridge_cloud"},
            }
        },
        links=[],
    )

    summary = NetworkService().ensure_lab_bridges(lab_name)
    assert helper_mocks["bridge_add"] == []
    skipped_bridges = [item["bridge"] for item in summary["skipped"]]
    assert "br-eth1" in skipped_bridges
    reasons = [item["reason"] for item in summary["skipped"] if item["bridge"] == "br-eth1"]
    assert any("host-bridge-missing" in r for r in reasons)


# ---------------------------------------------------------------------------
# Bridge-Cloud — patch_network forgery refusal (codex iter-2 finding)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_patch_network_refuses_type_transition_to_bridge_cloud(
    instance_id, settings, helper_mocks, stub_publish, labs_dir
):
    """PATCH cannot upgrade an existing linux_bridge network to
    bridge_cloud — only ``create_network`` runs the regex + bridge_exists
    validation that protects the host LAN."""
    lab_name = _seed_lab_with_links(
        labs_dir,
        "patch-bc-A",
        networks={
            "1": {
                "id": 1,
                "name": "lan",
                "type": "linux_bridge",
                "config": {},
                "runtime": {"bridge_name": "nove1234n1", "driver": "linux_bridge"},
            }
        },
        links=[],
    )

    with pytest.raises(NetworkServiceError) as ei:
        await NetworkService().patch_network(
            lab_name, 1, {"type": "bridge_cloud", "config": {"host_bridge": "br-eth0"}}
        )
    assert ei.value.code == 422
    # Lab record untouched.
    saved = json.loads((labs_dir / lab_name).read_text())
    assert saved["networks"]["1"]["type"] == "linux_bridge"
    assert saved["networks"]["1"]["runtime"]["driver"] == "linux_bridge"


@pytest.mark.asyncio
async def test_patch_network_refuses_type_transition_from_bridge_cloud(
    instance_id, settings, helper_mocks, stub_publish, labs_dir
):
    """The inverse: cannot downgrade a bridge_cloud record either — the
    runtime ownership metadata would no longer match the network type."""
    lab_name = _seed_lab_with_links(
        labs_dir,
        "patch-bc-B",
        networks={
            "1": {
                "id": 1,
                "name": "lan",
                "type": "bridge_cloud",
                "config": {"host_bridge": "br-eth0"},
                "runtime": {"bridge_name": "br-eth0", "driver": "bridge_cloud"},
            }
        },
        links=[],
    )

    with pytest.raises(NetworkServiceError) as ei:
        await NetworkService().patch_network(lab_name, 1, {"type": "linux_bridge"})
    assert ei.value.code == 422


@pytest.mark.asyncio
async def test_patch_network_refuses_host_bridge_on_non_bridge_cloud(
    instance_id, settings, helper_mocks, stub_publish, labs_dir
):
    """``config.host_bridge`` is only valid on bridge_cloud records;
    setting it elsewhere is meaningless and a smell of forgery."""
    lab_name = _seed_lab_with_links(
        labs_dir,
        "patch-bc-C",
        networks={
            "1": {
                "id": 1,
                "name": "lan",
                "type": "linux_bridge",
                "config": {},
                "runtime": {"bridge_name": "nove1234n1", "driver": "linux_bridge"},
            }
        },
        links=[],
    )

    with pytest.raises(NetworkServiceError) as ei:
        await NetworkService().patch_network(
            lab_name, 1, {"config": {"host_bridge": "br-eth0"}}
        )
    assert ei.value.code == 422


@pytest.mark.asyncio
async def test_patch_network_refuses_config_mutation_on_bridge_cloud(
    instance_id, settings, helper_mocks, stub_publish, labs_dir
):
    """Bridge-Cloud is create-only.  Even on an existing bridge_cloud
    record, ``config`` mutations are refused — re-targeting the host
    bridge requires delete + recreate to keep runtime/links consistent.
    """
    lab_name = _seed_lab_with_links(
        labs_dir,
        "patch-bc-E",
        networks={
            "1": {
                "id": 1,
                "name": "bc",
                "type": "bridge_cloud",
                "config": {"host_bridge": "br-eth0"},
                "runtime": {"bridge_name": "br-eth0", "driver": "bridge_cloud"},
            }
        },
        links=[],
    )

    # Even pointing at a different valid host bridge name is refused.
    with pytest.raises(NetworkServiceError) as ei:
        await NetworkService().patch_network(
            lab_name, 1, {"config": {"host_bridge": "br-eth1"}}
        )
    assert ei.value.code == 422

    # And an empty config patch is refused for the same reason.
    with pytest.raises(NetworkServiceError) as ei:
        await NetworkService().patch_network(lab_name, 1, {"config": {}})
    assert ei.value.code == 422

    # Lab record untouched.
    saved = json.loads((labs_dir / lab_name).read_text())
    assert saved["networks"]["1"]["config"] == {"host_bridge": "br-eth0"}
    assert saved["networks"]["1"]["runtime"]["bridge_name"] == "br-eth0"


@pytest.mark.asyncio
async def test_patch_network_refuses_runtime_id_implicit_mutation(
    instance_id, settings, helper_mocks, stub_publish, labs_dir
):
    """PATCH cannot touch service-managed fields."""
    lab_name = _seed_lab_with_links(
        labs_dir,
        "patch-bc-D",
        networks={
            "1": {
                "id": 1,
                "name": "lan",
                "type": "linux_bridge",
                "config": {},
                "runtime": {"bridge_name": "nove1234n1", "driver": "linux_bridge"},
            }
        },
        links=[],
    )

    for forbidden_patch in (
        {"runtime": {"driver": "bridge_cloud", "bridge_name": "br-eth0"}},
        {"id": 999},
        {"implicit": True},
    ):
        with pytest.raises(NetworkServiceError) as ei:
            await NetworkService().patch_network(lab_name, 1, forbidden_patch)
        assert ei.value.code == 422
