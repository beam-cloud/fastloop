"""
Pytest configuration and shared fixtures for fastloop tests.
"""

import os
import uuid
from contextlib import asynccontextmanager
from queue import Queue
from unittest.mock import AsyncMock

import pytest

from fastloop.models import WorkflowState
from fastloop.types import LoopStatus


def pytest_configure(config):
    """Configure pytest with custom markers."""
    config.addinivalue_line(
        "markers", "slow: marks tests as slow (deselect with '-m \"not slow\"')"
    )
    config.addinivalue_line("markers", "integration: marks tests as integration tests")


@pytest.fixture
def mock_state():
    """Reusable mock state manager with proper state tracking."""
    state = AsyncMock()
    workflows = {}

    @asynccontextmanager
    async def mock_claim(_wid):
        yield

    async def get_workflow(wid):
        if wid in workflows:
            return workflows[wid]
        return WorkflowState(workflow_run_id=wid, status=LoopStatus.RUNNING)

    async def update_workflow(wid, w):
        workflows[wid] = w

    state.with_workflow_claim = mock_claim
    state.get_workflow = get_workflow
    state.update_workflow = update_workflow
    state.update_workflow_status = AsyncMock()
    state.update_workflow_block_index = AsyncMock()
    state._workflows = workflows
    return state


@pytest.fixture
def mock_state_with_persistence():
    """Mock state manager with persistence simulation."""
    state = AsyncMock()
    workflows = {}

    @asynccontextmanager
    async def mock_claim(_wid):
        yield

    async def get_workflow(wid):
        return workflows.get(wid)

    async def update_workflow(wid, w):
        workflows[wid] = w

    async def update_status(wid, status):
        if wid in workflows:
            workflows[wid].status = status
        return workflows.get(wid)

    state.with_workflow_claim = mock_claim
    state.get_workflow = get_workflow
    state.update_workflow = update_workflow
    state.update_workflow_status = update_status
    state._workflows = workflows
    return state


@pytest.fixture
async def redis_state_manager():
    """Redis state manager for integration tests.

    Requires REDIS_TEST_HOST environment variable to be set.
    """
    if not os.environ.get("REDIS_TEST_HOST"):
        pytest.skip("Set REDIS_TEST_HOST to run Redis tests")

    from fastloop.state.state_redis import RedisStateManager
    from fastloop.types import RedisConfig

    config = RedisConfig(
        host=os.environ.get("REDIS_TEST_HOST", "localhost"),
        port=int(os.environ.get("REDIS_TEST_PORT", "6379")),
        database=int(os.environ.get("REDIS_TEST_DB", "15")),
        password=os.environ.get("REDIS_TEST_PASSWORD", ""),
        ssl=os.environ.get("REDIS_TEST_SSL", "").lower() == "true",
    )
    manager = RedisStateManager(
        app_name=f"test-{uuid.uuid4().hex[:8]}",
        config=config,
        wake_queue=Queue(),
    )
    yield manager
    manager.stop()
    await manager.rdb.flushdb()
