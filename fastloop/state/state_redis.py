import json
import threading
import time
import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from queue import Queue
from typing import TYPE_CHECKING, Any, cast

import cloudpickle  # type: ignore
import redis.asyncio as redis

if TYPE_CHECKING:
    from redis.asyncio.client import PubSub

from ..constants import (
    CLAIM_LOCK_BLOCKING_TIMEOUT_S,
    CLAIM_LOCK_SLEEP_S,
)
from ..exceptions import LoopClaimError, LoopNotFoundError
from ..loop import LoopEvent
from ..types import E, LoopEventSender, LoopStatus, RedisConfig
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
    LOOP_EVENT_CHANNEL = f"{KEY_PREFIX}:{{app_name}}:events:{{loop_id}}:notify"
    LOOP_WAKE_KEY = f"{KEY_PREFIX}:{{app_name}}:wake:{{loop_id}}"
    LOOP_WAKE_INDEX = f"{KEY_PREFIX}:{{app_name}}:wake_index"
    LOOP_MAPPING = f"{KEY_PREFIX}:{{app_name}}:mapping:{{external_ref_id}}"


class RedisStateManager(StateManager):
    def __init__(
        self,
        *,
        app_name: str,
        config: RedisConfig,
        wake_queue: Queue[str],
    ):
        self.app_name = app_name
        self.config: RedisConfig = config
        self.rdb: redis.Redis = redis.Redis(
            host=config.host,
            port=config.port,
            db=config.database,
            password=config.password,
            ssl=config.ssl,
        )
        self.pubsub_rdb: redis.Redis = redis.Redis(
            host=config.host,
            port=config.port,
            db=config.database,
            password=config.password,
            ssl=config.ssl,
        )

        self.wake_queue: Queue[str] = wake_queue
        if self.wake_queue:
            self.wake_thread = threading.Thread(
                target=self._run_wake_monitoring, daemon=True
            )
            self.wake_thread.start()

    def _run_wake_monitoring(self):
        import redis

        rdb = redis.Redis(
            host=self.config.host,
            port=self.config.port,
            db=self.config.database,
            password=self.config.password,
            ssl=self.config.ssl,
        )

        rdb.config_set("notify-keyspace-events", "Ex")  # type: ignore
        self._check_missed_wake_events_sync(rdb)  # type: ignore

        pubsub: PubSub = rdb.pubsub()  # type: ignore
        pubsub.psubscribe("__keyevent@*__:expired")  # type: ignore

        for message in pubsub.listen():  # type: ignore
            if message["type"] == "pmessage":
                key: str = message["data"].decode("utf-8")  # type: ignore
                if f":{self.app_name}:wake:" in key:
                    loop_id: str = key.split(":")[-1]  # type: ignore

                    if self.wake_queue:
                        self.wake_queue.put(loop_id)  # type: ignore

                    rdb.srem(
                        RedisKeys.LOOP_WAKE_INDEX.format(app_name=self.app_name),
                        loop_id,  # type: ignore
                    )

    def _check_missed_wake_events_sync(self, rdb: redis.Redis):
        wake_index: list[bytes] = rdb.smembers(  # type: ignore
            RedisKeys.LOOP_WAKE_INDEX.format(app_name=self.app_name)
        )

        for wake_key_bytes in wake_index:
            wake_key = wake_key_bytes.decode("utf-8")

            if not rdb.exists(wake_key):
                loop_id = wake_key.split(":")[-1]

                if self.wake_queue:
                    self.wake_queue.put(loop_id)

                rdb.srem(
                    RedisKeys.LOOP_WAKE_INDEX.format(app_name=self.app_name), wake_key
                )

    async def set_loop_mapping(self, external_ref_id: str, loop_id: str):
        await self.rdb.set(
            RedisKeys.LOOP_MAPPING.format(
                app_name=self.app_name, external_ref_id=external_ref_id
            ),
            loop_id,
        )

    async def get_loop_mapping(self, external_ref_id: str) -> str | None:
        return await self.rdb.get(
            RedisKeys.LOOP_MAPPING.format(
                app_name=self.app_name, external_ref_id=external_ref_id
            )
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
        current_function_path: str = "",
    ) -> tuple[LoopState, bool]:
        if loop_id:
            loop_str = await self.rdb.get(
                RedisKeys.LOOP_STATE.format(app_name=self.app_name, loop_id=loop_id)
            )
            if loop_str:
                return LoopState.from_json(loop_str.decode("utf-8")), False
            else:
                raise LoopNotFoundError(f"Loop {loop_id} not found")

        if not current_function_path:
            raise ValueError("Current function is required")

        loop_id = str(uuid.uuid4())
        loop = LoopState(
            loop_id=loop_id,
            loop_name=loop_name,
            current_function_path=current_function_path,
        )

        await self.rdb.set(
            RedisKeys.LOOP_STATE.format(app_name=self.app_name, loop_id=loop_id),
            loop.to_string(),
        )

        await self.rdb.sadd(
            RedisKeys.LOOP_INDEX.format(app_name=self.app_name), loop_id
        )  # type: ignore

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
    async def with_claim(self, loop_id: str) -> AsyncGenerator[None, None]:  # type: ignore
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
            loop_id.decode("utf-8")  # type: ignore
            for loop_id in await self.rdb.smembers(  # type: ignore
                RedisKeys.LOOP_INDEX.format(app_name=self.app_name)
            )  # type: ignore
        }

    async def get_all_loops(
        self,
        status: LoopStatus | None = None,
    ) -> list[LoopState]:
        loop_ids: list[str] = [
            loop_id.decode("utf-8")  # type: ignore
            for loop_id in await self.rdb.smembers(  # type: ignore
                RedisKeys.LOOP_INDEX.format(app_name=self.app_name)
            )  # type: ignore
        ]

        all: list[LoopState] = []
        for loop_id in loop_ids:
            loop_state_str = await self.rdb.get(
                RedisKeys.LOOP_STATE.format(app_name=self.app_name, loop_id=loop_id)
            )

            if not loop_state_str:
                await self.rdb.srem(
                    RedisKeys.LOOP_INDEX.format(app_name=self.app_name), loop_id
                )  # type: ignore
                continue

            try:
                loop_state = LoopState.from_json(loop_state_str.decode("utf-8"))
            except TypeError:
                await self.rdb.srem(
                    RedisKeys.LOOP_INDEX.format(app_name=self.app_name), loop_id
                )  # type: ignore
                continue

            if status and loop_state.status != status:
                continue

            all.append(loop_state)

        return all

    async def get_event_history(self, loop_id: str) -> list[dict[str, Any]]:
        event_history: list[bytes] | None = await self.rdb.lrange(  # type: ignore
            RedisKeys.LOOP_EVENT_HISTORY.format(
                app_name=self.app_name, loop_id=loop_id
            ),
            0,
            -1,
        )  # type: ignore
        events: list[dict[str, Any]] = []
        for event in event_history:  # type: ignore
            try:
                events.append(json.loads(event.decode("utf-8")))  # type: ignore
            except json.JSONDecodeError:
                continue

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

        initial_event_key = RedisKeys.LOOP_INITIAL_EVENT.format(
            app_name=self.app_name, loop_id=loop_id
        )
        history_key = RedisKeys.LOOP_EVENT_HISTORY.format(
            app_name=self.app_name, loop_id=loop_id
        )
        channel_key = RedisKeys.LOOP_EVENT_CHANNEL.format(
            app_name=self.app_name, loop_id=loop_id
        )

        event_str = event.to_string()

        async with self.rdb.pipeline(transaction=True) as pipe:
            pipe.exists(initial_event_key)
            (exists_result,) = await pipe.execute()

            if not exists_result:
                pipe.set(initial_event_key, event_str)

            pipe.lpush(queue_key, event_str)
            pipe.lpush(history_key, event_str)

            if event.sender == LoopEventSender.SERVER:
                pipe.publish(channel_key, "new_event")  # type: ignore

            await pipe.execute()

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
            value_str: bytes = cloudpickle.dumps(value)  # type: ignore
        except BaseException as exc:
            raise ValueError(f"Failed to serialize value: {exc}") from exc

        await self.rdb.set(
            RedisKeys.LOOP_CONTEXT.format(
                app_name=self.app_name, loop_id=loop_id, key=key
            ),
            value_str,
        )

    async def delete_context_value(self, loop_id: str, key: str):
        await self.rdb.delete(
            RedisKeys.LOOP_CONTEXT.format(
                app_name=self.app_name, loop_id=loop_id, key=key
            )
        )

    async def pop_server_event(
        self,
        loop_id: str,
    ) -> dict[str, Any] | None:
        queue_key = RedisKeys.LOOP_EVENT_QUEUE_SERVER.format(
            app_name=self.app_name, loop_id=loop_id
        )
        event_str: bytes | None = await self.rdb.rpop(queue_key)  # type: ignore
        if event_str:
            return json.loads(event_str.decode("utf-8"))
        else:
            return None

    async def pop_event(
        self,
        loop_id: str,
        event: type[E],
        sender: LoopEventSender = LoopEventSender.CLIENT,
    ) -> E | None:
        if sender == LoopEventSender.SERVER:
            queue_key = RedisKeys.LOOP_EVENT_QUEUE_SERVER.format(
                app_name=self.app_name, loop_id=loop_id, event_type=event.type
            )
        elif sender == LoopEventSender.CLIENT:
            queue_key = RedisKeys.LOOP_EVENT_QUEUE_CLIENT.format(
                app_name=self.app_name, loop_id=loop_id, event_type=event.type
            )

        event_str: bytes | None = await self.rdb.rpop(queue_key)  # type: ignore
        if event_str:
            return cast(E, event.from_json(event_str.decode("utf-8")))  # noqa
        else:
            return None

    async def set_wake_time(self, loop_id: str, timestamp: float) -> None:
        ttl = int(timestamp - time.time())
        if ttl <= 0:
            raise ValueError("Timestamp is in the past")

        wake_key = RedisKeys.LOOP_WAKE_KEY.format(
            app_name=self.app_name, loop_id=loop_id
        )
        wake_index = RedisKeys.LOOP_WAKE_INDEX.format(app_name=self.app_name)

        await self.rdb.set(wake_key, "wake", ex=ttl)
        await self.rdb.sadd(wake_index, wake_key)  # pyright: ignore

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
    ) -> list[dict[str, Any]]:
        """
        Get events that occurred since the given timestamp.
        """
        all_events = await self.get_event_history(loop_id)
        return [event for event in all_events if event["timestamp"] >= since_timestamp]

    async def subscribe_to_events(self, loop_id: str) -> Any:
        """Subscribe to event notifications for a specific loop"""
        pubsub: PubSub = self.pubsub_rdb.pubsub()  # type: ignore
        await pubsub.subscribe(  # type: ignore
            RedisKeys.LOOP_EVENT_CHANNEL.format(app_name=self.app_name, loop_id=loop_id)
        )
        return pubsub

    async def wait_for_event_notification(
        self, pubsub: Any, timeout: float | None = None
    ) -> bool:
        """Wait for an event notification or timeout"""
        try:
            message = await pubsub.get_message(timeout=timeout)
            return bool(message and message["type"] == "message")
        except TimeoutError:
            return False
