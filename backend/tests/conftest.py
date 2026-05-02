import pytest


def _stub_host_net_for_qemu_start(monkeypatch):
    """Issue #174: every non-uplink boot iface provisions a host TAP at
    start. This helper stubs the host_net surface used by the QEMU start
    path so unit tests run without a privileged helper or real
    ``instance_id`` file.

    Call this *only* from tests that don't already supply their own
    ``host_net`` patches (e.g. via ``_us302_helper_mock`` or
    ``_us203_helper_mock``); those tests record real call sequences and
    overwriting them here would mask their assertions.

    Issue #175 (S3): canonicalised here so test_node_runtime.py,
    test_node_extras.py, and test_qemu_pcie_root_ports.py share one
    definition. Stubs the seven ``host_net`` calls exercised by the QEMU
    start path: ``tap_name`` / ``tap_exists`` / ``tap_add`` /
    ``link_master`` / ``link_up`` / ``try_link_del`` / ``bridge_exists``.
    Tests that need to observe ``link_set_nomaster`` (e.g. the orphan-TAP
    cleanup test) layer their own monkeypatch on top after calling this
    helper — leaving it unstubbed here keeps the call observable.
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
