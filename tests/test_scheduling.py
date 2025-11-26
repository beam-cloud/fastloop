"""
End-to-end tests for the scheduling (sleep_for/sleep_until) functionality.

These tests use fakeredis to simulate Redis without needing a real server.
"""

import asyncio
import time
from queue import Queue
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import fakeredis.aioredis

from fastloop.context import LoopContext
from fastloop.loop import LoopEvent
from fastloop.state.state_redis import RedisKeys, RedisStateManager
from fastloop.types import LoopStatus, RedisConfig


# Configure pytest-asyncio
pytestmark = pytest.mark.asyncio


class FakeRedisStateManager(RedisStateManager):
    """
    A RedisStateManager that uses fakeredis instead of real Redis.
    
    This allows us to test the scheduling logic without a real Redis server.
    We disable the wake monitoring thread since fakeredis doesn't support
    keyspace notifications.
    """
    
    def __init__(self, *, app_name: str, config: RedisConfig, wake_queue: Queue[str]):
        self.app_name = app_name
        self.config = config
        self.wake_queue = wake_queue
        
        # Use fakeredis instead of real Redis
        self.rdb = fakeredis.aioredis.FakeRedis()
        self.pubsub_rdb = fakeredis.aioredis.FakeRedis()
        
        # Don't start the wake monitoring thread - fakeredis doesn't support 
        # keyspace notifications. We'll test that separately.


@pytest.fixture
def wake_queue():
    """Create a queue for wake events."""
    return Queue()


@pytest.fixture
async def state_manager(wake_queue):
    """Create a fake Redis state manager for testing."""
    config = RedisConfig(
        host="localhost",
        port=6379,
        database=0,
        password="",
        ssl=False,
    )
    manager = FakeRedisStateManager(
        app_name="test-app",
        config=config,
        wake_queue=wake_queue,
    )
    yield manager
    await manager.rdb.flushall()


@pytest.fixture
async def loop_context(state_manager):
    """Create a loop context for testing."""
    # First create a loop in the state manager
    loop, _ = await state_manager.get_or_create_loop(
        loop_name="test-loop",
        current_function_path="test.module.func",
    )
    
    context = LoopContext(
        loop_id=loop.loop_id,
        initial_event=None,
        state_manager=state_manager,
    )
    return context


class TestSetWakeTime:
    """Tests for the set_wake_time functionality."""
    
    async def test_set_wake_time_creates_key_with_ttl(self, state_manager):
        """Test that set_wake_time creates a Redis key with the correct TTL."""
        loop, _ = await state_manager.get_or_create_loop(
            loop_name="test-loop",
            current_function_path="test.func",
        )
        
        # Set wake time 5 seconds in the future
        wake_timestamp = time.time() + 5.0
        await state_manager.set_wake_time(loop.loop_id, wake_timestamp)
        
        # Check that the wake key exists
        wake_key = RedisKeys.LOOP_WAKE_KEY.format(
            app_name="test-app",
            loop_id=loop.loop_id,
        )
        value = await state_manager.rdb.get(wake_key)
        assert value == b"wake"
        
        # Check the TTL is approximately correct (within 1 second tolerance)
        ttl_ms = await state_manager.rdb.pttl(wake_key)
        assert 4000 <= ttl_ms <= 5100  # 4-5 seconds in milliseconds
    
    async def test_set_wake_time_adds_to_index(self, state_manager):
        """Test that set_wake_time adds the key to the wake index."""
        loop, _ = await state_manager.get_or_create_loop(
            loop_name="test-loop",
            current_function_path="test.func",
        )
        
        wake_timestamp = time.time() + 10.0
        await state_manager.set_wake_time(loop.loop_id, wake_timestamp)
        
        # Check that the key is in the wake index
        wake_index = RedisKeys.LOOP_WAKE_INDEX.format(app_name="test-app")
        wake_key = RedisKeys.LOOP_WAKE_KEY.format(
            app_name="test-app",
            loop_id=loop.loop_id,
        )
        
        members = await state_manager.rdb.smembers(wake_index)
        assert wake_key.encode() in members
    
    async def test_set_wake_time_with_subsecond_precision(self, state_manager):
        """Test that sub-second durations work correctly."""
        loop, _ = await state_manager.get_or_create_loop(
            loop_name="test-loop",
            current_function_path="test.func",
        )
        
        # Set wake time 500ms in the future
        wake_timestamp = time.time() + 0.5
        await state_manager.set_wake_time(loop.loop_id, wake_timestamp)
        
        wake_key = RedisKeys.LOOP_WAKE_KEY.format(
            app_name="test-app",
            loop_id=loop.loop_id,
        )
        
        # Check the TTL is approximately 500ms
        ttl_ms = await state_manager.rdb.pttl(wake_key)
        assert 400 <= ttl_ms <= 600  # 400-600ms tolerance
    
    async def test_set_wake_time_minimum_1ms(self, state_manager):
        """Test that very small durations get a minimum of 1ms TTL."""
        loop, _ = await state_manager.get_or_create_loop(
            loop_name="test-loop",
            current_function_path="test.func",
        )
        
        # Set wake time just barely in the future (1ms)
        wake_timestamp = time.time() + 0.001
        await state_manager.set_wake_time(loop.loop_id, wake_timestamp)
        
        wake_key = RedisKeys.LOOP_WAKE_KEY.format(
            app_name="test-app",
            loop_id=loop.loop_id,
        )
        
        # Key should exist with at least 1ms TTL
        ttl_ms = await state_manager.rdb.pttl(wake_key)
        assert ttl_ms >= 1
    
    async def test_set_wake_time_past_timestamp_raises(self, state_manager):
        """Test that setting a wake time in the past raises an error."""
        loop, _ = await state_manager.get_or_create_loop(
            loop_name="test-loop",
            current_function_path="test.func",
        )
        
        # Try to set wake time in the past
        past_timestamp = time.time() - 10.0
        with pytest.raises(ValueError, match="Timestamp is in the past"):
            await state_manager.set_wake_time(loop.loop_id, past_timestamp)


class TestContextSleepFor:
    """Tests for the context.sleep_for() method."""
    
    async def test_sleep_for_float_seconds(self, loop_context, state_manager):
        """Test sleep_for with a float duration in seconds."""
        from fastloop.exceptions import LoopPausedError
        
        with pytest.raises(LoopPausedError):
            await loop_context.sleep_for(5.0)
        
        # Check that the wake time was set
        wake_key = RedisKeys.LOOP_WAKE_KEY.format(
            app_name="test-app",
            loop_id=loop_context.loop_id,
        )
        value = await state_manager.rdb.get(wake_key)
        assert value == b"wake"
    
    async def test_sleep_for_string_seconds(self, loop_context, state_manager):
        """Test sleep_for with a string duration like '5 seconds'."""
        from fastloop.exceptions import LoopPausedError
        
        with pytest.raises(LoopPausedError):
            await loop_context.sleep_for("5 seconds")
        
        wake_key = RedisKeys.LOOP_WAKE_KEY.format(
            app_name="test-app",
            loop_id=loop_context.loop_id,
        )
        ttl_ms = await state_manager.rdb.pttl(wake_key)
        assert 4000 <= ttl_ms <= 5100
    
    async def test_sleep_for_string_minutes(self, loop_context, state_manager):
        """Test sleep_for with minutes."""
        from fastloop.exceptions import LoopPausedError
        
        with pytest.raises(LoopPausedError):
            await loop_context.sleep_for("2 minutes")
        
        wake_key = RedisKeys.LOOP_WAKE_KEY.format(
            app_name="test-app",
            loop_id=loop_context.loop_id,
        )
        ttl_ms = await state_manager.rdb.pttl(wake_key)
        # 2 minutes = 120 seconds = 120000ms
        assert 119000 <= ttl_ms <= 121000
    
    async def test_sleep_for_string_hours(self, loop_context, state_manager):
        """Test sleep_for with hours."""
        from fastloop.exceptions import LoopPausedError
        
        with pytest.raises(LoopPausedError):
            await loop_context.sleep_for("1 hour")
        
        wake_key = RedisKeys.LOOP_WAKE_KEY.format(
            app_name="test-app",
            loop_id=loop_context.loop_id,
        )
        ttl_ms = await state_manager.rdb.pttl(wake_key)
        # 1 hour = 3600 seconds = 3600000ms
        assert 3599000 <= ttl_ms <= 3601000
    
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
    
    async def test_sleep_until_future_timestamp(self, loop_context, state_manager):
        """Test sleep_until with a future timestamp."""
        from fastloop.exceptions import LoopPausedError
        
        future_time = time.time() + 10.0
        with pytest.raises(LoopPausedError):
            await loop_context.sleep_until(future_time)
        
        wake_key = RedisKeys.LOOP_WAKE_KEY.format(
            app_name="test-app",
            loop_id=loop_context.loop_id,
        )
        value = await state_manager.rdb.get(wake_key)
        assert value == b"wake"
    
    async def test_sleep_until_past_raises(self, loop_context):
        """Test that sleeping until a past timestamp raises an error."""
        past_time = time.time() - 10.0
        with pytest.raises(ValueError, match="Cannot sleep until a time in the past"):
            await loop_context.sleep_until(past_time)


class TestWakeIndexManagement:
    """Tests for wake index consistency."""
    
    async def test_wake_key_in_index_after_set(self, state_manager):
        """Test that wake key is correctly added to the index."""
        loop, _ = await state_manager.get_or_create_loop(
            loop_name="test-loop",
            current_function_path="test.func",
        )
        
        await state_manager.set_wake_time(loop.loop_id, time.time() + 60)
        
        wake_index = RedisKeys.LOOP_WAKE_INDEX.format(app_name="test-app")
        wake_key = RedisKeys.LOOP_WAKE_KEY.format(
            app_name="test-app",
            loop_id=loop.loop_id,
        )
        
        members = await state_manager.rdb.smembers(wake_index)
        assert wake_key.encode() in members
    
    async def test_multiple_loops_in_wake_index(self, state_manager):
        """Test that multiple loops can have wake times set."""
        loops = []
        for i in range(3):
            loop, _ = await state_manager.get_or_create_loop(
                loop_name=f"test-loop-{i}",
                current_function_path="test.func",
            )
            loops.append(loop)
            await state_manager.set_wake_time(loop.loop_id, time.time() + 60 + i)
        
        wake_index = RedisKeys.LOOP_WAKE_INDEX.format(app_name="test-app")
        members = await state_manager.rdb.smembers(wake_index)
        
        assert len(members) == 3
        
        for loop in loops:
            wake_key = RedisKeys.LOOP_WAKE_KEY.format(
                app_name="test-app",
                loop_id=loop.loop_id,
            )
            assert wake_key.encode() in members


class TestMissedWakeEvents:
    """Tests for the missed wake events detection."""
    
    async def test_check_missed_wake_events_detects_expired(self, state_manager, wake_queue):
        """Test that expired wake keys are detected and queued."""
        import redis
        
        loop, _ = await state_manager.get_or_create_loop(
            loop_name="test-loop",
            current_function_path="test.func",
        )
        
        # Manually add a wake key to the index without the actual key
        # This simulates a key that expired while the server was down
        wake_index = RedisKeys.LOOP_WAKE_INDEX.format(app_name="test-app")
        wake_key = RedisKeys.LOOP_WAKE_KEY.format(
            app_name="test-app",
            loop_id=loop.loop_id,
        )
        
        await state_manager.rdb.sadd(wake_index, wake_key)
        
        # Create a sync Redis client for the check function
        # Using fakeredis sync client
        import fakeredis
        sync_rdb = fakeredis.FakeRedis()
        
        # Add the wake key to the sync fake redis's index too
        sync_rdb.sadd(wake_index, wake_key)
        
        # Now check for missed events - the key doesn't exist so it should be detected
        state_manager._check_missed_wake_events_sync(sync_rdb)
        
        # Check that the loop_id was added to the wake queue
        assert not wake_queue.empty()
        queued_loop_id = wake_queue.get_nowait()
        assert queued_loop_id == loop.loop_id


class TestLoopStateConsistency:
    """Tests for loop state consistency during sleep/wake cycles."""
    
    async def test_loop_status_after_pause(self, state_manager):
        """Test that loop status is correctly updated after pausing."""
        loop, _ = await state_manager.get_or_create_loop(
            loop_name="test-loop",
            current_function_path="test.func",
        )
        
        # Update status to RUNNING
        await state_manager.update_loop_status(loop.loop_id, LoopStatus.RUNNING)
        
        # Verify it's running
        loop = await state_manager.get_loop(loop.loop_id)
        assert loop.status == LoopStatus.RUNNING
        
        # Update to IDLE (what happens when loop pauses for sleep)
        await state_manager.update_loop_status(loop.loop_id, LoopStatus.IDLE)
        
        loop = await state_manager.get_loop(loop.loop_id)
        assert loop.status == LoopStatus.IDLE
    
    async def test_has_claim_returns_bool(self, state_manager):
        """Test that has_claim returns a proper boolean."""
        loop, _ = await state_manager.get_or_create_loop(
            loop_name="test-loop",
            current_function_path="test.func",
        )
        
        # No claim yet
        result = await state_manager.has_claim(loop.loop_id)
        assert result is False
        assert isinstance(result, bool)


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
        assert loop_context._parse_duration("1.5 minutes") == 90.0
    
    async def test_parse_hours_variations(self, loop_context):
        """Test various ways to specify hours."""
        assert loop_context._parse_duration("2 hours") == 7200.0
        assert loop_context._parse_duration("2 hour") == 7200.0
        assert loop_context._parse_duration("2 hrs") == 7200.0
        assert loop_context._parse_duration("2 hr") == 7200.0
        assert loop_context._parse_duration("0.5 hours") == 1800.0
    
    async def test_parse_days_variations(self, loop_context):
        """Test various ways to specify days."""
        assert loop_context._parse_duration("1 day") == 86400.0
        assert loop_context._parse_duration("1 days") == 86400.0
        assert loop_context._parse_duration("0.5 days") == 43200.0
    
    async def test_parse_with_extra_whitespace(self, loop_context):
        """Test that extra whitespace is handled."""
        assert loop_context._parse_duration("  5 seconds  ") == 5.0
        assert loop_context._parse_duration("5  seconds") == 5.0


class TestEndToEndWakeFlow:
    """
    End-to-end tests for the complete wake flow.
    
    These tests simulate what happens when a loop sleeps and wakes up.
    """
    
    async def test_sleep_and_wake_queue_integration(self, state_manager, wake_queue):
        """Test the full flow: sleep -> key expires -> wake queue populated."""
        loop, _ = await state_manager.get_or_create_loop(
            loop_name="test-loop",
            current_function_path="test.func",
        )
        
        context = LoopContext(
            loop_id=loop.loop_id,
            initial_event=None,
            state_manager=state_manager,
        )
        
        # Set a very short sleep time
        from fastloop.exceptions import LoopPausedError
        
        with pytest.raises(LoopPausedError):
            await context.sleep_for(0.1)  # 100ms
        
        # Verify the wake key was created
        wake_key = RedisKeys.LOOP_WAKE_KEY.format(
            app_name="test-app",
            loop_id=loop.loop_id,
        )
        value = await state_manager.rdb.get(wake_key)
        assert value == b"wake"
        
        # Wait for the key to expire (fakeredis supports TTL expiration)
        await asyncio.sleep(0.2)
        
        # Key should be gone now
        value = await state_manager.rdb.get(wake_key)
        assert value is None
        
        # Simulate what _check_missed_wake_events_sync would do
        # Add to index and check
        wake_index = RedisKeys.LOOP_WAKE_INDEX.format(app_name="test-app")
        
        # In real scenario, the key expiration event would trigger the wake
        # Here we manually simulate checking for missed events
        import fakeredis
        sync_rdb = fakeredis.FakeRedis()
        sync_rdb.sadd(wake_index, wake_key)
        
        state_manager._check_missed_wake_events_sync(sync_rdb)
        
        # The loop should be in the wake queue
        assert not wake_queue.empty()
        woken_loop_id = wake_queue.get_nowait()
        assert woken_loop_id == loop.loop_id
    
    async def test_multiple_sequential_sleeps(self, state_manager):
        """Test that a loop can sleep multiple times in sequence."""
        loop, _ = await state_manager.get_or_create_loop(
            loop_name="test-loop",
            current_function_path="test.func",
        )
        
        context = LoopContext(
            loop_id=loop.loop_id,
            initial_event=None,
            state_manager=state_manager,
        )
        
        from fastloop.exceptions import LoopPausedError
        
        # First sleep
        with pytest.raises(LoopPausedError):
            await context.sleep_for(1.0)
        
        wake_key = RedisKeys.LOOP_WAKE_KEY.format(
            app_name="test-app",
            loop_id=loop.loop_id,
        )
        ttl1 = await state_manager.rdb.pttl(wake_key)
        
        # Reset pause flag to simulate loop resuming
        context._pause_requested = False
        
        # Second sleep (should overwrite the first)
        with pytest.raises(LoopPausedError):
            await context.sleep_for(5.0)
        
        ttl2 = await state_manager.rdb.pttl(wake_key)
        
        # Second TTL should be longer (approximately 5 seconds vs 1 second)
        assert ttl2 > ttl1


class TestContextState:
    """Tests for context state management."""
    
    async def test_set_and_get_context_value(self, loop_context, state_manager):
        """Test setting and getting context values."""
        await loop_context.set("test_key", "test_value")
        
        value = await loop_context.get("test_key")
        assert value == "test_value"
    
    async def test_get_nonexistent_key_returns_default(self, loop_context):
        """Test that getting a nonexistent key returns the default."""
        value = await loop_context.get("nonexistent", default="default_value")
        assert value == "default_value"
    
    async def test_delete_context_value(self, loop_context, state_manager):
        """Test deleting a context value."""
        await loop_context.set("test_key", "test_value")
        await loop_context.delete("test_key")
        
        value = await loop_context.get("test_key", default=None)
        assert value is None
