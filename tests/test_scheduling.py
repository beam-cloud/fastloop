"""
End-to-end tests for the scheduling (sleep_for/sleep_until) functionality.

These tests use a real Redis server to test the full wake monitoring flow,
including keyspace notifications.

Requires: REDIS_TEST_HOST environment variable to be set.
In CI, this is provided by the Redis service container.
Locally, run: docker run -d -p 6379:6379 redis:7
"""

import asyncio
import os
import time
from queue import Queue

import pytest

from fastloop.context import LoopContext
from fastloop.exceptions import LoopPausedError
from fastloop.loop import LoopEvent
from fastloop.state.state_redis import RedisKeys, RedisStateManager
from fastloop.types import LoopEventSender, LoopStatus, RedisConfig


# Skip all tests if Redis is not available
pytestmark = pytest.mark.skipif(
    not os.environ.get("REDIS_TEST_HOST"),
    reason="Set REDIS_TEST_HOST to run scheduling tests (e.g., REDIS_TEST_HOST=localhost)"
)


@pytest.fixture
def redis_config():
    """Get Redis config from environment."""
    return RedisConfig(
        host=os.environ.get("REDIS_TEST_HOST", "localhost"),
        port=int(os.environ.get("REDIS_TEST_PORT", "6379")),
        database=int(os.environ.get("REDIS_TEST_DB", "15")),  # Use DB 15 for tests
        password=os.environ.get("REDIS_TEST_PASSWORD", ""),
        ssl=os.environ.get("REDIS_TEST_SSL", "").lower() == "true",
    )


@pytest.fixture
def wake_queue():
    """Create a queue for wake events."""
    return Queue()


@pytest.fixture
async def state_manager(redis_config, wake_queue):
    """Create a Redis state manager connected to real Redis."""
    manager = RedisStateManager(
        app_name="test-app",
        config=redis_config,
        wake_queue=wake_queue,
    )
    
    # Wait a moment for the wake monitoring thread to start and configure notifications
    await asyncio.sleep(0.1)
    
    yield manager
    
    # Cleanup
    await manager.rdb.flushdb()


@pytest.fixture
async def loop_state(state_manager):
    """Create a loop in the state manager."""
    loop, _ = await state_manager.get_or_create_loop(
        loop_name="test-loop",
        current_function_path="test.module.func",
    )
    return loop


@pytest.fixture
async def loop_context(state_manager, loop_state):
    """Create a loop context for testing."""
    context = LoopContext(
        loop_id=loop_state.loop_id,
        initial_event=None,
        state_manager=state_manager,
    )
    return context


class TestSetWakeTime:
    """Tests for the set_wake_time functionality."""
    
    async def test_set_wake_time_creates_key_with_ttl(self, state_manager, loop_state):
        """Test that set_wake_time creates a Redis key with the correct TTL."""
        wake_timestamp = time.time() + 5.0
        await state_manager.set_wake_time(loop_state.loop_id, wake_timestamp)
        
        wake_key = RedisKeys.LOOP_WAKE_KEY.format(
            app_name="test-app",
            loop_id=loop_state.loop_id,
        )
        value = await state_manager.rdb.get(wake_key)
        assert value == b"wake"
        
        # Check the TTL is approximately correct (within 1 second tolerance)
        ttl_ms = await state_manager.rdb.pttl(wake_key)
        assert 4000 <= ttl_ms <= 5100
    
    async def test_set_wake_time_adds_to_index(self, state_manager, loop_state):
        """Test that set_wake_time adds the key to the wake index."""
        wake_timestamp = time.time() + 10.0
        await state_manager.set_wake_time(loop_state.loop_id, wake_timestamp)
        
        wake_index = RedisKeys.LOOP_WAKE_INDEX.format(app_name="test-app")
        wake_key = RedisKeys.LOOP_WAKE_KEY.format(
            app_name="test-app",
            loop_id=loop_state.loop_id,
        )
        
        members = await state_manager.rdb.smembers(wake_index)
        assert wake_key.encode() in members
    
    async def test_set_wake_time_with_subsecond_precision(self, state_manager, loop_state):
        """Test that sub-second durations work correctly."""
        wake_timestamp = time.time() + 0.5
        await state_manager.set_wake_time(loop_state.loop_id, wake_timestamp)
        
        wake_key = RedisKeys.LOOP_WAKE_KEY.format(
            app_name="test-app",
            loop_id=loop_state.loop_id,
        )
        
        ttl_ms = await state_manager.rdb.pttl(wake_key)
        assert 400 <= ttl_ms <= 600
    
    async def test_set_wake_time_past_timestamp_raises(self, state_manager, loop_state):
        """Test that setting a wake time in the past raises an error."""
        past_timestamp = time.time() - 10.0
        with pytest.raises(ValueError, match="Timestamp is in the past"):
            await state_manager.set_wake_time(loop_state.loop_id, past_timestamp)


class TestWakeMonitoring:
    """
    Tests for the wake monitoring thread that listens for Redis key expirations.
    
    These tests verify the full flow:
    1. Set a wake time (creates a Redis key with TTL)
    2. Key expires
    3. Redis fires keyspace notification
    4. Wake monitoring thread receives notification
    5. Loop ID is added to wake queue
    """
    
    async def test_wake_monitoring_detects_expiration(self, state_manager, wake_queue, loop_state):
        """Test that the wake monitoring thread detects key expiration."""
        # Set a short wake time
        await state_manager.set_wake_time(loop_state.loop_id, time.time() + 0.3)
        
        # Wait for expiration + processing time
        await asyncio.sleep(1.0)
        
        # The loop ID should be in the wake queue
        assert not wake_queue.empty(), "Wake queue should have the loop_id after expiration"
        woken_loop_id = wake_queue.get_nowait()
        assert woken_loop_id == loop_state.loop_id
    
    async def test_wake_monitoring_removes_from_index(self, state_manager, wake_queue, loop_state):
        """Test that expired keys are removed from the wake index."""
        await state_manager.set_wake_time(loop_state.loop_id, time.time() + 0.3)
        
        wake_index = RedisKeys.LOOP_WAKE_INDEX.format(app_name="test-app")
        wake_key = RedisKeys.LOOP_WAKE_KEY.format(
            app_name="test-app",
            loop_id=loop_state.loop_id,
        )
        
        # Key should be in index initially
        members = await state_manager.rdb.smembers(wake_index)
        assert wake_key.encode() in members
        
        # Wait for expiration
        await asyncio.sleep(1.0)
        
        # Key should be removed from index
        members = await state_manager.rdb.smembers(wake_index)
        assert wake_key.encode() not in members
    
    async def test_multiple_loops_wake_independently(self, state_manager, wake_queue):
        """Test that multiple loops can wake at different times."""
        loops = []
        for i in range(3):
            loop, _ = await state_manager.get_or_create_loop(
                loop_name=f"test-loop-{i}",
                current_function_path="test.func",
            )
            loops.append(loop)
            # Stagger wake times
            await state_manager.set_wake_time(loop.loop_id, time.time() + 0.2 * (i + 1))
        
        # Wait for all to expire
        await asyncio.sleep(1.5)
        
        # All three should be in the queue
        woken_ids = set()
        while not wake_queue.empty():
            woken_ids.add(wake_queue.get_nowait())
        
        expected_ids = {loop.loop_id for loop in loops}
        assert woken_ids == expected_ids
    
    async def test_overwriting_wake_time_works(self, state_manager, wake_queue, loop_state):
        """Test that overwriting a wake time cancels the old one."""
        # Set initial wake time far in the future
        await state_manager.set_wake_time(loop_state.loop_id, time.time() + 60)
        
        # Overwrite with a short wake time
        await state_manager.set_wake_time(loop_state.loop_id, time.time() + 0.3)
        
        # Wait for the short one to expire
        await asyncio.sleep(1.0)
        
        # Should wake up with the short time
        assert not wake_queue.empty()
        assert wake_queue.get_nowait() == loop_state.loop_id


class TestContextSleepFor:
    """Tests for the context.sleep_for() method."""
    
    async def test_sleep_for_triggers_wake(self, loop_context, state_manager, wake_queue):
        """Test that sleep_for sets wake time and the loop wakes up."""
        with pytest.raises(LoopPausedError):
            await loop_context.sleep_for(0.3)
        
        # Wait for wake
        await asyncio.sleep(1.0)
        
        assert not wake_queue.empty()
        assert wake_queue.get_nowait() == loop_context.loop_id
    
    async def test_sleep_for_string_duration(self, loop_context, state_manager):
        """Test sleep_for with string durations."""
        with pytest.raises(LoopPausedError):
            await loop_context.sleep_for("5 seconds")
        
        wake_key = RedisKeys.LOOP_WAKE_KEY.format(
            app_name="test-app",
            loop_id=loop_context.loop_id,
        )
        ttl_ms = await state_manager.rdb.pttl(wake_key)
        assert 4000 <= ttl_ms <= 5100
    
    async def test_sleep_for_negative_raises(self, loop_context):
        """Test that negative durations raise an error."""
        with pytest.raises(ValueError, match="must be positive"):
            await loop_context.sleep_for(-5.0)
    
    async def test_sleep_for_zero_raises(self, loop_context):
        """Test that zero duration raises an error."""
        with pytest.raises(ValueError, match="must be positive"):
            await loop_context.sleep_for(0)
    
    async def test_sleep_for_invalid_string_raises(self, loop_context):
        """Test that invalid duration strings raise an error."""
        with pytest.raises(ValueError, match="Invalid duration format"):
            await loop_context.sleep_for("five seconds")


class TestContextSleepUntil:
    """Tests for the context.sleep_until() method."""
    
    async def test_sleep_until_triggers_wake(self, loop_context, state_manager, wake_queue):
        """Test that sleep_until sets wake time and the loop wakes up."""
        future_time = time.time() + 0.3
        
        with pytest.raises(LoopPausedError):
            await loop_context.sleep_until(future_time)
        
        # Wait for wake
        await asyncio.sleep(1.0)
        
        assert not wake_queue.empty()
        assert wake_queue.get_nowait() == loop_context.loop_id
    
    async def test_sleep_until_past_raises(self, loop_context):
        """Test that sleeping until a past timestamp raises an error."""
        past_time = time.time() - 10.0
        with pytest.raises(ValueError, match="Cannot sleep until a time in the past"):
            await loop_context.sleep_until(past_time)


class TestDurationParsing:
    """Tests for the duration string parsing."""
    
    async def test_parse_seconds_variations(self, loop_context):
        """Test various ways to specify seconds."""
        assert loop_context._parse_duration("5 seconds") == 5.0
        assert loop_context._parse_duration("5 second") == 5.0
        assert loop_context._parse_duration("5 secs") == 5.0
        assert loop_context._parse_duration("5 sec") == 5.0
        assert loop_context._parse_duration("1.5 seconds") == 1.5
    
    async def test_parse_minutes_variations(self, loop_context):
        """Test various ways to specify minutes."""
        assert loop_context._parse_duration("5 minutes") == 300.0
        assert loop_context._parse_duration("5 minute") == 300.0
        assert loop_context._parse_duration("5 mins") == 300.0
        assert loop_context._parse_duration("5 min") == 300.0
    
    async def test_parse_hours_variations(self, loop_context):
        """Test various ways to specify hours."""
        assert loop_context._parse_duration("2 hours") == 7200.0
        assert loop_context._parse_duration("2 hour") == 7200.0
        assert loop_context._parse_duration("2 hrs") == 7200.0
        assert loop_context._parse_duration("2 hr") == 7200.0
    
    async def test_parse_days(self, loop_context):
        """Test days parsing."""
        assert loop_context._parse_duration("1 day") == 86400.0
        assert loop_context._parse_duration("1 days") == 86400.0


class TestLoopStateManagement:
    """Tests for loop state consistency."""
    
    async def test_has_claim_returns_bool(self, state_manager, loop_state):
        """Test that has_claim returns a proper boolean."""
        result = await state_manager.has_claim(loop_state.loop_id)
        assert result is False
        assert isinstance(result, bool)
    
    async def test_loop_status_updates(self, state_manager, loop_state):
        """Test that loop status is correctly updated."""
        await state_manager.update_loop_status(loop_state.loop_id, LoopStatus.RUNNING)
        loop = await state_manager.get_loop(loop_state.loop_id)
        assert loop.status == LoopStatus.RUNNING
        
        await state_manager.update_loop_status(loop_state.loop_id, LoopStatus.IDLE)
        loop = await state_manager.get_loop(loop_state.loop_id)
        assert loop.status == LoopStatus.IDLE


class TestContextState:
    """Tests for context state management."""
    
    async def test_set_and_get_context_value(self, loop_context):
        """Test setting and getting context values."""
        await loop_context.set("test_key", "test_value")
        value = await loop_context.get("test_key")
        assert value == "test_value"
    
    async def test_get_nonexistent_key_returns_default(self, loop_context):
        """Test that getting a nonexistent key returns the default."""
        value = await loop_context.get("nonexistent", default="default_value")
        assert value == "default_value"
    
    async def test_delete_context_value(self, loop_context):
        """Test deleting a context value."""
        await loop_context.set("test_key", "test_value")
        await loop_context.delete("test_key")
        value = await loop_context.get("test_key", default=None)
        assert value is None
