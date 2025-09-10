from __future__ import annotations

import importlib
import inspect
import os
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

from pathlib import Path
from typing import TYPE_CHECKING, Any


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


def infer_application_path(self, fallback_var: str = "app") -> str | None:
    """
    Try (1) to locate self.app in its defining module and use 'module:var',
    else (2) derive module from sys.argv[0] and use 'module:fallback_var'.
    """

    # (1) Introspect self.app if present
    app = getattr(self, "app", None)
    if app is not None and getattr(app, "__module__", None):
        mod_name = app.__module__
        try:
            mod = importlib.import_module(mod_name)
            for name, val in vars(mod).items():
                if val is app:
                    return f"{mod_name}:{name}"
            # app object found but variable name not recoverable — use fallback var
            return f"{mod_name}:{fallback_var}"
        except BaseException:
            pass  # fall through to argv-based inference

    # (2) Derive dotted module from the script path in sys.argv[0]
    script = Path(sys.argv[0]).resolve()
    if script.suffix == ".py":
        # Find the first sys.path entry that contains the script
        for base in map(Path, sys.path):
            try:
                rel = script.relative_to(base.resolve())
            except BaseException:
                continue

            # Convert path/to/module.py -> path.to.module
            module = ".".join(rel.with_suffix("").parts)
            if module:
                return f"{module}:{fallback_var}"

    return None
