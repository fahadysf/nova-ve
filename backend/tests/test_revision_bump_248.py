# Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
# SPDX-License-Identifier: Apache-2.0

"""#248 — revision hygiene for the #245 optimistic-concurrency guard.

Spurious 412s ("Lab changed in another session") occurred in single-session
use because (a) server-internal bookkeeping writes bumped meta.revision, and
(b) user-facing write endpoints bumped it server-side without reporting the
new value. These tests pin both halves of the fix:

- ``write_lab_json_static(bump_revision=False)`` preserves the on-disk
  revision for bookkeeping writes (heartbeat status sync, bridge/DHCP
  reconcile, rollback restores).
- Mutating endpoints (links, networks, nodes) return the post-write
  ``revision`` so the editor's next If-Match stays fresh.
- ``POST /runtime/reconcile`` does not bump the revision.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

from app.dependencies import get_current_user
from app.main import app
from app.services import host_net
from app.services.lab_service import LabService
from app.services.network_service import NetworkService


# ---------------------------------------------------------------------------
# Fixtures (mirrors test_links_api.py — per-file in this suite)
# ---------------------------------------------------------------------------


@pytest.fixture()
def route_settings(tmp_path):
    labs_dir = tmp_path / "labs"
    images_dir = tmp_path / "images"
    tmp_dir = tmp_path / "tmp"
    templates_dir = tmp_path / "templates"
    for d in (labs_dir, images_dir, tmp_dir, templates_dir):
        d.mkdir()
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
        GUACAMOLE_JSON_SECRET_KEY="x" * 32,
        GUACAMOLE_PUBLIC_PATH="/html5/",
        GUACAMOLE_TARGET_HOST="host.docker.internal",
        GUACAMOLE_JSON_EXPIRE_SECONDS=300,
        GUACAMOLE_TERMINAL_FONT_NAME="Roboto Mono",
        GUACAMOLE_TERMINAL_FONT_SIZE=10,
    )


@pytest.fixture()
def patched_route_settings(monkeypatch, route_settings):
    monkeypatch.setattr("app.services.lab_service.get_settings", lambda: route_settings)
    monkeypatch.setattr("app.services.template_service.get_settings", lambda: route_settings)
    monkeypatch.setattr("app.services.node_runtime_service.get_settings", lambda: route_settings)
    monkeypatch.setattr("app.services.html5_service.get_settings", lambda: route_settings)
    monkeypatch.setattr("app.services.guacamole_db_service.get_settings", lambda: route_settings)
    monkeypatch.setattr("app.services.link_service.get_settings", lambda: route_settings)
    monkeypatch.setattr("app.services.network_service.get_settings", lambda: route_settings)
    monkeypatch.setattr("app.services.cloud_inventory_service.get_settings", lambda: route_settings)
    monkeypatch.setattr("app.routers.labs.get_settings", lambda: route_settings)
    return route_settings


@pytest.fixture()
def auth_override():
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
        username="admin", role="admin", html5=True, folder="/",
    )
    yield
    app.dependency_overrides.clear()


@pytest.fixture()
def reset_idempotency():
    from app.services.link_service import link_service

    link_service._idempotency.clear()
    yield
    link_service._idempotency.clear()


@pytest.fixture()
def ws_recorder(monkeypatch):
    recorded: list[tuple[str, str, dict]] = []

    async def fake_publish(lab_id, event_type, payload, rev=""):
        recorded.append((lab_id, event_type, payload))
        return SimpleNamespace(seq=len(recorded), type=event_type, rev=rev, payload=payload)

    monkeypatch.setattr("app.services.ws_hub.ws_hub.publish", fake_publish)
    return recorded


@pytest.fixture()
def instance_id(monkeypatch, tmp_path) -> str:
    """Seed a deterministic instance_id for bridge_name() derivation."""
    instance_dir = tmp_path / "nova-ve-instance"
    instance_dir.mkdir()
    value = "test-instance-uuid-248"
    (instance_dir / "instance_id").write_text(value)
    monkeypatch.setenv("NOVA_VE_INSTANCE_DIR", str(instance_dir))
    return value


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------


def _seed_lab(lab_dir, name: str = "rev-lab.json", *, revision=4) -> str:
    meta = {"name": name}
    if revision is not None:
        meta["revision"] = revision
    payload = {
        "schema": 2,
        "id": name.replace(".json", ""),
        "meta": meta,
        "viewport": {"x": 0, "y": 0, "zoom": 1.0},
        "nodes": {
            "1": {
                "id": 1,
                "name": "n1",
                "type": "docker",
                "template": "docker",
                "image": "nova-ve/alpine-telnet:latest",
                "console": "telnet",
                "status": 0,
                "cpu": 1,
                "ram": 256,
                "ethernet": 2,
                "left": 100,
                "top": 100,
                "icon": "Server.png",
                "interfaces": [
                    {"index": 0, "name": "eth0", "planned_mac": None, "port_position": None},
                    {"index": 1, "name": "eth1", "planned_mac": None, "port_position": None},
                ],
            }
        },
        "networks": {
            "1": {
                "id": 1,
                "name": "lan",
                "type": "linux_bridge",
                "left": 200,
                "top": 200,
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
        },
        "links": [],
        "defaults": {"link_style": "orthogonal"},
    }
    path = lab_dir / name
    path.write_text(json.dumps(payload))
    return name


def _client():
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://testserver")


def _on_disk_revision(lab_path) -> int:
    return json.loads(lab_path.read_text())["meta"]["revision"]


# ---------------------------------------------------------------------------
# write_lab_json_static bump flag
# ---------------------------------------------------------------------------


def test_write_lab_bump_revision_default_and_false(patched_route_settings):
    """Default write bumps; bookkeeping write (bump_revision=False) preserves."""
    labs = patched_route_settings.LABS_DIR
    name = _seed_lab(labs, "flag-lab.json", revision=4)

    data = LabService.read_lab_json_static(name)
    LabService.write_lab_json_static(name, data)
    assert _on_disk_revision(labs / name) == 5, "default write must bump"

    data = LabService.read_lab_json_static(name)
    LabService.write_lab_json_static(name, data, bump_revision=False)
    assert _on_disk_revision(labs / name) == 5, "bookkeeping write must preserve revision"

    # Absent meta stays absent under bump_revision=False (no synthetic bump).
    name2 = _seed_lab(labs, "flag-lab-2.json", revision=None)
    data = LabService.read_lab_json_static(name2)
    LabService.write_lab_json_static(name2, data, bump_revision=False)
    assert "revision" not in (labs / name2).read_text().replace(" ", ""), (
        "bookkeeping write on a revision-less lab must not introduce one"
    )


# ---------------------------------------------------------------------------
# User-facing endpoints report the post-write revision
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_link_create_delete_patch_return_revision(
    monkeypatch, patched_route_settings, auth_override, reset_idempotency, ws_recorder, instance_id
):
    """POST/DELETE/PATCH /links responses carry the post-write revision."""
    labs = patched_route_settings.LABS_DIR
    _seed_lab(labs, "rev-links.json", revision=10)

    monkeypatch.setattr(
        "app.services.link_service.link_service._hot_attach_running_endpoints",
        lambda *args, **kwargs: {},
    )

    async with _client() as client:
        created = await client.post(
            "/api/labs/rev-links.json/links",
            json={"from": {"node_id": 1, "interface_index": 0}, "to": {"network_id": 1}},
        )
        assert created.status_code == 201, created.text
        body = created.json()
        assert body["revision"] == 11, "create must report post-write revision"
        assert _on_disk_revision(labs / "rev-links.json") == 11
        link_id = body["link"]["id"]

        patched = await client.patch(
            f"/api/labs/rev-links.json/links/{link_id}",
            json={"label": "renamed"},
        )
        assert patched.status_code == 200, patched.text
        assert patched.json()["revision"] == 12
        assert _on_disk_revision(labs / "rev-links.json") == 12

        deleted = await client.delete(f"/api/labs/rev-links.json/links/{link_id}")
        assert deleted.status_code == 200, deleted.text
        assert deleted.json()["revision"] == 13
        assert _on_disk_revision(labs / "rev-links.json") == 13


@pytest.mark.asyncio
async def test_network_create_delete_return_revision(
    monkeypatch, patched_route_settings, auth_override, ws_recorder, instance_id
):
    """POST/DELETE /networks responses carry the post-write revision."""
    labs = patched_route_settings.LABS_DIR
    _seed_lab(labs, "rev-nets.json", revision=20)

    # Fake the host bridge surface so provisioning runs without privileges.
    monkeypatch.setattr(host_net, "bridge_exists", lambda name: False)
    monkeypatch.setattr(host_net, "bridge_add", lambda name: None)
    monkeypatch.setattr(host_net, "bridge_del", lambda name: None)
    monkeypatch.setattr(host_net, "link_up", lambda iface: None)

    async with _client() as client:
        created = await client.post(
            "/api/labs/rev-nets.json/networks",
            json={"name": "net-x", "type": "linux_bridge", "left": 10, "top": 10},
        )
        assert created.status_code == 201, created.text
        assert created.json()["revision"] == 21
        assert _on_disk_revision(labs / "rev-nets.json") == 21
        net_id = created.json()["network"]["id"]

        deleted = await client.delete(f"/api/labs/rev-nets.json/networks/{net_id}")
        assert deleted.status_code == 200, deleted.text
        assert deleted.json()["revision"] == 22
        assert _on_disk_revision(labs / "rev-nets.json") == 22


@pytest.mark.asyncio
async def test_node_create_delete_return_revision(
    patched_route_settings, auth_override, ws_recorder
):
    """POST /nodes/batch + DELETE /nodes/{id} responses carry the revision."""
    labs = patched_route_settings.LABS_DIR
    _seed_lab(labs, "rev-nodes.json", revision=30)

    # Seed a qemu template + matching image dir the batch create validates
    # against (docker images come from the daemon catalog, unavailable here).
    template_yml = patched_route_settings.TEMPLATES_DIR / "qemu" / "csr.yml"
    template_yml.parent.mkdir(parents=True, exist_ok=True)
    template_yml.write_text(
        "type: qemu\nname: CSR1000v\ncpu: 2\nram: 4096\nethernet: 4\n"
        "console_type: telnet\nicon_type: router\n"
    )
    image_dir = patched_route_settings.IMAGES_DIR / "qemu" / "csr1000v"
    image_dir.mkdir(parents=True, exist_ok=True)
    (image_dir / "hda.qcow2").write_text("image")

    async with _client() as client:
        created = await client.post(
            "/api/labs/rev-nodes.json/nodes/batch",
            json={
                "name_prefix": "extra",
                "template": "csr",
                "type": "qemu",
                "image": "csr1000v",
                "console": "telnet",
                "ethernet": 1,
                "ram": 256,
                "cpu": 1,
                "count": 1,
            },
        )
        assert created.status_code == 200, created.text
        assert created.json()["revision"] == 31
        assert _on_disk_revision(labs / "rev-nodes.json") == 31
        new_id = created.json()["data"]["nodes"][0]["id"]

        deleted = await client.delete(f"/api/labs/rev-nodes.json/nodes/{new_id}")
        assert deleted.status_code == 200, deleted.text
        assert deleted.json()["revision"] == 32
        assert _on_disk_revision(labs / "rev-nodes.json") == 32


# ---------------------------------------------------------------------------
# Internal reconcile writes must not bump
# ---------------------------------------------------------------------------


@pytest.fixture()
def svc_labs_dir(tmp_path):
    d = tmp_path / "svc-labs"
    d.mkdir()
    return d


@pytest.fixture()
def svc_settings(monkeypatch, svc_labs_dir):
    s = SimpleNamespace(LABS_DIR=svc_labs_dir)
    monkeypatch.setattr("app.services.network_service.get_settings", lambda: s)
    monkeypatch.setattr("app.services.lab_service.get_settings", lambda: s)
    monkeypatch.setattr("app.services.link_service.get_settings", lambda: s)
    return s


@pytest.fixture()
def helper_mocks(monkeypatch):
    """Capture host_net calls; run bridge work without privileges."""
    monkeypatch.setattr(host_net, "bridge_exists", lambda name: False)
    monkeypatch.setattr(host_net, "bridge_add", lambda name: None)
    monkeypatch.setattr(host_net, "bridge_del", lambda name: None)
    monkeypatch.setattr(host_net, "bridge_addr_add", lambda name, cidr: None)
    monkeypatch.setattr(host_net, "link_up", lambda iface: None)
    return


def test_ensure_lab_bridges_does_not_bump_revision(instance_id, svc_settings, helper_mocks):
    """#248: lab-open reconcile is bookkeeping — revision must survive it.

    Before the fix every bridge creation bumped meta.revision, so the
    fire-and-forget reconcile on every lab load invalidated the editor's
    If-Match token and the next drag got a spurious 412.
    """
    lab_id = "rev-reconcile"
    lab_name = f"{lab_id}.json"
    labs_dir = svc_settings.LABS_DIR
    (labs_dir / lab_name).write_text(
        json.dumps(
            {
                "schema": 2,
                "id": lab_id,
                "meta": {"name": lab_name, "revision": 5},
                "viewport": {"x": 0, "y": 0, "zoom": 1.0},
                "nodes": {},
                "networks": {"1": {"id": 1, "name": "lan", "type": "linux_bridge"}},
                "links": [],
                "defaults": {"link_style": "orthogonal"},
            }
        )
    )

    NetworkService().ensure_lab_bridges(lab_name)

    saved = json.loads((labs_dir / lab_name).read_text())
    assert saved["networks"]["1"]["runtime"]["bridge_name"], "reconcile must stamp runtime"
    assert saved["meta"]["revision"] == 5, "reconcile must not bump the revision"
