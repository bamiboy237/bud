"""Tests for Persistent Timer Primitives and Background Wakeup Routines (COURSE Block B08)."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from bud.core.timers import (
    TimerStore,
    TimerWakeupDispatcher,
    calculate_next_fire_time,
    compute_slot_key,
)


def test_timer_primitives_crud_and_persistence(tmp_path: Path):
    db_file = tmp_path / "timers.db"
    store = TimerStore(db_file)

    now = datetime(2026, 11, 15, 12, 0, 0, tzinfo=UTC)

    # 1. Schedule one-shot timer
    fire_at = now + timedelta(hours=2)
    t_once = store.schedule_once(
        session_id="s1",
        name="Meeting Reminder",
        fire_at=fire_at,
        payload={"topic": "Design Review"},
    )
    assert t_once.schedule_type == "once"
    assert t_once.status == "active"
    assert t_once.payload["topic"] == "Design Review"

    # 2. Schedule interval timer
    t_interval = store.schedule_interval(
        session_id="s1",
        name="Sync Check",
        interval_seconds=300,
        payload={"action": "poll"},
        start_at=now,
    )
    assert t_interval.schedule_type == "interval"
    assert t_interval.schedule_value == "300"

    # 3. Schedule daily timer
    t_daily = store.schedule_daily(
        session_id="s1",
        name="Morning Briefing",
        time_str="09:00",
        timezone="America/Chicago",
        start_at=now,
    )
    assert t_daily.schedule_type == "daily"
    assert t_daily.timezone == "America/Chicago"

    # 4. Test Pause, Resume, Cancel controls
    assert store.pause(t_interval.timer_id) is True
    t_int_paused = store.get(t_interval.timer_id)
    assert t_int_paused is not None
    assert t_int_paused.status == "paused"

    assert store.resume(t_interval.timer_id) is True
    t_int_resumed = store.get(t_interval.timer_id)
    assert t_int_resumed is not None
    assert t_int_resumed.status == "active"

    assert store.cancel(t_once.timer_id) is True
    t_once_cancelled = store.get(t_once.timer_id)
    assert t_once_cancelled is not None
    assert t_once_cancelled.status == "cancelled"

    # 5. Test Persistence across restarts:
    # Close connection and open new TimerStore instance on same sqlite file
    store_restarted = TimerStore(db_file)
    restored_daily = store_restarted.get(t_daily.timer_id)
    assert restored_daily is not None
    assert restored_daily.name == "Morning Briefing"
    assert restored_daily.status == "active"

    restored_cancelled = store_restarted.get(t_once.timer_id)
    assert restored_cancelled is not None
    assert restored_cancelled.status == "cancelled"


def test_per_slot_deduplication(tmp_path: Path):
    db_file = tmp_path / "timers.db"
    store = TimerStore(db_file)

    now = datetime.now(UTC)
    t = store.schedule_interval("s_dedup", "heartbeat", interval_seconds=60, start_at=now)
    slot_key = compute_slot_key(t.timer_id, t.next_fire_at)

    # First claim must succeed
    exec_1 = store.claim_slot(
        timer_id=t.timer_id,
        session_id=t.session_id,
        slot_key=slot_key,
        scheduled_for=t.next_fire_at,
        fired_at=now.isoformat(),
    )
    assert exec_1 is not None

    # Duplicate claim on the same slot must be rejected (atomic deduplication)
    exec_2 = store.claim_slot(
        timer_id=t.timer_id,
        session_id=t.session_id,
        slot_key=slot_key,
        scheduled_for=t.next_fire_at,
        fired_at=now.isoformat(),
    )
    assert exec_2 is None


def test_timezone_and_dst_fire_time():
    # Test America/New_York daily time calculation
    zone = ZoneInfo("America/New_York")
    ref_dt = datetime(2026, 7, 1, 10, 0, 0, tzinfo=zone)  # 10:00 EDT

    # Scheduling for 14:00 local time today
    next_fire = calculate_next_fire_time("daily", "14:00", "America/New_York", ref_dt)
    assert next_fire is not None
    # 14:00 EDT is 18:00 UTC
    assert next_fire.astimezone(zone).hour == 14
    assert next_fire.astimezone(UTC).hour == 18

    # Scheduling for 08:00 (which already passed today) -> advances to tomorrow
    next_tomorrow = calculate_next_fire_time("daily", "08:00", "America/New_York", ref_dt)
    assert next_tomorrow is not None
    assert next_tomorrow.astimezone(zone).day == 2
    assert next_tomorrow.astimezone(zone).hour == 8


@pytest.mark.asyncio
async def test_timer_wakeup_dispatcher_execution(tmp_path: Path):
    db_file = tmp_path / "timers.db"
    store = TimerStore(db_file)

    wakeups_received = []

    async def handle_wakeup(timer, payload):
        wakeups_received.append({"timer_id": timer.timer_id, "payload": payload})

    dispatcher = TimerWakeupDispatcher(store, on_wakeup=handle_wakeup, poll_interval=0.1)

    now = datetime.now(UTC)
    # Schedule an interval timer set to fire immediately
    due_fire_time = now - timedelta(seconds=1)
    t = store.create_timer(
        session_id="session_wakeup",
        name="Instant Job",
        schedule_type="interval",
        schedule_value="10",
        payload={"task": "check_inbox"},
        start_at=due_fire_time,
    )

    # Run one dispatcher tick
    executed = await dispatcher.tick(now=now)
    assert len(executed) == 1
    assert len(wakeups_received) == 1
    assert wakeups_received[0]["timer_id"] == t.timer_id
    assert wakeups_received[0]["payload"]["task"] == "check_inbox"

    # Verify timer advanced to next occurrence and run_count incremented
    updated = store.get(t.timer_id)
    assert updated is not None
    assert updated.run_count == 1
    assert updated.last_fired_at is not None
    assert updated.is_active is True

    # Immediate second tick at the same timestamp must not fire again (already executed)
    executed_again = await dispatcher.tick(now=now)
    assert len(executed_again) == 0


@pytest.mark.asyncio
async def test_missed_run_downtime_policy(tmp_path: Path):
    db_file = tmp_path / "timers.db"
    store = TimerStore(db_file)

    wakeups_skip = []

    async def handle_skip(t, p):
        wakeups_skip.append(p)

    dispatcher_skip = TimerWakeupDispatcher(store, on_wakeup=handle_skip, poll_interval=0.1)

    # Timer was scheduled for 2 hours ago (downtime gap), with missed_policy='skip'
    downtime_past = datetime.now(UTC) - timedelta(hours=2)
    t_skip = store.create_timer(
        session_id="s_downtime",
        name="Missed Task",
        schedule_type="interval",
        schedule_value="3600",
        payload={"action": "daily_sync"},
        missed_policy="skip",
        start_at=downtime_past,
    )

    # Dispatcher runs upon server restart
    executed = await dispatcher_skip.tick(now=datetime.now(UTC))
    # Under 'skip' policy, the stale missed occurrence is not executed
    assert len(executed) == 0
    assert len(wakeups_skip) == 0

    # But the timer is safely advanced to the future
    advanced = store.get(t_skip.timer_id)
    assert advanced is not None
    assert advanced.status == "active"


@pytest.mark.asyncio
async def test_watchdog_stalled_task_wakeup(tmp_path: Path):
    db_file = tmp_path / "timers.db"
    store = TimerStore(db_file)

    alerts = []

    async def on_watchdog(timer, payload):
        alerts.append(payload)

    dispatcher = TimerWakeupDispatcher(store, on_wakeup=on_watchdog, poll_interval=0.1)

    now = datetime.now(UTC)
    # Schedule watchdog with timeout of 10 seconds, starting in past so it's immediately due
    t_watchdog = store.schedule_watchdog(
        session_id="session_task_123",
        task_id=42,
        timeout_seconds=10,
        payload={"reason": "github_branch_check_timeout"},
    )
    # Force next_fire_at to past to simulate timeout
    with store._connect() as db:
        past_iso = (now - timedelta(seconds=5)).isoformat()
        db.execute(
            "UPDATE timers SET next_fire_at = ? WHERE timer_id = ?",
            (past_iso, t_watchdog.timer_id),
        )

    executed = await dispatcher.tick(now=now)
    assert len(executed) == 1
    assert len(alerts) == 1
    assert alerts[0]["action"] == "task_watchdog"
    assert alerts[0]["task_id"] == 42
    assert alerts[0]["pet_status"] == "needsYou"
    assert alerts[0]["reason"] == "github_branch_check_timeout"
