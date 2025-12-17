"""
Loop monitor for background task management.

This module contains:
- LoopMonitor: Background task that monitors loops/workflows and handles wake-ups
"""

import asyncio
import time
from collections.abc import Callable, Coroutine
from queue import Empty, Queue
from typing import TYPE_CHECKING, Any

from .constants import WATCHDOG_INTERVAL_S
from .context import LoopContext
from .exceptions import LoopNotFoundError
from .logging import setup_logger
from .loop import Loop, LoopManager
from .state.state import StateManager
from .types import LoopStatus, TaskStatus

if TYPE_CHECKING:
    from .fastloop import FastLoop

logger = setup_logger(__name__)


class LoopMonitor:
    """Background monitor for loop and workflow lifecycle management."""

    def __init__(
        self,
        state_manager: StateManager,
        loop_manager: LoopManager,
        restart_callback: Callable[[str], Coroutine[Any, Any, bool]],
        wake_queue: Queue[str],
        fastloop_instance: "FastLoop",
    ):
        self.state_manager = state_manager
        self.loop_manager = loop_manager
        self.restart_callback = restart_callback
        self.wake_queue = wake_queue
        self.fastloop_instance = fastloop_instance
        self._stop_event = asyncio.Event()
        self._app_start_processed = False

    def stop(self) -> None:
        self._stop_event.set()

    async def _process_wake(self, wake_id: str) -> None:
        logger.info(
            "Processing wake from queue",
            extra={"wake_id": wake_id},
        )
        if wake_id.startswith("workflow:"):
            workflow_run_id = wake_id[9:]
            if await self.state_manager.workflow_has_claim(workflow_run_id):
                logger.info(
                    "Workflow has active claim, skipping wake",
                    extra={"workflow_run_id": workflow_run_id},
                )
                return
            logger.info(
                "Workflow woke up, attempting restart",
                extra={"workflow_run_id": workflow_run_id},
            )
            try:
                if await self.fastloop_instance.restart_workflow(workflow_run_id):
                    await self.state_manager.clear_workflow_wake_time(workflow_run_id)
                    logger.info(
                        "Workflow restarted successfully",
                        extra={"workflow_run_id": workflow_run_id},
                    )
                else:
                    await self.state_manager.clear_workflow_wake_time(workflow_run_id)
                    await self.state_manager.update_workflow_status(
                        workflow_run_id, LoopStatus.STOPPED
                    )
                    logger.warning(
                        "Workflow restart failed, marked as stopped",
                        extra={"workflow_run_id": workflow_run_id},
                    )
            except Exception as e:
                logger.error(
                    "Error restarting workflow from wake",
                    extra={"workflow_run_id": workflow_run_id, "error": str(e)},
                )
        else:
            loop_id = wake_id
            if await self.state_manager.has_claim(loop_id):
                return
            logger.info("Loop woke up, restarting", extra={"loop_id": loop_id})
            if not await self.restart_callback(loop_id):
                await self.state_manager.update_loop_status(loop_id, LoopStatus.STOPPED)

    async def _check_orphaned_loops(self) -> None:
        running_loops = await self.state_manager.get_all_loops(
            status=LoopStatus.RUNNING
        )
        for loop in running_loops:
            if await self.state_manager.has_claim(loop.loop_id):
                continue
            if not await self.state_manager.try_claim_loop_recovery(loop.loop_id):
                continue

            logger.info("Orphaned loop recovered", extra={"loop_id": loop.loop_id})
            if not await self.restart_callback(loop.loop_id):
                await self.state_manager.update_loop_status(
                    loop.loop_id, LoopStatus.STOPPED
                )

    async def _check_orphaned_workflows(self) -> None:
        running_workflows = await self.state_manager.get_all_workflows(
            status=LoopStatus.RUNNING
        )
        for workflow in running_workflows:
            if await self.state_manager.workflow_has_claim(workflow.workflow_run_id):
                continue
            if not await self.state_manager.try_claim_workflow_recovery(
                workflow.workflow_run_id
            ):
                continue

            logger.info(
                "Orphaned workflow recovered",
                extra={"workflow_run_id": workflow.workflow_run_id},
            )
            if not await self.fastloop_instance.restart_workflow(
                workflow.workflow_run_id
            ):
                await self.state_manager.update_workflow_status(
                    workflow.workflow_run_id, LoopStatus.STOPPED
                )

    async def _check_orphaned_tasks(self) -> None:
        running_tasks = await self.state_manager.get_all_tasks(
            status=TaskStatus.RUNNING
        )
        for task in running_tasks:
            if await self.state_manager.task_has_claim(task.task_id):
                continue
            if not await self.state_manager.try_claim_task_recovery(task.task_id):
                continue

            await self.state_manager.update_task_status(task.task_id, TaskStatus.FAILED)

            metadata = self.fastloop_instance._task_metadata.get(task.task_name)
            if not metadata:
                continue

            logger.info(
                "Orphaned task recovered",
                extra={"task_id": task.task_id, "task_name": task.task_name},
            )
            await self.fastloop_instance.task_manager.submit(
                func=metadata["func"],
                args=task.args,
                task_name=task.task_name,
                retry_policy=metadata.get("retry"),
                executor_type=metadata.get("executor"),
            )

    async def _check_scheduled_workflows(self) -> None:
        now = time.time()
        idle_workflows = await self.state_manager.get_all_workflows(
            status=LoopStatus.IDLE
        )
        for workflow in idle_workflows:
            if not workflow.scheduled_wake_time:
                continue
            if workflow.scheduled_wake_time > now:
                continue
            if await self.state_manager.workflow_has_claim(workflow.workflow_run_id):
                continue
            if not await self.state_manager.try_claim_workflow_wake(
                workflow.workflow_run_id
            ):
                continue

            logger.info(
                "IDLE workflow past-due, restarting",
                extra={"workflow_run_id": workflow.workflow_run_id},
            )
            if await self.fastloop_instance.restart_workflow(workflow.workflow_run_id):
                await self.state_manager.clear_workflow_wake_time(
                    workflow.workflow_run_id
                )
            else:
                await self.state_manager.clear_workflow_wake_time(
                    workflow.workflow_run_id
                )
                await self.state_manager.update_workflow_status(
                    workflow.workflow_run_id, LoopStatus.STOPPED
                )

    async def _check_scheduled_tasks(self) -> None:
        for schedule_id, schedule in await self.state_manager.get_due_schedules():
            if not await self.state_manager.try_claim_schedule(schedule_id):
                continue

            metadata = self.fastloop_instance._task_metadata.get(schedule.task_name)
            if not metadata:
                await self.state_manager.advance_schedule(schedule_id, schedule)
                continue

            try:
                await self.fastloop_instance.task_manager.submit(
                    func=metadata["func"],
                    args=schedule.args,
                    task_name=schedule.task_name,
                    retry_policy=metadata.get("retry"),
                    executor_type=metadata.get("executor"),
                )
            except Exception as e:
                logger.error(
                    "Scheduled task failed",
                    extra={"schedule_id": schedule_id, "error": str(e)},
                )
            finally:
                await self.state_manager.advance_schedule(schedule_id, schedule)

    async def _check_disconnect_stops(self) -> None:
        active_ids = await self.loop_manager.active_loop_ids()
        for loop_id in active_ids:
            try:
                loop = await self.state_manager.get_loop(loop_id)
            except LoopNotFoundError:
                continue
            if not loop.loop_name:
                continue
            metadata = self.fastloop_instance._loop_metadata.get(loop.loop_name)
            if not metadata or not metadata.get("stop_on_disconnect"):
                continue
            if not await self.fastloop_instance.has_active_clients(loop_id):
                logger.info(
                    "Loop has no clients, stopping",
                    extra={"loop_id": loop_id, "loop_name": loop.loop_name},
                )
                await self.state_manager.update_loop_status(loop_id, LoopStatus.STOPPED)
                await self.loop_manager.stop(loop_id)

    async def _process_app_start_callbacks(self) -> None:
        """Call on_app_start for each non-stopped class-based loop instance."""
        all_loops = await self.state_manager.get_all_loops()

        for loop in all_loops:
            if loop.status == LoopStatus.STOPPED or not loop.loop_name:
                continue

            metadata = self.fastloop_instance._loop_metadata.get(loop.loop_name)
            loop_instance: Loop | None = (
                metadata.get("loop_instance") if metadata else None
            )
            if not loop_instance:
                continue

            if not await self.state_manager.try_acquire_app_start_lock(loop.loop_id):
                continue

            try:
                context = LoopContext(
                    loop_id=loop.loop_id,
                    initial_event=await self.state_manager.get_initial_event(
                        loop.loop_id
                    ),
                    state_manager=self.state_manager,
                    integrations=metadata.get("integrations", []),  # type: ignore
                )
                loop_instance.ctx = context

                if await loop_instance.on_app_start(context):
                    logger.info(
                        "on_app_start returned True, starting loop",
                        extra={"loop_id": loop.loop_id, "loop_name": loop.loop_name},
                    )
                    if not await self.restart_callback(loop.loop_id):
                        await self.state_manager.update_loop_status(
                            loop.loop_id, LoopStatus.STOPPED
                        )
            except Exception as e:
                logger.error(
                    "Error in on_app_start",
                    extra={"loop_id": loop.loop_id, "error": str(e)},
                )
            finally:
                await self.state_manager.release_app_start_lock(loop.loop_id)

    async def _register_schedules(self) -> None:
        for name, meta in self.fastloop_instance._task_metadata.items():
            if schedule := meta.get("schedule"):
                await self.state_manager.save_schedule(name, schedule)

    async def run(self):
        """Main monitor loop."""
        logger.info("LoopMonitor started")

        if not self._app_start_processed:
            try:
                await self._process_app_start_callbacks()
                await self._register_schedules()
            except Exception as e:
                logger.error(
                    "LoopMonitor startup failed (continuing without app-start processing)",
                    extra={"error": str(e)},
                )
            finally:
                self._app_start_processed = True

        while not self._stop_event.is_set():
            try:
                # Block for up to WATCHDOG_INTERVAL_S to avoid polling the event loop.
                # This is a threadsafe Queue fed by the Redis wake thread.
                wake_batch: list[str] = []
                try:
                    first = await asyncio.to_thread(
                        self.wake_queue.get, True, WATCHDOG_INTERVAL_S
                    )
                    wake_batch.append(first)
                except Empty:
                    pass

                # Drain any additional wakes immediately.
                if wake_batch:
                    while True:
                        try:
                            wake_batch.append(self.wake_queue.get_nowait())
                        except Empty:
                            break

                processed_wakes = 0
                for wake_id in wake_batch:
                    try:
                        await self._process_wake(wake_id)
                        processed_wakes += 1
                    except Exception as e:
                        logger.error(
                            "Error processing wake",
                            extra={"wake_id": wake_id, "error": str(e)},
                        )
                if processed_wakes:
                    logger.info(
                        "Processed wakes from queue",
                        extra={
                            "count": processed_wakes,
                            "queue_size": self.wake_queue.qsize(),
                        },
                    )

                await self._check_orphaned_loops()
                await self._check_orphaned_workflows()
                await self._check_orphaned_tasks()
                await self._check_scheduled_workflows()
                await self._check_scheduled_tasks()
                await self._check_disconnect_stops()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Error in monitor", extra={"error": str(e)})
                await asyncio.sleep(WATCHDOG_INTERVAL_S)
