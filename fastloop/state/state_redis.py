import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any

import cloudpickle
import redis.asyncio as redis

from ..exceptions import LoopClaimError
from ..loop import LoopEvent
from ..types import LoopEventSender, LoopStatus, RedisConfig
from .state import LoopState, StateManager

KEY_PREFIX = "fastloop"


class RedisKeys:
    LOOP_INDEX = f"{KEY_PREFIX}:index"
    LOOP_EVENT_QUEUE_SERVER = f"{KEY_PREFIX}:events:{{loop_id}}:{{event_type}}:server"
    LOOP_EVENT_QUEUE_CLIENT = f"{KEY_PREFIX}:events:{{loop_id}}:{{event_type}}:client"
    LOOP_EVENT_HISTORY = f"{KEY_PREFIX}:event_history:{{loop_id}}"
    LOOP_STATE = f"{KEY_PREFIX}:state:{{loop_id}}"
    LOOP_CLAIM = f"{KEY_PREFIX}:claim:{{loop_id}}"
    LOOP_CONTEXT = f"{KEY_PREFIX}:context:{{loop_id}}:{{key}}"


class RedisStateManager(StateManager):
    def __init__(self, config: RedisConfig):
        self.rdb = redis.Redis(
            host=config.host,
            port=config.port,
            db=config.database,
            password=config.password,
            ssl=config.ssl,
        )

    async def get_or_create_loop(
        self,
        *,
        loop_name: str | None = None,
        loop_id: str | None = None,
        idle_timeout: float = 60.0,
    ) -> tuple[LoopState, bool]:
        if not loop_id:
            loop_id = str(uuid.uuid4())

        loop_str = await self.rdb.get(RedisKeys.LOOP_STATE.format(loop_id=loop_id))
        if loop_str:
            return LoopState.from_json(loop_str.decode("utf-8")), False

        loop = LoopState(
            loop_id=loop_id,
            loop_name=loop_name,
            idle_timeout=idle_timeout,
            last_event_at=int(datetime.now().timestamp()),
        )

        await self.rdb.set(RedisKeys.LOOP_STATE.format(loop_id=loop_id), loop.to_json())
        await self.rdb.sadd(RedisKeys.LOOP_INDEX, loop_id)

        return loop, True

    async def update_loop(self, loop_id: str, state: LoopState):
        await self.rdb.set(
            RedisKeys.LOOP_STATE.format(loop_id=loop_id), state.to_json()
        )

    @asynccontextmanager
    async def with_claim(self, loop_id: str):
        lock_key = RedisKeys.LOOP_CLAIM.format(loop_id=loop_id)
        lock = self.rdb.lock(name=lock_key, timeout=60, sleep=0.1, blocking_timeout=5)

        acquired = await lock.acquire()
        if not acquired:
            raise LoopClaimError(f"Could not acquire lock for loop {loop_id}")

        try:
            loop_str = await self.rdb.get(RedisKeys.LOOP_STATE.format(loop_id=loop_id))
            if loop_str:
                loop = LoopState.from_json(loop_str.decode("utf-8"))
                await self.rdb.set(
                    RedisKeys.LOOP_STATE.format(loop_id=loop_id), loop.to_json()
                )

            yield

        finally:
            await lock.release()

    async def get_all_loops(
        self,
        status: LoopStatus | None = None,
    ) -> list[LoopState]:
        loop_ids = [
            loop_id.decode("utf-8")
            for loop_id in await self.rdb.smembers(RedisKeys.LOOP_INDEX)
        ]

        all = []
        for loop_id in loop_ids:
            loop_state_str = await self.rdb.get(
                RedisKeys.LOOP_STATE.format(loop_id=loop_id)
            )

            if not loop_state_str:
                await self.rdb.srem(RedisKeys.LOOP_INDEX, loop_id)
                continue

            try:
                loop_state = LoopState.from_json(loop_state_str.decode("utf-8"))
            except TypeError:
                await self.rdb.srem(RedisKeys.LOOP_INDEX, loop_id)
                continue

            if status and loop_state.status != status:
                continue

            all.append(loop_state)

        return all

    async def get_event_history(self, loop_id: str) -> list["LoopEvent"]:
        event_history = await self.rdb.lrange(
            RedisKeys.LOOP_EVENT_HISTORY.format(loop_id=loop_id), 0, -1
        )
        return [LoopEvent.from_json(event.decode("utf-8")) for event in event_history]

    async def push_event(self, loop_id: str, event: "LoopEvent"):
        if event.sender == LoopEventSender.SERVER:
            queue_key = RedisKeys.LOOP_EVENT_QUEUE_SERVER.format(
                loop_id=loop_id, event_type=event.type
            )
        elif event.sender == LoopEventSender.CLIENT:
            queue_key = RedisKeys.LOOP_EVENT_QUEUE_CLIENT.format(
                loop_id=loop_id, event_type=event.type
            )
        else:
            raise ValueError(f"Invalid event sender: {event.sender}")

        await self.rdb.lpush(queue_key, event.to_json())
        await self.rdb.lpush(
            RedisKeys.LOOP_EVENT_HISTORY.format(loop_id=loop_id), event.to_json()
        )

        loop, _ = await self.get_or_create_loop(loop_id=loop_id)
        loop.last_event_at = int(datetime.now().timestamp())

        await self.update_loop(loop_id, loop)

    async def get_context_value(self, loop_id: str, key: str) -> Any:
        value_str = await self.rdb.get(
            RedisKeys.LOOP_CONTEXT.format(loop_id=loop_id, key=key)
        )
        print("get key: ", RedisKeys.LOOP_CONTEXT.format(loop_id=loop_id, key=key))
        if value_str:
            return cloudpickle.loads(value_str)
        else:
            return None

    async def set_context_value(self, loop_id: str, key: str, value: Any):
        try:
            value_str = cloudpickle.dumps(value)
        except BaseException as exc:
            raise ValueError(f"Failed to serialize value: {exc}") from exc

        print("set key: ", RedisKeys.LOOP_CONTEXT.format(loop_id=loop_id, key=key))
        await self.rdb.set(
            RedisKeys.LOOP_CONTEXT.format(loop_id=loop_id, key=key), value_str
        )

    async def pop_event(
        self,
        loop_id: str,
        event: "LoopEvent",
        sender: LoopEventSender = LoopEventSender.CLIENT,
    ) -> LoopEvent | None:
        if sender == LoopEventSender.SERVER:
            queue_key = RedisKeys.LOOP_EVENT_QUEUE_SERVER.format(
                loop_id=loop_id, event_type=event.type
            )
        elif sender == LoopEventSender.CLIENT:
            queue_key = RedisKeys.LOOP_EVENT_QUEUE_CLIENT.format(
                loop_id=loop_id, event_type=event.type
            )
        else:
            raise ValueError(f"Invalid event sender: {sender}")

        event_str = await self.rdb.rpop(queue_key)
        if event_str:
            return event.from_json(event_str.decode("utf-8"))
        else:
            return None
