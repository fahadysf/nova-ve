"""Unit tests for the Dynamips hypervisor client.

These tests stand up a real loopback TCP server that speaks the
Dynamips hypervisor reply format and verify the client's request/reply
parser. They do NOT require the ``dynamips`` binary; the goal is to
catch regressions in the protocol surface before any deploy.
"""

from __future__ import annotations

import socket
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import pytest

from app.services.runtime import dynamips
from app.services.runtime.dynamips import (
    DynamipsError,
    DynamipsLauncher,
    HypervisorClient,
)


def test_hypervisor_launch_binds_to_loopback(monkeypatch) -> None:
    launcher = DynamipsLauncher()
    process = Mock()
    process.poll.return_value = None
    spawn = Mock(return_value=process)
    client = Mock()
    monkeypatch.setattr(dynamips.shutil, "which", lambda binary: binary)
    monkeypatch.setattr(dynamips.subprocess, "Popen", spawn)
    monkeypatch.setattr(dynamips.socket, "create_connection", MagicMock())
    monkeypatch.setattr(dynamips, "HypervisorClient", Mock(return_value=client))
    monkeypatch.setattr(launcher, "_pick_free_tcp_port", lambda: 17200)

    launcher._start_hypervisor()

    assert spawn.call_args.args[0] == [dynamips._DYNAMIPS_BINARY, "-H", "127.0.0.1:17200"]
    client.connect.assert_called_once_with()


@pytest.mark.parametrize(
    ("template", "expected"),
    [({}, 512), ({"platform": "C3725"}, 256), ({"platform": "c7200", "ram": 384}, 384)],
)
def test_quota_ram_matches_dynamips_platform_defaults(template, expected):
    assert DynamipsLauncher.configured_ram_mib(template) == expected


@pytest.mark.parametrize(("status", "expected"), [("0", False), ("1", True), ("2", True), (None, None)])
def test_quota_liveness_distinguishes_stopped_from_unknown(status, expected):
    launcher = DynamipsLauncher()
    launcher._hypervisor_proc = SimpleNamespace(poll=lambda: None)
    launcher._hypervisor_port = 17200
    client = Mock()
    launcher._client = client
    if status is None:
        client.request.side_effect = DynamipsError("No hypervisor reply")
    else:
        client.request.return_value = SimpleNamespace(lines=[status])

    assert launcher.quota_liveness({"vm_name": "fixture", "hypervisor_port": 17200}) is expected
    assert client.close.call_count == (1 if status is None else 0)


def _serve_replies(replies: list[bytes]) -> tuple[int, threading.Event]:
    """Spin up a one-shot loopback server that emits each reply per request.

    Returns the bound port and a ``done`` event the test can wait on.
    The server accepts one connection, then for each line the client
    sends it pops one entry from ``replies`` and writes it back.
    """
    done = threading.Event()
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]

    def _run() -> None:
        try:
            conn, _ = server.accept()
            with conn:
                buf = b""
                for reply in replies:
                    while b"\n" not in buf:
                        chunk = conn.recv(4096)
                        if not chunk:
                            break
                        buf += chunk
                    line, _, buf = buf.partition(b"\n")
                    conn.sendall(reply)
        finally:
            server.close()
            done.set()

    threading.Thread(target=_run, daemon=True).start()
    return port, done


def test_request_parses_consecutive_single_line_replies(monkeypatch) -> None:
    monkeypatch.setattr("app.services.runtime.dynamips._HYPERVISOR_READ_TIMEOUT_S", 0.5)
    port, done = _serve_replies([b"100-0\r\n", b"100-OK\r\n"])
    client = HypervisorClient(port)
    try:
        status = client.request("vm get_status r1")
        assert status.lines == ["0"]
        reply = client.request("vm delete r1")
        assert reply.code == 100
        assert reply.ok is True
        assert reply.lines == ["OK"]
    finally:
        client.close()
    done.wait(2.0)


def test_request_parses_multiline_success() -> None:
    # Dynamips sets the hyphen only on the terminal record.
    port, done = _serve_replies(
        [b"101 line1\r\n101 line2\r\n100-final\r\n"]
    )
    client = HypervisorClient(port)
    try:
        reply = client.request("vm extract_idle_pc r1")
        assert reply.code == 100
        assert reply.lines == ["line1", "line2", "final"]
    finally:
        client.close()
    done.wait(2.0)


def test_request_raises_on_error_code() -> None:
    port, done = _serve_replies([b"209-invalid argument\r\n"])
    client = HypervisorClient(port)
    try:
        with pytest.raises(DynamipsError, match="209"):
            client.request("vm bogus_command r1")
    finally:
        client.close()
    done.wait(2.0)


def test_request_raises_on_malformed_reply() -> None:
    port, done = _serve_replies([b"garbage line\r\n"])
    client = HypervisorClient(port)
    try:
        with pytest.raises(DynamipsError, match="malformed"):
            client.request("hypervisor version")
    finally:
        client.close()
    done.wait(2.0)


def test_request_raises_on_premature_close() -> None:
    # Server immediately closes the connection without sending anything.
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]

    def _run() -> None:
        try:
            conn, _ = server.accept()
            conn.close()
        finally:
            server.close()

    threading.Thread(target=_run, daemon=True).start()

    client = HypervisorClient(port)
    try:
        with pytest.raises(DynamipsError, match="closed the connection"):
            client.request("hypervisor version")
    finally:
        client.close()


# ---- Image-path resolution -----------------------------------------------


def test_resolve_image_path_accepts_flat_layout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.services.runtime.dynamips._IMAGES_ROOT", tmp_path)
    filename = "c7200-adventerprisek9-mz.124-24.T5.image"
    (tmp_path / filename).write_bytes(b"FAKE")

    resolved = DynamipsLauncher._resolve_image_path({"image": filename})
    assert resolved == tmp_path / filename


def test_resolve_image_path_accepts_eveng_subdir_layout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """EVE-NG importer drops images at ``<root>/<key>/<key>.image`` —
    one image per subdirectory, where the subdir name equals the
    filename stem. The resolver must find images in this layout when
    the template stores only the bare filename.
    """
    monkeypatch.setattr("app.services.runtime.dynamips._IMAGES_ROOT", tmp_path)
    key = "c3725-adventerprisek9-mz.124-15.T14"
    filename = f"{key}.image"
    (tmp_path / key).mkdir()
    (tmp_path / key / filename).write_bytes(b"FAKE")

    resolved = DynamipsLauncher._resolve_image_path({"image": filename})
    assert resolved == tmp_path / key / filename


def test_resolve_image_path_prefers_flat_over_nested(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the same filename exists in both layouts (rare but possible
    after a manual ``cp``), the flat-layout copy wins — matches the
    resolver's documented search order.
    """
    monkeypatch.setattr("app.services.runtime.dynamips._IMAGES_ROOT", tmp_path)
    key = "c7200-image"
    filename = f"{key}.image"
    (tmp_path / filename).write_bytes(b"FLAT")
    (tmp_path / key).mkdir()
    (tmp_path / key / filename).write_bytes(b"NESTED")

    resolved = DynamipsLauncher._resolve_image_path({"image": filename})
    assert resolved.read_bytes() == b"FLAT"


def test_resolve_image_path_absolute_passthrough(tmp_path: Path) -> None:
    image = tmp_path / "absolute-c7200.image"
    image.write_bytes(b"FAKE")
    resolved = DynamipsLauncher._resolve_image_path({"image": str(image)})
    assert resolved == image


def test_resolve_image_path_missing_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.services.runtime.dynamips._IMAGES_ROOT", tmp_path)
    with pytest.raises(DynamipsError, match="not found"):
        DynamipsLauncher._resolve_image_path({"image": "missing.image"})


def test_resolve_image_path_no_image_field_raises() -> None:
    with pytest.raises(DynamipsError, match="no image path"):
        DynamipsLauncher._resolve_image_path({})


def test_resolve_image_path_stem_only_nested_eveng_layout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The node-create catalog reports the image directory name (no
    extension) for the EVE-NG subdir layout. Saved onto a node, that
    is the stem the launcher receives — it must still find the
    ``<stem>/<stem>.image`` file on disk.
    """
    monkeypatch.setattr("app.services.runtime.dynamips._IMAGES_ROOT", tmp_path)
    stem = "c3725-%5B1%5D-adventerprisek9-mz.124-25d"
    subdir = tmp_path / stem
    subdir.mkdir()
    actual = subdir / f"{stem}.image"
    actual.write_bytes(b"FAKE")

    resolved = DynamipsLauncher._resolve_image_path({"image": stem})
    assert resolved == actual


def test_resolve_image_path_stem_only_flat_image_ext(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.services.runtime.dynamips._IMAGES_ROOT", tmp_path)
    stem = "c7200-bare-stem"
    actual = tmp_path / f"{stem}.image"
    actual.write_bytes(b"FAKE")

    resolved = DynamipsLauncher._resolve_image_path({"image": stem})
    assert resolved == actual


def test_resolve_image_path_stem_only_flat_bin_ext(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.services.runtime.dynamips._IMAGES_ROOT", tmp_path)
    stem = "c7200-binary-stem"
    actual = tmp_path / f"{stem}.bin"
    actual.write_bytes(b"FAKE")

    resolved = DynamipsLauncher._resolve_image_path({"image": stem})
    assert resolved == actual


def test_resolve_image_path_stem_only_missing_lists_candidates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.services.runtime.dynamips._IMAGES_ROOT", tmp_path)
    with pytest.raises(DynamipsError) as exc:
        DynamipsLauncher._resolve_image_path({"image": "missing-stem"})
    # Error must enumerate the candidate paths searched so operators
    # can diagnose layout issues from the message alone.
    assert ".image" in str(exc.value)
    assert ".bin" in str(exc.value)


# ---- NIO cleanup on stop --------------------------------------------------


class _RecordingClient:
    """In-memory stand-in for HypervisorClient that records commands and
    replies with ``100 OK`` to every request. The ``fail_for`` list
    lets a test mark specific commands as failures so the launcher's
    error swallowing can be exercised.
    """

    def __init__(self, fail_for: tuple[str, ...] = ()) -> None:
        self.commands: list[str] = []
        self.fail_for = fail_for

    def request(self, command: str):  # noqa: ANN201 - mimics HypervisorClient
        self.commands.append(command)
        if any(needle in command for needle in self.fail_for):
            raise DynamipsError(f"hypervisor refused command {command!r}: 209 test")

        class _R:
            ok = True
            lines: list[str] = ["OK"]

        return _R()


@pytest.mark.parametrize("ethernet_only, ethernet_index", [(False, 4), (True, 0)])
def test_start_uses_c7200_hardware_options_and_correct_port_order(monkeypatch, tmp_path, ethernet_only, ethernet_index):
    launcher = DynamipsLauncher()
    client = _RecordingClient()
    monkeypatch.setattr("app.services.runtime.dynamips._DYNAMIPS_VM_ROOT", tmp_path)
    monkeypatch.setattr(launcher, "_client_locked", lambda: client)
    monkeypatch.setattr(launcher, "_resolve_image_path", lambda _: tmp_path / "router.image")
    monkeypatch.setattr(launcher, "_pick_free_tcp_port", lambda: 20001)

    launcher.start_node(
        lab_id="fixture", node_id=1, node={"extras": {"dynamips_ethernet_only": ethernet_only}}, console_port=15001,
        template={
            "platform": "c7200", "idlepc": "0x123", "npe": "npe-300", "nvram": 256,
            "midplane": "std", "slot0": "none", "slot1": "PA-4T+", "slot2": "PA-FE-TX",
        },
        attachments=[{"interface_index": ethernet_index, "bridge_name": "fixture"}],
        tap_factory=lambda *_: "fixture-tap",
    )

    assert "vm set_nvram nv_fixture_n1 256" in client.commands
    assert "c7200 set_midplane nv_fixture_n1 std" in client.commands
    assert "c7200 set_npe nv_fixture_n1 npe-300" in client.commands
    assert "vm set_idle_pc nv_fixture_n1 0x123" in client.commands
    assert "vm slot_add_binding nv_fixture_n1 0 0 C7200-IO-FE" not in client.commands
    assert f"vm slot_add_nio_binding nv_fixture_n1 2 0 nv_fixture_n1_i{ethernet_index}" in client.commands


def test_destroy_vm_deletes_named_nios() -> None:
    """``vm delete`` does not free the per-interface NIOs from dynamips'
    global registry. The launcher must explicitly issue
    ``nio delete <name>`` after the VM teardown, or the next start of
    the same node will collide on the duplicate NIO name and fail with
    ``206 unable to create TAP NIO``.
    """
    launcher = DynamipsLauncher()
    client = _RecordingClient()
    launcher._destroy_vm_locked(
        client,  # type: ignore[arg-type]
        "nv_lab_n3",
        nio_names=["nv_lab_n3_i0", "nv_lab_n3_i1"],
    )
    assert client.commands == [
        "vm stop nv_lab_n3",
        "vm delete nv_lab_n3",
        "nio delete nv_lab_n3_i0",
        "nio delete nv_lab_n3_i1",
    ]


def test_destroy_vm_swallows_absent_nio_errors() -> None:
    """``nio delete`` on a not-yet-registered NIO is a no-op for our
    purposes — happens on first-start, half-failed start, or after a
    hypervisor process restart. The launcher must not propagate the
    error.
    """
    launcher = DynamipsLauncher()
    client = _RecordingClient(fail_for=("nio delete",))
    launcher._destroy_vm_locked(
        client,  # type: ignore[arg-type]
        "nv_lab_n3",
        nio_names=["nv_lab_n3_i0"],
    )
    assert "nio delete nv_lab_n3_i0" in client.commands


def test_c3725_schema_exposes_three_slot_fields() -> None:
    from app.services.template_service import _dynamips_extras_schema
    keys = [f["key"] for f in _dynamips_extras_schema("c3725")]
    assert "slot0" in keys
    assert "slot1" in keys
    assert "slot2" in keys


def test_stop_node_reconstructs_nio_names_for_legacy_runtime(
    tmp_path: Path,
) -> None:
    """Runtime records persisted before the NIO-cleanup change have
    ``tap_names`` but no ``nio_names``. ``stop_node`` must still issue
    the NIO deletions so a subsequent start works — derive the names
    from ``lab_id``/``node_id`` and the tap-name count.
    """
    launcher = DynamipsLauncher()
    client = _RecordingClient()

    work_dir = tmp_path / "lab" / "3"
    work_dir.mkdir(parents=True)
    # Pre-write a runtime record so ``_clear_runtime`` has something to
    # unlink (matches what ``_persist_runtime`` would have left behind).
    (work_dir / "dynamips.json").write_text("{}")

    legacy_runtime = {
        "kind": "dynamips",
        "lab_id": "lab",
        "node_id": 3,
        "vm_name": "nv_lab_n3",
        "tap_names": ["nve_a", "nve_b"],
        "work_dir": str(work_dir),
        # Crucially: no ``nio_names`` field.
    }

    # Bypass the real hypervisor by injecting the recording client. The
    # launcher's lock is reentrant-safe for this single call.
    launcher._client = client  # type: ignore[assignment]
    launcher._hypervisor_proc = None  # type: ignore[assignment]
    # ``_client_locked`` would otherwise try to spawn dynamips; short-circuit it.
    launcher._client_locked = lambda: client  # type: ignore[method-assign]

    launcher.stop_node(legacy_runtime)

    assert "vm stop nv_lab_n3" in client.commands
    assert "vm delete nv_lab_n3" in client.commands
    assert "nio delete nv_lab_n3_i0" in client.commands
    assert "nio delete nv_lab_n3_i1" in client.commands


def test_confirmed_stop_keeps_runtime_after_a_protocol_failure(tmp_path):
    class Client:
        def __init__(self):
            self.present = True
            self.fail = True
            self.closed = 0

        def close(self):
            self.closed += 1

        def request(self, command):
            if command == "vm list":
                return SimpleNamespace(lines=["fixture (c3725)", "OK"] if self.present else ["OK"])
            if command == "vm get_status fixture":
                return SimpleNamespace(lines=["2"])
            if command == "vm stop fixture" and self.fail:
                raise DynamipsError("protocol timeout")
            if command == "vm delete fixture":
                self.present = False
            assert command in {"vm stop fixture", "vm delete fixture"}
            return SimpleNamespace(lines=["OK"])

    launcher = DynamipsLauncher()
    client = Client()
    launcher._client = client
    launcher._hypervisor_proc = SimpleNamespace(poll=lambda: None)
    launcher._hypervisor_port = 1000
    sidecar = tmp_path / "dynamips.json"
    sidecar.write_text("{}")
    runtime = {"vm_name": "fixture", "hypervisor_port": 1000, "work_dir": str(tmp_path)}

    with pytest.raises(DynamipsError, match="protocol timeout"):
        launcher.stop_node(runtime, confirm_stopped=True)

    assert sidecar.exists() and client.present and client.closed == 2
    client.fail = False
    launcher.stop_node(runtime, confirm_stopped=True)
    assert not sidecar.exists() and not client.present


# ---- Persistence regression tests ----------------------------------------


def test_destroy_vm_preserves_disk_artifacts() -> None:
    """``_destroy_vm_locked`` must use non-destructive ``vm stop`` +
    ``vm delete`` so NVRAM/disk0 files remain on disk for the next start.
    ``vm clean_delete`` must NOT appear (it would wipe saved config).
    """
    launcher = DynamipsLauncher()
    client = _RecordingClient()
    launcher._destroy_vm_locked(
        client,  # type: ignore[arg-type]
        "nv_lab_n1",
    )
    assert "vm stop nv_lab_n1" in client.commands
    assert "vm delete nv_lab_n1" in client.commands
    assert not any("clean_delete" in cmd for cmd in client.commands)


def test_destroy_vm_still_runs_nio_cleanup() -> None:
    """After ``vm stop`` + ``vm delete``, NIO entries must still be swept
    from the hypervisor's global registry so the next start can recreate
    them without a ``206 unable to create TAP NIO`` collision.
    """
    launcher = DynamipsLauncher()
    client = _RecordingClient()
    launcher._destroy_vm_locked(
        client,  # type: ignore[arg-type]
        "nv_lab_n1",
        nio_names=["nv_lab_n1_i0"],
    )
    assert "vm stop nv_lab_n1" in client.commands
    assert "vm delete nv_lab_n1" in client.commands
    assert "nio delete nv_lab_n1_i0" in client.commands


def test_purge_vm_uses_clean_delete_for_calibration() -> None:
    """Calibration and partial-start throwaway VMs must be fully purged
    via ``vm clean_delete`` — we intentionally want dynamips to also
    unlink their on-disk artifacts so stale nvram/disk0 files don't
    interfere with future calibration runs or retry attempts.
    """
    launcher = DynamipsLauncher()
    client = _RecordingClient()
    launcher._purge_vm_locked(
        client,  # type: ignore[arg-type]
        "calibrate_x",
    )
    assert "vm clean_delete calibrate_x" in client.commands


# ---- Slot inventory + interface→slot/port mapping tests ------------------


def test_build_slot_inventory_applies_c3725_default_for_slot0() -> None:
    """When slot 0 is not set in the template, the platform default
    (GT96100-FE for c3725) is used automatically.
    """
    template = {"platform": "c3725", "slot1": "NM-16ESW"}
    inventory = DynamipsLauncher._build_slot_inventory("c3725", template)
    assert inventory == [(0, "GT96100-FE"), (1, "NM-16ESW")]


def test_launcher_preserves_legacy_prebound_motherboard_slot_order():
    extras = {"slot0": "NM-1FE-TX", "slot3": "PA-GE"}
    assert DynamipsLauncher._build_slot_inventory("c3725", extras, legacy=True) == [(0, "NM-1FE-TX")]


def test_build_slot_inventory_skips_empty_slots() -> None:
    """Blank slot values are omitted from the inventory — they have no ports
    and no binding should be emitted for them.
    """
    template = {"slot0": "GT96100-FE", "slot1": "", "slot2": "NM-1FE-TX"}
    inventory = DynamipsLauncher._build_slot_inventory("c3725", template)
    assert inventory == [(0, "GT96100-FE"), (2, "NM-1FE-TX")]


def test_c7200_explicitly_empty_slot_zero_stays_empty() -> None:
    from app.services.template_service import DYNAMIPS_NO_IO_CARD

    assert DynamipsLauncher._build_slot_inventory("c7200", {}) == [(0, "C7200-IO-FE")]
    assert DynamipsLauncher._build_slot_inventory("c7200", {"slot0": ""}) == [(0, "C7200-IO-FE")]
    assert DynamipsLauncher._build_slot_inventory("c7200", {"slot0": DYNAMIPS_NO_IO_CARD}) == []


@pytest.mark.parametrize(
    ("platform", "slot", "adapter"),
    [("c7200", "slot1", "NM-1FE-TX"), ("c3725", "slot1", "GT96100-FE"), ("c7200", "slot0", "PA-GE")],
)
def test_dynamips_inventory_rejects_adapter_in_wrong_slot(platform, slot, adapter) -> None:
    with pytest.raises(DynamipsError, match=adapter):
        DynamipsLauncher._build_slot_inventory(platform, {slot: adapter})


def test_dynamips_catalog_exposes_only_valid_slot_options() -> None:
    from app.services.template_service import DYNAMIPS_NO_IO_CARD, _dynamips_extras_schema

    c3725 = {field["key"]: field for field in _dynamips_extras_schema("c3725")}
    c7200 = {field["key"]: field for field in _dynamips_extras_schema("c7200")}

    assert {option["value"] for option in c3725["slot0"]["options"]} == {"GT96100-FE"}
    assert "GT96100-FE" not in {option["value"] for option in c3725["slot1"]["options"]}
    assert {option["value"] for option in c7200["slot0"]["options"]} == {
        DYNAMIPS_NO_IO_CARD, "C7200-IO-FE", "C7200-IO-2FE", "C7200-IO-GE-E",
    }
    assert "C7200-IO-FE" not in {option["value"] for option in c7200["slot6"]["options"]}


def test_build_slot_inventory_raises_on_unknown_pa() -> None:
    """An unrecognised port-adapter name must raise DynamipsError so the
    operator sees a clear message instead of a silent mapping error later.
    """
    template = {"slot0": "FAKE-PA"}
    with pytest.raises(DynamipsError, match="FAKE-PA"):
        DynamipsLauncher._build_slot_inventory("c3725", template)


def test_resolve_slot_port_walks_inventory_cumulatively() -> None:
    """Flat interface indices are mapped across slot boundaries correctly.
    GT96100-FE has 2 ports, NM-16ESW has 16 — total 18.
    """
    inventory = [(0, "GT96100-FE"), (1, "NM-16ESW")]
    assert DynamipsLauncher._resolve_slot_port(inventory, 0) == (0, 0)
    assert DynamipsLauncher._resolve_slot_port(inventory, 1) == (0, 1)
    assert DynamipsLauncher._resolve_slot_port(inventory, 2) == (1, 0)
    assert DynamipsLauncher._resolve_slot_port(inventory, 17) == (1, 15)
    with pytest.raises(DynamipsError, match="18"):
        DynamipsLauncher._resolve_slot_port(inventory, 18)


def test_resolve_slot_port_skips_empty_slot_ranges() -> None:
    """When slot 1 is absent (inventory skips it), interface index 2 must
    map to slot 2 port 0 — the gap in slot numbering has no ports.
    """
    inventory = [(0, "GT96100-FE"), (2, "NM-1FE-TX")]
    assert DynamipsLauncher._resolve_slot_port(inventory, 2) == (2, 0)
