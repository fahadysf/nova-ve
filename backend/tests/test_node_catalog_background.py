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
        assert strict is True
        return {}

    monkeypatch.setattr(service, "_load_templates", load_templates)
    monkeypatch.setattr(service, "_docker_image_catalog", load_docker_images)
    monkeypatch.setattr(service, "list_icon_assets", lambda: [])
    monkeypatch.setattr(service, "_build_paired_catalog", lambda: [])

    catalog = service.build_node_catalog()

    assert [entry["key"] for entry in catalog["templates"]] == ["one", "two"]
    assert loads == 1
    assert docker_loads == 1


def test_legacy_list_images_keeps_docker_discovery_best_effort(tmp_path, monkeypatch):
    service = TemplateService.__new__(TemplateService)
    service.images_dir = tmp_path
    monkeypatch.setattr(service, "_load_templates", lambda: [_template("one")])

    def load_docker_images(*, strict=False):
        assert strict is False
        return {}

    monkeypatch.setattr(service, "_docker_image_catalog", load_docker_images)
    assert service.list_images("docker", "one") == {}


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
async def test_shutdown_cancels_waiters_and_suppresses_late_completion():
    started = threading.Event()
    release = threading.Event()
    published = []

    def build():
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
    release.set()
    await asyncio.sleep(0.02)
    assert published == []

    service.start()
    assert await service.get() == {"templates": []}


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

    events = await hub.publish_all("node_catalog_updated", {"generation": 4})

    assert {event.seq for event in events} == {1, 2}
    assert first.messages == [
        {"seq": 2, "type": "node_catalog_updated", "rev": "", "payload": {"generation": 4}}
    ]
    assert second.messages == [
        {"seq": 1, "type": "node_catalog_updated", "rev": "", "payload": {"generation": 4}}
    ]
    replay, needs_resnapshot = hub.replay_since("one.json", 0, identity="lab-one")
    assert not needs_resnapshot
    assert [event.type for event in replay] == ["node_updated", "node_catalog_updated"]
    assert replay[-1].payload == {"generation": 4}
    inactive_replay, needs_resnapshot = hub.replay_since("inactive.json", 1, identity="inactive")
    assert not needs_resnapshot
    assert inactive_replay == []
    assert hub.head_seq("inactive.json") == 1


def test_completion_between_hello_and_subscription_is_replayed(tmp_path, monkeypatch):
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
        asyncio.run(hub.publish_all("node_catalog_updated", {"generation": 7}))
        finish_subscribe.set()
        assert socket.receive_json() == {
            "seq": 1,
            "type": "node_catalog_updated",
            "rev": "",
            "payload": {"generation": 7},
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
