"""
Integration tests for workflows.
Tests HTTP endpoints and full workflow lifecycle.
"""

import asyncio
import os
import uuid
from contextlib import asynccontextmanager
from queue import Queue
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from fastloop import FastLoop, LoopContext, LoopEvent, RetryPolicy
from fastloop.loop import WorkflowManager, WorkflowState
from fastloop.types import LoopStatus

# --- Fixtures ---


@pytest.fixture
def mock_state():
    """Mock state manager with proper state tracking."""
    state = AsyncMock()
    workflows = {}

    @asynccontextmanager
    async def mock_claim(_wid):
        yield

    async def get_workflow(wid):
        if wid in workflows:
            return workflows[wid]
        return WorkflowState(workflow_run_id=wid, status=LoopStatus.RUNNING)

    async def update_workflow(wid, w):
        workflows[wid] = w

    state.with_workflow_claim = mock_claim
    state.get_workflow = get_workflow
    state.update_workflow = update_workflow
    state.update_workflow_status = AsyncMock()
    state.update_workflow_block_index = AsyncMock()
    state._workflows = workflows
    return state


@pytest.fixture
def app():
    """App with workflow for HTTP testing."""
    app = FastLoop(name="test-app")

    @app.event("start")
    class Start(LoopEvent):
        pass

    @app.workflow(name="test", start_event=Start)
    async def test_workflow(ctx, _blocks, _block):
        ctx.next()

    return app


# --- HTTP Lifecycle ---


class TestHTTPLifecycle:
    def test_routes_registered(self, app):
        paths = [r.path for r in app.routes]
        assert "/test" in paths
        assert "/test/{workflow_run_id}" in paths
        assert "/test/{workflow_run_id}/event" in paths
        assert "/test/{workflow_run_id}/cancel" in paths

    def test_start_returns_id(self, app):
        client = TestClient(app)
        resp = client.post(
            "/test", json={"type": "start", "blocks": [{"type": "s", "text": "t"}]}
        )
        assert resp.status_code == 200
        assert "workflow_run_id" in resp.json()
        assert resp.json()["status"] == "running"

    def test_get_status(self, app):
        client = TestClient(app)
        resp = client.post(
            "/test", json={"type": "start", "blocks": [{"type": "s", "text": "t"}]}
        )
        wid = resp.json()["workflow_run_id"]

        resp = client.get(f"/test/{wid}")
        assert resp.status_code == 200
        assert resp.json()["workflow_run_id"] == wid

    def test_cancel(self, app):
        client = TestClient(app)
        resp = client.post(
            "/test", json={"type": "start", "blocks": [{"type": "s", "text": "t"}]}
        )
        wid = resp.json()["workflow_run_id"]

        resp = client.post(f"/test/{wid}/cancel")
        assert resp.status_code == 200


# --- Control Flow ---


class TestControlFlow:
    async def test_next_advances_blocks(self, mock_state):
        """ctx.next() advances to next block."""
        executed = []

        workflow = WorkflowState(
            workflow_run_id="test",
            blocks=[{"type": "a", "text": ""}, {"type": "b", "text": ""}],
            current_block_index=0,
            status=LoopStatus.RUNNING,
        )
        mock_state._workflows["test"] = workflow

        async def func(ctx, _blocks, block):
            executed.append(block.type)
            ctx.next()

        ctx = MagicMock()
        ctx.next = LoopContext.next.__get__(ctx, LoopContext)

        wm = WorkflowManager(mock_state)
        await wm.start(func, ctx, workflow)
        await asyncio.sleep(0.1)

        assert executed == ["a", "b"]

    async def test_repeat_stays_on_block(self, mock_state):
        """ctx.repeat() re-runs current block."""
        count = [0]

        workflow = WorkflowState(
            workflow_run_id="test",
            blocks=[{"type": "step", "text": ""}, {"type": "done", "text": ""}],
            status=LoopStatus.RUNNING,
        )
        mock_state._workflows["test"] = workflow

        async def func(ctx, _blocks, _block):
            count[0] += 1
            if count[0] < 3:
                ctx.repeat()

        ctx = MagicMock()
        ctx.repeat = LoopContext.repeat.__get__(ctx, LoopContext)

        wm = WorkflowManager(mock_state)
        await wm.start(func, ctx, workflow)
        await asyncio.sleep(0.1)

        assert count[0] >= 3
        assert workflow.current_block_index >= 1  # Advanced after repeat finished

    async def test_normal_return_advances(self, mock_state):
        """Normal return advances to next block (default behavior)."""
        executed = []

        workflow = WorkflowState(
            workflow_run_id="test",
            blocks=[{"type": "a", "text": ""}, {"type": "b", "text": ""}],
            status=LoopStatus.RUNNING,
        )
        mock_state._workflows["test"] = workflow

        async def func(_ctx, _blocks, block):
            executed.append(block.type)

        wm = WorkflowManager(mock_state)
        await wm.start(func, MagicMock(), workflow)
        await asyncio.sleep(0.1)

        assert executed == ["a", "b"]
        mock_state.update_workflow_status.assert_called_with("test", LoopStatus.STOPPED)

    async def test_next_passes_payload(self, mock_state):
        """ctx.next(payload) passes data to next block."""
        workflow = WorkflowState(
            workflow_run_id="test",
            blocks=[{"type": "s", "text": ""}],
            current_block_index=0,
            status=LoopStatus.RUNNING,
        )
        mock_state._workflows["test"] = workflow

        async def func(ctx, _blocks, _block):
            ctx.next({"key": "value"})

        ctx = MagicMock()
        ctx.next = LoopContext.next.__get__(ctx, LoopContext)

        wm = WorkflowManager(mock_state)
        await wm.start(func, ctx, workflow)
        await asyncio.sleep(0.1)

        assert workflow.next_payload == {"key": "value"}


# --- Callbacks ---


class TestCallbacks:
    async def test_on_block_complete_called(self, mock_state):
        completed = []

        workflow = WorkflowState(
            workflow_run_id="test",
            blocks=[{"type": "a", "text": ""}, {"type": "b", "text": ""}],
            current_block_index=0,
            status=LoopStatus.RUNNING,
        )
        mock_state._workflows["test"] = workflow

        def on_complete(_ctx, block, _payload):
            completed.append(block.type)

        async def func(ctx, _blocks, _block):
            ctx.next()

        ctx = MagicMock()
        ctx.next = LoopContext.next.__get__(ctx, LoopContext)

        wm = WorkflowManager(mock_state)
        await wm.start(func, ctx, workflow, on_block_complete=on_complete)
        await asyncio.sleep(0.1)

        assert completed == ["a", "b"]

    async def test_on_block_complete_on_normal_return(self, mock_state):
        """on_block_complete fires even when workflow ends via normal return."""
        completed = []

        workflow = WorkflowState(
            workflow_run_id="test",
            blocks=[{"type": "final", "text": ""}],
            status=LoopStatus.RUNNING,
        )
        mock_state._workflows["test"] = workflow

        def on_complete(_ctx, block, _payload):
            completed.append(block.type)

        async def func(_ctx, _blocks, _block):
            pass

        wm = WorkflowManager(mock_state)
        await wm.start(func, MagicMock(), workflow, on_block_complete=on_complete)
        await asyncio.sleep(0.1)

        assert completed == ["final"]

    async def test_on_error_called(self, mock_state):
        errors = []

        workflow = WorkflowState(
            workflow_run_id="test",
            blocks=[{"type": "bad", "text": ""}],
            status=LoopStatus.RUNNING,
        )
        mock_state._workflows["test"] = workflow

        def on_error(_ctx, block, error):
            errors.append((block.type, str(error)))

        async def func(_ctx, _blocks, _block):
            raise ValueError("boom")

        wm = WorkflowManager(mock_state)
        await wm.start(
            func,
            MagicMock(),
            workflow,
            on_error=on_error,
            retry_policy=RetryPolicy(max_attempts=1),
        )
        await asyncio.sleep(0.1)

        assert len(errors) >= 1
        assert errors[0][0] == "bad"
        assert "boom" in errors[0][1]

    async def test_on_error_can_retry(self, mock_state):
        """on_error can raise ctx.repeat() to retry the block."""
        attempts = [0]

        workflow = WorkflowState(
            workflow_run_id="test",
            blocks=[{"type": "flaky", "text": ""}],
            status=LoopStatus.RUNNING,
        )
        mock_state._workflows["test"] = workflow

        def on_error(ctx, _block, _error):
            if attempts[0] < 3:
                ctx.repeat()

        async def func(_ctx, _blocks, _block):
            attempts[0] += 1
            if attempts[0] < 3:
                raise ValueError("transient error")

        ctx = MagicMock()
        ctx.repeat = LoopContext.repeat.__get__(ctx, LoopContext)

        wm = WorkflowManager(mock_state)
        await wm.start(
            func,
            ctx,
            workflow,
            on_error=on_error,
            retry_policy=RetryPolicy(max_attempts=5, initial_delay=0.01),
        )
        await asyncio.sleep(0.3)

        assert attempts[0] == 3


# --- Context Attributes ---


class TestContextAttributes:
    async def test_block_position(self, mock_state):
        """Context has block_index, block_count, previous_payload."""
        captured = []

        workflow = WorkflowState(
            workflow_run_id="test",
            blocks=[{"type": "a", "text": ""}, {"type": "b", "text": ""}],
            current_block_index=0,
            status=LoopStatus.RUNNING,
        )
        mock_state._workflows["test"] = workflow

        async def func(ctx, _blocks, _block):
            captured.append(
                {
                    "index": ctx.block_index,
                    "count": ctx.block_count,
                    "prev": ctx.previous_payload,
                }
            )
            ctx.next({"prev": True})

        ctx = LoopContext(loop_id="test", state_manager=mock_state)

        wm = WorkflowManager(mock_state)
        await wm.start(func, ctx, workflow)
        await asyncio.sleep(0.1)

        assert captured[0] == {"index": 0, "count": 2, "prev": None}
        assert captured[1] == {"index": 1, "count": 2, "prev": {"prev": True}}


# --- Durability ---


class TestDurability:
    def test_restart_method_exists(self):
        app = FastLoop(name="test")
        assert hasattr(app, "restart_workflow")

    async def test_resumes_from_block_index(self, mock_state):
        """Workflow resumes from persisted block index."""
        executed = []

        workflow = WorkflowState(
            workflow_run_id="test",
            blocks=[{"type": "skip", "text": ""}, {"type": "run", "text": ""}],
            current_block_index=1,
            status=LoopStatus.RUNNING,
        )
        mock_state._workflows["test"] = workflow

        async def func(_ctx, _blocks, block):
            executed.append(block.type)

        wm = WorkflowManager(mock_state)
        await wm.start(func, MagicMock(), workflow)
        await asyncio.sleep(0.1)

        assert executed == ["run"]

    async def test_ctx_state_persists(self, mock_state):
        """ctx.set/get persists state."""
        stored = {}

        async def set_val(_wid, key, val):
            stored[key] = val

        async def get_val(_wid, key):
            return stored.get(key)

        mock_state.set_context_value = set_val
        mock_state.get_context_value = get_val

        ctx = LoopContext(loop_id="test", state_manager=mock_state)

        await ctx.set("mykey", {"nested": "value"})
        result = await ctx.get("mykey")

        assert result == {"nested": "value"}


# --- Crash Recovery Tests (Redis required) ---


@pytest.mark.skipif(
    not os.environ.get("REDIS_TEST_HOST"),
    reason="Set REDIS_TEST_HOST to run",
)
class TestCrashRecovery:
    @pytest.fixture
    async def state_manager(self):
        from fastloop.state.state_redis import RedisStateManager
        from fastloop.types import RedisConfig

        config = RedisConfig(
            host=os.environ.get("REDIS_TEST_HOST", "localhost"),
            port=int(os.environ.get("REDIS_TEST_PORT", "6379")),
            database=int(os.environ.get("REDIS_TEST_DB", "15")),
            password=os.environ.get("REDIS_TEST_PASSWORD", ""),
            ssl=os.environ.get("REDIS_TEST_SSL", "").lower() == "true",
        )
        manager = RedisStateManager(
            app_name=f"test-{uuid.uuid4().hex[:8]}",
            config=config,
            wake_queue=Queue(),
        )
        yield manager
        manager.stop()
        await manager.rdb.flushdb()

    async def test_lease_acquired_and_released(self, state_manager):
        """Workflow claims are properly acquired and released."""
        workflow, _ = await state_manager.get_or_create_workflow(
            workflow_name="test",
            blocks=[{"type": "step", "text": "test"}],
        )

        assert not await state_manager.workflow_has_claim(workflow.workflow_run_id)

        async with state_manager.with_workflow_claim(workflow.workflow_run_id):
            assert await state_manager.workflow_has_claim(workflow.workflow_run_id)

        await asyncio.sleep(0.1)
        assert not await state_manager.workflow_has_claim(workflow.workflow_run_id)

    async def test_lease_expires_on_timeout(self, state_manager):
        """Lease expires if not refreshed (simulates crash)."""
        workflow, _ = await state_manager.get_or_create_workflow(
            workflow_name="test",
            blocks=[{"type": "step", "text": "test"}],
        )

        lease_key = f"fastloop:{state_manager.app_name}:workflow_claim:{workflow.workflow_run_id}"
        await state_manager.rdb.set(lease_key, "dead-owner", ex=1)

        assert await state_manager.workflow_has_claim(workflow.workflow_run_id)
        await asyncio.sleep(1.5)
        assert not await state_manager.workflow_has_claim(workflow.workflow_run_id)

    async def test_block_attempts_persisted(self, state_manager):
        """Block attempts are persisted to Redis."""
        workflow, _ = await state_manager.get_or_create_workflow(
            workflow_name="test",
            blocks=[{"type": "step", "text": "test"}],
        )

        workflow.block_attempts[0] = 2
        workflow.last_error = "test error"
        await state_manager.update_workflow(workflow.workflow_run_id, workflow)

        loaded = await state_manager.get_workflow(workflow.workflow_run_id)
        assert loaded.block_attempts == {0: 2}
        assert loaded.last_error == "test error"

    async def test_completed_blocks_persisted(self, state_manager):
        """Completed blocks list is persisted."""
        workflow, _ = await state_manager.get_or_create_workflow(
            workflow_name="test",
            blocks=[
                {"type": "a", "text": ""},
                {"type": "b", "text": ""},
                {"type": "c", "text": ""},
            ],
        )

        workflow.completed_blocks = [0, 1]
        workflow.current_block_index = 2
        await state_manager.update_workflow(workflow.workflow_run_id, workflow)

        loaded = await state_manager.get_workflow(workflow.workflow_run_id)
        assert loaded.completed_blocks == [0, 1]
        assert loaded.current_block_index == 2

    async def test_failed_status_persisted(self, state_manager):
        """FAILED status is persisted and loaded correctly."""
        workflow, _ = await state_manager.get_or_create_workflow(
            workflow_name="test",
            blocks=[{"type": "step", "text": "test"}],
        )

        await state_manager.update_workflow_status(
            workflow.workflow_run_id, LoopStatus.FAILED
        )

        loaded = await state_manager.get_workflow(workflow.workflow_run_id)
        assert loaded.status == LoopStatus.FAILED

    async def test_workflow_resumes_from_persisted_state(self, state_manager):
        """Workflow resumes execution from persisted block index."""
        executed = []

        workflow, _ = await state_manager.get_or_create_workflow(
            workflow_name="test",
            blocks=[
                {"type": "done", "text": ""},
                {"type": "pending", "text": ""},
            ],
        )
        workflow.completed_blocks = [0]
        workflow.current_block_index = 1
        workflow.status = LoopStatus.RUNNING
        await state_manager.update_workflow(workflow.workflow_run_id, workflow)

        async def func(ctx, _blocks, block):
            executed.append(block.type)
            ctx.next()

        ctx = MagicMock()
        ctx.next = LoopContext.next.__get__(ctx, LoopContext)

        wm = WorkflowManager(state_manager)
        await wm.start(func, ctx, workflow)
        await asyncio.sleep(0.3)

        assert "done" not in executed
        assert "pending" in executed

    async def test_concurrent_claim_blocked(self, state_manager):
        """Only one worker can hold the claim at a time (mutual exclusion)."""
        workflow, _ = await state_manager.get_or_create_workflow(
            workflow_name="test",
            blocks=[{"type": "step", "text": "test"}],
        )

        holding = [False, False]
        overlap_detected = [False]
        acquired_order = []

        async def worker(idx: int):
            async with state_manager.with_workflow_claim(workflow.workflow_run_id):
                if holding[1 - idx]:
                    overlap_detected[0] = True
                holding[idx] = True
                acquired_order.append(idx)
                await asyncio.sleep(0.5)
                holding[idx] = False

        task1 = asyncio.create_task(worker(0))
        await asyncio.sleep(0.1)
        task2 = asyncio.create_task(worker(1))

        await asyncio.gather(task1, task2, return_exceptions=True)

        assert not overlap_detected[0], "Both workers held the claim simultaneously"
        assert acquired_order == [0, 1], "Workers should acquire in order"


class TestRetryIntegration:
    @pytest.fixture
    def mock_state_with_persistence(self):
        state = AsyncMock()
        workflows = {}

        @asynccontextmanager
        async def mock_claim(_wid):
            yield

        async def get_workflow(wid):
            return workflows.get(wid)

        async def update_workflow(wid, w):
            workflows[wid] = w

        async def update_status(wid, status):
            if wid in workflows:
                workflows[wid].status = status
            return workflows.get(wid)

        state.with_workflow_claim = mock_claim
        state.get_workflow = get_workflow
        state.update_workflow = update_workflow
        state.update_workflow_status = update_status
        state._workflows = workflows
        return state

    async def test_retry_with_backoff(self, mock_state_with_persistence):
        """Retries use exponential backoff."""
        import time

        attempts = []

        async def failing_func(ctx, _blocks, _block):
            attempts.append(time.time())
            if len(attempts) < 3:
                raise ValueError("Transient error")
            ctx.next()

        workflow = WorkflowState(
            workflow_run_id="test",
            blocks=[{"type": "step", "text": ""}],
            status=LoopStatus.RUNNING,
        )
        mock_state_with_persistence._workflows["test"] = workflow

        ctx = MagicMock()
        ctx.next = LoopContext.next.__get__(ctx, LoopContext)

        wm = WorkflowManager(mock_state_with_persistence)
        await wm.start(
            failing_func,
            ctx,
            workflow,
            retry_policy=RetryPolicy(
                max_attempts=5, initial_delay=0.1, backoff_multiplier=2.0
            ),
        )
        await asyncio.sleep(1)

        assert len(attempts) == 3
        delay1 = attempts[1] - attempts[0]
        delay2 = attempts[2] - attempts[1]
        assert delay1 >= 0.09
        assert delay2 >= 0.18

    async def test_workflow_fails_after_max_retries(self, mock_state_with_persistence):
        """Workflow status set to FAILED after max retries."""

        async def always_fails(_ctx, _blocks, _block):
            raise ValueError("Always fails")

        workflow = WorkflowState(
            workflow_run_id="test",
            blocks=[{"type": "step", "text": ""}],
            status=LoopStatus.RUNNING,
        )
        mock_state_with_persistence._workflows["test"] = workflow

        wm = WorkflowManager(mock_state_with_persistence)
        await wm.start(
            always_fails,
            MagicMock(),
            workflow,
            retry_policy=RetryPolicy(max_attempts=2, initial_delay=0.01),
        )
        await asyncio.sleep(0.5)

        assert (
            mock_state_with_persistence._workflows["test"].status == LoopStatus.FAILED
        )
