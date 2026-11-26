# Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
# SPDX-License-Identifier: Apache-2.0

import asyncio

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.database import get_db
from app.routers.labs import _scoped_lab_path
from app.services.ws_hub import ws_hub
from app.services.lab_service import LabService, _normalize_relative_lab_path
from app.services.node_runtime_service import NodeRuntimeService

# WebSocket routes don't carry HTTP response codes the way REST routes do
# (the WebSocket protocol uses close codes, not response codes), so the
# COMMON_RESPONSES spread is intentionally omitted here.
router = APIRouter(prefix="/ws", tags=["websocket"])


@router.websocket("/labs/{lab:path}")
async def lab_ws(websocket: WebSocket, lab: str, last_seq: int = 0):
    # FastAPI Depends-based auth is unavailable for WebSocket; resolve manually from cookies.
    user = None
    try:
        async for db in get_db():
            from app.services.auth_service import AuthService
            session_cookie = websocket.cookies.get("nova_session")
            user_cookie = websocket.cookies.get("nova_user")
            if session_cookie and user_cookie:
                auth_service = AuthService(db)
                db_user = await auth_service.validate_session(session_cookie, user_cookie)
                if db_user:
                    from app.schemas.user import UserRead
                    user = UserRead.model_validate(db_user)
            break
    except Exception:
        pass

    if user is None:
        await websocket.close(code=1008)
        return

    # #327: the event stream and resnapshot carry the lab's topology, so the
    # caller must be able to open the lab, as on the HTTP lab routes.
    # #330: subscribe, replay and resnapshot under the same normalized key
    # publishers use, not the raw route value.
    try:
        # ``{lab:path}`` also matches an empty path. A folder-scoped user
        # would otherwise be subscribed to their folder itself.
        if not lab.strip().replace("\\", "/").strip("/"):
            raise ValueError("empty lab path")
        scoped = _scoped_lab_path(user, lab, treat_as_absolute=True)
        lab = _normalize_relative_lab_path(scoped)
        if not await asyncio.to_thread(LabService.file_exists, lab):
            raise FileNotFoundError(lab)
    except (OSError, ValueError):
        await websocket.close(code=1008)
        return

    await websocket.accept()
    await websocket.send_json({"type": "hello", "seq": ws_hub.head_seq(lab)})

    try:
        while True:
            head = ws_hub.head_seq(lab)
            events, needs_resnap = ws_hub.replay_since(lab, last_seq)
            if needs_resnap:
                await websocket.send_json({"type": "force_resnapshot"})
                try:
                    data = await asyncio.to_thread(LabService.read_lab_json_static, lab)
                except (OSError, ValueError):
                    await websocket.close(code=1008)
                    return
                await websocket.send_json({
                    "seq": head,
                    "type": "lab_topology",
                    "rev": str(data.get("id", "")),
                    "generation": NodeRuntimeService.get_discovery_generation(str(data.get("id", ""))),
                    "payload": data,
                })
            else:
                for ev in events:
                    await websocket.send_json(
                        {"seq": ev.seq, "type": ev.type, "rev": ev.rev, "payload": ev.payload}
                    )

            # An event can land while sending the handshake or replay. Catch it
            # before handing the connection over to live publication.
            if await ws_hub.subscribe(lab, websocket, after_seq=head):
                break
            last_seq = head

        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        await ws_hub.unsubscribe(lab, websocket)
