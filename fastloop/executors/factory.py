from fastloop.executors.executor import (
    AsyncioExecutor,
    Executor,
    ProcessExecutor,
    ThreadExecutor,
)
from fastloop.types import ExecutorType


def get_executor(executor_type: ExecutorType) -> Executor:
    if executor_type == ExecutorType.ASYNCIO:
        return AsyncioExecutor()
    elif executor_type == ExecutorType.THREAD:
        return ThreadExecutor()
    elif executor_type == ExecutorType.PROCESS:
        return ProcessExecutor()
    else:
        raise ValueError(f"Unknown executor type: {executor_type}")
