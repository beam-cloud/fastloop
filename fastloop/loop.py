"""
Loop base class and manager.

This module contains:
- Loop: Base class for class-based loop definitions
- LoopManager: Manages loop execution lifecycle
"""

import asyncio
import json
import time
import traceback
import uuid
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from fastapi import HTTPException
from fastapi.responses import StreamingResponse

from .constants import CANCEL_GRACE_PERIOD_S, MEANINGFUL_WORK_THRESHOLD_S
from .exceptions import (
    EventTimeoutError,
    LoopClaimError,
    LoopContextSwitchError,
    LoopNotFoundError,
    LoopPausedError,
    LoopStoppedError,
    WorkflowNotFoundError,
)
from .logging import setup_logger
from .models import LoopState
from .types import BaseConfig, LoopEventSender, LoopStatus
from .utils import get_func_import_path

if TYPE_CHECKING:
    from .context import LoopContext
    from .models import LoopEvent
    from .state.state import StateManager

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


class LoopManager:
    """Manages loop execution lifecycle."""

    def __init__(self, config: BaseConfig, state_manager: "StateManager"):
        self.loop_tasks: dict[str, asyncio.Task[None]] = {}
        self.config: BaseConfig = config
        self.state_manager: StateManager = state_manager

    async def _run(
        self,
        func: Callable[..., Any],
        context: Any,
        loop_id: str,
        delay: float,
        loop_start_func: Callable[..., Any] | None,
        loop_stop_func: Callable[..., Any] | None,
        stop_after_idle_seconds: float | None = None,
        pause_after_idle_seconds: float | None = None,
    ) -> None:
        try:
            async with self.state_manager.with_claim(loop_id):  # type: ignore
                await self.state_manager.update_loop_status(loop_id, LoopStatus.RUNNING)
                if loop_start_func:
                    if asyncio.iscoroutinefunction(loop_start_func):
                        await loop_start_func(context)
                    else:
                        loop_start_func(context)  # type: ignore

                last_active_time = time.time()
                last_claim_check = time.time()
                claim_check_interval = 5.0  # Check claim every 5 seconds

                while not context.should_stop and not context.should_pause:
                    context._reset_cycle_tracking()
                    cycle_start = time.monotonic()

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

                    cycle_duration = time.monotonic() - cycle_start
                    work_time = cycle_duration - context._wait_time_this_cycle
                    is_active = (
                        work_time > MEANINGFUL_WORK_THRESHOLD_S
                        or context.event_this_cycle
                    )

                    if is_active:
                        last_active_time = time.time()
                    else:
                        idle_seconds = time.time() - last_active_time
                        if (
                            stop_after_idle_seconds is not None
                            and idle_seconds >= stop_after_idle_seconds
                        ):
                            logger.info(
                                "Loop idle timeout reached, stopping",
                                extra={
                                    "loop_id": loop_id,
                                    "idle_seconds": idle_seconds,
                                },
                            )
                            raise LoopStoppedError()
                        if (
                            pause_after_idle_seconds is not None
                            and idle_seconds >= pause_after_idle_seconds
                        ):
                            logger.info(
                                "Loop idle timeout reached, pausing",
                                extra={
                                    "loop_id": loop_id,
                                    "idle_seconds": idle_seconds,
                                },
                            )
                            raise LoopPausedError()

                    try:
                        await asyncio.sleep(delay)
                    except asyncio.CancelledError:
                        logger.info(
                            "Task cancelled during sleep, exiting",
                            extra={"loop_id": loop_id},
                        )
                        break

                    # Verify we still own the claim periodically
                    if time.time() - last_claim_check >= claim_check_interval:
                        last_claim_check = time.time()
                        if not await self.state_manager.has_claim(loop_id):
                            logger.error(
                                "Claim lost during loop execution, stopping immediately",
                                extra={"loop_id": loop_id},
                            )
                            raise LoopClaimError(f"Claim lost for loop {loop_id}")

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
        stop_after_idle_seconds: float | None = None,
        pause_after_idle_seconds: float | None = None,
    ) -> bool:
        if loop.loop_id in self.loop_tasks:
            logger.debug(
                "Loop already running, skipping start",
                extra={"loop_id": loop.loop_id},
            )
            return False

        self.loop_tasks[loop.loop_id] = asyncio.create_task(
            self._run(
                func,
                context,
                loop.loop_id,
                loop_delay,
                loop_start_func,
                loop_stop_func,
                stop_after_idle_seconds,
                pause_after_idle_seconds,
            )
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
        """Returns a set of loop IDs with tasks that are currently running in this replica."""
        return {loop_id for loop_id, _ in self.loop_tasks.items()}

    async def events_sse(self, entity_id: str):
        """
        SSE endpoint for streaming events to clients.
        Works for both loops and workflows.
        """
        # Check if it's a loop or workflow, get creation time for event filtering
        created_at = None
        try:
            loop = await self.state_manager.get_loop(entity_id)
            created_at = loop.created_at
        except LoopNotFoundError:
            try:
                workflow = await self.state_manager.get_workflow(entity_id)
                created_at = workflow.created_at
            except WorkflowNotFoundError as e:
                raise HTTPException(
                    status_code=404, detail="Loop/workflow not found"
                ) from e

        connection_time = int(created_at) if created_at else 0
        last_sent_nonce = 0
        connection_id = str(uuid.uuid4())

        await self.state_manager.register_client_connection(entity_id, connection_id)
        pubsub = await self.state_manager.subscribe_to_events(entity_id)

        async def _event_generator():
            nonlocal last_sent_nonce

            try:
                while True:
                    all_events: list[
                        dict[str, Any]
                    ] = await self.state_manager.get_events_since(
                        entity_id, connection_time
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
                            entity_id, connection_id
                        )

            except asyncio.CancelledError:
                pass
            except BaseException as e:
                logger.error(
                    "Error in SSE stream",
                    extra={
                        "entity_id": entity_id,
                        "error": str(e),
                        "traceback": traceback.format_exc(),
                    },
                )
                yield f'data: {{"type": "error", "message": "{e!s}"}}\n\n'
            finally:
                await self.state_manager.unregister_client_connection(
                    entity_id, connection_id
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
