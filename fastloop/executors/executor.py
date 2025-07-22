import abc
import asyncio
from collections.abc import Callable
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from typing import Any


class Executor(abc.ABC):
    @abc.abstractmethod
    async def run(self, func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """Execute the given function with provided arguments."""
        raise NotImplementedError


class AsyncioExecutor(Executor):
    async def run(self, func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        if asyncio.iscoroutinefunction(func):
            return await func(*args, **kwargs)
        else:
            return func(*args, **kwargs)


class ThreadExecutor(Executor):
    def __init__(self):
        self._executor = ThreadPoolExecutor(max_workers=1)

    async def run(self, func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        if asyncio.iscoroutinefunction(func):
            # Async functions still need to run in the main loop to access state manager
            return await func(*args, **kwargs)
        else:
            # Sync functions can run in threads
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(self._executor, func, *args, **kwargs)

    def shutdown(self, wait: bool = True):
        self._executor.shutdown(wait)


class ProcessExecutor(Executor):
    def __init__(self):
        self._executor = ProcessPoolExecutor()

    async def run(self, func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        if asyncio.iscoroutinefunction(func):
            raise ValueError("ProcessExecutor cannot run async functions")

        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, func, *args, **kwargs)

    def shutdown(self, wait: bool = True):
        self._executor.shutdown(wait)
