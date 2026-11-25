# Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
# SPDX-License-Identifier: Apache-2.0

"""#318 network info and #319 cross-lab shared networks."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

from app.dependencies import get_current_user
from app.main import app
from app.routers.labs import _first_mac_for_node
from app.services import host_net, network_info_service
from app.services import network_service as network_service_mod
from app.services.cloud_inventory_service import SharedCloudReferenceError
from app.services.lab_service import _cleanup_lab_network_runtime
from app.services.link_service import LinkService
from app.services.network_info_service import build_network_info
from app.services.network_service import NetworkService, NetworkServiceError
from app.services.node_runtime_service import NodeRuntimeService
from app.services.shared_network_service import (
    assert_no_external_members_for_scope,
    list_shared_networks,
)

OWNER_ID = "lab-owner"
MEMBER_ID = "lab-member"
SHARED_ID = f"shared-net:{OWNER_ID}:1"


@pytest.fixture()
def labs_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "labs"
    directory.mkdir()
    return directory


@pytest.fixture()
def host(monkeypatch, tmp_path):
    """Fake kernel: a set of bridges plus the calls that changed it."""
    instance = tmp_path / "instance"
    instance.mkdir()
    (instance / "instance_id").write_text("test-instance")
    monkeypatch.setenv("NOVA_VE_INSTANCE_DIR", str(instance))
    monkeypatch.setenv("NOVA_VE_BRIDGE_FINGERPRINT_ROOT", str(tmp_path / "bridges"))
    state = SimpleNamespace(bridges=set(), deleted=[], nat=[])

    def bridge_del(name):
        state.bridges.discard(name)
        state.deleted.append(name)

    monkeypatch.setattr(host_net, "bridge_exists", lambda name: name in state.bridges)
    monkeypatch.setattr(host_net, "bridge_add", state.bridges.add)
    monkeypatch.setattr(host_net, "bridge_del", bridge_del)
    monkeypatch.setattr(host_net, "link_state", lambda name: (True, None, True))
    for name in (
        "bridge_addr_add", "link_up", "ipv4_forward_enable", "nat_remove",
        "forward_apply", "forward_remove", "dnsmasq_stop",
    ):
        monkeypatch.setattr(host_net, name, lambda *a, **k: None)
    monkeypatch.setattr(host_net, "nat_apply", lambda bridge, cidr, egress: state.nat.append(bridge))
    monkeypatch.setattr(host_net, "default_egress_iface", lambda: "eth0")
    monkeypatch.setattr(host_net, "list_ipv4_addrs", lambda: [])
    monkeypatch.setattr(host_net, "dnsmasq_start", lambda *a: 4321)
    return state


@pytest.fixture()
def settings(monkeypatch, labs_dir, host):
    value = SimpleNamespace(LABS_DIR=labs_dir, BASE_DATA_DIR=labs_dir.parent, TMP_DIR=labs_dir.parent / "tmp", NAT_CLOUD_POOL="10.255.0.0/16")
    for module in (
        "network_service", "cloud_inventory_service", "lab_service", "link_service", "node_runtime_service",
    ):
        monkeypatch.setattr(f"app.services.{module}.get_settings", lambda: value)

    async def publish(*args, **kwargs):
        return None

    monkeypatch.setattr("app.services.ws_hub.ws_hub.publish", publish)
    return value


def _lab(labs_dir: Path, name: str, lab_id: str, *, nodes=None, links=None) -> str:
    path = labs_dir / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "schema": 2, "id": lab_id, "meta": {"name": lab_id},
        "viewport": {"x": 0, "y": 0, "zoom": 1.0},
        "nodes": nodes or {}, "networks": {}, "links": links or [],
        "defaults": {"link_style": "orthogonal"},
    }))
    return name


def _saved(labs_dir: Path, name: str) -> dict:
    return json.loads((labs_dir / name).read_text())


def _edit(labs_dir: Path, name: str, change) -> None:
    data = _saved(labs_dir, name)
    change(data)
    (labs_dir / name).write_text(json.dumps(data))


def _node(firstmac: str | None, name="r1", kind="qemu") -> dict:
    return {
        "id": 1, "name": name, "type": kind, "firstmac": firstmac,
        "interfaces": [{"index": 0, "name": "Gi0", "planned_mac": None}],
    }


LINK = [{"id": "l1", "from": {"node_id": 1, "interface_index": 0}, "to": {"network_id": 1}}]


async def _pair(labs_dir, *, net_type="linux_bridge", config=None):
    """Owner lab with one shared network, joined by a member lab."""
    svc = NetworkService()
    owner = _lab(labs_dir, "owner.json", OWNER_ID)
    member = _lab(labs_dir, "member.json", MEMBER_ID)
    owned = await svc.create_network(owner, {"name": "core", "type": net_type, "config": config or {}})
    await svc.set_sharing(owner, 1, True)
    joined = await svc.create_network(
        member, {"name": "core-link", "config": {"shared_network_id": SHARED_ID}}
    )
    return svc, owner, member, owned, joined


# --------------------------------------------------------------------------
# Joining
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_member_uses_owner_bridge(settings, host, labs_dir):
    _svc, _owner, member, owned, joined = await _pair(labs_dir)

    bridge = owned["runtime"]["bridge_name"]
    assert joined["runtime"]["bridge_name"] == bridge
    assert joined["runtime"]["shared_reference"] is True
    assert joined["type"] == "linux_bridge"
    assert host.bridges == {bridge}
    # The path every node start and hot-attach takes.
    data = _saved(labs_dir, member)
    assert LinkService._resolve_bridge_name(
        lab_id=MEMBER_ID, network_id=1, networks=data["networks"], implicit_bridge=None
    ) == bridge
    del data["networks"]["1"]["runtime"]["bridge_name"]
    assert LinkService._resolve_bridge_name(
        lab_id=MEMBER_ID, network_id=1, networks=data["networks"], implicit_bridge=None
    ) == bridge


@pytest.mark.asyncio
async def test_join_requires_sharing(settings, labs_dir):
    svc = NetworkService()
    owner = _lab(labs_dir, "owner.json", OWNER_ID)
    member = _lab(labs_dir, "member.json", MEMBER_ID)
    await svc.create_network(owner, {"name": "core"})
    request = {"name": "x", "config": {"shared_network_id": SHARED_ID}}

    assert list_shared_networks() == []
    with pytest.raises(NetworkServiceError) as exc:
        await svc.create_network(member, request)
    assert exc.value.code == 404
    # A private network is equally out of reach through the NAT-Cloud id form.
    with pytest.raises(NetworkServiceError):
        await svc.create_network(
            member, {"name": "x", "config": {"shared_network_id": f"nat-cloud:{OWNER_ID}:1"}}
        )
    assert _saved(labs_dir, member)["networks"] == {}

    await svc.set_sharing(owner, 1, True)
    assert [(n["id"], n["lab_name"], n["name"]) for n in list_shared_networks()] == [
        (SHARED_ID, OWNER_ID, "core")
    ]
    await svc.create_network(member, request)


@pytest.mark.asyncio
async def test_cannot_join_own_or_already_joined_network(settings, labs_dir):
    svc, owner, member, _owned, _joined = await _pair(labs_dir)
    for lab in (owner, member):
        with pytest.raises(NetworkServiceError) as exc:
            await svc.create_network(lab, {"name": "again", "config": {"shared_network_id": SHARED_ID}})
        assert exc.value.code == 409


@pytest.mark.asyncio
async def test_failed_join_leaves_no_record(settings, host, labs_dir):
    svc = NetworkService()
    owner = _lab(labs_dir, "owner.json", OWNER_ID)
    member = _lab(labs_dir, "member.json", MEMBER_ID)
    owned = await svc.create_network(owner, {"name": "core"})
    await svc.set_sharing(owner, 1, True)
    host_net.bridge_fingerprint_write(owned["runtime"]["bridge_name"], "someone-else", 9)

    with pytest.raises(host_net.HostNetBridgeOwnershipError):
        await svc.create_network(member, {"name": "x", "config": {"shared_network_id": SHARED_ID}})
    assert _saved(labs_dir, member)["networks"] == {}


@pytest.mark.asyncio
async def test_shared_nat_cloud_keeps_one_subnet_and_pool(settings, host, labs_dir):
    svc, owner, member, owned, joined = await _pair(labs_dir, net_type="nat_cloud")

    assert joined["type"] == "nat_cloud"
    assert joined["config"]["cidr"] == owned["config"]["cidr"]
    assert joined["config"]["shared_cloud_id"] == f"nat-cloud:{OWNER_ID}:1"
    assert joined["runtime"]["bridge_name"] == owned["runtime"]["bridge_name"]
    assert svc._allocate_ip(member, 1) == "10.255.0.2"
    assert svc._allocate_ip(owner, 1) == "10.255.0.3"


@pytest.mark.asyncio
async def test_shared_bridge_with_cidr_allocates_from_owner(settings, labs_dir):
    svc, owner, member, _owned, _joined = await _pair(labs_dir, config={"cidr": "10.9.0.0/24"})
    first = svc._allocate_ip(member, 1)
    assert (first, svc._allocate_ip(owner, 1)) == ("10.9.0.2", "10.9.0.3")
    assert svc._release_ip(member, 1, first) is True
    assert svc._allocate_ip(owner, 1) == first


# --------------------------------------------------------------------------
# Lifecycle guards
# --------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("net_type", ["linux_bridge", "nat_cloud"])
async def test_owner_is_pinned_while_joined(settings, host, labs_dir, net_type):
    svc, owner, member, owned, _joined = await _pair(labs_dir, net_type=net_type)

    for action in (svc.delete_network(owner, 1), svc.set_sharing(owner, 1, False)):
        with pytest.raises(NetworkServiceError) as exc:
            await action
        assert exc.value.code == 409
        assert MEMBER_ID in exc.value.message
    with pytest.raises(SharedCloudReferenceError) as blocked:
        assert_no_external_members_for_scope(owner)
    assert MEMBER_ID in blocked.value.message
    assert_no_external_members_for_scope(member)
    assert host.deleted == []

    await svc.delete_network(member, 1)
    assert host.deleted == []
    await svc.set_sharing(owner, 1, False)
    await svc.delete_network(owner, 1)
    assert host.deleted == [owned["runtime"]["bridge_name"]]


@pytest.mark.asyncio
async def test_folder_scope_ignores_members_inside_it(settings, labs_dir):
    svc = NetworkService()
    owner = _lab(labs_dir, "team/owner.json", OWNER_ID)
    inside = _lab(labs_dir, "team/inside.json", "lab-inside")
    outside = _lab(labs_dir, "team2/outside.json", "lab-outside")
    await svc.create_network(owner, {"name": "core"})
    await svc.set_sharing(owner, 1, True)
    join = {"name": "j", "config": {"shared_network_id": SHARED_ID}}

    await svc.create_network(inside, join)
    assert_no_external_members_for_scope("team")
    await svc.create_network(outside, join)
    with pytest.raises(SharedCloudReferenceError):
        assert_no_external_members_for_scope("team")


@pytest.mark.asyncio
@pytest.mark.parametrize("strip", [None, "config", "runtime"])
async def test_deleting_member_lab_keeps_owner_bridge(settings, host, labs_dir, strip):
    """Either marker alone is enough to protect another lab's bridge."""
    _svc, _owner, member, owned, _joined = await _pair(labs_dir)
    data = _saved(labs_dir, member)
    if strip == "config":
        data["networks"]["1"]["config"] = {}
    elif strip == "runtime":
        data["networks"]["1"]["runtime"].pop("shared_reference")

    _cleanup_lab_network_runtime(data, member)
    assert host.deleted == []
    assert owned["runtime"]["bridge_name"] in host.bridges


@pytest.mark.asyncio
async def test_generic_updates_cannot_forge_or_break_sharing(settings, labs_dir):
    svc, owner, member, _owned, _joined = await _pair(labs_dir)

    for lab, patch in (
        (member, {"config": {}}),
        (member, {"type": "nat_cloud"}),
        (owner, {"type": "nat_cloud"}),
        (owner, {"config": {"shared_network_id": "shared-net:x:1"}}),
        (owner, {"config": {"shared_cloud_id": "nat-cloud:x:1"}}),
    ):
        with pytest.raises(NetworkServiceError) as exc:
            await svc.patch_network(lab, 1, patch)
        assert exc.value.code == 422
    renamed, _event = await svc.patch_network(member, 1, {"name": "uplink", "left": 5})
    assert renamed["name"] == "uplink"
    assert renamed["config"]["shared_network_id"] == SHARED_ID


@pytest.mark.asyncio
async def test_unshareable_networks(settings, host, labs_dir):
    svc, _owner, member, _owned, _joined = await _pair(labs_dir)
    other = _lab(labs_dir, "other.json", "lab-other")
    host.bridges.add("br-eth0")
    await svc.create_network(
        other, {"name": "lan", "type": "bridge_cloud", "config": {"host_bridge": "br-eth0"}}
    )
    for lab in (member, other):
        with pytest.raises(NetworkServiceError) as exc:
            await svc.set_sharing(lab, 1, True)
        assert exc.value.code == 422


# --------------------------------------------------------------------------
# Runtime reconcile
# --------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("net_type", ["linux_bridge", "nat_cloud"])
async def test_opening_either_lab_restores_bridge_as_owner(settings, host, labs_dir, net_type):
    svc, owner, member, owned, _joined = await _pair(labs_dir, net_type=net_type)
    bridge = owned["runtime"]["bridge_name"]

    for first, second in ((member, owner), (owner, member)):
        host.bridges.clear()
        host_net.bridge_fingerprint_remove(bridge)
        assert svc.ensure_lab_bridges(first)["skipped"] == []
        assert host.bridges == {bridge}
        assert host_net.bridge_fingerprint_check(bridge, OWNER_ID, 1) == "match"
        assert svc.ensure_lab_bridges(second)["skipped"] == []


@pytest.mark.asyncio
async def test_reconcile_skips_member_once_owner_stops_sharing(settings, host, labs_dir):
    svc, owner, member, _owned, _joined = await _pair(labs_dir)
    _edit(labs_dir, owner, lambda d: d["networks"]["1"].update(shared=False))
    host.bridges.clear()
    summary = svc.ensure_lab_bridges(member)
    assert "no longer shares" in summary["skipped"][0]["reason"]
    assert host.bridges == set()


def test_labs_joining_each_other_do_not_hold_both_locks(settings, labs_dir, monkeypatch):
    """Opposite joins must never nest lab locks, or two opens can deadlock."""
    import asyncio
    from contextlib import contextmanager

    svc = NetworkService()
    for name, lab_id in (("a.json", "lab-a"), ("b.json", "lab-b")):
        _lab(labs_dir, name, lab_id)

    async def build():
        for name in ("a.json", "b.json"):
            await svc.create_network(name, {"name": "cloud", "type": "nat_cloud"})
            await svc.set_sharing(name, 1, True)
        await svc.create_network("a.json", {"name": "j", "config": {"shared_network_id": "shared-net:lab-b:1"}})
        await svc.create_network("b.json", {"name": "j", "config": {"shared_network_id": "shared-net:lab-a:1"}})

    asyncio.run(build())
    held: list[str] = []
    depth: list[int] = []
    real = network_service_mod.lab_lock

    @contextmanager
    def tracking(path, *args, **kwargs):
        with real(path, *args, **kwargs):
            held.append(path)
            depth.append(len(held))
            try:
                yield
            finally:
                held.remove(path)

    monkeypatch.setattr(network_service_mod, "lab_lock", tracking)
    for name in ("a.json", "b.json"):
        assert svc.ensure_lab_bridges(name)["skipped"] == []
    assert len(depth) == 4
    assert max(depth) == 1


# --------------------------------------------------------------------------
# Review regressions
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_create_cannot_forge_membership(settings, host, labs_dir):
    svc = NetworkService()
    owner = _lab(labs_dir, "owner.json", OWNER_ID)
    forger = _lab(labs_dir, "forger.json", "lab-forger")
    await svc.create_network(owner, {"name": "private"})
    await svc.create_network(owner, {"name": "cloud", "type": "nat_cloud"})

    for request in (
        {"type": "linux_bridge", "config": {"shared_cloud_id": f"nat-cloud:{OWNER_ID}:1"}},
        {"type": "nat_cloud", "config": {"shared_cloud_id": f"nat-cloud:{OWNER_ID}:2"}},
        {"type": "nat_cloud", "config": {"shared_cloud_id": f"nat-cloud:{OWNER_ID}:1"}},
    ):
        with pytest.raises(NetworkServiceError):
            await svc.create_network(forger, {"name": "x", **request})
    assert _saved(labs_dir, forger)["networks"] == {}

    # A blank id is no membership: the network owns and cleans up its bridge.
    blank = await svc.create_network(
        forger, {"name": "blank", "config": {"shared_network_id": "  ", "shared_cloud_id": " "}}
    )
    assert blank["config"] == {}
    await svc.delete_network(forger, blank["id"])
    assert host.deleted == [blank["runtime"]["bridge_name"]]

    # Junk already stored by an older release stays inert.
    await svc.create_network(forger, {"name": "junk"})
    _edit(labs_dir, forger, lambda d: d["networks"]["1"]["config"].update(
        shared_cloud_id=f"nat-cloud:{OWNER_ID}:1"))
    info = build_network_info(forger, 1, lambda path: path == forger)
    assert info["sharing"]["role"] == "none" and info["sharing"]["labs"] == []
    with pytest.raises(NetworkServiceError):
        await svc.patch_network(forger, 1, {"type": "nat_cloud"})
    await svc.delete_network(owner, 1)

    # Once shared, the id form works for everyone; admins may reuse any NAT-Cloud.
    joined = await svc.create_network(
        forger,
        {"name": "a", "type": "nat_cloud", "config": {"shared_cloud_id": f"nat-cloud:{OWNER_ID}:2"}},
        allow_unshared_clouds=True,
    )
    assert joined["runtime"]["shared_reference"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("admin", [True, False])
async def test_api_reuses_unshared_cloud_for_admins_only(client, labs_dir, admin):
    owner = _lab(labs_dir, "Shared/owner.json", OWNER_ID)
    member = _lab(labs_dir, "Shared/member.json", MEMBER_ID)
    await NetworkService().create_network(owner, {"name": "cloud", "type": "nat_cloud"})
    client.user.role = "admin" if admin else "user"
    async with client as c:
        response = await c.post(
            f"/api/labs/{member}/networks",
            json={"name": "x", "type": "nat_cloud", "config": {"shared_cloud_id": f"nat-cloud:{OWNER_ID}:1"}},
        )
    assert response.status_code == (201 if admin else 404)


@pytest.mark.asyncio
@pytest.mark.parametrize("attacher", ["owner", "member"])
@pytest.mark.parametrize("net_type", ["linux_bridge", "nat_cloud"])
async def test_failed_hot_attach_keeps_a_shared_bridge(
    settings, host, labs_dir, monkeypatch, attacher, net_type
):
    _svc, owner, member, owned, _joined = await _pair(labs_dir, net_type=net_type)
    bridge = owned["runtime"]["bridge_name"]
    host.bridges.clear()
    host_net.bridge_fingerprint_remove(bridge)
    lab = owner if attacher == "owner" else member
    monkeypatch.setattr(NodeRuntimeService, "_runtime_record", lambda *a, **k: {"kind": "qemu"})

    def fail(*args, **kwargs):
        raise host_net.HostNetError("attach failed")

    monkeypatch.setattr(NodeRuntimeService, "_attach_qemu_interface_locked", fail)
    with pytest.raises(host_net.HostNetError, match="attach failed"):
        LinkService()._hot_attach_running_endpoints(
            lab_path=lab, lab_data=_saved(labs_dir, lab), implicit_bridge=None, concrete_links=LINK
        )
    assert host.deleted == []
    assert host_net.bridge_fingerprint_check(bridge, OWNER_ID, 1) == "match"


@pytest.mark.asyncio
@pytest.mark.parametrize("created", [True, False])
async def test_failed_hot_attach_rolls_back_only_a_private_bridge_it_created(
    settings, host, labs_dir, monkeypatch, created
):
    lab = _lab(labs_dir, "a.json", "lab-a")
    record = await NetworkService().create_network(lab, {"name": "n"})
    host.bridges.clear()
    if not created:
        def lost(name):
            raise host_net.HostNetEEXIST("File exists")

        monkeypatch.setattr(host_net, "bridge_add", lost)
    monkeypatch.setattr(NodeRuntimeService, "_runtime_record", lambda *a, **k: {"kind": "qemu"})

    def fail(*args, **kwargs):
        raise host_net.HostNetError("attach failed")

    monkeypatch.setattr(NodeRuntimeService, "_attach_qemu_interface_locked", fail)
    with pytest.raises(host_net.HostNetError):
        LinkService()._hot_attach_running_endpoints(
            lab_path=lab, lab_data=_saved(labs_dir, lab), implicit_bridge=None, concrete_links=LINK
        )
    assert host.deleted == ([record["runtime"]["bridge_name"]] if created else [])


@pytest.mark.asyncio
async def test_hot_attach_never_rebuilds_an_ownerless_bridge(settings, host, labs_dir, monkeypatch):
    _svc, owner, member, _owned, _joined = await _pair(labs_dir)
    _edit(labs_dir, member, lambda d: d["networks"]["1"].update(config={}))
    host.bridges.clear()
    monkeypatch.setattr(NodeRuntimeService, "_runtime_record", lambda *a, **k: {"kind": "qemu"})
    with pytest.raises(host_net.HostNetError, match="no longer exists"):
        LinkService()._hot_attach_running_endpoints(
            lab_path=member, lab_data=_saved(labs_dir, member), implicit_bridge=None, concrete_links=LINK
        )
    assert host.bridges == set()


@pytest.mark.asyncio
@pytest.mark.parametrize("opened", ["owner", "member"])
@pytest.mark.parametrize("net_type", ["linux_bridge", "nat_cloud"])
async def test_fingerprint_left_by_a_member_is_returned_to_the_owner(
    settings, host, labs_dir, net_type, opened
):
    svc, owner, member, owned, _joined = await _pair(labs_dir, net_type=net_type)
    bridge = owned["runtime"]["bridge_name"]
    host_net.bridge_fingerprint_write(bridge, MEMBER_ID, 1)
    assert svc.ensure_lab_bridges(owner if opened == "owner" else member)["skipped"] == []
    assert host_net.bridge_fingerprint_check(bridge, OWNER_ID, 1) == "match"

    host_net.bridge_fingerprint_write(bridge, "stranger", 1)
    assert len(svc.ensure_lab_bridges(member)["skipped"]) == 1
    assert host_net.bridge_fingerprint_check(bridge, "stranger", 1) == "match"


@pytest.mark.asyncio
async def test_reconcile_tolerates_losing_the_create_race(settings, host, labs_dir, monkeypatch):
    svc, owner, member, owned, _joined = await _pair(labs_dir)
    host.bridges.clear()

    def lost(name):
        raise host_net.HostNetEEXIST("File exists")

    monkeypatch.setattr(host_net, "bridge_add", lost)
    assert svc.ensure_lab_bridges(member)["skipped"] == []


def test_node_start_uses_owner_bridge_without_stored_name(settings, host, labs_dir):
    network = {
        "id": 1, "type": "linux_bridge", "runtime": {},
        "config": {"shared_network_id": SHARED_ID},
    }
    data = {"id": MEMBER_ID, "nodes": {"1": _node("50:00:00:01:00:00")}, "networks": {"1": network}, "links": LINK}
    node = {**data["nodes"]["1"], "ethernet": 1}
    runtime = NodeRuntimeService()
    expected = host_net.bridge_name(OWNER_ID, 1)
    assert [a["bridge_name"] for a in runtime._qemu_attachments(data, node)] == [expected]
    assert [a["bridge_name"] for a in runtime._docker_attachments(data, node)] == [expected]


@pytest.mark.asyncio
async def test_backup_copy_does_not_shadow_the_sharing_owner(settings, labs_dir):
    svc, owner, member, _owned, _joined = await _pair(labs_dir)
    copy = _saved(labs_dir, owner)
    copy["networks"]["1"]["shared"] = False
    (labs_dir / "backup").mkdir()
    (labs_dir / "backup" / "owner.json").write_text(json.dumps(copy))
    assert svc.ensure_lab_bridges(member)["skipped"] == []


def test_lab_writes_are_atomic(settings, labs_dir, monkeypatch):
    from app.services import lab_service
    from app.services.lab_service import LabService

    lab = _lab(labs_dir, "a.json", "lab-a")
    (labs_dir / lab).chmod(0o640)
    before = (labs_dir / lab).read_text()
    data = LabService.read_lab_json_static(lab)

    def broken(*args, **kwargs):
        args[1].write("{")
        raise OSError("disk full")

    with monkeypatch.context() as patch:
        patch.setattr(lab_service.json, "dump", broken)
        with pytest.raises(OSError):
            LabService.write_lab_json_static(lab, data)
    assert (labs_dir / lab).read_text() == before
    LabService.write_lab_json_static(lab, data)
    assert (labs_dir / lab).stat().st_mode & 0o777 == 0o640
    assert [p.name for p in labs_dir.iterdir()] == ["a.json"]
    long_name = _lab(labs_dir, "x" * 240 + ".json", "lab-long")
    LabService.write_lab_json_static(long_name, data)


@pytest.mark.parametrize("node, interface, index, expected", [
    ({"type": "qemu", "ethernet": 2, "firstmac": "50:00:00:01:00:00"}, {}, 1, "50:00:00:01:00:01"),
    # Boot ignores planned_mac; only NICs added later use it.
    ({"type": "qemu", "ethernet": 2, "firstmac": "50:00:00:01:00:00"}, {"planned_mac": "AA:00:00:00:00:01"}, 0, "50:00:00:01:00:00"),
    ({"type": "qemu", "ethernet": 1, "firstmac": "50:00:00:01:00:00"}, {"planned_mac": "AA:00:00:00:00:01"}, 1, "aa:00:00:00:00:01"),
    ({"type": "qemu", "ethernet": 1}, {}, 0, "52:54:00:00:00:00"),
    ({"type": "qemu", "ethernet": 1, "firstmac": "x:y"}, {}, 0, None),
    ({"type": "docker"}, {}, 0, None),
])
def test_endpoint_mac_matches_what_qemu_boots(node, interface, index, expected):
    assert network_info_service._endpoint_mac(node, interface, index) == expected


@pytest.mark.asyncio
async def test_editing_first_mac_takes_effect(client, labs_dir, monkeypatch):
    monkeypatch.setattr("app.routers.labs._validate_node_update_request", lambda *a: None)
    monkeypatch.setattr("app.routers.labs._node_is_running", lambda *a: False)
    monkeypatch.setattr(NodeRuntimeService, "enrich_node", lambda self, lab_id, node_id, node: node)
    old = "50:00:00:01:00:00"
    node = {
        **_node(old), "ethernet": 2, "extras": {"firstmac": old},
        "interfaces": [
            {"index": 0, "name": "Gi0", "planned_mac": old},
            {"index": 1, "name": "Gi1", "planned_mac": "aa:00:00:00:00:09"},
        ],
    }
    lab = _lab(labs_dir, "a.json", "lab-a", nodes={"1": node})
    async with client as c:
        bad = await c.put(f"/api/labs/{lab}/nodes/1", json={"extras": {"firstmac": "nope"}})
        assert bad.json()["code"] == 400
        # Re-submitting the same value, as the dialog does, changes nothing.
        await c.put(f"/api/labs/{lab}/nodes/1", json={"extras": {"firstmac": old}})
        assert _saved(labs_dir, lab)["nodes"]["1"]["interfaces"][0]["planned_mac"] == old
        ok = await c.put(f"/api/labs/{lab}/nodes/1", json={"extras": {"firstmac": "50:00:00:AA:00:00"}})
        assert ok.json()["code"] == 200
    saved = _saved(labs_dir, lab)["nodes"]["1"]
    assert saved["firstmac"] == "50:00:00:aa:00:00"
    assert [i["planned_mac"] for i in saved["interfaces"]] == [None, "aa:00:00:00:00:09"]


def test_discovery_ignores_other_labs_interfaces(host):
    own = host_net.tap_name(OWNER_ID, 3, 0)
    foreign = host_net.tap_name(MEMBER_ID, 3, 0)
    check = NodeRuntimeService._belongs_to_other_lab
    assert check(OWNER_ID, foreign) is True
    assert check(OWNER_ID, host_net.veth_host_name(MEMBER_ID, 12, 1)) is True
    assert check(OWNER_ID, own) is False
    assert check(OWNER_ID, "eth0") is False


def test_default_macs_differ_between_labs():
    a, b = _first_mac_for_node("lab-a", 1), _first_mac_for_node("lab-b", 1)
    assert a != b
    assert a == _first_mac_for_node("lab-a", 1)
    seen = {_first_mac_for_node("lab-a", n) for n in range(1, 600)}
    assert len(seen) == 599
    for mac in (a, b, _first_mac_for_node("lab-a", 300)):
        octets = mac.split(":")
        assert len(octets) == 6 and all(len(o) == 2 for o in octets)
        assert int(octets[0], 16) & 1 == 0  # unicast
        assert octets[5] == "00"


# --------------------------------------------------------------------------
# Info endpoint
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_info_reports_dhcp_scope_server_and_leases(settings, labs_dir, monkeypatch, tmp_path):
    svc = NetworkService()
    lab = _lab(labs_dir, "nat.json", "lab-nat", nodes={"1": _node("50:00:00:01:00:00")}, links=LINK)
    record = await svc.create_network(lab, {"name": "inet", "type": "nat_cloud"})
    svc._allocate_ip(lab, 1)
    bridge = record["runtime"]["bridge_name"]

    root = tmp_path / "nat-cloud" / bridge
    root.mkdir(parents=True)
    (root / "dnsmasq.conf").write_text("port=0\ndhcp-range=10.255.0.100,10.255.0.254,12h\n")
    (root / "dnsmasq.pid").write_text("77\n")
    (root / "leases").write_text(
        "9999999999 50:00:00:01:00:00 10.255.0.140 r1 *\n"
        "1 aa:bb:cc:00:00:01 10.255.0.150 expired *\n"
        "0 aa:bb:cc:00:00:02 10.255.0.101 * *\n"
        "garbage\n"
    )
    proc = tmp_path / "proc"
    (proc / "77").mkdir(parents=True)
    (proc / "77" / "cmdline").write_bytes(f"dnsmasq\0--conf-file={root}/dnsmasq.conf\0".encode())
    sysnet = tmp_path / "sysnet"
    (sysnet / bridge / "brif" / "tap0").mkdir(parents=True)
    for name, value in {"operstate": "up", "address": "02:00:00:00:00:01", "mtu": "1500", "flags": "0x1003"}.items():
        (sysnet / bridge / name).write_text(value + "\n")
    monkeypatch.setattr(network_info_service, "PROC_ROOT", proc)
    monkeypatch.setattr(network_info_service, "SYS_CLASS_NET", sysnet)

    info = build_network_info(lab, 1, lambda path: True)

    ip = info["ip"]
    assert (ip["cidr"], ip["gateway"], ip["egress_interface"]) == ("10.255.0.0/24", "10.255.0.1", "eth0")
    assert (ip["static_start"], ip["static_end"], ip["used_ips"]) == ("10.255.0.2", "10.255.0.99", ["10.255.0.2"])
    dhcp = ip["dhcp"]
    assert (dhcp["start"], dhcp["end"], dhcp["size"]) == ("10.255.0.100", "10.255.0.254", 155)
    assert (dhcp["running"], dhcp["pid"], dhcp["lease_time"]) == (True, 77, "12h")
    assert [(l["ip"], l["hostname"], l["node_name"]) for l in dhcp["leases"]] == [
        ("10.255.0.101", None, None), ("10.255.0.140", "r1", "r1"),
    ]
    assert info["bridge"]["admin_up"] is True and info["bridge"]["ports"] == 1
    # #320: found without its pid file; another network's server never counts.
    (root / "dnsmasq.pid").unlink()
    assert build_network_info(lab, 1, lambda path: True)["ip"]["dhcp"]["pid"] == 77
    (root / "dnsmasq.pid").write_text("77\n")
    # The helper passes the data directory unresolved.
    (tmp_path / "data-link").symlink_to(tmp_path, target_is_directory=True)
    linked = tmp_path / "data-link" / "nat-cloud" / bridge
    (proc / "77" / "cmdline").write_bytes(f"dnsmasq\0--conf-file={linked}/dnsmasq.conf\0".encode())
    assert build_network_info(lab, 1, lambda path: True)["ip"]["dhcp"]["pid"] == 77
    (proc / "77" / "cmdline").write_bytes(f"dnsmasq\0--conf-file={root}0/dnsmasq.conf\0".encode())
    assert build_network_info(lab, 1, lambda path: True)["ip"]["dhcp"]["running"] is False
    assert info["endpoints"][0]["interface"] == "Gi0"
    assert info["sharing"] == {
        "role": "none", "shareable": True, "reason": None,
        "owner_lab": None, "owner_missing": False, "labs": [],
    }


@pytest.mark.asyncio
async def test_info_names_docker_nodes_from_learned_macs(settings, labs_dir, monkeypatch):
    svc = NetworkService()
    links = [
        {"id": f"l{n}", "from": {"node_id": n, "interface_index": 0}, "to": {"network_id": 1}} for n in (1, 2)
    ]
    nodes = {str(n): {**_node(None, name=f"host-{n}", kind="docker"), "id": n} for n in (1, 2)}
    lab = _lab(labs_dir, "nat.json", "lab-nat", nodes=nodes, links=links)
    bridge = (await svc.create_network(lab, {"name": "inet", "type": "nat_cloud"}))["runtime"]["bridge_name"]
    one, two = (host_net.veth_host_name("lab-nat", n, 0) for n in (1, 2))
    output = (
        f"AA:00:00:00:00:01 dev {one} master {bridge}\n"
        f"aa:00:00:00:00:02 dev {two} master {bridge}\n"
        f"aa:00:00:00:00:03 dev {two} master {bridge}\n"
        f"aa:00:00:00:00:ff dev {one} master {bridge} permanent\n"
        f"33:33:00:00:00:01 dev {bridge} self permanent\n"
    )
    monkeypatch.setattr(
        network_info_service.subprocess, "run",
        lambda *a, **k: SimpleNamespace(returncode=0, stdout=output),
    )
    root = labs_dir.parent / "nat-cloud" / bridge
    root.mkdir(parents=True)
    (root / "leases").write_text("0 aa:00:00:00:00:01 10.255.0.101 * *\n0 aa:00:00:00:00:03 10.255.0.102 * *\n")

    info = build_network_info(lab, 1, lambda path: True)
    assert [(e["node_name"], e["mac"]) for e in info["endpoints"]] == [
        ("host-1", "aa:00:00:00:00:01"), ("host-2", None),
    ]
    assert [l["node_name"] for l in info["ip"]["dhcp"]["leases"]] == ["host-1", "host-2"]
    assert all("_ports" not in e for e in info["endpoints"])


@pytest.mark.asyncio
async def test_info_survives_missing_host_state(settings, labs_dir):
    svc = NetworkService()
    lab = _lab(labs_dir, "nat.json", "lab-nat")
    await svc.create_network(lab, {"name": "inet", "type": "nat_cloud"})
    info = build_network_info(lab, 1, lambda path: True)
    assert info["bridge"]["exists"] is False
    assert info["ip"]["dhcp"]["running"] is False
    assert info["ip"]["dhcp"]["leases"] == []
    with pytest.raises(NetworkServiceError) as exc:
        build_network_info(lab, 99, lambda path: True)
    assert exc.value.code == 404


@pytest.mark.asyncio
async def test_info_lists_segment_and_mac_conflicts(settings, labs_dir):
    _svc, owner, member, owned, _joined = await _pair(labs_dir)
    for lab in (owner, member):
        _edit(labs_dir, lab, lambda d: d.update(nodes={"1": _node("50:00:00:01:00:00")}, links=LINK))

    info = build_network_info(member, 1, lambda path: path == member)
    assert info["bridge"]["name"] == owned["runtime"]["bridge_name"]
    sharing = info["sharing"]
    assert (sharing["role"], sharing["owner_lab"], sharing["shareable"]) == ("member", OWNER_ID, False)
    assert [(l["lab_name"], l["role"], l["lab_path"], l["connections"]) for l in sharing["labs"]] == [
        (OWNER_ID, "owner", None, 1), (MEMBER_ID, "member", member, 1),
    ]
    assert [e["lab_name"] for e in info["endpoints"]] == [MEMBER_ID, OWNER_ID]
    assert info["mac_conflicts"] == [{
        "mac": "50:00:00:01:00:00",
        "endpoints": [f"{MEMBER_ID} / r1 Gi0", f"{OWNER_ID} / r1 Gi0"],
    }]
    assert build_network_info(owner, 1, lambda path: True)["sharing"]["role"] == "owner"


@pytest.mark.parametrize("bridge", ["../../etc", "..", "a/b"])
def test_info_never_treats_bridge_name_as_path(settings, labs_dir, bridge):
    lab = _lab(labs_dir, "x.json", "lab-x")
    _edit(labs_dir, lab, lambda d: d["networks"].update({"1": {
        "id": 1, "name": "n", "type": "nat_cloud",
        "config": {"cidr": "10.1.0.0/24"}, "runtime": {"bridge_name": bridge},
    }}))
    info = build_network_info(lab, 1, lambda path: True)
    assert info["bridge"]["exists"] is False
    assert info["ip"]["dhcp"]["leases"] == []


# --------------------------------------------------------------------------
# API
# --------------------------------------------------------------------------

@pytest.fixture()
def client(settings, monkeypatch):
    user = SimpleNamespace(username="admin", role="admin", folder="/")
    app.dependency_overrides[get_current_user] = lambda: user
    audits: list[tuple] = []

    async def record(action, **context):
        audits.append((action, context))

    monkeypatch.setattr("app.services.audit_service.record", record)
    c = AsyncClient(transport=ASGITransport(app=app), base_url="http://t")
    c.user, c.audits = user, audits
    yield c
    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_sharing_api(client, labs_dir):
    owner = _lab(labs_dir, "owner.json", OWNER_ID)
    member = _lab(labs_dir, "member.json", MEMBER_ID)
    await NetworkService().create_network(owner, {"name": "core"})

    async with client as c:
        on = await c.put(f"/api/labs/{owner}/networks/1/sharing", json={"shared": True})
        assert on.status_code == 200 and on.json()["network"]["shared"] is True
        assert isinstance(on.json()["revision"], int)
        listed = (await c.get("/api/system/shared-networks")).json()["data"]
        assert listed == [{
            "id": SHARED_ID, "type": "linux_bridge", "name": "core",
            "lab_id": OWNER_ID, "lab_name": OWNER_ID, "cidr": None,
        }]
        mine = await c.get("/api/system/shared-networks", params={"for_lab": f"/{owner}"})
        assert mine.json()["data"] == []
        bad = await c.get("/api/system/shared-networks", params={"for_lab": "../x"})
        assert bad.status_code == 400
        joined = await c.post(
            f"/api/labs/{member}/networks",
            json={"name": "j", "config": {"shared_network_id": listed[0]["id"]}},
        )
        assert joined.status_code == 201
        again = await c.get("/api/system/shared-networks", params={"for_lab": member})
        assert again.json()["data"] == []
        off = await c.put(f"/api/labs/{owner}/networks/1/sharing", json={"shared": False})
        assert off.status_code == 409 and MEMBER_ID in off.json()["message"]
        info = (await c.get(f"/api/labs/{member}/networks/1/info")).json()["data"]
        assert info["sharing"]["owner_lab"] == OWNER_ID
        # A generic update carries no sharing flag.
        await c.patch(f"/api/labs/{owner}/networks/1", json={"shared": False, "left": 9})
        assert _saved(labs_dir, owner)["networks"]["1"]["shared"] is True

    changes = [ctx["detail"]["changes"] for action, ctx in client.audits
               if action == "network.update" and "shared" in ctx["detail"]["changes"]]
    assert changes == [{"shared": {"from": False, "to": True}}]


@pytest.mark.asyncio
async def test_new_endpoints_respect_user_scope(client, labs_dir):
    _lab(labs_dir, "bob/lab.json", "lab-bob")
    await NetworkService().create_network("bob/lab.json", {"name": "core"})
    client.user.role = "user"
    client.user.folder = "/alice"
    client.user.username = "alice"
    async with client as c:
        assert (await c.get("/api/labs/bob/lab.json/networks/1/info")).status_code == 403
        probe = await c.get("/api/system/shared-networks", params={"for_lab": "bob/lab.json"})
        assert probe.status_code == 403
        denied = await c.put("/api/labs/bob/lab.json/networks/1/sharing", json={"shared": True})
        assert denied.status_code == 403
    assert "shared" not in _saved(labs_dir, "bob/lab.json")["networks"]["1"]
