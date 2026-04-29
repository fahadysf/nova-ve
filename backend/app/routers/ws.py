# Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
# SPDX-License-Identifier: Apache-2.0

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.database import get_db
from app.services.ws_hub import ws_hub
from app.services.lab_service import LabService
from app.services.node_runtime_service import NodeRuntimeService

router = APIRouter(prefix="/ws", tags=["websocket"])


@router.websocket("/labs/{lab}")
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

    await websocket.accept()
    await websocket.send_json({"type": "hello", "seq": ws_hub.head_seq(lab)})

    events, needs_resnap = ws_hub.replay_since(lab, last_seq)
    if needs_resnap:
        await websocket.send_json({"type": "force_resnapshot"})
        try:
            data = LabService.read_lab_json_static(lab)
            await websocket.send_json({
                "seq": ws_hub.head_seq(lab),
                "type": "lab_topology",
                "rev": str(data.get("id", "")),
                "generation": NodeRuntimeService.get_discovery_generation(lab),
                "payload": data,
            })
        except Exception:
            pass
    else:
        for ev in events:
            await websocket.send_json(
                {"seq": ev.seq, "type": ev.type, "rev": ev.rev, "payload": ev.payload}
            )

    await ws_hub.subscribe(lab, websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        await ws_hub.unsubscribe(lab, websocket)
