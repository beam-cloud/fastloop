import importlib
import inspect
import os
import sys
from collections.abc import Callable
from typing import Any


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
