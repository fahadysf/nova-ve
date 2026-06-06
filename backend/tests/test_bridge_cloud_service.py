# Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for ``app.services.bridge_cloud_service``.

Covers AC10a from .omc/plans/bridge-cloud-feature.md §3: list filters
to ``^br-eth[0-9]+$``, includes carrier+addrs, and uses a 5s cache.
"""

from __future__ import annotations

import asyncio
import subprocess
from types import SimpleNamespace

import pytest

from app.services import bridge_cloud_service as bcs


def _run_async(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


@pytest.fixture
def svc(monkeypatch):
    service = bcs.BridgeCloudService()
    # Inject our own subprocess + sys reads via monkeypatch in each test.
    yield service


def _mk_proc(stdout: str) -> SimpleNamespace:
    return SimpleNamespace(stdout=stdout, stderr="", returncode=0)


def test_list_filters_to_br_eth(monkeypatch, svc):
    monkeypatch.setattr(
        bcs.os, "listdir", lambda path: [
            "lo", "eth0", "docker0", "nove1234n1", "br-eth0", "br-eth10", "br-foo",
        ]
    )
    monkeypatch.setattr(bcs.BridgeCloudService, "_read_carrier", staticmethod(lambda b: True))
    monkeypatch.setattr(
        bcs.BridgeCloudService,
        "_collect_addrs",
        staticmethod(lambda: {"br-eth0": ["192.168.1.42/24"], "br-eth10": ["10.1.0.1/24"]}),
    )

    result = asyncio.run(svc.list())

    names = [item["host_bridge"] for item in result]
    assert names == ["br-eth0", "br-eth10"]
    by_name = {item["host_bridge"]: item for item in result}
    assert by_name["br-eth0"]["iface"] == "eth0"
    assert by_name["br-eth0"]["id"] == "bridge_cloud_eth0"
    assert by_name["br-eth0"]["label"] == "Bridge-Cloud-eth0"
    assert by_name["br-eth0"]["carrier"] is True
    assert by_name["br-eth0"]["addrs"] == ["192.168.1.42/24"]


def test_list_returns_no_carrier_when_sysfs_missing(monkeypatch, svc):
    monkeypatch.setattr(bcs.os, "listdir", lambda path: ["br-eth0"])
    monkeypatch.setattr(bcs.BridgeCloudService, "_collect_addrs", staticmethod(lambda: {}))
    # Default carrier read returns False on OSError.
    def boom(_self_or_bridge):
        raise OSError("nope")
    # Override the static reader by monkeypatching open():
    monkeypatch.setattr(bcs, "open", lambda *a, **k: (_ for _ in ()).throw(OSError()), raising=False)
    result = asyncio.run(svc.list())
    assert result[0]["carrier"] is False
    assert result[0]["addrs"] == []


def test_list_cache_hit_within_5s(monkeypatch, svc):
    calls = {"count": 0}
    def fake_listdir(_path):
        calls["count"] += 1
        return ["br-eth0"]
    monkeypatch.setattr(bcs.os, "listdir", fake_listdir)
    monkeypatch.setattr(bcs.BridgeCloudService, "_read_carrier", staticmethod(lambda b: True))
    monkeypatch.setattr(bcs.BridgeCloudService, "_collect_addrs", staticmethod(lambda: {}))

    asyncio.run(svc.list())
    asyncio.run(svc.list())
    asyncio.run(svc.list())

    assert calls["count"] == 1, "expected single sys read across 3 rapid calls (cache)"


def test_list_cache_expires_after_ttl(monkeypatch, svc):
    monkeypatch.setattr(bcs.os, "listdir", lambda p: ["br-eth0"])
    monkeypatch.setattr(bcs.BridgeCloudService, "_read_carrier", staticmethod(lambda b: True))
    monkeypatch.setattr(bcs.BridgeCloudService, "_collect_addrs", staticmethod(lambda: {}))

    fake_time = {"value": 1000.0}
    monkeypatch.setattr(bcs.time, "monotonic", lambda: fake_time["value"])

    asyncio.run(svc.list())
    fake_time["value"] += 10.0  # advance past 5s TTL
    # If cache expired, internal _collect_sync should run again — we can
    # verify by changing the listdir mock between calls.
    seen = []
    def listdir_after_expire(_p):
        seen.append("called")
        return ["br-eth1"]
    monkeypatch.setattr(bcs.os, "listdir", listdir_after_expire)
    second = asyncio.run(svc.list())
    assert seen, "cache should have expired and re-read"
    assert second[0]["host_bridge"] == "br-eth1"


def test_collect_addrs_parses_ip_br_output(monkeypatch, svc):
    sample = (
        "lo               UNKNOWN        127.0.0.1/8 ::1/128\n"
        "br-eth0          UP             192.168.1.10/24 fe80::1/64\n"
        "br-eth1          DOWN           \n"
        "nove1234n1       UP             10.0.0.1/24\n"
    )
    monkeypatch.setattr(bcs.shutil, "which", lambda name: "/usr/sbin/ip")
    monkeypatch.setattr(bcs.os.path, "exists", lambda p: True)
    monkeypatch.setattr(
        bcs.subprocess,
        "run",
        lambda *a, **k: _mk_proc(sample),
    )
    result = bcs.BridgeCloudService._collect_addrs()
    assert result == {
        "br-eth0": ["192.168.1.10/24", "fe80::1/64"],
        "br-eth1": [],
    }


def test_collect_addrs_empty_on_subprocess_error(monkeypatch, svc):
    monkeypatch.setattr(bcs.shutil, "which", lambda name: "/usr/sbin/ip")
    monkeypatch.setattr(bcs.os.path, "exists", lambda p: True)
    def boom(*a, **k):
        raise OSError("denied")
    monkeypatch.setattr(bcs.subprocess, "run", boom)
    assert bcs.BridgeCloudService._collect_addrs() == {}


# ---------------------------------------------------------------------------
# HTTP route smoke — exercises Depends(get_current_user) + cached service.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_route_returns_payload_with_auth(monkeypatch):
    from types import SimpleNamespace
    from httpx import ASGITransport, AsyncClient
    from app.main import app
    from app.dependencies import get_current_user

    # Reset the singleton cache so the test's monkeypatches take effect.
    bcs._service_for_test()._reset_cache_for_test()
    monkeypatch.setattr(bcs.os, "listdir", lambda p: ["br-eth0"])
    monkeypatch.setattr(bcs.BridgeCloudService, "_read_carrier", staticmethod(lambda b: True))
    monkeypatch.setattr(bcs.BridgeCloudService, "_collect_addrs", staticmethod(lambda: {"br-eth0": ["10.0.0.5/24"]}))

    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=1, username="tester")
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/api/system/bridge-clouds")
    finally:
        app.dependency_overrides.clear()
        bcs._service_for_test()._reset_cache_for_test()

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["status"] == "success"
    assert payload["data"] == [
        {
            "id": "bridge_cloud_eth0",
            "label": "Bridge-Cloud-eth0",
            "host_bridge": "br-eth0",
            "iface": "eth0",
            "carrier": True,
            "addrs": ["10.0.0.5/24"],
        }
    ]


@pytest.mark.asyncio
async def test_route_requires_auth():
    from httpx import ASGITransport, AsyncClient
    from app.main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/system/bridge-clouds")
    # Either 401 or 403 acceptable depending on auth dep behaviour.
    assert resp.status_code in (401, 403), resp.status_code


@pytest.mark.asyncio
async def test_cloud_inventory_route_requires_admin_and_lists_nat_clouds(monkeypatch, tmp_path):
    import json
    from httpx import ASGITransport, AsyncClient
    from app.dependencies import get_current_admin
    from app.main import app
    from app.routers import system as system_router

    labs_dir = tmp_path / "labs"
    labs_dir.mkdir()
    (labs_dir / "owner.json").write_text(
        json.dumps(
            {
                "schema": 2,
                "id": "owner-lab",
                "meta": {"name": "Owner Lab"},
                "networks": {
                    "1": {
                        "id": 1,
                        "name": "Internet",
                        "type": "nat_cloud",
                        "config": {
                            "cidr": "10.255.7.0/24",
                            "gateway": "10.255.7.1",
                            "dhcp": True,
                            "dhcp_start": "10.255.7.100",
                            "dhcp_end": "10.255.7.254",
                        },
                        "runtime": {"bridge_name": "noveownn1"},
                    }
                },
            }
        )
    )
    (labs_dir / "ref.json").write_text(
        json.dumps(
            {
                "schema": 2,
                "id": "ref-lab",
                "meta": {"name": "Ref Lab"},
                "networks": {
                    "1": {
                        "id": 1,
                        "name": "Shared Internet",
                        "type": "nat_cloud",
                        "config": {"shared_cloud_id": "nat-cloud:owner-lab:1"},
                        "runtime": {"bridge_name": "noveownn1"},
                    }
                },
            }
        )
    )

    settings = SimpleNamespace(LABS_DIR=labs_dir)
    monkeypatch.setattr("app.services.cloud_inventory_service.get_settings", lambda: settings)

    async def fake_bridge_clouds():
        return []

    monkeypatch.setattr(system_router, "list_bridge_clouds", fake_bridge_clouds)

    app.dependency_overrides[get_current_admin] = lambda: SimpleNamespace(
        id=1,
        username="admin",
        role="admin",
    )
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/api/system/clouds")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["bridge_clouds"] == []
    assert data["shared_bridges"] == []
    owner = next(item for item in data["nat_clouds"] if item["is_reference"] is False)
    reference = next(item for item in data["nat_clouds"] if item["is_reference"] is True)
    assert owner["id"] == "nat-cloud:owner-lab:1"
    assert owner["cidr"] == "10.255.7.0/24"
    assert owner["safe_for_reuse"] is True
    assert reference["shared_cloud_id"] == "nat-cloud:owner-lab:1"
