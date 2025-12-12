"""
Tests for the task system.
"""

import asyncio
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest

from fastloop import ExecutorType, FastLoop, RetryPolicy, TaskStatus
from fastloop.exceptions import LoopAlreadyDefinedError
from fastloop.executor import run_in_executor, shutdown_executors
from fastloop.models import TaskState
from fastloop.scheduler import Schedule, validate_cron
from fastloop.task import TaskManager, TaskResult


def _process_test_func(x: int) -> int:
    """Module-level function for process executor test (must be picklable)."""
    return sum(i for i in range(x))


class TestExecutor:
    """Tests for the executor dispatch function."""

    @pytest.fixture(autouse=True)
    def cleanup(self):
        yield
        shutdown_executors()

    async def test_async_executor_with_coroutine(self):
        async def async_func(x: int) -> int:
            return x * 2

        result = await run_in_executor(ExecutorType.ASYNC, async_func, 5)
        assert result == 10

    async def test_async_executor_with_sync_func(self):
        def sync_func(x: int) -> int:
            return x * 2

        result = await run_in_executor(ExecutorType.ASYNC, sync_func, 5)
        assert result == 10

    async def test_thread_executor(self):
        def blocking_func(x: int) -> int:
            return x * x

        result = await run_in_executor(ExecutorType.THREAD, blocking_func, 4)
        assert result == 16

    async def test_process_executor(self):
        result = await run_in_executor(ExecutorType.PROCESS, _process_test_func, 100)
        assert result == 4950

    async def test_executor_with_kwargs(self):
        def func_with_kwargs(a: int, b: int = 10) -> int:
            return a + b

        result = await run_in_executor(ExecutorType.ASYNC, func_with_kwargs, 5, b=20)
        assert result == 25


class TestTaskState:
    """Tests for TaskState model."""

    def test_defaults(self):
        task = TaskState(task_id="t1", task_name="test")
        assert task.status == TaskStatus.PENDING
        assert task.args == {}
        assert task.result is None
        assert task.error is None
        assert task.attempts == 0

    def test_serialization_roundtrip(self):
        task = TaskState(
            task_id="t1",
            task_name="test",
            status=TaskStatus.SUCCESS,
            args={"x": 1, "y": 2},
            result={"sum": 3},
            attempts=1,
        )
        serialized = task.to_string()
        restored = TaskState.from_json(serialized)

        assert restored.task_id == task.task_id
        assert restored.task_name == task.task_name
        assert restored.status == task.status
        assert restored.args == task.args
        assert restored.result == task.result
        assert restored.attempts == task.attempts


class TestTaskManager:
    """Tests for TaskManager."""

    @pytest.fixture
    def mock_task_state(self):
        state = AsyncMock()
        tasks = {}

        @asynccontextmanager
        async def mock_claim(_tid):
            yield

        async def create_task(t):
            tasks[t.task_id] = t
            return t

        async def get_task(tid):
            return tasks[tid]

        async def update_task(t):
            tasks[t.task_id] = t

        async def update_status(tid, status):
            tasks[tid].status = status
            return tasks[tid]

        async def set_result(tid, result):
            tasks[tid].result = result

        async def get_result(tid):
            return tasks[tid].result

        state.with_task_claim = mock_claim
        state.create_task = create_task
        state.get_task = get_task
        state.update_task = update_task
        state.update_task_status = update_status
        state.set_task_result = set_result
        state.get_task_result = get_result
        state._tasks = tasks
        return state

    async def test_submit_creates_task(self, mock_task_state):
        manager = TaskManager(mock_task_state)

        async def my_task(x: int) -> int:
            return x

        handle = await manager.submit(
            func=my_task,
            args={"x": 5},
            task_name="my_task",
        )

        assert isinstance(handle, TaskResult)
        assert handle.task_id in mock_task_state._tasks
        task = mock_task_state._tasks[handle.task_id]
        assert task.task_name == "my_task"
        assert task.args == {"x": 5}

    async def test_task_runs_to_completion(self, mock_task_state):
        manager = TaskManager(mock_task_state)

        async def add(a: int, b: int) -> int:
            return a + b

        handle = await manager.submit(
            func=add,
            args={"a": 2, "b": 3},
            task_name="add",
        )

        result = await handle.result(timeout=5.0)
        assert result == 5

        task = mock_task_state._tasks[handle.task_id]
        assert task.status == TaskStatus.SUCCESS

    async def test_task_retries_on_failure(self, mock_task_state):
        manager = TaskManager(mock_task_state)
        call_count = 0

        async def flaky() -> str:
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise Exception("fail")
            return "ok"

        handle = await manager.submit(
            func=flaky,
            args={},
            task_name="flaky",
            retry_policy=RetryPolicy(max_attempts=5, initial_delay=0.01),
        )

        result = await handle.result(timeout=5.0)
        assert result == "ok"
        assert call_count == 3

    async def test_task_fails_after_max_retries(self, mock_task_state):
        manager = TaskManager(mock_task_state)

        async def always_fail() -> str:
            raise Exception("always fails")

        handle = await manager.submit(
            func=always_fail,
            args={},
            task_name="always_fail",
            retry_policy=RetryPolicy(max_attempts=2, initial_delay=0.01),
        )

        task = await handle.wait(timeout=5.0)
        assert task.status == TaskStatus.FAILED
        assert "always fails" in task.error

    async def test_task_status(self, mock_task_state):
        manager = TaskManager(mock_task_state)

        async def slow_task() -> str:
            await asyncio.sleep(0.1)
            return "done"

        handle = await manager.submit(
            func=slow_task,
            args={},
            task_name="slow",
        )

        status = await handle.status()
        assert status in (TaskStatus.PENDING, TaskStatus.RUNNING)

        await handle.wait(timeout=5.0)
        status = await handle.status()
        assert status == TaskStatus.SUCCESS


class TestTaskDecorator:
    """Tests for @app.task() decorator."""

    def test_registers_metadata(self):
        app = FastLoop(name="test")

        @app.task(name="my_task")
        async def my_task(x: int) -> int:
            return x

        assert "my_task" in app._task_metadata
        assert app._task_metadata["my_task"]["func"] == my_task

    def test_registers_with_retry_policy(self):
        app = FastLoop(name="test")
        policy = RetryPolicy(max_attempts=5)

        @app.task(name="retry_task", retry=policy)
        async def retry_task() -> str:
            return "ok"

        assert app._task_metadata["retry_task"]["retry"] == policy

    def test_registers_with_executor_type(self):
        app = FastLoop(name="test")

        @app.task(name="cpu_task", executor=ExecutorType.PROCESS)
        def cpu_task(n: int) -> int:
            return n * n

        assert app._task_metadata["cpu_task"]["executor"] == ExecutorType.PROCESS

    def test_registers_http_routes(self):
        app = FastLoop(name="test")

        @app.task(name="my_task")
        async def my_task() -> str:
            return "ok"

        routes = [r.path for r in app.routes]
        assert "/my_task" in routes
        assert "/my_task/{task_id}" in routes

    def test_duplicate_task_raises(self):
        app = FastLoop(name="test")

        @app.task(name="dup")
        async def task1() -> str:
            return "1"

        with pytest.raises(LoopAlreadyDefinedError):

            @app.task(name="dup")
            async def task2() -> str:
                return "2"


class TestInvokeMethod:
    """Tests for app.invoke()."""

    @pytest.fixture
    def app_with_task(self):
        app = FastLoop(name="test")

        @app.task(name="add")
        async def add(a: int, b: int) -> int:
            return a + b

        return app

    async def test_invoke_returns_task_id(self, app_with_task):
        task_id = await app_with_task.invoke("add", a=1, b=2)
        assert isinstance(task_id, str)
        assert len(task_id) > 0

    async def test_invoke_unknown_task_raises(self, app_with_task):
        with pytest.raises(ValueError, match="Unknown task"):
            await app_with_task.invoke("nonexistent", x=1)


class TestTaskHTTPEndpoints:
    """Tests for task HTTP endpoints using test client."""

    @pytest.fixture
    def app(self):
        app = FastLoop(name="test")

        @app.task(name="echo")
        async def echo(msg: str) -> dict:
            return {"echo": msg}

        return app

    def test_post_endpoint_exists(self, app):
        from fastapi.testclient import TestClient

        client = TestClient(app)
        response = client.post("/echo", json={"msg": "hello"})
        assert response.status_code == 202
        data = response.json()
        assert "task_id" in data
        assert data["status"] == "pending"

    def test_get_endpoint_not_found(self, app):
        from fastapi.testclient import TestClient

        client = TestClient(app)
        response = client.get("/echo/nonexistent-id")
        assert response.status_code == 404


class TestSchedule:
    """Tests for Schedule model."""

    def test_defaults(self):
        schedule = Schedule(task_name="test", cron="* * * * *")
        assert schedule.enabled is True
        assert schedule.args == {}
        assert schedule.next_run is None

    def test_compute_next_run_cron(self):
        from datetime import datetime

        now = datetime.now()
        schedule = Schedule(task_name="test", cron="* * * * *")
        next_run = schedule.compute_next_run(now.timestamp())
        next_dt = datetime.fromtimestamp(next_run)
        assert next_dt.minute == (now.minute + 1) % 60 or (
            next_dt.minute == 0 and now.minute == 59
        )

    def test_compute_next_run_interval(self):
        import time

        schedule = Schedule(task_name="test", interval_seconds=60)
        base = time.time()
        next_run = schedule.compute_next_run(base)
        assert next_run == base + 60

    def test_serialization_roundtrip(self):
        schedule = Schedule(
            task_name="test",
            cron="*/5 * * * *",
            args={"x": 1},
            enabled=True,
        )
        data = schedule.to_dict()
        restored = Schedule.from_dict(data)

        assert restored.task_name == schedule.task_name
        assert restored.cron == schedule.cron
        assert restored.args == schedule.args
        assert restored.enabled == schedule.enabled


class TestValidateCron:
    """Tests for cron validation."""

    def test_valid_expressions(self):
        assert validate_cron("* * * * *") is True
        assert validate_cron("*/5 * * * *") is True
        assert validate_cron("0 * * * *") is True
        assert validate_cron("0 0 * * *") is True
        assert validate_cron("0 0 1 * *") is True
        assert validate_cron("0 0 * * 0") is True

    def test_invalid_expressions(self):
        assert validate_cron("invalid") is False
        assert validate_cron("") is False
        assert validate_cron("* * *") is False


class TestScheduleDecorator:
    """Tests for @app.schedule() decorator."""

    def test_requires_cron_or_interval(self):
        app = FastLoop(name="test")

        with pytest.raises(ValueError, match="Must specify either cron or interval"):

            @app.schedule(name="bad")
            async def bad_task():
                pass

    def test_validates_cron(self):
        app = FastLoop(name="test")

        with pytest.raises(ValueError, match="Invalid cron expression"):

            @app.schedule(name="bad", cron="invalid")
            async def bad_task():
                pass

    def test_registers_task(self):
        app = FastLoop(name="test")

        @app.schedule(name="periodic", cron="* * * * *")
        async def periodic():
            return "ok"

        assert "periodic" in app._task_metadata

    def test_registers_with_interval(self):
        app = FastLoop(name="test")

        @app.schedule(name="heartbeat", interval=60)
        async def heartbeat():
            return "ok"

        assert "heartbeat" in app._task_metadata


class TestScheduleTaskMethod:
    """Tests for app.schedule_task() method."""

    @pytest.fixture
    def app_with_task(self):
        app = FastLoop(name="test")

        @app.task(name="my_task")
        async def my_task(x: int = 0) -> int:
            return x

        return app

    async def test_schedule_task_requires_cron_or_interval(self, app_with_task):
        with pytest.raises(ValueError, match="Must specify either cron or interval"):
            await app_with_task.schedule_task("my_task")

    async def test_schedule_task_validates_cron(self, app_with_task):
        with pytest.raises(ValueError, match="Invalid cron expression"):
            await app_with_task.schedule_task("my_task", cron="invalid")

    async def test_schedule_task_unknown_task_raises(self, app_with_task):
        with pytest.raises(ValueError, match="Unknown task"):
            await app_with_task.schedule_task("nonexistent", cron="* * * * *")
