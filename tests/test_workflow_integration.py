"""
Integration tests for workflows.
Tests HTTP endpoints and full workflow lifecycle.
"""

import asyncio
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from fastloop import FastLoop, LoopContext, LoopEvent
from fastloop.loop import WorkflowManager, WorkflowState
from fastloop.types import LoopStatus

# --- Fixtures ---


@pytest.fixture
def mock_state():
    """Mock state manager with configurable block index tracking."""
    state = AsyncMock()
    idx = [0]

    @asynccontextmanager
    async def mock_claim(wid):
        yield

    state.with_workflow_claim = mock_claim
    state.update_workflow_status = AsyncMock()

    async def update_idx(wid, new_idx, payload=None):
        idx[0] = new_idx

    state.update_workflow_block_index = update_idx
    state._idx = idx  # Expose for tests
    return state


@pytest.fixture
def app():
    """App with workflow for HTTP testing."""
    app = FastLoop(name="test-app")

    @app.event("start")
    class Start(LoopEvent):
        pass

    @app.workflow(name="test", start_event=Start)
    async def test_workflow(ctx, blocks, block):
        ctx.next()

    return app


# --- HTTP Lifecycle ---


class TestHTTPLifecycle:
    def test_routes_registered(self, app):
        paths = [r.path for r in app.routes]
        assert "/test" in paths
        assert "/test/{workflow_id}" in paths
        assert "/test/{workflow_id}/event" in paths
        assert "/test/{workflow_id}/stop" in paths

    def test_start_returns_id(self, app):
        client = TestClient(app)
        resp = client.post(
            "/test", json={"type": "start", "blocks": [{"type": "s", "text": "t"}]}
        )
        assert resp.status_code == 200
        assert "workflow_id" in resp.json()
        assert resp.json()["status"] == "running"

    def test_get_status(self, app):
        client = TestClient(app)
        resp = client.post(
            "/test", json={"type": "start", "blocks": [{"type": "s", "text": "t"}]}
        )
        wid = resp.json()["workflow_id"]

        resp = client.get(f"/test/{wid}")
        assert resp.status_code == 200
        assert resp.json()["workflow_id"] == wid

    def test_stop(self, app):
        client = TestClient(app)
        resp = client.post(
            "/test", json={"type": "start", "blocks": [{"type": "s", "text": "t"}]}
        )
        wid = resp.json()["workflow_id"]

        resp = client.post(f"/test/{wid}/stop")
        assert resp.status_code == 200


# --- Control Flow ---


class TestControlFlow:
    async def test_next_advances_blocks(self, mock_state):
        """ctx.next() advances to next block."""
        executed = []

        async def get_workflow(wid):
            return WorkflowState(
                workflow_id="test",
                blocks=[{"type": "a", "text": ""}, {"type": "b", "text": ""}],
                current_block_index=mock_state._idx[0],
                status=LoopStatus.RUNNING,
            )

        mock_state.get_workflow = get_workflow

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

    async def test_repeat_stays_on_block(self, mock_state):
        """ctx.repeat() re-runs current block."""
        count = [0]

        mock_state.get_workflow = AsyncMock(
            return_value=WorkflowState(
                workflow_id="test",
                blocks=[{"type": "step", "text": ""}],
                status=LoopStatus.RUNNING,
            )
        )

        async def func(ctx, blocks, block):
            count[0] += 1
            if count[0] < 3:
                ctx.repeat()

        ctx = MagicMock()
        ctx.repeat = LoopContext.repeat.__get__(ctx, LoopContext)

        wm = WorkflowManager(mock_state)
        await wm.start(
            func,
            ctx,
            WorkflowState(workflow_id="test", blocks=[], status=LoopStatus.RUNNING),
        )
        await asyncio.sleep(0.1)

        assert count[0] == 3
        assert mock_state._idx[0] == 0  # Never advanced

    async def test_normal_return_stops(self, mock_state):
        """Normal return stops workflow."""
        executed = []

        mock_state.get_workflow = AsyncMock(
            return_value=WorkflowState(
                workflow_id="test",
                blocks=[{"type": "a", "text": ""}, {"type": "b", "text": ""}],
                status=LoopStatus.RUNNING,
            )
        )

        async def func(ctx, blocks, block):
            executed.append(block.type)
            # Normal return = stop

        wm = WorkflowManager(mock_state)
        await wm.start(
            func,
            MagicMock(),
            WorkflowState(workflow_id="test", blocks=[], status=LoopStatus.RUNNING),
        )
        await asyncio.sleep(0.1)

        assert executed == ["a"]  # Only first block
        mock_state.update_workflow_status.assert_called_with("test", LoopStatus.STOPPED)

    async def test_next_passes_payload(self, mock_state):
        """ctx.next(payload) passes data to next block."""
        payloads = []

        async def get_workflow(wid):
            return WorkflowState(
                workflow_id="test",
                blocks=[{"type": "s", "text": ""}],
                current_block_index=mock_state._idx[0],
                status=LoopStatus.RUNNING,
            )

        async def capture_update(wid, new_idx, payload=None):
            payloads.append(payload)
            mock_state._idx[0] = new_idx

        mock_state.get_workflow = get_workflow
        mock_state.update_workflow_block_index = capture_update

        async def func(ctx, blocks, block):
            ctx.next({"key": "value"})

        ctx = MagicMock()
        ctx.next = LoopContext.next.__get__(ctx, LoopContext)

        wm = WorkflowManager(mock_state)
        await wm.start(
            func,
            ctx,
            WorkflowState(workflow_id="test", blocks=[], status=LoopStatus.RUNNING),
        )
        await asyncio.sleep(0.1)

        assert {"key": "value"} in payloads


# --- Callbacks ---


class TestCallbacks:
    async def test_on_block_complete_called(self, mock_state):
        completed = []

        async def get_workflow(wid):
            return WorkflowState(
                workflow_id="test",
                blocks=[{"type": "a", "text": ""}, {"type": "b", "text": ""}],
                current_block_index=mock_state._idx[0],
                status=LoopStatus.RUNNING,
            )

        mock_state.get_workflow = get_workflow

        def on_complete(ctx, block, payload):
            completed.append(block.type)

        async def func(ctx, blocks, block):
            ctx.next()

        ctx = MagicMock()
        ctx.next = LoopContext.next.__get__(ctx, LoopContext)

        wm = WorkflowManager(mock_state)
        await wm.start(
            func,
            ctx,
            WorkflowState(workflow_id="test", blocks=[], status=LoopStatus.RUNNING),
            on_block_complete=on_complete,
        )
        await asyncio.sleep(0.1)

        assert completed == ["a", "b"]

    async def test_on_block_complete_on_normal_return(self, mock_state):
        """on_block_complete fires even when workflow ends via normal return."""
        completed = []

        mock_state.get_workflow = AsyncMock(
            return_value=WorkflowState(
                workflow_id="test",
                blocks=[{"type": "final", "text": ""}],
                status=LoopStatus.RUNNING,
            )
        )

        def on_complete(ctx, block, payload):
            completed.append(block.type)

        async def func(ctx, blocks, block):
            pass  # Normal return

        wm = WorkflowManager(mock_state)
        await wm.start(
            func,
            MagicMock(),
            WorkflowState(workflow_id="test", blocks=[], status=LoopStatus.RUNNING),
            on_block_complete=on_complete,
        )
        await asyncio.sleep(0.1)

        assert completed == ["final"]

    async def test_on_error_called(self, mock_state):
        errors = []

        mock_state.get_workflow = AsyncMock(
            return_value=WorkflowState(
                workflow_id="test",
                blocks=[{"type": "bad", "text": ""}],
                status=LoopStatus.RUNNING,
            )
        )

        def on_error(ctx, block, error):
            errors.append((block.type, str(error)))

        async def func(ctx, blocks, block):
            raise ValueError("boom")

        wm = WorkflowManager(mock_state)
        await wm.start(
            func,
            MagicMock(),
            WorkflowState(workflow_id="test", blocks=[], status=LoopStatus.RUNNING),
            on_error=on_error,
        )
        await asyncio.sleep(0.1)

        assert len(errors) == 1
        assert errors[0][0] == "bad"
        assert "boom" in errors[0][1]


# --- Context Attributes ---


class TestContextAttributes:
    async def test_block_position(self, mock_state):
        """Context has block_index, block_count, previous_payload."""
        captured = []

        async def get_workflow(wid):
            return WorkflowState(
                workflow_id="test",
                blocks=[{"type": "a", "text": ""}, {"type": "b", "text": ""}],
                current_block_index=mock_state._idx[0],
                next_payload={"prev": True} if mock_state._idx[0] > 0 else None,
                status=LoopStatus.RUNNING,
            )

        mock_state.get_workflow = get_workflow

        async def func(ctx, blocks, block):
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
        await wm.start(
            func,
            ctx,
            WorkflowState(workflow_id="test", blocks=[], status=LoopStatus.RUNNING),
        )
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

        # Simulate restart at block 1
        mock_state.get_workflow = AsyncMock(
            return_value=WorkflowState(
                workflow_id="test",
                blocks=[{"type": "skip", "text": ""}, {"type": "run", "text": ""}],
                current_block_index=1,  # Start at second block
                status=LoopStatus.RUNNING,
            )
        )

        async def func(ctx, blocks, block):
            executed.append(block.type)

        wm = WorkflowManager(mock_state)
        await wm.start(
            func,
            MagicMock(),
            WorkflowState(workflow_id="test", blocks=[], status=LoopStatus.RUNNING),
        )
        await asyncio.sleep(0.1)

        assert executed == ["run"]  # Skipped first block

    async def test_ctx_state_persists(self, mock_state):
        """ctx.set/get persists state."""
        stored = {}

        async def set_val(wid, key, val):
            stored[key] = val

        async def get_val(wid, key):
            return stored.get(key)

        mock_state.set_context_value = set_val
        mock_state.get_context_value = get_val

        ctx = LoopContext(loop_id="test", state_manager=mock_state)

        await ctx.set("mykey", {"nested": "value"})
        result = await ctx.get("mykey")

        assert result == {"nested": "value"}
