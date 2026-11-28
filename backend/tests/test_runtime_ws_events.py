import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services.lab_lock import lab_lock_async
from app.services.lab_paths import publish_runtime_event
from app.services.node_runtime_service import NodeRuntimeService
from app.services.ws_hub import WsHub


def write_lab(root: Path, filename: str, lab_id: str) -> Path:
    path = root / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"schema": 2, "id": lab_id}), encoding="utf-8")
    return path


class Listener:
    def __init__(self):
        self.events = asyncio.Queue()

    async def send_json(self, message):
        self.events.put_nowait((asyncio.get_running_loop(), message))


@pytest.mark.asyncio
async def test_runtime_event_drops_a_replaced_path_and_follows_the_lab_on_lookup(tmp_path):
    hub = WsHub()
    old_path = "old/lab.json"
    new_path = "new/lab.json"
    lab_file = write_lab(tmp_path, old_path, "fixture-id")
    old_listener = Listener()
    await hub.subscribe(old_path, old_listener, identity="fixture-id")
    await publish_runtime_event(hub, tmp_path, "fixture-id", "node_warning", {"node_id": 1})
    assert old_listener.events.qsize() == 1

    replacement = tmp_path / new_path
    replacement.parent.mkdir()
    lab_file.rename(replacement)
    write_lab(tmp_path, old_path, "other-id")

    await publish_runtime_event(
        hub, tmp_path, "fixture-id", "discovered_link", {"generation": 1}, path=old_path,
    )
    assert hub.head_seq(old_path) == 1
    assert hub.replay_since(old_path, 0, identity="other-id") == ([], False)
    await publish_runtime_event(hub, tmp_path, "other-id", "node_warning", {"node_id": 2})
    assert old_listener.events.qsize() == 1

    await publish_runtime_event(hub, tmp_path, "fixture-id", "node_warning", {"node_id": 1})
    events, _ = hub.replay_since(new_path, 0, identity="fixture-id")
    assert [event.type for event in events] == ["node_warning"]

    write_lab(tmp_path, "copy.json", "fixture-id")
    await publish_runtime_event(hub, tmp_path, "fixture-id", "node_warning", {"node_id": 2})
    assert hub.head_seq(new_path) == 1
    assert hub.head_seq("copy.json") == 0


@pytest.mark.asyncio
async def test_runtime_event_does_not_hold_lab_lock_while_sending_to_client(tmp_path):
    hub = WsHub()
    filename = "nested/lab.json"
    write_lab(tmp_path, filename, "fixture-id")
    received = []

    class LockCheckingListener:
        async def send_json(self, message):
            async with lab_lock_async(filename, tmp_path, timeout_s=0, create_parent=False):
                received.append(message["type"])

    await hub.subscribe(filename, LockCheckingListener(), identity="fixture-id")
    await publish_runtime_event(hub, tmp_path, "fixture-id", "node_warning", {}, path=filename)
    assert received == ["node_warning"]


@pytest.mark.asyncio
@pytest.mark.parametrize("filename", ["lab.json", "team/project/lab.json"])
async def test_worker_warning_and_live_mac_cadence_use_the_backend_loop(
    tmp_path, monkeypatch, filename,
):
    lab_id = "fixture-id"
    lab_data = {"id": lab_id, "nodes": {"1": {"interfaces": [{"index": 0}]}}}
    write_lab(tmp_path, filename, lab_id)
    hub = WsHub()
    monkeypatch.setattr("app.services.ws_hub.ws_hub", hub)
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(NodeRuntimeService, "_runtime_loop", loop)
    service = object.__new__(NodeRuntimeService)
    service.settings = SimpleNamespace(LABS_DIR=tmp_path)
    monkeypatch.setattr(service, "read_live_mac", lambda *args, **kwargs: {"state": "confirmed"})
    listener = Listener()
    await hub.subscribe(filename, listener, identity=lab_id)
    real_sleep = asyncio.sleep

    async def no_cadence_wait(delay, *args):
        # The cadence waits 1s, 3s and 8s between reads; skip those waits.
        await real_sleep(0 if delay >= 1 else delay, *args)

    monkeypatch.setattr(asyncio, "sleep", no_cadence_wait)

    current_tasks = asyncio.all_tasks()
    try:
        await asyncio.to_thread(
            service._publish_node_warning,
            lab_id=lab_id, node_id=1, interface_index=0, message="fixture warning",
        )
        await asyncio.to_thread(service._schedule_live_mac_cadence, lab_data, lab_id, 1, [{"index": 0}])
        event_types = set()
        for _ in range(2):
            sending_loop, event = await asyncio.wait_for(listener.events.get(), timeout=3)
            assert sending_loop is loop
            event_types.add(event["type"])
            assert event["payload"]["node_id"] == 1
        assert event_types == {"node_warning", "interface_live_mac"}
        assert hub.head_seq(lab_id) == 0
    finally:
        cadence_tasks = asyncio.all_tasks() - current_tasks
        for task in cadence_tasks:
            task.cancel()
        await asyncio.gather(*cadence_tasks, return_exceptions=True)
