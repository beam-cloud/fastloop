from __future__ import annotations

import importlib
import inspect
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable


def get_func_import_path(func: Callable[..., Any]) -> str:
    module = func.__module__
    qualname = func.__qualname__

    # If not __main__, just return the normal path
    if module != "__main__":
        return f"{module}.{qualname}"

    # Try to resolve the file path to a module path
    file = inspect.getsourcefile(func)
    if not file:
        raise ValueError("Cannot determine source file for function in __main__")

    file = os.path.abspath(file)
    for path in sys.path:
        path = os.path.abspath(path)
        if file.startswith(path):
            rel_path = os.path.relpath(file, path)
            mod_path = rel_path.replace(os.sep, ".")
            if mod_path.endswith(".py"):
                mod_path = mod_path[:-3]
            return f"{mod_path}.{qualname}"

    return f"__main__.{qualname}"


def import_func_from_path(path: str) -> Callable[..., Any]:
    module_path, func_name = path.rsplit(".", 1)
    module = importlib.import_module(module_path)
    return getattr(module, func_name)


def infer_application_path(app_instance: Any, fallback_var: str = "app") -> str | None:
    """
    Infer the application path for Hypercorn reload support.

    Searches loaded modules to find where app_instance is stored as a variable,
    then falls back to argv-based inference.

    Args:
        app_instance: The FastLoop/FastAPI application instance
        fallback_var: Variable name to use as fallback (default: "app")

    Returns:
        Application path string like "module.path:app" or None if cannot be determined
    """
    # (1) Search loaded modules for the app_instance
    # Skip fastloop package modules to avoid returning "fastloop.fastloop:app"
    # which doesn't exist (FastLoop is a class, not an instance there)
    for mod_name, mod in list(sys.modules.items()):
        # Skip fastloop package, private modules, and None modules
        if mod is None or mod_name.startswith("fastloop") or mod_name.startswith("_"):
            continue

        try:
            for name, val in vars(mod).items():
                if val is app_instance:
                    return f"{mod_name}:{name}"
        except Exception:
            continue

    # (2) If app_instance has an 'app' attribute, try that (for wrapper patterns)
    app = getattr(app_instance, "app", None)
    if app is not None and getattr(app, "__module__", None):
        mod_name = app.__module__
        # Skip fastloop package
        if not mod_name.startswith("fastloop"):
            try:
                mod = importlib.import_module(mod_name)
                for name, val in vars(mod).items():
                    if val is app:
                        return f"{mod_name}:{name}"
            except Exception:
                pass

    # (3) Derive dotted module from the script path in sys.argv[0]
    script = Path(sys.argv[0]).resolve()
    if script.suffix == ".py":
        # Find the first sys.path entry that contains the script
        for base in map(Path, sys.path):
            try:
                rel = script.relative_to(base.resolve())
            except Exception:
                continue

            # Convert path/to/module.py -> path.to.module
            module = ".".join(rel.with_suffix("").parts)
            if module:
                return f"{module}:{fallback_var}"

    return None
