import json
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any

import aioboto3
import cloudpickle
from botocore.exceptions import ClientError

from ..exceptions import LoopClaimError, LoopNotFoundError
from ..loop import LoopEvent
from ..types import LoopEventSender, LoopStatus, S3Config
from .state import LoopState, StateManager


class S3Keys:
    """Key patterns for S3 objects."""

    LOOP_INDEX = "{prefix}/index/loops.json"
    LOOP_STATE = "{prefix}/state/{loop_id}.json"
    LOOP_EVENT_QUEUE_SERVER = (
        "{prefix}/events/{loop_id}/{event_type}/server/{event_id}.json"
    )
    LOOP_EVENT_QUEUE_CLIENT = (
        "{prefix}/events/{loop_id}/{event_type}/client/{event_id}.json"
    )
    LOOP_EVENT_HISTORY = "{prefix}/history/{loop_id}/{timestamp}-{event_id}.json"
    LOOP_CLAIM = "{prefix}/claims/{loop_id}.json"
    LOOP_CONTEXT = "{prefix}/context/{loop_id}/{key}.pkl"


class S3StateManager(StateManager):
    def __init__(self, config: S3Config):
        self.config = config
        self.session = aioboto3.Session(
            aws_access_key_id=config.aws_access_key_id or None,
            aws_secret_access_key=config.aws_secret_access_key or None,
            region_name=config.region_name,
        )

    def _get_key(self, pattern: str, **kwargs) -> str:
        """Format S3 key with prefix and variables."""
        return pattern.format(prefix=self.config.prefix, **kwargs)

    async def _s3_get_json(self, key: str) -> dict | None:
        """Get JSON object from S3, return None if not found."""
        async with self.session.client("s3") as s3:
            try:
                response = await s3.get_object(Bucket=self.config.bucket_name, Key=key)
                content = await response["Body"].read()
                return json.loads(content.decode("utf-8"))
            except ClientError as e:
                if e.response["Error"]["Code"] == "NoSuchKey":
                    return None
                raise

    async def _s3_put_json(self, key: str, data: dict | list) -> None:
        """Put JSON object to S3."""
        async with self.session.client("s3") as s3:
            await s3.put_object(
                Bucket=self.config.bucket_name,
                Key=key,
                Body=json.dumps(data, default=str).encode("utf-8"),
                ContentType="application/json",
            )

    async def _s3_put_bytes(self, key: str, data: bytes) -> None:
        """Put binary data to S3."""
        async with self.session.client("s3") as s3:
            await s3.put_object(
                Bucket=self.config.bucket_name,
                Key=key,
                Body=data,
                ContentType="application/octet-stream",
            )

    async def _s3_get_bytes(self, key: str) -> bytes | None:
        """Get binary data from S3, return None if not found."""
        async with self.session.client("s3") as s3:
            try:
                response = await s3.get_object(Bucket=self.config.bucket_name, Key=key)
                return await response["Body"].read()
            except ClientError as e:
                if e.response["Error"]["Code"] == "NoSuchKey":
                    return None
                raise

    async def _s3_delete(self, key: str) -> None:
        """Delete object from S3."""
        async with self.session.client("s3") as s3:
            try:
                await s3.delete_object(Bucket=self.config.bucket_name, Key=key)
            except ClientError as e:
                if e.response["Error"]["Code"] != "NoSuchKey":
                    raise

    async def _s3_list_objects(self, prefix: str) -> list[str]:
        """List objects with given prefix."""
        async with self.session.client("s3") as s3:
            try:
                response = await s3.list_objects_v2(
                    Bucket=self.config.bucket_name, Prefix=prefix
                )
                return [obj["Key"] for obj in response.get("Contents", [])]
            except ClientError:
                return []

    async def _get_loop_index(self) -> set[str]:
        """Get the set of all loop IDs."""
        key = self._get_key(S3Keys.LOOP_INDEX)
        data = await self._s3_get_json(key)
        return set(data.get("loop_ids", [])) if data else set()

    async def _update_loop_index(self, loop_ids: set[str]) -> None:
        """Update the loop index with the current set of loop IDs."""
        key = self._get_key(S3Keys.LOOP_INDEX)
        await self._s3_put_json(key, {"loop_ids": list(loop_ids)})

    async def get_loop(self, loop_id: str) -> LoopState:
        key = self._get_key(S3Keys.LOOP_STATE, loop_id=loop_id)
        loop_data = await self._s3_get_json(key)
        if loop_data:
            return LoopState.from_json(json.dumps(loop_data))
        else:
            raise LoopNotFoundError(f"Loop {loop_id} not found")

    async def get_or_create_loop(
        self,
        loop_name: str | None = None,
        loop_id: str | None = None,
        idle_timeout: float = 60.0,
    ) -> tuple[LoopState, bool]:
        if not loop_id:
            loop_id = str(uuid.uuid4())

        key = self._get_key(S3Keys.LOOP_STATE, loop_id=loop_id)
        loop_data = await self._s3_get_json(key)

        if loop_data:
            return LoopState.from_json(json.dumps(loop_data)), False

        loop = LoopState(
            loop_id=loop_id,
            loop_name=loop_name,
            idle_timeout=idle_timeout,
            last_event_at=int(datetime.now().timestamp()),
        )

        await self._s3_put_json(key, loop.__dict__)

        # Update the index
        loop_index = await self._get_loop_index()
        loop_index.add(loop_id)
        await self._update_loop_index(loop_index)

        return loop, True

    async def update_loop(self, loop_id: str, state: LoopState):
        key = self._get_key(S3Keys.LOOP_STATE, loop_id=loop_id)
        await self._s3_put_json(key, state.__dict__)

    @asynccontextmanager
    async def with_claim(self, loop_id: str):
        claim_key = self._get_key(S3Keys.LOOP_CLAIM, loop_id=loop_id)
        claim_id = str(uuid.uuid4())
        timeout = 60  # 60 seconds timeout

        # Try to acquire the claim
        claim_data = {
            "claim_id": claim_id,
            "acquired_at": datetime.now().timestamp(),
            "timeout": timeout,
        }

        # Check if there's an existing claim
        existing_claim = await self._s3_get_json(claim_key)
        if existing_claim:
            acquired_at = existing_claim.get("acquired_at", 0)
            claim_timeout = existing_claim.get("timeout", 60)

            # Check if the existing claim has expired
            if datetime.now().timestamp() - acquired_at < claim_timeout:
                raise LoopClaimError(f"Could not acquire claim for loop {loop_id}")

        # Acquire the claim
        await self._s3_put_json(claim_key, claim_data)

        try:
            yield
        finally:
            # Release the claim
            current_claim = await self._s3_get_json(claim_key)
            if current_claim and current_claim.get("claim_id") == claim_id:
                await self._s3_delete(claim_key)

    async def get_all_loops(
        self,
        status: LoopStatus | None = None,
    ) -> list[LoopState]:
        loop_index = await self._get_loop_index()
        loops = []

        for loop_id in loop_index:
            key = self._get_key(S3Keys.LOOP_STATE, loop_id=loop_id)
            loop_data = await self._s3_get_json(key)

            if not loop_data:
                # Remove from index if the loop state doesn't exist
                loop_index.discard(loop_id)
                continue

            loop_state = LoopState.from_json(json.dumps(loop_data))
            if status and loop_state.status != status:
                continue

            loops.append(loop_state)

        # Update index to remove any stale entries
        await self._update_loop_index(loop_index)
        return loops

    async def get_event_history(self, loop_id: str) -> list["LoopEvent"]:
        prefix = self._get_key(S3Keys.LOOP_EVENT_HISTORY, loop_id=loop_id).replace(
            "/{timestamp}-{event_id}.json", "/"
        )

        keys = await self._s3_list_objects(prefix)
        events = []

        for key in sorted(keys):
            event_data = await self._s3_get_json(key)
            if event_data:
                events.append(LoopEvent.from_dict(event_data))

        events.sort(key=lambda e: e.nonce or 0)
        return events

    async def push_event(self, loop_id: str, event: "LoopEvent"):
        event_id = str(uuid.uuid4())
        timestamp = int(
            datetime.now().timestamp() * 1000000
        )  # microseconds for ordering

        # Store in appropriate queue
        if event.sender == LoopEventSender.SERVER:
            queue_key = self._get_key(
                S3Keys.LOOP_EVENT_QUEUE_SERVER,
                loop_id=loop_id,
                event_type=event.type,
                event_id=event_id,
            )
        elif event.sender == LoopEventSender.CLIENT:
            queue_key = self._get_key(
                S3Keys.LOOP_EVENT_QUEUE_CLIENT,
                loop_id=loop_id,
                event_type=event.type,
                event_id=event_id,
            )
        else:
            raise ValueError(f"Invalid event sender: {event.sender}")

        await self._s3_put_json(queue_key, event.to_dict())

        # Store in history
        history_key = self._get_key(
            S3Keys.LOOP_EVENT_HISTORY,
            loop_id=loop_id,
            timestamp=timestamp,
            event_id=event_id,
        )
        await self._s3_put_json(history_key, event.to_dict())

        # Update loop timestamp
        loop, _ = await self.get_or_create_loop(loop_id)
        loop.last_event_at = datetime.now().timestamp()  # Use microsecond precision
        await self.update_loop(loop_id, loop)

    async def get_context_value(self, loop_id: str, key: str) -> Any:
        s3_key = self._get_key(S3Keys.LOOP_CONTEXT, loop_id=loop_id, key=key)
        data = await self._s3_get_bytes(s3_key)

        if data:
            return cloudpickle.loads(data)
        return None

    async def set_context_value(self, loop_id: str, key: str, value: Any):
        try:
            data = cloudpickle.dumps(value)
        except BaseException as exc:
            raise ValueError(f"Failed to serialize value: {exc}") from exc

        s3_key = self._get_key(S3Keys.LOOP_CONTEXT, loop_id=loop_id, key=key)
        await self._s3_put_bytes(s3_key, data)

    async def get_initial_event(self, loop_id: str) -> "LoopEvent | None":
        """Get the initial event for a loop."""
        # For S3, we'll need to implement this based on how initial events are stored
        # For now, return None as a placeholder
        return None

    async def get_next_nonce(self, loop_id: str) -> int:
        """
        Get the next nonce for a loop using S3 atomic counter.
        """
        nonce_key = f"{self.config.prefix}/nonce/{loop_id}.json"

        # Try to get current nonce
        current_nonce_data = await self._s3_get_json(nonce_key)
        current_nonce = current_nonce_data.get("nonce", 0) if current_nonce_data else 0

        # Increment and store
        new_nonce = current_nonce + 1
        await self._s3_put_json(nonce_key, {"nonce": new_nonce})

        return new_nonce

    async def get_events_since(
        self, loop_id: str, since_timestamp: float
    ) -> list["LoopEvent"]:
        """
        Get events that occurred since the given timestamp.
        """
        all_events = await self.get_event_history(loop_id)
        return [event for event in all_events if event.timestamp >= since_timestamp]

    async def pop_event(
        self,
        loop_id: str,
        event: "LoopEvent",
        sender: LoopEventSender = LoopEventSender.CLIENT,
    ) -> LoopEvent | None:
        return await self._pop_event_from_queue(loop_id, event, sender)

    async def _pop_event_from_queue(
        self,
        loop_id: str,
        event: "LoopEvent",
        sender: LoopEventSender,
    ) -> LoopEvent | None:
        if sender == LoopEventSender.SERVER:
            queue_prefix = self._get_key(
                S3Keys.LOOP_EVENT_QUEUE_SERVER,
                loop_id=loop_id,
                event_type=event.type,
                event_id="",
            ).rstrip(".json")
        else:
            queue_prefix = self._get_key(
                S3Keys.LOOP_EVENT_QUEUE_CLIENT,
                loop_id=loop_id,
                event_type=event.type,
                event_id="",
            ).rstrip(".json")

        # List objects in the queue
        keys = await self._s3_list_objects(queue_prefix)
        if not keys:
            return None

        # Get the first (oldest) event
        key = sorted(keys)[0]  # FIFO order
        event_data = await self._s3_get_json(key)

        if event_data:
            # Delete the event from the queue
            await self._s3_delete(key)
            return LoopEvent.from_dict(event_data)

        return None
