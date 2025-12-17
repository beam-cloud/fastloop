import asyncio
from collections.abc import Callable
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from functools import partial
from typing import Any

from .types import ExecutorType

_thread_pool: ThreadPoolExecutor | None = None
_process_pool: ProcessPoolExecutor | None = None


def _get_pool(executor_type: ExecutorType) -> ThreadPoolExecutor | ProcessPoolExecutor:
    global _thread_pool, _process_pool

    if executor_type == ExecutorType.THREAD:
        if _thread_pool is None:
            _thread_pool = ThreadPoolExecutor(max_workers=4)
        return _thread_pool

    if _process_pool is None:
        _process_pool = ProcessPoolExecutor(max_workers=4)

    return _process_pool


async def run_in_executor(
    executor_type: ExecutorType,
    func: Callable[..., Any],
    *args: Any,
    **kwargs: Any,
) -> Any:
    if executor_type == ExecutorType.ASYNC:
        if asyncio.iscoroutinefunction(func):
            return await func(*args, **kwargs)
        return func(*args, **kwargs)

    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        _get_pool(executor_type), partial(func, *args, **kwargs)
    )


def shutdown_executors() -> None:
    global _thread_pool, _process_pool
    if _thread_pool:
        _thread_pool.shutdown(wait=False)
        _thread_pool = None
    if _process_pool:
        _process_pool.shutdown(wait=False)
        _process_pool = None
