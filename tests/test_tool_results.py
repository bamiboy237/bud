"""Saved tool results expire by default; Bud can choose to keep one longer."""

import sqlite3
import time
from datetime import timedelta

from bud.core.log import EventLog
from bud.harness.results import SqliteResultStore
from tests.test_live_tools import harness

DAY = 86_400


def store_with_results(tmp_path, *handles):
    log = EventLog(tmp_path / "events.db")
    log.append_once("session", "run.queued", {}, key="run:queued")
    # The run ended, so its results are no longer protected as in use.
    log.append("session", "run.completed", {"run_id": "run"})
    store = SqliteResultStore(log.path, retention=timedelta(hours=24))
    for handle in handles:
        store._write(handle, b"research notes")
    return store


def age(store, handle, seconds):
    with sqlite3.connect(store.path) as db:
        db.execute(
            "UPDATE tool_results SET last_used = ? WHERE handle = ?",
            (time.time() - seconds, handle),
        )


REAL_TIME = time.time


def shift_clock(monkeypatch, seconds):
    """Moves the clock to `seconds` from the real now; shifts do not add up."""
    monkeypatch.setattr("bud.harness.results.time.time", lambda: REAL_TIME() + seconds)


async def test_an_unkept_result_expires_after_the_default(tmp_path):
    store = store_with_results(tmp_path, "run/fresh", "run/stale")
    age(store, "run/stale", 2 * DAY)

    assert await store.prune() == 1
    assert await store.exists("run/fresh")
    assert not await store.exists("run/stale")


async def test_a_kept_result_lasts_the_chosen_days_then_expires(tmp_path, monkeypatch):
    store = store_with_results(tmp_path, "run/research")
    reply = await store.keep_tool_result("run/research", days=7)
    assert reply.startswith("Kept run/research until ")
    age(store, "run/research", 2 * DAY)

    shift_clock(monkeypatch, 6 * DAY)
    assert await store.prune() == 0
    shift_clock(monkeypatch, 8 * DAY)
    assert await store.prune() == 1


async def test_the_default_keep_is_one_day_and_none_keeps_it_until_changed(tmp_path, monkeypatch):
    store = store_with_results(tmp_path, "run/day", "run/always")
    await store.keep_tool_result("run/day")
    assert "until you choose" in await store.keep_tool_result("run/always", days=None)

    shift_clock(monkeypatch, DAY - 60)
    assert await store.prune() == 0
    shift_clock(monkeypatch, DAY + 60)
    assert await store.prune() == 1
    shift_clock(monkeypatch, 3650 * DAY)
    assert await store.prune() == 0
    assert await store.read("run/always") == b"research notes"

    # A new choice replaces the old one.
    await store.keep_tool_result("run/always", days=1)
    shift_clock(monkeypatch, 3652 * DAY)
    assert await store.prune() == 1


async def test_keeping_never_shortens_the_default_and_a_read_still_extends_it(
    tmp_path, monkeypatch
):
    log = EventLog(tmp_path / "events.db")
    log.append_once("session", "run.queued", {}, key="run:queued")
    log.append("session", "run.completed", {"run_id": "run"})
    store = SqliteResultStore(log.path, retention=timedelta(hours=72))
    store._write("run/long", b"notes")
    await store.keep_tool_result("run/long", days=1)

    shift_clock(monkeypatch, 2 * DAY)
    assert await store.prune() == 0  # the 72-hour default still holds
    await store.read("run/long")  # read on day 2: the default now runs to day 5
    shift_clock(monkeypatch, 4 * DAY)
    assert await store.prune() == 0
    shift_clock(monkeypatch, 6 * DAY)
    assert await store.prune() == 1


async def test_a_running_session_protects_a_kept_result_past_its_date(tmp_path, monkeypatch):
    log = EventLog(tmp_path / "events.db")
    log.append_once("session", "run.queued", {"run_id": "run"}, key="run:queued")
    store = SqliteResultStore(log.path, retention=timedelta(hours=24))
    store._write("run/live", b"notes")
    await store.keep_tool_result("run/live", days=1)

    shift_clock(monkeypatch, 3 * DAY)
    assert await store.prune() == 0


async def test_keeping_an_expired_result_says_so(tmp_path):
    store = store_with_results(tmp_path)
    assert "expired or is unavailable" in await store.keep_tool_result("run/gone")


def test_an_older_store_gains_the_expiry_column(tmp_path):
    path = tmp_path / "events.db"
    with sqlite3.connect(path) as db:
        db.execute(
            """CREATE TABLE tool_results (
                handle TEXT PRIMARY KEY, run_id TEXT NOT NULL, session_id TEXT NOT NULL,
                payload BLOB NOT NULL, last_used REAL NOT NULL)"""
        )
        db.execute("INSERT INTO tool_results VALUES ('run/old', 'run', 's', x'00', 0)")
    SqliteResultStore(path, retention=timedelta(hours=1))
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT keep_until FROM tool_results").fetchone() == (None,)


def test_keeping_runs_inside_code_mode_not_as_its_own_tool(tmp_path):
    subject = harness(tmp_path)
    assert "keep_tool_result" in subject.code_mode.tools
    assert "load_tool_result" in subject.code_mode.tools
