import asyncio
import json
import time
import uuid
from contextlib import asynccontextmanager, suppress
from typing import Any, cast

import boto3  # type: ignore
import cloudpickle  # type: ignore
from botocore.exceptions import ClientError  # type: ignore

from ..constants import CLAIM_LOCK_BLOCKING_TIMEOUT_S, CLAIM_LOCK_SLEEP_S
from ..exceptions import LoopClaimError, LoopNotFoundError
from ..loop import LoopEvent
from ..types import E, LoopEventSender, LoopStatus, S3Config
from .state import LoopState, StateManager


class S3Keys:
    @staticmethod
    def loop_index(prefix: str, app_name: str) -> str:
        return f"{prefix}/{app_name}/index.json"

    @staticmethod
    def loop_state(prefix: str, app_name: str, loop_id: str) -> str:
        return f"{prefix}/{app_name}/state/{loop_id}.json"

    @staticmethod
    def loop_event_history(prefix: str, app_name: str, loop_id: str) -> str:
        return f"{prefix}/{app_name}/event_history/{loop_id}.json"

    @staticmethod
    def loop_initial_event(prefix: str, app_name: str, loop_id: str) -> str:
        return f"{prefix}/{app_name}/initial_event/{loop_id}.json"

    @staticmethod
    def loop_context(prefix: str, app_name: str, loop_id: str, key: str) -> str:
        return f"{prefix}/{app_name}/context/{loop_id}/{key}.json"

    @staticmethod
    def loop_nonce(prefix: str, app_name: str, loop_id: str) -> str:
        return f"{prefix}/{app_name}/nonce/{loop_id}.json"

    @staticmethod
    def loop_lock(prefix: str, app_name: str, loop_id: str) -> str:
        return f"{prefix}/{app_name}/locks/{loop_id}.lock"

    @staticmethod
    def loop_event_queue_server(prefix: str, app_name: str, loop_id: str) -> str:
        return f"{prefix}/{app_name}/events/{loop_id}/server.json"

    @staticmethod
    def loop_event_queue_client(
        prefix: str, app_name: str, loop_id: str, event_type: str
    ) -> str:
        return f"{prefix}/{app_name}/events/{loop_id}/{event_type}/client.json"

    @staticmethod
    def loop_mapping(prefix: str, app_name: str, external_ref_id: str) -> str:
        return f"{prefix}/{app_name}/mapping/{external_ref_id}.json"


class S3StateManager(StateManager):
    def __init__(self, *, app_name: str, config: S3Config):
        self.app_name = app_name

        s3_config = {
            "aws_access_key_id": config.aws_access_key_id,
            "aws_secret_access_key": config.aws_secret_access_key,
            "region_name": config.region_name,
        }

        if hasattr(config, "endpoint_url") and config.endpoint_url:
            s3_config["endpoint_url"] = config.endpoint_url

        self.s3: Any = boto3.client("s3", **s3_config)  # type: ignore
        self.prefix = config.prefix
        self.bucket = config.bucket_name
        self._lock_renewal_tasks: dict[str, asyncio.Task[None]] = {}

    def _get_json(self, key: str) -> Any:
        try:
            response: dict[str, Any] = self.s3.get_object(Bucket=self.bucket, Key=key)
            return json.loads(response["Body"].read().decode("utf-8"))
        except ClientError as e:
            if e.response["Error"]["Code"] == "NoSuchKey":
                return None
            raise

    def _put_json(self, key: str, data: Any):
        self.s3.put_object(Bucket=self.bucket, Key=key, Body=json.dumps(data))

    def _get_bytes(self, key: str) -> bytes | None:
        try:
            response = self.s3.get_object(Bucket=self.bucket, Key=key)
            return response["Body"].read()
        except ClientError as e:
            if e.response["Error"]["Code"] == "NoSuchKey":
                return None
            raise

    def _put_bytes(self, key: str, data: bytes):
        self.s3.put_object(Bucket=self.bucket, Key=key, Body=data)

    async def _renew_lock_periodically(self, lock_key: str, renewal_interval: float):
        """Background task to continuously renew the lock while process is alive"""
        while True:
            try:
                await asyncio.sleep(renewal_interval)
                self.s3.put_object(
                    Bucket=self.bucket,
                    Key=lock_key,
                    Body=json.dumps(
                        {
                            "locked_at": time.time(),
                            "host_id": str(uuid.uuid4()),
                            "renewed_at": time.time(),
                        }
                    ),
                )

            except asyncio.CancelledError:
                break
            except BaseException:
                # If renewal fails, the lock will expire naturally
                # and another process can claim it
                break

    @asynccontextmanager
    async def with_claim(self, loop_id: str):  # type: ignore
        lock_key = S3Keys.loop_lock(self.prefix, self.app_name, loop_id)
        start_time = time.time()
        acquired = False
        renewal_task: asyncio.Task[None] | None = None

        # Lock renewal interval (should be much shorter than timeout)
        renewal_interval = CLAIM_LOCK_BLOCKING_TIMEOUT_S / 3
        lock_timeout = CLAIM_LOCK_BLOCKING_TIMEOUT_S

        while not acquired:
            try:
                # Try to get existing lock
                try:
                    response = self.s3.head_object(Bucket=self.bucket, Key=lock_key)

                    # Check if lock is expired based on LastModified
                    last_modified = response["LastModified"].timestamp()
                    if time.time() - last_modified < lock_timeout:
                        # Lock is still valid, wait
                        if time.time() - start_time > CLAIM_LOCK_BLOCKING_TIMEOUT_S:
                            raise LoopClaimError(
                                f"Could not acquire lock for loop {loop_id}"
                            )
                        await asyncio.sleep(CLAIM_LOCK_SLEEP_S)
                        continue

                    # Lock expired, try to overwrite
                    current_etag = response["ETag"]
                    self.s3.put_object(
                        Bucket=self.bucket,
                        Key=lock_key,
                        Body=json.dumps(
                            {"locked_at": time.time(), "process_id": str(uuid.uuid4())}
                        ),
                        IfMatch=current_etag,
                    )
                    acquired = True

                except ClientError as e:
                    if e.response["Error"]["Code"] == "404":
                        # No lock exists, create new one
                        self.s3.put_object(
                            Bucket=self.bucket,
                            Key=lock_key,
                            Body=json.dumps(
                                {
                                    "locked_at": time.time(),
                                    "process_id": str(uuid.uuid4()),
                                }
                            ),
                            IfNoneMatch="*",
                        )
                        acquired = True
                    else:
                        raise

            except ClientError as e:
                error_code: str = e.response["Error"]["Code"]  # type: ignore
                if error_code in ["PreconditionFailed", "412"]:
                    if time.time() - start_time > CLAIM_LOCK_BLOCKING_TIMEOUT_S:
                        raise LoopClaimError(
                            f"Could not acquire lock for loop {loop_id}"
                        ) from e
                    await asyncio.sleep(CLAIM_LOCK_SLEEP_S)
                else:
                    raise

        renewal_task = asyncio.create_task(
            self._renew_lock_periodically(lock_key, renewal_interval)
        )
        self._lock_renewal_tasks[lock_key] = renewal_task

        try:
            yield
        finally:
            if renewal_task and not renewal_task.done():
                renewal_task.cancel()
                with suppress(asyncio.CancelledError):
                    await renewal_task

            self._lock_renewal_tasks.pop(lock_key, None)
            with suppress(ClientError):
                self.s3.delete_object(Bucket=self.bucket, Key=lock_key)

    async def has_claim(self, loop_id: str) -> bool:
        lock_key = S3Keys.loop_lock(self.prefix, self.app_name, loop_id)

        # Check if we have an active renewal task for this lock
        if lock_key in self._lock_renewal_tasks:
            task = self._lock_renewal_tasks[lock_key]
            if not task.done():
                return True

        # Fallback and check S3
        try:
            response = self.s3.head_object(Bucket=self.bucket, Key=lock_key)
            last_modified = response["LastModified"].timestamp()
            return time.time() - last_modified < CLAIM_LOCK_BLOCKING_TIMEOUT_S
        except ClientError as e:
            if e.response["Error"]["Code"] == "404":
                return False
            raise

    async def get_loop(self, loop_id: str) -> LoopState:
        data = self._get_json(S3Keys.loop_state(self.prefix, self.app_name, loop_id))
        if data:
            return LoopState(**data)
        raise LoopNotFoundError(f"Loop {loop_id} not found")

    async def get_or_create_loop(
        self,
        *,
        loop_name: str | None = None,
        loop_id: str | None = None,
        current_function_path: str = "",
    ) -> tuple[LoopState, bool]:
        if loop_id:
            loop = await self.get_loop(loop_id)
            return loop, False

        loop_id = str(uuid.uuid4())
        loop = LoopState(
            loop_id=loop_id,
            loop_name=loop_name,
            current_function_path=current_function_path,
        )
        self._put_json(
            S3Keys.loop_state(self.prefix, self.app_name, loop_id), loop.__dict__
        )

        index: list[str] = (
            self._get_json(S3Keys.loop_index(self.prefix, self.app_name)) or []
        )
        index.append(loop_id)
        self._put_json(S3Keys.loop_index(self.prefix, self.app_name), index)

        return loop, True

    async def update_loop(self, loop_id: str, state: LoopState):
        self._put_json(
            S3Keys.loop_state(self.prefix, self.app_name, loop_id), state.__dict__
        )

    async def update_loop_status(self, loop_id: str, status: LoopStatus) -> LoopState:
        loop = await self.get_loop(loop_id)
        loop.status = status
        await self.update_loop(loop_id, loop)
        return loop

    async def get_all_loop_ids(self) -> set[str]:
        return set(self._get_json(S3Keys.loop_index(self.prefix, self.app_name)) or [])

    async def get_all_loops(self, status: LoopStatus | None = None) -> list[LoopState]:
        loop_ids = await self.get_all_loop_ids()
        loops: list[LoopState] = []

        for loop_id in loop_ids:
            try:
                loop: LoopState = await self.get_loop(loop_id)
                if status and loop.status != status:
                    continue
                loops.append(loop)
            except LoopNotFoundError:
                continue

        return loops

    async def get_event_history(self, loop_id: str) -> list[dict[str, Any]]:
        return (
            self._get_json(
                S3Keys.loop_event_history(self.prefix, self.app_name, loop_id)
            )
            or []
        )

    async def push_event(self, loop_id: str, event: LoopEvent):
        if event.sender == LoopEventSender.SERVER:
            queue_key = S3Keys.loop_event_queue_server(
                self.prefix, self.app_name, loop_id
            )
        elif event.sender == LoopEventSender.CLIENT:
            queue_key = S3Keys.loop_event_queue_client(
                self.prefix, self.app_name, loop_id, event.type
            )
        else:
            raise ValueError(f"Invalid sender: {event.sender}")

        queue: list[dict[str, Any]] = self._get_json(queue_key) or []
        queue.append(event.to_dict())
        self._put_json(queue_key, queue)

        history = await self.get_event_history(loop_id)
        history.append(event.to_dict())
        self._put_json(
            S3Keys.loop_event_history(self.prefix, self.app_name, loop_id), history
        )

        initial_event = self._get_json(
            S3Keys.loop_initial_event(self.prefix, self.app_name, loop_id)
        )
        if not initial_event:
            self._put_json(
                S3Keys.loop_initial_event(self.prefix, self.app_name, loop_id),
                event.to_dict(),
            )

        loop = await self.get_loop(loop_id=loop_id)
        await self.update_loop(loop_id, loop)

    async def get_context_value(self, loop_id: str, key: str) -> Any:
        value_bytes = self._get_bytes(
            S3Keys.loop_context(self.prefix, self.app_name, loop_id, key)
        )
        if value_bytes:
            return cloudpickle.loads(value_bytes)
        else:
            return None

    async def set_context_value(self, loop_id: str, key: str, value: Any):
        try:
            value_bytes = cloudpickle.dumps(value)  # type: ignore
        except BaseException as exc:
            raise ValueError(f"Failed to serialize value: {exc}") from exc

        self._put_bytes(
            S3Keys.loop_context(self.prefix, self.app_name, loop_id, key), value_bytes
        )

    async def delete_context_value(self, loop_id: str, key: str):
        self.s3.delete_object(
            Bucket=self.bucket,
            Key=S3Keys.loop_context(self.prefix, self.app_name, loop_id, key),
        )

    async def pop_event(
        self,
        loop_id: str,
        event: type[E],
        sender: LoopEventSender = LoopEventSender.CLIENT,
    ) -> E | None:
        if sender == LoopEventSender.SERVER:
            queue_key = S3Keys.loop_event_queue_server(
                self.prefix, self.app_name, loop_id
            )
        elif sender == LoopEventSender.CLIENT:
            queue_key = S3Keys.loop_event_queue_client(
                self.prefix, self.app_name, loop_id, event.type
            )
        else:
            raise ValueError(f"Invalid sender: {sender}")

        queue: list[dict[str, Any]] = self._get_json(queue_key) or []
        if queue:
            event_data = queue.pop(0)
            self._put_json(queue_key, queue)
            return cast(E, event.from_dict(event_data))  # noqa
        return None

    async def set_wake_time(self, loop_id: str, timestamp: float) -> None:
        raise NotImplementedError("S3 state backend does not yet support wake times")

    async def get_initial_event(self, loop_id: str) -> LoopEvent | None:
        data = self._get_json(
            S3Keys.loop_initial_event(self.prefix, self.app_name, loop_id)
        )
        return data

    async def set_loop_mapping(self, external_ref_id: str, loop_id: str):
        self._put_json(
            S3Keys.loop_mapping(self.prefix, self.app_name, external_ref_id), loop_id
        )

    async def get_loop_mapping(self, external_ref_id: str) -> str | None:
        return self._get_json(
            S3Keys.loop_mapping(self.prefix, self.app_name, external_ref_id)
        )

    async def get_next_nonce(self, loop_id: str) -> int:
        nonce = (
            self._get_json(S3Keys.loop_nonce(self.prefix, self.app_name, loop_id)) or 0
        )
        nonce += 1
        self._put_json(S3Keys.loop_nonce(self.prefix, self.app_name, loop_id), nonce)
        return nonce

    async def get_events_since(
        self, loop_id: str, since_timestamp: float
    ) -> list[dict[str, Any]]:
        history = await self.get_event_history(loop_id)
        return [event for event in history if event["timestamp"] >= since_timestamp]

    async def pop_server_event(self, loop_id: str) -> dict[str, Any] | None:
        queue_key = S3Keys.loop_event_queue_server(self.prefix, self.app_name, loop_id)
        queue: list[dict[str, Any]] = self._get_json(queue_key) or []

        if queue:
            event_data = queue.pop(0)
            self._put_json(queue_key, queue)
            return event_data

        return None

    async def subscribe_to_events(self, _: str) -> Any:  # type: ignore
        """Since we don't have pub/sub, we return a dummy subscription"""
        return None

    async def wait_for_event_notification(  # type: ignore
        self, _: Any, timeout: float | None = None
    ) -> bool:
        """Since we don't have pub/sub, we just sleep for the timeout period with a 1 second minimum"""
        timeout = 1.0 if timeout is None else max(1.0, timeout)
        await asyncio.sleep(timeout)
        return False
