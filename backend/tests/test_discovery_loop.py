# Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""Tests for US-402 / US-404 — ``NodeRuntimeService._discovery_loop``.

These tests cover:

* Kernel-side bridge member with no matching ``links[]`` entry triggers
  a ``discovered_link`` WS event with the expected payload shape (US-402).
* Members corresponding to declared links are not re-flagged (US-402).
* Hot-plug-in-flight links recorded in ``transition_lease`` are skipped
  (US-402 + US-404).
* ``_discovery_loop`` re-reads ``get_settings()`` per iteration so live
  cadence edits land within one cycle (US-402).
* US-404: declared links whose veth/TAP is missing from the kernel emit
  a ``link_divergent`` WS event with ``link_id``, ``lab_id``, ``reason``
  and ISO-8601 ``last_checked`` timestamp; declared links that ARE present
  in the kernel are not flagged; leased divergent links are skipped.
"""

from __future__ import annotations

import asyncio
import json
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services import host_net
from app.services.node_runtime_service import NodeRuntimeService


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_registry():
    NodeRuntimeService.reset_registry()
    yield
    NodeRuntimeService.reset_registry()


@pytest.fixture(autouse=True)
def running_nodes(monkeypatch):
    """Node ids the divergence check treats as running (default: all)."""
    running: set[int] | None = None
    holder = {"ids": running}

    def is_running(cls, lab_id, node_id):
        return holder["ids"] is None or node_id in holder["ids"]

    monkeypatch.setattr(NodeRuntimeService, "_node_is_running", classmethod(is_running))
    return holder


@pytest.fixture()
def discovery_settings(tmp_path):
    labs_dir = tmp_path / "labs"
    labs_dir.mkdir()
    tmp_dir = tmp_path / "tmp"
    tmp_dir.mkdir()
    return SimpleNamespace(
        LABS_DIR=labs_dir,
        TMP_DIR=tmp_dir,
        DOCKER_HOST="unix:///var/run/docker.sock",
        DISCOVERY_CADENCE_SECONDS=30,
    )


@pytest.fixture()
def stub_instance_id(monkeypatch, tmp_path):
    """Provide a stable instance_id so ``host_net.bridge_name`` is deterministic."""
    instance_dir = tmp_path / "instance"
    instance_dir.mkdir()
    (instance_dir / "instance_id").write_text(
        "11111111-2222-3333-4444-555555555555\n"
    )
    monkeypatch.setenv("NOVA_VE_INSTANCE_DIR", str(instance_dir))
    monkeypatch.delenv("NOVA_VE_INSTANCE_ID", raising=False)
    monkeypatch.delenv("NOVA_VE_INSTANCE_ID_OVERRIDE_OK", raising=False)
    yield


def _write_lab(labs_dir: Path, lab_id: str, links: list[dict], filename: str | None = None) -> str:
    data = {
        "schema": 2,
        "id": lab_id,
        "meta": {"name": "test-lab"},
        "viewport": {"x": 0, "y": 0, "zoom": 1.0},
        "nodes": {
            "1": {"id": 1, "type": "docker", "interfaces": [{"index": 0}]},
            "2": {"id": 2, "type": "docker", "interfaces": [{"index": 0}]},
        },
        "networks": {
            "1": {
                "id": 1,
                "name": "lab-link",
                "type": "linux_bridge",
                "runtime": {
                    "bridge_name": host_net.bridge_name(lab_id, 1),
                },
            },
        },
        "links": links,
        "defaults": {"link_style": "orthogonal"},
    }
    filename = filename or f"{lab_id}.json"
    lab_file = labs_dir / filename
    lab_file.parent.mkdir(parents=True, exist_ok=True)
    lab_file.write_text(json.dumps(data))
    return filename


class _RecordingHub:
    """Drop-in stand-in for ``app.services.ws_hub.ws_hub``."""

    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict]] = []

    async def publish(
        self, lab_id: str, event_type: str, payload: dict, rev: str = "", *, guard, identity
    ) -> None:
        async with guard as valid:
            if valid:
                self.events.append((lab_id, event_type, payload))


def _patch_ws_hub(monkeypatch) -> _RecordingHub:
    hub = _RecordingHub()
    fake_module = types.ModuleType("app.services.ws_hub")
    fake_module.ws_hub = hub  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "app.services.ws_hub", fake_module)
    return hub


def _patch_settings(monkeypatch, settings) -> None:
    monkeypatch.setattr(
        "app.services.node_runtime_service.get_settings", lambda: settings
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("filename", ["discovery.json", "team/project/discovery.json"])
async def test_unknown_bridge_member_triggers_discovered_link(
    monkeypatch, discovery_settings, stub_instance_id, filename
):
    """A kernel-side bridge member with no matching ``links[]`` entry must
    publish a ``discovered_link`` WS event."""
    lab_id = "lab-discover-1"
    # Lab declares link from node 1 — node 2 is undeclared.
    declared_link = {
        "id": "lnk_001",
        "from": {"node_id": 1, "interface_index": 0},
        "to": {"network_id": 1},
    }
    _write_lab(discovery_settings.LABS_DIR, lab_id, [declared_link], filename)

    bridge = host_net.bridge_name(lab_id, 1)
    declared_iface = host_net.veth_host_name(lab_id, 1, 0)
    rogue_iface = host_net.veth_host_name(lab_id, 2, 0)

    monkeypatch.setattr(
        NodeRuntimeService,
        "_bridge_members_sync",
        staticmethod(lambda b: [declared_iface, rogue_iface] if b == bridge else []),
    )
    _patch_settings(monkeypatch, discovery_settings)
    hub = _patch_ws_hub(monkeypatch)

    await NodeRuntimeService._run_discovery_cycle()

    discovered = [e for e in hub.events if e[1] == "discovered_link"]
    assert len(discovered) == 1, hub.events
    assert discovered[0][0] == filename
    payload = discovered[0][2]
    assert payload["lab_id"] == lab_id
    assert payload["network_id"] == 1
    assert payload["bridge_name"] == bridge
    assert payload["iface"] == rogue_iface
    assert payload["peer_node_id"] == 2
    assert isinstance(payload["generation"], int) and payload["generation"] >= 1


@pytest.mark.asyncio
async def test_discovery_skips_ambiguous_runtime_identity(monkeypatch, discovery_settings, stub_instance_id):
    lab_id = "duplicate-id"
    root = discovery_settings.LABS_DIR
    _write_lab(root, lab_id, [], "lab.json")
    _write_lab(root, lab_id, [], "team/lab.json")
    rogue_iface = host_net.veth_host_name(lab_id, 1, 0)
    monkeypatch.setattr(NodeRuntimeService, "_bridge_members_sync", staticmethod(lambda bridge: [rogue_iface]))
    _patch_settings(monkeypatch, discovery_settings)
    hub = _patch_ws_hub(monkeypatch)

    await NodeRuntimeService._run_discovery_cycle()

    assert hub.events == []


@pytest.mark.asyncio
async def test_declared_link_is_not_flagged(
    monkeypatch, discovery_settings, stub_instance_id
):
    """When every kernel-side member matches a declared ``links[]`` entry,
    no ``discovered_link`` event is emitted."""
    lab_id = "lab-discover-2"
    links = [
        {"id": "lnk_001", "from": {"node_id": 1, "interface_index": 0},
         "to": {"network_id": 1}},
        {"id": "lnk_002", "from": {"node_id": 2, "interface_index": 0},
         "to": {"network_id": 1}},
    ]
    _write_lab(discovery_settings.LABS_DIR, lab_id, links)

    bridge = host_net.bridge_name(lab_id, 1)
    members = [
        host_net.veth_host_name(lab_id, 1, 0),
        host_net.veth_host_name(lab_id, 2, 0),
    ]
    monkeypatch.setattr(
        NodeRuntimeService,
        "_bridge_members_sync",
        staticmethod(lambda b: members if b == bridge else []),
    )
    _patch_settings(monkeypatch, discovery_settings)
    hub = _patch_ws_hub(monkeypatch)

    await NodeRuntimeService._run_discovery_cycle()

    discovered = [e for e in hub.events if e[1] == "discovered_link"]
    assert discovered == []


@pytest.mark.asyncio
async def test_leased_link_is_skipped(
    monkeypatch, discovery_settings, stub_instance_id
):
    """Links currently in a transition lease (in-flight hot-plug from
    US-204b) must NOT emit a ``discovered_link`` event."""
    lab_id = "lab-discover-3"
    _write_lab(discovery_settings.LABS_DIR, lab_id, [])  # no declared links

    bridge = host_net.bridge_name(lab_id, 1)
    rogue_iface = host_net.veth_host_name(lab_id, 1, 0)

    monkeypatch.setattr(
        NodeRuntimeService,
        "_bridge_members_sync",
        staticmethod(lambda b: [rogue_iface] if b == bridge else []),
    )
    _patch_settings(monkeypatch, discovery_settings)

    # Inject a fake ``transition_lease`` module that claims the iface is
    # currently leased (mid hot-plug).
    fake_lease = types.ModuleType("app.services.transition_lease")
    fake_lease.is_leased = lambda *, lab_id, link_id: link_id == rogue_iface  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "app.services.transition_lease", fake_lease)

    hub = _patch_ws_hub(monkeypatch)
    await NodeRuntimeService._run_discovery_cycle()

    assert [e for e in hub.events if e[1] == "discovered_link"] == []


@pytest.mark.asyncio
async def test_discovery_loop_honors_live_cadence_change(monkeypatch):
    """The loop reads ``get_settings()`` per iteration — mutating
    ``DISCOVERY_CADENCE_SECONDS`` between cycles must change the next sleep.

    We assert this by capturing every value passed to ``asyncio.sleep`` and
    confirming the second value reflects the post-mutation cadence.
    """
    settings = SimpleNamespace(DISCOVERY_CADENCE_SECONDS=30, LABS_DIR=Path("/nope"))
    _patch_settings(monkeypatch, settings)

    sleeps: list[float] = []
    iterations = {"count": 0}

    async def _fake_sleep(seconds):
        sleeps.append(seconds)
        iterations["count"] += 1
        if iterations["count"] == 1:
            # Simulate a live config edit between cycles.
            settings.DISCOVERY_CADENCE_SECONDS = 45
        if iterations["count"] >= 3:
            raise asyncio.CancelledError()

    async def _noop_cycle():
        return None

    monkeypatch.setattr(
        "app.services.node_runtime_service.asyncio.sleep", _fake_sleep
    )
    monkeypatch.setattr(NodeRuntimeService, "_run_discovery_cycle", classmethod(
        lambda cls: _noop_cycle()
    ))

    with pytest.raises(asyncio.CancelledError):
        await NodeRuntimeService._discovery_loop()

    # Iteration 1 used the initial cadence (30); iteration 2 used the
    # mutated cadence (45) — proving the loop re-reads settings live.
    assert sleeps[0] == 30
    assert sleeps[1] == 45


@pytest.mark.asyncio
async def test_discovery_loop_clamps_below_minimum(monkeypatch):
    """If somehow a settings instance reports a value below 5 (e.g. a test
    fixture bypasses validation), the loop floors to 5 to avoid a tight
    spin."""
    settings = SimpleNamespace(DISCOVERY_CADENCE_SECONDS=1, LABS_DIR=Path("/nope"))
    _patch_settings(monkeypatch, settings)

    captured: list[float] = []

    async def _fake_sleep(seconds):
        captured.append(seconds)
        raise asyncio.CancelledError()

    monkeypatch.setattr(
        "app.services.node_runtime_service.asyncio.sleep", _fake_sleep
    )

    with pytest.raises(asyncio.CancelledError):
        await NodeRuntimeService._discovery_loop()

    assert captured == [5]


# ---------------------------------------------------------------------------
# US-404 — Divergent link tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("filename", ["divergence.json", "team/project/divergence.json"])
async def test_divergent_link_emits_link_divergent(
    monkeypatch, discovery_settings, stub_instance_id, filename
):
    """A link declared in ``links[]`` whose veth/TAP host-side name is NOT
    present on any bridge for the lab MUST trigger a ``link_divergent`` WS
    event whose payload includes ``link_id``, ``lab_id``, ``reason`` and an
    ISO-8601 ``last_checked`` timestamp."""
    lab_id = "lab-divergent-1"
    declared_link = {
        "id": "lnk_001",
        "from": {"node_id": 1, "interface_index": 0},
        "to": {"network_id": 1},
    }
    _write_lab(discovery_settings.LABS_DIR, lab_id, [declared_link], filename)

    bridge = host_net.bridge_name(lab_id, 1)
    # Kernel reports an empty bridge — declared link is divergent.
    monkeypatch.setattr(
        NodeRuntimeService,
        "_bridge_members_sync",
        staticmethod(lambda b: [] if b == bridge else []),
    )
    _patch_settings(monkeypatch, discovery_settings)
    hub = _patch_ws_hub(monkeypatch)

    await NodeRuntimeService._run_discovery_cycle()

    divergent = [e for e in hub.events if e[1] == "link_divergent"]
    assert len(divergent) == 1, hub.events
    assert divergent[0][0] == filename
    payload = divergent[0][2]
    assert payload["link_id"] == "lnk_001"
    assert payload["lab_id"] == lab_id
    assert isinstance(payload["reason"], str) and payload["reason"]
    assert "last_checked" in payload
    # ISO-8601 round-trip — bare smoke check.
    from datetime import datetime
    parsed = datetime.fromisoformat(payload["last_checked"])
    assert parsed.tzinfo is not None
    assert isinstance(payload["generation"], int) and payload["generation"] >= 1


@pytest.mark.asyncio
async def test_present_declared_link_is_not_divergent(
    monkeypatch, discovery_settings, stub_instance_id
):
    """When the declared link's veth IS present on the bridge, no
    ``link_divergent`` event is emitted."""
    lab_id = "lab-divergent-2"
    declared_link = {
        "id": "lnk_present",
        "from": {"node_id": 1, "interface_index": 0},
        "to": {"network_id": 1},
    }
    _write_lab(discovery_settings.LABS_DIR, lab_id, [declared_link])

    bridge = host_net.bridge_name(lab_id, 1)
    veth = host_net.veth_host_name(lab_id, 1, 0)
    monkeypatch.setattr(
        NodeRuntimeService,
        "_bridge_members_sync",
        staticmethod(lambda b: [veth] if b == bridge else []),
    )
    _patch_settings(monkeypatch, discovery_settings)
    hub = _patch_ws_hub(monkeypatch)

    await NodeRuntimeService._run_discovery_cycle()

    assert [e for e in hub.events if e[1] == "link_divergent"] == []


@pytest.mark.asyncio
async def test_present_declared_link_via_tap_is_not_divergent(
    monkeypatch, discovery_settings, stub_instance_id
):
    """A QEMU-style TAP whose name matches the declared link is also
    considered present (not divergent)."""
    lab_id = "lab-divergent-3"
    declared_link = {
        "id": "lnk_tap",
        "from": {"node_id": 2, "interface_index": 0},
        "to": {"network_id": 1},
    }
    _write_lab(discovery_settings.LABS_DIR, lab_id, [declared_link])

    bridge = host_net.bridge_name(lab_id, 1)
    tap = host_net.tap_name(lab_id, 2, 0)
    monkeypatch.setattr(
        NodeRuntimeService,
        "_bridge_members_sync",
        staticmethod(lambda b: [tap] if b == bridge else []),
    )
    _patch_settings(monkeypatch, discovery_settings)
    hub = _patch_ws_hub(monkeypatch)

    await NodeRuntimeService._run_discovery_cycle()

    assert [e for e in hub.events if e[1] == "link_divergent"] == []


@pytest.mark.asyncio
async def test_leased_divergent_link_is_skipped(
    monkeypatch, discovery_settings, stub_instance_id
):
    """A divergent link held by ``transition_lease`` (in-flight hot-plug
    from US-204b) MUST NOT emit a ``link_divergent`` event — in-flight
    hot-plug is not a divergence."""
    lab_id = "lab-divergent-4"
    declared_link = {
        "id": "lnk_leased",
        "from": {"node_id": 1, "interface_index": 0},
        "to": {"network_id": 1},
    }
    _write_lab(discovery_settings.LABS_DIR, lab_id, [declared_link])

    bridge = host_net.bridge_name(lab_id, 1)
    monkeypatch.setattr(
        NodeRuntimeService,
        "_bridge_members_sync",
        staticmethod(lambda b: [] if b == bridge else []),
    )
    _patch_settings(monkeypatch, discovery_settings)

    # Lease registered under the link_id — divergent flagging must back off.
    fake_lease = types.ModuleType("app.services.transition_lease")
    fake_lease.is_leased = lambda *, lab_id, link_id: link_id == "lnk_leased"  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "app.services.transition_lease", fake_lease)

    hub = _patch_ws_hub(monkeypatch)
    await NodeRuntimeService._run_discovery_cycle()

    assert [e for e in hub.events if e[1] == "link_divergent"] == []


@pytest.mark.asyncio
async def test_leased_divergent_link_via_iface_key_is_skipped(
    monkeypatch, discovery_settings, stub_instance_id
):
    """A lease registered under the iface name (rather than the link_id)
    must also suppress ``link_divergent`` — forward-compatible with the
    iface-keyed lease scheme used by ``discovered_link``."""
    lab_id = "lab-divergent-5"
    declared_link = {
        "id": "lnk_iface_leased",
        "from": {"node_id": 3, "interface_index": 0},
        "to": {"network_id": 1},
    }
    _write_lab(discovery_settings.LABS_DIR, lab_id, [declared_link])

    bridge = host_net.bridge_name(lab_id, 1)
    veth = host_net.veth_host_name(lab_id, 3, 0)
    monkeypatch.setattr(
        NodeRuntimeService,
        "_bridge_members_sync",
        staticmethod(lambda b: [] if b == bridge else []),
    )
    _patch_settings(monkeypatch, discovery_settings)

    fake_lease = types.ModuleType("app.services.transition_lease")
    fake_lease.is_leased = lambda *, lab_id, link_id: link_id == veth  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "app.services.transition_lease", fake_lease)

    hub = _patch_ws_hub(monkeypatch)
    await NodeRuntimeService._run_discovery_cycle()

    assert [e for e in hub.events if e[1] == "link_divergent"] == []


# ---------------------------------------------------------------------------
# US-403 MEDIUM-3 — producer-side generation token
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_discovery_generation_strictly_increasing(
    monkeypatch, discovery_settings, stub_instance_id
):
    """Two consecutive ``_discover_lab`` calls must emit events with strictly
    increasing ``generation`` values.

    This is the producer-side guarantee: the frontend can safely reject any
    ``discovered_link`` / ``link_divergent`` event whose ``generation`` is <=
    the ``generation`` recorded in the last ``lab_topology`` snapshot.
    """
    lab_id = "lab-gen-token-1"
    # No declared links so each cycle emits a discovered_link for the rogue iface.
    _write_lab(discovery_settings.LABS_DIR, lab_id, [])

    bridge = host_net.bridge_name(lab_id, 1)
    rogue_iface = host_net.veth_host_name(lab_id, 99, 0)

    monkeypatch.setattr(
        NodeRuntimeService,
        "_bridge_members_sync",
        staticmethod(lambda b: [rogue_iface] if b == bridge else []),
    )
    _patch_settings(monkeypatch, discovery_settings)
    hub = _patch_ws_hub(monkeypatch)

    # First cycle.
    await NodeRuntimeService._run_discovery_cycle()
    # Second cycle.
    await NodeRuntimeService._run_discovery_cycle()

    discovered = [e for e in hub.events if e[1] == "discovered_link"]
    assert len(discovered) == 2, hub.events

    gen_first = discovered[0][2]["generation"]
    gen_second = discovered[1][2]["generation"]
    assert isinstance(gen_first, int)
    assert isinstance(gen_second, int)
    assert gen_first < gen_second, (
        f"Expected strictly increasing generations; got {gen_first} then {gen_second}"
    )


# ---------------------------------------------------------------------------
# Divergence noise: stopped nodes, link direction, pnet, recovery
# ---------------------------------------------------------------------------


def _empty_bridges(monkeypatch) -> None:
    monkeypatch.setattr(NodeRuntimeService, "_bridge_members_sync", staticmethod(lambda b: []))


@pytest.mark.asyncio
async def test_stopped_node_links_are_not_divergent(
    monkeypatch, discovery_settings, stub_instance_id, running_nodes
):
    """A stopped node owns no TAP/veth, so its links are expected to be
    absent from the kernel and must not be drawn red."""
    lab_id = "lab-divergent-stopped"
    _write_lab(
        discovery_settings.LABS_DIR,
        lab_id,
        [
            {"id": "lnk_001", "from": {"node_id": 1, "interface_index": 0}, "to": {"network_id": 1}},
            {"id": "lnk_002", "from": {"node_id": 2, "interface_index": 0}, "to": {"network_id": 1}},
        ],
    )
    running_nodes["ids"] = {2}
    _empty_bridges(monkeypatch)
    _patch_settings(monkeypatch, discovery_settings)
    hub = _patch_ws_hub(monkeypatch)

    await NodeRuntimeService._run_discovery_cycle()

    divergent = [e[2]["link_id"] for e in hub.events if e[1] == "link_divergent"]
    assert divergent == ["lnk_002"]


@pytest.mark.asyncio
async def test_divergence_checks_node_end_of_network_to_node_link(
    monkeypatch, discovery_settings, stub_instance_id
):
    """A link drawn from the network to the node is checked by its node end."""
    lab_id = "lab-divergent-reversed"
    _write_lab(
        discovery_settings.LABS_DIR,
        lab_id,
        [{"id": "lnk_001", "from": {"network_id": 1}, "to": {"node_id": 1, "interface_index": 0}}],
    )
    _empty_bridges(monkeypatch)
    _patch_settings(monkeypatch, discovery_settings)
    hub = _patch_ws_hub(monkeypatch)

    await NodeRuntimeService._run_discovery_cycle()
    assert [e[2]["link_id"] for e in hub.events if e[1] == "link_divergent"] == ["lnk_001"]

    # Once the TAP is on the bridge it is consistent (and not "discovered").
    tap = host_net.tap_name(lab_id, 1, 0)
    monkeypatch.setattr(NodeRuntimeService, "_bridge_members_sync", staticmethod(lambda b: [tap]))
    hub.events.clear()
    await NodeRuntimeService._run_discovery_cycle()
    assert [e for e in hub.events if e[1] in ("link_divergent", "discovered_link")] == []


@pytest.mark.asyncio
async def test_pnet_links_are_not_divergent(
    monkeypatch, discovery_settings, stub_instance_id
):
    lab_id = "lab-divergent-pnet"
    filename = _write_lab(
        discovery_settings.LABS_DIR,
        lab_id,
        [{"id": "lnk_001", "from": {"node_id": 1, "interface_index": 0}, "to": {"network_id": 1}}],
    )
    lab_file = discovery_settings.LABS_DIR / filename
    data = json.loads(lab_file.read_text())
    data["networks"]["1"]["type"] = "pnet0"
    lab_file.write_text(json.dumps(data))
    _empty_bridges(monkeypatch)
    _patch_settings(monkeypatch, discovery_settings)
    hub = _patch_ws_hub(monkeypatch)

    await NodeRuntimeService._run_discovery_cycle()

    assert [e for e in hub.events if e[1] == "link_divergent"] == []


@pytest.mark.asyncio
async def test_recovered_link_publishes_divergence_cleared_once(
    monkeypatch, discovery_settings, stub_instance_id, running_nodes
):
    """A link flagged in one cycle and consistent (or stopped) in the next
    gets exactly one ``link_divergence_cleared`` so the canvas drops it."""
    lab_id = "lab-divergent-recover"
    filename = _write_lab(
        discovery_settings.LABS_DIR,
        lab_id,
        [
            {"id": "lnk_001", "from": {"node_id": 1, "interface_index": 0}, "to": {"network_id": 1}},
            {"id": "lnk_002", "from": {"node_id": 2, "interface_index": 0}, "to": {"network_id": 1}},
        ],
    )
    _empty_bridges(monkeypatch)
    _patch_settings(monkeypatch, discovery_settings)
    hub = _patch_ws_hub(monkeypatch)

    await NodeRuntimeService._run_discovery_cycle()
    assert sorted(e[2]["link_id"] for e in hub.events if e[1] == "link_divergent") == [
        "lnk_001",
        "lnk_002",
    ]

    # Cycle 2: node 1's TAP is attached; node 2 was stopped.
    veth = host_net.veth_host_name(lab_id, 1, 0)
    monkeypatch.setattr(NodeRuntimeService, "_bridge_members_sync", staticmethod(lambda b: [veth]))
    running_nodes["ids"] = {1}
    hub.events.clear()
    await NodeRuntimeService._run_discovery_cycle()

    cleared = [e for e in hub.events if e[1] == "link_divergence_cleared"]
    assert sorted(e[2]["link_id"] for e in cleared) == ["lnk_001", "lnk_002"]
    assert all(e[0] == filename and e[2]["lab_id"] == lab_id for e in cleared)
    assert all(isinstance(e[2]["generation"], int) for e in cleared)
    assert [e for e in hub.events if e[1] == "link_divergent"] == []

    # Cycle 3: nothing left to clear.
    hub.events.clear()
    await NodeRuntimeService._run_discovery_cycle()
    assert [e for e in hub.events if e[1] == "link_divergence_cleared"] == []


@pytest.mark.asyncio
async def test_leased_link_keeps_previous_divergent_verdict(
    monkeypatch, discovery_settings, stub_instance_id
):
    """A divergent link that enters a hot-plug lease is neither re-flagged
    nor cleared; it would otherwise flicker if the hot-plug then fails."""
    lab_id = "lab-divergent-lease-carry"
    _write_lab(
        discovery_settings.LABS_DIR,
        lab_id,
        [{"id": "lnk_001", "from": {"node_id": 1, "interface_index": 0}, "to": {"network_id": 1}}],
    )
    _empty_bridges(monkeypatch)
    _patch_settings(monkeypatch, discovery_settings)
    hub = _patch_ws_hub(monkeypatch)

    await NodeRuntimeService._run_discovery_cycle()
    assert [e[2]["link_id"] for e in hub.events if e[1] == "link_divergent"] == ["lnk_001"]

    fake_lease = types.ModuleType("app.services.transition_lease")
    fake_lease.is_leased = lambda *, lab_id, link_id: link_id == "lnk_001"  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "app.services.transition_lease", fake_lease)
    hub.events.clear()
    await NodeRuntimeService._run_discovery_cycle()

    assert [e for e in hub.events if e[1] in ("link_divergent", "link_divergence_cleared")] == []


@pytest.mark.asyncio
async def test_divergence_state_is_dropped_for_deleted_labs(
    monkeypatch, discovery_settings, stub_instance_id
):
    lab_id = "lab-divergent-deleted"
    filename = _write_lab(
        discovery_settings.LABS_DIR,
        lab_id,
        [{"id": "lnk_001", "from": {"node_id": 1, "interface_index": 0}, "to": {"network_id": 1}}],
    )
    _empty_bridges(monkeypatch)
    _patch_settings(monkeypatch, discovery_settings)
    _patch_ws_hub(monkeypatch)

    await NodeRuntimeService._run_discovery_cycle()
    assert NodeRuntimeService._divergent_links[lab_id] == {"lnk_001"}

    (discovery_settings.LABS_DIR / filename).unlink()
    await NodeRuntimeService._run_discovery_cycle()
    assert lab_id not in NodeRuntimeService._divergent_links
