# SPDX-License-Identifier: Apache-2.0

"""#325 — isolated Linux bridges with an optional CIDR and DHCP.

A CIDR alone is informational. DHCP needs a /22-/29 CIDR, a subnet nothing
else on the host uses, and puts a guard, a server address and a dnsmasq
instance (no router, no DNS) on the host. The privileged helper is mocked:
every host call lands in one ordered log.
"""

from __future__ import annotations

import ipaddress
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

from app.dependencies import get_current_user
from app.main import app
from app.services import host_net
from app.services.lab_service import LabService, _cleanup_lab_network_runtime
from app.services.network_info_service import build_network_info
from app.services.network_service import (
    NetworkService,
    NetworkServiceError,
    _normalize_bridge_config,
    bridge_dhcp_defaults,
)

LAB_A = "lab-a"
LAB_B = "lab-b"


@pytest.fixture()
def labs_dir(tmp_path: Path) -> Path:
    d = tmp_path / "labs"
    d.mkdir()
    return d


@pytest.fixture()
def settings(monkeypatch, tmp_path, labs_dir):
    instance_dir = tmp_path / "instance"
    instance_dir.mkdir()
    (instance_dir / "instance_id").write_text("instance-325")
    monkeypatch.setenv("NOVA_VE_INSTANCE_DIR", str(instance_dir))
    s = SimpleNamespace(LABS_DIR=labs_dir, NAT_CLOUD_POOL="10.255.0.0/16")
    for target in (
        "app.services.network_service.get_settings",
        "app.services.cloud_inventory_service.get_settings",
        "app.services.lab_service.get_settings",
        "app.services.link_service.get_settings",
        "app.routers.labs.get_settings",
    ):
        monkeypatch.setattr(target, lambda: s)

    async def fake_publish(*_args, **_kwargs):
        return SimpleNamespace(seq=1)

    monkeypatch.setattr("app.services.ws_hub.ws_hub.publish", fake_publish)
    return s


class Host:
    """Records host_net calls in order; ``fail`` makes a call raise."""

    def __init__(self):
        self.calls: list[tuple] = []
        self.existing: set[str] = set()
        self.host_addrs: list[tuple[str, str]] = []
        self.fail: dict[str, Exception] = {}

    def record(self, name, *args):
        self.calls.append((name, *args))
        if name in self.fail:
            raise self.fail[name]

    def names(self) -> list[str]:
        return [call[0] for call in self.calls]

    def dhcp_calls(self) -> list[tuple]:
        return [call for call in self.calls if call[0] in DHCP_CALLS]


DHCP_CALLS = {
    "bridge_guard_apply",
    "bridge_guard_remove",
    "bridge_addr_add",
    "bridge_addr_del",
    "dnsmasq_start",
    "dnsmasq_start_isolated",
    "dnsmasq_stop",
    "list_ipv4_addrs",
    "link_up",
}


@pytest.fixture()
def host(monkeypatch, settings):
    h = Host()

    def returning(name, value=None):
        def call(*args, **_kwargs):
            h.record(name, *args)
            return value() if callable(value) else value

        return call

    for name in (
        "bridge_add",
        "bridge_del",
        "bridge_addr_add",
        "bridge_addr_del",
        "bridge_guard_apply",
        "bridge_guard_remove",
        "link_up",
        "dnsmasq_stop",
        "ipv4_forward_enable",
        "nat_apply",
        "nat_remove",
        "forward_apply",
        "forward_remove",
        "bridge_fingerprint_write",
    ):
        monkeypatch.setattr(host_net, name, returning(name))
    monkeypatch.setattr(host_net, "dnsmasq_start", returning("dnsmasq_start", 1111))
    monkeypatch.setattr(host_net, "dnsmasq_start_isolated", returning("dnsmasq_start_isolated", 2222))
    monkeypatch.setattr(host_net, "default_egress_iface", lambda: "eth0")
    monkeypatch.setattr(host_net, "bridge_exists", lambda name: name in h.existing)
    monkeypatch.setattr(host_net, "bridge_fingerprint_check", lambda *a: "match")
    monkeypatch.setattr(host_net, "link_state", lambda name: (True, None, True))
    monkeypatch.setattr(host_net, "list_ipv4_addrs", returning("list_ipv4_addrs", lambda: h.host_addrs))
    monkeypatch.setattr("app.services.network_info_service.dhcp_server_pid", lambda bridge: None)
    return h


def _lab(labs_dir: Path, name: str, lab_id: str, networks: dict | None = None, **extra) -> str:
    (labs_dir / name).write_text(
        json.dumps(
            {
                "schema": 2,
                "id": lab_id,
                "meta": {"name": name.removesuffix(".json")},
                "nodes": {},
                "networks": networks or {},
                "links": [],
                **extra,
            }
        )
    )
    return name


def _saved(labs_dir: Path, name: str) -> dict:
    return json.loads((labs_dir / name).read_text())


def _bridge(net_id: int, lab_id: str = LAB_A, *, config: dict | None = None, runtime: dict | None = None,
            **extra) -> dict:
    return {
        "id": net_id,
        "name": f"net{net_id}",
        "type": "linux_bridge",
        "implicit": False,
        "visibility": True,
        "config": config or {},
        "runtime": {
            "bridge_name": host_net.bridge_name(lab_id, net_id),
            "driver": "linux_bridge",
            "used_ips": [],
            "first_offset": 2,
            **(runtime or {}),
        },
        **extra,
    }


def _dhcp_config(cidr: str = "10.40.1.0/24", **overrides) -> dict:
    net = ipaddress.ip_network(cidr)
    return {"cidr": cidr, "dhcp": True, **bridge_dhcp_defaults(net), **overrides}


def _error(exc_info) -> tuple[int, str | None, str]:
    exc = exc_info.value
    return exc.code, exc.extra.get("field"), exc.message


# ---------------------------------------------------------------------------
# Settings: CIDR, prefix gate, defaults, server and scope
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("cidr", "expected"),
    [
        ("10.8.0.0/22", ("10.8.3.254", "10.8.2.0", "10.8.3.253")),
        ("10.40.1.0/24", ("10.40.1.254", "10.40.1.128", "10.40.1.253")),
        ("10.40.1.0/29", ("10.40.1.6", "10.40.1.4", "10.40.1.5")),
        ("10.40.1.8/29", ("10.40.1.14", "10.40.1.12", "10.40.1.13")),
    ],
)
def test_defaults_fit_every_allowed_size(cidr, expected):
    config = _normalize_bridge_config({"cidr": cidr, "dhcp": True})
    assert (config["dhcp_server"], config["dhcp_start"], config["dhcp_end"]) == expected
    assert config["cidr"] == cidr and config["dhcp"] is True


def test_every_allowed_prefix_gets_valid_defaults():
    for prefix in range(22, 30):
        config = _normalize_bridge_config({"cidr": f"10.0.0.0/{prefix}", "dhcp": True})
        net = ipaddress.ip_network(config["cidr"])
        server, start, end = (ipaddress.IPv4Address(config[k]) for k in ("dhcp_server", "dhcp_start", "dhcp_end"))
        hosts = set(net.hosts())
        assert {server, start, end} <= hosts
        assert start <= end and not start <= server <= end


@pytest.mark.parametrize("cidr", ["10.0.0.0/21", "10.0.0.0/16", "10.0.0.0/30", "10.0.0.1/32"])
def test_dhcp_is_refused_outside_22_to_29(cidr):
    with pytest.raises(NetworkServiceError) as exc_info:
        _normalize_bridge_config({"cidr": cidr, "dhcp": True})
    code, field, message = _error(exc_info)
    assert (code, field) == (422, "config.dhcp")
    assert "/22 and /29" in message


@pytest.mark.parametrize("cidr", ["10.0.0.0/21", "10.0.0.0/30", "10.0.0.1/32"])
def test_any_ipv4_cidr_is_kept_as_information(cidr):
    config = _normalize_bridge_config(
        {"cidr": cidr, "dhcp": False, "dhcp_server": "10.0.0.1", "dhcp_start": "x"}
    )
    assert config == {"cidr": cidr, "dhcp": False}


def test_dhcp_without_a_cidr_is_refused():
    with pytest.raises(NetworkServiceError) as exc_info:
        _normalize_bridge_config({"dhcp": True})
    assert _error(exc_info)[:2] == (422, "config.dhcp")
    assert _normalize_bridge_config({"name": "x"}) == {"name": "x"}


@pytest.mark.parametrize(
    ("config", "field"),
    [
        ({"cidr": "10.40.1.5/24"}, "config.cidr"),  # host bits
        ({"cidr": "fd00::/64"}, "config.cidr"),
        ({"cidr": "not-a-cidr"}, "config.cidr"),
        ({"cidr": "10.40.1.0/24", "dhcp": "yes"}, "config.dhcp"),
        ({"cidr": "10.40.1.0/24", "dhcp": True, "dhcp_server": "10.40.1.200"}, "config.dhcp_server"),
        ({"cidr": "10.40.1.0/24", "dhcp": True, "dhcp_server": "10.40.1.255"}, "config.dhcp_server"),
        ({"cidr": "10.40.1.0/24", "dhcp": True, "dhcp_server": "10.40.2.1"}, "config.dhcp_server"),
        ({"cidr": "10.40.1.0/24", "dhcp": True, "dhcp_server": "bogus"}, "config.dhcp_server"),
        ({"cidr": "10.40.1.0/24", "dhcp": True, "dhcp_start": "10.40.1.0"}, "config.dhcp_start"),
        ({"cidr": "10.40.1.0/24", "dhcp": True, "dhcp_end": "10.40.1.255"}, "config.dhcp_end"),
        (
            {"cidr": "10.40.1.0/24", "dhcp": True, "dhcp_start": "10.40.1.200", "dhcp_end": "10.40.1.150"},
            "config.dhcp_end",
        ),
    ],
)
def test_bad_settings_name_their_field(config, field):
    with pytest.raises(NetworkServiceError) as exc_info:
        _normalize_bridge_config(config)
    assert _error(exc_info)[:2] == (422, field)


def test_blank_fields_mean_defaults_and_custom_values_stay():
    config = _normalize_bridge_config(
        {
            "cidr": "10.40.1.0/24",
            "dhcp": True,
            "dhcp_server": " ",
            "dhcp_start": "10.40.1.10",
            "dhcp_end": "10.40.1.20",
        }
    )
    assert (config["dhcp_server"], config["dhcp_start"], config["dhcp_end"]) == (
        "10.40.1.254",
        "10.40.1.10",
        "10.40.1.20",
    )


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cidr_only_bridge_makes_no_host_changes(host, labs_dir):
    lab = _lab(labs_dir, "a.json", LAB_A)
    payload = await NetworkService().create_network(
        lab, {"name": "lan", "config": {"cidr": "10.40.1.0/24", "dhcp": False}}
    )
    assert payload["config"] == {
        "cidr": "10.40.1.0/24",
        "dhcp": False,
        "dhcp_server": "10.40.1.254",
        "dhcp_start": "10.40.1.128",
        "dhcp_end": "10.40.1.253",
    }
    # Only the bridge itself; no address, guard, DHCP or host-subnet scan.
    assert host.names() == ["bridge_add", "bridge_fingerprint_write"]
    assert "dhcp" not in payload["runtime"]


@pytest.mark.asyncio
async def test_create_with_dhcp_guards_before_addressing(host, labs_dir):
    lab = _lab(labs_dir, "a.json", LAB_A)
    bridge = host_net.bridge_name(LAB_A, 1)
    payload = await NetworkService().create_network(
        lab, {"name": "lan", "config": {"cidr": "10.40.1.0/24", "dhcp": True}}
    )
    assert host.dhcp_calls() == [
        ("list_ipv4_addrs",),
        ("bridge_guard_apply", bridge),
        ("bridge_addr_add", bridge, "10.40.1.254/24"),
        ("link_up", bridge),
        ("dnsmasq_start_isolated", bridge, "10.40.1.254", "10.40.1.128", "10.40.1.253"),
    ]
    runtime = _saved(labs_dir, lab)["networks"]["1"]["runtime"]
    assert runtime["dhcp"] is True and runtime["dhcp_pid"] == 2222
    assert runtime["dhcp_server"] == "10.40.1.254"
    assert payload["runtime"]["dhcp_pid"] == 2222
    # Never NAT, never the NAT-Cloud server with its router option.
    assert not {"nat_apply", "forward_apply", "dnsmasq_start"} & set(host.names())


@pytest.mark.asyncio
async def test_create_failure_leaves_nothing_behind(host, labs_dir):
    lab = _lab(labs_dir, "a.json", LAB_A)
    bridge = host_net.bridge_name(LAB_A, 1)
    host.fail["dnsmasq_start_isolated"] = host_net.HostNetUnknown("boom")
    with pytest.raises(NetworkServiceError) as exc_info:
        await NetworkService().create_network(
            lab, {"name": "lan", "config": {"cidr": "10.40.1.0/24", "dhcp": True}}
        )
    assert exc_info.value.code == 500
    assert _saved(labs_dir, lab)["networks"] == {}
    # Rolled back in reverse: server, address, guard, then the new bridge.
    tail = host.calls[host.names().index("dnsmasq_start_isolated") + 1 :]
    assert tail == [
        ("dnsmasq_stop", bridge),
        ("bridge_addr_del", bridge, "10.40.1.254/24"),
        ("bridge_guard_remove", bridge),
        ("bridge_del", bridge),
    ]


@pytest.mark.asyncio
async def test_create_dhcp_on_other_types_is_refused(host, labs_dir):
    lab = _lab(labs_dir, "a.json", LAB_A)
    with pytest.raises(NetworkServiceError) as exc_info:
        await NetworkService().create_network(
            lab, {"name": "o", "type": "ovs_bridge", "config": {"dhcp": True}}
        )
    assert _error(exc_info)[:2] == (422, "config.dhcp")
    assert host.calls == []


# ---------------------------------------------------------------------------
# Overlaps (DHCP only)
# ---------------------------------------------------------------------------


def _nat_cloud(net_id: int, lab_id: str, cidr: str) -> dict:
    base = cidr.rsplit(".", 1)[0]
    return {
        "id": net_id,
        "name": f"nat{net_id}",
        "type": "nat_cloud",
        "config": {"cidr": cidr, "gateway": f"{base}.1", "dhcp": True,
                   "dhcp_start": f"{base}.100", "dhcp_end": f"{base}.254"},
        "runtime": {"bridge_name": host_net.bridge_name(lab_id, net_id)},
    }


async def _create_dhcp(lab, cidr="10.40.1.0/24", can_open=None):
    return await NetworkService().create_network(
        lab, {"name": "lan", "config": {"cidr": cidr, "dhcp": True}}, can_open=can_open
    )


@pytest.mark.asyncio
async def test_dhcp_refuses_a_host_interface_subnet_but_not_its_own(host, labs_dir):
    lab = _lab(labs_dir, "a.json", LAB_A)
    # The bridge's own address, left by an earlier attempt, is no conflict.
    host.host_addrs = [(host_net.bridge_name(LAB_A, 1), "10.40.1.254/24"), ("eth0", "192.168.7.10/24")]
    await _create_dhcp(lab)

    other = _lab(labs_dir, "b.json", LAB_B)
    with pytest.raises(NetworkServiceError) as exc_info:
        await _create_dhcp(other, "192.168.7.0/24")
    exc = exc_info.value
    assert (exc.code, exc.extra["field"], exc.extra["conflicting_cidr"]) == (409, "config.cidr", "192.168.7.0/24")
    assert "host interface eth0" in exc.message
    assert _saved(labs_dir, other)["networks"] == {}


@pytest.mark.asyncio
async def test_dhcp_refuses_a_nat_cloud_in_another_lab_without_naming_hidden_labs(host, labs_dir):
    _lab(labs_dir, "secret.json", LAB_B, {"1": _nat_cloud(1, LAB_B, "10.40.1.0/24")})
    lab = _lab(labs_dir, "a.json", LAB_A)
    with pytest.raises(NetworkServiceError) as exc_info:
        await _create_dhcp(lab, "10.40.0.0/22", can_open=lambda path: False)
    assert exc_info.value.code == 409
    assert exc_info.value.extra["source"] == "a NAT-Cloud in another lab"
    assert "secret" not in exc_info.value.message
    with pytest.raises(NetworkServiceError) as exc_info:
        await _create_dhcp(lab, can_open=lambda path: True)
    assert "secret.json" in exc_info.value.message


@pytest.mark.asyncio
async def test_dhcp_refuses_another_dhcp_bridge_but_not_members_or_cidr_only(host, labs_dir):
    _lab(
        labs_dir,
        "b.json",
        LAB_B,
        {
            "1": _bridge(1, LAB_B, config=_dhcp_config("10.40.1.0/24")),
            # A member carries a copy of its owner's settings: never a conflict.
            "2": _bridge(2, LAB_B, config={**_dhcp_config("10.50.1.0/24"), "shared_network_id": "shared-net:x:1"}),
            # Informational CIDRs may overlap anything.
            "3": _bridge(3, LAB_B, config={"cidr": "10.60.1.0/24", "dhcp": False}),
        },
    )
    lab = _lab(labs_dir, "a.json", LAB_A)
    with pytest.raises(NetworkServiceError) as exc_info:
        await _create_dhcp(lab, "10.40.1.0/29", can_open=lambda path: False)
    assert exc_info.value.extra["source"] == "a DHCP bridge in another lab"
    await NetworkService().create_network(lab, {"name": "m", "config": _dhcp_config("10.50.1.0/24")})
    await NetworkService().create_network(lab, {"name": "c", "config": _dhcp_config("10.60.1.0/24")})


@pytest.mark.asyncio
async def test_cidr_only_bridges_may_overlap_anything(host, labs_dir):
    _lab(labs_dir, "b.json", LAB_B, {"1": _bridge(1, LAB_B, config=_dhcp_config("10.40.1.0/24")),
                                     "2": _nat_cloud(2, LAB_B, "10.41.1.0/24")})
    host.host_addrs = [("eth0", "10.42.1.5/24")]
    lab = _lab(labs_dir, "a.json", LAB_A)
    for index, cidr in enumerate(("10.40.1.0/24", "10.41.1.0/24", "10.42.1.0/24")):
        await NetworkService().create_network(lab, {"name": f"n{index}", "config": {"cidr": cidr}})
    assert "list_ipv4_addrs" not in host.names()


@pytest.mark.asyncio
async def test_nat_cloud_allocation_and_validation_skip_dhcp_bridges(host, labs_dir):
    _lab(labs_dir, "b.json", LAB_B, {"1": _bridge(1, LAB_B, config=_dhcp_config("10.255.0.0/24"))})
    lab = _lab(labs_dir, "a.json", LAB_A)
    svc = NetworkService()
    # Auto-allocation steps over the DHCP bridge's subnet.
    assert svc.validate_nat_cloud_config(lab, {})["cidr"] == "10.255.1.0/24"
    payload = await svc.create_network(lab, {"name": "nat", "type": "nat_cloud", "config": {}})
    assert payload["config"]["cidr"] == "10.255.1.0/24"
    # An explicit overlap is refused by both validate and create.
    for call in (
        lambda: svc.validate_nat_cloud_config(lab, {"cidr": "10.255.0.0/24"}),
    ):
        with pytest.raises(NetworkServiceError) as exc_info:
            call()
        assert exc_info.value.code == 409 and "DHCP bridge" in exc_info.value.message
    with pytest.raises(NetworkServiceError) as exc_info:
        await svc.create_network(
            lab, {"name": "nat2", "type": "nat_cloud", "config": {"cidr": "10.255.0.0/24"}}
        )
    assert exc_info.value.extra["field"] == "config.cidr"


# ---------------------------------------------------------------------------
# IPAM
# ---------------------------------------------------------------------------


def test_static_addresses_skip_the_dhcp_server_and_scope(host, labs_dir):
    lab = _lab(labs_dir, "a.json", LAB_A, {"1": _bridge(1, config=_dhcp_config("10.40.1.0/29"))})
    svc = NetworkService()
    assert [svc._allocate_ip(lab, 1), svc._allocate_ip(lab, 1)] == ["10.40.1.2", "10.40.1.3"]
    with pytest.raises(NetworkServiceError) as exc_info:
        svc._allocate_ip(lab, 1)
    assert exc_info.value.code == 409


def test_cidr_only_bridge_keeps_todays_ipam(host, labs_dir):
    config = {"cidr": "10.40.1.0/29", "dhcp": False, **bridge_dhcp_defaults(ipaddress.ip_network("10.40.1.0/29"))}
    lab = _lab(labs_dir, "a.json", LAB_A, {"1": _bridge(1, config=config, runtime={"used_ips": ["10.40.1.2", "10.40.1.3"]})})
    assert NetworkService()._allocate_ip(lab, 1) == "10.40.1.4"


@pytest.mark.parametrize(
    ("used", "field"), [("10.40.1.130", "config.dhcp_start"), ("10.40.1.254", "config.dhcp_server")]
)
@pytest.mark.asyncio
async def test_enabling_dhcp_over_node_addresses_is_refused(host, labs_dir, used, field):
    config = {"cidr": "10.40.1.0/24", "dhcp": False}
    lab = _lab(labs_dir, "a.json", LAB_A, {"1": _bridge(1, config=config, runtime={"used_ips": [used]})})
    before = (labs_dir / lab).read_text()
    with pytest.raises(NetworkServiceError) as exc_info:
        await NetworkService().patch_network(lab, 1, {"config": {"dhcp": True}})
    assert _error(exc_info)[:2] == (422, field)
    assert used in exc_info.value.message
    assert (labs_dir / lab).read_text() == before
    assert host.dhcp_calls() == [("list_ipv4_addrs",)]


# ---------------------------------------------------------------------------
# Edit: immutability, toggle, ownership, rollback
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("cidr", ["10.40.2.0/24", None, ""])
@pytest.mark.asyncio
async def test_cidr_cannot_change_or_go_away(host, labs_dir, cidr):
    lab = _lab(labs_dir, "a.json", LAB_A, {"1": _bridge(1, config={"cidr": "10.40.1.0/24"})})
    before = (labs_dir / lab).read_text()
    with pytest.raises(NetworkServiceError) as exc_info:
        await NetworkService().patch_network(lab, 1, {"config": {"cidr": cidr}})
    code, field, message = _error(exc_info)
    assert (code, field) == (422, "config.cidr")
    assert "delete and recreate" in message
    assert (labs_dir / lab).read_text() == before


@pytest.mark.asyncio
async def test_cidr_can_be_added_later_and_resent_unchanged(host, labs_dir):
    lab = _lab(labs_dir, "a.json", LAB_A, {"1": _bridge(1, config={})})
    svc = NetworkService()
    payload, _ = await svc.patch_network(lab, 1, {"config": {"cidr": "10.40.1.0/24"}})
    assert payload["config"]["cidr"] == "10.40.1.0/24"
    payload, _ = await svc.patch_network(lab, 1, {"config": {"cidr": "10.40.1.0/24", "dhcp_start": "10.40.1.200"}})
    assert payload["config"]["dhcp_start"] == "10.40.1.200"
    assert host.calls == []


@pytest.mark.asyncio
async def test_toggle_starts_and_stops_dhcp_without_touching_the_bridge(host, labs_dir):
    lab = _lab(labs_dir, "a.json", LAB_A, {"1": _bridge(1, config={"cidr": "10.40.1.0/24", "dhcp": False})})
    bridge = host_net.bridge_name(LAB_A, 1)
    svc = NetworkService()

    payload, _ = await svc.patch_network(lab, 1, {"config": {"dhcp": True}})
    assert payload["config"]["dhcp"] is True
    assert payload["runtime"]["dhcp"] is True and payload["runtime"]["dhcp_pid"] == 2222
    assert host.dhcp_calls()[1:] == [
        ("bridge_guard_apply", bridge),
        ("bridge_addr_add", bridge, "10.40.1.254/24"),
        ("link_up", bridge),
        ("dnsmasq_start_isolated", bridge, "10.40.1.254", "10.40.1.128", "10.40.1.253"),
    ]
    host.calls.clear()

    payload, _ = await svc.patch_network(lab, 1, {"config": {"dhcp": False}})
    assert host.calls == [
        ("dnsmasq_stop", bridge),
        ("bridge_addr_del", bridge, "10.40.1.254/24"),
        ("bridge_guard_remove", bridge),
    ]
    # Off keeps the scope for the next time, but nothing is applied.
    assert payload["config"]["dhcp_server"] == "10.40.1.254"
    assert payload["runtime"]["dhcp"] is False and "dhcp_server" not in payload["runtime"]
    assert not {"bridge_add", "bridge_del"} & set(host.names())


@pytest.mark.asyncio
async def test_moving_the_server_swaps_the_address(host, labs_dir):
    lab = _lab(labs_dir, "a.json", LAB_A, {"1": _bridge(1, config=_dhcp_config(), runtime={"dhcp": True})})
    bridge = host_net.bridge_name(LAB_A, 1)
    await NetworkService().patch_network(lab, 1, {"config": {"dhcp_server": "10.40.1.1"}})
    assert host.calls == [
        ("bridge_guard_apply", bridge),
        ("bridge_addr_add", bridge, "10.40.1.1/24"),
        ("link_up", bridge),
        ("dnsmasq_start_isolated", bridge, "10.40.1.1", "10.40.1.128", "10.40.1.253"),
        ("bridge_addr_del", bridge, "10.40.1.254/24"),
    ]


@pytest.mark.asyncio
async def test_nat_cloud_dhcp_toggle_keeps_bridge_and_nat(host, labs_dir):
    lab = _lab(labs_dir, "a.json", LAB_A, {"1": _nat_cloud(1, LAB_A, "10.70.0.0/24")})
    svc = NetworkService()
    await svc.patch_network(lab, 1, {"config": {"dhcp": False}})
    await svc.patch_network(lab, 1, {"config": {"dhcp": True}})
    assert host.names() == ["dnsmasq_stop", "dnsmasq_start"]


@pytest.mark.asyncio
async def test_failed_enable_restores_host_and_file(host, labs_dir):
    lab = _lab(labs_dir, "a.json", LAB_A, {"1": _bridge(1, config={"cidr": "10.40.1.0/24"})})
    before = (labs_dir / lab).read_text()
    bridge = host_net.bridge_name(LAB_A, 1)
    host.fail["bridge_addr_add"] = host_net.HostNetUnknown("no")
    with pytest.raises(NetworkServiceError) as exc_info:
        await NetworkService().patch_network(lab, 1, {"config": {"dhcp": True}})
    assert exc_info.value.code == 500
    assert (labs_dir / lab).read_text() == before
    assert host.calls[-3:] == [
        ("dnsmasq_stop", bridge),
        ("bridge_addr_del", bridge, "10.40.1.254/24"),
        ("bridge_guard_remove", bridge),
    ]


@pytest.mark.asyncio
async def test_failed_disable_restarts_the_server(host, labs_dir):
    lab = _lab(labs_dir, "a.json", LAB_A, {"1": _bridge(1, config=_dhcp_config(), runtime={"dhcp": True})})
    before = (labs_dir / lab).read_text()
    host.fail["bridge_guard_remove"] = host_net.HostNetUnknown("no")
    with pytest.raises(NetworkServiceError):
        await NetworkService().patch_network(lab, 1, {"config": {"dhcp": False}})
    assert (labs_dir / lab).read_text() == before
    assert host.names()[-4:] == ["bridge_guard_apply", "bridge_addr_add", "link_up", "dnsmasq_start_isolated"]


@pytest.mark.asyncio
async def test_failed_write_puts_the_host_back(host, labs_dir, monkeypatch):
    lab = _lab(labs_dir, "a.json", LAB_A, {"1": _bridge(1, config={"cidr": "10.40.1.0/24"})})

    def broken_write(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(LabService, "write_lab_json_static", staticmethod(broken_write))
    with pytest.raises(OSError):
        await NetworkService().patch_network(lab, 1, {"config": {"dhcp": True}})
    assert host.names()[-3:] == ["dnsmasq_stop", "bridge_addr_del", "bridge_guard_remove"]


@pytest.mark.asyncio
async def test_explicit_dhcp_repairs_a_drifted_server(host, labs_dir, monkeypatch):
    lab = _lab(labs_dir, "a.json", LAB_A, {"1": _bridge(1, config=_dhcp_config(), runtime={"dhcp": True})})
    svc = NetworkService()
    await svc.patch_network(lab, 1, {"config": {"dhcp": True}})  # server is not running
    assert host.names() == ["bridge_guard_apply", "bridge_addr_add", "link_up", "dnsmasq_start_isolated"]

    host.calls.clear()
    monkeypatch.setattr("app.services.network_info_service.dhcp_server_pid", lambda bridge: 77)
    await svc.patch_network(lab, 1, {"config": {"dhcp": True}})  # running: nothing to do
    assert host.calls == []


@pytest.mark.asyncio
async def test_members_inherit_and_cannot_configure(host, labs_dir):
    member = _bridge(1, config={**_dhcp_config(), "shared_network_id": f"shared-net:{LAB_B}:1"},
                     runtime={"shared_reference": True})
    lab = _lab(labs_dir, "a.json", LAB_A, {"1": member})
    svc = NetworkService()
    with pytest.raises(NetworkServiceError) as exc_info:
        await svc.patch_network(lab, 1, {"config": {"dhcp": False}})
    assert exc_info.value.code == 422 and "owns it" in exc_info.value.message
    with pytest.raises(NetworkServiceError) as exc_info:
        svc.validate_network_config(lab, "linux_bridge", {"dhcp": False}, network_id=1)
    assert exc_info.value.code == 422
    assert host.calls == []


@pytest.mark.asyncio
async def test_dhcp_is_not_for_point_to_point_or_other_types(host, labs_dir):
    lab = _lab(
        labs_dir,
        "a.json",
        LAB_A,
        {
            "1": _bridge(1, config={"cidr": "10.40.1.0/24"}, implicit=True),
            "2": {**_bridge(2), "type": "ovs_bridge"},
            "3": _bridge(3, config=_dhcp_config(), runtime={"dhcp": True}),
        },
    )
    svc = NetworkService()
    with pytest.raises(NetworkServiceError) as exc_info:
        await svc.patch_network(lab, 1, {"config": {"dhcp": True}})
    assert "point-to-point" in exc_info.value.message
    with pytest.raises(NetworkServiceError) as exc_info:
        await svc.patch_network(lab, 2, {"config": {"dhcp": True}})
    assert exc_info.value.extra["field"] == "config.dhcp"
    with pytest.raises(NetworkServiceError) as exc_info:
        await svc.patch_network(lab, 3, {"type": "ovs_bridge"})
    assert exc_info.value.extra["field"] == "type"
    assert host.calls == []


# ---------------------------------------------------------------------------
# Delete, lab cleanup, reconcile
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_removes_server_address_and_guard(host, labs_dir):
    lab = _lab(labs_dir, "a.json", LAB_A, {"1": _bridge(1, config=_dhcp_config(), runtime={"dhcp": True, "dhcp_server": "10.40.1.254"}),
                                           "2": _bridge(2, config={"cidr": "10.50.0.0/24"})})
    svc = NetworkService()
    bridge = host_net.bridge_name(LAB_A, 1)
    await svc.delete_network(lab, 1)
    assert host.calls == [
        ("dnsmasq_stop", bridge),
        ("bridge_addr_del", bridge, "10.40.1.254/24"),
        ("bridge_guard_remove", bridge),
        ("bridge_del", bridge),
    ]
    host.calls.clear()
    await svc.delete_network(lab, 2)
    assert host.names() == ["bridge_del"]


def test_lab_cleanup_removes_dhcp_state(host, labs_dir):
    data = {
        "id": LAB_A,
        "networks": {
            "1": _bridge(1, config=_dhcp_config(), runtime={"dhcp": True}),
            "2": _bridge(2, config={"cidr": "10.50.0.0/24"}),
            "3": _bridge(3, config={**_dhcp_config("10.60.0.0/24"), "shared_network_id": "shared-net:x:1"},
                         runtime={"shared_reference": True}),
        },
    }
    _cleanup_lab_network_runtime(data, "a.json")
    b1, b2 = host_net.bridge_name(LAB_A, 1), host_net.bridge_name(LAB_A, 2)
    assert host.calls == [
        ("dnsmasq_stop", b1),
        ("bridge_addr_del", b1, "10.40.1.254/24"),
        ("bridge_guard_remove", b1),
        ("bridge_del", b1),
        ("bridge_del", b2),
    ]


def test_reconcile_restores_dhcp_and_restarts_only_on_drift(host, labs_dir, monkeypatch):
    lab = _lab(labs_dir, "a.json", LAB_A, {"1": _bridge(1, config=_dhcp_config())})
    bridge = host_net.bridge_name(LAB_A, 1)
    host.existing.add(bridge)
    svc = NetworkService()
    summary = svc.ensure_lab_bridges(lab)
    assert summary["dhcp_bridges"] == [{"network_id": 1, "bridge": bridge}]
    assert host.dhcp_calls() == [
        ("bridge_guard_apply", bridge),
        ("bridge_addr_add", bridge, "10.40.1.254/24"),
        ("link_up", bridge),
        ("dnsmasq_start_isolated", bridge, "10.40.1.254", "10.40.1.128", "10.40.1.253"),
    ]
    assert _saved(labs_dir, lab)["networks"]["1"]["runtime"]["dhcp_pid"] == 2222

    host.calls.clear()
    monkeypatch.setattr("app.services.network_info_service.dhcp_server_pid", lambda b: 2222)
    svc.ensure_lab_bridges(lab)
    assert "dnsmasq_start_isolated" not in host.names()
    assert "bridge_guard_apply" in host.names()


def test_reconcile_honours_dhcp_false(host, labs_dir):
    lab = _lab(
        labs_dir,
        "a.json",
        LAB_A,
        {
            # Turned off while the host still has it applied.
            "1": _bridge(1, config={**_dhcp_config(), "dhcp": False},
                         runtime={"dhcp": True, "dhcp_server": "10.40.1.254", "dhcp_start": "10.40.1.128",
                                  "dhcp_end": "10.40.1.253"}),
            # Never on: nothing to do.
            "2": _bridge(2, config={**_dhcp_config("10.50.0.0/24"), "dhcp": False}),
        },
    )
    b1, b2 = host_net.bridge_name(LAB_A, 1), host_net.bridge_name(LAB_A, 2)
    host.existing.update({b1, b2})
    NetworkService().ensure_lab_bridges(lab)
    assert host.dhcp_calls() == [
        ("dnsmasq_stop", b1),
        ("bridge_addr_del", b1, "10.40.1.254/24"),
        ("bridge_guard_remove", b1),
    ]
    runtime = _saved(labs_dir, lab)["networks"]["1"]["runtime"]
    assert runtime["dhcp"] is False and "dhcp_server" not in runtime


def test_reconcile_ignores_legacy_dhcp_flag_without_settings(host, labs_dir):
    lab = _lab(labs_dir, "a.json", LAB_A, {"1": _bridge(1, config={"cidr": "10.40.1.0/24", "dhcp": True})})
    host.existing.add(host_net.bridge_name(LAB_A, 1))
    NetworkService().ensure_lab_bridges(lab)
    assert host.dhcp_calls() == []


def test_member_lab_open_restores_the_owners_dhcp(host, labs_dir):
    owner_lab = _lab(labs_dir, "owner.json", LAB_B, {"1": {**_bridge(1, LAB_B, config=_dhcp_config()), "shared": True}})
    bridge = host_net.bridge_name(LAB_B, 1)
    member = _bridge(1, LAB_A, config={**_dhcp_config(), "shared_network_id": f"shared-net:{LAB_B}:1"},
                     runtime={"shared_reference": True, "bridge_name": bridge})
    lab = _lab(labs_dir, "member.json", LAB_A, {"1": member})
    host.existing.add(bridge)
    summary = NetworkService().ensure_lab_bridges(lab)
    assert ("dnsmasq_start_isolated", bridge, "10.40.1.254", "10.40.1.128", "10.40.1.253") in host.calls
    assert summary["dhcp_bridges"] == [{"network_id": 1, "bridge": bridge, "owner_lab": "owner"}]
    # The owner's record carries the runtime; the member's stays as it was.
    assert _saved(labs_dir, owner_lab)["networks"]["1"]["runtime"]["dhcp"] is True
    assert "dhcp" not in _saved(labs_dir, lab)["networks"]["1"]["runtime"]


# ---------------------------------------------------------------------------
# Info panel
# ---------------------------------------------------------------------------


def test_info_shows_cidr_and_dhcp_for_bridges(host, labs_dir):
    lab = _lab(
        labs_dir,
        "a.json",
        LAB_A,
        {
            "1": _bridge(1, config=_dhcp_config()),
            "2": _bridge(2, config={**_dhcp_config("10.50.0.0/24"), "dhcp": False}),
            "3": _bridge(3, config={"cidr": "10.0.0.0/21"}),
            "4": _bridge(4, config={}),
        },
    )
    ip = build_network_info(lab, 1, lambda path: True)["ip"]
    assert ip["cidr"] == "10.40.1.0/24" and ip["nat"] is False
    assert {k: ip["dhcp"][k] for k in ("enabled", "server", "start", "end", "size", "running", "leases")} == {
        "enabled": True,
        "server": "10.40.1.254",
        "start": "10.40.1.128",
        "end": "10.40.1.253",
        "size": 126,
        "running": False,
        "leases": [],
    }
    assert build_network_info(lab, 2, lambda path: True)["ip"]["dhcp"]["enabled"] is False
    assert build_network_info(lab, 3, lambda path: True)["ip"]["dhcp"] is None
    assert build_network_info(lab, 4, lambda path: True)["ip"] is None


# ---------------------------------------------------------------------------
# HTTP API
# ---------------------------------------------------------------------------


@pytest.fixture()
def api(host, settings, monkeypatch):
    monkeypatch.setattr("app.services.template_service.get_settings", lambda: settings)
    user = {"value": SimpleNamespace(username="admin", role="admin", html5=True, folder="/")}
    app.dependency_overrides[get_current_user] = lambda: user["value"]
    yield user
    app.dependency_overrides.clear()


def _client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://t")


@pytest.mark.asyncio
async def test_validate_config_returns_defaults_and_changes_nothing(api, host, labs_dir):
    lab = _lab(labs_dir, "a.json", LAB_A, {"1": _bridge(1, config={"cidr": "10.40.1.0/24"})})
    before = (labs_dir / lab).read_text()
    async with _client() as c:
        new = await c.post(f"/api/labs/{lab}/networks/validate-config",
                           json={"type": "linux_bridge", "config": {"cidr": "10.8.0.0/22", "dhcp": True}})
        edit = await c.post(f"/api/labs/{lab}/networks/validate-config",
                            json={"type": "linux_bridge", "network_id": 1, "config": {"dhcp": True}})
        bad = await c.post(f"/api/labs/{lab}/networks/validate-config",
                           json={"type": "linux_bridge", "config": {"cidr": "10.0.0.0/30", "dhcp": True}})
        nat = await c.post(f"/api/labs/{lab}/networks/validate-config", json={"type": "nat_cloud", "config": {}})
        other = await c.post(f"/api/labs/{lab}/networks/validate-config", json={"type": "ovs_bridge", "config": {}})
    assert new.status_code == 200, new.text
    assert new.json()["data"]["config"] == {
        "cidr": "10.8.0.0/22",
        "dhcp": True,
        "dhcp_server": "10.8.3.254",
        "dhcp_start": "10.8.2.0",
        "dhcp_end": "10.8.3.253",
    }
    assert edit.json()["data"]["config"]["dhcp_server"] == "10.40.1.254"
    assert (bad.status_code, bad.json()["field"]) == (422, "config.dhcp")
    assert nat.json()["data"]["config"]["gateway"] == "10.255.0.1"
    assert (other.status_code, other.json()["field"]) == (422, "type")
    assert (labs_dir / lab).read_text() == before
    assert set(host.names()) <= {"list_ipv4_addrs"}


@pytest.mark.asyncio
async def test_validate_config_needs_lab_access(api, host, labs_dir):
    lab = _lab(labs_dir, "a.json", LAB_A)
    api["value"] = SimpleNamespace(username="alice", role="user", html5=True, folder="alice")
    async with _client() as c:
        resp = await c.post(f"/api/labs/{lab}/networks/validate-config",
                            json={"type": "linux_bridge", "config": {"cidr": "10.40.1.0/24", "dhcp": True}})
    assert resp.status_code == 403
    assert host.calls == []


@pytest.mark.asyncio
async def test_api_create_patch_and_put_follow_the_rules(api, host, labs_dir):
    lab = _lab(labs_dir, "a.json", LAB_A)
    async with _client() as c:
        created = await c.post(f"/api/labs/{lab}/networks", json={
            "name": "lan", "type": "linux_bridge",
            "config": {"cidr": "10.40.1.0/24", "dhcp": True, "dhcp_server": "10.40.1.1"},
        })
        assert created.status_code == 201, created.text
        assert created.json()["network"]["config"]["dhcp_server"] == "10.40.1.1"
        net_id = created.json()["network"]["id"]

        for method in (c.patch, c.put):
            resp = await method(f"/api/labs/{lab}/networks/{net_id}", json={"config": {"cidr": "10.41.0.0/24"}})
            assert (resp.status_code, resp.json()["field"]) == (422, "config.cidr"), resp.text

        off = await c.patch(f"/api/labs/{lab}/networks/{net_id}", json={"config": {"dhcp": False}})
        assert off.status_code == 200 and off.json()["network"]["config"]["dhcp"] is False

        # The legacy PUT takes the same validated path.
        on = await c.put(f"/api/labs/{lab}/networks/{net_id}", json={"config": {"dhcp": True}})
        assert on.status_code == 200, on.text
        assert on.json()["data"]["runtime"]["dhcp"] is True

        info = await c.get(f"/api/labs/{lab}/networks/{net_id}/info")
    assert info.json()["data"]["ip"]["dhcp"]["server"] == "10.40.1.1"
    assert host.names().count("dnsmasq_start_isolated") == 2


@pytest.mark.asyncio
async def test_api_patch_conflict_hides_labs_the_user_cannot_open(api, host, labs_dir):
    (labs_dir / "private").mkdir()
    _lab(labs_dir, "private/b.json", LAB_B, {"1": _bridge(1, LAB_B, config=_dhcp_config())})
    (labs_dir / "alice").mkdir()
    lab = _lab(labs_dir, "alice/a.json", LAB_A, {"1": _bridge(1, config={"cidr": "10.40.1.0/24"})})
    api["value"] = SimpleNamespace(username="alice", role="user", html5=True, folder="alice")
    async with _client() as c:
        resp = await c.patch(f"/api/labs/{lab}/networks/1", json={"config": {"dhcp": True}})
    assert resp.status_code == 409
    assert resp.json()["source"] == "a DHCP bridge in another lab"
    assert "private" not in resp.json()["message"]
