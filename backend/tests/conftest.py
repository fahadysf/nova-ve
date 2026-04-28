import pytest


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
