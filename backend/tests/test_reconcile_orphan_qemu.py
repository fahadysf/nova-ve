"""Issue #225 — startup reconciliation for orphan QEMU processes."""

import json
from types import SimpleNamespace

import pytest

from app.services.node_runtime_service import NodeRuntimeService


@pytest.fixture()
def reconcile_settings(tmp_path):
    labs_dir = tmp_path / "labs"
    tmp_dir = tmp_path / "tmp"
    labs_dir.mkdir()
    tmp_dir.mkdir()
    return SimpleNamespace(LABS_DIR=labs_dir, TMP_DIR=tmp_dir)


@pytest.fixture()
def patched_reconcile_settings(monkeypatch, reconcile_settings):
    monkeypatch.setattr(
        "app.services.node_runtime_service.get_settings",
        lambda: reconcile_settings,
    )
    NodeRuntimeService.reset_registry()
    yield reconcile_settings
    NodeRuntimeService.reset_registry()


def _write_lab(labs_dir, *, filename, lab_id, node_id, node_name):
    """Drop a minimal v2 lab with a single QEMU node onto disk."""
    payload = {
        "schema": 2,
        "id": lab_id,
        "meta": {"name": filename.removesuffix(".json")},
        "viewport": {"x": 0, "y": 0, "zoom": 1.0},
        "nodes": {
            str(node_id): {
                "id": node_id,
                "name": node_name,
                "type": "qemu",
                "template": "vyos",
                "image": "vyos-rolling",
                "cpu": 2,
                "ram": 1536,
                "ethernet": 4,
                "console": "telnet",
                "left": 100,
                "top": 100,
                "interfaces": [],
            }
        },
        "networks": {},
        "links": [],
        "defaults": {"link_style": "orthogonal"},
    }
    (labs_dir / filename).write_text(json.dumps(payload))


def _fake_proc(*, pid, cmdline, create_time=1000.0, name="qemu-system-x86_64"):
    return SimpleNamespace(
        info={
            "pid": pid,
            "name": name,
            "cmdline": cmdline,
            "create_time": create_time,
        }
    )


def test_reconcile_adopts_orphan_qemu_into_registry(monkeypatch, patched_reconcile_settings):
    settings = patched_reconcile_settings
    _write_lab(
        settings.LABS_DIR,
        filename="rename-probe.json",
        lab_id="renameprobe",
        node_id=1,
        node_name="upstream-isp-net",
    )

    qemu_cmd = [
        "/usr/bin/qemu-system-x86_64",
        "-display", "none",
        "-machine", "type=q35,accel=tcg",
        "-smp", "2",
        "-m", "1536",
        "-name", "upstream-isp-net",
        "-uuid", "renameprobe-1",
        "-drive", "file=/var/lib/nova-ve/tmp/renameprobe/1/virtioa.qcow2,if=virtio,cache=writeback,format=qcow2",
        "-serial", "telnet::32100,server,nowait",
        "-qmp", "unix:/var/lib/nova-ve/tmp/renameprobe/1/qmp.sock,server,nowait",
    ]
    fake_procs = [_fake_proc(pid=4242, cmdline=qemu_cmd)]
    monkeypatch.setattr(
        "app.services.node_runtime_service.psutil.process_iter",
        lambda attrs=None: iter(fake_procs),
    )
    registered_pids: list[tuple[int, str, str, int]] = []
    monkeypatch.setattr(
        "app.services.node_runtime_service.runtime_pids.register",
        lambda pid, kind, lab_id, node_id: registered_pids.append((pid, kind, lab_id, node_id)),
    )

    stats = NodeRuntimeService.reconcile_orphan_qemu()

    assert stats == {"adopted": 1, "scanned": 1}
    runtime = NodeRuntimeService._registry["renameprobe:1"]
    assert runtime["pid"] == 4242
    assert runtime["lab_id"] == "renameprobe"
    assert runtime["node_id"] == 1
    assert runtime["kind"] == "qemu"
    assert runtime["console"] == "telnet"
    assert runtime["console_port"] == 32100
    assert runtime["qmp_socket"] == "/var/lib/nova-ve/tmp/renameprobe/1/qmp.sock"
    assert runtime["overlay_path"].endswith("virtioa.qcow2")
    assert runtime["machine"] == "q35"
    assert runtime["adopted"] is True
    state_file = settings.TMP_DIR / "node-runtime" / "renameprobe-1.json"
    assert state_file.exists()
    persisted = json.loads(state_file.read_text())
    assert persisted["pid"] == 4242
    assert registered_pids == [(4242, "qemu", "renameprobe", 1)]


def test_reconcile_skips_pids_already_registered(monkeypatch, patched_reconcile_settings):
    settings = patched_reconcile_settings
    _write_lab(
        settings.LABS_DIR,
        filename="already.json",
        lab_id="alreadyreg",
        node_id=7,
        node_name="rtr",
    )
    # Seed the registry as if the previous backend session left a record.
    NodeRuntimeService._registry["alreadyreg:7"] = {
        "lab_id": "alreadyreg",
        "node_id": 7,
        "kind": "qemu",
        "pid": 9999,
    }
    NodeRuntimeService._loaded = True

    qemu_cmd = [
        "qemu-system-x86_64",
        "-uuid", "alreadyreg-7",
        "-serial", "telnet::32100,server,nowait",
    ]
    fake_procs = [_fake_proc(pid=4242, cmdline=qemu_cmd)]
    monkeypatch.setattr(
        "app.services.node_runtime_service.psutil.process_iter",
        lambda attrs=None: iter(fake_procs),
    )
    monkeypatch.setattr(
        "app.services.node_runtime_service.runtime_pids.register",
        lambda *_args, **_kw: pytest.fail("registry should not be touched for already-registered nodes"),
    )

    stats = NodeRuntimeService.reconcile_orphan_qemu()

    assert stats == {"adopted": 0, "scanned": 1}
    # Existing entry untouched.
    assert NodeRuntimeService._registry["alreadyreg:7"]["pid"] == 9999


def test_reconcile_ignores_unrelated_processes(monkeypatch, patched_reconcile_settings):
    settings = patched_reconcile_settings
    _write_lab(
        settings.LABS_DIR,
        filename="known.json",
        lab_id="knownlab",
        node_id=1,
        node_name="rtr",
    )

    fake_procs = [
        # Not qemu.
        _fake_proc(pid=100, cmdline=["/usr/sbin/sshd", "-D"], name="sshd"),
        # QEMU but for an unknown lab.
        _fake_proc(pid=101, cmdline=["qemu-system-x86_64", "-uuid", "otherlab-3"]),
        # QEMU for a known lab but a node that does not exist on disk.
        _fake_proc(pid=102, cmdline=["qemu-system-x86_64", "-uuid", "knownlab-99"]),
        # QEMU without a -uuid arg.
        _fake_proc(pid=103, cmdline=["qemu-system-aarch64", "-display", "none"]),
    ]
    monkeypatch.setattr(
        "app.services.node_runtime_service.psutil.process_iter",
        lambda attrs=None: iter(fake_procs),
    )
    monkeypatch.setattr(
        "app.services.node_runtime_service.runtime_pids.register",
        lambda *_args, **_kw: pytest.fail("no registration expected"),
    )

    stats = NodeRuntimeService.reconcile_orphan_qemu()

    assert stats == {"adopted": 0, "scanned": 4}
    assert "knownlab:1" not in NodeRuntimeService._registry


def test_reconcile_handles_hyphenated_lab_ids(monkeypatch, patched_reconcile_settings):
    """Lab IDs may contain hyphens; the uuid form is ``<lab>-<node_id>`` and
    must be split from the right."""
    settings = patched_reconcile_settings
    _write_lab(
        settings.LABS_DIR,
        filename="multi.json",
        lab_id="multi-word-lab",
        node_id=12,
        node_name="rtr",
    )

    qemu_cmd = [
        "qemu-system-x86_64",
        "-uuid", "multi-word-lab-12",
        "-vnc", ":4",
    ]
    fake_procs = [_fake_proc(pid=5555, cmdline=qemu_cmd)]
    monkeypatch.setattr(
        "app.services.node_runtime_service.psutil.process_iter",
        lambda attrs=None: iter(fake_procs),
    )
    monkeypatch.setattr(
        "app.services.node_runtime_service.runtime_pids.register",
        lambda *_args, **_kw: None,
    )

    stats = NodeRuntimeService.reconcile_orphan_qemu()

    assert stats == {"adopted": 1, "scanned": 1}
    runtime = NodeRuntimeService._registry["multi-word-lab:12"]
    assert runtime["pid"] == 5555
    assert runtime["console_port"] == 5904  # 5900 + display index 4
