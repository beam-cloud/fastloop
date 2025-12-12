"""
Regression tests for FastLoop app initialization.

These tests ensure that:
1. The application path inference works correctly for hypercorn hot reload
2. FastLoop apps with registered loops initialize properly

Regression for: When infer_application_path incorrectly returns "fastloop.fastloop:app",
hypercorn fails with:
    NoAppError: Cannot load application from 'fastloop.fastloop:app', application not found.
"""

import sys
import tempfile
from pathlib import Path
from unittest import mock

from fastloop import FastLoop
from fastloop.context import LoopContext
from fastloop.models import LoopEvent
from fastloop.utils import infer_application_path

# --- Event Types for Testing ---


class QueryEvent(LoopEvent):
    type: str = "query"
    message: str


class ResponseEvent(LoopEvent):
    type: str = "response"
    reply: str


# --- Test Classes ---


class TestInferApplicationPath:
    """
    Regression tests for infer_application_path.

    The bug: When a FastLoop instance has no 'app' attribute, the function
    was falling back to app_instance itself, returning "fastloop.fastloop:app"
    which doesn't exist.
    """

    def test_does_not_return_fastloop_module_path(self):
        """FastLoop instances should NOT resolve to the fastloop package."""
        app = FastLoop(name="test-app")
        result = infer_application_path(app)

        if result is not None:
            assert not result.startswith("fastloop."), (
                f"infer_application_path returned '{result}' pointing to fastloop package. "
                "This causes: NoAppError: Cannot load application from 'fastloop.fastloop:app'"
            )

    def test_falls_back_to_argv_inference(self):
        """When no module var found, should use argv-based inference."""
        app = FastLoop(name="test-app")

        with tempfile.TemporaryDirectory() as tmpdir:
            script_path = Path(tmpdir) / "myapp" / "main.py"
            script_path.parent.mkdir(parents=True)
            script_path.touch()

            with (
                mock.patch.object(sys, "argv", [str(script_path)]),
                mock.patch.object(sys, "path", [tmpdir, *sys.path]),
            ):
                result = infer_application_path(app)

                if result is not None:
                    assert "myapp.main" in result
                    assert ":app" in result

    def test_finds_app_in_external_module(self):
        """
        Regression test for app defined in separate module (like internal_agents.app).

        When a FastLoop instance is created in a user module and imported into main,
        infer_application_path should find it in that module.

        This is the pattern:
            # mypackage/app.py
            app = FastLoop(name="my-app")

            # mypackage/main.py
            from mypackage.app import app
            app.run()
        """
        import types

        # Create a mock module simulating 'mypackage.app'
        mock_module = types.ModuleType("mypackage.app")
        app = FastLoop(name="external-module-app")
        mock_module.app = app

        # Register the mock module
        sys.modules["mypackage.app"] = mock_module

        try:
            result = infer_application_path(app)

            # Should find the app in our mock module
            assert result is not None, (
                "infer_application_path should find app in external module"
            )
            assert result == "mypackage.app:app", (
                f"Expected 'mypackage.app:app', got '{result}'"
            )
            assert not result.startswith("fastloop."), (
                "Should not resolve to fastloop package"
            )
        finally:
            # Clean up
            del sys.modules["mypackage.app"]

    def test_finds_app_with_different_variable_name(self):
        """Test that app is found even if stored with a different variable name."""
        import types

        mock_module = types.ModuleType("mypackage.server")
        app = FastLoop(name="custom-var-app")
        mock_module.my_server = app  # Different variable name

        sys.modules["mypackage.server"] = mock_module

        try:
            result = infer_application_path(app)

            assert result is not None
            assert result == "mypackage.server:my_server"
        finally:
            del sys.modules["mypackage.server"]


class TestFastLoopWithLoops:
    """Tests for FastLoop app with registered loops."""

    def test_app_with_loop_decorator(self):
        """
        Test that a FastLoop app with a @loop decorator initializes correctly
        and doesn't cause import issues.
        """
        app = FastLoop(name="test-chat-app")
        app.register_events([QueryEvent, ResponseEvent])

        async def on_start(context: LoopContext):
            await context.set("initialized", True)

        @app.loop("chat", start_event=QueryEvent, on_start=on_start)
        async def chat_loop(context: LoopContext):
            msg = await context.wait_for(QueryEvent, raise_on_timeout=False, timeout=1)
            if msg is None:
                return
            await context.emit(ResponseEvent(reply=f"Echo: {msg.message}"))

        # Verify loop was registered
        assert "chat" in app._loop_metadata
        assert app._loop_metadata["chat"]["func"] == chat_loop
        assert app._loop_metadata["chat"]["start_event"] == "query"

        # Verify events were registered
        assert "query" in app._event_types
        assert "response" in app._event_types

        # Verify infer_application_path doesn't break
        result = infer_application_path(app)
        if result is not None:
            assert not result.startswith("fastloop.")

    def test_app_with_multiple_loops(self):
        """Test app with multiple loop definitions."""
        app = FastLoop(name="multi-loop-app")
        app.register_events([QueryEvent, ResponseEvent])

        @app.loop("loop-a", start_event=QueryEvent)
        async def loop_a(context: LoopContext):
            pass

        @app.loop("loop-b", start_event=ResponseEvent)
        async def loop_b(context: LoopContext):
            pass

        assert "loop-a" in app._loop_metadata
        assert "loop-b" in app._loop_metadata
        assert len(app._loop_metadata) == 2

    def test_app_routes_registered(self):
        """Verify that loop decorator registers the expected API routes."""
        app = FastLoop(name="route-test-app")
        app.register_events([QueryEvent])

        @app.loop("myloop", start_event=QueryEvent)
        async def my_loop(context: LoopContext):
            pass

        # Check routes were added
        route_paths = [route.path for route in app.routes]
        assert "/myloop" in route_paths
        assert "/myloop/{loop_id}" in route_paths
        assert "/myloop/{loop_id}/stop" in route_paths
        assert "/myloop/{loop_id}/pause" in route_paths


class TestAppConfiguration:
    """Tests for FastLoop configuration."""

    def test_debug_mode_defaults_to_false(self):
        """Debug mode should be off by default."""
        app = FastLoop(name="test-app")
        assert app.config_manager.get("debugMode", False) is False

    def test_custom_config_applied(self):
        """Custom config should be merged with defaults."""
        app = FastLoop(
            name="test-app",
            config={"port": 9000, "debugMode": True},
        )
        assert app.config_manager.get("port") == 9000
        assert app.config_manager.get("debugMode") is True

    def test_fastloop_has_no_app_attribute(self):
        """FastLoop should not have an 'app' attribute (it IS the app)."""
        app = FastLoop(name="test-app")
        # FastLoop is a FastAPI subclass, not a wrapper
        assert getattr(app, "app", None) is None
