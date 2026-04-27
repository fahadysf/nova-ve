# Copyright (c) 2026 Fahad Yousuf <fahadysf@gmail.com>
# SPDX-License-Identifier: Apache-2.0

import asyncio
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from fastapi import WebSocket


@dataclass
class WsEvent:
    seq: int
    type: str
    rev: str
    payload: dict


@dataclass
class _LabState:
    events: deque = field(default_factory=deque)
    next_seq: int = 1
    subscribers: set = field(default_factory=set)


class WsHub:
    def __init__(self, ring_size: int = 200) -> None:
        self._ring_size = ring_size
        self._labs: dict[str, _LabState] = {}
        self._lock = asyncio.Lock()

    def _get_or_create(self, lab_id: str) -> _LabState:
        if lab_id not in self._labs:
            self._labs[lab_id] = _LabState(events=deque(maxlen=self._ring_size))
        return self._labs[lab_id]

    async def publish(
        self,
        lab_id: str,
        event_type: str,
        payload: dict,
        rev: str = "",
    ) -> WsEvent:
        async with self._lock:
            state = self._get_or_create(lab_id)
            seq = state.next_seq
            state.next_seq += 1
            event = WsEvent(seq=seq, type=event_type, rev=rev, payload=payload)
            state.events.append(event)
            subscribers = set(state.subscribers)

        # Broadcast outside lock to avoid deadlock if send blocks
        dead: list[WebSocket] = []
        for ws in subscribers:
            try:
                await ws.send_json(
                    {"seq": event.seq, "type": event.type, "rev": event.rev, "payload": event.payload}
                )
            except Exception:
                dead.append(ws)

        if dead:
            async with self._lock:
                state = self._labs.get(lab_id)
                if state:
                    for ws in dead:
                        state.subscribers.discard(ws)

        return event

    async def subscribe(self, lab_id: str, ws: WebSocket) -> None:
        async with self._lock:
            state = self._get_or_create(lab_id)
            state.subscribers.add(ws)

    async def unsubscribe(self, lab_id: str, ws: WebSocket) -> None:
        async with self._lock:
            state = self._labs.get(lab_id)
            if state:
                state.subscribers.discard(ws)

    def replay_since(self, lab_id: str, last_seq: int) -> tuple[list[WsEvent], bool]:
        """Return (events_to_replay, needs_force_resnapshot).

        - If last_seq + 1 is in the ring, return the slice from that point.
        - If ring is non-empty and last_seq < head of ring - 1, ring overflow:
          return ([], True) to trigger force_resnapshot.
        - If ring is empty or last_seq == 0 and ring is empty, return ([], False).
        """
        state = self._labs.get(lab_id)
        if not state or not state.events:
            return [], False

        events = list(state.events)
        ring_head_seq = events[0].seq  # oldest event in ring

        if last_seq == 0:
            # No prior state — replay everything in ring
            return events, False

        # Check if the next expected seq is still in the ring
        next_needed = last_seq + 1
        if next_needed < ring_head_seq:
            # Gap: ring has overflowed past what client had
            return [], True

        # Find and return all events with seq > last_seq
        replay = [ev for ev in events if ev.seq > last_seq]
        return replay, False

    def head_seq(self, lab_id: str) -> int:
        """Return the last published seq for lab_id, or 0 if none."""
        state = self._labs.get(lab_id)
        if not state or state.next_seq == 1:
            return 0
        return state.next_seq - 1


ws_hub = WsHub()
