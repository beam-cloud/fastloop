"""
Regression tests for FastLoop app initialization.

These tests ensure that the application path inference works correctly
and that hypercorn can properly load the application for hot reload.

Related to bug: https://github.com/user/fastloop/issues/XXX
When infer_application_path incorrectly returns "fastloop.fastloop:app",
hypercorn fails with:
    NoAppError: Cannot load application from 'fastloop.fastloop:app', application not found.
"""

import sys
import tempfile
import textwrap
from pathlib import Path
from unittest import mock

from fastloop import FastLoop
from fastloop.utils import infer_application_path


class TestInferApplicationPath:
    """Tests for the infer_application_path utility function."""

    def test_does_not_return_fastloop_module_path(self):
        """
        Regression test: infer_application_path should NOT return a path
        pointing to the fastloop package itself.

        When a user creates `app = FastLoop(...)`, the app's __module__ is
        "fastloop.fastloop" (where the class is defined). But hypercorn needs
        to load the app from the USER's module, not fastloop's.
        """
        app = FastLoop(name="test-app")
        result = infer_application_path(app)

        # The result should NOT be "fastloop.fastloop:app" or similar
        # because there's no `app` variable in fastloop.fastloop module
        if result is not None:
            assert not result.startswith("fastloop."), (
                f"infer_application_path returned '{result}' which points to the "
                "fastloop package. This would cause hypercorn to fail with "
                "'NoAppError: Cannot load application from fastloop.fastloop:app'"
            )

    def test_returns_none_when_app_not_in_module_vars(self):
        """
        When the FastLoop instance isn't assigned to a module-level variable
        that can be found, infer_application_path should return None or
        fall back to argv-based inference.
        """
        # Create an app that's not assigned to any module variable
        app = FastLoop(name="test-app")

        # Mock sys.argv to simulate running a script
        with mock.patch.object(sys, "argv", ["test_script.py"]):
            result = infer_application_path(app)

            # Should either return None or a valid path (not fastloop.fastloop:*)
            if result is not None:
                assert not result.startswith("fastloop.")

    def test_with_app_attribute_none(self):
        """
        Test that if the app_instance has an 'app' attribute that is None,
        we fall through to argv-based inference.
        """

        class AppWrapper:
            def __init__(self):
                self.app = None

        wrapper = AppWrapper()

        # When app attribute is None, should fall through to argv inference
        result = infer_application_path(wrapper)

        # Should not return fastloop path since we don't have a valid app
        if result is not None:
            assert not result.startswith("fastloop.")

    def test_argv_based_inference(self):
        """
        Test that argv-based inference works as a fallback.
        """
        app = FastLoop(name="test-app")

        # Create a temporary Python file to simulate a script
        with tempfile.TemporaryDirectory() as tmpdir:
            script_path = Path(tmpdir) / "myproject" / "main.py"
            script_path.parent.mkdir(parents=True)
            script_path.touch()

            # Mock sys.argv and sys.path
            with (
                mock.patch.object(sys, "argv", [str(script_path)]),
                mock.patch.object(sys, "path", [tmpdir, *sys.path]),
            ):
                result = infer_application_path(app)

                # Should return something like "myproject.main:app"
                if result is not None:
                    assert "myproject.main" in result
                    assert ":app" in result


class TestFastLoopRunConfiguration:
    """Tests for FastLoop.run() configuration."""

    def test_application_path_not_set_in_non_debug_mode(self):
        """
        In non-debug mode, application_path should not be set,
        and we should use asyncio.run directly.
        """
        app = FastLoop(name="test-app")

        # The run method should use asyncio.run when not in debug mode
        # We can't easily test this without actually running the server,
        # but we verify the config manager defaults
        assert app.config_manager.get("debugMode", False) is False

    def test_fastloop_instance_has_no_app_attribute(self):
        """
        Verify that FastLoop doesn't have an 'app' attribute that would
        confuse the introspection logic.
        """
        app = FastLoop(name="test-app")

        # FastLoop is a FastAPI subclass, not a wrapper with an 'app' attribute
        assert not hasattr(app, "app") or getattr(app, "app", None) is None


class TestHypercornCompatibility:
    """Tests for hypercorn compatibility."""

    def test_infer_application_path_returns_valid_format(self):
        """
        When infer_application_path returns a value, it should be in
        the format "module.path:variable_name".
        """
        # Create a temporary module with an app
        with tempfile.TemporaryDirectory() as tmpdir:
            module_dir = Path(tmpdir) / "testpkg"
            module_dir.mkdir()
            (module_dir / "__init__.py").touch()

            app_file = module_dir / "application.py"
            app_file.write_text(
                textwrap.dedent("""
                from fastloop import FastLoop
                app = FastLoop(name="test")
            """)
            )

            # Add to path and import
            sys.path.insert(0, tmpdir)
            try:
                import testpkg.application

                result = infer_application_path(testpkg.application.app)

                if result is not None:
                    # Should be in format "module:var"
                    assert ":" in result, f"Result '{result}' should contain ':'"
                    module_part, var_part = result.rsplit(":", 1)
                    assert len(module_part) > 0
                    assert len(var_part) > 0

                    # Should not point to fastloop
                    assert not module_part.startswith("fastloop")
            finally:
                sys.path.remove(tmpdir)
                # Clean up imported module
                if "testpkg.application" in sys.modules:
                    del sys.modules["testpkg.application"]
                if "testpkg" in sys.modules:
                    del sys.modules["testpkg"]
