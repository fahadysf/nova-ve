import asyncio
import copy
import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.models.lab import LabMeta
from app.models.user import User
from app.routers import labs
from app.services.node_runtime_service import NodeRuntimeError, NodeRuntimeService


class _Result:
    def __init__(self, values):
        self.values = values

    def scalars(self):
        return self

    def all(self):
        return self.values

    def scalar_one_or_none(self):
        return self.values[0] if self.values else None


class _Db:
    def __init__(self, lab_rows, users):
        self.lab_rows = lab_rows
        self.users = users

    async def rollback(self):
        pass

    async def execute(self, statement):
        entity = statement.column_descriptions[0]["entity"]
        if entity is LabMeta:
            if statement.whereclause is None:
                return _Result(self.lab_rows)
            key = statement.whereclause.left.key
            value = statement.whereclause.right.value
            return _Result([row for row in self.lab_rows if getattr(row, key) == value])
        if entity is User:
            username = statement.whereclause.right.value
            return _Result([user for user in self.users if user.username == username])
        raise AssertionError(f"Unexpected quota query: {statement}")


@pytest.fixture()
def quota_env(tmp_path, monkeypatch):
    labs_dir = tmp_path / "labs"
    tmp_dir = tmp_path / "tmp"
    images_dir = tmp_path / "images"
    templates_dir = tmp_path / "templates"
    for directory in (labs_dir, tmp_dir, images_dir, templates_dir):
        directory.mkdir()
    settings = SimpleNamespace(
        LABS_DIR=labs_dir,
        TMP_DIR=tmp_dir,
        IMAGES_DIR=images_dir,
        TEMPLATES_DIR=templates_dir,
        USER_TEMPLATES_DIR=None,
        QEMU_BINARY="qemu-system-x86_64",
        QEMU_IMG_BINARY="qemu-img",
        DOCKER_HOST="unix:///mock-docker.sock",
    )
    for target in (
        "app.routers.labs.get_settings",
        "app.services.lab_service.get_settings",
        "app.services.node_runtime_service.get_settings",
        "app.services.template_service.get_settings",
    ):
        monkeypatch.setattr(target, lambda: settings)
    monkeypatch.setattr("app.services.node_runtime_service.host_net.sandbox_mode", lambda: False)

    lab_rows = []
    users = {}
    live_pids = set()
    attempts = []
    next_pid = 1000

    def is_alive(runtime):
        return runtime.get("pid") in live_pids

    def start_qemu(self, lab_data, node):
        nonlocal next_pid
        next_pid += 1
        attempts.append((lab_data["id"], node["id"]))
        live_pids.add(next_pid)
        return {
            "lab_id": lab_data["id"],
            "node_id": node["id"],
            "kind": "qemu",
            "pid": next_pid,
            "pid_create_time": 1.0,
            "command": [
                "qemu-system-x86_64",
                "-smp", str(node.get("cpu", 1)),
                "-m", str(node.get("ram", 1024)),
            ],
        }

    def unexpected_launch(self, lab_data, node):
        pytest.fail(f"Unexpected {node.get('type')} launcher invocation")

    monkeypatch.setattr(NodeRuntimeService, "_is_runtime_alive", staticmethod(is_alive))
    monkeypatch.setattr(NodeRuntimeService, "_start_qemu_node", start_qemu)
    monkeypatch.setattr(NodeRuntimeService, "_start_docker_node", unexpected_launch)
    monkeypatch.setattr(NodeRuntimeService, "_start_iol_node", unexpected_launch)
    monkeypatch.setattr(NodeRuntimeService, "_start_dynamips_node", unexpected_launch)
    monkeypatch.setattr(NodeRuntimeService, "_stop_qemu_runtime", lambda self, runtime, **kwargs: live_pids.discard(runtime.get("pid")))
    monkeypatch.setattr("app.services.node_runtime_service.runtime_pids.register", lambda *args: None)
    monkeypatch.setattr("app.services.node_runtime_service.runtime_pids.list_entries", lambda: [])
    monkeypatch.setattr(
        NodeRuntimeService, "_check_alive_sync",
        staticmethod(lambda runtime, *args: runtime.get("pid") in live_pids),
    )
    NodeRuntimeService.reset_registry()
    yield SimpleNamespace(
        settings=settings,
        lab_rows=lab_rows,
        users=users,
        live_pids=live_pids,
        attempts=attempts,
        monkeypatch=monkeypatch,
    )
    NodeRuntimeService.reset_registry()


def _user(username, *, role="user", cpu=-1, ram=-1):
    return SimpleNamespace(
        username=username,
        role=role,
        folder="/" if role == "admin" else f"Users/{username}",
        cpu=cpu,
        ram=ram,
        html5=True,
        pod=0,
    )


def _node(node_id=1, *, cpu=1, ram=512, node_type="qemu", extras=None):
    return {
        "id": node_id,
        "name": f"node-{node_id}",
        "type": node_type,
        "template": "test-template",
        "image": "test-image",
        "cpu": cpu,
        "ram": ram,
        "ethernet": 0,
        "interfaces": [],
        "extras": extras or {},
    }


def _add_lab(env, filename, lab_id, owner, node):
    env.settings.LABS_DIR.joinpath(filename).parent.mkdir(parents=True, exist_ok=True)
    data = {
        "schema": 2,
        "id": lab_id,
        "meta": {"name": Path(filename).stem},
        "nodes": {str(node["id"]): node},
        "networks": {},
        "links": [],
    }
    env.settings.LABS_DIR.joinpath(filename).write_text(json.dumps(data))
    env.lab_rows.append(SimpleNamespace(id=lab_id, filename=filename, owner=owner))
    return data


def _db(env):
    return _Db(env.lab_rows, list(env.users.values()))


async def _start(env, filename, user, node_id=1):
    return await labs.start_node(filename, node_id, current_user=user, db=_db(env))


@pytest.mark.asyncio
async def test_shared_lab_start_uses_owner_quota_across_nested_labs(quota_env):
    env = quota_env
    env.users.update({"owner": _user("owner", cpu=1), "starter": _user("starter", cpu=99)})
    _add_lab(env, "Shared/first.json", "lab-first", "owner", _node())
    _add_lab(env, "Shared/nested/second.json", "lab-second", "owner", _node())

    first = await _start(env, "Shared/first.json", env.users["starter"])
    second = await _start(env, "Shared/nested/second.json", env.users["starter"])

    assert first["code"] == 200
    assert second["code"] == 400
    assert "CPU" in second["message"]
    assert env.attempts == [("lab-first", 1)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("resource", "node", "limit"),
    [
        ("cpu", _node(cpu=2, ram=512), {"cpu": 2, "ram": 512}),
        ("ram", _node(cpu=1, ram=1024), {"cpu": -1, "ram": 1024}),
    ],
)
async def test_quota_allows_exact_boundary_and_rejects_the_next_node(quota_env, resource, node, limit):
    env = quota_env
    env.users["owner"] = _user("owner", **limit)
    _add_lab(env, "Shared/boundary.json", "lab-boundary", "owner", copy.deepcopy(node))
    _add_lab(env, "Shared/over.json", "lab-over", "owner", copy.deepcopy(node))

    assert (await _start(env, "Shared/boundary.json", env.users["owner"]))["code"] == 200
    response = await _start(env, "Shared/over.json", env.users["owner"])
    assert response["code"] == 400
    assert resource.upper() in response["message"]


@pytest.mark.asyncio
async def test_unlimited_quotas_still_snapshot_successful_start_resources(quota_env):
    env = quota_env
    env.users["owner"] = _user("owner", cpu=-1, ram=-1)
    _add_lab(env, "Shared/unlimited.json", "lab-unlimited", "owner", _node(cpu=2, ram=768))

    assert (await _start(env, "Shared/unlimited.json", env.users["owner"]))["code"] == 200
    runtime = NodeRuntimeService._registry["lab-unlimited:1"]
    assert (runtime["quota_cpu"], runtime["quota_ram"]) == (2, 768)
    saved = json.loads((env.settings.TMP_DIR / "node-runtime" / "lab-unlimited-1.json").read_text())
    assert (saved["quota_cpu"], saved["quota_ram"]) == (2, 768)


@pytest.mark.asyncio
async def test_idempotent_running_start_uses_original_snapshot_after_file_edit(quota_env):
    env = quota_env
    env.users["owner"] = _user("owner", cpu=1, ram=512)
    data = _add_lab(env, "Shared/original.json", "lab-original", "owner", _node(cpu=1, ram=512))
    _add_lab(env, "Shared/other.json", "lab-other", "owner", _node(cpu=1, ram=512))

    assert (await _start(env, "Shared/original.json", env.users["owner"]))["code"] == 200
    data["nodes"]["1"].update(cpu=99, ram=9999)
    assert (await _start(env, "Shared/original.json", env.users["owner"]))["code"] == 200
    env.monkeypatch.setattr(
        "app.services.node_runtime_service.read_canonical_lab",
        lambda *args: pytest.fail("A fully snapshotted runtime should not re-read lab JSON"),
    )
    assert (await _start(env, "Shared/other.json", env.users["owner"]))["code"] == 400
    assert len(env.attempts) == 1


@pytest.mark.asyncio
async def test_failed_launch_does_not_consume_quota_and_retry_succeeds(quota_env):
    env = quota_env
    env.users["owner"] = _user("owner", cpu=1, ram=512)
    _add_lab(env, "Shared/retry.json", "lab-retry", "owner", _node())
    calls = 0

    def fail_then_start(self, lab_data, node):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise NodeRuntimeError("mock launch failure")
        env.live_pids.add(2000)
        return {"lab_id": lab_data["id"], "node_id": node["id"], "kind": "qemu", "pid": 2000}

    env.monkeypatch.setattr(NodeRuntimeService, "_start_qemu_node", fail_then_start)
    assert (await _start(env, "Shared/retry.json", env.users["owner"]))["code"] == 400
    assert (await _start(env, "Shared/retry.json", env.users["owner"]))["code"] == 200
    assert calls == 2


@pytest.mark.asyncio
async def test_stop_by_another_authorized_user_releases_owner_usage(quota_env):
    env = quota_env
    env.users.update({
        "owner": _user("owner", cpu=1, ram=512),
        "starter": _user("starter"),
        "stopper": _user("stopper"),
    })
    _add_lab(env, "Shared/stoppable.json", "lab-stoppable", "owner", _node())
    _add_lab(env, "Shared/replacement.json", "lab-replacement", "owner", _node())

    assert (await _start(env, "Shared/stoppable.json", env.users["starter"]))["code"] == 200
    stopped = await labs.stop_node(
        "Shared/stoppable.json", 1, current_user=env.users["stopper"]
    )
    assert stopped["code"] == 200
    assert (await _start(env, "Shared/replacement.json", env.users["starter"]))["code"] == 200


@pytest.mark.asyncio
async def test_same_owner_cross_lab_admission_is_atomic_but_other_owner_progresses(quota_env):
    env = quota_env
    env.users.update({"alpha": _user("alpha", cpu=1), "beta": _user("beta", cpu=1)})
    _add_lab(env, "Shared/alpha-one.json", "alpha-one", "alpha", _node())
    _add_lab(env, "Shared/alpha-two.json", "alpha-two", "alpha", _node())
    _add_lab(env, "Shared/beta-one.json", "beta-one", "beta", _node())
    alpha_started = threading.Event()
    beta_started = threading.Event()
    release_alpha = threading.Event()
    alpha_second_attempted = threading.Event()

    def controlled_start(self, lab_data, node):
        lab_id = lab_data["id"]
        if lab_id == "alpha-two":
            alpha_second_attempted.set()
        if lab_id == "alpha-one":
            alpha_started.set()
            assert release_alpha.wait(timeout=3)
        if lab_id == "beta-one":
            beta_started.set()
        pid = {"alpha-one": 3001, "alpha-two": 3002, "beta-one": 3003}[lab_id]
        env.live_pids.add(pid)
        return {"lab_id": lab_id, "node_id": 1, "kind": "qemu", "pid": pid}

    env.monkeypatch.setattr(NodeRuntimeService, "_start_qemu_node", controlled_start)
    first = asyncio.create_task(_start(env, "Shared/alpha-one.json", env.users["alpha"]))
    await asyncio.wait_for(asyncio.to_thread(alpha_started.wait), timeout=1)
    second = asyncio.create_task(_start(env, "Shared/alpha-two.json", env.users["alpha"]))
    other = asyncio.create_task(_start(env, "Shared/beta-one.json", env.users["beta"]))
    await asyncio.wait_for(asyncio.to_thread(beta_started.wait), timeout=1)
    assert await asyncio.wait_for(other, timeout=1) == {
        "code": 200, "status": "success", "message": "Node started (80049)."
    }
    await asyncio.sleep(0.05)
    assert not alpha_second_attempted.is_set()
    release_alpha.set()
    assert (await asyncio.wait_for(first, timeout=1))["code"] == 200
    assert (await asyncio.wait_for(second, timeout=1))["code"] == 400
    assert len(env.live_pids) == 2


@pytest.mark.asyncio
async def test_adopted_qemu_counts_its_command_resources_against_owner(quota_env):
    env = quota_env
    env.users["owner"] = _user("owner", cpu=1, ram=512)
    _add_lab(env, "Shared/adopted.json", "lab-adopted", "owner", _node(cpu=1, ram=512))
    _add_lab(env, "Shared/new.json", "lab-new", "owner", _node(cpu=1, ram=512))
    process = SimpleNamespace(info={
        "pid": 4242,
        "name": "qemu-system-x86_64",
        "create_time": 1000.0,
        "cmdline": [
            "qemu-system-x86_64", "-uuid", "lab-adopted-1",
            "-smp", "1", "-m", "512",
        ],
    })
    env.monkeypatch.setattr("app.services.node_runtime_service.psutil.process_iter", lambda attrs=None: iter([process]))

    assert NodeRuntimeService.reconcile_orphan_qemu()["adopted"] == 1
    runtime = NodeRuntimeService._registry["lab-adopted:1"]
    assert (runtime["quota_cpu"], runtime["quota_ram"]) == (1, 512)
    env.live_pids.add(4242)
    assert (await _start(env, "Shared/new.json", env.users["owner"]))["code"] == 400


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("options", "expected"),
    [
        ("-smp 2 -m 1G", (2, 1024)),
        ("-smp 2 -m size=1G", (2, 1024)),
        ("-smp cpus=3,sockets=2,cores=2 -m 1536M", (3, 1536)),
        ("-smp sockets=2,maxcpus=6 -m size=1G,slots=2,maxmem=2G", (6, 1024)),
        ("-smp sockets=2,cores=2,threads=2 -m 2G", (8, 2048)),
        ("-smp 2,maxcpus=8 -m 512", (2, 512)),
    ],
)
async def test_qemu_options_replace_standard_quota_resources(quota_env, options, expected):
    env = quota_env
    env.users["owner"] = _user("owner", cpu=expected[0], ram=expected[1])
    _add_lab(env, "Shared/options.json", "lab-options", "owner", _node(
        cpu=1, ram=512, extras={"qemu_options": options}
    ))
    _add_lab(env, "Shared/options-over.json", "lab-options-over", "owner", _node())

    assert (await _start(env, "Shared/options.json", env.users["owner"]))["code"] == 200
    runtime = NodeRuntimeService._registry["lab-options:1"]
    assert (runtime["quota_cpu"], runtime["quota_ram"]) == expected
    assert (await _start(env, "Shared/options-over.json", env.users["owner"]))["code"] == 400


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("node_ram", "extras", "expected_ram"),
    [
        (512, {}, 512),
        (128, {"extra_args": "-m 1073741824"}, 1024),
        (128, {"extra_args": "--memory=1536m"}, 1536),
    ],
)
async def test_docker_quota_uses_effective_mib_memory(
    quota_env, node_ram, extras, expected_ram
):
    env = quota_env
    env.users["owner"] = _user("owner", ram=expected_ram)
    _add_lab(env, "Shared/docker.json", "lab-docker", "owner", _node(
        node_type="docker", ram=node_ram, extras=extras
    ))
    _add_lab(env, "Shared/docker-over.json", "lab-docker-over", "owner", _node())

    def start_docker(self, lab_data, node):
        env.live_pids.add(3500)
        return {
            "lab_id": lab_data["id"], "node_id": node["id"], "kind": "docker", "pid": 3500,
            "command": ["docker", "run", "--memory", f"{node_ram}m"],
        }

    env.monkeypatch.setattr(NodeRuntimeService, "_start_docker_node", start_docker)
    assert (await _start(env, "Shared/docker.json", env.users["owner"]))["code"] == 200
    runtime = NodeRuntimeService._registry["lab-docker:1"]
    assert runtime["quota_ram"] == expected_ram
    assert (await _start(env, "Shared/docker-over.json", env.users["owner"]))["code"] == 400


@pytest.mark.asyncio
async def test_tracked_docker_unknown_liveness_stays_charged_until_confirmed_absent(quota_env):
    env = quota_env
    env.users["owner"] = _user("owner", ram=512)
    _add_lab(env, "Shared/docker-live.json", "lab-docker-live", "owner", _node(node_type="docker"))
    _add_lab(env, "Shared/docker-next.json", "lab-docker-next", "owner", _node())

    def start_docker(self, lab_data, node):
        env.live_pids.add(3501)
        return {"lab_id": lab_data["id"], "node_id": node["id"], "kind": "docker", "pid": 3501}

    env.monkeypatch.setattr(NodeRuntimeService, "_start_docker_node", start_docker)
    assert (await _start(env, "Shared/docker-live.json", env.users["owner"]))["code"] == 200
    probe = None
    env.monkeypatch.setattr(
        NodeRuntimeService, "_check_alive_sync", staticmethod(lambda *args: probe)
    )
    assert (await _start(env, "Shared/docker-next.json", env.users["owner"]))["code"] == 400
    probe = False
    assert (await _start(env, "Shared/docker-next.json", env.users["owner"]))["code"] == 200


@pytest.mark.asyncio
@pytest.mark.parametrize("stale_record", [False, True])
async def test_missing_docker_runtime_recovers_usage_from_matching_pid_ledger(quota_env, stale_record):
    env = quota_env
    env.users["owner"] = _user("owner", ram=512)
    _add_lab(env, "Shared/docker-ledger.json", "lab-docker-ledger", "owner", _node(node_type="docker"))
    _add_lab(env, "Shared/ledger-next.json", "lab-ledger-next", "owner", _node())
    entry = {"pid": 4444, "kind": "docker", "lab_id": "lab-docker-ledger", "node_id": 1}
    env.monkeypatch.setattr("app.services.node_runtime_service.runtime_pids.list_entries", lambda: [entry])
    if stale_record:
        NodeRuntimeService()._persist_runtime({**entry, "pid": 3333})
        NodeRuntimeService.reset_registry()
    inspections = []

    def inspect(self, lab_id, node_id, container_name=None):
        inspections.append((lab_id, node_id))
        return {
            "pid": 4444,
            "container_name": container_name or "mock-container",
            "network_mode": "none",
            "memory_bytes": 512 * 1024 * 1024,
            "nano_cpus": 1_000_000_000,
        }

    env.monkeypatch.setattr(NodeRuntimeService, "docker_network_state", inspect)
    assert (await _start(env, "Shared/ledger-next.json", env.users["owner"]))["code"] == 400
    assert inspections == [("lab-docker-ledger", 1)]
    saved = NodeRuntimeService._registry.get("lab-docker-ledger:1")
    if stale_record:
        assert saved["pid"] == 3333
    else:
        assert saved is None


@pytest.mark.asyncio
@pytest.mark.parametrize("inspection", ["pid-mismatch", "error"])
async def test_unverifiable_ledger_docker_identity_fails_closed(quota_env, inspection):
    env = quota_env
    env.users["owner"] = _user("owner", ram=512)
    _add_lab(env, "Shared/docker-unknown.json", "lab-docker-unknown", "owner", _node(
        node_type="docker", ram=1
    ))
    _add_lab(env, "Shared/unknown-next.json", "lab-unknown-next", "owner", _node())
    entry = {"pid": 4444, "kind": "docker", "lab_id": "lab-docker-unknown", "node_id": 1}
    env.monkeypatch.setattr("app.services.node_runtime_service.runtime_pids.list_entries", lambda: [entry])

    def inspect(self, lab_id, node_id, container_name=None):
        if inspection == "error":
            raise NodeRuntimeError("mock inspection unavailable")
        return {"pid": 5555, "memory_bytes": 1024 * 1024, "nano_cpus": 1}

    env.monkeypatch.setattr(NodeRuntimeService, "docker_network_state", inspect)
    response = await _start(env, "Shared/unknown-next.json", env.users["owner"])
    assert response["code"] == 400
    assert "could not be determined" in response["message"]
    assert env.attempts == []


@pytest.mark.asyncio
async def test_ledger_docker_verified_absence_releases_usage(quota_env):
    env = quota_env
    env.users["owner"] = _user("owner", ram=512)
    _add_lab(env, "Shared/docker-absent.json", "lab-docker-absent", "owner", _node(node_type="docker"))
    _add_lab(env, "Shared/absent-next.json", "lab-absent-next", "owner", _node())
    entry = {"pid": 4444, "kind": "docker", "lab_id": "lab-docker-absent", "node_id": 1}
    env.monkeypatch.setattr("app.services.node_runtime_service.runtime_pids.list_entries", lambda: [entry])
    env.monkeypatch.setattr(NodeRuntimeService, "docker_network_state", lambda *args, **kwargs: None)

    assert (await _start(env, "Shared/absent-next.json", env.users["owner"]))["code"] == 200


@pytest.mark.asyncio
async def test_legacy_tracked_docker_uses_hostconfig_not_saved_argv(quota_env):
    env = quota_env
    env.users["owner"] = _user("owner", ram=512)
    _add_lab(env, "Shared/docker-old.json", "lab-docker-old", "owner", _node(node_type="docker"))
    _add_lab(env, "Shared/docker-old-next.json", "lab-docker-old-next", "owner", _node())
    NodeRuntimeService._registry["lab-docker-old:1"] = {
        "lab_id": "lab-docker-old",
        "node_id": 1,
        "kind": "docker",
        "pid": 4555,
        "container_name": "mock-old-container",
        "command": ["docker", "run", "--memory", "1m"],
    }
    NodeRuntimeService._loaded = True
    env.live_pids.add(4555)
    env.monkeypatch.setattr(
        NodeRuntimeService, "docker_network_state",
        lambda *args, **kwargs: {
            "pid": 4555, "memory_bytes": 512 * 1024 * 1024, "nano_cpus": 1_000_000_000,
        },
    )

    assert (await _start(env, "Shared/docker-old-next.json", env.users["owner"]))["code"] == 400


@pytest.mark.asyncio
async def test_legacy_tracked_docker_inspection_failure_is_unknown(quota_env):
    env = quota_env
    env.users["owner"] = _user("owner", ram=512)
    _add_lab(env, "Shared/docker-unverified.json", "lab-docker-unverified", "owner", _node(
        node_type="docker", ram=1
    ))
    _add_lab(env, "Shared/docker-unverified-next.json", "lab-docker-unverified-next", "owner", _node())
    NodeRuntimeService._registry["lab-docker-unverified:1"] = {
        "lab_id": "lab-docker-unverified",
        "node_id": 1,
        "kind": "docker",
        "pid": 4556,
        "container_name": "mock-unverified-container",
        "command": ["docker", "run", "--memory", "1m"],
    }
    NodeRuntimeService._loaded = True
    env.live_pids.add(4556)
    env.monkeypatch.setattr(
        NodeRuntimeService, "docker_network_state",
        lambda *args, **kwargs: (_ for _ in ()).throw(NodeRuntimeError("mock inspect failure")),
    )

    response = await _start(env, "Shared/docker-unverified-next.json", env.users["owner"])
    assert response["code"] == 400
    assert "could not be determined" in response["message"]


@pytest.mark.asyncio
async def test_cleanup_pending_live_node_still_consumes_quota(quota_env):
    env = quota_env
    env.users["owner"] = _user("owner", cpu=1)
    _add_lab(env, "Shared/cleanup.json", "lab-cleanup", "owner", _node())
    _add_lab(env, "Shared/cleanup-over.json", "lab-cleanup-over", "owner", _node())
    assert (await _start(env, "Shared/cleanup.json", env.users["owner"]))["code"] == 200
    NodeRuntimeService._registry["lab-cleanup:1"]["lab_cleanup_pending"] = True

    assert (await _start(env, "Shared/cleanup-over.json", env.users["owner"]))["code"] == 400


@pytest.mark.asyncio
async def test_unknown_resources_do_not_block_unlimited_or_unowned_starts(quota_env):
    env = quota_env
    env.users["owner"] = _user("owner", cpu=-1, ram=-1)
    _add_lab(env, "Shared/unknown.json", "lab-unknown", "owner", _node(ram="unrecognized"))

    assert (await _start(env, "Shared/unknown.json", env.users["owner"]))["code"] == 200
    runtime = NodeRuntimeService._registry["lab-unknown:1"]
    assert runtime["quota_cpu"] == 1 and runtime["quota_ram"] is None


@pytest.mark.asyncio
async def test_unknown_resources_fail_closed_for_the_finite_dimension(quota_env):
    env = quota_env
    env.users["owner"] = _user("owner", cpu=-1, ram=512)
    _add_lab(env, "Shared/unknown.json", "lab-unknown", "owner", _node(ram="unrecognized"))

    assert (await _start(env, "Shared/unknown.json", env.users["owner"]))["code"] == 400
    assert env.attempts == []


@pytest.mark.asyncio
async def test_dynamips_ram_uses_template_and_extras_override(quota_env):
    env = quota_env
    env.users["owner"] = _user("owner", ram=512)
    _add_lab(env, "Shared/dynamips.json", "lab-dynamips", "owner", _node(
        node_type="dynamips", ram=128, extras={"ram": 1024}
    ))
    env.monkeypatch.setattr(
        NodeRuntimeService, "_resolve_template_for_node",
        lambda self, node: {"platform": "c3725", "ram": 256},
    )
    response = await _start(env, "Shared/dynamips.json", env.users["owner"])
    assert response["code"] == 400
    assert "RAM" in response["message"]


@pytest.mark.asyncio
@pytest.mark.parametrize("platform", [None, "C7200"])
async def test_dynamips_ram_uses_launcher_platform_default(quota_env, platform):
    env = quota_env
    env.users["owner"] = _user("owner", ram=512)
    _add_lab(env, "Shared/dynamips-default.json", "lab-dynamips-default", "owner", _node(
        node_type="dynamips", ram=128
    ))
    env.monkeypatch.setattr(
        NodeRuntimeService, "_resolve_template_for_node",
        lambda self, node: {"platform": platform, "ram": None},
    )

    def start_dynamips(self, lab_data, node):
        env.live_pids.add(3600)
        return {"lab_id": lab_data["id"], "node_id": node["id"], "kind": "dynamips", "pid": 3600}

    env.monkeypatch.setattr(NodeRuntimeService, "_start_dynamips_node", start_dynamips)
    assert (await _start(env, "Shared/dynamips-default.json", env.users["owner"]))["code"] == 200
    assert NodeRuntimeService._registry["lab-dynamips-default:1"]["quota_ram"] == 512


@pytest.mark.asyncio
async def test_iol_ram_falls_back_to_extras(quota_env):
    env = quota_env
    env.users["owner"] = _user("owner", ram=512)
    _add_lab(env, "Shared/iol.json", "lab-iol", "owner", _node(
        node_type="iol", ram=None, extras={"ram": 1024}
    ))
    response = await _start(env, "Shared/iol.json", env.users["owner"])
    assert response["code"] == 400
    assert "RAM" in response["message"]


@pytest.mark.asyncio
async def test_legacy_iol_command_ram_supports_positional_size_and_options(quota_env):
    env = quota_env
    env.users["owner"] = _user("owner", ram=512)
    _add_lab(env, "Shared/iol-old.json", "lab-iol-old", "owner", _node(node_type="iol", ram=None))
    _add_lab(env, "Shared/iol-next.json", "lab-iol-next", "owner", _node())
    NodeRuntimeService._registry["lab-iol-old:1"] = {
        "lab_id": "lab-iol-old",
        "node_id": 1,
        "kind": "iol",
        "pid": 3777,
        "command": ["iol-image", "-m", "size=512,slots=2,maxmem=2G", "1"],
    }
    NodeRuntimeService._loaded = True
    env.live_pids.add(3777)

    assert (await _start(env, "Shared/iol-next.json", env.users["owner"]))["code"] == 400


@pytest.mark.asyncio
async def test_legacy_file_only_lab_preserves_existing_unowned_start_behavior(quota_env):
    env = quota_env
    env.users["starter"] = _user("starter", cpu=0, ram=0)
    filename = "Shared/legacy-import.json"
    data = {
        "schema": 2, "id": "legacy-import", "meta": {"name": "legacy", "owner": "untrusted"},
        "nodes": {"1": _node()}, "networks": {}, "links": [],
    }
    path = env.settings.LABS_DIR / filename
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(data))

    response = await _start(env, filename, env.users["starter"])

    assert response["code"] == 200
    runtime = NodeRuntimeService._registry["legacy-import:1"]
    assert (runtime["quota_cpu"], runtime["quota_ram"]) == (1, 512)


@pytest.mark.asyncio
async def test_missing_owner_account_fails_closed(quota_env):
    env = quota_env
    env.users["starter"] = _user("starter")
    _add_lab(env, "Shared/missing-owner.json", "lab-missing-owner", "deleted-owner", _node())
    response = await _start(env, "Shared/missing-owner.json", env.users["starter"])
    assert response["code"] == 400
    assert env.attempts == []
