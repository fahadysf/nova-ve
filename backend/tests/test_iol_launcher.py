from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services import runtime_pids
from app.services.runtime.iol import (
    _TELNET_DO,
    _TELNET_ECHO,
    _TELNET_IAC,
    _TELNET_SB,
    _TELNET_SE,
    _TELNET_SERVER_NEGOTIATION,
    _TELNET_SUPPRESS_GO_AHEAD,
    _TelnetInputFilter,
    IolError,
    IolLauncher,
)


def test_iol_application_id_is_deterministic_and_avoids_active_ids():
    first = IolLauncher.application_id("lab-a", 7, set())
    assert first == IolLauncher.application_id("lab-a", 7, set())

    second = IolLauncher.application_id("lab-a", 7, {first})
    assert 1 <= second <= 512
    assert second != first


def test_iol_command_uses_adapter_counts_and_application_id(tmp_path: Path):
    image = tmp_path / "i86bi-linux-l3.bin"
    image.write_text("fake")
    node = {
        "id": 7,
        "type": "iol",
        "ethernet": 9,
        "ram": 768,
        "extras": {"nvram": 512, "serial_adapters": 0},
    }

    command = IolLauncher.build_command(image, node, 123)

    assert command == [
        str(image),
        "-e",
        "3",
        "-s",
        "0",
        "-n",
        "512",
        "-m",
        "768",
        "123",
    ]


def test_iol_netmap_maps_unique_bridge_id_to_application_id(tmp_path: Path):
    netmap = tmp_path / "NETMAP"

    IolLauncher.write_netmap(netmap, application_id=123, bridge_id=635)

    lines = netmap.read_text().splitlines()
    assert len(lines) == 64
    assert lines[0] == "635:0/0  123:0/0"
    assert lines[-1] == "635:15/3  123:15/3"


def test_iol_image_resolution_supports_importer_directory_layout(tmp_path: Path):
    root = tmp_path / "images" / "iol"
    image_dir = root / "i86bi-linux-l3"
    image_dir.mkdir(parents=True)
    image = image_dir / "i86bi-linux-l3.bin"
    image.write_text("fake")

    resolved = IolLauncher.resolve_image({"image": "i86bi-linux-l3"}, root)

    assert resolved == image


def test_iol_environment_uses_iourc_next_to_image(tmp_path: Path):
    image_dir = tmp_path / "i86bi-linux-l3"
    image_dir.mkdir()
    image = image_dir / "i86bi-linux-l3.bin"
    image.write_text("fake")
    iourc = image_dir / "iourc"
    iourc.write_text("[license]\n")

    env = IolLauncher.build_environment(IolLauncher.resolve_iourc(image))

    assert env is not None
    assert env["IOURC"] == str(iourc)


def test_iol_iourc_prefers_image_local_over_system(tmp_path: Path):
    image_dir = tmp_path / "image"
    image_dir.mkdir()
    image = image_dir / "i86bi-linux-l3.bin"
    image.write_text("fake")
    local_iourc = image_dir / "iourc"
    local_iourc.write_text("[local]\n")
    system_dir = tmp_path / "system"
    system_dir.mkdir()
    (system_dir / "iourc").write_text("[system]\n")

    assert IolLauncher.resolve_iourc(image, system_dir) == local_iourc


def test_iol_iourc_falls_back_to_system_directory(tmp_path: Path):
    image = tmp_path / "i86bi-linux-l3.bin"
    image.write_text("fake")
    system_dir = tmp_path / "system"
    system_dir.mkdir()
    system_iourc = system_dir / "iourc"
    system_iourc.write_text("[system]\n")

    env = IolLauncher.build_environment(IolLauncher.resolve_iourc(image, system_dir))

    assert env is not None
    assert env["IOURC"] == str(system_iourc)


def test_iol_environment_is_inherited_when_iourc_is_absent(tmp_path: Path):
    image = tmp_path / "i86bi-linux-l3.bin"
    image.write_text("fake")

    env = IolLauncher.build_environment(IolLauncher.resolve_iourc(image, tmp_path / "missing"))

    assert env is None


def test_iol_console_advertises_remote_echo_to_telnet_clients():
    assert _TELNET_SERVER_NEGOTIATION == bytes(
        [
            _TELNET_IAC,
            251,
            _TELNET_ECHO,
            _TELNET_IAC,
            251,
            _TELNET_SUPPRESS_GO_AHEAD,
        ]
    )


def test_iol_telnet_filter_strips_negotiation_and_preserves_input():
    parser = _TelnetInputFilter()

    filtered = parser.feed(
        bytes([_TELNET_IAC, _TELNET_DO, _TELNET_ECHO])
        + b"show"
        + bytes([_TELNET_IAC, _TELNET_DO, _TELNET_SUPPRESS_GO_AHEAD])
        + b" clock\r"
    )

    assert filtered == b"show clock\r"


def test_iol_telnet_filter_handles_split_subnegotiation():
    parser = _TelnetInputFilter()

    assert parser.feed(bytes([_TELNET_IAC, _TELNET_SB, 31, 0, 80])) == b""
    assert parser.feed(bytes([0, 24, _TELNET_IAC, _TELNET_SE]) + b"x") == b"x"


def test_iol_telnet_filter_preserves_escaped_iac_data():
    parser = _TelnetInputFilter()

    assert parser.feed(b"a" + bytes([_TELNET_IAC, _TELNET_IAC]) + b"b") == (
        b"a" + bytes([_TELNET_IAC]) + b"b"
    )


def test_iol_exec_not_found_message_explains_existing_32_bit_image(tmp_path: Path):
    image = tmp_path / "i86bi-linux.bin"
    image.write_text("fake")

    message = IolLauncher.exec_not_found_message(
        image, FileNotFoundError(2, "No such file or directory", str(image))
    )

    assert "file exists" in message
    assert "32-bit runtime packages" in message


def test_runtime_pid_registry_accepts_iol_kind(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("NOVA_VE_PIDS_JSON", str(tmp_path / "pids.json"))

    runtime_pids.register(1234, "iol", "lab-iol", 7)

    assert runtime_pids.lookup(1234)["kind"] == "iol"


_MISSING_CONFIG = object()


class _FakeIolProcess:
    def __init__(self, pid: int):
        self.pid = pid

    def poll(self):
        return None


def _native_iol_fixture(monkeypatch, tmp_path: Path):
    image = tmp_path / "iol-fixture.bin"
    image.write_bytes(b"not an emulator")
    ubridge = tmp_path / "ubridge-fixture"
    ubridge.write_bytes(b"not a helper")
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    preferred_id = IolLauncher.application_id("lab-native-nvram", 7, set())
    native_path = work_dir / f"nvram_{preferred_id:05d}"
    popen_calls = []

    def fake_popen(command, **kwargs):
        popen_calls.append((list(command), kwargs))
        return _FakeIolProcess(7000 + len(popen_calls))

    class FakeUbridgeClient:
        def __init__(self, port):
            self.port = port

        def request(self, command):
            return None

        def close(self):
            return None

    class FakeConsoleBridge:
        def __init__(self, **kwargs):
            pass

        def start(self):
            return None

        def stop(self):
            return None

    def unexpected_signal(*args):
        raise AssertionError(f"unexpected process signal: {args}")

    monkeypatch.setattr("app.services.runtime.iol._UBRIDGE_BINARY", str(ubridge))
    monkeypatch.setattr("app.services.runtime.iol.subprocess.Popen", fake_popen)
    monkeypatch.setattr(IolLauncher, "_terminate_pid", staticmethod(lambda pid: None))
    monkeypatch.setattr("app.services.runtime.iol.os.killpg", unexpected_signal)
    monkeypatch.setattr("app.services.runtime.iol.os.kill", unexpected_signal)
    monkeypatch.setattr("app.services.runtime.iol._UbridgeClient", FakeUbridgeClient)
    monkeypatch.setattr("app.services.runtime.iol.IolConsoleBridge", FakeConsoleBridge)
    monkeypatch.setattr(IolLauncher, "_free_tcp_port", staticmethod(lambda: 18000))
    monkeypatch.setattr(IolLauncher, "resolve_iourc", staticmethod(lambda image_path: None))
    monkeypatch.setattr("app.services.runtime.iol.os.openpty", lambda: (101, 102))
    monkeypatch.setattr("app.services.runtime.iol.os.close", lambda fd: None)
    monkeypatch.setattr("app.services.runtime.iol.time.sleep", lambda seconds: None)
    monkeypatch.setattr(
        "psutil.Process", lambda pid: SimpleNamespace(create_time=lambda: 1000.0 + pid)
    )
    monkeypatch.setattr(IolLauncher, "_processes", {})
    monkeypatch.setattr(IolLauncher, "_bridges", {})

    def start(active_ids: set[int], config=_MISSING_CONFIG):
        extras = {"nvram": 1024}
        if config is not _MISSING_CONFIG:
            extras["config"] = config
        return IolLauncher.start_node(
            lab_id="lab-native-nvram",
            node_id=7,
            node={
                "id": 7,
                "type": "iol",
                "image": str(image),
                "ethernet": 4,
                "ram": 1024,
                "extras": extras,
            },
            images_root=tmp_path,
            work_dir=work_dir,
            console_port=15000,
            attachments=[],
            tap_factory=lambda *_: pytest.fail("unexpected TAP setup"),
            active_application_ids=active_ids,
        )

    return SimpleNamespace(
        image=image,
        native_path=native_path,
        popen_calls=popen_calls,
        preferred_id=preferred_id,
        start=start,
        work_dir=work_dir,
    )


def test_iol_unconfigured_start_reuses_preferred_native_nvram(monkeypatch, tmp_path):
    fixture = _native_iol_fixture(monkeypatch, tmp_path)
    fixture.native_path.write_bytes(b"native-state\x00\xff")

    record = fixture.start(set())

    assert record.application_id == fixture.preferred_id
    assert record.command[-1] == str(fixture.preferred_id)
    assert fixture.popen_calls[1][0] == record.command
    assert fixture.popen_calls[1][1]["cwd"] == fixture.work_dir
    assert fixture.native_path.read_bytes() == b"native-state\x00\xff"
    assert list(fixture.work_dir.glob("nvram_*")) == [fixture.native_path]


@pytest.mark.parametrize("config", [_MISSING_CONFIG, None, ""], ids=["missing", "null", "empty"])
def test_iol_unconfigured_start_uses_unique_alternate_native_nvram(
    monkeypatch, tmp_path, config
):
    fixture = _native_iol_fixture(monkeypatch, tmp_path)
    alternate_id = fixture.preferred_id % 512 + 1
    native_path = fixture.work_dir / f"nvram_{alternate_id:05d}"
    native_path.write_bytes(b"alternate-state")

    record = fixture.start(set(), config=config)

    assert record.application_id == alternate_id
    assert record.command[-1] == str(alternate_id)
    assert native_path.read_bytes() == b"alternate-state"
    assert list(fixture.work_dir.glob("nvram_*")) == [native_path]


def test_iol_unconfigured_ignores_backup_sidecar_next_to_valid_native_file(
    monkeypatch, tmp_path
):
    fixture = _native_iol_fixture(monkeypatch, tmp_path)
    alternate_id = fixture.preferred_id % 512 + 1
    native_path = fixture.work_dir / f"nvram_{alternate_id:05d}"
    native_path.write_bytes(b"alternate-state")
    backup_path = fixture.work_dir / f"{native_path.name}.bak"
    backup_path.write_bytes(b"backup-state")

    record = fixture.start(set())

    assert record.application_id == alternate_id
    assert native_path.read_bytes() == b"alternate-state"
    assert backup_path.read_bytes() == b"backup-state"


@pytest.mark.parametrize("use_alternate", [False, True], ids=["preferred", "alternate"])
def test_iol_unconfigured_busy_native_id_preserves_file_and_later_reuses_it(
    monkeypatch, tmp_path, use_alternate
):
    fixture = _native_iol_fixture(monkeypatch, tmp_path)
    native_id = fixture.preferred_id % 512 + 1 if use_alternate else fixture.preferred_id
    native_path = fixture.work_dir / f"nvram_{native_id:05d}"
    native_path.write_bytes(b"native-state")
    image_mode = fixture.image.stat().st_mode

    with pytest.raises(
        IolError, match=rf"application ID {native_id}.*in use.*Retry when.*available"
    ) as exc_info:
        fixture.start({native_id})

    assert str(tmp_path) not in str(exc_info.value)
    assert fixture.popen_calls == []
    assert not (fixture.work_dir / "NETMAP").exists()
    assert fixture.image.stat().st_mode == image_mode
    assert native_path.read_bytes() == b"native-state"

    record = fixture.start(set())
    assert record.application_id == native_id
    assert native_path.read_bytes() == b"native-state"


def test_iol_unconfigured_multiple_native_files_are_ambiguous(monkeypatch, tmp_path):
    fixture = _native_iol_fixture(monkeypatch, tmp_path)
    fixture.native_path.write_bytes(b"preferred-state")
    alternate_id = fixture.preferred_id % 512 + 1
    historical_path = fixture.work_dir / f"nvram_{alternate_id:05d}"
    historical_path.write_bytes(b"historical-state")
    image_mode = fixture.image.stat().st_mode

    with pytest.raises(IolError, match="multiple native NVRAM files") as exc_info:
        fixture.start(set())

    assert "Keep exactly one" in str(exc_info.value)
    assert str(tmp_path) not in str(exc_info.value)
    assert fixture.popen_calls == []
    assert not (fixture.work_dir / "NETMAP").exists()
    assert fixture.image.stat().st_mode == image_mode
    assert fixture.native_path.read_bytes() == b"preferred-state"
    assert historical_path.read_bytes() == b"historical-state"


@pytest.mark.parametrize("config", ["Saved", "Exported"])
def test_iol_saved_and_exported_collisions_keep_alternate_id_behavior(
    monkeypatch, tmp_path, config
):
    fixture = _native_iol_fixture(monkeypatch, tmp_path)
    fixture.native_path.write_bytes(b"native-state")
    alternate_id = fixture.preferred_id % 512 + 1
    historical_path = fixture.work_dir / f"nvram_{alternate_id:05d}"
    historical_path.write_bytes(b"historical-state")

    record = fixture.start({fixture.preferred_id}, config=config)

    assert record.application_id != fixture.preferred_id
    assert record.command[-1] == str(record.application_id)
    assert fixture.native_path.read_bytes() == b"native-state"
    assert historical_path.read_bytes() == b"historical-state"


def test_iol_unconfigured_collision_without_native_file_keeps_alternate_id_behavior(
    monkeypatch, tmp_path
):
    fixture = _native_iol_fixture(monkeypatch, tmp_path)

    record = fixture.start({fixture.preferred_id})

    assert record.application_id != fixture.preferred_id
    assert list(fixture.work_dir.glob("nvram_*")) == []


@pytest.mark.parametrize("entry_kind", ["symlink", "directory"])
def test_iol_unconfigured_refuses_nonregular_native_nvram(monkeypatch, tmp_path, entry_kind):
    fixture = _native_iol_fixture(monkeypatch, tmp_path)
    target = tmp_path / "native-target"
    if entry_kind == "symlink":
        target.write_bytes(b"native-state")
        fixture.native_path.symlink_to(target)
    else:
        fixture.native_path.mkdir()

    with pytest.raises(IolError, match="regular, non-symlink file") as exc_info:
        fixture.start(set())

    assert "Replace it" in str(exc_info.value)
    assert str(tmp_path) not in str(exc_info.value)
    assert fixture.popen_calls == []
    if entry_kind == "symlink":
        assert fixture.native_path.is_symlink()
        assert target.read_bytes() == b"native-state"
    else:
        assert fixture.native_path.is_dir()


def test_iol_unconfigured_fails_closed_when_native_nvram_inspection_fails(
    monkeypatch, tmp_path
):
    fixture = _native_iol_fixture(monkeypatch, tmp_path)
    fixture.native_path.write_bytes(b"native-state")
    original_lstat = Path.lstat

    def fail_native_lstat(path):
        if path == fixture.native_path:
            raise PermissionError("fixture inspection failure")
        return original_lstat(path)

    monkeypatch.setattr(Path, "lstat", fail_native_lstat)

    with pytest.raises(IolError, match="could not inspect native NVRAM.*Restore access"):
        fixture.start(set())

    assert fixture.popen_calls == []


def test_iol_unconfigured_fails_closed_when_native_nvram_is_unreadable(monkeypatch, tmp_path):
    fixture = _native_iol_fixture(monkeypatch, tmp_path)
    fixture.native_path.write_bytes(b"native-state")
    original_open = Path.open

    def fail_native_open(path, mode="r", *args, **kwargs):
        if path == fixture.native_path and mode == "rb":
            raise PermissionError("fixture read failure")
        return original_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", fail_native_open)

    with pytest.raises(IolError, match="could not inspect native NVRAM.*Restore access"):
        fixture.start(set())

    assert fixture.popen_calls == []
    with original_open(fixture.native_path, "rb") as native_file:
        assert native_file.read() == b"native-state"


@pytest.mark.parametrize("suffix", ["00000", "00513"])
def test_iol_unconfigured_refuses_unsupported_native_application_id(
    monkeypatch, tmp_path, suffix
):
    fixture = _native_iol_fixture(monkeypatch, tmp_path)
    native_path = fixture.work_dir / f"nvram_{suffix}"
    native_path.write_bytes(b"native-state")

    with pytest.raises(IolError, match="supported IOL application ID") as exc_info:
        fixture.start(set())

    assert "Use a supported native file" in str(exc_info.value)
    assert "wipe" not in str(exc_info.value)
    assert str(tmp_path) not in str(exc_info.value)
    assert fixture.popen_calls == []
    assert native_path.read_bytes() == b"native-state"
