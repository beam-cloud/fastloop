import json
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any

import cloudpickle
import redis.asyncio as redis

from ..constants import (
    CLAIM_LOCK_BLOCKING_TIMEOUT_S,
    CLAIM_LOCK_SLEEP_S,
)
from ..exceptions import LoopClaimError, LoopNotFoundError
from ..loop import LoopEvent
from ..types import LoopEventSender, LoopStatus, RedisConfig
from .state import LoopState, StateManager

KEY_PREFIX = "fastloop"


class RedisKeys:
    LOOP_INDEX = f"{KEY_PREFIX}:{{app_name}}:index"
    LOOP_EVENT_QUEUE_SERVER = f"{KEY_PREFIX}:{{app_name}}:events:{{loop_id}}:server"
    LOOP_EVENT_QUEUE_CLIENT = (
        f"{KEY_PREFIX}:{{app_name}}:events:{{loop_id}}:{{event_type}}:client"
    )
    LOOP_EVENT_HISTORY = f"{KEY_PREFIX}:{{app_name}}:event_history:{{loop_id}}"
    LOOP_INITIAL_EVENT = f"{KEY_PREFIX}:{{app_name}}:initial_event:{{loop_id}}"
    LOOP_STATE = f"{KEY_PREFIX}:{{app_name}}:state:{{loop_id}}"
    LOOP_CLAIM = f"{KEY_PREFIX}:{{app_name}}:claim:{{loop_id}}"
    LOOP_CONTEXT = f"{KEY_PREFIX}:{{app_name}}:context:{{loop_id}}:{{key}}"
    LOOP_NONCE = f"{KEY_PREFIX}:{{app_name}}:nonce:{{loop_id}}"


class RedisStateManager(StateManager):
    def __init__(self, *, app_name: str, config: RedisConfig):
        self.app_name = app_name
        self.rdb = redis.Redis(
            host=config.host,
            port=config.port,
            db=config.database,
            password=config.password,
            ssl=config.ssl,
        )

    async def get_loop(self, loop_id: str) -> LoopState:
        loop_str = await self.rdb.get(
            RedisKeys.LOOP_STATE.format(app_name=self.app_name, loop_id=loop_id)
        )
        if loop_str:
            return LoopState.from_json(loop_str.decode("utf-8"))
        else:
            raise LoopNotFoundError(f"Loop {loop_id} not found")

    async def get_or_create_loop(
        self,
        *,
        loop_name: str | None = None,
        loop_id: str | None = None,
        idle_timeout: float = 60.0,
    ) -> tuple[LoopState, bool]:
        if loop_id:
            loop_str = await self.rdb.get(
                RedisKeys.LOOP_STATE.format(app_name=self.app_name, loop_id=loop_id)
            )
            if loop_str:
                return LoopState.from_json(loop_str.decode("utf-8")), False
            else:
                raise LoopNotFoundError(f"Loop {loop_id} not found")

        loop_id = str(uuid.uuid4())
        loop = LoopState(
            loop_id=loop_id,
            loop_name=loop_name,
            idle_timeout=idle_timeout,
            last_event_at=int(datetime.now().timestamp()),
        )

        await self.rdb.set(
            RedisKeys.LOOP_STATE.format(app_name=self.app_name, loop_id=loop_id),
            loop.to_string(),
        )
        await self.rdb.sadd(
            RedisKeys.LOOP_INDEX.format(app_name=self.app_name), loop_id
        )

        return loop, True

    async def update_loop(self, loop_id: str, state: LoopState):
        await self.rdb.set(
            RedisKeys.LOOP_STATE.format(app_name=self.app_name, loop_id=loop_id),
            state.to_string(),
        )

    async def update_loop_status(self, loop_id: str, status: LoopStatus) -> LoopState:
        loop = await self.get_loop(loop_id=loop_id)
        loop.status = status
        await self.update_loop(loop_id, loop)
        return loop

    @asynccontextmanager
    async def with_claim(self, loop_id: str):
        lock_key = RedisKeys.LOOP_CLAIM.format(app_name=self.app_name, loop_id=loop_id)
        lock = self.rdb.lock(
            name=lock_key,
            timeout=None,
            sleep=CLAIM_LOCK_SLEEP_S,
            blocking_timeout=CLAIM_LOCK_BLOCKING_TIMEOUT_S,
        )

        acquired = await lock.acquire()
        if not acquired:
            raise LoopClaimError(f"Could not acquire lock for loop {loop_id}")

        try:
            loop_str = await self.rdb.get(
                RedisKeys.LOOP_STATE.format(app_name=self.app_name, loop_id=loop_id)
            )
            if loop_str:
                loop = LoopState.from_json(loop_str.decode("utf-8"))
                await self.rdb.set(
                    RedisKeys.LOOP_STATE.format(
                        app_name=self.app_name, loop_id=loop_id
                    ),
                    loop.to_string(),
                )

            yield

        finally:
            await lock.release()

    async def has_claim(self, loop_id: str) -> bool:
        return await self.rdb.get(
            RedisKeys.LOOP_CLAIM.format(app_name=self.app_name, loop_id=loop_id)
        )

    async def get_all_loop_ids(self) -> set[str]:
        return {
            loop_id.decode("utf-8")
            for loop_id in await self.rdb.smembers(
                RedisKeys.LOOP_INDEX.format(app_name=self.app_name)
            )
        }

    async def get_all_loops(
        self,
        status: LoopStatus | None = None,
    ) -> list[LoopState]:
        loop_ids = [
            loop_id.decode("utf-8")
            for loop_id in await self.rdb.smembers(
                RedisKeys.LOOP_INDEX.format(app_name=self.app_name)
            )
        ]

        all = []
        for loop_id in loop_ids:
            loop_state_str = await self.rdb.get(
                RedisKeys.LOOP_STATE.format(app_name=self.app_name, loop_id=loop_id)
            )

            if not loop_state_str:
                await self.rdb.srem(
                    RedisKeys.LOOP_INDEX.format(app_name=self.app_name), loop_id
                )
                continue

            try:
                loop_state = LoopState.from_json(loop_state_str.decode("utf-8"))
            except TypeError:
                await self.rdb.srem(
                    RedisKeys.LOOP_INDEX.format(app_name=self.app_name), loop_id
                )
                continue

            if status and loop_state.status != status:
                continue

            all.append(loop_state)

        return all

    async def get_event_history(self, loop_id: str) -> dict[str, Any]:
        event_history = await self.rdb.lrange(
            RedisKeys.LOOP_EVENT_HISTORY.format(
                app_name=self.app_name, loop_id=loop_id
            ),
            0,
            -1,
        )
        events = [json.loads(event.decode("utf-8")) for event in event_history]
        events.sort(key=lambda e: e["nonce"] or 0)
        return events

    async def push_event(self, loop_id: str, event: "LoopEvent"):
        if event.sender == LoopEventSender.SERVER:
            queue_key = RedisKeys.LOOP_EVENT_QUEUE_SERVER.format(
                app_name=self.app_name,
                loop_id=loop_id,
            )
        elif event.sender == LoopEventSender.CLIENT:
            queue_key = RedisKeys.LOOP_EVENT_QUEUE_CLIENT.format(
                app_name=self.app_name, loop_id=loop_id, event_type=event.type
            )
        else:
            raise ValueError(f"Invalid sender: {event.sender}")

        if not await self.rdb.exists(
            RedisKeys.LOOP_INITIAL_EVENT.format(app_name=self.app_name, loop_id=loop_id)
        ):
            await self.rdb.set(
                RedisKeys.LOOP_INITIAL_EVENT.format(
                    app_name=self.app_name, loop_id=loop_id
                ),
                event.to_string(),
            )

        await self.rdb.lpush(queue_key, event.to_string())
        await self.rdb.lpush(
            RedisKeys.LOOP_EVENT_HISTORY.format(
                app_name=self.app_name, loop_id=loop_id
            ),
            event.to_string(),
        )

        loop, _ = await self.get_or_create_loop(loop_id=loop_id)
        loop.last_event_at = datetime.now().timestamp()  # Use microsecond precision

        await self.update_loop(loop_id, loop)

    async def get_context_value(self, loop_id: str, key: str) -> Any:
        value_str = await self.rdb.get(
            RedisKeys.LOOP_CONTEXT.format(
                app_name=self.app_name, loop_id=loop_id, key=key
            )
        )
        if value_str:
            return cloudpickle.loads(value_str)
        else:
            return None

    async def set_context_value(self, loop_id: str, key: str, value: Any):
        try:
            value_str = cloudpickle.dumps(value)
        except BaseException as exc:
            raise ValueError(f"Failed to serialize value: {exc}") from exc

        await self.rdb.set(
            RedisKeys.LOOP_CONTEXT.format(
                app_name=self.app_name, loop_id=loop_id, key=key
            ),
            value_str,
        )

    async def pop_server_event(
        self,
        loop_id: str,
    ) -> dict[str, Any] | None:
        queue_key = RedisKeys.LOOP_EVENT_QUEUE_SERVER.format(
            app_name=self.app_name, loop_id=loop_id
        )
        event_str = await self.rdb.rpop(queue_key)
        if event_str:
            return json.loads(event_str.decode("utf-8"))
        else:
            return None

    async def pop_event(
        self,
        loop_id: str,
        event: "LoopEvent",
        sender: LoopEventSender = LoopEventSender.CLIENT,
    ) -> LoopEvent | None:
        if sender == LoopEventSender.SERVER:
            queue_key = RedisKeys.LOOP_EVENT_QUEUE_SERVER.format(
                app_name=self.app_name, loop_id=loop_id, event_type=event.type
            )
        elif sender == LoopEventSender.CLIENT:
            queue_key = RedisKeys.LOOP_EVENT_QUEUE_CLIENT.format(
                app_name=self.app_name, loop_id=loop_id, event_type=event.type
            )

        event_str = await self.rdb.rpop(queue_key)
        if event_str:
            return event.from_json(event_str.decode("utf-8"))
        else:
            return None

    async def get_initial_event(self, loop_id: str) -> "LoopEvent | None":
        """Get the initial event for a loop."""
        initial_event_str = await self.rdb.get(
            RedisKeys.LOOP_INITIAL_EVENT.format(app_name=self.app_name, loop_id=loop_id)
        )
        if initial_event_str:
            return LoopEvent.from_json(initial_event_str.decode("utf-8"))
        else:
            return None

    async def get_next_nonce(self, loop_id: str) -> int:
        """
        Get the next nonce for a loop using Redis INCR for atomic incrementing.
        """
        nonce_key = RedisKeys.LOOP_NONCE.format(app_name=self.app_name, loop_id=loop_id)
        return await self.rdb.incr(nonce_key)

    async def get_events_since(
        self, loop_id: str, since_timestamp: float
    ) -> dict[str, Any]:
        """
        Get events that occurred since the given timestamp.
        """
        all_events = await self.get_event_history(loop_id)
        return [event for event in all_events if event["timestamp"] >= since_timestamp]
