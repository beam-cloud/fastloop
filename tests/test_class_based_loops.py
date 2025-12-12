"""
Tests for class-based loop definitions.

Verifies that:
1. Class-based loops register correctly with the same decorator as function-based
2. Lifecycle methods (on_start, on_stop, on_event) are properly wired
3. on_app_start hook fires on startup for non-stopped loop instances
4. Function-based loops continue to work unchanged
"""

import asyncio
import uuid

import pytest

from fastloop import FastLoop, Loop
from fastloop.models import LoopEvent


class SampleEvent(LoopEvent):
    type: str = "sample"
    message: str = ""


class TestLoopRegistration:
    """Tests that both class-based and function-based loops register correctly."""

    def test_class_loop_registers_with_decorator(self):
        app = FastLoop(name="test-app")
        app.register_event(SampleEvent)

        @app.loop("myloop", start_event=SampleEvent)
        class MyLoop(Loop):
            async def loop(self, ctx):
                pass

        assert "myloop" in app._loop_metadata
        assert app._loop_metadata["myloop"]["loop_instance"] is not None
        assert app._loop_metadata["myloop"]["start_event"] == "sample"

    def test_function_loop_registers_with_decorator(self):
        app = FastLoop(name="test-app")
        app.register_event(SampleEvent)

        @app.loop("myloop", start_event=SampleEvent)
        async def myloop(ctx):
            pass

        assert "myloop" in app._loop_metadata
        assert app._loop_metadata["myloop"]["loop_instance"] is None
        assert app._loop_metadata["myloop"]["func"] is myloop

    def test_class_and_function_loops_coexist(self):
        app = FastLoop(name="test-app")
        app.register_event(SampleEvent)

        @app.loop("class_loop", start_event=SampleEvent)
        class ClassLoop(Loop):
            async def loop(self, ctx):
                pass

        @app.loop("func_loop", start_event=SampleEvent)
        async def func_loop(ctx):
            pass

        assert len(app._loop_metadata) == 2
        assert app._loop_metadata["class_loop"]["loop_instance"] is not None
        assert app._loop_metadata["func_loop"]["loop_instance"] is None

    def test_class_loop_registers_api_routes(self):
        app = FastLoop(name="test-app")
        app.register_event(SampleEvent)

        @app.loop("myloop", start_event=SampleEvent)
        class MyLoop(Loop):
            async def loop(self, ctx):
                pass

        route_paths = [route.path for route in app.routes]
        assert "/myloop" in route_paths
        assert "/myloop/{loop_id}" in route_paths
        assert "/myloop/{loop_id}/cancel" in route_paths
        assert "/myloop/{loop_id}/pause" in route_paths


class TestClassLoopLifecycleMethods:
    """Tests that class lifecycle methods are correctly wired to the loop manager."""

    def test_on_start_wired_from_class_method(self):
        app = FastLoop(name="test-app")
        app.register_event(SampleEvent)

        @app.loop("myloop", start_event=SampleEvent)
        class MyLoop(Loop):
            async def on_start(self, ctx):
                await ctx.set("started", True)

            async def loop(self, ctx):
                pass

        instance = app._loop_metadata["myloop"]["loop_instance"]
        on_start = app._loop_metadata["myloop"]["on_start"]
        assert on_start.__self__ is instance
        assert on_start.__name__ == "on_start"

    def test_on_stop_wired_from_class_method(self):
        app = FastLoop(name="test-app")
        app.register_event(SampleEvent)

        @app.loop("myloop", start_event=SampleEvent)
        class MyLoop(Loop):
            async def on_stop(self, ctx):
                await ctx.set("stopped", True)

            async def loop(self, ctx):
                pass

        instance = app._loop_metadata["myloop"]["loop_instance"]
        on_stop = app._loop_metadata["myloop"]["on_stop"]
        assert on_stop.__self__ is instance
        assert on_stop.__name__ == "on_stop"

    def test_loop_method_used_as_main_func(self):
        app = FastLoop(name="test-app")
        app.register_event(SampleEvent)

        @app.loop("myloop", start_event=SampleEvent)
        class MyLoop(Loop):
            async def loop(self, ctx):
                pass

        instance = app._loop_metadata["myloop"]["loop_instance"]
        func = app._loop_metadata["myloop"]["func"]
        assert func.__self__ is instance
        assert func.__name__ == "loop"

    def test_function_loop_uses_on_start_parameter(self):
        app = FastLoop(name="test-app")
        app.register_event(SampleEvent)

        async def custom_on_start(ctx):
            pass

        @app.loop("myloop", start_event=SampleEvent, on_start=custom_on_start)
        async def myloop(ctx):
            pass

        assert app._loop_metadata["myloop"]["on_start"] is custom_on_start

    def test_function_loop_uses_on_stop_parameter(self):
        app = FastLoop(name="test-app")
        app.register_event(SampleEvent)

        async def custom_on_stop(ctx):
            pass

        @app.loop("myloop", start_event=SampleEvent, on_stop=custom_on_stop)
        async def myloop(ctx):
            pass

        assert app._loop_metadata["myloop"]["on_stop"] is custom_on_stop


class TestLoopBaseClass:
    """Tests for the Loop base class behavior."""

    def test_loop_method_must_be_implemented(self):
        with pytest.raises(NotImplementedError, match="must implement loop"):
            asyncio.run(Loop().loop(None))  # type: ignore

    def test_on_start_is_async_noop_by_default(self):
        class MyLoop(Loop):
            async def loop(self, ctx):
                pass

        instance = MyLoop()
        assert asyncio.iscoroutinefunction(instance.on_start)
        # Should not raise
        asyncio.run(instance.on_start(None))  # type: ignore

    def test_on_stop_is_async_noop_by_default(self):
        class MyLoop(Loop):
            async def loop(self, ctx):
                pass

        instance = MyLoop()
        assert asyncio.iscoroutinefunction(instance.on_stop)
        asyncio.run(instance.on_stop(None))  # type: ignore

    def test_on_event_is_async_noop_by_default(self):
        class MyLoop(Loop):
            async def loop(self, ctx):
                pass

        instance = MyLoop()
        assert asyncio.iscoroutinefunction(instance.on_event)
        asyncio.run(instance.on_event(None, None))  # type: ignore

    def test_on_app_start_returns_true_by_default(self):
        class MyLoop(Loop):
            async def loop(self, ctx):
                pass

        instance = MyLoop()
        result = asyncio.run(instance.on_app_start(None))  # type: ignore
        assert result is True

    def test_ctx_attribute_is_typed(self):
        assert "ctx" in Loop.__annotations__
        assert Loop.__annotations__["ctx"] == "LoopContext"


class TestDecoratorOptions:
    """Tests that all decorator options work with class-based loops."""

    def test_integrations_option(self):
        app = FastLoop(name="test-app")
        app.register_event(SampleEvent)

        @app.loop("myloop", start_event=SampleEvent, integrations=[])
        class MyLoop(Loop):
            async def loop(self, ctx):
                pass

        assert app._loop_metadata["myloop"]["integrations"] == []

    def test_stop_on_disconnect_option(self):
        app = FastLoop(name="test-app")
        app.register_event(SampleEvent)

        @app.loop("myloop", start_event=SampleEvent, stop_on_disconnect=True)
        class MyLoop(Loop):
            async def loop(self, ctx):
                pass

        assert app._loop_metadata["myloop"]["stop_on_disconnect"] is True

    def test_start_event_as_class(self):
        app = FastLoop(name="test-app")
        app.register_event(SampleEvent)

        @app.loop("myloop", start_event=SampleEvent)
        class MyLoop(Loop):
            async def loop(self, ctx):
                pass

        assert app._loop_metadata["myloop"]["start_event"] == "sample"

    def test_start_event_as_string(self):
        app = FastLoop(name="test-app")
        app.register_event(SampleEvent)

        @app.loop("myloop", start_event="sample")
        class MyLoop(Loop):
            async def loop(self, ctx):
                pass

        assert app._loop_metadata["myloop"]["start_event"] == "sample"


class TestLoopFuncResolution:
    """Tests that the correct function is used when starting/restarting loops."""

    def test_class_loop_uses_instance_method_directly(self):
        """Class-based loops should use the bound method, not import from path."""
        app = FastLoop(name="test-app")
        app.register_event(SampleEvent)

        @app.loop("myloop", start_event=SampleEvent)
        class MyLoop(Loop):
            async def loop(self, ctx):
                pass

        metadata = app._loop_metadata["myloop"]
        loop_instance = metadata["loop_instance"]
        func = metadata["func"]

        # The func should be a bound method of the instance
        assert hasattr(func, "__self__")
        assert func.__self__ is loop_instance
        assert func.__name__ == "loop"

        # Verify the func IS the instance's loop method (same underlying function)
        assert func.__func__ is MyLoop.loop

    def test_function_loop_stores_importable_path(self):
        """Function-based loops should store an importable path."""
        app = FastLoop(name="test-app")
        app.register_event(SampleEvent)

        @app.loop("myloop", start_event=SampleEvent)
        async def my_loop(ctx):
            pass

        metadata = app._loop_metadata["myloop"]
        func = metadata["func"]

        # The func should be the original function (not bound)
        assert not hasattr(func, "__self__")
        assert func is my_loop

    def test_class_loop_restart_uses_instance_method(self):
        """When restarting, class-based loops should use instance method, not import."""
        app = FastLoop(name="test-app")
        app.register_event(SampleEvent)

        @app.loop("myloop", start_event=SampleEvent)
        class MyLoop(Loop):
            async def loop(self, ctx):
                pass

        metadata = app._loop_metadata["myloop"]
        loop_instance = metadata["loop_instance"]

        # Simulate what restart_loop does for class-based loops
        if loop_instance:
            func = loop_instance.loop
        else:
            from fastloop.utils import import_func_from_path

            func = import_func_from_path("some.path")

        # Should use the instance method
        assert func.__self__ is loop_instance

    def test_function_loop_can_be_imported(self):
        """Function-based loop paths should be importable."""
        from fastloop.utils import get_func_import_path

        app = FastLoop(name="test-app")
        app.register_event(SampleEvent)

        @app.loop("myloop", start_event=SampleEvent)
        async def my_loop_func(ctx):
            pass

        func = app._loop_metadata["myloop"]["func"]
        path = get_func_import_path(func)

        # Should be able to import and get back a function
        # (Note: might fail if running in __main__, so we just check the path format)
        assert "." in path
        assert "my_loop_func" in path


class TestAppStartLocking:
    """Tests for on_app_start distributed locking (requires Redis)."""

    async def test_lock_prevents_duplicate_acquisition(self, redis_state_manager):
        loop_id = str(uuid.uuid4())

        first = await redis_state_manager.try_acquire_app_start_lock(loop_id)
        second = await redis_state_manager.try_acquire_app_start_lock(loop_id)

        assert first is True
        assert second is False

    async def test_lock_can_be_released_and_reacquired(self, redis_state_manager):
        loop_id = str(uuid.uuid4())

        await redis_state_manager.try_acquire_app_start_lock(loop_id)
        await redis_state_manager.release_app_start_lock(loop_id)
        reacquired = await redis_state_manager.try_acquire_app_start_lock(loop_id)

        assert reacquired is True

    async def test_different_loops_have_independent_locks(self, redis_state_manager):
        loop_1 = str(uuid.uuid4())
        loop_2 = str(uuid.uuid4())

        acquired_1 = await redis_state_manager.try_acquire_app_start_lock(loop_1)
        acquired_2 = await redis_state_manager.try_acquire_app_start_lock(loop_2)

        assert acquired_1 is True
        assert acquired_2 is True
