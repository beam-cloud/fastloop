import asyncio
import traceback
import uuid
from collections.abc import Callable
from contextlib import suppress
from datetime import datetime
from typing import TYPE_CHECKING, Any

from .constants import CANCEL_GRACE_PERIOD_S
from .exceptions import LoopClaimError
from .executor import run_in_executor
from .logging import setup_logger
from .models import TaskState
from .types import ExecutorType, RetryPolicy, TaskStatus

if TYPE_CHECKING:
    from .state.state import StateManager

logger = setup_logger(__name__)

DEFAULT_RETRY_POLICY = RetryPolicy()


class TaskResult:
    """Handle for a submitted task. Use to check status or wait for result."""

    def __init__(self, task_id: str, state_manager: "StateManager"):
        self.task_id = task_id
        self._sm = state_manager

    async def status(self) -> TaskStatus:
        """Get current task status (PENDING, RUNNING, SUCCESS, FAILED, RETRYING)."""
        return (await self._sm.get_task(self.task_id)).status

    async def result(self, timeout: float | None = None) -> Any:
        """Wait for task completion and return result. Raises on failure."""
        task = await self._wait_for_completion(timeout)
        if task.status == TaskStatus.FAILED:
            raise Exception(task.error or "Task failed")
        return await self._sm.get_task_result(self.task_id)

    async def wait(self, timeout: float | None = None) -> TaskState:
        """Wait for task completion and return full TaskState."""
        return await self._wait_for_completion(timeout)

    async def _wait_for_completion(self, timeout: float | None) -> TaskState:
        start = asyncio.get_event_loop().time()
        while True:
            task = await self._sm.get_task(self.task_id)
            if task.status in (TaskStatus.SUCCESS, TaskStatus.FAILED):
                return task
            if timeout and (asyncio.get_event_loop().time() - start) >= timeout:
                raise TimeoutError(f"Task {self.task_id} timed out")
            await asyncio.sleep(0.1)


class TaskManager:
    def __init__(self, state_manager: "StateManager"):
        self.state_manager = state_manager
        self.tasks: dict[str, asyncio.Task[None]] = {}

    async def submit(
        self,
        func: Callable[..., Any],
        args: dict[str, Any],
        task_name: str,
        retry_policy: RetryPolicy | None = None,
        executor_type: ExecutorType = ExecutorType.ASYNC,
    ) -> TaskResult:
        task_id = str(uuid.uuid4())
        await self.state_manager.create_task(
            TaskState(task_id=task_id, task_name=task_name, args=args)
        )
        self.tasks[task_id] = asyncio.create_task(
            self._run(
                task_id, func, args, retry_policy or DEFAULT_RETRY_POLICY, executor_type
            )
        )
        return TaskResult(task_id, self.state_manager)

    async def _run(
        self,
        task_id: str,
        func: Callable[..., Any],
        args: dict[str, Any],
        retry_policy: RetryPolicy,
        executor_type: ExecutorType,
    ) -> None:
        try:
            async with self.state_manager.with_task_claim(task_id):
                await self.state_manager.update_task_status(task_id, TaskStatus.RUNNING)
                attempts = 0

                while attempts < retry_policy.max_attempts:
                    attempts += 1
                    try:
                        result = await run_in_executor(executor_type, func, **args)
                        await self._complete_task(task_id, result, attempts)
                        return
                    except Exception as e:
                        logger.error(
                            "Task error",
                            extra={
                                "task_id": task_id,
                                "attempt": attempts,
                                "error": str(e),
                                "traceback": traceback.format_exc(),
                            },
                        )
                        if attempts < retry_policy.max_attempts:
                            await self._mark_retrying(task_id, attempts, str(e))
                            await asyncio.sleep(retry_policy.compute_delay(attempts))
                        else:
                            await self._fail_task(task_id, attempts, str(e))
                            return

        except LoopClaimError:
            logger.warning("Task claim failed", extra={"task_id": task_id})
        except asyncio.CancelledError:
            pass
        finally:
            self.tasks.pop(task_id, None)

    async def _complete_task(self, task_id: str, result: Any, attempts: int) -> None:
        task = await self.state_manager.get_task(task_id)
        task.status = TaskStatus.SUCCESS
        task.result = result
        task.attempts = attempts
        task.completed_at = datetime.now().timestamp()
        await self.state_manager.update_task(task)
        await self.state_manager.set_task_result(task_id, result)
        logger.info("Task completed", extra={"task_id": task_id, "attempts": attempts})

    async def _mark_retrying(self, task_id: str, attempts: int, error: str) -> None:
        task = await self.state_manager.get_task(task_id)
        task.status = TaskStatus.RETRYING
        task.attempts = attempts
        task.error = error
        await self.state_manager.update_task(task)

    async def _fail_task(self, task_id: str, attempts: int, error: str) -> None:
        task = await self.state_manager.get_task(task_id)
        task.status = TaskStatus.FAILED
        task.error = error
        task.attempts = attempts
        task.completed_at = datetime.now().timestamp()
        await self.state_manager.update_task(task)

    async def stop(self, task_id: str) -> bool:
        task = self.tasks.pop(task_id, None)
        if not task:
            return False
        task.cancel()
        with suppress(asyncio.CancelledError, TimeoutError):
            await asyncio.wait_for(task, timeout=CANCEL_GRACE_PERIOD_S)
        return True

    async def stop_all(self) -> None:
        tasks = list(self.tasks.values())
        self.tasks.clear()
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
