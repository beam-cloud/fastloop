"""Loop monitor for background task management."""

import asyncio
import contextlib
import time
from collections.abc import Callable, Coroutine
from queue import Empty, Queue
from typing import TYPE_CHECKING, Any

from .constants import WATCHDOG_INTERVAL_S, WORKFLOW_WAKE_PREFIX
from .context import LoopContext
from .exceptions import LoopNotFoundError
from .logging import setup_logger
from .loop import LoopManager
from .state.state import StateManager
from .types import LoopStatus, TaskStatus

if TYPE_CHECKING:
    from .fastloop import FastLoop

logger = setup_logger(__name__)

HEARTBEAT_S = 60
MAX_CONCURRENT_WAKES = 10


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
        self.fastloop = fastloop_instance

        self._stop = asyncio.Event()
        self._app_started = False
        self._iteration = 0
        self._last_tick = 0.0

    def stop(self) -> None:
        self._stop.set()

    def is_healthy(self, max_stale_s: float = 30.0) -> bool:
        return self._iteration == 0 or (time.time() - self._last_tick) < max_stale_s

    async def run(self) -> None:
        """Main loop."""
        logger.info(f"LoopMonitor started, queue_id={id(self.wake_queue)}")
        try:
            await self._app_start()
            await self._main_loop()
        finally:
            logger.info(f"LoopMonitor exited after {self._iteration} iterations")

    async def _app_start(self) -> None:
        """Process on_app_start callbacks."""
        if self._app_started:
            return

        for loop in await self.state_manager.get_all_loops():
            if loop.status == LoopStatus.STOPPED or not loop.loop_name:
                continue

            meta = self.fastloop._loop_metadata.get(loop.loop_name)
            if not meta or not (instance := meta.get("loop_instance")):
                continue

            if not await self.state_manager.try_acquire_app_start_lock(loop.loop_id):
                continue

            try:
                ctx = LoopContext(
                    loop_id=loop.loop_id,
                    initial_event=await self.state_manager.get_initial_event(
                        loop.loop_id
                    ),
                    state_manager=self.state_manager,
                    integrations=meta.get("integrations", []),
                )
                instance.ctx = ctx
                if await instance.on_app_start(ctx):
                    await self.restart_callback(loop.loop_id)
            finally:
                await self.state_manager.release_app_start_lock(loop.loop_id)

        # Register scheduled tasks
        for name, meta in self.fastloop._task_metadata.items():
            if schedule := meta.get("schedule"):
                await self.state_manager.save_schedule(name, schedule)

        self._app_started = True
        logger.info("LoopMonitor app start complete")

    async def _main_loop(self) -> None:
        """Process wakes and run maintenance."""
        last_heartbeat = time.time()

        while not self._stop.is_set():
            self._iteration += 1
            self._last_tick = time.time()

            try:
                wakes = await self._drain_queue()
                if wakes:
                    logger.info(f"Processing {len(wakes)} wakes: {wakes}")
                    await self._process_wakes(wakes)
                await self._maintenance()
            except Exception as e:
                logger.error(f"Monitor iteration error: {e}")

            if time.time() - last_heartbeat >= HEARTBEAT_S:
                logger.info(
                    f"LoopMonitor heartbeat: iter={self._iteration} queue={self.wake_queue.qsize()} queue_id={id(self.wake_queue)}"
                )
                last_heartbeat = time.time()

    async def _drain_queue(self) -> list[str]:
        """Drain wakes.

        Prefer Redis-backed wake queue (cross-process), fallback to in-memory queue.
        """
        # Redis-backed: fixes multi-worker + multi-replica.
        if hasattr(self.state_manager, "drain_wake_queue"):
            try:
                wakes = await self.state_manager.drain_wake_queue(  # type: ignore[attr-defined]
                    timeout_s=WATCHDOG_INTERVAL_S
                )
                if wakes:
                    logger.info(f"Got {len(wakes)} wakes from redis queue: {wakes[:5]}")
                return wakes
            except Exception as e:
                logger.error(f"Error draining redis wake queue: {e}")

        # Fallback: in-memory queue (single-process only).
        wakes: list[str] = []
        try:
            item = await asyncio.to_thread(
                self.wake_queue.get, True, WATCHDOG_INTERVAL_S
            )
            wakes.append(item)
            logger.info(
                f"Got wake from in-memory queue: {item}, queue_id={id(self.wake_queue)}"
            )
            while True:
                try:
                    wakes.append(self.wake_queue.get_nowait())
                except Empty:
                    break
        except Empty:
            pass
        except Exception as e:
            logger.error(f"Error draining in-memory queue: {e}")
        return wakes

    async def _process_wakes(self, wakes: list[str]) -> None:
        """Process wake events concurrently."""
        sem = asyncio.Semaphore(MAX_CONCURRENT_WAKES)

        async def handle(wake_id: str) -> None:
            async with sem:
                if wake_id.startswith(WORKFLOW_WAKE_PREFIX):
                    await self._wake_workflow(wake_id[len(WORKFLOW_WAKE_PREFIX) :])
                else:
                    await self._wake_loop(wake_id)

        await asyncio.gather(*[handle(w) for w in wakes], return_exceptions=True)

    async def _wake_loop(self, loop_id: str) -> None:
        if await self.state_manager.has_claim(loop_id):
            logger.debug(f"Loop {loop_id} already has claim, skipping wake")
            return

        if not await self.state_manager.try_claim_loop_wake(loop_id):
            logger.debug(f"Loop {loop_id} wake already claimed, skipping")
            return

        logger.info(f"Waking loop: {loop_id}")
        if not await self.restart_callback(loop_id):
            logger.warning(f"Failed to restart loop: {loop_id}")

    async def _wake_workflow(self, run_id: str) -> None:
        if await self.state_manager.workflow_has_claim(run_id):
            return

        if not await self.fastloop.restart_workflow(run_id):
            await self.state_manager.update_workflow_status(run_id, LoopStatus.STOPPED)

        await self.state_manager.clear_workflow_wake_time(run_id)

    async def _maintenance(self) -> None:
        """Run all maintenance tasks."""
        await self._recover_orphaned_loops()
        await self._recover_orphaned_workflows()
        await self._recover_orphaned_tasks()
        await self._check_scheduled_workflows()
        await self._check_scheduled_tasks()
        await self._check_disconnect_stops()

    async def _recover_orphaned_loops(self) -> None:
        for loop in await self.state_manager.get_all_loops(status=LoopStatus.RUNNING):
            if await self.state_manager.has_claim(loop.loop_id):
                continue
            if await self.state_manager.try_claim_loop_recovery(loop.loop_id):
                await self.restart_callback(loop.loop_id)

    async def _recover_orphaned_workflows(self) -> None:
        for wf in await self.state_manager.get_all_workflows(status=LoopStatus.RUNNING):
            if await self.state_manager.workflow_has_claim(wf.workflow_run_id):
                continue
            if not await self.state_manager.try_claim_workflow_recovery(
                wf.workflow_run_id
            ):
                continue
            if not await self.fastloop.restart_workflow(wf.workflow_run_id):
                await self.state_manager.update_workflow_status(
                    wf.workflow_run_id, LoopStatus.STOPPED
                )

    async def _recover_orphaned_tasks(self) -> None:
        for task in await self.state_manager.get_all_tasks(status=TaskStatus.RUNNING):
            if await self.state_manager.task_has_claim(task.task_id):
                continue
            if not await self.state_manager.try_claim_task_recovery(task.task_id):
                continue

            await self.state_manager.update_task_status(task.task_id, TaskStatus.FAILED)
            if meta := self.fastloop._task_metadata.get(task.task_name):
                await self.fastloop.task_manager.submit(
                    func=meta["func"],
                    args=task.args,
                    task_name=task.task_name,
                    retry_policy=meta.get("retry"),
                    executor_type=meta.get("executor"),
                )

    async def _check_scheduled_workflows(self) -> None:
        now = time.time()
        for wf in await self.state_manager.get_all_workflows(status=LoopStatus.IDLE):
            if not wf.scheduled_wake_time or wf.scheduled_wake_time > now:
                continue
            if await self.state_manager.workflow_has_claim(wf.workflow_run_id):
                continue
            if not await self.state_manager.try_claim_workflow_wake(wf.workflow_run_id):
                continue

            if not await self.fastloop.restart_workflow(wf.workflow_run_id):
                await self.state_manager.update_workflow_status(
                    wf.workflow_run_id, LoopStatus.STOPPED
                )
            await self.state_manager.clear_workflow_wake_time(wf.workflow_run_id)

    async def _check_scheduled_tasks(self) -> None:
        for schedule_id, schedule in await self.state_manager.get_due_schedules():
            if not await self.state_manager.try_claim_schedule(schedule_id):
                continue

            try:
                if meta := self.fastloop._task_metadata.get(schedule.task_name):
                    await self.fastloop.task_manager.submit(
                        func=meta["func"],
                        args=schedule.args,
                        task_name=schedule.task_name,
                        retry_policy=meta.get("retry"),
                        executor_type=meta.get("executor"),
                    )
            finally:
                await self.state_manager.advance_schedule(schedule_id, schedule)

    async def _check_disconnect_stops(self) -> None:
        for loop_id in await self.loop_manager.active_loop_ids():
            with contextlib.suppress(LoopNotFoundError):
                loop = await self.state_manager.get_loop(loop_id)
                if not loop.loop_name:
                    continue
                meta = self.fastloop._loop_metadata.get(loop.loop_name)
                if meta and meta.get("stop_on_disconnect"):
                    if not await self.fastloop.has_active_clients(loop_id):
                        await self.state_manager.update_loop_status(
                            loop_id, LoopStatus.STOPPED
                        )
                        await self.loop_manager.stop(loop_id)
