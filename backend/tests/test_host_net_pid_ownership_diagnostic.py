"""US-201 — pid-ownership diagnostic & registry tests.

Covers:
  * Missing ``pids.json`` -> deny all pid-taking verbs.
  * Corrupted JSON in ``pids.json`` -> deny all + diagnostic on stderr.
  * Pid in registry but cgroup/comm fingerprint does not match -> deny.
  * Pid in registry AND cgroup matches docker (v1, v2-cgroupfs, v2-systemd,
    crio) -> allow.
  * Pid in registry AND comm matches ``qemu-system-*`` -> allow.
  * Diagnostic on failure includes /proc/<pid>/comm (truncated to 80 chars),
    first 5 lines of /proc/<pid>/cgroup, and the expected-fingerprints
    summary line.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
HELPER_PATH = REPO_ROOT / "deploy" / "nova-ve-net.py"


@pytest.fixture
def helper(monkeypatch, tmp_path):
    spec = importlib.util.spec_from_file_location("nova_ve_net_helper_pid", HELPER_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["nova_ve_net_helper_pid"] = mod
    spec.loader.exec_module(mod)

    pids_path = tmp_path / "pids.json"
    proc_root = tmp_path / "proc"
    proc_root.mkdir()
    monkeypatch.setattr(mod, "PIDS_JSON_PATH", pids_path)
    monkeypatch.setattr(mod, "PROC_ROOT", proc_root)

    calls: list[list[str]] = []
    monkeypatch.setattr(mod, "_run", lambda argv: calls.append(list(argv)) or 0)

    mod._test_calls = calls  # type: ignore[attr-defined]
    mod._test_pids_path = pids_path  # type: ignore[attr-defined]
    mod._test_proc_root = proc_root  # type: ignore[attr-defined]
    return mod


def _seed_proc(helper, pid: int, cgroup: str, comm: str) -> None:
    pid_dir: Path = helper._test_proc_root / str(pid)
    pid_dir.mkdir(parents=True, exist_ok=True)
    (pid_dir / "cgroup").write_text(cgroup)
    (pid_dir / "comm").write_text(comm)


def _seed_registry(helper, pid: int, *, kind: str = "docker") -> None:
    helper._test_pids_path.write_text(
        json.dumps(
            [
                {
                    "pid": pid,
                    "kind": kind,
                    "lab_id": "lab-test",
                    "node_id": "node-test",
                    "started_at": 0,
                    "generation": 1,
                }
            ]
        )
    )


# ---------------------------------------------------------------------------
# Registry-file failure modes (deny all)
# ---------------------------------------------------------------------------


def test_missing_pids_json_denies_all(helper, capsys):
    # No registry written.
    _seed_proc(helper, 4242, "0::/system.slice/docker-abc.scope\n", "docker\n")
    rc = helper.main(["link-netns", "nve0000d1i1p", "4242"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "pid ownership check FAILED" in err
    assert not helper._test_calls


def test_corrupted_pids_json_denies_all(helper, capsys):
    helper._test_pids_path.write_text("{not valid json")
    _seed_proc(helper, 4242, "0::/system.slice/docker-abc.scope\n", "docker\n")
    rc = helper.main(["link-netns", "nve0000d1i1p", "4242"])
    assert rc == 2
    err = capsys.readouterr().err
    # Two stderr emissions: the corrupted-json warning AND the standard
    # ownership-failure diagnostic.
    assert "pids.json corrupted" in err
    assert "pid ownership check FAILED" in err
    assert not helper._test_calls


def test_non_list_pids_json_denies_all(helper, capsys):
    helper._test_pids_path.write_text(json.dumps({"pid": 4242}))
    _seed_proc(helper, 4242, "0::/system.slice/docker-abc.scope\n", "docker\n")
    rc = helper.main(["link-netns", "nve0000d1i1p", "4242"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "pids.json malformed" in err
    assert not helper._test_calls


# ---------------------------------------------------------------------------
# Cgroup fingerprint matrix (defense-in-depth secondary check)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "cgroup",
    [
        # cgroup v1 cgroupfs (docker)
        "11:devices:/docker/abcdef0123456789abcdef0123456789\n",
        # cgroup v2 cgroupfs (docker)
        "0::/docker/abcdef0123456789abcdef0123456789\n",
        # cgroup v2 systemd (docker — Ubuntu 26.04 default)
        "0::/system.slice/docker-abcdef0123456789abcdef.scope\n",
        # cgroup v2 systemd (containerd)
        "0::/system.slice/containerd-abcdef0123456789abcdef.scope\n",
        # cgroup v1 (crio) — slash variant
        "11:devices:/crio/abcdef0123456789abcdef\n",
        # cgroup v1 (crio) — hyphen variant
        "11:devices:/crio-abcdef0123456789abcdef.scope\n",
    ],
)
def test_authorized_pid_with_matching_cgroup_passes(helper, cgroup):
    _seed_registry(helper, 4242)
    _seed_proc(helper, 4242, cgroup, "anything\n")
    rc = helper.main(["link-netns", "nve0000d1i1p", "4242"])
    assert rc == 0
    assert helper._test_calls


def test_qemu_pid_via_comm_matches(helper):
    _seed_registry(helper, 5555, kind="qemu")
    # cgroup is unrelated; QEMU runs under user.slice on most distros.
    _seed_proc(helper, 5555, "0::/user.slice/user-1000.slice\n", "qemu-system-x86_64\n")
    rc = helper.main(["link-netns", "nve0000d1i1p", "5555"])
    assert rc == 0
    assert helper._test_calls


def test_registered_pid_but_no_cgroup_match_denied(helper, capsys):
    # Pid is in registry (e.g. recycled after process restart) but cgroup
    # fingerprint indicates a non-container, non-qemu process.  Deny.
    _seed_registry(helper, 6000)
    _seed_proc(helper, 6000, "0::/user.slice/user-1000.slice/session-3.scope\n", "bash\n")
    rc = helper.main(["link-netns", "nve0000d1i1p", "6000"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "pid ownership check FAILED for pid=6000" in err
    assert not helper._test_calls


# ---------------------------------------------------------------------------
# Diagnostic format (Critic iter-2 Major #1 fix)
# ---------------------------------------------------------------------------


def test_diagnostic_block_for_unregistered_pid(helper, capsys):
    _seed_proc(
        helper,
        7777,
        "12:cpuset:/\n11:hugetlb:/\n10:cpu,cpuacct:/\n9:pids:/\n8:rdma:/\n7:devices:/\n",
        "some-binary-name\n",
    )
    rc = helper.main(["link-netns", "nve0000d1i1p", "7777"])
    assert rc == 2
    err = capsys.readouterr().err

    # Required diagnostic lines per US-201.
    assert "pid ownership check FAILED for pid=7777" in err
    assert "/proc/7777/comm: some-binary-name" in err
    assert "/proc/7777/cgroup (first 5 lines):" in err
    # Should include exactly the first 5 lines, not all 6.
    assert "12:cpuset:/" in err
    assert "11:hugetlb:/" in err
    assert "10:cpu,cpuacct:/" in err
    assert "9:pids:/" in err
    assert "8:rdma:/" in err
    assert "7:devices:/" not in err  # 6th line MUST be truncated
    assert "expected one of:" in err
    assert "docker[/-]<hex>" in err
    assert "docker-<hex>.scope" in err
    assert "containerd-<hex>.scope" in err
    assert "crio[/-]<hex>" in err
    assert "qemu-system-*" in err


def test_diagnostic_truncates_long_comm(helper, capsys):
    long_comm = "x" * 200
    _seed_proc(helper, 8888, "0::/foo\n", long_comm + "\n")
    rc = helper.main(["link-netns", "nve0000d1i1p", "8888"])
    assert rc == 2
    err = capsys.readouterr().err
    # Find the comm diagnostic line and verify ≤80 chars after the prefix.
    line = next(
        line for line in err.splitlines() if line.startswith(f"  /proc/8888/comm: ")
    )
    suffix = line[len(f"  /proc/8888/comm: "):]
    assert len(suffix) <= 80


def test_diagnostic_when_proc_missing(helper, capsys):
    # No /proc/<pid> directory exists; helper must still emit a diagnostic
    # rather than crash.
    rc = helper.main(["link-netns", "nve0000d1i1p", "9999"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "pid ownership check FAILED for pid=9999" in err
    # /proc/<pid>/comm: <empty>
    assert "/proc/9999/comm:" in err
