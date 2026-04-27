"""Tests for the (network_id, planned_mac) registry — US-065.

Acceptance criteria covered (see .omc/prd.json US-065):

  - test_registry_key_includes_network_id
  - test_two_networks_same_mac_no_collision
  - test_one_network_dup_mac_409
  - test_link_retarget_updates_registry
  - test_isolated_bridges_no_collision
  - test_host_nic_never_probed
"""
from __future__ import annotations

import ast

from app.services import mac_registry as mac_registry_module
from app.services.mac_registry import MacRegistry


def make_lab(networks: dict, nodes: dict, links: list) -> dict:
    """Minimal v2 lab.json shape for in-memory tests."""
    return {
        "schema": 2,
        "id": "x",
        "meta": {"name": "x"},
        "viewport": {"x": 0, "y": 0, "zoom": 1.0},
        "nodes": nodes,
        "networks": networks,
        "links": links,
        "defaults": {"link_style": "orthogonal"},
    }


def _node(node_id: int, interfaces: list[dict]) -> dict:
    return {
        "id": node_id,
        "name": f"r{node_id}",
        "interfaces": interfaces,
    }


def _iface(index: int, planned_mac: str | None) -> dict:
    return {
        "index": index,
        "name": f"eth{index}",
        "planned_mac": planned_mac,
        "port_position": None,
    }


def _network(network_id: int, name: str = "lan") -> dict:
    return {
        "id": network_id,
        "name": name,
        "type": "linux_bridge",
        "visibility": True,
        "implicit": False,
    }


def _node_to_network_link(
    link_id: str, node_id: int, interface_index: int, network_id: int
) -> dict:
    return {
        "id": link_id,
        "from": {"node_id": node_id, "interface_index": interface_index},
        "to": {"network_id": network_id},
        "style_override": None,
        "label": "",
        "color": "",
        "width": "1",
        "metrics": {
            "delay_ms": 0,
            "loss_pct": 0,
            "bandwidth_kbps": 0,
            "jitter_ms": 0,
        },
    }


def test_registry_key_includes_network_id() -> None:
    registry = MacRegistry()
    lab = make_lab(
        networks={"10": _network(10)},
        nodes={
            "1": _node(1, [_iface(0, "50:00:00:00:00:01")]),
        },
        links=[_node_to_network_link("lnk_1", 1, 0, 10)],
    )
    registry.recompute_for_lab("lab_a", lab)

    # Access internal map only to verify key shape per AC.
    assert registry._entries, "registry should contain at least one entry"
    for key in registry._entries:
        assert isinstance(key, tuple)
        assert len(key) == 2, f"key {key!r} must be a 2-tuple (network_id, mac)"
        network_id, mac = key
        assert isinstance(network_id, int)
        assert isinstance(mac, str)
        assert mac == mac.lower()


def test_two_networks_same_mac_no_collision() -> None:
    registry = MacRegistry()
    same_mac = "50:00:00:AA:BB:CC"
    lab = make_lab(
        networks={"10": _network(10, "lan_a"), "20": _network(20, "lan_b")},
        nodes={
            "1": _node(1, [_iface(0, same_mac)]),
            "2": _node(2, [_iface(0, same_mac)]),
        },
        links=[
            _node_to_network_link("lnk_1", 1, 0, 10),
            _node_to_network_link("lnk_2", 2, 0, 20),
        ],
    )
    registry.recompute_for_lab("lab_a", lab)

    assert registry.check_collision(10, same_mac, owner_key=("lab_a", 1, 0)) is None
    assert registry.check_collision(20, same_mac, owner_key=("lab_a", 2, 0)) is None


def test_one_network_dup_mac_409() -> None:
    registry = MacRegistry()
    dup = "50:00:00:DE:AD:01"
    # First registration via recompute.
    lab = make_lab(
        networks={"10": _network(10)},
        nodes={"1": _node(1, [_iface(0, dup)])},
        links=[_node_to_network_link("lnk_1", 1, 0, 10)],
    )
    registry.recompute_for_lab("lab_a", lab)

    # A different interface (different lab/node) trying to take the same
    # (network_id, mac) must collide.
    conflict = registry.check_collision(10, dup, owner_key=("lab_b", 99, 0))
    assert conflict == ("lab_a", 1, 0)

    # Case-insensitive lookup: lower-case input still collides.
    conflict_lower = registry.check_collision(10, dup.lower(), owner_key=("lab_b", 99, 0))
    assert conflict_lower == ("lab_a", 1, 0)

    # The owner of the existing entry can re-register without collision.
    assert registry.check_collision(10, dup, owner_key=("lab_a", 1, 0)) is None


def test_link_retarget_updates_registry() -> None:
    registry = MacRegistry()
    mac = "50:00:00:11:22:33"

    # Initial: interface 0 of node 1 lives on network 10.
    lab_v1 = make_lab(
        networks={"10": _network(10, "net_a"), "20": _network(20, "net_b")},
        nodes={"1": _node(1, [_iface(0, mac)])},
        links=[_node_to_network_link("lnk_1", 1, 0, 10)],
    )
    registry.recompute_for_lab("lab_a", lab_v1)
    assert (10, mac.lower()) in registry._entries
    assert (20, mac.lower()) not in registry._entries

    # Retarget the link to network 20 — same lab, same interface.
    lab_v2 = make_lab(
        networks={"10": _network(10, "net_a"), "20": _network(20, "net_b")},
        nodes={"1": _node(1, [_iface(0, mac)])},
        links=[_node_to_network_link("lnk_1", 1, 0, 20)],
    )
    registry.recompute_for_lab("lab_a", lab_v2)

    assert (10, mac.lower()) not in registry._entries, "old (net_A, mac) must be evicted"
    assert (20, mac.lower()) in registry._entries, "new (net_B, mac) must exist"
    assert registry._entries[(20, mac.lower())] == ("lab_a", 1, 0)


def test_isolated_bridges_no_collision() -> None:
    registry = MacRegistry()
    shared_mac = "50:00:00:CA:FE:01"

    # Lab A: a linux_bridge with id=10 and a node interface on it.
    lab_a = make_lab(
        networks={"10": _network(10, "br0")},
        nodes={"1": _node(1, [_iface(0, shared_mac)])},
        links=[_node_to_network_link("lnk_1", 1, 0, 10)],
    )
    # Lab B: its own linux_bridge but a different network_id (20) — different
    # L2 namespace, even though both labs use the same MAC.
    lab_b = make_lab(
        networks={"20": _network(20, "br0")},
        nodes={"1": _node(1, [_iface(0, shared_mac)])},
        links=[_node_to_network_link("lnk_1", 1, 0, 20)],
    )

    registry.recompute_for_lab("lab_a", lab_a)
    registry.recompute_for_lab("lab_b", lab_b)

    # No collision for lab_a's owner on net 10.
    assert registry.check_collision(10, shared_mac, owner_key=("lab_a", 1, 0)) is None
    # No collision for lab_b's owner on net 20.
    assert registry.check_collision(20, shared_mac, owner_key=("lab_b", 1, 0)) is None
    # And they truly are stored under different keys.
    assert (10, shared_mac.lower()) in registry._entries
    assert (20, shared_mac.lower()) in registry._entries


def test_host_nic_never_probed() -> None:
    """The registry must not introspect the host NIC table."""
    source_path = mac_registry_module.__file__
    with open(source_path, "r", encoding="utf-8") as fh:
        source = fh.read()

    # Sentinel string check: the host /sys path must never appear at all.
    assert "/sys/class/net" not in source, (
        "mac_registry.py must not reference /sys/class/net"
    )

    tree = ast.parse(source)

    forbidden_attr_calls = {
        ("socket", "if_nameindex"),
        ("fcntl", "ioctl"),
        ("psutil", "net_if_addrs"),
    }
    forbidden_bare_names = {"getifaddrs"}
    forbidden_imports = {"psutil", "fcntl"}

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                assert root not in forbidden_imports, (
                    f"forbidden import: {alias.name}"
                )
        elif isinstance(node, ast.ImportFrom):
            mod = (node.module or "").split(".")[0]
            assert mod not in forbidden_imports, (
                f"forbidden from-import: {node.module}"
            )
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
                pair = (func.value.id, func.attr)
                assert pair not in forbidden_attr_calls, (
                    f"forbidden host-NIC call: {pair[0]}.{pair[1]}"
                )
            elif isinstance(func, ast.Name):
                assert func.id not in forbidden_bare_names, (
                    f"forbidden host-NIC call: {func.id}"
                )
