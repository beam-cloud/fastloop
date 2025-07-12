import asyncio
import json
from collections.abc import Callable
from datetime import datetime
from typing import Any

from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from .constants import CANCEL_GRACE_PERIOD_S
from .context import LoopContext
from .exceptions import (
    EventTimeoutError,
    LoopClaimError,
    LoopPausedError,
    LoopStoppedError,
)
from .logging import setup_logger
from .state.state import LoopState, StateManager
from .types import BaseConfig, LoopEventSender, LoopStatus

logger = setup_logger(__name__)


class LoopEvent(BaseModel):
    loop_id: str | None = None
    type: str = Field(default_factory=lambda: getattr(LoopEvent, "type", ""))
    sender: LoopEventSender = LoopEventSender.CLIENT
    timestamp: float = Field(default_factory=lambda: datetime.now().timestamp())
    nonce: int | None = None

    def __init__(self, **data):
        if "type" not in data and hasattr(self.__class__, "type"):
            data["type"] = self.__class__.type
        super().__init__(**data)

    def to_dict(self) -> dict[str, Any]:
        data = self.model_dump()
        return data

    def to_string(self) -> str:
        return json.dumps(self.to_dict(), default=str)

    def to_json(self) -> str:
        return self.__dict__.copy()

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "LoopEvent":
        return cls.model_validate(data)

    @classmethod
    def from_json(cls, data: str) -> "LoopEvent":
        dict_data = json.loads(data)
        return cls.from_dict(dict_data)


class LoopManager:
    def __init__(self, config: BaseConfig, state_manager: StateManager):
        self.loop_tasks: dict[str, asyncio.Task] = {}
        self.config: BaseConfig = config
        self.state_manager: StateManager = state_manager

    async def _run(
        self, func: Callable, context: LoopContext, loop_id: str, delay: float
    ):
        try:
            async with self.state_manager.with_claim(loop_id):
                while not context.should_stop and not context.should_pause:
                    try:
                        if asyncio.iscoroutinefunction(func):
                            await func(context)
                        else:
                            func(context)
                    except asyncio.CancelledError:
                        logger.info(
                            "Loop task cancelled, exiting",
                            extra={"loop_id": loop_id},
                        )
                        break
                    except EventTimeoutError:
                        ...
                    except BaseException as e:
                        logger.error(
                            "Unhandled exception in loop",
                            extra={"loop_id": loop_id, "error": str(e)},
                        )

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
            pass
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
            self.loop_tasks.pop(loop_id, None)

    async def start(
        self,
        *,
        func: Callable,
        loop_start_func: Callable | None,
        context: LoopContext,
        loop: LoopState,
        loop_delay: float = 0.1,
    ) -> bool:
        if loop.loop_id in self.loop_tasks:
            return False

        if loop_start_func:
            if asyncio.iscoroutinefunction(loop_start_func):
                await loop_start_func(context)
            else:
                loop_start_func(context)

        self.loop_tasks[loop.loop_id] = asyncio.create_task(
            self._run(func, context, loop.loop_id, loop_delay)
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
        Uses event history to support multiple listeners.
        Only sends events that occurred after the connection was established.
        """

        loop = await self.state_manager.get_loop(loop_id)
        connection_time = loop.last_event_at
        last_sent_nonce = 0

        async def _event_generator():
            nonlocal last_sent_nonce

            while True:
                try:
                    # Get events since connection time
                    all_events = await self.state_manager.get_events_since(
                        loop_id, connection_time
                    )
                    server_events = [
                        e
                        for e in all_events
                        if e["sender"] == LoopEventSender.SERVER.value
                        and e["nonce"] > last_sent_nonce
                    ]

                    for event in server_events:
                        event_data = json.dumps(event)
                        yield f"data: {event_data}\n\n"
                        last_sent_nonce = max(last_sent_nonce, event["nonce"])

                    if not server_events:
                        yield "data: keepalive\n\n"

                    await asyncio.sleep(self.config.sse_poll_interval_s)

                except asyncio.CancelledError:
                    break
                except Exception as e:
                    logger.error(
                        "Error in SSE stream for loop",
                        extra={"loop_id": loop_id, "error": str(e)},
                    )
                    yield f'data: {{"type": "error", "message": "{e!s}"}}\n\n'
                    break

        return StreamingResponse(
            _event_generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Headers": "Cache-Control",
            },
        )
