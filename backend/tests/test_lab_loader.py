"""Tests for the v2 lab.json loader and template interface_naming validation.

Covers US-058 acceptance criteria:
  - test_v1_rejected_with_422
  - test_v2_round_trip_identity
  - test_interface_naming_format_or_explicit
"""

import copy
import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

from app.dependencies import get_current_user
from app.main import app
from app.services.lab_service import LEGACY_SCHEMA_ERROR, LabService
from app.services.template_service import TemplateError, TemplateService


FIXTURES_DIR = Path(__file__).parent / "fixtures"
LEGACY_FIXTURE = FIXTURES_DIR / "lab_v1_legacy.json"


@pytest.fixture()
def loader_settings(tmp_path):
    labs_dir = tmp_path / "labs"
    images_dir = tmp_path / "images"
    tmp_dir = tmp_path / "tmp"
    templates_dir = tmp_path / "templates"
    for directory in (labs_dir, images_dir, tmp_dir, templates_dir):
        directory.mkdir()
    return SimpleNamespace(
        LABS_DIR=labs_dir,
        IMAGES_DIR=images_dir,
        TMP_DIR=tmp_dir,
        TEMPLATES_DIR=templates_dir,
        QEMU_BINARY="qemu-system-x86_64",
        QEMU_IMG_BINARY="qemu-img",
        DOCKER_HOST="unix:///var/run/docker.sock",
        GUACAMOLE_DATABASE_URL="",
        GUACAMOLE_DATA_SOURCE="postgresql",
        GUACAMOLE_INTERNAL_URL="http://127.0.0.1:8081/html5/",
        GUACAMOLE_JSON_SECRET_KEY="4c0b569e4c96df157eee1b65dd0e4d41",
        GUACAMOLE_PUBLIC_PATH="/html5/",
        GUACAMOLE_TARGET_HOST="host.docker.internal",
        GUACAMOLE_JSON_EXPIRE_SECONDS=300,
        GUACAMOLE_TERMINAL_FONT_NAME="Roboto Mono",
        GUACAMOLE_TERMINAL_FONT_SIZE=10,
        SESSION_MAX_AGE=14400,
    )


@pytest.fixture()
def patched_loader_settings(monkeypatch, loader_settings):
    monkeypatch.setattr("app.services.lab_service.get_settings", lambda: loader_settings)
    monkeypatch.setattr("app.services.template_service.get_settings", lambda: loader_settings)
    monkeypatch.setattr("app.services.node_runtime_service.get_settings", lambda: loader_settings)
    monkeypatch.setattr("app.services.html5_service.get_settings", lambda: loader_settings)
    monkeypatch.setattr("app.services.guacamole_db_service.get_settings", lambda: loader_settings)
    return loader_settings


def _admin():
    return SimpleNamespace(username="admin", role="admin", html5=True, folder="/")


def _v2_lab(lab_id: str = "lab-v2-rt", *, nodes_count: int = 5, networks_count: int = 2) -> dict:
    """Build a fully-populated v2 lab.json with the requested counts."""

    nodes = {}
    for index in range(1, nodes_count + 1):
        nodes[str(index)] = {
            "id": index,
            "name": f"R{index}",
            "type": "qemu",
            "template": "vyos",
            "image": "vyos-1.4",
            "console": "telnet",
            "status": 0,
            "delay": 0,
            "cpu": 1,
            "ram": 1024,
            "ethernet": 4,
            "cpulimit": 1,
            "uuid": f"00000000-0000-0000-0000-{index:012d}",
            "firstmac": f"50:00:00:{index:02x}:00:00",
            "left": 100 + (index - 1) * 120,
            "top": 100,
            "icon": "Router.png",
            "width": "0",
            "config": False,
            "config_list": [],
            "sat": 0,
            "computed_sat": 0,
            "interfaces": [
                {
                    "index": 0,
                    "name": "Gi0",
                    "planned_mac": f"50:00:00:{index:02x}:00:00",
                    "port_position": {"side": "right", "offset": 0.4},
                },
                {
                    "index": 1,
                    "name": "Gi1",
                    "planned_mac": None,
                    "port_position": None,
                },
            ],
            "extras": {},
        }

    networks = {}
    for index in range(1, networks_count + 1):
        networks[str(index)] = {
            "id": index,
            "name": f"net-{index}",
            "type": "linux_bridge",
            "left": 200 + (index - 1) * 200,
            "top": 300,
            "icon": "01-Cloud-Default.svg",
            "width": 0,
            "style": "Solid",
            "linkstyle": "Straight",
            "color": "",
            "label": "",
            "visibility": True,
            "implicit": False,
            "smart": -1,
            "config": {},
        }

    links = [
        {
            "id": "lnk_001",
            "from": {"node_id": 1, "interface_index": 0},
            "to": {"network_id": 1},
            "style_override": None,
            "label": "",
            "color": "",
            "width": "1",
            "metrics": {"delay_ms": 0, "loss_pct": 0, "bandwidth_kbps": 0, "jitter_ms": 0},
        },
        {
            "id": "lnk_002",
            "from": {"node_id": 2, "interface_index": 0},
            "to": {"network_id": 1},
            "style_override": None,
            "label": "",
            "color": "",
            "width": "1",
            "metrics": {"delay_ms": 0, "loss_pct": 0, "bandwidth_kbps": 0, "jitter_ms": 0},
        },
        {
            "id": "lnk_003",
            "from": {"node_id": 3, "interface_index": 0},
            "to": {"network_id": 2},
            "style_override": "orthogonal",
            "label": "uplink",
            "color": "#0a0",
            "width": "2",
            "metrics": {"delay_ms": 5, "loss_pct": 1, "bandwidth_kbps": 1000000, "jitter_ms": 2},
        },
        {
            "id": "lnk_004",
            "from": {"node_id": 4, "interface_index": 0},
            "to": {"node_id": 5, "interface_index": 1},
            "style_override": None,
            "label": "p2p",
            "color": "",
            "width": "1",
            "metrics": {"delay_ms": 0, "loss_pct": 0, "bandwidth_kbps": 0, "jitter_ms": 0},
        },
    ]

    return {
        "schema": 2,
        "id": lab_id,
        "meta": {
            "name": "Round Trip",
            "author": "tests",
            "description": "v2 round trip fixture",
            "version": "0",
            "scripttimeout": 300,
            "countdown": 0,
            "linkwidth": "1",
            "grid": True,
            "lock": False,
        },
        "viewport": {"x": 0, "y": 0, "zoom": 1.0},
        "nodes": nodes,
        "networks": networks,
        "links": links,
        "defaults": {"link_style": "orthogonal"},
        "textobjects": [],
        "lineobjects": [],
        "pictures": [],
        "tasks": [],
        "configsets": {},
    }


@pytest.mark.asyncio
async def test_v1_rejected_with_422(patched_loader_settings):
    """Loading a v1 lab.json via the HTTP API must return 422 with the lab path."""

    legacy_dest = patched_loader_settings.LABS_DIR / "legacy-lab.json"
    shutil.copy(LEGACY_FIXTURE, legacy_dest)

    app.dependency_overrides[get_current_user] = _admin
    transport = ASGITransport(app=app)
    try:
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            response = await client.get("/api/labs/legacy-lab.json/topology")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200  # FastAPI returns 200 with our envelope
    payload = response.json()
    assert payload["code"] == 422
    assert payload["status"] == "fail"
    assert "run scripts/migrate_lab_v1_to_v2.py" in payload["message"]
    assert payload["message"] == LEGACY_SCHEMA_ERROR
    # The lab path is included in the response.
    assert "lab_path" in payload
    assert "legacy-lab.json" in payload["lab_path"]


def test_v2_round_trip_identity(patched_loader_settings):
    """A v2 lab.json with 5 nodes / 4 links / 2 networks survives write→read identity."""

    lab_data = _v2_lab()
    expected_persisted = copy.deepcopy(lab_data)

    LabService.write_lab_json_static("round-trip.json", copy.deepcopy(lab_data))

    # The file on disk must NOT contain any synthesised legacy fields.
    raw_on_disk = json.loads(
        (patched_loader_settings.LABS_DIR / "round-trip.json").read_text()
    )
    assert "topology" not in raw_on_disk
    assert raw_on_disk["schema"] == 2
    for node in raw_on_disk["nodes"].values():
        for interface in node["interfaces"]:
            assert "network_id" not in interface
    for network in raw_on_disk["networks"].values():
        assert "count" not in network
    assert raw_on_disk == expected_persisted

    # Reading it back through the loader inflates the legacy compat fields,
    # but stripping them must yield the original v2 structure exactly.
    read_back = LabService.read_lab_json_static("round-trip.json")

    assert read_back["schema"] == 2
    assert "topology" in read_back  # legacy compat shim
    for node in read_back["nodes"].values():
        for interface in node["interfaces"]:
            assert "network_id" in interface  # legacy compat shim
    for network in read_back["networks"].values():
        assert "count" in network  # derived from links

    # Strip legacy compat fields and compare to the original.
    sanitized = {k: v for k, v in read_back.items() if k != "topology"}
    sanitized["nodes"] = {
        node_id: {
            **{k: v for k, v in node.items() if k != "interfaces"},
            "interfaces": [
                {k: v for k, v in iface.items() if k != "network_id"}
                for iface in node["interfaces"]
            ],
        }
        for node_id, node in sanitized["nodes"].items()
    }
    sanitized["networks"] = {
        network_id: {k: v for k, v in network.items() if k != "count"}
        for network_id, network in sanitized["networks"].items()
    }

    assert sanitized == expected_persisted


def _write_template(templates_dir: Path, key: str, body: str) -> Path:
    qemu_dir = templates_dir / "qemu"
    qemu_dir.mkdir(parents=True, exist_ok=True)
    path = qemu_dir / f"{key}.yml"
    path.write_text(body)
    return path


_BASE_TEMPLATE_BODY = """type: qemu
name: VYOS
cpu: 1
ram: 1024
ethernet: 4
console_type: telnet
icon_type: router
cpulimit: 1
"""


def test_interface_naming_format_or_explicit(patched_loader_settings):
    """Validate the template loader's interface_naming acceptance/rejection rules."""

    templates_dir = patched_loader_settings.TEMPLATES_DIR

    # --- format-only is OK ---
    _write_template(
        templates_dir,
        "fmt-only",
        _BASE_TEMPLATE_BODY + "interface_naming:\n  format: \"Gi0/{n}\"\n",
    )
    naming = TemplateService().interface_naming("qemu", "fmt-only")
    assert naming == {"format": "Gi0/{n}"}

    # --- explicit-only is OK ---
    (templates_dir / "qemu" / "fmt-only.yml").unlink()
    _write_template(
        templates_dir,
        "explicit-only",
        _BASE_TEMPLATE_BODY + "interface_naming:\n  explicit:\n    - mgmt0\n    - Gi1\n",
    )
    naming = TemplateService().interface_naming("qemu", "explicit-only")
    assert naming == {"explicit": ["mgmt0", "Gi1"]}

    # --- both present is rejected ---
    (templates_dir / "qemu" / "explicit-only.yml").unlink()
    _write_template(
        templates_dir,
        "both",
        _BASE_TEMPLATE_BODY
        + "interface_naming:\n  format: \"Gi0/{n}\"\n  explicit:\n    - mgmt0\n",
    )
    with pytest.raises(TemplateError) as exc_info:
        TemplateService().get_template("qemu", "both")
    assert "exactly one" in str(exc_info.value)

    # --- neither present is rejected ---
    (templates_dir / "qemu" / "both.yml").unlink()
    _write_template(
        templates_dir,
        "neither",
        _BASE_TEMPLATE_BODY + "interface_naming: {}\n",
    )
    with pytest.raises(TemplateError) as exc_info:
        TemplateService().get_template("qemu", "neither")
    assert "either" in str(exc_info.value)

    # --- format lacking placeholder is rejected ---
    (templates_dir / "qemu" / "neither.yml").unlink()
    _write_template(
        templates_dir,
        "fmt-no-placeholder",
        _BASE_TEMPLATE_BODY + "interface_naming:\n  format: \"GigabitEth\"\n",
    )
    with pytest.raises(TemplateError) as exc_info:
        TemplateService().get_template("qemu", "fmt-no-placeholder")
    assert "{n}" in str(exc_info.value) or "{slot}" in str(exc_info.value)

    # --- explicit empty list is rejected ---
    (templates_dir / "qemu" / "fmt-no-placeholder.yml").unlink()
    _write_template(
        templates_dir,
        "explicit-empty",
        _BASE_TEMPLATE_BODY + "interface_naming:\n  explicit: []\n",
    )
    with pytest.raises(TemplateError) as exc_info:
        TemplateService().get_template("qemu", "explicit-empty")
    assert "non-empty" in str(exc_info.value)
