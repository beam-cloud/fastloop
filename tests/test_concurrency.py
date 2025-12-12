"""
Tests for concurrency safety and race condition prevention.

These tests verify that the system is safe in multi-replica environments:
- Only one replica can hold a claim at a time
- Orphan detection doesn't create duplicates
- Scheduled tasks fire exactly once
- Recovery claims are atomic
"""

import asyncio
import uuid

import pytest

from fastloop import FastLoop
from fastloop.models import TaskState
from fastloop.scheduler import Schedule
from fastloop.types import LoopStatus, TaskStatus


def unique_id() -> str:
    return str(uuid.uuid4())[:8]


class TestClaimExclusivity:
    """Verify that claims prevent concurrent holding."""

    @pytest.mark.asyncio
    async def test_only_one_can_hold_workflow_claim(self):
        """Only one holder can exist at a time for a workflow claim."""
        app = FastLoop(name=f"test-{unique_id()}")
        wf_id = unique_id()

        await app.state_manager.get_or_create_workflow(
            workflow_name="test",
            workflow_run_id=wf_id,
            blocks=[{"type": "t", "text": "x"}],
        )

        holders = []
        lock = asyncio.Lock()

        async def try_hold():
            async with app.state_manager.with_workflow_claim(wf_id):
                async with lock:
                    holders.append(1)
                    current = len(holders)
                await asyncio.sleep(0.05)
                async with lock:
                    holders.pop()
                return current

        results = await asyncio.gather(
            try_hold(), try_hold(), try_hold(), return_exceptions=True
        )

        successes = [r for r in results if isinstance(r, int)]
        assert all(h == 1 for h in successes), "Multiple concurrent holders detected!"

    @pytest.mark.asyncio
    async def test_only_one_can_hold_task_claim(self):
        """Only one holder can exist at a time for a task claim."""
        app = FastLoop(name=f"test-{unique_id()}")
        task_id = unique_id()

        task = TaskState(task_id=task_id, task_name="test")
        await app.state_manager.create_task(task)

        holders = []
        lock = asyncio.Lock()

        async def try_hold():
            async with app.state_manager.with_task_claim(task_id):
                async with lock:
                    holders.append(1)
                    current = len(holders)
                await asyncio.sleep(0.05)
                async with lock:
                    holders.pop()
                return current

        results = await asyncio.gather(
            try_hold(), try_hold(), try_hold(), return_exceptions=True
        )

        successes = [r for r in results if isinstance(r, int)]
        assert all(h == 1 for h in successes), "Multiple concurrent holders detected!"


class TestRecoveryClaimAtomicity:
    """Verify recovery claims are atomic - only one caller can win."""

    @pytest.mark.asyncio
    async def test_task_recovery_claim_atomic(self):
        """Only one caller wins the task recovery claim."""
        app = FastLoop(name=f"test-{unique_id()}")
        task_id = unique_id()

        results = await asyncio.gather(
            app.state_manager.try_claim_task_recovery(task_id),
            app.state_manager.try_claim_task_recovery(task_id),
            app.state_manager.try_claim_task_recovery(task_id),
        )

        assert sum(results) == 1

    @pytest.mark.asyncio
    async def test_loop_recovery_claim_atomic(self):
        """Only one caller wins the loop recovery claim."""
        app = FastLoop(name=f"test-{unique_id()}")
        loop_id = unique_id()

        results = await asyncio.gather(
            app.state_manager.try_claim_loop_recovery(loop_id),
            app.state_manager.try_claim_loop_recovery(loop_id),
            app.state_manager.try_claim_loop_recovery(loop_id),
        )

        assert sum(results) == 1

    @pytest.mark.asyncio
    async def test_workflow_recovery_claim_atomic(self):
        """Only one caller wins the workflow recovery claim."""
        app = FastLoop(name=f"test-{unique_id()}")
        wf_id = unique_id()

        results = await asyncio.gather(
            app.state_manager.try_claim_workflow_recovery(wf_id),
            app.state_manager.try_claim_workflow_recovery(wf_id),
            app.state_manager.try_claim_workflow_recovery(wf_id),
        )

        assert sum(results) == 1


class TestScheduleClaimAtomicity:
    """Verify schedule claims are atomic - only one replica fires scheduled task."""

    @pytest.mark.asyncio
    async def test_schedule_claim_atomic(self):
        """Only one caller wins the schedule claim."""
        app = FastLoop(name=f"test-{unique_id()}")
        sched_id = unique_id()

        schedule = Schedule(task_name="test", interval_seconds=1)
        await app.state_manager.save_schedule(sched_id, schedule)

        results = await asyncio.gather(
            app.state_manager.try_claim_schedule(sched_id),
            app.state_manager.try_claim_schedule(sched_id),
            app.state_manager.try_claim_schedule(sched_id),
        )

        assert sum(results) == 1


class TestStatusTransitions:
    """Verify status transitions happen at the right time."""

    @pytest.mark.asyncio
    async def test_workflow_starts_pending(self):
        """New workflow should have PENDING status."""
        app = FastLoop(name=f"test-{unique_id()}")

        workflow, created = await app.state_manager.get_or_create_workflow(
            workflow_name="test",
            workflow_run_id=unique_id(),
            blocks=[{"type": "t", "text": "x"}],
        )

        assert created
        assert workflow.status == LoopStatus.PENDING

    @pytest.mark.asyncio
    async def test_task_starts_pending(self):
        """New task should have PENDING status."""
        app = FastLoop(name=f"test-{unique_id()}")
        task_id = unique_id()

        task = TaskState(task_id=task_id, task_name="test")
        await app.state_manager.create_task(task)

        retrieved = await app.state_manager.get_task(task_id)
        assert retrieved.status == TaskStatus.PENDING

    @pytest.mark.asyncio
    async def test_running_set_after_claim(self):
        """RUNNING status should only be set after claim is acquired."""
        app = FastLoop(name=f"test-{unique_id()}")
        wf_id = unique_id()

        workflow, _ = await app.state_manager.get_or_create_workflow(
            workflow_name="test",
            workflow_run_id=wf_id,
            blocks=[{"type": "t", "text": "x"}],
        )

        assert workflow.status == LoopStatus.PENDING

        async with app.state_manager.with_workflow_claim(wf_id):
            await app.state_manager.update_workflow_status(wf_id, LoopStatus.RUNNING)
            wf = await app.state_manager.get_workflow(wf_id)
            assert wf.status == LoopStatus.RUNNING


class TestOrphanDetectionSafety:
    """Verify orphan detection only triggers for truly orphaned entities."""

    @pytest.mark.asyncio
    async def test_pending_workflow_not_in_running_query(self):
        """PENDING workflows should not appear in RUNNING status query."""
        app = FastLoop(name=f"test-{unique_id()}")
        wf_id = unique_id()

        workflow, _ = await app.state_manager.get_or_create_workflow(
            workflow_name="test",
            workflow_run_id=wf_id,
            blocks=[{"type": "t", "text": "x"}],
        )

        assert workflow.status == LoopStatus.PENDING

        running = await app.state_manager.get_all_workflows(status=LoopStatus.RUNNING)
        assert wf_id not in [w.workflow_run_id for w in running]

    @pytest.mark.asyncio
    async def test_pending_task_not_in_running_query(self):
        """PENDING tasks should not appear in RUNNING status query."""
        app = FastLoop(name=f"test-{unique_id()}")
        task_id = unique_id()

        task = TaskState(task_id=task_id, task_name="test")
        await app.state_manager.create_task(task)

        running = await app.state_manager.get_all_tasks(status=TaskStatus.RUNNING)
        assert task_id not in [t.task_id for t in running]

    @pytest.mark.asyncio
    async def test_claimed_workflow_has_claim(self):
        """Workflow with active claim should report has_claim=True."""
        app = FastLoop(name=f"test-{unique_id()}")
        wf_id = unique_id()

        await app.state_manager.get_or_create_workflow(
            workflow_name="test",
            workflow_run_id=wf_id,
            blocks=[{"type": "t", "text": "x"}],
        )

        async with app.state_manager.with_workflow_claim(wf_id):
            has_claim = await app.state_manager.workflow_has_claim(wf_id)
            assert has_claim

        has_claim_after = await app.state_manager.workflow_has_claim(wf_id)
        assert not has_claim_after
