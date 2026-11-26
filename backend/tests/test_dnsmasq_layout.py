# SPDX-License-Identifier: Apache-2.0

"""#334 — DHCP servers left on the layout used before the move to the /run tree.

The startup sweep reads the lab files and the same ``/proc`` rule as the info
panel. ``/proc`` is a scratch directory here, and ``host_net._run`` — the only
place the backend spawns the privileged helper — is a recorder that moves the
fake server the way the real helper does.
"""

from __future__ import annotations

import inspect
import json
import logging
import shutil
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import main
from app.services import dnsmasq_layout as layout
from app.services import host_net
from app.services import network_info_service as info

LAB = "lab-334"


@pytest.fixture(autouse=True)
def refuse_host_spawn(monkeypatch):
    def refuse(*_args, **_kwargs):
        raise AssertionError("host operations must be stubbed in this test")

    monkeypatch.setattr(host_net, "_run", refuse)


@pytest.fixture()
def labs_dir(tmp_path: Path) -> Path:
    d = tmp_path / "labs"
    d.mkdir()
    return d


@pytest.fixture()
def settings(monkeypatch, labs_dir):
    """Lab files under ``labs_dir``; its parent plays the data directory."""
    monkeypatch.setattr(
        "app.services.cloud_inventory_service.get_settings",
        lambda: SimpleNamespace(LABS_DIR=labs_dir),
    )
    instance_dir = labs_dir.parent / "instance"
    instance_dir.mkdir()
    (instance_dir / "instance_id").write_text("instance-334")
    monkeypatch.setenv("NOVA_VE_INSTANCE_DIR", str(instance_dir))
    return labs_dir


class Host:
    """Records the helper argv the backend would spawn and fakes its effect."""

    def __init__(self, proc: Path, current: Path, legacy: Path):
        self.proc = proc
        self.roots = {"current": current, "legacy": legacy}
        self.calls: list[tuple[str, ...]] = []
        self.pids: dict[str, list[int]] = {}
        # False on a host whose helper still writes the old layout.
        self.new_helper = True
        self.fail: set[str] = set()

    def process(self, pid: int, argv: list[str]) -> None:
        entry = self.proc / str(pid)
        entry.mkdir(parents=True)
        (entry / "cmdline").write_bytes(b"\0".join(arg.encode() for arg in argv) + b"\0")

    def seed(self, bridge: str, kind: str, pid: int) -> None:
        """A server for ``bridge`` that reads its config from the ``kind`` layout."""
        self.pids.setdefault(bridge, []).append(pid)
        self.process(pid, ["dnsmasq", f"--conf-file={self.roots[kind] / bridge / 'dnsmasq.conf'}"])

    def _stop(self, bridge: str) -> None:
        for pid in self.pids.pop(bridge, []):
            shutil.rmtree(self.proc / str(pid), ignore_errors=True)

    def run(self, argv):
        verb, args = argv[3], tuple(argv[4:])
        self.calls.append((verb, *args))
        if verb == "dnsmasq-stop":
            self._stop(args[0])
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        if args[0] in self.fail:
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="boom")
        self._stop(args[0])
        self.seed(args[0], "current" if self.new_helper else "legacy", 5000 + len(self.calls))
        return subprocess.CompletedProcess(argv, 0, stdout=f"{self.pids[args[0]][-1]}\n", stderr="")


@pytest.fixture()
def host(monkeypatch, tmp_path, settings):
    def no_spawn(*_args, **_kwargs):
        raise AssertionError("a process was spawned behind host_net._run")

    monkeypatch.setattr(subprocess, "run", no_spawn)
    proc = tmp_path / "proc"
    proc.mkdir()
    current = tmp_path / "run" / "dnsmasq"
    monkeypatch.setattr(info, "DNSMASQ_ROOT", current, raising=True)
    monkeypatch.setattr(info, "PROC_ROOT", proc, raising=True)
    fake = Host(proc, current, settings.parent.resolve() / "nat-cloud")
    monkeypatch.setattr(host_net, "_run", fake.run)
    return fake


def _nat(network_id: int, **config) -> dict:
    return {
        "id": network_id,
        "name": f"nat{network_id}",
        "type": "nat_cloud",
        "config": {"gateway": "10.255.0.1", "dhcp_start": "10.255.0.100", "dhcp_end": "10.255.0.254", **config},
        "runtime": {"bridge_name": host_net.bridge_name(LAB, network_id)},
    }


def _bridge(network_id: int, **config) -> dict:
    return {
        "id": network_id,
        "name": f"lan{network_id}",
        "type": "linux_bridge",
        "config": {
            "cidr": "10.40.1.0/24",
            "dhcp": True,
            "dhcp_server": "10.40.1.254",
            "dhcp_start": "10.40.1.128",
            "dhcp_end": "10.40.1.253",
            **config,
        },
        "runtime": {"bridge_name": host_net.bridge_name(LAB, network_id)},
    }


def _lab(labs_dir: Path, name: str, lab_id: str, networks: dict) -> None:
    (labs_dir / name).write_text(
        json.dumps(
            {
                "schema": 2,
                "id": lab_id,
                "meta": {"name": name.removesuffix(".json")},
                "nodes": {},
                "networks": networks,
                "links": [],
            }
        )
    )


# ---------------------------------------------------------------------------
# Reader
# ---------------------------------------------------------------------------


def test_layout_is_read_from_the_conf_the_server_was_started_with(settings, host):
    """#334: the same rule as the info panel, for both layouts at once."""
    nat = host_net.bridge_name(LAB, 1)
    assert info.dhcp_server_layout(nat) is None
    assert info.running_dnsmasq_layouts() == {}

    host.seed(nat, "current", 4101)
    assert info.dhcp_server_layout(nat) == "current"
    # A second server on the old layout wins: it has to go whatever else runs.
    host.seed(nat, "legacy", 4102)
    assert info.dhcp_server_layout(nat) == "legacy"
    assert info.running_dnsmasq_layouts() == {nat: {"current", "legacy"}}

    # Not a lab bridge's configuration file, or not a dnsmasq.
    conf = host.roots["legacy"] / nat / "dnsmasq.conf"
    host.process(4103, ["dnsmasq", f"--conf-file={host.roots['legacy'] / nat / 'other.conf'}"])
    host.process(4104, ["python3", "dnsmasq", f"--conf-file={conf}"])
    assert info.running_dnsmasq_layouts() == {nat: {"current", "legacy"}}
    assert info.dhcp_server_layout("br-eth0") is None
    assert info.dhcp_server_layout(None) is None


# The gate uses a scratch process tree, never the live host.

@pytest.fixture
def proc(monkeypatch, tmp_path):
    root = tmp_path / "proc"
    root.mkdir()
    monkeypatch.setattr(layout, "PROC_ROOT", root)
    monkeypatch.setattr(layout.host_net, "sandbox_mode", lambda: False)
    monkeypatch.setattr(layout.host_net, "_invoke_helper", lambda *args: SimpleNamespace(stdout="root-owned-dnsmasq-v1\n"))
    return root


def process(proc, pid, *argv):
    entry = proc / str(pid)
    entry.mkdir()
    (entry / "cmdline").write_bytes(b"\0".join(arg.encode() for arg in argv) + b"\0")


def test_gate_accepts_current_and_unrelated_servers(proc):
    process(proc, 101, "dnsmasq", "--conf-file=/run/nova-ve/dnsmasq/novec0den1/dnsmasq.conf")
    process(proc, 102, "dnsmasq", "--conf-file=/other/dnsmasq.conf")
    process(proc, 103, "python", "--conf-file=/data/nat-cloud/novec0den1/dnsmasq.conf")
    layout.require_secure_dnsmasq_layout()


@pytest.mark.parametrize("conf", [
    "/var/lib/nova-ve/nat-cloud/novec0den1/dnsmasq.conf",
    "/another/data/nat-cloud/novec0den1/dnsmasq.conf",
    "nat-cloud/novec0den1/dnsmasq.conf",
])
def test_gate_rejects_legacy_even_if_current_also_runs(proc, conf):
    process(proc, 101, "dnsmasq", "--conf-file=/run/nova-ve/dnsmasq/novec0den1/dnsmasq.conf")
    process(proc, 102, "/usr/sbin/dnsmasq", f"--conf-file={conf}")
    with pytest.raises(RuntimeError, match="Legacy DHCP servers"):
        layout.require_secure_dnsmasq_layout()


def test_gate_does_not_resolve_service_owned_legacy_paths(proc, tmp_path):
    old = tmp_path / "nat-cloud"
    old.symlink_to(tmp_path / "elsewhere")
    process(proc, 101, "dnsmasq", f"--conf-file={old}/novec0den1/dnsmasq.conf")
    with pytest.raises(RuntimeError, match="Legacy DHCP servers"):
        layout.require_secure_dnsmasq_layout()


def test_gate_rejects_incomplete_process_scan(proc, monkeypatch):
    process(proc, 101, "dnsmasq")
    real_read = Path.read_bytes

    def denied(path):
        if path == proc / "101" / "cmdline":
            raise PermissionError(13, "denied")
        return real_read(path)

    monkeypatch.setattr(Path, "read_bytes", denied)
    with pytest.raises(RuntimeError, match="Cannot inspect all processes"):
        layout.require_secure_dnsmasq_layout()


def test_gate_rejects_unreadable_proc_root(proc, monkeypatch, tmp_path):
    monkeypatch.setattr(layout, "PROC_ROOT", tmp_path / "missing")
    monkeypatch.setattr(layout.host_net, "sandbox_mode", lambda: False)
    with pytest.raises(RuntimeError, match="Cannot inspect all processes"):
        layout.require_secure_dnsmasq_layout()


def test_gate_allows_a_process_that_disappeared(proc):
    (proc / "101").mkdir()
    layout.require_secure_dnsmasq_layout()


def test_gate_skips_sandbox(monkeypatch, tmp_path):
    monkeypatch.setattr(layout, "PROC_ROOT", tmp_path / "missing")
    monkeypatch.setattr(layout.host_net, "sandbox_mode", lambda: True)
    layout.require_secure_dnsmasq_layout()


@pytest.mark.parametrize("result", ["", "legacy", "root-owned-dnsmasq-v2"])
def test_gate_refuses_old_helper_with_no_running_server(proc, monkeypatch, result):
    monkeypatch.setattr(layout.host_net, "_invoke_helper", lambda *args: SimpleNamespace(stdout=result))
    with pytest.raises(RuntimeError, match="installed network helper"):
        layout.require_secure_dnsmasq_layout()


def test_gate_refuses_helper_failure_with_no_running_server(proc, monkeypatch):
    def old_helper(*args):
        raise layout.host_net.HostNetValidationError("unknown verb")

    monkeypatch.setattr(layout.host_net, "_invoke_helper", old_helper)
    with pytest.raises(RuntimeError, match="installed network helper"):
        layout.require_secure_dnsmasq_layout()


def test_gate_precedes_startup_work_and_is_awaited():
    source = inspect.getsource(main.startup)
    gate = source.index("await asyncio.to_thread(require_secure_dnsmasq_layout)")
    assert gate < source.index("get_instance_id()")
    assert gate < source.index("start_heartbeat()")
    assert "create_task" not in source


@pytest.mark.asyncio
async def test_legacy_server_prevents_startup_before_other_work(proc, monkeypatch):
    process(proc, 101, "dnsmasq", "--conf-file=/data/nat-cloud/novec0den1/dnsmasq.conf")

    def unexpected():
        pytest.fail("startup continued past the legacy DHCP gate")

    monkeypatch.setattr(main, "get_instance_id", unexpected)
    with pytest.raises(RuntimeError, match="Legacy DHCP servers"):
        await main.startup()
