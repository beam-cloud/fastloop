"""
Unit tests for workflow components.
For integration tests, see test_workflow_integration.py.
"""

import asyncio
import os
import uuid
from contextlib import asynccontextmanager
from queue import Queue
from unittest.mock import AsyncMock, MagicMock

import pytest

from fastloop import FastLoop, LoopContext, LoopEvent, WorkflowBlock
from fastloop.exceptions import WorkflowNextError, WorkflowRepeatError
from fastloop.loop import WorkflowManager, WorkflowState
from fastloop.types import LoopStatus


class StartEvent(LoopEvent):
    type: str = "start"


# --- Fixtures ---


@pytest.fixture
def mock_state():
    """Reusable mock state manager."""
    state = AsyncMock()

    @asynccontextmanager
    async def mock_claim(wid):
        yield

    state.with_workflow_claim = mock_claim
    state.update_workflow_status = AsyncMock()
    state.update_workflow_block_index = AsyncMock()
    return state


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
        state = WorkflowState(workflow_id="test-id")
        assert state.workflow_id == "test-id"
        assert state.status == LoopStatus.PENDING
        assert state.blocks == []
        assert state.current_block_index == 0
        assert state.next_payload is None

    def test_serialization_roundtrip(self):
        state = WorkflowState(
            workflow_id="test-id",
            workflow_name="test",
            blocks=[{"type": "step", "text": "test"}],
            current_block_index=1,
            next_payload={"key": "value"},
        )
        restored = WorkflowState.from_json(state.to_string())
        assert restored.workflow_id == "test-id"
        assert restored.current_block_index == 1
        assert restored.next_payload == {"key": "value"}


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
        assert "/myworkflow/{workflow_id}" in paths
        assert "/myworkflow/{workflow_id}/stop" in paths
        assert "/myworkflow/{workflow_id}/event" in paths

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


# --- WorkflowManager ---


class TestWorkflowManager:
    async def test_normal_return_stops(self, mock_state):
        called = []

        async def func(ctx, blocks, block):
            called.append(block.type)

        workflow = WorkflowState(
            workflow_id="test",
            blocks=[{"type": "step", "text": "t"}],
            status=LoopStatus.RUNNING,
        )
        mock_state.get_workflow = AsyncMock(return_value=workflow)

        wm = WorkflowManager(mock_state)
        await wm.start(func, MagicMock(), workflow)
        await asyncio.sleep(0.1)

        assert called == ["step"]
        mock_state.update_workflow_status.assert_called_with("test", LoopStatus.STOPPED)

    async def test_next_advances(self, mock_state):
        idx = [0]

        async def get_workflow(wid):
            return WorkflowState(
                workflow_id="test",
                blocks=[{"type": "a", "text": ""}, {"type": "b", "text": ""}],
                current_block_index=idx[0],
                status=LoopStatus.RUNNING,
            )

        async def update_idx(wid, new_idx, payload=None):
            idx[0] = new_idx

        mock_state.get_workflow = get_workflow
        mock_state.update_workflow_block_index = update_idx

        executed = []

        async def func(ctx, blocks, block):
            executed.append(block.type)
            ctx.next()

        ctx = MagicMock()
        ctx.next = LoopContext.next.__get__(ctx, LoopContext)

        wm = WorkflowManager(mock_state)
        await wm.start(
            func,
            ctx,
            WorkflowState(workflow_id="test", blocks=[], status=LoopStatus.RUNNING),
        )
        await asyncio.sleep(0.1)

        assert executed == ["a", "b"]

    async def test_repeat_stays(self, mock_state):
        count = [0]

        async def func(ctx, blocks, block):
            count[0] += 1
            if count[0] < 3:
                ctx.repeat()

        workflow = WorkflowState(
            workflow_id="test",
            blocks=[{"type": "step", "text": ""}],
            status=LoopStatus.RUNNING,
        )
        mock_state.get_workflow = AsyncMock(return_value=workflow)

        ctx = MagicMock()
        ctx.repeat = LoopContext.repeat.__get__(ctx, LoopContext)

        wm = WorkflowManager(mock_state)
        await wm.start(func, ctx, workflow)
        await asyncio.sleep(0.1)

        assert count[0] == 3


# --- Redis-dependent tests (skipped unless REDIS_TEST_HOST is set) ---


@pytest.mark.skipif(
    not os.environ.get("REDIS_TEST_HOST"),
    reason="Set REDIS_TEST_HOST to run",
)
class TestWorkflowStatePersistence:
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

    async def test_create_and_get(self, state_manager):
        workflow, created = await state_manager.get_or_create_workflow(
            workflow_name="test",
            blocks=[{"type": "step", "text": "test"}],
        )
        assert created
        assert workflow.workflow_name == "test"

        workflow2, created2 = await state_manager.get_or_create_workflow(
            workflow_id=workflow.workflow_id,
            blocks=[],
        )
        assert not created2
        assert workflow2.workflow_id == workflow.workflow_id

    async def test_update_block_index(self, state_manager):
        workflow, _ = await state_manager.get_or_create_workflow(
            workflow_name="test",
            blocks=[{"type": "a", "text": ""}, {"type": "b", "text": ""}],
        )

        await state_manager.update_workflow_block_index(
            workflow.workflow_id, 1, {"data": "payload"}
        )

        updated = await state_manager.get_workflow(workflow.workflow_id)
        assert updated.current_block_index == 1
        assert updated.next_payload == {"data": "payload"}

    async def test_filter_by_status(self, state_manager):
        w1, _ = await state_manager.get_or_create_workflow(
            workflow_name="running", blocks=[{"type": "s", "text": ""}]
        )
        w2, _ = await state_manager.get_or_create_workflow(
            workflow_name="stopped", blocks=[{"type": "s", "text": ""}]
        )

        await state_manager.update_workflow_status(w1.workflow_id, LoopStatus.RUNNING)
        await state_manager.update_workflow_status(w2.workflow_id, LoopStatus.STOPPED)

        running = await state_manager.get_all_workflows(status=LoopStatus.RUNNING)
        assert len(running) == 1
        assert running[0].workflow_id == w1.workflow_id
