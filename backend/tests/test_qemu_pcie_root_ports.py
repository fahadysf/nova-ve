# Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
# SPDX-License-Identifier: Apache-2.0
"""US-301 — Pre-allocate ``pcie-root-port`` slots at QEMU start.

These tests use an argv-capture fixture and never actually launch QEMU.
They verify the q35-only slot pre-allocation discriminator and the legacy
``pc`` compatibility path.
"""

from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services.node_runtime_service import NodeRuntimeService


@pytest.fixture(autouse=True)
def reset_runtime_registry():
    NodeRuntimeService.reset_registry()
    yield
    NodeRuntimeService.reset_registry()


@pytest.fixture()
def runtime_settings(tmp_path):
    labs_dir = tmp_path / "labs"
    images_dir = tmp_path / "images"
    tmp_dir = tmp_path / "tmp"
    templates_dir = tmp_path / "templates"
    labs_dir.mkdir()
    images_dir.mkdir()
    tmp_dir.mkdir()
    templates_dir.mkdir()
    return SimpleNamespace(
        LABS_DIR=labs_dir,
        IMAGES_DIR=images_dir,
        TMP_DIR=tmp_dir,
        TEMPLATES_DIR=templates_dir,
        QEMU_BINARY="qemu-system-x86_64",
        QEMU_IMG_BINARY="qemu-img",
        DOCKER_HOST="unix:///var/run/docker.sock",
    )


@pytest.fixture()
def patched_settings(monkeypatch, runtime_settings):
    monkeypatch.setattr("app.services.node_runtime_service.get_settings", lambda: runtime_settings)
    monkeypatch.setattr("app.services.template_service.get_settings", lambda: runtime_settings)
    return runtime_settings


def _write_template(settings, key: str, body: str) -> None:
    qemu_dir = settings.TEMPLATES_DIR / "qemu"
    qemu_dir.mkdir(parents=True, exist_ok=True)
    (qemu_dir / f"{key}.yml").write_text(body)


def _seed_image(settings, image_name: str = "router-image") -> None:
    image_dir = settings.IMAGES_DIR / "qemu" / image_name
    image_dir.mkdir(parents=True, exist_ok=True)
    (image_dir / "hda.qcow2").write_text("base-image")


class _FakeProcess:
    def __init__(self, pid: int = 4321):
        self.pid = pid

    def poll(self):
        return None


@pytest.fixture()
def argv_capture(monkeypatch):
    """Replace QEMU launch primitives so argv is captured but no real
    process is created. Returns a list that fills with the QEMU command."""
    captured: list[list[str]] = []

    def fake_run(cmd, capture_output=False, text=False, **_kwargs):
        # qemu-img create writes the overlay file; mimic that.
        if cmd and Path(cmd[0]).name == "qemu-img":
            Path(cmd[-1]).write_text("overlay-image")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    def fake_popen(cmd, cwd=None, stdin=None, stdout=None, stderr=None, start_new_session=None):
        captured.append(list(cmd))
        return _FakeProcess()

    monkeypatch.setattr("app.services.node_runtime_service.subprocess.run", fake_run)
    monkeypatch.setattr("app.services.node_runtime_service.subprocess.Popen", fake_popen)
    monkeypatch.setattr("app.services.node_runtime_service.time.sleep", lambda *_a, **_kw: None)
    monkeypatch.setattr(
        "app.services.node_runtime_service.psutil.Process",
        lambda pid: SimpleNamespace(
            create_time=lambda: 111.0,
            cpu_percent=lambda interval=0.0: 0,
            memory_info=lambda: SimpleNamespace(rss=0),
            wait=lambda timeout=5: None,
            is_running=lambda: True,
            status=lambda: "sleeping",
        ),
    )
    monkeypatch.setattr(
        "app.services.node_runtime_service.NodeRuntimeService._resolve_binary",
        staticmethod(lambda binary: binary),
    )
    return captured


def _build_lab(node: dict) -> dict:
    return {
        "schema": 2,
        "id": "lab-301",
        "meta": {"name": "us-301"},
        "viewport": {"x": 0, "y": 0, "zoom": 1.0},
        "nodes": {str(node["id"]): node},
        "networks": {},
        "links": [],
        "defaults": {"link_style": "orthogonal"},
    }


def _q35_template(max_nics: int = 8) -> str:
    return f"""type: qemu
name: q35-router
cpu: 1
ram: 1024
ethernet: 2
console_type: telnet
capabilities:
  hotplug: true
  max_nics: {max_nics}
  machine: q35
"""


def _pc_template() -> str:
    return """type: qemu
name: pc-legacy
cpu: 1
ram: 1024
ethernet: 2
console_type: telnet
capabilities:
  hotplug: false
  max_nics: 4
  machine: pc
"""


def _slot_args(cmd: list[str]) -> list[tuple[str, str]]:
    """Extract ``(rp_id, slot)`` pairs from -device pcie-root-port entries."""
    pairs: list[tuple[str, str]] = []
    for index, arg in enumerate(cmd):
        if arg == "-device" and index + 1 < len(cmd):
            value = cmd[index + 1]
            if value.startswith("pcie-root-port,"):
                fields = dict(part.split("=", 1) for part in value.split(",") if "=" in part)
                pairs.append((fields.get("id", ""), fields.get("slot", "")))
    return pairs


# ---------------------------------------------------------------------------
# q35: default max_nics=8 → 8 pre-allocated root ports
# ---------------------------------------------------------------------------


def test_q35_default_preallocates_eight_root_ports(patched_settings, argv_capture):
    _write_template(patched_settings, "q35router", _q35_template(max_nics=8))
    _seed_image(patched_settings)

    node = {
        "id": 1,
        "name": "r1",
        "type": "qemu",
        "template": "q35router",
        "image": "router-image",
        "console": "telnet",
        "cpu": 1,
        "ram": 1024,
        "ethernet": 2,
        "extras": {"qemu_nic": "virtio-net-pci"},
    }
    lab = _build_lab(node)
    runtime = NodeRuntimeService().start_node(lab, 1)

    cmd = argv_capture[0]
    assert "-machine" in cmd
    machine_arg = cmd[cmd.index("-machine") + 1]
    assert machine_arg.startswith("type=q35")

    slots = _slot_args(cmd)
    assert len(slots) == 8
    # Slot 0 reserved on q35 — first usable is 1; rp ids count from 0.
    assert slots == [(f"rp{i}", str(i + 1)) for i in range(8)]

    assert runtime["machine"] == "q35"
    assert runtime["max_nics"] == 8
    assert runtime["allocated_slots"] == list(range(8))
    assert runtime["hotplug_capable"] is True


# ---------------------------------------------------------------------------
# q35: custom max_nics=4
# ---------------------------------------------------------------------------


def test_q35_custom_max_nics_preallocates_correct_count(patched_settings, argv_capture):
    _write_template(patched_settings, "q35slim", _q35_template(max_nics=4))
    _seed_image(patched_settings)

    node = {
        "id": 1,
        "name": "r1",
        "type": "qemu",
        "template": "q35slim",
        "image": "router-image",
        "console": "telnet",
        "cpu": 1,
        "ram": 1024,
        "ethernet": 1,
        "extras": {"qemu_nic": "e1000"},
    }
    runtime = NodeRuntimeService().start_node(_build_lab(node), 1)

    cmd = argv_capture[0]
    slots = _slot_args(cmd)
    assert len(slots) == 4
    assert slots == [("rp0", "1"), ("rp1", "2"), ("rp2", "3"), ("rp3", "4")]
    assert runtime["max_nics"] == 4


# ---------------------------------------------------------------------------
# pc compat: no pre-allocation, hotplug_capable=False
# ---------------------------------------------------------------------------


def test_pc_machine_emits_no_root_ports(patched_settings, argv_capture):
    _write_template(patched_settings, "pcrouter", _pc_template())
    _seed_image(patched_settings)

    node = {
        "id": 1,
        "name": "r1",
        "type": "qemu",
        "template": "pcrouter",
        "image": "router-image",
        "console": "telnet",
        "cpu": 1,
        "ram": 1024,
        "ethernet": 1,
        "extras": {"qemu_nic": "e1000"},
    }
    runtime = NodeRuntimeService().start_node(_build_lab(node), 1)

    cmd = argv_capture[0]
    machine_arg = cmd[cmd.index("-machine") + 1]
    assert machine_arg.startswith("type=pc")
    assert _slot_args(cmd) == []
    assert runtime["machine"] == "pc"
    assert runtime["hotplug_capable"] is False
    assert runtime["allocated_slots"] == []


# ---------------------------------------------------------------------------
# machine_override wins over template
# ---------------------------------------------------------------------------


def test_machine_override_pc_overrides_q35_template(patched_settings, argv_capture):
    """A pre-Wave-7 node stamped with machine_override='pc' must NOT be
    silently switched to q35 even when the template defaults to q35."""
    _write_template(patched_settings, "q35router", _q35_template())
    _seed_image(patched_settings)

    node = {
        "id": 1,
        "name": "r1",
        "type": "qemu",
        "template": "q35router",
        "image": "router-image",
        "console": "telnet",
        "cpu": 1,
        "ram": 1024,
        "ethernet": 1,
        "extras": {"qemu_nic": "e1000"},
        "machine_override": "pc",
    }
    runtime = NodeRuntimeService().start_node(_build_lab(node), 1)

    cmd = argv_capture[0]
    machine_arg = cmd[cmd.index("-machine") + 1]
    assert machine_arg.startswith("type=pc")
    assert _slot_args(cmd) == []
    assert runtime["machine"] == "pc"


def test_machine_override_q35_overrides_pc_template(patched_settings, argv_capture):
    _write_template(patched_settings, "pcrouter", _pc_template())
    _seed_image(patched_settings)

    node = {
        "id": 1,
        "name": "r1",
        "type": "qemu",
        "template": "pcrouter",
        "image": "router-image",
        "console": "telnet",
        "cpu": 1,
        "ram": 1024,
        "ethernet": 1,
        "extras": {"qemu_nic": "virtio-net-pci"},
        "machine_override": "q35",
    }
    runtime = NodeRuntimeService().start_node(_build_lab(node), 1)

    cmd = argv_capture[0]
    machine_arg = cmd[cmd.index("-machine") + 1]
    assert machine_arg.startswith("type=q35")
    # max_nics still comes from the pc template (4)
    assert runtime["max_nics"] == 4
    assert _slot_args(cmd) == [
        ("rp0", "1"),
        ("rp1", "2"),
        ("rp2", "3"),
        ("rp3", "4"),
    ]


# ---------------------------------------------------------------------------
# Schema rejects invalid machine_override
# ---------------------------------------------------------------------------


def test_invalid_machine_override_rejected_by_schema():
    from app.schemas.node import NodeBase

    with pytest.raises(Exception):  # pydantic.ValidationError
        NodeBase(
            id=1,
            name="bad",
            type="qemu",
            template="q35router",
            machine_override="i440fx",
        )


def test_machine_override_accepts_valid_values():
    from app.schemas.node import NodeBase

    n_pc = NodeBase(id=1, name="a", type="qemu", machine_override="pc")
    n_q35 = NodeBase(id=2, name="b", type="qemu", machine_override="q35")
    n_none = NodeBase(id=3, name="c", type="qemu", machine_override=None)
    assert n_pc.machine_override == "pc"
    assert n_q35.machine_override == "q35"
    assert n_none.machine_override is None


# ---------------------------------------------------------------------------
# NIC model honored from extras.qemu_nic (not hardcoded)
# ---------------------------------------------------------------------------


def test_nic_model_honored_from_qemu_nic_extra(patched_settings, argv_capture):
    _write_template(patched_settings, "q35router", _q35_template(max_nics=2))
    _seed_image(patched_settings)

    node = {
        "id": 1,
        "name": "r1",
        "type": "qemu",
        "template": "q35router",
        "image": "router-image",
        "console": "telnet",
        "cpu": 1,
        "ram": 1024,
        "ethernet": 2,
        "extras": {"qemu_nic": "virtio-net-pci"},
    }
    NodeRuntimeService().start_node(_build_lab(node), 1)

    cmd = argv_capture[0]
    nic_devices: list[str] = []
    for index, arg in enumerate(cmd):
        if arg == "-device" and index + 1 < len(cmd):
            value = cmd[index + 1]
            if value.startswith(("virtio-net-pci", "e1000", "rtl8139", "vmxnet3", "pcnet")):
                nic_devices.append(value)
    assert len(nic_devices) == 2
    assert all(value.startswith("virtio-net-pci,") for value in nic_devices)
    # On q35, NICs land on the pre-allocated bus rp{i}
    assert "bus=rp0" in nic_devices[0]
    assert "bus=rp1" in nic_devices[1]


def test_nic_model_default_e1000_when_extra_missing(patched_settings, argv_capture):
    _write_template(patched_settings, "q35router", _q35_template(max_nics=1))
    _seed_image(patched_settings)

    node = {
        "id": 1,
        "name": "r1",
        "type": "qemu",
        "template": "q35router",
        "image": "router-image",
        "console": "telnet",
        "cpu": 1,
        "ram": 1024,
        "ethernet": 1,
        # No extras.qemu_nic — falls back to e1000.
    }
    NodeRuntimeService().start_node(_build_lab(node), 1)

    cmd = argv_capture[0]
    e1000_devices = [
        cmd[i + 1]
        for i, arg in enumerate(cmd)
        if arg == "-device" and i + 1 < len(cmd) and cmd[i + 1].startswith("e1000,")
    ]
    assert len(e1000_devices) == 1


# ---------------------------------------------------------------------------
# Slot ID format: id=rpN, chassis=N+1, slot=N+1
# ---------------------------------------------------------------------------


def test_slot_id_format(patched_settings, argv_capture):
    _write_template(patched_settings, "q35router", _q35_template(max_nics=3))
    _seed_image(patched_settings)

    node = {
        "id": 1,
        "name": "r1",
        "type": "qemu",
        "template": "q35router",
        "image": "router-image",
        "console": "telnet",
        "cpu": 1,
        "ram": 1024,
        "ethernet": 0,
        "extras": {"qemu_nic": "e1000"},
    }
    NodeRuntimeService().start_node(_build_lab(node), 1)

    cmd = argv_capture[0]
    rp_devices: list[str] = []
    for index, arg in enumerate(cmd):
        if arg == "-device" and index + 1 < len(cmd) and cmd[index + 1].startswith("pcie-root-port,"):
            rp_devices.append(cmd[index + 1])
    assert rp_devices == [
        "pcie-root-port,id=rp0,chassis=1,slot=1",
        "pcie-root-port,id=rp1,chassis=2,slot=2",
        "pcie-root-port,id=rp2,chassis=3,slot=3",
    ]
    # Slot 0 must never be allocated.
    for value in rp_devices:
        assert "slot=0" not in value


# ---------------------------------------------------------------------------
# Legacy node with no template field falls back to pc
# ---------------------------------------------------------------------------


def test_node_without_template_falls_back_to_pc(patched_settings, argv_capture):
    _seed_image(patched_settings)

    node = {
        "id": 1,
        "name": "legacy",
        "type": "qemu",
        "image": "router-image",
        "console": "telnet",
        "cpu": 1,
        "ram": 1024,
        "ethernet": 1,
        # No template, no machine_override.
    }
    runtime = NodeRuntimeService().start_node(_build_lab(node), 1)

    cmd = argv_capture[0]
    machine_arg = cmd[cmd.index("-machine") + 1]
    assert machine_arg.startswith("type=pc")
    assert _slot_args(cmd) == []
    assert runtime["machine"] == "pc"
