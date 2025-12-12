"""
Tests for loop idle detection.

Verifies that:
1. New decorator parameters work correctly
2. Mutual exclusivity is enforced
3. Activity tracking works as expected
4. Loops pause/stop based on idle time
"""

import asyncio
import contextlib
from unittest.mock import AsyncMock, MagicMock

import pytest

from fastloop import FastLoop, Loop
from fastloop.constants import MEANINGFUL_WORK_THRESHOLD_S
from fastloop.context import LoopContext
from fastloop.models import LoopEvent


class SampleIdleEvent(LoopEvent):
    type: str = "test"
    message: str = ""


class TestDecoratorParameters:
    """Tests for the new idle-related decorator parameters."""

    def test_stop_after_idle_seconds_stored_in_metadata(self):
        app = FastLoop(name="test-app")
        app.register_event(SampleIdleEvent)

        @app.loop("myloop", start_event=SampleIdleEvent, stop_after_idle_seconds=60.0)
        async def myloop(ctx):
            pass

        assert app._loop_metadata["myloop"]["stop_after_idle_seconds"] == 60.0
        assert app._loop_metadata["myloop"]["pause_after_idle_seconds"] is None

    def test_pause_after_idle_seconds_stored_in_metadata(self):
        app = FastLoop(name="test-app")
        app.register_event(SampleIdleEvent)

        @app.loop("myloop", start_event=SampleIdleEvent, pause_after_idle_seconds=30.0)
        async def myloop(ctx):
            pass

        assert app._loop_metadata["myloop"]["pause_after_idle_seconds"] == 30.0
        assert app._loop_metadata["myloop"]["stop_after_idle_seconds"] is None

    def test_both_idle_params_raises_error(self):
        app = FastLoop(name="test-app")
        app.register_event(SampleIdleEvent)

        with pytest.raises(ValueError, match="Cannot set both"):

            @app.loop(
                "myloop",
                start_event=SampleIdleEvent,
                stop_after_idle_seconds=60.0,
                pause_after_idle_seconds=30.0,
            )
            async def myloop(ctx):
                pass

    def test_neither_idle_param_is_valid(self):
        app = FastLoop(name="test-app")
        app.register_event(SampleIdleEvent)

        @app.loop("myloop", start_event=SampleIdleEvent)
        async def myloop(ctx):
            pass

        assert app._loop_metadata["myloop"]["stop_after_idle_seconds"] is None
        assert app._loop_metadata["myloop"]["pause_after_idle_seconds"] is None

    def test_class_loop_with_idle_params(self):
        app = FastLoop(name="test-app")
        app.register_event(SampleIdleEvent)

        @app.loop("myloop", start_event=SampleIdleEvent, stop_after_idle_seconds=120.0)
        class MyLoop(Loop):
            async def loop(self, ctx):
                pass

        assert app._loop_metadata["myloop"]["stop_after_idle_seconds"] == 120.0


class TestContextCycleTracking:
    """Tests for the context cycle tracking methods."""

    def test_reset_cycle_tracking_clears_flags(self):
        mock_state = MagicMock()
        ctx = LoopContext(loop_id="test", state_manager=mock_state)

        ctx.event_this_cycle = True
        ctx._wait_time_this_cycle = 5.0

        ctx._reset_cycle_tracking()

        assert ctx.event_this_cycle is False
        assert ctx._wait_time_this_cycle == 0.0

    def test_wait_time_initialized_to_zero(self):
        mock_state = MagicMock()
        ctx = LoopContext(loop_id="test", state_manager=mock_state)

        assert ctx._wait_time_this_cycle == 0.0


class TestActivityDetection:
    """Tests for activity detection logic."""

    def test_meaningful_work_threshold_constant_exists(self):
        assert MEANINGFUL_WORK_THRESHOLD_S == 0.01

    def test_emit_marks_cycle_as_active(self):
        mock_state = AsyncMock()
        mock_state.get_next_nonce = AsyncMock(return_value=1)
        mock_state.push_event = AsyncMock()

        ctx = LoopContext(loop_id="test", state_manager=mock_state)
        ctx.event_this_cycle = False

        asyncio.run(ctx.emit(SampleIdleEvent(message="hello")))

        assert ctx.event_this_cycle is True


class TestIdleDetectionBehavior:
    """Integration tests for idle detection behavior in loop runner."""

    @pytest.mark.asyncio
    async def test_loop_without_idle_params_runs_forever(self):
        from fastloop.loop import LoopManager
        from fastloop.models import LoopState
        from fastloop.types import BaseConfig

        mock_state = AsyncMock()
        mock_state.with_claim = MagicMock()
        mock_state.update_loop_status = AsyncMock()
        mock_state.get_loop = AsyncMock()
        mock_state.update_loop = AsyncMock()

        class MockClaim:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

        mock_state.with_claim.return_value = MockClaim()

        config = BaseConfig()
        manager = LoopManager(config, mock_state)

        iterations = 0
        max_iterations = 5

        async def test_loop(ctx):
            nonlocal iterations
            iterations += 1
            if iterations >= max_iterations:
                ctx.stop()

        mock_ctx = MagicMock()
        mock_ctx.should_stop = False
        mock_ctx.should_pause = False
        mock_ctx.event_this_cycle = False
        mock_ctx._wait_time_this_cycle = 0.0
        mock_ctx._reset_cycle_tracking = MagicMock()

        def check_stop():
            return iterations >= max_iterations

        type(mock_ctx).should_stop = property(lambda _: check_stop())

        loop_state = LoopState(loop_id="test-loop")

        await manager.start(
            func=test_loop,
            loop_start_func=None,
            loop_stop_func=None,
            context=mock_ctx,
            loop=loop_state,
            loop_delay=0.01,
        )

        await asyncio.sleep(0.2)
        await manager.stop("test-loop")

        assert iterations >= max_iterations

    @pytest.mark.asyncio
    async def test_activity_detection_work_time(self):
        from contextlib import asynccontextmanager

        from fastloop.loop import LoopManager
        from fastloop.models import LoopState
        from fastloop.types import BaseConfig

        mock_state = AsyncMock()
        mock_state.update_loop_status = AsyncMock()

        @asynccontextmanager
        async def mock_claim(_):
            yield

        mock_state.with_claim = mock_claim

        config = BaseConfig()
        manager = LoopManager(config, mock_state)

        async def idle_loop(ctx):
            pass

        mock_ctx = MagicMock()
        mock_ctx.should_stop = False
        mock_ctx.should_pause = False
        mock_ctx.event_this_cycle = False
        mock_ctx._wait_time_this_cycle = 0.0
        mock_ctx._reset_cycle_tracking = MagicMock()

        loop_state = LoopState(loop_id="test-loop")

        await manager.start(
            func=idle_loop,
            loop_start_func=None,
            loop_stop_func=None,
            context=mock_ctx,
            loop=loop_state,
            loop_delay=0.01,
            pause_after_idle_seconds=0.1,
        )

        await asyncio.sleep(0.3)

        mock_state.update_loop_status.assert_called()
        await manager.stop("test-loop")


class TestWaitTimeTracking:
    """Tests that wait_for properly tracks waiting time."""

    @pytest.mark.asyncio
    async def test_wait_for_accumulates_wait_time(self):
        mock_state = AsyncMock()
        mock_pubsub = AsyncMock()
        mock_state.subscribe_to_events = AsyncMock(return_value=mock_pubsub)
        mock_state.pop_event = AsyncMock(return_value=None)
        mock_state.wait_for_event_notification = AsyncMock(return_value=False)

        ctx = LoopContext(loop_id="test", state_manager=mock_state)

        start_wait_time = ctx._wait_time_this_cycle

        with contextlib.suppress(Exception):
            await ctx.wait_for(SampleIdleEvent, timeout=0.1, raise_on_timeout=False)

        assert ctx._wait_time_this_cycle > start_wait_time
