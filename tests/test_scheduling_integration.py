"""
Integration tests for scheduling that require a real Redis server.

These tests verify the actual wake-on-expiry flow using Redis keyspace notifications.

To run these tests, you need a Redis server running locally:
    docker run -d -p 6379:6379 redis:latest

Run with: pytest tests/test_scheduling_integration.py -v -s
"""

import asyncio
import os
import time
from queue import Queue

import pytest

# Skip all tests in this file if REDIS_TEST_HOST is not set
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.environ.get("REDIS_TEST_HOST"),
        reason="Set REDIS_TEST_HOST=localhost to run integration tests with real Redis"
    ),
]


@pytest.fixture
def redis_config():
    """Get Redis config from environment."""
    from fastloop.types import RedisConfig
    
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
async def real_state_manager(redis_config, wake_queue):
    """Create a real Redis state manager for integration testing."""
    from fastloop.state.state_redis import RedisStateManager
    
    manager = RedisStateManager(
        app_name="integration-test",
        config=redis_config,
        wake_queue=wake_queue,
    )
    
    yield manager
    
    # Cleanup: flush the test database
    await manager.rdb.flushdb()


class TestRealRedisWakeFlow:
    """
    Integration tests that verify the actual Redis keyspace notification flow.
    
    These tests require a real Redis server with keyspace notifications enabled.
    """
    
    async def test_wake_key_expires_and_triggers_notification(
        self, real_state_manager, wake_queue
    ):
        """
        Test the full flow: set wake time -> key expires -> wake queue populated.
        
        This test verifies that Redis keyspace notifications work correctly.
        """
        from fastloop.state.state_redis import RedisKeys
        
        # Create a loop
        loop, _ = await real_state_manager.get_or_create_loop(
            loop_name="integration-test-loop",
            current_function_path="test.func",
        )
        
        # Set a short wake time (500ms)
        wake_timestamp = time.time() + 0.5
        await real_state_manager.set_wake_time(loop.loop_id, wake_timestamp)
        
        # Verify the key was created
        wake_key = RedisKeys.LOOP_WAKE_KEY.format(
            app_name="integration-test",
            loop_id=loop.loop_id,
        )
        value = await real_state_manager.rdb.get(wake_key)
        assert value == b"wake"
        
        # Wait for the key to expire and the notification to be processed
        # Give extra time for Redis to fire the notification and our thread to process it
        await asyncio.sleep(1.5)
        
        # The wake monitoring thread should have put the loop_id in the queue
        assert not wake_queue.empty(), "Wake queue should have received the loop_id"
        
        woken_loop_id = wake_queue.get_nowait()
        assert woken_loop_id == loop.loop_id
    
    async def test_multiple_loops_wake_independently(
        self, real_state_manager, wake_queue
    ):
        """Test that multiple loops can wake at different times."""
        from fastloop.state.state_redis import RedisKeys
        
        # Create multiple loops with different wake times
        loops = []
        for i in range(3):
            loop, _ = await real_state_manager.get_or_create_loop(
                loop_name=f"integration-test-loop-{i}",
                current_function_path="test.func",
            )
            loops.append(loop)
            
            # Stagger wake times: 300ms, 600ms, 900ms
            wake_timestamp = time.time() + (0.3 * (i + 1))
            await real_state_manager.set_wake_time(loop.loop_id, wake_timestamp)
        
        # Wait for all to expire
        await asyncio.sleep(1.5)
        
        # All three should be in the queue
        woken_ids = set()
        while not wake_queue.empty():
            woken_ids.add(wake_queue.get_nowait())
        
        expected_ids = {loop.loop_id for loop in loops}
        assert woken_ids == expected_ids, f"Expected {expected_ids}, got {woken_ids}"
    
    async def test_overwrite_wake_time(self, real_state_manager, wake_queue):
        """Test that setting a new wake time overwrites the old one."""
        from fastloop.state.state_redis import RedisKeys
        
        loop, _ = await real_state_manager.get_or_create_loop(
            loop_name="integration-test-loop",
            current_function_path="test.func",
        )
        
        # Set initial wake time far in the future
        await real_state_manager.set_wake_time(loop.loop_id, time.time() + 60)
        
        wake_key = RedisKeys.LOOP_WAKE_KEY.format(
            app_name="integration-test",
            loop_id=loop.loop_id,
        )
        
        # Verify long TTL
        ttl_ms = await real_state_manager.rdb.pttl(wake_key)
        assert ttl_ms > 50000  # More than 50 seconds
        
        # Overwrite with shorter wake time
        await real_state_manager.set_wake_time(loop.loop_id, time.time() + 0.5)
        
        # Verify short TTL
        ttl_ms = await real_state_manager.rdb.pttl(wake_key)
        assert ttl_ms < 1000  # Less than 1 second
        
        # Wait and verify it wakes
        await asyncio.sleep(1.0)
        
        assert not wake_queue.empty()
        assert wake_queue.get_nowait() == loop.loop_id


class TestRealRedisPubSub:
    """Test pubsub functionality with real Redis."""
    
    async def test_event_notification_pubsub(self, real_state_manager):
        """Test that event notifications work via pubsub."""
        from fastloop.loop import LoopEvent
        from fastloop.types import LoopEventSender
        
        loop, _ = await real_state_manager.get_or_create_loop(
            loop_name="pubsub-test-loop",
            current_function_path="test.func",
        )
        
        # Subscribe to events
        pubsub = await real_state_manager.subscribe_to_events(loop.loop_id)
        
        # Create and push an event
        event = LoopEvent(loop_id=loop.loop_id, sender=LoopEventSender.SERVER)
        event.nonce = await real_state_manager.get_next_nonce(loop.loop_id)
        await real_state_manager.push_event(loop.loop_id, event)
        
        # Wait for notification
        received = await real_state_manager.wait_for_event_notification(
            pubsub, timeout=1.0
        )
        
        assert received is True
        
        # Cleanup
        await pubsub.unsubscribe()
        await pubsub.close()
