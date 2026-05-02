import pytest


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


@pytest.fixture(autouse=True)
def _redirect_pids_registry(tmp_path_factory, monkeypatch):
    """Redirect ``runtime_pids`` to a tmp path so registration during node
    start paths (US-201/US-203) cannot escape into ``/var/lib/nova-ve``.

    Tests that need to inspect the registry directly should override
    ``NOVA_VE_PIDS_JSON`` themselves with a path under their own tmp_path.
    """
    pids_dir = tmp_path_factory.mktemp("pids-default")
    monkeypatch.setenv("NOVA_VE_PIDS_JSON", str(pids_dir / "pids.json"))
    yield


@pytest.fixture(autouse=True)
def _redirect_bridge_fingerprints(tmp_path_factory, monkeypatch):
    """Redirect bridge ownership fingerprints away from ``/var/lib/nova-ve``."""
    fingerprint_dir = tmp_path_factory.mktemp("bridge-fingerprints-default")
    monkeypatch.setenv("NOVA_VE_BRIDGE_FINGERPRINT_ROOT", str(fingerprint_dir))
    yield
