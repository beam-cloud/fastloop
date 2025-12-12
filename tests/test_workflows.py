"""
Unit tests for workflow components.
For integration tests, see test_workflow_integration.py.
"""

import asyncio
from unittest.mock import MagicMock

import pytest

from fastloop import (
    FastLoop,
    LoopContext,
    LoopEvent,
    RetryPolicy,
    Workflow,
    WorkflowBlock,
)
from fastloop.exceptions import (
    WorkflowMaxRetriesError,
    WorkflowNextError,
    WorkflowRepeatError,
)
from fastloop.models import WorkflowState
from fastloop.types import LoopStatus
from fastloop.workflow import WorkflowManager


class StartEvent(LoopEvent):
    type: str = "start"


# --- Data Models ---


class TestWorkflowBlock:
    def test_fields(self):
        block = WorkflowBlock(type="step", text="Do something")
        assert block.type == "step"
        assert block.text == "Do something"

    def test_serialization(self):
        block = WorkflowBlock(type="step", text="Do something")
        assert block.model_dump() == {"type": "step", "text": "Do something"}


class TestWorkflowState:
    def test_defaults(self):
        state = WorkflowState(workflow_run_id="test-id")
        assert state.workflow_run_id == "test-id"
        assert state.status == LoopStatus.PENDING
        assert state.blocks == []
        assert state.current_block_index == 0
        assert state.next_payload is None
        assert state.completed_blocks == []
        assert state.block_attempts == {}
        assert state.last_error is None

    def test_serialization_roundtrip(self):
        state = WorkflowState(
            workflow_run_id="test-id",
            workflow_name="test",
            blocks=[{"type": "step", "text": "test"}],
            current_block_index=1,
            next_payload={"key": "value"},
        )
        restored = WorkflowState.from_json(state.to_string())
        assert restored.workflow_run_id == "test-id"
        assert restored.current_block_index == 1
        assert restored.next_payload == {"key": "value"}

    def test_durability_fields_serialization(self):
        state = WorkflowState(
            workflow_run_id="test-id",
            completed_blocks=[0, 1],
            block_attempts={2: 3},
            last_error="test error",
        )
        restored = WorkflowState.from_json(state.to_string())
        assert restored.completed_blocks == [0, 1]
        assert restored.block_attempts == {2: 3}
        assert restored.last_error == "test error"


# --- Control Flow Exceptions ---


class TestControlFlowExceptions:
    def test_next_raises(self):
        ctx = MagicMock(spec=LoopContext)
        ctx.next = LoopContext.next.__get__(ctx, LoopContext)

        with pytest.raises(WorkflowNextError) as exc:
            ctx.next()
        assert exc.value.payload is None

    def test_next_with_payload(self):
        ctx = MagicMock(spec=LoopContext)
        ctx.next = LoopContext.next.__get__(ctx, LoopContext)

        with pytest.raises(WorkflowNextError) as exc:
            ctx.next({"key": "value"})
        assert exc.value.payload == {"key": "value"}

    def test_repeat_raises(self):
        ctx = MagicMock(spec=LoopContext)
        ctx.repeat = LoopContext.repeat.__get__(ctx, LoopContext)

        with pytest.raises(WorkflowRepeatError):
            ctx.repeat()

    def test_abort_raises_stopped(self):
        from fastloop.exceptions import LoopStoppedError

        ctx = MagicMock(spec=LoopContext)
        ctx.abort = LoopContext.abort.__get__(ctx, LoopContext)

        with pytest.raises(LoopStoppedError):
            ctx.abort()


# --- Workflow Registration ---


class TestWorkflowRegistration:
    def test_registers_metadata(self):
        app = FastLoop(name="test")
        app.register_event(StartEvent)

        @app.workflow("myworkflow", start_event=StartEvent)
        async def my_workflow(ctx, blocks, block):
            pass

        assert "myworkflow" in app._workflow_metadata
        assert app._workflow_metadata["myworkflow"]["func"] is my_workflow

    def test_registers_routes(self):
        app = FastLoop(name="test")
        app.register_event(StartEvent)

        @app.workflow("myworkflow", start_event=StartEvent)
        async def my_workflow(ctx, blocks, block):
            pass

        paths = [r.path for r in app.routes]
        assert "/myworkflow" in paths
        assert "/myworkflow/{workflow_run_id}" in paths
        assert "/myworkflow/{workflow_run_id}/cancel" in paths
        assert "/myworkflow/{workflow_run_id}/event" in paths

    def test_stores_callbacks(self):
        app = FastLoop(name="test")
        app.register_event(StartEvent)

        def on_start(ctx):
            pass

        def on_stop(ctx):
            pass

        def on_block(ctx, block, payload):
            pass

        def on_err(ctx, block, error):
            pass

        @app.workflow(
            "w",
            start_event=StartEvent,
            on_start=on_start,
            on_stop=on_stop,
            on_block_complete=on_block,
            on_error=on_err,
        )
        async def w(ctx, blocks, block):
            pass

        meta = app._workflow_metadata["w"]
        assert meta["on_start"] is on_start
        assert meta["on_stop"] is on_stop
        assert meta["on_block_complete"] is on_block
        assert meta["on_error"] is on_err

    def test_duplicate_raises(self):
        app = FastLoop(name="test")
        app.register_event(StartEvent)

        @app.workflow("w", start_event=StartEvent)
        async def w1(ctx, blocks, block):
            pass

        with pytest.raises(Exception, match="already registered"):

            @app.workflow("w", start_event=StartEvent)
            async def w2(ctx, blocks, block):
                pass

    def test_class_based_workflow(self):
        app = FastLoop(name="test")
        app.register_event(StartEvent)

        @app.workflow("myworkflow", start_event=StartEvent)
        class MyWorkflow(Workflow):
            async def execute(self, ctx, _blocks, _block):
                ctx.next()

        assert "myworkflow" in app._workflow_metadata
        assert app._workflow_metadata["myworkflow"]["workflow_instance"] is not None

    def test_class_based_workflow_callbacks(self):
        app = FastLoop(name="test")
        app.register_event(StartEvent)

        @app.workflow("myworkflow", start_event=StartEvent)
        class MyWorkflow(Workflow):
            async def on_start(self, ctx):
                pass

            async def on_block_complete(self, ctx, block, payload):
                pass

            async def execute(self, ctx, blocks, block):
                pass

        meta = app._workflow_metadata["myworkflow"]
        assert meta["on_start"] is not None
        assert meta["on_block_complete"] is not None


# --- WorkflowManager ---


class TestWorkflowManager:
    async def test_normal_return_stops(self, mock_state):
        called = []

        async def func(_ctx, _blocks, block):
            called.append(block.type)

        workflow = WorkflowState(
            workflow_run_id="test",
            blocks=[{"type": "step", "text": "t"}],
            status=LoopStatus.RUNNING,
        )
        mock_state._workflows["test"] = workflow

        wm = WorkflowManager(mock_state)
        await wm.start(func, MagicMock(), workflow)
        await asyncio.sleep(0.1)

        assert called == ["step"]
        mock_state.update_workflow_status.assert_called_with("test", LoopStatus.STOPPED)

    async def test_next_advances(self, mock_state):
        workflow = WorkflowState(
            workflow_run_id="test",
            blocks=[{"type": "a", "text": ""}, {"type": "b", "text": ""}],
            current_block_index=0,
            status=LoopStatus.RUNNING,
        )
        mock_state._workflows["test"] = workflow

        executed = []

        async def func(ctx, _blocks, block):
            executed.append(block.type)
            ctx.next()

        ctx = MagicMock()
        ctx.next = LoopContext.next.__get__(ctx, LoopContext)

        wm = WorkflowManager(mock_state)
        await wm.start(func, ctx, workflow)
        await asyncio.sleep(0.1)

        assert executed == ["a", "b"]

    async def test_repeat_stays(self, mock_state):
        count = [0]

        async def func(ctx, _blocks, _block):
            count[0] += 1
            if count[0] < 3:
                ctx.repeat()

        workflow = WorkflowState(
            workflow_run_id="test",
            blocks=[{"type": "step", "text": ""}],
            status=LoopStatus.RUNNING,
        )
        mock_state._workflows["test"] = workflow

        ctx = MagicMock()
        ctx.repeat = LoopContext.repeat.__get__(ctx, LoopContext)

        wm = WorkflowManager(mock_state)
        await wm.start(func, ctx, workflow)
        await asyncio.sleep(0.1)

        assert count[0] == 3


class TestRetryPolicy:
    def test_default_values(self):
        policy = RetryPolicy()
        assert policy.max_attempts == 3
        assert policy.initial_delay == 1.0
        assert policy.max_delay == 60.0
        assert policy.backoff_multiplier == 2.0

    def test_compute_delay(self):
        policy = RetryPolicy(initial_delay=1.0, backoff_multiplier=2.0, max_delay=10.0)
        assert policy.compute_delay(1) == 1.0
        assert policy.compute_delay(2) == 2.0
        assert policy.compute_delay(3) == 4.0
        assert policy.compute_delay(4) == 8.0
        assert policy.compute_delay(5) == 10.0

    def test_max_delay_cap(self):
        policy = RetryPolicy(
            initial_delay=10.0, backoff_multiplier=10.0, max_delay=50.0
        )
        assert policy.compute_delay(3) == 50.0


class TestWorkflowManagerRetries:
    async def test_retry_on_error(self, mock_state):
        attempts = [0]

        async def failing_func(ctx, _blocks, _block):
            attempts[0] += 1
            if attempts[0] < 3:
                raise ValueError("Test error")
            ctx.next()

        workflow = WorkflowState(
            workflow_run_id="test",
            blocks=[{"type": "step", "text": ""}],
            status=LoopStatus.RUNNING,
        )
        mock_state._workflows["test"] = workflow

        ctx = MagicMock()
        ctx.next = LoopContext.next.__get__(ctx, LoopContext)

        wm = WorkflowManager(mock_state)
        await wm.start(
            failing_func,
            ctx,
            workflow,
            retry_policy=RetryPolicy(max_attempts=5, initial_delay=0.01),
        )
        await asyncio.sleep(0.3)

        assert attempts[0] == 3

    async def test_max_retries_exhausted(self, mock_state):
        attempts = [0]
        error_callback_called = [False]
        max_retries_error_received = [None]

        async def always_failing(_ctx, _blocks, _block):
            attempts[0] += 1
            raise ValueError("Always fails")

        async def on_error(_ctx, _block, error):
            if isinstance(error, WorkflowMaxRetriesError):
                error_callback_called[0] = True
                max_retries_error_received[0] = error

        workflow = WorkflowState(
            workflow_run_id="test",
            blocks=[{"type": "step", "text": ""}],
            status=LoopStatus.RUNNING,
        )
        mock_state._workflows["test"] = workflow

        wm = WorkflowManager(mock_state)
        await wm.start(
            always_failing,
            MagicMock(),
            workflow,
            on_error=on_error,
            retry_policy=RetryPolicy(max_attempts=2, initial_delay=0.01),
        )
        await asyncio.sleep(0.3)

        assert attempts[0] == 2
        assert error_callback_called[0]
        assert max_retries_error_received[0].attempts == 2
        mock_state.update_workflow_status.assert_called_with("test", LoopStatus.FAILED)

    async def test_skips_completed_blocks(self, mock_state):
        executed_blocks = []

        async def track_blocks(ctx, _blocks, block):
            executed_blocks.append(block.type)
            ctx.next()

        workflow = WorkflowState(
            workflow_run_id="test",
            blocks=[
                {"type": "a", "text": ""},
                {"type": "b", "text": ""},
                {"type": "c", "text": ""},
            ],
            current_block_index=0,
            completed_blocks=[0],
            status=LoopStatus.RUNNING,
        )
        mock_state._workflows["test"] = workflow

        ctx = MagicMock()
        ctx.next = LoopContext.next.__get__(ctx, LoopContext)

        wm = WorkflowManager(mock_state)
        await wm.start(track_blocks, ctx, workflow)
        await asyncio.sleep(0.2)

        assert "a" not in executed_blocks
        assert "b" in executed_blocks
        assert "c" in executed_blocks


class TestWorkflowRegistrationRetry:
    def test_stores_retry_policy(self):
        app = FastLoop(name="test")
        app.register_event(StartEvent)
        policy = RetryPolicy(max_attempts=5, initial_delay=2.0)

        @app.workflow("w", start_event=StartEvent, retry=policy)
        async def w(ctx, blocks, block):
            pass

        assert app._workflow_metadata["w"]["retry_policy"] is policy

    def test_default_retry_policy_is_none(self):
        app = FastLoop(name="test")
        app.register_event(StartEvent)

        @app.workflow("w", start_event=StartEvent)
        async def w(ctx, blocks, block):
            pass

        assert app._workflow_metadata["w"]["retry_policy"] is None


# --- Redis-dependent tests (skipped unless REDIS_TEST_HOST is set) ---


class TestWorkflowStatePersistence:
    async def test_create_and_get(self, redis_state_manager):
        workflow, created = await redis_state_manager.get_or_create_workflow(
            workflow_name="test",
            blocks=[{"type": "step", "text": "test"}],
        )
        assert created
        assert workflow.workflow_name == "test"

        workflow2, created2 = await redis_state_manager.get_or_create_workflow(
            workflow_run_id=workflow.workflow_run_id,
            blocks=[],
        )
        assert not created2
        assert workflow2.workflow_run_id == workflow.workflow_run_id

    async def test_update_block_index(self, redis_state_manager):
        workflow, _ = await redis_state_manager.get_or_create_workflow(
            workflow_name="test",
            blocks=[{"type": "a", "text": ""}, {"type": "b", "text": ""}],
        )

        await redis_state_manager.update_workflow_block_index(
            workflow.workflow_run_id, 1, {"data": "payload"}
        )

        updated = await redis_state_manager.get_workflow(workflow.workflow_run_id)
        assert updated.current_block_index == 1
        assert updated.next_payload == {"data": "payload"}

    async def test_filter_by_status(self, redis_state_manager):
        w1, _ = await redis_state_manager.get_or_create_workflow(
            workflow_name="running", blocks=[{"type": "s", "text": ""}]
        )
        w2, _ = await redis_state_manager.get_or_create_workflow(
            workflow_name="stopped", blocks=[{"type": "s", "text": ""}]
        )

        await redis_state_manager.update_workflow_status(
            w1.workflow_run_id, LoopStatus.RUNNING
        )
        await redis_state_manager.update_workflow_status(
            w2.workflow_run_id, LoopStatus.STOPPED
        )

        running = await redis_state_manager.get_all_workflows(status=LoopStatus.RUNNING)
        assert len(running) == 1
        assert running[0].workflow_run_id == w1.workflow_run_id
