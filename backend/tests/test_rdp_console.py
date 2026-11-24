# Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
# SPDX-License-Identifier: Apache-2.0

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.routers import labs
from app.services.guacamole_db_service import GuacamoleDatabaseService
from app.services.html5_service import Html5SessionService
from app.services.node_runtime_service import NodeRuntimeError, NodeRuntimeService


@pytest.mark.parametrize("password", [None, "container-session-password"])
def test_rdp_display_update_is_configured_for_both_guacamole_auth_modes(password):
    database = GuacamoleDatabaseService.__new__(GuacamoleDatabaseService)
    html5_params = Html5SessionService._connection_parameters("target", 3389, "rdp", password)
    database_params = database._connection_parameters(
        host="target", port=3389, protocol="rdp", rdp_password=password,
    )
    assert html5_params == database_params == {
        "hostname": "target", "port": "3389", "ignore-cert": "true",
        "security": "any", "resize-method": "display-update",
        **({"username": "lab", "password": password} if password else {}),
    }
    assert "resize-method" not in Html5SessionService._connection_parameters("target", 5900, "vnc")


@pytest.mark.parametrize("image", [
    "nova-ve/browser:latest", "nova-ve-lab/nova-ve_desktop:latest",
    "ghcr.io/fahadysf/nova-ve-desktop@sha256:digest",
])
def test_platform_desktop_login_comes_from_running_container(monkeypatch, tmp_path, image):
    monkeypatch.setattr("app.services.node_runtime_service.get_settings", lambda: SimpleNamespace(
        TMP_DIR=tmp_path, DOCKER_HOST="unix:///var/run/docker.sock",
    ))
    service = NodeRuntimeService()
    monkeypatch.setattr(service, "_runtime_record", lambda *_: {"container_id": "desktop-container"})
    monkeypatch.setattr(service, "_resolve_binary", lambda _: "docker")
    run = Mock(return_value=SimpleNamespace(returncode=0, stdout="session-password\n"))
    monkeypatch.setattr("app.services.node_runtime_service.subprocess.run", run)
    data = {"id": "lab-id", "nodes": {"1": {"type": "docker", "image": image}}}

    assert service.platform_rdp_password(data, 1) == "session-password"
    assert run.call_args.args[0][-6:] == [
        "exec", "--user", "0", "desktop-container", "cat", "/run/nova-ve/rdp-password",
    ]

    run.return_value = SimpleNamespace(returncode=1, stdout="sensitive output")
    with pytest.raises(NodeRuntimeError, match="login is unavailable") as caught:
        service.platform_rdp_password(data, 1)
    assert "sensitive output" not in str(caught.value)


def test_custom_rdp_images_keep_their_own_authentication(monkeypatch, tmp_path):
    monkeypatch.setattr("app.services.node_runtime_service.get_settings", lambda: SimpleNamespace(TMP_DIR=tmp_path))
    service = NodeRuntimeService()
    get_runtime = Mock()
    monkeypatch.setattr(service, "_runtime_record", get_runtime)
    data = {"nodes": {"1": {"type": "docker", "image": "example.com/nova-ve/desktop:latest"}}}

    assert service.platform_rdp_password(data, 1) is None
    get_runtime.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("database_url", ["", "postgresql://configured"])
async def test_secondary_rdp_launcher_passes_password_only_to_guacamole(monkeypatch, database_url):
    data = {"id": "lab-id"}
    endpoint = {"name": "desktop-second", "console": "rdp", "host": "target", "port": 3389}
    runtime = SimpleNamespace(
        console_info=Mock(return_value=endpoint),
        platform_rdp_password=Mock(return_value="session-password"),
    )
    monkeypatch.setattr(labs, "_scoped_lab_path", lambda *_args, **_kwargs: "sample.json")
    monkeypatch.setattr(labs, "_read_lab_data", lambda _: data)
    monkeypatch.setattr(labs, "get_settings", lambda: SimpleNamespace(GUACAMOLE_DATABASE_URL=database_url))
    monkeypatch.setattr(labs, "NodeRuntimeService", lambda: runtime)
    url = "/html5/#/client/test?token=test-token"

    async def create_console_url(_user, **kwargs):
        assert kwargs["rdp_password"] == "session-password"
        assert kwargs["protocol"] == "rdp"
        return url

    async def record_console_open(**kwargs):
        assert "session-password" not in str(kwargs)

    service = "GuacamoleDatabaseService" if database_url else "Html5SessionService"
    monkeypatch.setattr(labs, service, lambda: SimpleNamespace(create_console_url=create_console_url))
    monkeypatch.setattr(labs.audit_service, "record_console_open", record_console_open)
    response = await labs.node_html5(
        "sample.json", 1, console="second", current_user=SimpleNamespace(username="admin", html5=True),
    )

    runtime.console_info.assert_called_once_with(data, 1, mode="second")
    runtime.platform_rdp_password.assert_called_once_with(data, 1)
    assert response.status_code == 307
    assert response.headers["location"] == url
