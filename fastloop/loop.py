import asyncio
import contextlib
import json
import traceback
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

from fastapi import HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from .constants import CANCEL_GRACE_PERIOD_S
from .exceptions import (
    EventTimeoutError,
    LoopClaimError,
    LoopContextSwitchError,
    LoopNotFoundError,
    LoopPausedError,
    LoopStoppedError,
    WorkflowMaxRetriesError,
    WorkflowNextError,
    WorkflowNotFoundError,
    WorkflowRepeatError,
)
from .logging import setup_logger
from .state.state import LoopState, StateManager
from .types import BaseConfig, LoopEventSender, LoopStatus, RetryPolicy
from .utils import get_func_import_path

DEFAULT_RETRY_POLICY = RetryPolicy()

if TYPE_CHECKING:
    from .context import LoopContext

logger = setup_logger(__name__)


class Loop:
    """Base class for class-based loop definitions."""

    ctx: "LoopContext"

    async def on_start(self, ctx: "LoopContext") -> None:
        pass

    async def on_stop(self, ctx: "LoopContext") -> None:
        pass

    async def on_app_start(self, _ctx: "LoopContext") -> bool:
        return True

    async def on_event(self, ctx: "LoopContext", event: "LoopEvent") -> None:
        pass

    async def loop(self, ctx: "LoopContext") -> None:
        raise NotImplementedError("Subclasses must implement loop()")


class Workflow:
    """Base class for class-based workflow definitions."""

    ctx: "LoopContext"

    async def on_start(self, ctx: "LoopContext") -> None:
        pass

    async def on_stop(self, ctx: "LoopContext") -> None:
        pass

    async def on_block_complete(
        self, ctx: "LoopContext", block: "WorkflowBlock", payload: dict | None
    ) -> None:
        pass

    async def on_error(
        self, ctx: "LoopContext", block: "WorkflowBlock", error: Exception
    ) -> None:
        pass

    async def execute(
        self,
        ctx: "LoopContext",
        blocks: list["WorkflowBlock"],
        current_block: "WorkflowBlock",
    ) -> None:
        raise NotImplementedError("Subclasses must implement execute()")


class LoopEvent(BaseModel):
    loop_id: str | None = None
    event_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    partial: bool = False
    type: str = Field(default_factory=lambda: getattr(LoopEvent, "type", ""))
    sender: LoopEventSender = LoopEventSender.CLIENT
    timestamp: float = Field(default_factory=lambda: datetime.now().timestamp())
    nonce: int | None = None

    def __init__(self, **data: Any) -> None:
        if "type" not in data and hasattr(self.__class__, "type"):
            data["type"] = self.__class__.type
        super().__init__(**data)

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump()

    def to_string(self) -> str:
        """Return a JSON string representation of the event."""
        return json.dumps(self.to_dict(), default=str)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "LoopEvent":
        return cls.model_validate(data)

    @classmethod
    def from_json(cls, data: str) -> "LoopEvent":
        dict_data = json.loads(data)
        return cls.from_dict(dict_data)


class WorkflowBlock(BaseModel):
    """A single step in a workflow. Extend with additional fields as needed."""

    text: str
    type: str


@dataclass
class WorkflowState:
    """Persisted state for a running workflow."""

    workflow_id: str
    workflow_name: str | None = None
    created_at: float = field(default_factory=lambda: datetime.now().timestamp())
    status: LoopStatus = LoopStatus.PENDING
    blocks: list[dict[str, Any]] = field(default_factory=list)
    current_block_index: int = 0
    next_payload: dict[str, Any] | None = None
    completed_blocks: list[int] = field(default_factory=list)
    block_attempts: dict[int, int] = field(default_factory=dict)
    last_error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d = self.__dict__.copy()
        d["block_attempts"] = {str(k): v for k, v in self.block_attempts.items()}
        return d

    def to_string(self) -> str:
        return json.dumps(self.to_dict(), default=str)

    @classmethod
    def from_json(cls, json_str: str) -> "WorkflowState":
        data = json.loads(json_str)
        if "block_attempts" in data and isinstance(data["block_attempts"], dict):
            data["block_attempts"] = {
                int(k): v for k, v in data["block_attempts"].items()
            }
        return cls(**data)


class LoopManager:
    def __init__(self, config: BaseConfig, state_manager: StateManager):
        self.loop_tasks: dict[str, asyncio.Task[None]] = {}
        self.config: BaseConfig = config
        self.state_manager: StateManager = state_manager

    async def _run(
        self,
        func: Callable[..., Any],
        context: Any,
        loop_id: str,
        delay: float,
        loop_stop_func: Callable[..., Any] | None,
    ) -> None:
        try:
            async with self.state_manager.with_claim(loop_id):  # type: ignore
                idle_cycles = 0

                while not context.should_stop and not context.should_pause:
                    context.event_this_cycle = False

                    try:
                        if asyncio.iscoroutinefunction(func):
                            await func(context)
                        else:
                            func(context)  # type: ignore
                    except asyncio.CancelledError:
                        logger.info(
                            "Loop task cancelled, exiting",
                            extra={"loop_id": loop_id},
                        )
                        break
                    except LoopContextSwitchError as e:
                        func = e.func
                        context = e.context
                        loop = await self.state_manager.get_loop(loop_id)
                        loop.current_function_path = get_func_import_path(func)
                        await self.state_manager.update_loop(loop_id, loop)
                        continue
                    except EventTimeoutError:
                        ...
                    except (LoopPausedError, LoopStoppedError):
                        raise
                    except BaseException as e:
                        logger.error(
                            "Unhandled exception in loop",
                            extra={
                                "loop_id": loop_id,
                                "error": str(e),
                                "traceback": traceback.format_exc(),
                            },
                        )

                    if not context.event_this_cycle:
                        idle_cycles += 1
                        if (
                            idle_cycles >= self.config.max_idle_cycles
                            and self.config.shutdown_idle
                        ):
                            raise LoopPausedError()
                    else:
                        idle_cycles = 0

                    try:
                        await asyncio.sleep(delay)
                    except asyncio.CancelledError:
                        logger.info(
                            "Task cancelled during sleep, exiting",
                            extra={"loop_id": loop_id},
                        )
                        break

                if context.should_stop:
                    raise LoopStoppedError()
                elif context.should_pause:
                    raise LoopPausedError()

        except asyncio.CancelledError:
            logger.info("Loop task cancelled, exiting", extra={"loop_id": loop_id})
        except LoopClaimError:
            logger.info("Loop claim error, exiting", extra={"loop_id": loop_id})
        except LoopStoppedError:
            logger.info(
                "Loop stopped",
                extra={"loop_id": loop_id},
            )
            await self.state_manager.update_loop_status(loop_id, LoopStatus.STOPPED)
        except LoopPausedError:
            logger.info(
                "Loop paused",
                extra={"loop_id": loop_id},
            )
            await self.state_manager.update_loop_status(loop_id, LoopStatus.IDLE)
        finally:
            if loop_stop_func:
                if asyncio.iscoroutinefunction(loop_stop_func):
                    await loop_stop_func(context)
                else:
                    loop_stop_func(context)  # type: ignore

            self.loop_tasks.pop(loop_id, None)

    async def start(
        self,
        *,
        func: Callable[..., Any],
        loop_start_func: Callable[..., Any] | None,
        loop_stop_func: Callable[..., Any] | None,
        context: Any,
        loop: LoopState,
        loop_delay: float = 0.1,
    ) -> bool:
        if loop.loop_id in self.loop_tasks:
            return False

        if loop_start_func:
            if asyncio.iscoroutinefunction(loop_start_func):
                await loop_start_func(context)
            else:
                loop_start_func(context)  # type: ignore

        # TODO: switch out executor for thread/process based on config
        self.loop_tasks[loop.loop_id] = asyncio.create_task(
            self._run(func, context, loop.loop_id, loop_delay, loop_stop_func)
        )

        return True

    async def stop(self, loop_id: str) -> bool:
        task = self.loop_tasks.pop(loop_id, None)
        if task:
            task.cancel()

            try:
                await asyncio.wait_for(task, timeout=CANCEL_GRACE_PERIOD_S)
            except TimeoutError:
                logger.warning(
                    "Loop task did not stop within timeout",
                    extra={"loop_id": loop_id},
                )

            return True

        return False

    async def stop_all(self):
        """Stop all running loop tasks and wait for them to complete."""

        tasks_to_cancel = list(self.loop_tasks.values())
        self.loop_tasks.clear()

        for task in tasks_to_cancel:
            task.cancel()

        # Wait for all loop tasks to complete (w/ timeout)
        if tasks_to_cancel:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*tasks_to_cancel, return_exceptions=True),
                    timeout=CANCEL_GRACE_PERIOD_S,
                )
            except TimeoutError:
                logger.warning(
                    "Some loop tasks did not complete within timeout",
                    extra={"tasks": [task.get_name() for task in tasks_to_cancel]},
                )
            except BaseException as e:
                logger.error(
                    "Error waiting for loop tasks to complete",
                    extra={"error": str(e)},
                )

    async def active_loop_ids(self) -> set[str]:
        """
        Returns a set of loop IDs with tasks that are currently running in this replica.
        """

        return {loop_id for loop_id, _ in self.loop_tasks.items()}

    async def events_sse(self, loop_id: str):
        """
        SSE endpoint for streaming events to clients.
        Works for both loops and workflows.
        """
        # Check if it's a loop or workflow
        try:
            await self.state_manager.get_loop(loop_id)
        except LoopNotFoundError:
            try:
                await self.state_manager.get_workflow(loop_id)
            except WorkflowNotFoundError as e:
                raise HTTPException(
                    status_code=404, detail="Loop/workflow not found"
                ) from e

        connection_time = int(datetime.now().timestamp())
        last_sent_nonce = 0
        connection_id = str(uuid.uuid4())

        await self.state_manager.register_client_connection(loop_id, connection_id)
        pubsub = await self.state_manager.subscribe_to_events(loop_id)

        async def _event_generator():
            nonlocal last_sent_nonce

            try:
                while True:
                    all_events: list[
                        dict[str, Any]
                    ] = await self.state_manager.get_events_since(
                        loop_id, connection_time
                    )
                    server_events = [
                        e
                        for e in all_events
                        if e["sender"] == LoopEventSender.SERVER.value
                        and e["nonce"] > last_sent_nonce
                    ]

                    # Send any new events
                    for event in server_events:
                        event_data = json.dumps(event)
                        yield f"data: {event_data}\n\n"
                        last_sent_nonce = max(last_sent_nonce, event["nonce"])

                    # If no events, wait for notification or timeout
                    if not server_events:
                        # Wait for either a new event notification or keepalive timeout
                        notification_received = (
                            await self.state_manager.wait_for_event_notification(
                                pubsub, timeout=self.config.sse_keep_alive_s
                            )
                        )

                        if not notification_received:
                            yield "data: keepalive\n\n"

                        # Refresh connection TTL periodically
                        await self.state_manager.refresh_client_connection(
                            loop_id, connection_id
                        )

            except asyncio.CancelledError:
                pass
            except BaseException as e:
                logger.error(
                    "Error in SSE stream for loop",
                    extra={
                        "loop_id": loop_id,
                        "error": str(e),
                        "traceback": traceback.format_exc(),
                    },
                )
                yield f'data: {{"type": "error", "message": "{e!s}"}}\n\n'
            finally:
                await self.state_manager.unregister_client_connection(
                    loop_id, connection_id
                )
                if pubsub is not None:
                    await pubsub.unsubscribe()  # type: ignore
                    await pubsub.close()  # type: ignore

        return StreamingResponse(
            _event_generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "Access-Control-Allow-Headers": "Cache-Control",
            },
        )


async def _call(fn: Callable[..., Any] | None, *args: Any) -> None:
    if fn:
        if asyncio.iscoroutinefunction(fn):
            await fn(*args)
        else:
            fn(*args)


class WorkflowManager:
    def __init__(self, state_manager: StateManager):
        self.tasks: dict[str, asyncio.Task[None]] = {}
        self.state_manager = state_manager

    async def _persist_block_attempt(
        self, workflow_id: str, idx: int, error: str | None = None
    ) -> int:
        workflow = await self.state_manager.get_workflow(workflow_id)
        attempts = workflow.block_attempts.get(idx, 0) + 1
        workflow.block_attempts[idx] = attempts
        workflow.last_error = error
        await self.state_manager.update_workflow(workflow_id, workflow)
        return attempts

    async def _mark_block_completed(
        self, workflow_id: str, idx: int, next_idx: int, payload: dict | None
    ) -> None:
        workflow = await self.state_manager.get_workflow(workflow_id)
        if idx not in workflow.completed_blocks:
            workflow.completed_blocks.append(idx)
        workflow.current_block_index = next_idx
        workflow.next_payload = payload
        workflow.block_attempts.pop(idx, None)
        await self.state_manager.update_workflow(workflow_id, workflow)

    async def _run(
        self,
        func: Callable[..., Any],
        context: Any,
        workflow_id: str,
        on_stop: Callable[..., Any] | None,
        on_block_complete: Callable[..., Any] | None,
        on_error: Callable[..., Any] | None,
        retry_policy: RetryPolicy,
    ) -> None:
        try:
            async with self.state_manager.with_workflow_claim(workflow_id):
                while True:
                    workflow = await self.state_manager.get_workflow(workflow_id)
                    if workflow.status in (LoopStatus.STOPPED, LoopStatus.FAILED):
                        break

                    blocks = [WorkflowBlock(**b) for b in workflow.blocks]
                    idx = workflow.current_block_index

                    if idx >= len(blocks):
                        raise LoopStoppedError()

                    if idx in workflow.completed_blocks:
                        await self._mark_block_completed(
                            workflow_id, idx, idx + 1, workflow.next_payload
                        )
                        continue

                    current_block = blocks[idx]
                    context.block_index = idx
                    context.block_count = len(blocks)
                    context.previous_payload = workflow.next_payload

                    try:
                        await _call(func, context, blocks, current_block)
                        await _call(on_block_complete, context, current_block, None)
                        raise LoopStoppedError()

                    except WorkflowNextError as e:
                        await self._mark_block_completed(
                            workflow_id, idx, idx + 1, e.payload
                        )
                        await _call(
                            on_block_complete, context, current_block, e.payload
                        )

                    except WorkflowRepeatError:
                        pass

                    except (asyncio.CancelledError, LoopPausedError, LoopStoppedError):
                        raise

                    except BaseException as e:
                        error_str = str(e)
                        logger.error(
                            "Workflow block error",
                            extra={
                                "workflow_id": workflow_id,
                                "block_index": idx,
                                "error": error_str,
                                "traceback": traceback.format_exc(),
                            },
                        )

                        attempts = await self._persist_block_attempt(
                            workflow_id, idx, error_str
                        )

                        should_retry = False
                        if on_error:
                            try:
                                await _call(on_error, context, current_block, e)
                            except WorkflowRepeatError:
                                should_retry = True

                        if not should_retry and attempts < retry_policy.max_attempts:
                            should_retry = True

                        if should_retry and attempts < retry_policy.max_attempts:
                            delay = retry_policy.compute_delay(attempts)
                            logger.info(
                                "Retrying workflow block",
                                extra={
                                    "workflow_id": workflow_id,
                                    "block_index": idx,
                                    "attempt": attempts,
                                    "delay": delay,
                                },
                            )
                            await asyncio.sleep(delay)
                            continue

                        max_retries_error = WorkflowMaxRetriesError(
                            workflow_id, idx, attempts, error_str
                        )
                        logger.error(
                            "Workflow block failed after max retries",
                            extra={
                                "workflow_id": workflow_id,
                                "block_index": idx,
                                "attempts": attempts,
                            },
                        )
                        await _call(on_error, context, current_block, max_retries_error)
                        await self.state_manager.update_workflow_status(
                            workflow_id, LoopStatus.FAILED
                        )
                        return

        except asyncio.CancelledError:
            pass
        except LoopClaimError:
            logger.warning("Workflow claim failed", extra={"workflow_id": workflow_id})
        except LoopStoppedError:
            await self.state_manager.update_workflow_status(
                workflow_id, LoopStatus.STOPPED
            )
        except LoopPausedError:
            await self.state_manager.update_workflow_status(
                workflow_id, LoopStatus.IDLE
            )
        finally:
            await _call(on_stop, context)
            self.tasks.pop(workflow_id, None)

    async def start(
        self,
        func: Callable[..., Any],
        context: Any,
        workflow: WorkflowState,
        on_start: Callable[..., Any] | None = None,
        on_stop: Callable[..., Any] | None = None,
        on_block_complete: Callable[..., Any] | None = None,
        on_error: Callable[..., Any] | None = None,
        retry_policy: RetryPolicy | None = None,
    ) -> bool:
        if workflow.workflow_id in self.tasks:
            return False

        await _call(on_start, context)

        self.tasks[workflow.workflow_id] = asyncio.create_task(
            self._run(
                func,
                context,
                workflow.workflow_id,
                on_stop,
                on_block_complete,
                on_error,
                retry_policy or DEFAULT_RETRY_POLICY,
            )
        )
        return True

    async def stop(self, workflow_id: str) -> bool:
        task = self.tasks.pop(workflow_id, None)
        if not task:
            return False
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, TimeoutError):
            await asyncio.wait_for(task, timeout=CANCEL_GRACE_PERIOD_S)
        return True

    async def stop_all(self) -> None:
        tasks = list(self.tasks.values())
        self.tasks.clear()
        for t in tasks:
            t.cancel()
        if tasks:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(
                    asyncio.gather(*tasks, return_exceptions=True),
                    timeout=CANCEL_GRACE_PERIOD_S,
                )
