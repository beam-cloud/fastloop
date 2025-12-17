"""
Workflow base class and manager.

This module contains:
- Workflow: Base class for class-based workflow definitions
- WorkflowManager: Manages workflow execution lifecycle
"""

import asyncio
import contextlib
import traceback
from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING, Any

from .constants import CANCEL_GRACE_PERIOD_S
from .exceptions import (
    LoopClaimError,
    LoopPausedError,
    LoopStoppedError,
    WorkflowGotoError,
    WorkflowMaxRetriesError,
    WorkflowNextError,
    WorkflowRepeatError,
)
from .logging import setup_logger
from .models import WorkflowBlock, WorkflowState
from .types import BlockPlan, LoopStatus, RetryPolicy, ScheduleType

if TYPE_CHECKING:
    from .context import LoopContext
    from .state.state import StateManager

logger = setup_logger(__name__)

DEFAULT_RETRY_POLICY = RetryPolicy()


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

    async def plan(
        self,
        _ctx: "LoopContext",
        _blocks: list["WorkflowBlock"],
        _current_block: "WorkflowBlock",
        _block_output: Any,
    ) -> BlockPlan | dict | None:
        """Override to control block execution order and scheduling.

        Args:
            ctx: The workflow context
            blocks: All workflow blocks
            current_block: The block that just executed
            block_output: Return value from execute()

        Returns:
            BlockPlan, dict, or None (None = advance to next block)
        """
        return None

    async def execute(
        self,
        ctx: "LoopContext",
        blocks: list["WorkflowBlock"],
        current_block: "WorkflowBlock",
    ) -> Any:
        raise NotImplementedError("Subclasses must implement execute()")


async def _call(fn: Callable[..., Any] | None, *args: Any) -> None:
    """Call a function (sync or async) if it exists."""
    if fn:
        if asyncio.iscoroutinefunction(fn):
            await fn(*args)
        else:
            fn(*args)


async def _call_with_result(fn: Callable[..., Any] | None, *args: Any) -> Any:
    """Call a function (sync or async) and return result."""
    if fn:
        if asyncio.iscoroutinefunction(fn):
            return await fn(*args)
        else:
            return fn(*args)
    return None


def _dict_to_block_plan(d: dict[str, Any]) -> BlockPlan:
    """Convert a dict to BlockPlan, normalizing schedule_type to handle case differences."""
    schedule_type = d.get("schedule_type", ScheduleType.IMMEDIATE)
    if isinstance(schedule_type, str):
        schedule_type = ScheduleType(schedule_type.lower())
    return BlockPlan(
        next_block_index=d.get("next_block_index"),
        schedule_type=schedule_type,
        delay_seconds=d.get("delay_seconds"),
        reason=d.get("reason"),
    )


class WorkflowManager:
    """Manages workflow execution lifecycle."""

    def __init__(self, state_manager: "StateManager"):
        self.tasks: dict[str, asyncio.Task[None]] = {}
        self.state_manager = state_manager

    async def _persist_block_attempt(
        self, workflow_run_id: str, idx: int, error: str | None = None
    ) -> int:
        workflow = await self.state_manager.get_workflow(workflow_run_id)
        attempts = workflow.block_attempts.get(idx, 0) + 1
        workflow.block_attempts[idx] = attempts
        workflow.last_error = error
        await self.state_manager.update_workflow(workflow_run_id, workflow)
        return attempts

    async def _mark_block_completed(
        self, workflow_run_id: str, idx: int, next_idx: int, payload: dict | None
    ) -> None:
        workflow = await self.state_manager.get_workflow(workflow_run_id)
        if idx not in workflow.completed_blocks:
            workflow.completed_blocks.append(idx)

        workflow.current_block_index = next_idx
        workflow.next_payload = payload
        workflow.block_attempts.pop(idx, None)

        # If going backwards, clear completed status for blocks we're rewinding to
        if next_idx <= idx:
            workflow.completed_blocks = [
                b for b in workflow.completed_blocks if b < next_idx
            ]

        await self.state_manager.update_workflow(workflow_run_id, workflow)

    async def _apply_plan(
        self,
        plan_result: BlockPlan | None,
        workflow_run_id: str,
        idx: int,
        blocks: list[WorkflowBlock],
    ) -> tuple[int | None, float | None]:
        if plan_result is None:
            return idx + 1, None

        if plan_result.schedule_type == ScheduleType.STOP:
            return None, None

        next_idx = plan_result.next_block_index
        if next_idx is None:
            next_idx = idx + 1

        if next_idx < 0 or next_idx >= len(blocks):
            logger.warning(
                "Plan returned invalid block index, using next sequential",
                extra={
                    "workflow_run_id": workflow_run_id,
                    "requested_index": next_idx,
                    "block_count": len(blocks),
                },
            )
            next_idx = idx + 1

        delay = None
        if plan_result.schedule_type == ScheduleType.DELAY:
            delay = plan_result.delay_seconds

        return next_idx, delay

    async def _schedule_delay(
        self,
        workflow_run_id: str,
        delay_seconds: float,
        block_output: Any,
        reason: str | None = None,
    ) -> None:
        """Schedule a workflow to resume after a delay using Redis scheduler."""
        wake_time = datetime.now().timestamp() + delay_seconds
        await self.state_manager.set_workflow_block_output(
            workflow_run_id, block_output
        )
        await self.state_manager.set_workflow_wake_time(workflow_run_id, wake_time)
        logger.info(
            "Workflow scheduled to resume",
            extra={
                "workflow_run_id": workflow_run_id,
                "delay_seconds": delay_seconds,
                "reason": reason,
            },
        )
        raise LoopPausedError()

    async def _run(
        self,
        func: Callable[..., Any],
        context: Any,
        workflow_run_id: str,
        on_start: Callable[..., Any] | None,
        on_stop: Callable[..., Any] | None,
        on_block_complete: Callable[..., Any] | None,
        on_error: Callable[..., Any] | None,
        plan_func: Callable[..., Any] | None,
        retry_policy: RetryPolicy,
    ) -> None:
        try:
            async with self.state_manager.with_workflow_claim(workflow_run_id):
                await self.state_manager.update_workflow_status(
                    workflow_run_id, LoopStatus.RUNNING
                )
                await _call(on_start, context)
                while True:
                    # Verify claim is still held at each block boundary
                    if not await self.state_manager.workflow_has_claim(workflow_run_id):
                        logger.error(
                            "Workflow claim lost, stopping immediately",
                            extra={"workflow_run_id": workflow_run_id},
                        )
                        raise LoopClaimError(
                            f"Claim lost for workflow {workflow_run_id}"
                        )

                    workflow = await self.state_manager.get_workflow(workflow_run_id)
                    if workflow.status in (LoopStatus.STOPPED, LoopStatus.FAILED):
                        break

                    blocks = [WorkflowBlock(**b) for b in workflow.blocks]
                    idx = workflow.current_block_index

                    if idx >= len(blocks):
                        raise LoopStoppedError()

                    if idx in workflow.completed_blocks:
                        await self._mark_block_completed(
                            workflow_run_id, idx, idx + 1, workflow.next_payload
                        )
                        continue

                    current_block = blocks[idx]
                    context.block_index = idx
                    context.block_count = len(blocks)
                    context.blocks = blocks
                    context.current_block = current_block
                    context.previous_payload = workflow.next_payload
                    context.block_output = (
                        await self.state_manager.get_workflow_block_output(
                            workflow_run_id
                        )
                    )

                    try:
                        block_output = await _call_with_result(
                            func, context, blocks, current_block
                        )
                        context.block_output = block_output

                        plan_result: BlockPlan | None = None
                        if plan_func:
                            plan_result = await _call_with_result(
                                plan_func, context, blocks, current_block, block_output
                            )
                            if plan_result is not None:
                                if isinstance(plan_result, dict):
                                    plan_result = _dict_to_block_plan(plan_result)
                                logger.info(
                                    "Plan function returned result",
                                    extra={
                                        "workflow_run_id": workflow_run_id,
                                        "block_index": idx,
                                        "plan": plan_result.to_dict()
                                        if isinstance(plan_result, BlockPlan)
                                        else plan_result,
                                    },
                                )

                        next_idx, delay = await self._apply_plan(
                            plan_result, workflow_run_id, idx, blocks
                        )

                        if next_idx is None:
                            await _call(on_block_complete, context, current_block, None)
                            raise LoopStoppedError()

                        # Only mark block as completed if we're advancing past it
                        # If staying on same block (retry/loop), don't mark completed
                        if next_idx != idx:
                            await self._mark_block_completed(
                                workflow_run_id, idx, next_idx, None
                            )
                            await _call(on_block_complete, context, current_block, None)
                        else:
                            # Staying on same block - just update the current index
                            workflow = await self.state_manager.get_workflow(
                                workflow_run_id
                            )
                            workflow.current_block_index = next_idx
                            await self.state_manager.update_workflow(
                                workflow_run_id, workflow
                            )

                        if delay and delay > 0:
                            reason = plan_result.reason if plan_result else None
                            await self._schedule_delay(
                                workflow_run_id, delay, block_output, reason
                            )

                    except WorkflowNextError as e:
                        await self._mark_block_completed(
                            workflow_run_id, idx, idx + 1, e.payload
                        )
                        await _call(
                            on_block_complete, context, current_block, e.payload
                        )

                    except WorkflowRepeatError:
                        pass

                    except WorkflowGotoError as e:
                        next_idx = e.block_index
                        if next_idx < 0 or next_idx >= len(blocks):
                            logger.warning(
                                "goto() called with invalid index, stopping",
                                extra={
                                    "workflow_run_id": workflow_run_id,
                                    "requested_index": next_idx,
                                    "block_count": len(blocks),
                                },
                            )
                            raise LoopStoppedError() from None

                        await self._mark_block_completed(
                            workflow_run_id, idx, next_idx, None
                        )
                        await _call(on_block_complete, context, current_block, None)

                        if e.delay_seconds and e.delay_seconds > 0:
                            await self._schedule_delay(
                                workflow_run_id, e.delay_seconds, None, e.reason
                            )

                    except (asyncio.CancelledError, LoopPausedError, LoopStoppedError):
                        raise

                    except BaseException as e:
                        error_str = str(e)
                        logger.error(
                            "Workflow block error",
                            extra={
                                "workflow_run_id": workflow_run_id,
                                "block_index": idx,
                                "error": error_str,
                                "traceback": traceback.format_exc(),
                            },
                        )

                        attempts = await self._persist_block_attempt(
                            workflow_run_id, idx, error_str
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
                                    "workflow_run_id": workflow_run_id,
                                    "block_index": idx,
                                    "attempt": attempts,
                                    "delay": delay,
                                },
                            )
                            await asyncio.sleep(delay)
                            continue

                        max_retries_error = WorkflowMaxRetriesError(
                            workflow_run_id, idx, attempts, error_str
                        )
                        logger.error(
                            "Workflow block failed after max retries",
                            extra={
                                "workflow_run_id": workflow_run_id,
                                "block_index": idx,
                                "attempts": attempts,
                            },
                        )
                        await _call(on_error, context, current_block, max_retries_error)
                        await self.state_manager.update_workflow_status(
                            workflow_run_id, LoopStatus.FAILED
                        )
                        await _call(on_stop, context)
                        return

        except asyncio.CancelledError:
            pass
        except LoopClaimError:
            logger.warning(
                "Workflow claim failed", extra={"workflow_run_id": workflow_run_id}
            )
        except LoopStoppedError:
            await self.state_manager.update_workflow_status(
                workflow_run_id, LoopStatus.STOPPED
            )
            await _call(on_stop, context)
        except LoopPausedError:
            await self.state_manager.update_workflow_status(
                workflow_run_id, LoopStatus.IDLE
            )
            # Don't call on_stop - workflow is just paused, not finished
        finally:
            self.tasks.pop(workflow_run_id, None)

    async def start(
        self,
        func: Callable[..., Any],
        context: Any,
        workflow: WorkflowState,
        on_start: Callable[..., Any] | None = None,
        on_stop: Callable[..., Any] | None = None,
        on_block_complete: Callable[..., Any] | None = None,
        on_error: Callable[..., Any] | None = None,
        plan: Callable[..., Any] | None = None,
        retry_policy: RetryPolicy | None = None,
    ) -> bool:
        if workflow.workflow_run_id in self.tasks:
            return False

        self.tasks[workflow.workflow_run_id] = asyncio.create_task(
            self._run(
                func,
                context,
                workflow.workflow_run_id,
                on_start,
                on_stop,
                on_block_complete,
                on_error,
                plan,
                retry_policy or DEFAULT_RETRY_POLICY,
            )
        )
        return True

    async def stop(self, workflow_run_id: str) -> bool:
        task = self.tasks.pop(workflow_run_id, None)
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
