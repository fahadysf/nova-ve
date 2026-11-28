import itertools
import os

import pytest


os.environ.setdefault(
    "DATABASE_URL", "postgresql+asyncpg://nova:test-password@localhost:5432/novadb"
)
os.environ.setdefault("SECRET_KEY", "test-secret-key-for-backend-suite-000000")

_STATE_DIR_IDS = itertools.count()


@pytest.fixture(autouse=True)
def _guard_dnsmasq_capability_probe(monkeypatch):
    """A startup test must stub the capability probe before it can spawn sudo."""
    from app.services import host_net

    invoke = host_net._invoke_helper

    def guarded(verb, *args):
        if verb == "dnsmasq-layout-version":
            raise AssertionError("dnsmasq capability probe must be stubbed in tests")
        return invoke(verb, *args)

    monkeypatch.setattr(host_net, "_invoke_helper", guarded)


def _unregistered_lab_db():
    """Model an imported lab with no registered owner."""
    from unittest.mock import AsyncMock, Mock

    result = Mock()
    result.scalar_one_or_none.return_value = None
    return AsyncMock(execute=AsyncMock(return_value=result))


def _stub_host_net_for_qemu_start(monkeypatch):
    """Issue #174 / #175 (S3): stub the host_net surface used by the QEMU
    start path so tests run without a privileged helper or real
    ``instance_id`` file. Canonicalised here so test_node_runtime.py,
    test_node_extras.py, and test_qemu_pcie_root_ports.py share one
    definition.

    Call this *only* from tests that don't already supply their own
    ``host_net`` patches (e.g. via ``_us302_helper_mock`` or
    ``_us203_helper_mock``); those tests record real call sequences and
    overwriting them here would mask their assertions.

    ``link_set_nomaster`` is intentionally left unstubbed so tests that
    need to observe the orphan-TAP cleanup branch can layer their own patch.
    """
    monkeypatch.setattr(
        "app.services.node_runtime_service.host_net.tap_name",
        lambda lab_id, node_id, iface: f"nve-test-d{node_id}i{iface}",
    )
    monkeypatch.setattr(
        "app.services.node_runtime_service.host_net.tap_exists", lambda name: False
    )
    monkeypatch.setattr(
        "app.services.node_runtime_service.host_net.tap_add", lambda name: None
    )
    monkeypatch.setattr(
        "app.services.node_runtime_service.host_net.link_master",
        lambda iface, bridge: None,
    )
    monkeypatch.setattr(
        "app.services.node_runtime_service.host_net.link_up", lambda iface: None
    )
    monkeypatch.setattr(
        "app.services.node_runtime_service.host_net.try_link_del", lambda name: None
    )
    monkeypatch.setattr(
        "app.services.node_runtime_service.host_net.bridge_exists", lambda name: True
    )


def _shorten_runtime_mutex_wait(monkeypatch, timeout: float = 0.2) -> None:
    """Make a contended ``runtime_mutex`` acquire give up after ``timeout``.

    The acquire methods bind ``DEFAULT_ACQUIRE_TIMEOUT_S`` as a keyword
    default when the module is imported, so patching the constant changes
    nothing and a contention test waits the full 2s.
    """
    from app.services.runtime_mutex import RuntimeMutexRegistry

    for name in ("acquire_sync", "acquire", "acquire_node_sync"):
        defaults = getattr(RuntimeMutexRegistry, name).__wrapped__.__kwdefaults__
        monkeypatch.setitem(defaults, "timeout", timeout)


@pytest.fixture(scope="session")
def _default_state_root(tmp_path_factory):
    """One parent for every test's default runtime-state directory.

    ``tmp_path_factory.mktemp`` scans its whole base directory on each call,
    so two calls per test made suite setup quadratic. Tests get a numbered
    child of this single directory instead.
    """
    return tmp_path_factory.mktemp("default-state")


@pytest.fixture(autouse=True)
def _default_state_dir(_default_state_root):
    state_dir = _default_state_root / str(next(_STATE_DIR_IDS))
    state_dir.mkdir()
    return state_dir


@pytest.fixture(autouse=True)
def _redirect_pids_registry(_default_state_dir, monkeypatch):
    """Redirect ``runtime_pids`` to a tmp path so registration during node
    start paths (US-201/US-203) cannot escape into ``/var/lib/nova-ve``.

    Tests that need to inspect the registry directly should override
    ``NOVA_VE_PIDS_JSON`` themselves with a path under their own tmp_path.
    """
    pids_dir = _default_state_dir / "pids"
    pids_dir.mkdir()
    monkeypatch.setenv("NOVA_VE_PIDS_JSON", str(pids_dir / "pids.json"))


@pytest.fixture(autouse=True)
def _redirect_bridge_fingerprints(_default_state_dir, monkeypatch):
    """Redirect bridge ownership fingerprints away from ``/var/lib/nova-ve``."""
    fingerprint_dir = _default_state_dir / "bridge-fingerprints"
    fingerprint_dir.mkdir()
    monkeypatch.setenv("NOVA_VE_BRIDGE_FINGERPRINT_ROOT", str(fingerprint_dir))


@pytest.fixture(autouse=True)
def audit_events(monkeypatch):
    """Capture audit events in memory — the suite runs without a database (#297).

    Every audited action in every test lands in the returned list, so no
    test can reach for a real Postgres through the audit writer. Tests that
    care about the trail take ``audit_events`` and assert on it; the
    entries are ``audit_service.AuditEvent`` instances.
    """
    from app.services import audit_service

    events: list = []

    async def _capture(entries):
        events.extend(entries)

    monkeypatch.setattr(audit_service, "record_batch", _capture)
    return events
