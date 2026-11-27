# Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
# SPDX-License-Identifier: Apache-2.0

import asyncio
from collections import deque
from contextlib import AbstractAsyncContextManager, nullcontext
from dataclasses import dataclass, field
from typing import Any

from fastapi import WebSocket


_CATALOG_SEND_TIMEOUT_S = 1.0


@dataclass
class WsEvent:
    seq: int
    type: str
    rev: str
    payload: dict
    identity: str | None = None


@dataclass
class _LabState:
    events: deque = field(default_factory=deque)
    next_seq: int = 1
    subscribers: dict[WebSocket, str | None] = field(default_factory=dict)
    pending_subscribers: set[WebSocket] = field(default_factory=set)


class WsHub:
    def __init__(self, ring_size: int = 200) -> None:
        self._ring_size = ring_size
        self._labs: dict[str, _LabState] = {}
        self._lock = asyncio.Lock()

    def _get_or_create(self, lab_id: str) -> _LabState:
        if lab_id not in self._labs:
            self._labs[lab_id] = _LabState(events=deque(maxlen=self._ring_size))
        return self._labs[lab_id]

    def register(self, lab_id: str, ws: WebSocket) -> None:
        self._get_or_create(lab_id).pending_subscribers.add(ws)

    async def publish_all(self, event_type: str, payload: dict) -> list[WsEvent]:
        async with self._lock:
            lab_ids = [
                lab_id
                for lab_id, state in self._labs.items()
                if state.subscribers or state.pending_subscribers
            ]
        events = await asyncio.gather(*(
            self.publish(lab_id, event_type, payload, send_timeout=_CATALOG_SEND_TIMEOUT_S)
            for lab_id in lab_ids
        ))
        return [event for event in events if event is not None]

    async def publish(
        self,
        lab_id: str,
        event_type: str,
        payload: dict,
        rev: str = "",
        *,
        identity: str | None = None,
        guard: AbstractAsyncContextManager[bool] | None = None,
        send_timeout: float | None = None,
    ) -> WsEvent | None:
        async with guard if guard is not None else nullcontext(True) as valid:
            if not valid:
                return None
            async with self._lock:
                state = self._get_or_create(lab_id)
                seq = state.next_seq
                state.next_seq += 1
                event = WsEvent(seq=seq, type=event_type, rev=rev, payload=payload, identity=identity)
                state.events.append(event)
                subscribers = [
                    ws for ws, subscriber_id in state.subscribers.items()
                    if identity is None or subscriber_id == identity
                ]

        # Socket sends can block. Release the lab and hub locks first.
        dead: list[WebSocket] = []
        for ws in subscribers:
            try:
                message = {"seq": event.seq, "type": event.type, "rev": event.rev, "payload": event.payload}
                if send_timeout is None:
                    await ws.send_json(message)
                else:
                    async with asyncio.timeout(send_timeout):
                        await ws.send_json(message)
            except TimeoutError:
                # Catalog sends are best effort. The event remains replayable,
                # and a slow socket can still receive its normal lab events.
                if send_timeout is None:
                    dead.append(ws)
            except Exception:
                dead.append(ws)

        if dead:
            async with self._lock:
                state = self._labs.get(lab_id)
                if state:
                    for ws in dead:
                        state.subscribers.pop(ws, None)

        return event

    async def subscribe(
        self, lab_id: str, ws: WebSocket, *, after_seq: int | None = None, identity: str | None = None,
    ) -> bool:
        async with self._lock:
            state = self._get_or_create(lab_id)
            if after_seq is not None and after_seq != state.next_seq - 1:
                return False
            state.pending_subscribers.discard(ws)
            state.subscribers[ws] = identity
            return True

    async def unsubscribe(self, lab_id: str, ws: WebSocket) -> None:
        async with self._lock:
            state = self._labs.get(lab_id)
            if state:
                state.subscribers.pop(ws, None)
                state.pending_subscribers.discard(ws)

    def replay_since(
        self, lab_id: str, last_seq: int, *, identity: str | None = None,
    ) -> tuple[list[WsEvent], bool]:
        """Return (events_to_replay, needs_force_resnapshot).

        - If last_seq + 1 is in the ring, return the slice from that point.
        - If ring is non-empty and last_seq < head of ring - 1, ring overflow:
          return ([], True) to trigger force_resnapshot.
        - If last_seq is ahead of this hub, its sequence was reset.
        """
        state = self._labs.get(lab_id)
        if last_seq > self.head_seq(lab_id):
            return [], True
        if not state or not state.events:
            return [], False

        events = list(state.events)
        ring_head_seq = events[0].seq  # oldest event in ring

        # Check if the next expected seq is still in the ring
        next_needed = last_seq + 1
        if last_seq and next_needed < ring_head_seq:
            # Gap: ring has overflowed past what client had
            return [], True

        # Find and return all events with seq > last_seq
        replay = [
            ev for ev in events
            if ev.seq > last_seq and (ev.identity is None or ev.identity == identity)
        ]
        return replay, False

    def head_seq(self, lab_id: str) -> int:
        """Return the last published seq for lab_id, or 0 if none."""
        state = self._labs.get(lab_id)
        if not state or state.next_seq == 1:
            return 0
        return state.next_seq - 1


ws_hub = WsHub()
