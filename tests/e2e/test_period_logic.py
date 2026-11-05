"""Streak/period rules for weekly, monthly and negative habits, against the native module directly
with a FROZEN 'today'. These need DB access and write backdated taps, so they only run against an
isolated database (GHOST_DB_NAME must not be ghost_db) and skip otherwise."""
import asyncio
import os
import sys
from datetime import date, timedelta

import pytest

if os.environ.get("GHOST_DB_NAME", "ghost_db") == "ghost_db" or not os.environ.get("POSTGRES_PASSWORD"):
    pytest.skip("period-logic tests need an isolated DB: set GHOST_DB_NAME (not ghost_db) + POSTGRES_PASSWORD",
                allow_module_level=True)

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "bot"))
import native  # noqa: E402

SUN = date(2026, 11, 8)          # a Sunday: Mon 2 Nov .. Sun 8 Nov is the "current week"
UID = 990077


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def frozen(monkeypatch):
    monkeypatch.setattr(native, "today_local", lambda: SUN)
    yield
    async def clean():
        c = await native.pg_conn()
        await c.execute("DELETE FROM habits WHERE user_id=$1", UID)
        await c.close()
    run(clean())


async def _mk(**kw):
    kw.setdefault("user_id", UID)
    return await native.create_habit(**kw)


async def _taps(hid, days, direction="check", value=None):
    c = await native.pg_conn()
    for d in days:
        await c.execute("INSERT INTO habit_taps (habit_id, direction, tap_date, value) VALUES ($1,$2,$3,$4)",
                        hid, direction, d, value)
    await c.close()


async def _state(hid):
    return next(h for h in await native.list_habits(UID) if h["id"] == hid)


def wk(week_offset, *weekdays):
    monday = SUN - timedelta(days=6) + timedelta(weeks=week_offset)
    return [monday + timedelta(days=w) for w in weekdays]


def test_weekly_streak_counts_consecutive_met_weeks_and_current_in_progress_does_not_break_it():
    async def go():
        hid = await _mk(name="ZZ-TEST wk", frequency_kind="count", frequency_count=3)
        await _taps(hid, wk(-2, 0, 2, 4) + wk(-1, 1, 3, 5))          # two prior weeks hit 3
        await _taps(hid, wk(0, 0))                                   # current week: 1 of 3 so far
        await native.tap_habit(hid, "uncheck")                       # (re)reconcile at SUN
        s = await _state(hid)
        assert s["streak"] == 2 and s["period"]["done"] == 1 and not s["period"]["met"], s
    run(go())


def test_weekly_streak_extends_when_current_week_meets_target_and_breaks_on_a_missed_week():
    async def go():
        hid = await _mk(name="ZZ-TEST wk2", frequency_kind="count", frequency_count=3, frequency_count_max=4)
        await _taps(hid, wk(-3, 0, 1, 2))                            # met, then a gap week -2 (only 2), then met
        await _taps(hid, wk(-2, 0, 1))
        await _taps(hid, wk(-1, 0, 1, 2))
        await _taps(hid, wk(0, 0, 1))
        await native.tap_habit(hid, "check")                         # SUN is the 3rd distinct day of this week
        s = await _state(hid)
        assert s["period"]["done"] == 3 and s["period"]["met"] and not s["period"]["bonus"], s
        assert s["streak"] == 2, "current week + last week; the 2-day week -2 breaks the chain"
        await _taps(hid, wk(0, 2, 3))                                # 5 days > max 4 -> bonus
        await native.tap_habit(hid, "check")
        assert (await _state(hid))["period"]["bonus"] is True
    run(go())


def test_multiple_ticks_same_day_count_once_toward_weekly_target():
    async def go():
        hid = await _mk(name="ZZ-TEST wk3", frequency_kind="count", frequency_count=3)
        await _taps(hid, wk(0, 6, 6, 6))
        assert (await _state(hid))["period"]["done"] == 1
    run(go())


def test_unticked_day_does_not_count_toward_weekly_target():
    async def go():
        hid = await _mk(name="ZZ-TEST wk4", frequency_kind="count", frequency_count=2)
        await _taps(hid, wk(0, 0, 1))
        await _taps(hid, wk(0, 1), direction="uncheck")              # Tue voided
        assert (await _state(hid))["period"]["done"] == 1
    run(go())


def test_value_habit_weekly_sum_target_range_and_bonus():
    async def go():
        hid = await _mk(name="ZZ-TEST val", field_type="value", frequency_kind="count",
                        target_value=5, target_max=10, unit="hrs")
        await _taps(hid, wk(0, 0), "up", 4.0)
        s = await _state(hid)
        assert s["period"]["done"] == 4.0 and not s["period"]["met"]
        await _taps(hid, wk(0, 1), "up", 3.0)
        s = await _state(hid)
        assert s["period"]["met"] and not s["period"]["bonus"]
        await _taps(hid, wk(0, 2), "up", 4.0)
        assert (await _state(hid))["period"]["bonus"] is True        # 11 > 10
    run(go())


def test_monthly_streak_counts_consecutive_months_and_resets_after_a_missed_month():
    async def go():
        hid = await _mk(name="ZZ-TEST mon", frequency_kind="monthly")
        await _taps(hid, [date(2026, 8, 15), date(2026, 10, 3)])     # Sep missed
        await native.tap_habit(hid, "check")                         # tick in Nov
        s = await _state(hid)
        assert s["period"]["met"] and s["period"]["kind"] == "month", s
        assert s["streak"] == 2, "Nov + Oct; Sep was missed so Aug does not count"
    run(go())


def test_negative_habit_streak_is_clean_days_since_last_down_tap_with_no_up_tap_needed():
    async def go():
        hid = await _mk(name="ZZ-TEST smoke", field_type="negative")
        c = await native.pg_conn()
        await c.execute("UPDATE habits SET created_at=now() - interval '30 days' WHERE id=$1", hid)
        await c.close()
        await _taps(hid, [SUN - timedelta(days=4)], "down")
        assert (await _state(hid))["streak"] == 3, "down on day-4: days 3,2,1 clean; today still in progress"
        await _taps(hid, [SUN], "down")
        assert (await _state(hid))["streak"] == 0
    run(go())
