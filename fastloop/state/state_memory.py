import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any

from ..exceptions import LoopClaimError, LoopNotFoundError
from ..loop import LoopEvent
from ..types import LoopEventSender, LoopStatus
from .state import LoopState, StateManager


class MemoryStateManager(StateManager):
    def __init__(self, app_name: str):
        self.app_name = app_name
        self._loops: dict[str, LoopState] = {}
        self._events: dict[str, list[LoopEvent]] = {}
        self._context: dict[str, dict[str, Any]] = {}
        self._claims: dict[str, str] = {}
        self._nonces: dict[str, int] = {}
        self._initial_events: dict[str, LoopEvent] = {}

    async def get_loop(self, loop_id: str) -> LoopState:
        if loop_id not in self._loops:
            raise LoopNotFoundError(f"Loop {loop_id} not found")
        return self._loops[loop_id]

    async def get_or_create_loop(
        self,
        *,
        loop_name: str | None = None,
        loop_id: str | None = None,
        idle_timeout: float = 60.0,
    ) -> tuple[LoopState, bool]:
        if not loop_id:
            loop_id = str(uuid.uuid4())

        if loop_id in self._loops:
            return self._loops[loop_id], False

        loop = LoopState(
            loop_id=loop_id,
            loop_name=loop_name,
            idle_timeout=idle_timeout,
            last_event_at=int(datetime.now().timestamp()),
        )

        self._loops[loop_id] = loop
        self._events[loop_id] = []
        self._context[loop_id] = {}
        return loop, True

    async def update_loop(self, loop_id: str, state: LoopState):
        self._loops[loop_id] = state

    async def update_loop_status(self, loop_id: str, status: LoopStatus) -> LoopState:
        loop = await self.get_loop(loop_id)
        loop.status = status
        await self.update_loop(loop_id, loop)
        return loop

    async def get_all_loop_ids(self) -> set[str]:
        return set(self._loops.keys())

    async def get_all_loops(
        self,
        status: LoopStatus | None = None,
    ) -> list[LoopState]:
        loops = list(self._loops.values())
        if status:
            loops = [loop for loop in loops if loop.status == status]
        return loops

    async def push_event(self, loop_id: str, event: LoopEvent):
        if loop_id not in self._events:
            self._events[loop_id] = []

        # Set nonce if not already set
        if event.nonce is None:
            event.nonce = await self.get_next_nonce(loop_id)

        # Store initial event if this is the first event
        if loop_id not in self._initial_events:
            self._initial_events[loop_id] = event

        self._events[loop_id].append(event)

        # Update loop timestamp
        if loop_id in self._loops:
            self._loops[loop_id].last_event_at = datetime.now().timestamp()

    async def get_event_history(self, loop_id: str) -> list[LoopEvent]:
        return self._events.get(loop_id, [])

    async def pop_server_event(self, loop_id: str) -> dict[str, Any] | None:
        events = self._events.get(loop_id, [])
        for i, event in enumerate(events):
            if event.sender == LoopEventSender.SERVER:
                return events.pop(i).to_dict()
        return None

    async def pop_event(
        self,
        loop_id: str,
        event: LoopEvent,
        sender: LoopEventSender = LoopEventSender.CLIENT,
    ) -> LoopEvent | None:
        events = self._events.get(loop_id, [])
        for i, stored_event in enumerate(events):
            if stored_event.type == event.type and stored_event.sender == sender:
                return events.pop(i)
        return None

    @asynccontextmanager
    async def with_claim(self, loop_id: str):
        claim_id = str(uuid.uuid4())

        if loop_id in self._claims:
            raise LoopClaimError(f"Could not acquire claim for loop {loop_id}")

        self._claims[loop_id] = claim_id

        try:
            yield
        finally:
            if self._claims.get(loop_id) == claim_id:
                del self._claims[loop_id]

    async def has_claim(self, loop_id: str) -> bool:
        return loop_id in self._claims

    async def get_context_value(self, loop_id: str, key: str) -> Any:
        loop_context = self._context.get(loop_id, {})
        return loop_context.get(key)

    async def set_context_value(self, loop_id: str, key: str, value: Any):
        if loop_id not in self._context:
            self._context[loop_id] = {}
        self._context[loop_id][key] = value

    async def get_initial_event(self, loop_id: str) -> LoopEvent | None:
        return self._initial_events.get(loop_id)

    async def get_next_nonce(self, loop_id: str) -> int:
        if loop_id not in self._nonces:
            self._nonces[loop_id] = 0
        self._nonces[loop_id] += 1
        return self._nonces[loop_id]

    async def get_events_since(
        self, loop_id: str, since_timestamp: float
    ) -> list[LoopEvent]:
        all_events = await self.get_event_history(loop_id)
        return [event for event in all_events if event.timestamp >= since_timestamp]
