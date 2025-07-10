import asyncio
import json
import logging
from collections.abc import Callable
from datetime import datetime
from typing import Any

from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from .constants import CANCEL_GRACE_PERIOD_S
from .context import LoopContext
from .exceptions import LoopClaimError, LoopPausedError, LoopStoppedError
from .state.state import LoopState, StateManager
from .types import BaseConfig, LoopEventSender, LoopStatus

logger = logging.getLogger(__name__)


class LoopEvent(BaseModel):
    loop_id: str | None = None
    type: str = Field(default_factory=lambda: getattr(LoopEvent, "type", ""))
    sender: LoopEventSender = LoopEventSender.CLIENT
    timestamp: float = Field(default_factory=lambda: datetime.now().timestamp())

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
                        logger.info(f"{loop_id}: Task cancelled, exiting")
                        break
                    except BaseException as e:
                        logger.error(f"{loop_id}: {e}")

                    try:
                        await asyncio.sleep(delay)
                    except asyncio.CancelledError:
                        logger.info(f"{loop_id}: Task cancelled during sleep, exiting")
                        break

                if context.should_stop:
                    raise LoopStoppedError()
                elif context.should_pause:
                    raise LoopPausedError()

        except asyncio.CancelledError:
            logger.info(f"{loop_id}: Task cancelled, exiting")
        except LoopClaimError:
            pass
        except LoopStoppedError:
            await self.state_manager.update_loop_status(loop_id, LoopStatus.STOPPED)
        except LoopPausedError:
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
                logger.warning(f"Task {loop_id} did not stop within timeout")

            return True

        return False

    async def stop_all(self):
        """Stop all running tasks and wait for them to complete."""

        tasks_to_cancel = list(self.loop_tasks.values())
        self.loop_tasks.clear()

        for task in tasks_to_cancel:
            task.cancel()

        # Wait for all tasks to complete (w/ timeout)
        if tasks_to_cancel:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*tasks_to_cancel, return_exceptions=True),
                    timeout=CANCEL_GRACE_PERIOD_S,
                )
            except TimeoutError:
                logger.warning("Some tasks did not complete within timeout")
            except BaseException as e:
                logger.error(f"Error waiting for tasks to complete: {e}")

    async def active_loop_ids(self) -> set[str]:
        """
        Returns a set of loop IDs with tasks that are currently running in this replica.
        """

        return {loop_id for loop_id, _ in self.loop_tasks.items()}

    async def events_sse(self, loop_id: str, event_type: str):
        """
        SSE endpoint for streaming events to clients.
        """

        async def _event_generator():
            yield (
                'data: {"type": "connection_established", "loop_id": "'
                + loop_id
                + '"}\n\n'
            )

            while True:
                try:
                    event: LoopEvent | None = await self.state_manager.pop_event(
                        loop_id=loop_id,
                        event_type=event_type,
                        sender=LoopEventSender.SERVER,
                    )

                    if event:
                        event_data = event.to_string()
                        yield f"data: {event_data}\n\n"
                    else:
                        yield ": keepalive\n\n"

                    await asyncio.sleep(self.config.sse_poll_interval_s)

                except asyncio.CancelledError:
                    break
                except Exception as e:
                    logger.error(f"Error in SSE stream for loop {loop_id}: {e}")
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
