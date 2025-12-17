import asyncio
import json
import threading
import time
import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager, suppress
from queue import Queue
from typing import TYPE_CHECKING, Any, cast

import cloudpickle  # type: ignore
import redis.asyncio as redis

if TYPE_CHECKING:
    from redis.asyncio.client import PubSub

from ..constants import (
    CLAIM_LOCK_BLOCKING_TIMEOUT_S,
    CLAIM_LOCK_SLEEP_S,
    LEASE_HEARTBEAT_INTERVAL_S,
    LEASE_TTL_S,
    MAX_EVENT_HISTORY,
)
from ..exceptions import (
    LoopClaimError,
    LoopNotFoundError,
    TaskNotFoundError,
    WorkflowNotFoundError,
)
from ..logging import setup_logger
from ..models import LoopEvent, LoopState, TaskState, WorkflowState
from ..scheduler import Schedule
from ..types import E, LoopEventSender, LoopStatus, RedisConfig, TaskStatus
from .state import StateManager

logger = setup_logger(__name__)

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
    LOOP_WAKE_SCHEDULE = f"{KEY_PREFIX}:{{app_name}}:wake_schedule"
    LOOP_MAPPING = f"{KEY_PREFIX}:{{app_name}}:mapping:{{external_ref_id}}"
    LOOP_CONNECTION_INDEX = f"{KEY_PREFIX}:{{app_name}}:connection_index:{{loop_id}}"
    LOOP_CONNECTION_KEY = (
        f"{KEY_PREFIX}:{{app_name}}:connection:{{loop_id}}:{{connection_id}}"
    )
    LOOP_APP_START_LOCK = f"{KEY_PREFIX}:{{app_name}}:app_start_lock:{{loop_id}}"
    WORKFLOW_INDEX = f"{KEY_PREFIX}:{{app_name}}:workflow_index"
    WORKFLOW_STATE = f"{KEY_PREFIX}:{{app_name}}:workflow:{{workflow_run_id}}"
    WORKFLOW_CLAIM = f"{KEY_PREFIX}:{{app_name}}:workflow_claim:{{workflow_run_id}}"
    WORKFLOW_WAKE_KEY = f"{KEY_PREFIX}:{{app_name}}:workflow_wake:{{workflow_run_id}}"
    WORKFLOW_WAKE_SCHEDULE = f"{KEY_PREFIX}:{{app_name}}:workflow_wake_schedule"
    WORKFLOW_BLOCK_OUTPUT = (
        f"{KEY_PREFIX}:{{app_name}}:workflow_block_output:{{workflow_run_id}}"
    )
    TASK_INDEX = f"{KEY_PREFIX}:{{app_name}}:task_index"
    TASK_STATE = f"{KEY_PREFIX}:{{app_name}}:task:{{task_id}}"
    TASK_CLAIM = f"{KEY_PREFIX}:{{app_name}}:task_claim:{{task_id}}"
    TASK_RESULT = f"{KEY_PREFIX}:{{app_name}}:task_result:{{task_id}}"
    SCHEDULE = f"{KEY_PREFIX}:{{app_name}}:schedule:{{schedule_id}}"
    SCHEDULE_QUEUE = f"{KEY_PREFIX}:{{app_name}}:schedule_queue"


WAKE_RECONCILIATION_INTERVAL_S = 1.0

LUA_CONDITIONAL_EXPIRE = """
local current = redis.call('GET', KEYS[1])
if current == ARGV[1] then
    -- We still own it, just extend TTL
    return redis.call('EXPIRE', KEYS[1], ARGV[2])
elseif current == false then
    -- Key expired, try to re-acquire (NX ensures we don't steal from another replica)
    local acquired = redis.call('SET', KEYS[1], ARGV[1], 'NX', 'EX', ARGV[2])
    if acquired then
        return 1
    else
        return 0
    end
else
    -- Someone else owns it
    return 0
end
"""

LUA_CONDITIONAL_DELETE = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('DEL', KEYS[1])
end
return 0
"""


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

        self._script_conditional_expire = self.rdb.register_script(
            LUA_CONDITIONAL_EXPIRE
        )
        self._script_conditional_delete = self.rdb.register_script(
            LUA_CONDITIONAL_DELETE
        )

        self.wake_queue: Queue[str] = wake_queue
        self._stop_wake_monitor = threading.Event()
        self.wake_thread: threading.Thread | None = None

        if self.wake_queue:
            self.wake_thread = threading.Thread(
                target=self._run_wake_monitoring, daemon=True
            )
            self.wake_thread.start()

    def stop(self):
        self._stop_wake_monitor.set()
        if self.wake_thread and self.wake_thread.is_alive():
            self.wake_thread.join(timeout=2.0)

    def _run_wake_monitoring(self):
        """Background thread for reliable wake scheduling using ZSET + periodic reconciliation.

        This thread uses two mechanisms for reliability:
        1. Redis keyspace notifications for immediate wake on TTL key expiry
        2. Periodic ZSET reconciliation as a fallback

        The thread will automatically reconnect on Redis connection errors.
        """
        import redis as sync_redis

        from ..logging import setup_logger

        logger = setup_logger(__name__)

        while not self._stop_wake_monitor.is_set():
            rdb = None
            pubsub = None

            try:
                rdb = sync_redis.Redis(
                    host=self.config.host,
                    port=self.config.port,
                    db=self.config.database,
                    password=self.config.password,
                    ssl=self.config.ssl,
                )

                with suppress(sync_redis.exceptions.ResponseError):
                    rdb.config_set("notify-keyspace-events", "Ex")

                logger.info("Wake monitoring thread started, processing due wakes")
                due_count = self._process_due_wakes(rdb)
                if due_count > 0:
                    logger.info(
                        "Processed due wakes on startup",
                        extra={"count": due_count},
                    )

                pubsub = rdb.pubsub()
                pubsub.psubscribe("__keyevent@*__:expired")
                last_reconciliation = time.time()

                while not self._stop_wake_monitor.is_set():
                    try:
                        message = pubsub.get_message(timeout=0.1)

                        if message and message["type"] == "pmessage":
                            try:
                                key = message["data"].decode("utf-8")
                                if f":{self.app_name}:wake:" in key:
                                    loop_id = key.split(":")[-1]
                                    logger.info(
                                        "Loop wake key expired",
                                        extra={"loop_id": loop_id},
                                    )
                                    self._queue_wake(rdb, loop_id)
                                elif f":{self.app_name}:workflow_wake:" in key:
                                    workflow_run_id = key.split(":")[-1]
                                    logger.info(
                                        "Workflow wake key expired",
                                        extra={"workflow_run_id": workflow_run_id},
                                    )
                                    self._queue_wake(rdb, workflow_run_id)
                            except Exception as e:
                                logger.error(f"Error processing wake notification: {e}")

                        now = time.time()
                        if now - last_reconciliation >= WAKE_RECONCILIATION_INTERVAL_S:
                            due_count = self._process_due_wakes(rdb)
                            if due_count > 0:
                                logger.info(
                                    "Wake reconciliation processed due wakes",
                                    extra={
                                        "count": due_count,
                                        "queue_size": self.wake_queue.qsize(),
                                    },
                                )
                            last_reconciliation = now

                    except sync_redis.exceptions.ConnectionError as e:
                        logger.warning(
                            f"Redis connection error in wake monitor inner loop: {e}, reconnecting"
                        )
                        break  # Break inner loop to reconnect

            except sync_redis.exceptions.ConnectionError as e:
                logger.warning(
                    f"Redis connection error in wake monitor: {e}, retrying in 5s"
                )
                time.sleep(5)
            except Exception as e:
                logger.error(f"Wake monitoring thread error: {e}, retrying in 5s")
                time.sleep(5)
            finally:
                if pubsub:
                    with suppress(Exception):
                        pubsub.close()
                if rdb:
                    with suppress(Exception):
                        rdb.close()

        logger.info("Wake monitoring thread stopped")

    def _process_due_wakes(self, rdb) -> int:
        """Process all wakes with score <= now. Returns count processed."""
        now = time.time()
        processed = 0

        loop_schedule_key = RedisKeys.LOOP_WAKE_SCHEDULE.format(app_name=self.app_name)
        due_loop_wakes: list[bytes] = rdb.zrangebyscore(loop_schedule_key, "-inf", now)
        for loop_id_bytes in due_loop_wakes:
            loop_id = loop_id_bytes.decode("utf-8")
            if rdb.zrem(loop_schedule_key, loop_id):
                logger.info(
                    "Due loop wake found, queuing",
                    extra={"loop_id": loop_id},
                )
                self.wake_queue.put(loop_id)
                processed += 1

        workflow_schedule_key = RedisKeys.WORKFLOW_WAKE_SCHEDULE.format(
            app_name=self.app_name
        )
        due_workflow_wakes: list[bytes] = rdb.zrangebyscore(
            workflow_schedule_key, "-inf", now
        )
        for workflow_run_id_bytes in due_workflow_wakes:
            workflow_run_id = workflow_run_id_bytes.decode("utf-8")
            if rdb.zrem(workflow_schedule_key, workflow_run_id):
                logger.info(
                    "Due workflow wake found, queuing",
                    extra={"workflow_run_id": workflow_run_id},
                )
                self.wake_queue.put(f"workflow:{workflow_run_id}")
                processed += 1

        return processed

    def _queue_wake(self, rdb, loop_id: str) -> bool:
        """Remove loop/workflow from schedule and queue wake. Returns True if queued."""
        loop_schedule_key = RedisKeys.LOOP_WAKE_SCHEDULE.format(app_name=self.app_name)
        if rdb.zrem(loop_schedule_key, loop_id):
            self.wake_queue.put(loop_id)
            return True

        workflow_schedule_key = RedisKeys.WORKFLOW_WAKE_SCHEDULE.format(
            app_name=self.app_name
        )
        if rdb.zrem(workflow_schedule_key, loop_id):
            self.wake_queue.put(f"workflow:{loop_id}")
            return True

        return False

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

        state_key = RedisKeys.LOOP_STATE.format(app_name=self.app_name, loop_id=loop_id)

        if status == LoopStatus.STOPPED:
            schedule_key = RedisKeys.LOOP_WAKE_SCHEDULE.format(app_name=self.app_name)
            wake_key = RedisKeys.LOOP_WAKE_KEY.format(
                app_name=self.app_name, loop_id=loop_id
            )
            async with self.rdb.pipeline(transaction=True) as pipe:
                pipe.set(state_key, loop.to_string())
                pipe.zrem(schedule_key, loop_id)
                pipe.delete(wake_key)
                await pipe.execute()
        else:
            await self.rdb.set(state_key, loop.to_string())

        return loop

    async def _acquire_lease(self, lease_key: str, owner_id: str) -> bool:
        start = time.time()
        while time.time() - start < CLAIM_LOCK_BLOCKING_TIMEOUT_S:
            acquired = await self.rdb.set(lease_key, owner_id, nx=True, ex=LEASE_TTL_S)
            if acquired:
                return True
            await asyncio.sleep(CLAIM_LOCK_SLEEP_S)
        return False

    @asynccontextmanager
    async def _with_lease(
        self,
        lease_key: str,
        entity_type: str,
        entity_id: str,
    ) -> AsyncGenerator[None, None]:
        """Shared lease management with heartbeat and automatic cleanup."""
        owner_id = str(uuid.uuid4())
        max_retries = 3

        acquired = await self._acquire_lease(lease_key, owner_id)
        if not acquired:
            raise LoopClaimError(
                f"Could not acquire lease for {entity_type} {entity_id}"
            )

        logger.debug(
            f"{entity_type.title()} claim acquired",
            extra={f"{entity_type}_id": entity_id},
        )

        stop_event = asyncio.Event()
        claim_lost = asyncio.Event()
        current_task = asyncio.current_task()

        async def heartbeat():
            consecutive_failures = 0
            while not stop_event.is_set():
                try:
                    await asyncio.wait_for(
                        stop_event.wait(), timeout=LEASE_HEARTBEAT_INTERVAL_S
                    )
                    return
                except TimeoutError:
                    pass

                try:
                    if await self._script_conditional_expire(
                        keys=[lease_key], args=[owner_id, LEASE_TTL_S]
                    ):
                        consecutive_failures = 0
                    else:
                        logger.warning(
                            f"{entity_type.title()} claim stolen",
                            extra={f"{entity_type}_id": entity_id},
                        )
                        claim_lost.set()
                        if current_task:
                            current_task.cancel()
                        return
                except Exception as e:
                    consecutive_failures += 1
                    if consecutive_failures >= max_retries:
                        logger.error(
                            f"{entity_type.title()} heartbeat failed",
                            extra={f"{entity_type}_id": entity_id, "error": str(e)},
                        )
                        claim_lost.set()
                        if current_task:
                            current_task.cancel()
                        return

        heartbeat_task = asyncio.create_task(heartbeat())

        try:
            yield
            if claim_lost.is_set():
                raise LoopClaimError(f"Claim lost for {entity_type} {entity_id}")
        finally:
            stop_event.set()
            heartbeat_task.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat_task
            await self._script_conditional_delete(keys=[lease_key], args=[owner_id])
            logger.debug(
                f"{entity_type.title()} claim released",
                extra={f"{entity_type}_id": entity_id},
            )

    @asynccontextmanager
    async def with_claim(self, loop_id: str) -> AsyncGenerator[None, None]:  # type: ignore
        lease_key = RedisKeys.LOOP_CLAIM.format(app_name=self.app_name, loop_id=loop_id)
        async with self._with_lease(lease_key, "loop", loop_id):
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

    async def has_claim(self, loop_id: str) -> bool:
        result = await self.rdb.get(
            RedisKeys.LOOP_CLAIM.format(app_name=self.app_name, loop_id=loop_id)
        )
        return result is not None

    async def try_claim_loop_recovery(self, loop_id: str) -> bool:
        """Atomically claim right to recover an orphaned loop. Returns True if won."""
        claim_key = f"{RedisKeys.LOOP_CLAIM.format(app_name=self.app_name, loop_id=loop_id)}:recovery"
        acquired = await self.rdb.set(claim_key, "1", nx=True, ex=60)
        return acquired is not None

    async def get_all_loop_ids(self) -> set[str]:
        members = await self.rdb.smembers(
            RedisKeys.LOOP_INDEX.format(app_name=self.app_name)
        )
        return {m.decode("utf-8") for m in members}

    async def get_all_loops(self, status: LoopStatus | None = None) -> list[LoopState]:
        loop_ids = list(await self.get_all_loop_ids())
        if not loop_ids:
            return []

        keys = [
            RedisKeys.LOOP_STATE.format(app_name=self.app_name, loop_id=lid)
            for lid in loop_ids
        ]
        values = await self.rdb.mget(keys)

        results: list[LoopState] = []
        stale_ids: list[str] = []

        for loop_id, val in zip(loop_ids, values, strict=True):
            if not val:
                stale_ids.append(loop_id)
                continue
            try:
                loop_state = LoopState.from_json(val.decode("utf-8"))
            except (TypeError, json.JSONDecodeError):
                stale_ids.append(loop_id)
                continue
            if status and loop_state.status != status:
                continue
            results.append(loop_state)

        if stale_ids:
            index_key = RedisKeys.LOOP_INDEX.format(app_name=self.app_name)
            await self.rdb.srem(index_key, *stale_ids)

        return results

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
            pipe.ltrim(history_key, 0, MAX_EVENT_HISTORY - 1)

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
        """Schedule a wake time. Uses ZSET (source of truth) + TTL key (fast notification)."""
        if timestamp <= time.time():
            raise ValueError("Timestamp is in the past")

        schedule_key = RedisKeys.LOOP_WAKE_SCHEDULE.format(app_name=self.app_name)
        wake_key = RedisKeys.LOOP_WAKE_KEY.format(
            app_name=self.app_name, loop_id=loop_id
        )
        ttl_ms = max(1, int((timestamp - time.time()) * 1000))

        async with self.rdb.pipeline(transaction=True) as pipe:
            pipe.zadd(schedule_key, {loop_id: timestamp})
            pipe.set(wake_key, "1", px=ttl_ms)
            await pipe.execute()

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
        return [
            event
            for event in all_events
            if float(event["timestamp"]) >= since_timestamp
        ]

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

    async def register_client_connection(
        self, loop_id: str, connection_id: str
    ) -> None:
        """Register an active SSE client connection for a loop using TTL keys"""
        connection_key = RedisKeys.LOOP_CONNECTION_KEY.format(
            app_name=self.app_name, loop_id=loop_id, connection_id=connection_id
        )
        index_key = RedisKeys.LOOP_CONNECTION_INDEX.format(
            app_name=self.app_name, loop_id=loop_id
        )

        # Set TTL key for the connection (expires in 30 seconds)
        await self.rdb.set(connection_key, "active", ex=30)
        # Add to index
        await self.rdb.sadd(index_key, connection_id)

    async def unregister_client_connection(
        self, loop_id: str, connection_id: str
    ) -> None:
        """Unregister an SSE client connection for a loop"""
        connection_key = RedisKeys.LOOP_CONNECTION_KEY.format(
            app_name=self.app_name, loop_id=loop_id, connection_id=connection_id
        )
        index_key = RedisKeys.LOOP_CONNECTION_INDEX.format(
            app_name=self.app_name, loop_id=loop_id
        )

        # Remove the TTL key and from index
        await self.rdb.delete(connection_key)
        await self.rdb.srem(index_key, connection_id)

    async def get_active_client_count(self, loop_id: str) -> int:
        """Get the number of active SSE client connections for a loop"""
        index_key = RedisKeys.LOOP_CONNECTION_INDEX.format(
            app_name=self.app_name, loop_id=loop_id
        )

        # Get all connection IDs from index
        connection_ids = await self.rdb.smembers(index_key)
        if not connection_ids:
            return 0

        # Check which connections still have active TTL keys
        active_count = 0
        pipeline = self.rdb.pipeline()

        for connection_id in connection_ids:
            connection_key = RedisKeys.LOOP_CONNECTION_KEY.format(
                app_name=self.app_name,
                loop_id=loop_id,
                connection_id=connection_id.decode(),
            )
            pipeline.exists(connection_key)

        results = await pipeline.execute()

        # Clean up expired connections from index and count active ones
        expired_connections = []
        for i, exists in enumerate(results):
            connection_id = list(connection_ids)[i].decode()
            if exists:
                active_count += 1
            else:
                expired_connections.append(connection_id)

        # Remove expired connections from index
        if expired_connections:
            await self.rdb.srem(index_key, *expired_connections)

        return active_count

    async def refresh_client_connection(self, loop_id: str, connection_id: str) -> None:
        """Refresh the TTL for an active SSE client connection"""
        connection_key = RedisKeys.LOOP_CONNECTION_KEY.format(
            app_name=self.app_name, loop_id=loop_id, connection_id=connection_id
        )
        # Refresh TTL to 30 seconds
        await self.rdb.expire(connection_key, 30)

    async def try_acquire_app_start_lock(self, loop_id: str) -> bool:
        """Try to acquire an app start lock for a loop using SETNX with TTL."""
        lock_key = RedisKeys.LOOP_APP_START_LOCK.format(
            app_name=self.app_name, loop_id=loop_id
        )
        acquired = await self.rdb.set(lock_key, "1", nx=True, ex=60)
        return bool(acquired)

    async def release_app_start_lock(self, loop_id: str) -> None:
        """Release the app start lock for a loop."""
        lock_key = RedisKeys.LOOP_APP_START_LOCK.format(
            app_name=self.app_name, loop_id=loop_id
        )
        await self.rdb.delete(lock_key)

    async def get_workflow(self, workflow_run_id: str) -> WorkflowState:
        workflow_str = await self.rdb.get(
            RedisKeys.WORKFLOW_STATE.format(
                app_name=self.app_name, workflow_run_id=workflow_run_id
            )
        )
        if workflow_str:
            return WorkflowState.from_json(workflow_str.decode("utf-8"))
        else:
            raise WorkflowNotFoundError(f"Workflow run {workflow_run_id} not found")

    async def get_or_create_workflow(
        self,
        *,
        workflow_name: str | None = None,
        workflow_run_id: str | None = None,
        blocks: list[dict[str, Any]],
    ) -> tuple[WorkflowState, bool]:
        if workflow_run_id:
            workflow_str = await self.rdb.get(
                RedisKeys.WORKFLOW_STATE.format(
                    app_name=self.app_name, workflow_run_id=workflow_run_id
                )
            )
            if workflow_str:
                return WorkflowState.from_json(workflow_str.decode("utf-8")), False
        else:
            workflow_run_id = str(uuid.uuid4())

        workflow = WorkflowState(
            workflow_run_id=workflow_run_id,
            workflow_name=workflow_name,
            blocks=blocks,
        )

        await self.rdb.set(
            RedisKeys.WORKFLOW_STATE.format(
                app_name=self.app_name, workflow_run_id=workflow_run_id
            ),
            workflow.to_string(),
        )

        await self.rdb.sadd(
            RedisKeys.WORKFLOW_INDEX.format(app_name=self.app_name), workflow_run_id
        )

        return workflow, True

    async def update_workflow(self, workflow_run_id: str, state: WorkflowState) -> None:
        await self.rdb.set(
            RedisKeys.WORKFLOW_STATE.format(
                app_name=self.app_name, workflow_run_id=workflow_run_id
            ),
            state.to_string(),
        )

    async def update_workflow_status(
        self, workflow_run_id: str, status: LoopStatus
    ) -> WorkflowState:
        workflow = await self.get_workflow(workflow_run_id=workflow_run_id)
        workflow.status = status
        await self.rdb.set(
            RedisKeys.WORKFLOW_STATE.format(
                app_name=self.app_name, workflow_run_id=workflow_run_id
            ),
            workflow.to_string(),
        )
        return workflow

    async def update_workflow_block_index(
        self, workflow_run_id: str, index: int, payload: dict[str, Any] | None = None
    ) -> None:
        workflow = await self.get_workflow(workflow_run_id=workflow_run_id)
        workflow.current_block_index = index
        workflow.next_payload = payload
        await self.rdb.set(
            RedisKeys.WORKFLOW_STATE.format(
                app_name=self.app_name, workflow_run_id=workflow_run_id
            ),
            workflow.to_string(),
        )

    async def get_workflow_blocks(self, workflow_run_id: str) -> list[dict[str, Any]]:
        workflow = await self.get_workflow(workflow_run_id=workflow_run_id)
        return workflow.blocks

    async def workflow_has_claim(self, workflow_run_id: str) -> bool:
        result = await self.rdb.get(
            RedisKeys.WORKFLOW_CLAIM.format(
                app_name=self.app_name, workflow_run_id=workflow_run_id
            )
        )
        return result is not None

    async def try_claim_workflow_recovery(self, workflow_run_id: str) -> bool:
        """Atomically claim right to recover an orphaned workflow. Returns True if won."""
        claim_key = f"{RedisKeys.WORKFLOW_CLAIM.format(app_name=self.app_name, workflow_run_id=workflow_run_id)}:recovery"
        acquired = await self.rdb.set(claim_key, "1", nx=True, ex=60)
        return acquired is not None

    @asynccontextmanager
    async def with_workflow_claim(
        self, workflow_run_id: str
    ) -> AsyncGenerator[None, None]:
        lease_key = RedisKeys.WORKFLOW_CLAIM.format(
            app_name=self.app_name, workflow_run_id=workflow_run_id
        )
        async with self._with_lease(lease_key, "workflow", workflow_run_id):
            yield

    async def get_all_workflows(
        self, status: LoopStatus | None = None
    ) -> list[WorkflowState]:
        workflow_run_ids = await self.rdb.smembers(
            RedisKeys.WORKFLOW_INDEX.format(app_name=self.app_name)
        )
        if not workflow_run_ids:
            return []

        keys = [
            RedisKeys.WORKFLOW_STATE.format(
                app_name=self.app_name, workflow_run_id=wid.decode()
            )
            for wid in workflow_run_ids
        ]
        values = await self.rdb.mget(keys)

        results: list[WorkflowState] = []
        for val in values:
            if not val:
                continue
            try:
                workflow = WorkflowState.from_json(val.decode("utf-8"))
                if status and workflow.status != status:
                    continue
                results.append(workflow)
            except (TypeError, json.JSONDecodeError):
                continue

        return results

    async def set_workflow_wake_time(
        self, workflow_run_id: str, timestamp: float
    ) -> None:
        """Schedule a workflow wake time using ZSET + TTL key."""
        if timestamp <= time.time():
            raise ValueError("Timestamp is in the past")

        schedule_key = RedisKeys.WORKFLOW_WAKE_SCHEDULE.format(app_name=self.app_name)
        wake_key = RedisKeys.WORKFLOW_WAKE_KEY.format(
            app_name=self.app_name, workflow_run_id=workflow_run_id
        )
        ttl_ms = max(1, int((timestamp - time.time()) * 1000))

        workflow = await self.get_workflow(workflow_run_id)
        workflow.scheduled_wake_time = timestamp
        workflow.status = LoopStatus.IDLE

        async with self.rdb.pipeline(transaction=True) as pipe:
            pipe.zadd(schedule_key, {workflow_run_id: timestamp})
            pipe.set(wake_key, "1", px=ttl_ms)
            pipe.set(
                RedisKeys.WORKFLOW_STATE.format(
                    app_name=self.app_name, workflow_run_id=workflow_run_id
                ),
                workflow.to_string(),
            )
            await pipe.execute()

        logger.info(
            "Workflow wake scheduled",
            extra={
                "workflow_run_id": workflow_run_id,
                "wake_timestamp": timestamp,
                "ttl_ms": ttl_ms,
            },
        )

    async def clear_workflow_wake_time(self, workflow_run_id: str) -> None:
        """Clear any scheduled workflow wake time."""
        schedule_key = RedisKeys.WORKFLOW_WAKE_SCHEDULE.format(app_name=self.app_name)
        wake_key = RedisKeys.WORKFLOW_WAKE_KEY.format(
            app_name=self.app_name, workflow_run_id=workflow_run_id
        )

        async with self.rdb.pipeline(transaction=True) as pipe:
            pipe.zrem(schedule_key, workflow_run_id)
            pipe.delete(wake_key)
            await pipe.execute()

        logger.info(
            "Workflow wake cleared",
            extra={"workflow_run_id": workflow_run_id},
        )

    async def try_claim_workflow_wake(self, workflow_run_id: str) -> bool:
        """Atomically try to claim a workflow wake. Returns True if this caller won the race."""
        schedule_key = RedisKeys.WORKFLOW_WAKE_SCHEDULE.format(app_name=self.app_name)
        wake_key = RedisKeys.WORKFLOW_WAKE_KEY.format(
            app_name=self.app_name, workflow_run_id=workflow_run_id
        )

        removed = await self.rdb.zrem(schedule_key, workflow_run_id)
        if removed:
            await self.rdb.delete(wake_key)
            logger.info(
                "Workflow wake claimed",
                extra={"workflow_run_id": workflow_run_id},
            )
            return True
        return False

    async def set_workflow_block_output(
        self, workflow_run_id: str, output: Any
    ) -> None:
        """Store the block output for a workflow using cloudpickle."""
        output_key = RedisKeys.WORKFLOW_BLOCK_OUTPUT.format(
            app_name=self.app_name, workflow_run_id=workflow_run_id
        )
        try:
            output_bytes: bytes = cloudpickle.dumps(output)
        except BaseException as exc:
            raise ValueError(f"Failed to serialize block output: {exc}") from exc

        await self.rdb.set(output_key, output_bytes)

    async def get_workflow_block_output(self, workflow_run_id: str) -> Any:
        """Retrieve the block output for a workflow."""
        output_key = RedisKeys.WORKFLOW_BLOCK_OUTPUT.format(
            app_name=self.app_name, workflow_run_id=workflow_run_id
        )
        output_bytes = await self.rdb.get(output_key)
        if output_bytes:
            return cloudpickle.loads(output_bytes)
        return None

    async def create_task(self, task: TaskState) -> TaskState:
        state_key = RedisKeys.TASK_STATE.format(
            app_name=self.app_name, task_id=task.task_id
        )
        index_key = RedisKeys.TASK_INDEX.format(app_name=self.app_name)

        async with self.rdb.pipeline(transaction=True) as pipe:
            pipe.set(state_key, task.to_string())
            pipe.sadd(index_key, task.task_id)
            await pipe.execute()

        return task

    async def get_task(self, task_id: str) -> TaskState:
        state_key = RedisKeys.TASK_STATE.format(app_name=self.app_name, task_id=task_id)
        task_str = await self.rdb.get(state_key)
        if not task_str:
            raise TaskNotFoundError(f"Task {task_id} not found")
        return TaskState.from_json(task_str.decode("utf-8"))

    async def update_task(self, task: TaskState) -> None:
        state_key = RedisKeys.TASK_STATE.format(
            app_name=self.app_name, task_id=task.task_id
        )
        await self.rdb.set(state_key, task.to_string())

    async def update_task_status(self, task_id: str, status: TaskStatus) -> TaskState:
        task = await self.get_task(task_id)
        task.status = status
        await self.update_task(task)
        return task

    async def set_task_result(self, task_id: str, result: Any) -> None:
        result_key = RedisKeys.TASK_RESULT.format(
            app_name=self.app_name, task_id=task_id
        )
        result_bytes: bytes = cloudpickle.dumps(result)
        await self.rdb.set(result_key, result_bytes, ex=86400)

    async def get_task_result(self, task_id: str) -> Any:
        result_key = RedisKeys.TASK_RESULT.format(
            app_name=self.app_name, task_id=task_id
        )
        result_bytes = await self.rdb.get(result_key)
        if result_bytes:
            return cloudpickle.loads(result_bytes)
        return None

    async def task_has_claim(self, task_id: str) -> bool:
        claim_key = RedisKeys.TASK_CLAIM.format(app_name=self.app_name, task_id=task_id)
        result = await self.rdb.get(claim_key)
        return result is not None

    async def try_claim_task_recovery(self, task_id: str) -> bool:
        """Atomically claim right to recover an orphaned task. Returns True if won."""
        claim_key = f"{RedisKeys.TASK_CLAIM.format(app_name=self.app_name, task_id=task_id)}:recovery"
        acquired = await self.rdb.set(claim_key, "1", nx=True, ex=60)
        return acquired is not None

    @asynccontextmanager
    async def with_task_claim(self, task_id: str) -> AsyncGenerator[None, None]:
        lease_key = RedisKeys.TASK_CLAIM.format(app_name=self.app_name, task_id=task_id)
        async with self._with_lease(lease_key, "task", task_id):
            yield

    async def get_all_tasks(self, status: TaskStatus | None = None) -> list[TaskState]:
        index_key = RedisKeys.TASK_INDEX.format(app_name=self.app_name)
        task_ids = await self.rdb.smembers(index_key)
        if not task_ids:
            return []

        keys = [
            RedisKeys.TASK_STATE.format(app_name=self.app_name, task_id=tid.decode())
            for tid in task_ids
        ]
        values = await self.rdb.mget(keys)

        results: list[TaskState] = []
        for val in values:
            if not val:
                continue
            try:
                task = TaskState.from_json(val.decode("utf-8"))
                if status and task.status != status:
                    continue
                results.append(task)
            except (TypeError, json.JSONDecodeError):
                continue

        return results

    async def save_schedule(self, schedule_id: str, schedule: Schedule) -> None:
        schedule_key = RedisKeys.SCHEDULE.format(
            app_name=self.app_name, schedule_id=schedule_id
        )
        queue_key = RedisKeys.SCHEDULE_QUEUE.format(app_name=self.app_name)

        if schedule.next_run is None:
            schedule.next_run = schedule.compute_next_run()

        async with self.rdb.pipeline(transaction=True) as pipe:
            pipe.set(schedule_key, json.dumps(schedule.to_dict()))
            pipe.zadd(queue_key, {schedule_id: schedule.next_run})
            await pipe.execute()

    async def get_schedule(self, schedule_id: str) -> Schedule | None:
        schedule_key = RedisKeys.SCHEDULE.format(
            app_name=self.app_name, schedule_id=schedule_id
        )
        data = await self.rdb.get(schedule_key)
        if data:
            return Schedule.from_dict(json.loads(data.decode("utf-8")))
        return None

    async def delete_schedule(self, schedule_id: str) -> None:
        schedule_key = RedisKeys.SCHEDULE.format(
            app_name=self.app_name, schedule_id=schedule_id
        )
        queue_key = RedisKeys.SCHEDULE_QUEUE.format(app_name=self.app_name)

        async with self.rdb.pipeline(transaction=True) as pipe:
            pipe.delete(schedule_key)
            pipe.zrem(queue_key, schedule_id)
            await pipe.execute()

    async def get_due_schedules(self) -> list[tuple[str, Schedule]]:
        queue_key = RedisKeys.SCHEDULE_QUEUE.format(app_name=self.app_name)
        now = time.time()

        schedule_ids: list[bytes] = await self.rdb.zrangebyscore(queue_key, "-inf", now)
        results: list[tuple[str, Schedule]] = []

        for sid_bytes in schedule_ids:
            schedule_id = sid_bytes.decode("utf-8")
            schedule = await self.get_schedule(schedule_id)
            if schedule and schedule.enabled:
                results.append((schedule_id, schedule))

        return results

    async def try_claim_schedule(self, schedule_id: str) -> bool:
        """Atomically claim a due schedule. Returns True if this replica won."""
        queue_key = RedisKeys.SCHEDULE_QUEUE.format(app_name=self.app_name)
        removed = await self.rdb.zrem(queue_key, schedule_id)
        return removed > 0

    async def advance_schedule(self, schedule_id: str, schedule: Schedule) -> None:
        schedule.next_run = schedule.compute_next_run()
        await self.save_schedule(schedule_id, schedule)
