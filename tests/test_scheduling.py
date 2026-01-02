"""
End-to-end tests for scheduling (sleep_for/sleep_until).

Requires REDIS_TEST_HOST env var. In CI, provided by Redis service.
Locally: docker run -d -p 6379:6379 redis:7
"""

import asyncio
import os
import time
import uuid
from contextlib import suppress
from queue import Queue

import pytest

from fastloop import Loop
from fastloop.context import LoopContext
from fastloop.exceptions import LoopPausedError
from fastloop.models import LoopEvent
from fastloop.state.state_redis import (
    WAKE_RECONCILIATION_INTERVAL_S,
    RedisKeys,
    RedisStateManager,
)
from fastloop.types import LoopStatus, RedisConfig

# Skip all tests if Redis is not available
pytestmark = pytest.mark.skipif(
    not os.environ.get("REDIS_TEST_HOST"),
    reason="Set REDIS_TEST_HOST to run scheduling tests (e.g., REDIS_TEST_HOST=localhost)",
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
def app_name():
    """Unique app name per test to prevent thread interference."""
    return f"test-app-{uuid.uuid4().hex[:8]}"


@pytest.fixture
def wake_queue():
    """Create a queue for wake events."""
    return Queue()


@pytest.fixture
async def state_manager(redis_config, wake_queue, app_name):
    """Create a Redis state manager connected to real Redis."""
    manager = RedisStateManager(
        app_name=app_name,
        config=redis_config,
        wake_queue=wake_queue,
    )

    # Wait for wake monitoring thread to start and configure notifications
    await asyncio.sleep(0.2)

    yield manager

    # Cleanup: stop wake thread first, then clear Redis
    manager.stop()
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

    async def test_set_wake_time_adds_to_schedule(
        self, state_manager, loop_state, app_name
    ):
        """Test that set_wake_time adds the loop to the wake schedule ZSET."""
        wake_timestamp = time.time() + 5.0
        await state_manager.set_wake_time(loop_state.loop_id, wake_timestamp)

        schedule_key = RedisKeys.LOOP_WAKE_SCHEDULE.format(app_name=app_name)

        # Check the loop is in the schedule with correct timestamp
        score = await state_manager.rdb.zscore(schedule_key, loop_state.loop_id)
        assert score is not None
        assert abs(score - wake_timestamp) < 0.1  # Within 100ms tolerance

    async def test_set_wake_time_with_subsecond_precision(
        self, state_manager, loop_state, app_name
    ):
        """Test that sub-second durations work correctly."""
        wake_timestamp = time.time() + 0.5
        await state_manager.set_wake_time(loop_state.loop_id, wake_timestamp)

        schedule_key = RedisKeys.LOOP_WAKE_SCHEDULE.format(app_name=app_name)
        score = await state_manager.rdb.zscore(schedule_key, loop_state.loop_id)
        assert abs(score - wake_timestamp) < 0.1

    async def test_set_wake_time_past_timestamp_raises(self, state_manager, loop_state):
        """Test that setting a wake time in the past raises an error."""
        past_timestamp = time.time() - 10.0
        with pytest.raises(ValueError, match="Timestamp is in the past"):
            await state_manager.set_wake_time(loop_state.loop_id, past_timestamp)

    async def test_set_wake_time_overwrites_previous(
        self, state_manager, loop_state, app_name
    ):
        """Test that setting a new wake time overwrites the previous one."""
        # Set initial wake time
        await state_manager.set_wake_time(loop_state.loop_id, time.time() + 60)

        # Overwrite with new wake time
        new_timestamp = time.time() + 5.0
        await state_manager.set_wake_time(loop_state.loop_id, new_timestamp)

        schedule_key = RedisKeys.LOOP_WAKE_SCHEDULE.format(app_name=app_name)
        score = await state_manager.rdb.zscore(schedule_key, loop_state.loop_id)

        # Should have the new timestamp, not the old one
        assert abs(score - new_timestamp) < 0.1


class TestWakeMonitoring:
    """Tests for wake monitoring via ZSET reconciliation."""

    async def test_wake_via_reconciliation_polling(
        self, state_manager, wake_queue, loop_state
    ):
        """Test that reconciliation polling triggers wake."""
        await state_manager.set_wake_time(loop_state.loop_id, time.time() + 0.3)

        # Wait for reconciliation interval to process the wake
        await asyncio.sleep(WAKE_RECONCILIATION_INTERVAL_S + 0.5)

        assert not wake_queue.empty(), "Wake queue should have the loop_id"
        assert wake_queue.get_nowait() == loop_state.loop_id

    async def test_wake_via_reconciliation(
        self, state_manager, wake_queue, loop_state, app_name
    ):
        """Test that periodic reconciliation catches due wakes."""
        # Directly add to schedule (simulating a wake that was set before restart)
        schedule_key = RedisKeys.LOOP_WAKE_SCHEDULE.format(app_name=app_name)
        past_timestamp = time.time() - 1.0  # Already due
        await state_manager.rdb.zadd(schedule_key, {loop_state.loop_id: past_timestamp})

        # Wait for reconciliation interval
        await asyncio.sleep(WAKE_RECONCILIATION_INTERVAL_S + 0.5)

        assert not wake_queue.empty(), "Reconciliation should have caught the due wake"
        assert wake_queue.get_nowait() == loop_state.loop_id

    async def test_wake_removes_from_schedule(
        self, state_manager, wake_queue, loop_state, app_name
    ):
        """Test that woken loops are removed from the schedule."""
        await state_manager.set_wake_time(loop_state.loop_id, time.time() + 0.3)

        schedule_key = RedisKeys.LOOP_WAKE_SCHEDULE.format(app_name=app_name)

        # Should be in schedule initially
        score = await state_manager.rdb.zscore(schedule_key, loop_state.loop_id)
        assert score is not None

        # Wait for wake
        await asyncio.sleep(1.5)

        # Drain the wake queue
        while not wake_queue.empty():
            wake_queue.get_nowait()

        # Should be removed from schedule
        score = await state_manager.rdb.zscore(schedule_key, loop_state.loop_id)
        assert score is None

    async def test_multiple_loops_wake_correctly(self, state_manager, wake_queue):
        """Test that multiple loops wake at their scheduled times."""
        loops = []
        for i in range(3):
            loop, _ = await state_manager.get_or_create_loop(
                loop_name=f"test-loop-{i}",
                current_function_path="test.func",
            )
            loops.append(loop)
            await state_manager.set_wake_time(loop.loop_id, time.time() + 0.2 * (i + 1))

        # Wait for all to wake (longest is 0.6s + reconciliation buffer)
        await asyncio.sleep(2.0)

        woken_ids = set()
        while not wake_queue.empty():
            woken_ids.add(wake_queue.get_nowait())

        expected_ids = {loop.loop_id for loop in loops}
        assert woken_ids == expected_ids

    async def test_no_duplicate_wakes(self, state_manager, wake_queue, loop_state):
        """Test that a loop is only woken once per schedule."""
        await state_manager.set_wake_time(loop_state.loop_id, time.time() + 0.3)

        # Wait for multiple reconciliation cycles
        await asyncio.sleep(WAKE_RECONCILIATION_INTERVAL_S * 3)

        # Should only have one wake
        woken_ids = []
        while not wake_queue.empty():
            woken_ids.append(wake_queue.get_nowait())

        assert len(woken_ids) == 1
        assert woken_ids[0] == loop_state.loop_id

    async def test_overwriting_wake_time(self, state_manager, wake_queue, loop_state):
        """Test that setting a new wake time overwrites the old one."""
        # Set initial wake time far in the future
        await state_manager.set_wake_time(loop_state.loop_id, time.time() + 60)

        # Overwrite with short wake time
        await state_manager.set_wake_time(loop_state.loop_id, time.time() + 0.3)

        await asyncio.sleep(1.5)

        assert not wake_queue.empty()
        assert wake_queue.get_nowait() == loop_state.loop_id


class TestContextSleepFor:
    """Tests for the context.sleep_for() method."""

    async def test_sleep_for_triggers_wake(self, loop_context, wake_queue):
        """Test that sleep_for sets wake time and the loop wakes up."""
        with pytest.raises(LoopPausedError):
            await loop_context.sleep_for(0.3)

        await asyncio.sleep(1.5)

        assert not wake_queue.empty()
        assert wake_queue.get_nowait() == loop_context.loop_id

    async def test_sleep_for_string_duration(
        self, loop_context, state_manager, app_name
    ):
        """Test sleep_for with string durations adds to ZSET schedule."""
        with pytest.raises(LoopPausedError):
            await loop_context.sleep_for("5 seconds")

        schedule_key = RedisKeys.LOOP_WAKE_SCHEDULE.format(app_name=app_name)
        score = await state_manager.rdb.zscore(schedule_key, loop_context.loop_id)
        assert score is not None
        expected_wake = time.time() + 5.0
        assert abs(score - expected_wake) < 0.5

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

    async def test_sleep_until_triggers_wake(self, loop_context, wake_queue):
        """Test that sleep_until sets wake time and the loop wakes up."""
        future_time = time.time() + 0.3

        with pytest.raises(LoopPausedError):
            await loop_context.sleep_until(future_time)

        await asyncio.sleep(1.5)

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


class TestWakeRobustness:
    """Robustness tests for wake logic to catch missed wakes."""

    async def test_wake_immediately_due(self, state_manager, wake_queue, app_name):
        """Test that wake scheduled for now triggers in next reconciliation."""
        loop, _ = await state_manager.get_or_create_loop(
            loop_name="immediate-wake",
            current_function_path="test.func",
        )
        schedule_key = RedisKeys.LOOP_WAKE_SCHEDULE.format(app_name=app_name)
        await state_manager.rdb.zadd(schedule_key, {loop.loop_id: time.time()})

        await asyncio.sleep(WAKE_RECONCILIATION_INTERVAL_S + 0.3)

        woken_ids = []
        while not wake_queue.empty():
            woken_ids.append(wake_queue.get_nowait())
        assert loop.loop_id in woken_ids

    async def test_wake_short_durations(self, state_manager, wake_queue):
        """Test that short durations (100ms, 200ms, 500ms) all wake reliably."""
        durations = [0.1, 0.2, 0.5]
        loops = []

        for dur in durations:
            loop, _ = await state_manager.get_or_create_loop(
                loop_name=f"short-{int(dur * 1000)}ms",
                current_function_path="test.func",
            )
            loops.append(loop)
            await state_manager.set_wake_time(loop.loop_id, time.time() + dur)

        await asyncio.sleep(WAKE_RECONCILIATION_INTERVAL_S * 2 + 0.5)

        woken_ids = set()
        while not wake_queue.empty():
            woken_ids.add(wake_queue.get_nowait())

        expected_ids = {loop.loop_id for loop in loops}
        assert woken_ids == expected_ids, f"Missing wakes: {expected_ids - woken_ids}"

    async def test_wake_under_load(self, state_manager, wake_queue):
        """Test 20 loops with staggered wakes all complete."""
        loops = []
        for i in range(20):
            loop, _ = await state_manager.get_or_create_loop(
                loop_name=f"load-test-{i}",
                current_function_path="test.func",
            )
            loops.append(loop)
            await state_manager.set_wake_time(
                loop.loop_id, time.time() + 0.1 * ((i % 5) + 1)
            )

        await asyncio.sleep(WAKE_RECONCILIATION_INTERVAL_S * 3)

        woken_ids = set()
        while not wake_queue.empty():
            woken_ids.add(wake_queue.get_nowait())

        expected_ids = {loop.loop_id for loop in loops}
        missing = expected_ids - woken_ids
        assert not missing, f"Missed {len(missing)} wakes under load: {missing}"

    async def test_rapid_reschedule(self, state_manager, wake_queue, app_name):
        """Test calling set_wake_time multiple times quickly uses latest."""
        loop, _ = await state_manager.get_or_create_loop(
            loop_name="rapid-reschedule",
            current_function_path="test.func",
        )

        await state_manager.set_wake_time(loop.loop_id, time.time() + 60)
        await state_manager.set_wake_time(loop.loop_id, time.time() + 30)
        await state_manager.set_wake_time(loop.loop_id, time.time() + 0.2)

        schedule_key = RedisKeys.LOOP_WAKE_SCHEDULE.format(app_name=app_name)
        score = await state_manager.rdb.zscore(schedule_key, loop.loop_id)
        assert score is not None
        assert score < time.time() + 1.0

        await asyncio.sleep(WAKE_RECONCILIATION_INTERVAL_S + 0.5)

        assert not wake_queue.empty()
        assert wake_queue.get_nowait() == loop.loop_id

    async def test_wake_after_loop_stop(self, state_manager, wake_queue, app_name):
        """Test that stopped loop's wake is removed and doesn't cause errors."""
        loop, _ = await state_manager.get_or_create_loop(
            loop_name="stop-test",
            current_function_path="test.func",
        )

        await state_manager.set_wake_time(loop.loop_id, time.time() + 0.5)

        schedule_key = RedisKeys.LOOP_WAKE_SCHEDULE.format(app_name=app_name)
        score = await state_manager.rdb.zscore(schedule_key, loop.loop_id)
        assert score is not None

        await state_manager.update_loop_status(loop.loop_id, LoopStatus.STOPPED)

        score = await state_manager.rdb.zscore(schedule_key, loop.loop_id)
        assert score is None, "Wake should be removed when loop is stopped"

        await asyncio.sleep(WAKE_RECONCILIATION_INTERVAL_S + 0.5)

        woken_ids = []
        while not wake_queue.empty():
            woken_ids.append(wake_queue.get_nowait())
        assert loop.loop_id not in woken_ids

    async def test_wake_not_triggered_while_claimed(self, state_manager, wake_queue):
        """Test loop with active claim gets queued but won't double-start."""
        loop, _ = await state_manager.get_or_create_loop(
            loop_name="claimed-test",
            current_function_path="test.func",
        )

        await state_manager.set_wake_time(loop.loop_id, time.time() + 0.2)

        await asyncio.sleep(WAKE_RECONCILIATION_INTERVAL_S + 0.5)

        woken_ids = []
        while not wake_queue.empty():
            woken_ids.append(wake_queue.get_nowait())
        assert loop.loop_id in woken_ids

    async def test_wake_survives_schedule_persistence(
        self, redis_config, wake_queue, app_name
    ):
        """Test that wakes persist and are processed by a new state manager."""
        manager1 = RedisStateManager(
            app_name=app_name,
            config=redis_config,
            wake_queue=wake_queue,
        )
        await asyncio.sleep(0.1)

        loop, _ = await manager1.get_or_create_loop(
            loop_name="persist-test",
            current_function_path="test.func",
        )
        await manager1.set_wake_time(loop.loop_id, time.time() + 0.3)

        manager1.stop()
        await asyncio.sleep(0.1)

        new_queue: Queue[str] = Queue()
        manager2 = RedisStateManager(
            app_name=app_name,
            config=redis_config,
            wake_queue=new_queue,
        )
        await asyncio.sleep(WAKE_RECONCILIATION_INTERVAL_S + 0.5)

        woken_ids = []
        while not new_queue.empty():
            woken_ids.append(new_queue.get_nowait())

        manager2.stop()
        await manager2.rdb.flushdb()

        assert loop.loop_id in woken_ids, "Wake should be processed by new manager"

    async def test_concurrent_wake_processing(self, state_manager, wake_queue):
        """Test multiple wakes scheduled at exact same time are all processed."""
        loops = []
        wake_time = time.time() + 0.2

        for i in range(5):
            loop, _ = await state_manager.get_or_create_loop(
                loop_name=f"concurrent-{i}",
                current_function_path="test.func",
            )
            loops.append(loop)
            await state_manager.set_wake_time(loop.loop_id, wake_time)

        await asyncio.sleep(WAKE_RECONCILIATION_INTERVAL_S + 0.5)

        woken_ids = set()
        while not wake_queue.empty():
            woken_ids.add(wake_queue.get_nowait())

        expected_ids = {loop.loop_id for loop in loops}
        assert woken_ids == expected_ids

    async def test_very_short_sleep_precision(self, state_manager, wake_queue):
        """Test sub-100ms durations are handled (queued for next reconciliation)."""
        loop, _ = await state_manager.get_or_create_loop(
            loop_name="very-short",
            current_function_path="test.func",
        )

        await state_manager.set_wake_time(loop.loop_id, time.time() + 0.05)

        await asyncio.sleep(WAKE_RECONCILIATION_INTERVAL_S + 0.2)

        woken_ids = []
        while not wake_queue.empty():
            woken_ids.append(wake_queue.get_nowait())
        assert loop.loop_id in woken_ids

    async def test_long_duration_schedule(self, state_manager, app_name):
        """Test scheduling far-future wake stores correctly."""
        loop, _ = await state_manager.get_or_create_loop(
            loop_name="long-duration",
            current_function_path="test.func",
        )

        future_time = time.time() + 86400
        await state_manager.set_wake_time(loop.loop_id, future_time)

        schedule_key = RedisKeys.LOOP_WAKE_SCHEDULE.format(app_name=app_name)
        score = await state_manager.rdb.zscore(schedule_key, loop.loop_id)
        assert score is not None
        assert abs(score - future_time) < 1.0


class TestSleepForEndToEnd:
    """End-to-end tests for sleep_for with actual loop execution."""

    async def test_sleep_for_pauses_loop(self, state_manager, loop_state):
        """Test that sleep_for properly pauses the loop via LoopPausedError."""
        context = LoopContext(
            loop_id=loop_state.loop_id,
            initial_event=None,
            state_manager=state_manager,
        )

        with pytest.raises(LoopPausedError):
            await context.sleep_for(1.0)

        assert context.should_pause is True

    async def test_sleep_for_sets_idle_status(self, state_manager, loop_state):
        """Test the loop transitions to IDLE after sleep_for."""
        await state_manager.update_loop_status(loop_state.loop_id, LoopStatus.RUNNING)

        context = LoopContext(
            loop_id=loop_state.loop_id,
            initial_event=None,
            state_manager=state_manager,
        )

        try:
            await context.sleep_for(0.5)
        except LoopPausedError:
            await state_manager.update_loop_status(loop_state.loop_id, LoopStatus.IDLE)

        loop = await state_manager.get_loop(loop_state.loop_id)
        assert loop.status == LoopStatus.IDLE

    async def test_wake_triggers_restart_callback(
        self, state_manager, wake_queue, loop_state
    ):
        """Test that wake puts loop_id in queue for restart processing."""
        context = LoopContext(
            loop_id=loop_state.loop_id,
            initial_event=None,
            state_manager=state_manager,
        )

        with suppress(LoopPausedError):
            await context.sleep_for(0.2)

        await asyncio.sleep(WAKE_RECONCILIATION_INTERVAL_S + 0.5)

        woken_ids = []
        while not wake_queue.empty():
            woken_ids.append(wake_queue.get_nowait())

        assert loop_state.loop_id in woken_ids

    async def test_full_sleep_wake_cycle(self, state_manager, wake_queue, app_name):
        """Test complete cycle: create loop -> sleep -> wake -> verify restart ready."""
        loop, _ = await state_manager.get_or_create_loop(
            loop_name="full-cycle",
            current_function_path="test.func",
        )

        await state_manager.update_loop_status(loop.loop_id, LoopStatus.RUNNING)

        context = LoopContext(
            loop_id=loop.loop_id,
            initial_event=None,
            state_manager=state_manager,
        )

        try:
            await context.sleep_for("0.5 seconds")
        except LoopPausedError:
            await state_manager.update_loop_status(loop.loop_id, LoopStatus.IDLE)

        await asyncio.sleep(WAKE_RECONCILIATION_INTERVAL_S + 0.5)

        woken = False
        while not wake_queue.empty():
            if wake_queue.get_nowait() == loop.loop_id:
                woken = True
                break

        assert woken, "Loop should have been woken"

        schedule_key = RedisKeys.LOOP_WAKE_SCHEDULE.format(app_name=app_name)
        score = await state_manager.rdb.zscore(schedule_key, loop.loop_id)
        assert score is None, "Wake should be removed from schedule after processing"

    async def test_multiple_sleep_for_in_sequence(self, state_manager, wake_queue):
        """Test that a loop can sleep multiple times in sequence."""
        loop, _ = await state_manager.get_or_create_loop(
            loop_name="multi-sleep",
            current_function_path="test.func",
        )

        for i in range(3):
            context = LoopContext(
                loop_id=loop.loop_id,
                initial_event=None,
                state_manager=state_manager,
            )

            with suppress(LoopPausedError):
                await context.sleep_for(0.2)

            await asyncio.sleep(WAKE_RECONCILIATION_INTERVAL_S + 0.3)

            woken = False
            while not wake_queue.empty():
                if wake_queue.get_nowait() == loop.loop_id:
                    woken = True

            assert woken, f"Loop should have woken on iteration {i}"


class TestWakeTiming:
    """Tests for wake timing precision and ordering."""

    async def test_wake_within_reconciliation_window(
        self, state_manager, wake_queue, loop_state
    ):
        """Test wake happens within expected time window."""
        start_time = time.time()
        sleep_duration = 0.3

        await state_manager.set_wake_time(
            loop_state.loop_id, start_time + sleep_duration
        )

        woken = False
        max_wait = WAKE_RECONCILIATION_INTERVAL_S + sleep_duration + 0.5
        elapsed = 0

        while elapsed < max_wait and not woken:
            await asyncio.sleep(0.1)
            elapsed = time.time() - start_time
            while not wake_queue.empty():
                if wake_queue.get_nowait() == loop_state.loop_id:
                    woken = True
                    break

        assert woken, "Wake should have occurred"
        assert elapsed >= sleep_duration, "Wake should not happen before scheduled time"
        assert elapsed < max_wait, f"Wake took too long: {elapsed}s"

    async def test_wake_ordering_preserved(self, state_manager, wake_queue):
        """Test that loops scheduled earlier tend to wake first."""
        loops = []
        wake_times = []

        for i in range(5):
            loop, _ = await state_manager.get_or_create_loop(
                loop_name=f"order-{i}",
                current_function_path="test.func",
            )
            loops.append(loop)
            wake_time = time.time() + 0.1 * (5 - i)
            wake_times.append((loop.loop_id, wake_time))
            await state_manager.set_wake_time(loop.loop_id, wake_time)

        await asyncio.sleep(WAKE_RECONCILIATION_INTERVAL_S * 2)

        woken_order = []
        while not wake_queue.empty():
            woken_order.append(wake_queue.get_nowait())

        expected_ids = {loop.loop_id for loop in loops}
        actual_ids = set(woken_order)
        assert actual_ids == expected_ids, (
            f"All loops should wake. Missing: {expected_ids - actual_ids}"
        )

    async def test_past_due_wakes_processed_immediately(
        self, state_manager, wake_queue, app_name
    ):
        """Test that already-due wakes are processed in first reconciliation."""
        loops = []
        schedule_key = RedisKeys.LOOP_WAKE_SCHEDULE.format(app_name=app_name)

        for i in range(3):
            loop, _ = await state_manager.get_or_create_loop(
                loop_name=f"past-due-{i}",
                current_function_path="test.func",
            )
            loops.append(loop)
            past_time = time.time() - 10 - i
            await state_manager.rdb.zadd(schedule_key, {loop.loop_id: past_time})

        await asyncio.sleep(WAKE_RECONCILIATION_INTERVAL_S + 0.3)

        woken_ids = set()
        while not wake_queue.empty():
            woken_ids.add(wake_queue.get_nowait())

        expected_ids = {loop.loop_id for loop in loops}
        assert woken_ids == expected_ids

    async def test_subsecond_precision_timing(self, state_manager):
        """Test that subsecond durations are stored with precision."""
        loop, _ = await state_manager.get_or_create_loop(
            loop_name="subsecond",
            current_function_path="test.func",
        )

        target_time = time.time() + 0.123
        await state_manager.set_wake_time(loop.loop_id, target_time)

        schedule_key = RedisKeys.LOOP_WAKE_SCHEDULE.format(
            app_name=state_manager.app_name
        )
        score = await state_manager.rdb.zscore(schedule_key, loop.loop_id)

        assert abs(score - target_time) < 0.01, (
            "Subsecond precision should be preserved"
        )

    async def test_simultaneous_wakes_all_processed(self, state_manager, wake_queue):
        """Test multiple loops with exact same wake time all get processed."""
        exact_time = time.time() + 0.2
        loops = []

        for i in range(10):
            loop, _ = await state_manager.get_or_create_loop(
                loop_name=f"simultaneous-{i}",
                current_function_path="test.func",
            )
            loops.append(loop)
            await state_manager.set_wake_time(loop.loop_id, exact_time)

        await asyncio.sleep(WAKE_RECONCILIATION_INTERVAL_S + 0.5)

        woken_ids = set()
        while not wake_queue.empty():
            woken_ids.add(wake_queue.get_nowait())

        expected_ids = {loop.loop_id for loop in loops}
        missing = expected_ids - woken_ids
        assert not missing, (
            f"All simultaneous wakes should process. Missing: {len(missing)}"
        )


class StartEvent(LoopEvent):
    type: str = "start"
    workspace_id: str = ""


class TestClassBasedLoopSleepWake:
    """Tests for class-based loops with sleep_for/wake logic."""

    async def test_class_loop_sleep_for_pauses(self, state_manager):
        """Test that a class-based loop properly pauses when calling sleep_for."""
        loop, _ = await state_manager.get_or_create_loop(
            loop_name="class-loop-pause",
            current_function_path="test.func",
        )

        class SleepingLoop(Loop):
            async def loop(self, ctx):
                await ctx.sleep_for(0.5)

        instance = SleepingLoop()
        context = LoopContext(
            loop_id=loop.loop_id,
            initial_event=None,
            state_manager=state_manager,
        )

        with pytest.raises(LoopPausedError):
            await instance.loop(context)

        assert context.should_pause is True

    async def test_class_loop_state_persists_across_wake(self, state_manager):
        """Test that state set before sleep_for persists after wake."""
        loop, _ = await state_manager.get_or_create_loop(
            loop_name="class-loop-state",
            current_function_path="test.func",
        )

        context = LoopContext(
            loop_id=loop.loop_id,
            initial_event=None,
            state_manager=state_manager,
        )

        await context.set("counter", 1)
        await context.set("workspace_id", "ws-123")
        await context.set("last_fired", {"trigger-1": 1234567890.0})

        with suppress(LoopPausedError):
            await context.sleep_for(0.3)

        await asyncio.sleep(WAKE_RECONCILIATION_INTERVAL_S + 0.5)

        new_context = LoopContext(
            loop_id=loop.loop_id,
            initial_event=None,
            state_manager=state_manager,
        )
        counter = await new_context.get("counter")
        workspace_id = await new_context.get("workspace_id")
        last_fired = await new_context.get("last_fired")

        assert counter == 1
        assert workspace_id == "ws-123"
        assert last_fired == {"trigger-1": 1234567890.0}

    async def test_class_loop_complex_state_serialization(self, state_manager):
        """Test that complex nested state serializes correctly across wakes."""
        loop, _ = await state_manager.get_or_create_loop(
            loop_name="class-loop-complex",
            current_function_path="test.func",
        )

        complex_state = {
            "last_polled": {"trigger-1": 1234567890.0, "trigger-2": 1234567900.0},
            "last_fired": {"trigger-1": 1234567800.0},
            "metadata": {
                "workspace_id": "ws-abc",
                "timezone": "America/New_York",
                "config": {"interval_minutes": 30, "enabled": True},
            },
            "counters": [1, 2, 3, 4, 5],
        }

        context = LoopContext(
            loop_id=loop.loop_id,
            initial_event=None,
            state_manager=state_manager,
        )

        await context.set("evaluator_state", complex_state)

        with suppress(LoopPausedError):
            await context.sleep_for(0.2)

        await asyncio.sleep(WAKE_RECONCILIATION_INTERVAL_S + 0.5)

        new_context = LoopContext(
            loop_id=loop.loop_id,
            initial_event=None,
            state_manager=state_manager,
        )
        restored = await new_context.get("evaluator_state")

        assert restored == complex_state
        assert restored["metadata"]["config"]["interval_minutes"] == 30

    async def test_class_loop_multiple_sleep_wake_cycles(
        self, state_manager, wake_queue
    ):
        """Test a class-based loop through multiple sleep/wake cycles with state."""
        loop, _ = await state_manager.get_or_create_loop(
            loop_name="class-loop-cycles",
            current_function_path="test.func",
        )

        for cycle in range(3):
            context = LoopContext(
                loop_id=loop.loop_id,
                initial_event=None,
                state_manager=state_manager,
            )

            current_counter = await context.get("cycle_counter", default=0)
            assert current_counter == cycle

            await context.set("cycle_counter", cycle + 1)
            await context.set("last_cycle_time", time.time())

            with suppress(LoopPausedError):
                await context.sleep_for(0.2)

            await asyncio.sleep(WAKE_RECONCILIATION_INTERVAL_S + 0.3)

            woken = False
            while not wake_queue.empty():
                if wake_queue.get_nowait() == loop.loop_id:
                    woken = True
            assert woken, f"Loop should wake on cycle {cycle}"

        final_context = LoopContext(
            loop_id=loop.loop_id,
            initial_event=None,
            state_manager=state_manager,
        )
        final_counter = await final_context.get("cycle_counter")
        assert final_counter == 3

    async def test_class_loop_wake_after_app_restart_simulation(
        self, redis_config, app_name
    ):
        """Simulate app restart: loop sleeps, manager stops, new manager processes wake."""
        queue1: Queue[str] = Queue()
        manager1 = RedisStateManager(
            app_name=app_name,
            config=redis_config,
            wake_queue=queue1,
        )
        await asyncio.sleep(0.1)

        loop, _ = await manager1.get_or_create_loop(
            loop_name="restart-simulation",
            current_function_path="test.func",
        )

        context = LoopContext(
            loop_id=loop.loop_id,
            initial_event=None,
            state_manager=manager1,
        )

        await context.set("pre_restart_data", {"key": "value", "count": 42})

        with suppress(LoopPausedError):
            await context.sleep_for(0.5)

        manager1.stop()
        await asyncio.sleep(0.1)

        queue2: Queue[str] = Queue()
        manager2 = RedisStateManager(
            app_name=app_name,
            config=redis_config,
            wake_queue=queue2,
        )
        await asyncio.sleep(WAKE_RECONCILIATION_INTERVAL_S + 0.5)

        woken_ids = []
        while not queue2.empty():
            woken_ids.append(queue2.get_nowait())

        assert loop.loop_id in woken_ids, "Wake should be processed by new manager"

        new_context = LoopContext(
            loop_id=loop.loop_id,
            initial_event=None,
            state_manager=manager2,
        )
        restored_data = await new_context.get("pre_restart_data")

        manager2.stop()
        await manager2.rdb.flushdb()

        assert restored_data == {"key": "value", "count": 42}

    async def test_class_loop_evaluator_pattern(self, state_manager, wake_queue):
        """Test the TriggerEvaluator-style pattern: evaluate -> sleep -> wake -> repeat."""
        loop, _ = await state_manager.get_or_create_loop(
            loop_name="evaluator-pattern",
            current_function_path="test.func",
        )

        class EvaluatorState:
            def __init__(self):
                self.last_fired: dict[str, float] = {}
                self.last_polled: dict[str, float] = {}
                self.run_count: int = 0

            def to_dict(self):
                return {
                    "last_fired": self.last_fired,
                    "last_polled": self.last_polled,
                    "run_count": self.run_count,
                }

            @classmethod
            def from_dict(cls, d):
                state = cls()
                state.last_fired = d.get("last_fired", {})
                state.last_polled = d.get("last_polled", {})
                state.run_count = d.get("run_count", 0)
                return state

        for iteration in range(2):
            context = LoopContext(
                loop_id=loop.loop_id,
                initial_event=None,
                state_manager=state_manager,
            )

            cached = await context.get("evaluator_state")
            state = EvaluatorState.from_dict(cached) if cached else EvaluatorState()

            state.run_count += 1
            state.last_polled[f"trigger-{iteration}"] = time.time()

            if iteration == 1:
                state.last_fired["trigger-0"] = time.time()

            await context.set("evaluator_state", state.to_dict())

            with suppress(LoopPausedError):
                await context.sleep_for(0.2)

            await asyncio.sleep(WAKE_RECONCILIATION_INTERVAL_S + 0.3)

            while not wake_queue.empty():
                wake_queue.get_nowait()

        final_context = LoopContext(
            loop_id=loop.loop_id,
            initial_event=None,
            state_manager=state_manager,
        )
        final_state_dict = await final_context.get("evaluator_state")
        final_state = EvaluatorState.from_dict(final_state_dict)

        assert final_state.run_count == 2
        assert "trigger-0" in final_state.last_polled
        assert "trigger-1" in final_state.last_polled
        assert "trigger-0" in final_state.last_fired

    async def test_class_loop_with_initial_event(self, state_manager):
        """Test class-based loop receives and processes initial event correctly."""
        loop, _ = await state_manager.get_or_create_loop(
            loop_name="event-loop",
            current_function_path="test.func",
        )

        initial_event = StartEvent(workspace_id="ws-test-123")

        context = LoopContext(
            loop_id=loop.loop_id,
            initial_event=initial_event,
            state_manager=state_manager,
        )

        assert context.initial_event is not None
        assert context.initial_event.workspace_id == "ws-test-123"

        await context.set("processed_workspace", context.initial_event.workspace_id)

        with suppress(LoopPausedError):
            await context.sleep_for(0.2)

        await asyncio.sleep(WAKE_RECONCILIATION_INTERVAL_S + 0.3)

        new_context = LoopContext(
            loop_id=loop.loop_id,
            initial_event=None,
            state_manager=state_manager,
        )
        processed = await new_context.get("processed_workspace")
        assert processed == "ws-test-123"

    async def test_stopped_loop_wake_removed_from_schedule(
        self, state_manager, wake_queue, app_name
    ):
        """Test that stopping a loop removes it from wake schedule."""
        loop, _ = await state_manager.get_or_create_loop(
            loop_name="stop-while-sleeping",
            current_function_path="test.func",
        )

        context = LoopContext(
            loop_id=loop.loop_id,
            initial_event=None,
            state_manager=state_manager,
        )

        with suppress(LoopPausedError):
            await context.sleep_for(60.0)

        schedule_key = RedisKeys.LOOP_WAKE_SCHEDULE.format(app_name=app_name)
        score = await state_manager.rdb.zscore(schedule_key, loop.loop_id)
        assert score is not None

        await state_manager.update_loop_status(loop.loop_id, LoopStatus.STOPPED)

        score = await state_manager.rdb.zscore(schedule_key, loop.loop_id)
        assert score is None, "Wake should be cleared when loop is stopped"

        await asyncio.sleep(WAKE_RECONCILIATION_INTERVAL_S + 0.3)

        woken_ids = []
        while not wake_queue.empty():
            woken_ids.append(wake_queue.get_nowait())
        assert loop.loop_id not in woken_ids
