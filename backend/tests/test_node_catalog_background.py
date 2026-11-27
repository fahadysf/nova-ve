import asyncio
import subprocess
import threading
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.routers import labs
from app.dependencies import get_current_user
from app.services.docker_image_service import DockerImageError
from app.services.node_catalog_service import NodeCatalogService
from app.services.template_service import TemplateService
from app.services.ws_hub import WsHub


def _template(key: str) -> SimpleNamespace:
    return SimpleNamespace(
        key=key,
        type="docker",
        name=key,
        description="",
        icon_type="server",
        icon="Server.png",
        cpu=1,
        ram=1024,
        ethernet=1,
        console_type="telnet",
        second_console=None,
        cpulimit=1,
        extras={},
        raw={},
        capabilities={},
        paired_parent=None,
    )


def test_catalog_build_loads_templates_and_docker_images_once(tmp_path, monkeypatch):
    service = TemplateService.__new__(TemplateService)
    service.images_dir = tmp_path
    templates = [_template("one"), _template("two")]
    loads = 0
    docker_loads = 0

    def load_templates():
        nonlocal loads
        loads += 1
        return templates

    def load_docker_images(*, strict=False):
        nonlocal docker_loads
        docker_loads += 1
        assert strict is False
        return {}

    monkeypatch.setattr(service, "_load_templates", load_templates)
    monkeypatch.setattr(service, "_docker_image_catalog", load_docker_images)
    monkeypatch.setattr(service, "list_icon_assets", lambda: [])
    monkeypatch.setattr(service, "_build_paired_catalog", lambda: [])

    catalog = service.build_node_catalog()

    assert [entry["key"] for entry in catalog["templates"]] == ["one", "two"]
    assert loads == 1
    assert docker_loads == 1


def test_catalog_build_strict_docker_is_opt_in(tmp_path, monkeypatch):
    service = TemplateService.__new__(TemplateService)
    service.images_dir = tmp_path
    monkeypatch.setattr(service, "_load_templates", lambda: [_template("docker")])
    monkeypatch.setattr(service, "list_icon_assets", lambda: [])
    monkeypatch.setattr(service, "_build_paired_catalog", lambda: [])

    def load_docker_images(*, strict=False):
        assert strict is True
        return {}

    monkeypatch.setattr(service, "_docker_image_catalog", load_docker_images)
    assert service.build_node_catalog(strict_docker=True)["templates"][0]["key"] == "docker"


def test_legacy_catalog_keeps_non_docker_templates_available_when_docker_is_offline(
    tmp_path, monkeypatch
):
    service = TemplateService.__new__(TemplateService)
    service.images_dir = tmp_path
    qemu = _template("qemu")
    qemu.type = "qemu"
    iol = _template("iol")
    iol.type = "iol"
    monkeypatch.setattr(service, "_load_templates", lambda: [qemu, iol, _template("docker")])
    monkeypatch.setattr(service, "list_icon_assets", lambda: [])
    monkeypatch.setattr(service, "_build_paired_catalog", lambda: [])
    monkeypatch.setattr("app.services.template_service._qemu_version_options", lambda: [])

    def offline_docker(*, strict=False):
        if strict:
            raise DockerImageError("Cannot connect to the Docker daemon")
        return {}

    monkeypatch.setattr(service, "_docker_image_catalog", offline_docker)
    catalog = service.build_node_catalog()
    assert {entry["key"] for entry in catalog["templates"]} == {"qemu", "iol", "docker"}


@pytest.mark.parametrize(
    ("template_type", "raw"),
    [("qemu", {}), ("iol", {}), ("docker", {"image": "nova-ve/browser:latest"})],
)
def test_strict_catalog_skips_docker_discovery_when_it_is_not_needed(
    tmp_path, monkeypatch, template_type, raw
):
    service = TemplateService.__new__(TemplateService)
    service.images_dir = tmp_path
    template = _template(template_type)
    template.type = template_type
    template.raw = raw
    monkeypatch.setattr(service, "_load_templates", lambda: [template])
    monkeypatch.setattr(service, "list_icon_assets", lambda: [])
    monkeypatch.setattr(service, "_build_paired_catalog", lambda: [])
    monkeypatch.setattr("app.services.template_service._qemu_version_options", lambda: [])

    def unexpected_docker_discovery(*, strict=False):
        raise AssertionError("Docker discovery was not needed")

    monkeypatch.setattr(service, "_docker_image_catalog", unexpected_docker_discovery)
    catalog = service.build_node_catalog(strict_docker=True)
    assert catalog["templates"][0]["key"] == template_type


def test_legacy_list_images_keeps_docker_discovery_best_effort(tmp_path, monkeypatch):
    service = TemplateService.__new__(TemplateService)
    service.images_dir = tmp_path
    monkeypatch.setattr(service, "_load_templates", lambda: [_template("one")])

    def load_docker_images(*, strict=False):
        assert strict is False
        return {}

    monkeypatch.setattr(service, "_docker_image_catalog", load_docker_images)
    assert service.list_images("docker", "one") == {}


def test_node_validation_keeps_docker_discovery_best_effort(tmp_path, monkeypatch):
    service = TemplateService.__new__(TemplateService)
    service.images_dir = tmp_path
    monkeypatch.setattr(service, "_load_templates", lambda: [_template("docker")])

    def load_docker_images(*, strict=False):
        assert strict is False
        return {"alpine:latest": {"image": "alpine:latest", "source": "docker"}}

    monkeypatch.setattr(service, "_docker_image_catalog", load_docker_images)
    template = service.validate_node_request("docker", "docker", "alpine:latest")
    assert template.key == "docker"


def test_cache_key_uses_the_template_services_actual_sources(tmp_path, monkeypatch):
    template_settings = SimpleNamespace(
        TEMPLATES_DIR=tmp_path / "templates",
        USER_TEMPLATES_DIR=tmp_path / "user-templates",
        IMAGES_DIR=tmp_path / "images",
    )
    monkeypatch.setattr("app.services.template_service.get_settings", lambda: template_settings)
    monkeypatch.setattr(
        "app.routers.labs.get_settings",
        lambda: SimpleNamespace(
            TEMPLATES_DIR=tmp_path / "wrong-templates",
            IMAGES_DIR=tmp_path / "wrong-images",
        ),
    )

    assert labs._node_catalog_cache_key() == (
        str(template_settings.TEMPLATES_DIR),
        str(template_settings.USER_TEMPLATES_DIR),
        str(template_settings.IMAGES_DIR),
    )


@pytest.mark.asyncio
async def test_cold_build_is_shared_off_the_event_loop():
    started = threading.Event()
    release = threading.Event()
    builds = 0
    request_thread = threading.get_ident()
    build_threads = []
    published = []

    def build():
        nonlocal builds
        builds += 1
        build_threads.append(threading.get_ident())
        started.set()
        assert release.wait(2)
        return {"templates": [{"key": "fresh"}]}

    async def publish(generation):
        published.append(generation)

    service = NodeCatalogService(build, on_updated=publish)
    try:
        first = asyncio.create_task(service.get())
        second = asyncio.create_task(service.get())
        while not started.is_set():
            await asyncio.sleep(0.001)

        heartbeat = asyncio.create_task(asyncio.sleep(0.01, result="responsive"))
        assert await asyncio.wait_for(heartbeat, 0.2) == "responsive"
        release.set()
        first_result, second_result = await asyncio.gather(first, second)

        assert first_result == second_result == {"templates": [{"key": "fresh"}]}
        assert builds == 1
        assert build_threads[0] != request_thread
        while not published:
            await asyncio.sleep(0)
        assert published == [1]
    finally:
        release.set()
        await service.close()


@pytest.mark.asyncio
async def test_cold_get_does_not_wait_for_socket_notification():
    notification_started = asyncio.Event()
    finish_notification = asyncio.Event()

    async def publish(_generation):
        notification_started.set()
        await finish_notification.wait()

    service = NodeCatalogService(lambda: {"templates": []}, on_updated=publish)
    try:
        assert await asyncio.wait_for(service.get(), 0.2) == {"templates": []}
        await asyncio.wait_for(notification_started.wait(), 0.2)
    finally:
        finish_notification.set()
        await service.close()


@pytest.mark.asyncio
async def test_cold_catalog_keeps_non_docker_templates_available_while_discovery_recovers():
    now = 0.0
    builds = 0
    notified = asyncio.Event()
    qemu_only = {"templates": [{"type": "qemu"}]}
    full_catalog = {"templates": [{"type": "qemu"}, {"type": "docker"}]}

    def build():
        nonlocal builds
        builds += 1
        if builds == 1:
            raise RuntimeError("discovery temporarily unavailable")
        return full_catalog

    def build_degraded(error):
        assert str(error) == "discovery temporarily unavailable"
        return qemu_only

    async def publish(generation):
        assert generation == 1
        notified.set()

    service = NodeCatalogService(
        build, build_degraded=build_degraded, on_updated=publish, clock=lambda: now,
    )
    try:
        assert await service.get_with_pending() == (qemu_only, True)
        assert await service.get_with_pending() == (qemu_only, True)
        assert builds == 1
        assert not notified.is_set()

        now = 6.0
        assert await service.get_with_pending() == (qemu_only, True)
        await asyncio.wait_for(notified.wait(), 1)
        assert await service.get_with_pending() == (full_catalog, False)
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_stale_snapshot_returns_promptly_and_publishes_replacement():
    now = 0.0
    release = threading.Event()
    second_started = threading.Event()
    builds = 0
    updated = asyncio.Event()

    def build():
        nonlocal builds
        builds += 1
        if builds == 2:
            second_started.set()
            assert release.wait(2)
        return {"templates": [{"key": f"v{builds}"}]}

    async def publish(generation):
        if generation == 2:
            updated.set()

    service = NodeCatalogService(
        build,
        on_updated=publish,
        max_age_seconds=30,
        clock=lambda: now,
    )
    try:
        assert await service.get_with_pending() == ({"templates": [{"key": "v1"}]}, False)
        now = 31

        stale = await asyncio.wait_for(service.get_with_pending(), 0.2)
        assert stale == ({"templates": [{"key": "v1"}]}, True)
        while not second_started.is_set():
            await asyncio.sleep(0.001)
        assert await service.get_with_pending() == ({"templates": [{"key": "v1"}]}, True)
        assert builds == 2

        release.set()
        await asyncio.wait_for(updated.wait(), 0.2)
        assert await service.get_with_pending() == ({"templates": [{"key": "v2"}]}, False)
        assert builds == 2
    finally:
        release.set()
        await service.close()


@pytest.mark.asyncio
async def test_failed_refresh_keeps_snapshot_and_next_request_retries():
    now = 0.0
    builds = 0
    published = []

    def build():
        nonlocal builds
        builds += 1
        if builds == 2:
            raise subprocess.TimeoutExpired("docker image inspect", 5)
        return {"templates": [{"key": f"v{builds}"}]}

    async def publish(generation):
        published.append(generation)

    service = NodeCatalogService(
        build,
        on_updated=publish,
        max_age_seconds=1,
        retry_delay_seconds=1,
        clock=lambda: now,
    )
    try:
        assert await service.get() == {"templates": [{"key": "v1"}]}
        now = 1
        assert await service.get() == {"templates": [{"key": "v1"}]}
        failed_task = service._task
        assert failed_task is not None
        with pytest.raises(subprocess.TimeoutExpired):
            await failed_task

        now = 1.5
        assert await service.get_with_pending() == ({"templates": [{"key": "v1"}]}, True)
        assert builds == 2
        assert service._task is None

        now = 2
        assert await service.get_with_pending() == ({"templates": [{"key": "v1"}]}, True)
        retry_task = service._task
        assert retry_task is not None
        await retry_task
        assert await service.get_with_pending() == ({"templates": [{"key": "v3"}]}, False)
        assert builds == 3
        while len(published) < 2:
            await asyncio.sleep(0)
        assert published == [1, 2]
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_offline_docker_refresh_keeps_warm_template_catalog(tmp_path, monkeypatch):
    template_service = TemplateService.__new__(TemplateService)
    template_service.images_dir = tmp_path
    monkeypatch.setattr(template_service, "_load_templates", lambda: [_template("docker")])
    monkeypatch.setattr(template_service, "list_icon_assets", lambda: [])
    monkeypatch.setattr(template_service, "_build_paired_catalog", lambda: [])
    now = 0.0
    docker_online = True

    def discover_docker(*, strict=False):
        assert strict is True
        if not docker_online:
            raise DockerImageError("Cannot connect to the Docker daemon")
        return {
            "alpine:latest": {
                "image": "alpine:latest",
                "files": [],
                "path": "alpine:latest",
                "source": "docker",
            }
        }

    monkeypatch.setattr(template_service, "_docker_image_catalog", discover_docker)
    service = NodeCatalogService(
        lambda: template_service.build_node_catalog(strict_docker=True),
        max_age_seconds=1,
        retry_delay_seconds=5,
        clock=lambda: now,
    )
    try:
        warm = await service.get()
        assert warm["templates"][0]["defaults"]["image"] == "alpine:latest"

        docker_online = False
        now = 2
        assert await service.get_with_pending() == (warm, True)
        failed_task = service._task
        assert failed_task is not None
        with pytest.raises(DockerImageError, match="Cannot connect"):
            await failed_task

        assert await service.get_with_pending() == (warm, True)
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_shutdown_cancels_waiters_and_suppresses_late_completion():
    started = threading.Event()
    release = threading.Event()
    published = []
    builds = 0

    def build():
        nonlocal builds
        builds += 1
        started.set()
        release.wait(2)
        return {"templates": []}

    async def publish(generation):
        published.append(generation)

    service = NodeCatalogService(build, on_updated=publish)
    request = asyncio.create_task(service.get())
    while not started.is_set():
        await asyncio.sleep(0.001)

    await service.close()
    with pytest.raises(asyncio.CancelledError):
        await request

    service.start()
    restarted = asyncio.create_task(service.get())
    try:
        await asyncio.sleep(0.02)
        assert builds == 1
        assert published == []
    finally:
        release.set()
        assert await restarted == {"templates": []}
        await service.close()


@pytest.mark.asyncio
async def test_close_clears_a_build_that_never_started():
    service = NodeCatalogService(lambda: {"templates": []})
    service._start_build_locked("templates")

    await service.close()

    assert service._task is None
    assert service._building_key is None
    service.start()
    assert await service.get("templates") == {"templates": []}
    await service.close()


class _Socket:
    def __init__(self):
        self.messages = []

    async def send_json(self, message):
        self.messages.append(message)


@pytest.mark.asyncio
async def test_catalog_update_is_generation_only_and_skips_inactive_rooms():
    hub = WsHub()
    first = _Socket()
    second = _Socket()
    await hub.publish("one.json", "node_updated", {"private": True})
    await hub.subscribe("one.json", first, identity="lab-one")
    await hub.subscribe("two.json", second, identity="lab-two")
    await hub.publish("inactive.json", "node_updated", {"private": True})

    await hub.notify_catalog_updated(4)

    message = {"type": "node_catalog_updated", "payload": {"generation": 4}}
    assert first.messages == [message]
    assert second.messages == [message]
    assert hub.head_seq("one.json") == 1
    assert hub.head_seq("two.json") == 0
    replay, needs_resnapshot = hub.replay_since("one.json", 0, identity="lab-one")
    assert not needs_resnapshot
    assert [event.type for event in replay] == ["node_updated"]
    inactive_replay, needs_resnapshot = hub.replay_since("inactive.json", 1, identity="inactive")
    assert not needs_resnapshot
    assert inactive_replay == []
    assert hub.head_seq("inactive.json") == 1


def test_completion_between_hello_and_subscription_is_not_replayed(tmp_path, monkeypatch):
    import app.routers.ws as ws_router
    import app.services.ws_hub as ws_hub_module

    hub = WsHub()
    monkeypatch.setattr(ws_hub_module, "ws_hub", hub)
    monkeypatch.setattr(ws_router, "ws_hub", hub)
    monkeypatch.setattr(
        "app.services.lab_service.get_settings",
        lambda: SimpleNamespace(LABS_DIR=tmp_path),
    )
    (tmp_path / "demo.json").write_text(
        '{"schema":2,"id":"demo","nodes":{},"networks":{},"links":[]}'
    )
    user = SimpleNamespace(
        id=1,
        username="testuser",
        role="user",
        email="test@example.com",
        name="Test User",
        html5=True,
        folder="/",
    )

    async def validate_session(_self, _token, _username):
        return user

    monkeypatch.setattr(
        "app.services.auth_service.AuthService.validate_session", validate_session
    )
    monkeypatch.setattr("app.schemas.user.UserRead.model_validate", lambda obj: obj)

    subscribe_started = threading.Event()
    finish_subscribe = threading.Event()
    original_subscribe = hub.subscribe
    delayed = True

    async def delayed_subscribe(*args, **kwargs):
        nonlocal delayed
        if delayed:
            delayed = False
            subscribe_started.set()
            while not finish_subscribe.is_set():
                await asyncio.sleep(0.005)
        return await original_subscribe(*args, **kwargs)

    monkeypatch.setattr(hub, "subscribe", delayed_subscribe)
    client = TestClient(app)
    client.cookies.update({"nova_session": "fake-token", "nova_user": "testuser"})
    with client.websocket_connect("/ws/labs/demo.json?last_seq=0") as socket:
        assert socket.receive_json() == {"type": "hello", "seq": 0}
        assert subscribe_started.wait(1)
        asyncio.run(hub.notify_catalog_updated(7))
        finish_subscribe.set()
        asyncio.run(hub.publish("demo.json", "node_updated", {"id": 1}))
        assert socket.receive_json() == {
            "seq": 1,
            "type": "node_updated",
            "rev": "",
            "payload": {"id": 1},
        }


@pytest.mark.asyncio
async def test_slow_catalog_does_not_block_other_routes(tmp_path, monkeypatch):
    started = threading.Event()
    release = threading.Event()
    labs_dir = tmp_path / "labs"
    labs_dir.mkdir()
    (labs_dir / "demo.json").write_text(
        '{"schema":2,"id":"demo","nodes":{},"networks":{},"links":[]}'
    )
    settings = SimpleNamespace(LABS_DIR=labs_dir)
    monkeypatch.setattr("app.services.lab_service.get_settings", lambda: settings)

    def build():
        started.set()
        assert release.wait(2)
        return {"templates": [], "paired_templates": [], "icon_options": []}

    service = NodeCatalogService(build)
    monkeypatch.setattr(labs, "node_catalog_service", service)
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
        username="admin", role="admin", html5=True, folder="/"
    )
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            catalog_request = asyncio.create_task(client.get("/api/labs/demo.json/node-catalog"))
            while not started.is_set():
                await asyncio.sleep(0.001)
            health = await asyncio.wait_for(client.get("/VERSION"), 0.2)
            assert health.status_code == 200
            release.set()
            catalog = await catalog_request
            assert catalog.status_code == 200
            assert catalog.json()["data"]["templates"] == []
            assert catalog.json()["data"]["pending"] is False
    finally:
        release.set()
        app.dependency_overrides.clear()
        await service.close()
