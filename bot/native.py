"""Ghost 2.0 — native Habits & Todos (Phase 1).

Replaces Habitica's Habit/Daily/Todo split with two native types stored in Ghost's own
Postgres (the existing ghost_db — no new datastore). Runs IN PARALLEL with the Habitica
integration this phase (nothing here touches habitica.py); Lea validates it in real use
before Habitica is retired in a later, deliberate pass.

Design (per [[Ghost — 2.0 Native Habit & Nutrition Rebuild]]):
  - Todos: tickbox or checklist, a REAL due date; overdue is computed off that actual date
    (the deliberate bug-fix vs. Habitica's generic default-window "overdue").
  - Habits: field type per item (positive / negative / both / tickbox / checklist) and,
    INDEPENDENTLY, frequency per item (fixed weekdays, a monthly date, or a flexible
    "N times per week" count). Streak default = a net-positive day maintains it; per-habit
    override = "requires N positive taps" regardless of negatives.
  - Six-bucket completion counts (Session 57 taxonomy) increment on every positive/check tap
    and every todo completion, written to the shared momentum_events table.

This module is the SINGLE code path for both surfaces: the Telegram pipeline (bot.py) and
the dashboard controls (dashboard.py) both call these functions — no parallel logic. It is
deliberately deterministic (no LLM in any write), matching Ghost's anti-fabrication /
deterministic-first principle.

pg_conn() here is the shared connection helper (the Session-55 dedupe item, folded in): new
code uses this instead of re-inlining asyncpg.connect. bot.py and dashboard.py delegate to it.
"""
import os
import re
import random
import logging
import zoneinfo
from datetime import datetime, timedelta, date

import asyncpg

logger = logging.getLogger("native")

POSTGRES_HOST = os.environ.get("POSTGRES_HOST", "ghost-postgres-postgresql.ghost.svc.cluster.local")
POSTGRES_PASSWORD = os.environ.get("POSTGRES_PASSWORD", "")
LOCAL_TZ = zoneinfo.ZoneInfo(os.environ.get("GHOST_TZ", "UTC"))
DEFAULT_USER_ID = int(os.environ.get("TELEGRAM_USER_ID") or "0")

FIELD_TYPES = ("positive", "negative", "both", "tickbox", "checklist")
FREQ_KINDS = ("weekdays", "monthly", "count")
PRIORITIES = ("high", "med", "low")
BUCKETS = ["Maintenance", "Habits", "Work", "Business", "Sidequests", "Creative/Relax", "unsorted"]

# Session 76 currency economy. Base payout per completion: a routine habit/todo tap = 1 unit; a
# full quest ("adventure") = 10 units — an order of magnitude more, a reasonable starting ratio
# (revisit once real data suggests otherwise). Critical hit: CRIT_CHANCE per award, 2-3x, logged
# as its own event so it reads as a distinct moment rather than folding silently into the total.
CURRENCY_BASE = {"habit": 1, "todo": 1, "quest": 10}
CURRENCY_CRIT_CHANCE = 0.10
CURRENCY_CRIT_MULTIPLIERS = (2, 3)
ARC_DEFAULT_DAYS = 14   # matches Lea's existing sprint cadence


async def pg_conn():
    """The one shared Postgres connection helper for native (and, via delegation, for the
    new query paths in bot.py/dashboard.py). Caller closes."""
    return await asyncpg.connect(host=POSTGRES_HOST, port=5432, user="postgres",
                                 password=POSTGRES_PASSWORD, database="ghost_db")


def today_local() -> date:
    return datetime.now(LOCAL_TZ).date()


# ---------------------------------------------------------------------------
# Schema (idempotent). Ensured on first touch; cheap after that.
# ---------------------------------------------------------------------------
_SCHEMA_READY = False

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS habits (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL,
    name TEXT NOT NULL,
    field_type TEXT NOT NULL DEFAULT 'tickbox',       -- positive|negative|both|tickbox|checklist
    frequency_kind TEXT NOT NULL DEFAULT 'weekdays',  -- weekdays|monthly|count
    frequency_days INT[] DEFAULT NULL,                -- weekdays: 0=Mon..6=Sun ; monthly: day-of-month list
    frequency_count INT DEFAULT NULL,                 -- count: N times per week
    bucket TEXT NOT NULL DEFAULT 'Habits',
    streak_rule TEXT NOT NULL DEFAULT 'net_positive', -- net_positive | min_taps
    streak_min_taps INT DEFAULT NULL,
    streak_count INT NOT NULL DEFAULT 0,
    streak_best INT NOT NULL DEFAULT 0,
    last_streak_date DATE,
    active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS habit_checklist (
    id BIGSERIAL PRIMARY KEY,
    habit_id BIGINT NOT NULL REFERENCES habits(id) ON DELETE CASCADE,
    text TEXT NOT NULL,
    position INT NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS habit_taps (
    id BIGSERIAL PRIMARY KEY,
    habit_id BIGINT NOT NULL REFERENCES habits(id) ON DELETE CASCADE,
    direction TEXT NOT NULL,           -- up | down | check
    tap_date DATE NOT NULL,
    tapped_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS habit_taps_habit_date_idx ON habit_taps (habit_id, tap_date);
-- S92 (Ghost App v1): a logged value (reps, seconds, ml, …) on a tap, for value-entry habits.
ALTER TABLE habit_taps ADD COLUMN IF NOT EXISTS value NUMERIC;
-- S92: optional per-habit numeric target + unit so a value-entry habit can show "X of TARGET unit".
ALTER TABLE habits ADD COLUMN IF NOT EXISTS target_value NUMERIC;
ALTER TABLE habits ADD COLUMN IF NOT EXISTS unit TEXT;
CREATE TABLE IF NOT EXISTS habit_checklist_state (
    checklist_id BIGINT NOT NULL REFERENCES habit_checklist(id) ON DELETE CASCADE,
    state_date DATE NOT NULL,
    done BOOLEAN NOT NULL DEFAULT FALSE,
    PRIMARY KEY (checklist_id, state_date)
);
CREATE TABLE IF NOT EXISTS todos (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL,
    text TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'tickbox',   -- tickbox | checklist
    priority TEXT NOT NULL DEFAULT 'med',   -- high | med | low
    due_date DATE,                          -- real due date; overdue computed off THIS
    bucket TEXT NOT NULL DEFAULT 'unsorted',
    done BOOLEAN NOT NULL DEFAULT FALSE,
    archived BOOLEAN NOT NULL DEFAULT FALSE,
    completed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE todos ADD COLUMN IF NOT EXISTS archived BOOLEAN NOT NULL DEFAULT FALSE;
-- S78: a todo can be flagged a "quest" — completing it awards currency at the quest tier (10)
-- instead of the routine todo tier (1), via the SAME _award_currency path (source 'quest').
ALTER TABLE todos ADD COLUMN IF NOT EXISTS is_quest BOOLEAN NOT NULL DEFAULT FALSE;
CREATE TABLE IF NOT EXISTS todo_checklist (
    id BIGSERIAL PRIMARY KEY,
    todo_id BIGINT NOT NULL REFERENCES todos(id) ON DELETE CASCADE,
    text TEXT NOT NULL,
    done BOOLEAN NOT NULL DEFAULT FALSE,
    position INT NOT NULL DEFAULT 0
);
-- shared with the Session-57 momentum signal; created here too so native can write it
-- even if the dashboard/scheduler haven't run yet this deploy.
CREATE TABLE IF NOT EXISTS momentum_events (
    id BIGSERIAL PRIMARY KEY,
    item_id TEXT NOT NULL,
    source TEXT NOT NULL,
    bucket TEXT NOT NULL DEFAULT 'unsorted',
    completed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS momentum_events_completed_at_idx ON momentum_events (completed_at);
-- Session 75: one-time Habitica import provenance. A native row created from a Habitica task
-- carries that task's id here, so re-running the import is idempotent (skip if already present).
-- NULL for anything created natively; the partial unique index only constrains imported rows.
ALTER TABLE habits ADD COLUMN IF NOT EXISTS habitica_source_id TEXT;
ALTER TABLE todos  ADD COLUMN IF NOT EXISTS habitica_source_id TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS habits_habitica_src_idx ON habits(habitica_source_id) WHERE habitica_source_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS todos_habitica_src_idx  ON todos(habitica_source_id)  WHERE habitica_source_id IS NOT NULL;
-- Session 76: the ONE currency economy (reward shop + future boss-battle currency both draw from
-- this). One row per award, written by the single shared _award_currency() from both native's
-- habit-tap/todo-complete path AND the Quest toggle — no parallel award functions to drift.
-- Balance = SUM(amount). event_type distinguishes a base award from a critical hit (visible as a
-- distinct feed moment) and reserves 'token' for later flavour drops.
CREATE TABLE IF NOT EXISTS quest_currency_events (
    id BIGSERIAL PRIMARY KEY,
    source TEXT NOT NULL,                       -- habit | todo | quest
    item_id TEXT NOT NULL,
    amount INT NOT NULL,
    event_type TEXT NOT NULL DEFAULT 'base',    -- base | critical | token
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS quest_currency_created_at_idx ON quest_currency_events (created_at);
-- Session 76: story arcs — a named date window only. "Progress" is a pure query over existing
-- tables (currency earned + completions in range); no per-arc completion tracker beyond this row.
CREATE TABLE IF NOT EXISTS story_arcs (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL,
    name TEXT NOT NULL,
    start_date DATE NOT NULL,
    end_date DATE NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


async def ensure_schema(conn=None):
    global _SCHEMA_READY
    if _SCHEMA_READY:
        return
    own = conn is None
    if own:
        conn = await pg_conn()
    try:
        await conn.execute(SCHEMA_SQL)
        _SCHEMA_READY = True
    finally:
        if own:
            await conn.close()


# ---------------------------------------------------------------------------
# Bucket assignment (six-bucket taxonomy). Explicit bucket wins; else a light keyword
# inference; habits default to "Habits", todos to "unsorted" — same taxonomy as the
# Session-57 momentum crosswalk, kept simple per the scoping note.
# ---------------------------------------------------------------------------
def infer_bucket(text: str, default: str) -> str:
    t = (text or "").lower()
    if any(w in t for w in ("morning routine", "evening routine", "bedtime", "breakfast",
                            "lunch", "dinner", "tidy", "clean", "skincare", "make bed", "supplements", "laundry")):
        return "Maintenance"
    if any(w in t for w in ("exercise", "yoga", "weights", "handstand", "splits", "water",
                            "workout", "run", "walk", "stretch", "movement", "gym", "read")):
        return "Habits"
    if any(w in t for w in ("career", "job", "resume", "linkedin", "work", "invoice", "email")):
        return "Work"
    if any(w in t for w in ("dark and wild", "tdaw", "witch", "shopify", "shop", "adsense",
                            "substack", "blog", "brand", "website", "content")):
        return "Business"
    if any(w in t for w in ("quest", "sidequest", "trial class", "adventure")):
        return "Sidequests"
    if any(w in t for w in ("novel", "fanfic", "writing", "paint", "craft", "relax", "read", "creative")):
        return "Creative/Relax"
    return default


def _norm_bucket(bucket, text, default):
    if bucket and bucket in BUCKETS:
        return bucket
    return infer_bucket(text, default)


async def _record_momentum(conn, item_id, source, bucket):
    """One qualifying completion -> one momentum_events row. Never raises into the caller."""
    try:
        await conn.execute(
            "INSERT INTO momentum_events (item_id, source, bucket) VALUES ($1,$2,$3)",
            str(item_id), source, bucket if bucket in BUCKETS else "unsorted")
    except Exception as e:
        logger.error(f"native momentum write failed ({source}/{item_id}): {e}")


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------
async def create_habit(name, field_type="tickbox", frequency_kind="weekdays",
                       frequency_days=None, frequency_count=None, bucket=None,
                       streak_rule="net_positive", streak_min_taps=None,
                       checklist=None, user_id=None,
                       streak_count=0, habitica_source_id=None):
    """Create a native habit. Returns the new row id, or None on invalid input.
    streak_count seeds the visible streak (Session 75 Habitica import carries the existing streak
    over so momentum isn't lost); when >0, last_streak_date is set to today so it reads as a live
    streak that a tap tomorrow continues. habitica_source_id records import provenance."""
    name = (name or "").strip()
    if not name:
        return None
    if field_type not in FIELD_TYPES:
        field_type = "tickbox"
    if frequency_kind not in FREQ_KINDS:
        frequency_kind = "weekdays"
    if streak_rule not in ("net_positive", "min_taps"):
        streak_rule = "net_positive"
    if streak_rule == "min_taps" and not streak_min_taps:
        streak_min_taps = 1
    # sensible frequency defaults
    if frequency_kind == "weekdays" and not frequency_days:
        frequency_days = [0, 1, 2, 3, 4, 5, 6]   # every day
    if frequency_kind == "count" and not frequency_count:
        frequency_count = 3
    try:
        streak_count = max(0, int(streak_count or 0))
    except (TypeError, ValueError):
        streak_count = 0
    last_streak_date = today_local() if streak_count > 0 else None
    bucket = _norm_bucket(bucket, name, "Habits")
    uid = user_id if user_id is not None else DEFAULT_USER_ID
    conn = await pg_conn()
    try:
        await ensure_schema(conn)
        hid = await conn.fetchval(
            """INSERT INTO habits (user_id, name, field_type, frequency_kind, frequency_days,
                   frequency_count, bucket, streak_rule, streak_min_taps,
                   streak_count, streak_best, last_streak_date, habitica_source_id)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$10,$11,$12) RETURNING id""",
            uid, name, field_type, frequency_kind, frequency_days, frequency_count,
            bucket, streak_rule, streak_min_taps,
            streak_count, last_streak_date, habitica_source_id)
        if field_type == "checklist" and checklist:
            for i, item in enumerate(checklist):
                item = (item or "").strip()
                if item:
                    await conn.execute(
                        "INSERT INTO habit_checklist (habit_id, text, position) VALUES ($1,$2,$3)",
                        hid, item, i)
        return hid
    finally:
        await conn.close()


async def create_todo(text, kind="tickbox", priority="med", due_date=None,
                      bucket=None, checklist=None, user_id=None, habitica_source_id=None,
                      is_quest=False):
    """Create a native todo. due_date may be a date or an ISO 'YYYY-MM-DD' string. Returns id.
    habitica_source_id records Session-75 import provenance (idempotency key). is_quest (S78) flags
    it a quest → completion awards at the quest currency tier."""
    text = (text or "").strip()
    if not text:
        return None
    if kind not in ("tickbox", "checklist"):
        kind = "tickbox"
    if priority not in PRIORITIES:
        priority = "med"
    if isinstance(due_date, str) and due_date.strip():
        try:
            due_date = date.fromisoformat(due_date.strip()[:10])
        except ValueError:
            due_date = None
    elif isinstance(due_date, str):
        due_date = None
    bucket = _norm_bucket(bucket, text, "unsorted")
    uid = user_id if user_id is not None else DEFAULT_USER_ID
    conn = await pg_conn()
    try:
        await ensure_schema(conn)
        tid = await conn.fetchval(
            """INSERT INTO todos (user_id, text, kind, priority, due_date, bucket, habitica_source_id, is_quest)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8) RETURNING id""",
            uid, text, kind, priority, due_date, bucket, habitica_source_id, bool(is_quest))
        if kind == "checklist" and checklist:
            for i, item in enumerate(checklist):
                item = (item or "").strip()
                if item:
                    await conn.execute(
                        "INSERT INTO todo_checklist (todo_id, text, position) VALUES ($1,$2,$3)",
                        tid, item, i)
        return tid
    finally:
        await conn.close()


# ---------------------------------------------------------------------------
# Session 76 — the single currency economy. _award_currency() is the ONE award path: native's
# habit-tap/todo-complete calls it on an existing connection (coincident with the momentum write);
# the dashboard Quest toggle calls award_currency() (opens its own conn). No parallel logic.
# ---------------------------------------------------------------------------
async def _award_currency(conn, source, item_id, base=None):
    """Award currency for one completion on an existing conn. Rolls a critical hit
    (CURRENCY_CRIT_CHANCE -> 2-3x, event_type='critical'), else a 'base' award. Never raises into
    the caller — a currency miss must not break a real completion."""
    try:
        base = CURRENCY_BASE.get(source, 1) if base is None else int(base)
        etype, amount = "base", base
        if random.random() < CURRENCY_CRIT_CHANCE:
            etype, amount = "critical", base * random.choice(CURRENCY_CRIT_MULTIPLIERS)
        await conn.execute(
            "INSERT INTO quest_currency_events (source, item_id, amount, event_type) VALUES ($1,$2,$3,$4)",
            source, str(item_id), amount, etype)
        return {"amount": amount, "event_type": etype, "base": base}
    except Exception as e:
        logger.error(f"_award_currency error ({source}/{item_id}): {e}")
        return None


async def award_currency(source, item_id, base=None):
    """Connection-opening wrapper for callers that don't already hold one (the Quest toggle)."""
    conn = await pg_conn()
    try:
        await ensure_schema(conn)
        return await _award_currency(conn, source, item_id, base)
    finally:
        await conn.close()


async def currency_summary(conn=None, recent=8):
    """Running balance (sum of all events) + critical-hit count + the most recent events, for the
    dashboard card. Read-only."""
    own = conn is None
    if own:
        conn = await pg_conn()
    try:
        await ensure_schema(conn)
        bal = await conn.fetchval("SELECT COALESCE(SUM(amount),0) FROM quest_currency_events") or 0
        crit = await conn.fetchval(
            "SELECT COUNT(*) FROM quest_currency_events WHERE event_type='critical'") or 0
        rows = await conn.fetch(
            "SELECT source, amount, event_type, "
            "to_char(created_at AT TIME ZONE $2,'YYYY-MM-DD HH24:MI') ts "
            "FROM quest_currency_events ORDER BY id DESC LIMIT $1", int(recent), str(LOCAL_TZ))
        return {"balance": int(bal), "critical_hits": int(crit),
                "recent": [{"source": r["source"], "amount": r["amount"],
                            "event_type": r["event_type"], "ts": r["ts"]} for r in rows]}
    finally:
        if own:
            await conn.close()


# ---------------------------------------------------------------------------
# Session 76 — story arcs. An arc is just a named date window; "progress" is a pure query over
# tables that already exist (currency earned + completions in range). No per-arc tracker.
# ---------------------------------------------------------------------------
async def start_arc(name, days=ARC_DEFAULT_DAYS, start=None, user_id=None):
    name = (name or "").strip()
    if not name:
        return None
    uid = user_id if user_id is not None else DEFAULT_USER_ID
    if isinstance(start, str) and start.strip():
        try:
            start = date.fromisoformat(start.strip()[:10])
        except ValueError:
            start = None
    if not isinstance(start, date):
        start = today_local()
    try:
        days = max(1, int(days))
    except (TypeError, ValueError):
        days = ARC_DEFAULT_DAYS
    end = start + timedelta(days=days - 1)
    conn = await pg_conn()
    try:
        await ensure_schema(conn)
        return await conn.fetchval(
            "INSERT INTO story_arcs (user_id, name, start_date, end_date) VALUES ($1,$2,$3,$4) RETURNING id",
            uid, name, start, end)
    finally:
        await conn.close()


async def active_arc(user_id=None, conn=None):
    """The arc whose window includes today (else the most recent arc), with progress computed live:
    currency earned in the window (the headline number) + completion count. None if no arcs yet."""
    own = conn is None
    if own:
        conn = await pg_conn()
    try:
        await ensure_schema(conn)
        uid = user_id if user_id is not None else DEFAULT_USER_ID
        d = today_local()
        row = await conn.fetchrow(
            "SELECT * FROM story_arcs WHERE user_id=$1 AND start_date<=$2 AND end_date>=$2 "
            "ORDER BY id DESC LIMIT 1", uid, d)
        if not row:
            row = await conn.fetchrow(
                "SELECT * FROM story_arcs WHERE user_id=$1 ORDER BY id DESC LIMIT 1", uid)
        if not row:
            return None
        s, e = row["start_date"], row["end_date"]
        tz = str(LOCAL_TZ)
        earned = await conn.fetchval(
            "SELECT COALESCE(SUM(amount),0) FROM quest_currency_events "
            "WHERE (created_at AT TIME ZONE $3)::date BETWEEN $1 AND $2", s, e, tz) or 0
        comps = await conn.fetchval(
            "SELECT COUNT(*) FROM momentum_events "
            "WHERE (completed_at AT TIME ZONE $3)::date BETWEEN $1 AND $2", s, e, tz) or 0
        return {"id": row["id"], "name": row["name"],
                "start_date": s.isoformat(), "end_date": e.isoformat(),
                "active": s <= d <= e, "days_total": (e - s).days + 1,
                "day_index": ((min(d, e) - s).days + 1) if d >= s else 0,
                "currency_earned": int(earned), "completions": int(comps)}
    finally:
        if own:
            await conn.close()


# ---------------------------------------------------------------------------
# Session 75 — one-time Habitica import. READ-ONLY against Habitica: the caller fetches
# GET /tasks/user itself and passes the raw task list in here; this maps each task and writes
# native records through create_habit/create_todo (NO direct-SQL bypass), idempotent on
# habitica_source_id (skip anything already imported). Nothing in here calls Habitica.
# ---------------------------------------------------------------------------
# Habitica daily.repeat keys -> native weekday index (0=Mon .. 6=Sun).
_HABITICA_REPEAT_TO_WD = {"m": 0, "t": 1, "w": 2, "th": 3, "f": 4, "s": 5, "su": 6}


def _map_habit_field_type(task):
    """Habitica habit up/down flags -> native field type."""
    up, down = bool(task.get("up")), bool(task.get("down"))
    if up and down:
        return "both"
    if up:
        return "positive"
    if down:
        return "negative"
    return "tickbox"


def _checklist_texts(task):
    return [c.get("text", "").strip() for c in (task.get("checklist") or []) if c.get("text", "").strip()]


def _map_daily_frequency(task):
    """A Habitica daily's schedule -> native (frequency_kind, frequency_days, frequency_count, note).
    note is set when the mapping is approximate (native can't express the exact Habitica rule)."""
    freq = task.get("frequency", "weekly")
    if freq == "weekly":
        rep = task.get("repeat") or {}
        days = sorted({_HABITICA_REPEAT_TO_WD[k] for k, on in rep.items()
                       if on and k in _HABITICA_REPEAT_TO_WD})
        return "weekdays", (days or [0, 1, 2, 3, 4, 5, 6]), None, None
    if freq == "daily":
        every = int(task.get("everyX") or 1)
        if every <= 1:
            return "weekdays", [0, 1, 2, 3, 4, 5, 6], None, None
        cnt = max(1, round(7 / every))     # "every N days" has no native rule -> N-times/week
        return "count", None, cnt, f"Habitica 'every {every} days' approximated as {cnt}x/week"
    if freq == "monthly":
        dom = task.get("daysOfMonth") or []
        if dom:
            return "monthly", list(dom), None, None
        day = 1
        try:
            day = date.fromisoformat((task.get("startDate") or "")[:10]).day
        except Exception:
            pass
        return "monthly", [day], None, "Habitica week-of-month rule approximated to a day-of-month"
    return "weekdays", [0, 1, 2, 3, 4, 5, 6], None, f"unknown Habitica frequency '{freq}' -> every day"


def _map_priority(p):
    try:
        p = float(p)
    except (TypeError, ValueError):
        return "med"
    if p >= 2:
        return "high"
    if p >= 1.5:
        return "med"
    return "low"


async def import_habitica_tasks(tasks, user_id=None):
    """Import a Habitica /tasks/user list into native. Idempotent on habitica_source_id. Maps:
    habit->habit (up/down->field type, streak carried over), daily->habit (weekday/monthly/count
    frequency, streak carried over), todo->todo (due date carried over), any checklist->checklist
    field type. Skips rewards (no native equivalent). Returns a report dict. Never calls Habitica."""
    uid = user_id if user_id is not None else DEFAULT_USER_ID
    report = {"habits": 0, "dailies": 0, "todos": 0, "skipped_existing": 0,
              "skipped_unsupported": [], "notes": [], "items": []}
    conn = await pg_conn()
    try:
        await ensure_schema(conn)
        for task in tasks:
            ttype = task.get("type")
            sid = task.get("id") or task.get("_id")
            text = (task.get("text") or "").strip()
            if not sid or not text:
                report["skipped_unsupported"].append({"id": sid, "reason": "missing id or text", "type": ttype})
                continue
            if ttype == "reward":
                report["skipped_unsupported"].append({"text": text, "reason": "reward (no native equivalent)"})
                continue
            table = "todos" if ttype == "todo" else "habits"
            if await conn.fetchval(f"SELECT id FROM {table} WHERE habitica_source_id=$1", sid):
                report["skipped_existing"] += 1
                continue
            checklist = _checklist_texts(task)
            if ttype == "habit":
                ft = "checklist" if checklist else _map_habit_field_type(task)
                streak = int(task.get("streak") or task.get("counterUp") or 0)
                nid = await create_habit(text, field_type=ft, frequency_kind="weekdays",
                                         streak_count=streak, checklist=checklist or None,
                                         user_id=uid, habitica_source_id=sid)
                report["habits"] += 1
                report["items"].append({"kind": "habit", "id": nid, "name": text,
                                        "field_type": ft, "streak": streak})
            elif ttype == "daily":
                fk, fdays, fcount, note = _map_daily_frequency(task)
                ft = "checklist" if checklist else "tickbox"
                streak = int(task.get("streak") or 0)
                nid = await create_habit(text, field_type=ft, frequency_kind=fk,
                                         frequency_days=fdays, frequency_count=fcount,
                                         streak_count=streak, checklist=checklist or None,
                                         user_id=uid, habitica_source_id=sid)
                report["dailies"] += 1
                if note:
                    report["notes"].append(f"{text}: {note}")
                report["items"].append({"kind": "daily", "id": nid, "name": text,
                                        "field_type": ft, "freq": fk, "streak": streak})
            elif ttype == "todo":
                kind = "checklist" if checklist else "tickbox"
                due = (task.get("date") or "") or None
                nid = await create_todo(text, kind=kind,
                                        priority=_map_priority(task.get("priority")),
                                        due_date=due, checklist=checklist or None,
                                        user_id=uid, habitica_source_id=sid)
                report["todos"] += 1
                report["items"].append({"kind": "todo", "id": nid, "name": text,
                                        "due_date": (due or "")[:10] or None})
            else:
                report["skipped_unsupported"].append({"text": text, "reason": f"unknown type '{ttype}'"})
    finally:
        await conn.close()
    return report


# ---------------------------------------------------------------------------
# Streak logic — tap-time crediting.
# A day "qualifies" per the habit's field type + streak rule (recomputed from that day's
# taps on every tap). When today first qualifies: streak becomes streak+1 if yesterday was
# credited, else 1. A same-day down tap that removes qualification steps the streak back by
# one. Calendar-accurate crediting of a clean no-tap day (relevant to negative/count habits)
# needs the daily rollover job that arrives with Phase 3's leveling — documented, not faked.
# ---------------------------------------------------------------------------
async def _today_taps(conn, habit_id, d):
    rows = await conn.fetch(
        "SELECT direction, COUNT(*) n FROM habit_taps WHERE habit_id=$1 AND tap_date=$2 GROUP BY direction",
        habit_id, d)
    agg = {"up": 0, "down": 0, "check": 0, "value": 0.0}
    for r in rows:
        agg[r["direction"]] = r["n"]
    # S92: sum of logged values today (value-entry habits)
    v = await conn.fetchval(
        "SELECT COALESCE(SUM(value),0) FROM habit_taps WHERE habit_id=$1 AND tap_date=$2", habit_id, d)
    agg["value"] = float(v or 0)
    return agg


async def _checklist_all_done(conn, habit_id, d):
    total = await conn.fetchval("SELECT COUNT(*) FROM habit_checklist WHERE habit_id=$1", habit_id)
    if not total:
        return False
    done = await conn.fetchval(
        """SELECT COUNT(*) FROM habit_checklist hc
           JOIN habit_checklist_state s ON s.checklist_id=hc.id AND s.state_date=$2
           WHERE hc.habit_id=$1 AND s.done=TRUE""", habit_id, d)
    return done >= total


async def _qualifies_today(conn, habit, d):
    ft = habit["field_type"]
    agg = await _today_taps(conn, habit["id"], d)
    if ft == "tickbox":
        return agg["check"] > 0
    if ft == "checklist":
        return await _checklist_all_done(conn, habit["id"], d)
    if ft == "positive":
        return agg["up"] > 0
    if ft == "negative":
        # a clean day is credited by the daily rollover (Phase 3); at tap time a down tap
        # only ever breaks it, so "qualifies" is False whenever a down tap exists today.
        return agg["down"] == 0 and (agg["up"] > 0)  # up used as an explicit "held it" tap
    # both
    if habit["streak_rule"] == "min_taps":
        return agg["up"] >= (habit["streak_min_taps"] or 1)
    return (agg["up"] - agg["down"]) > 0


async def _reconcile_streak(conn, habit, d):
    """Update streak_count/best/last_streak_date from today's qualification. Returns new streak."""
    qualifies = await _qualifies_today(conn, habit, d)
    last = habit["last_streak_date"]
    streak = habit["streak_count"]
    best = habit["streak_best"]
    y = d - timedelta(days=1)
    if qualifies:
        if last == d:
            pass  # already credited today
        elif last == y:
            streak += 1
            last = d
        else:
            streak = 1
            last = d
    else:
        if last == d:
            # was credited earlier today, a later tap removed qualification
            streak = max(0, streak - 1)
            last = y if streak > 0 else None
    best = max(best, streak)
    await conn.execute(
        "UPDATE habits SET streak_count=$1, streak_best=$2, last_streak_date=$3 WHERE id=$4",
        streak, best, last, habit["id"])
    return streak


# ---------------------------------------------------------------------------
# Tap / complete
# ---------------------------------------------------------------------------
async def tap_habit(habit_id, direction="up", value=None):
    """Record a habit tap and update its streak. direction: up|down|check. `value` (S92) is an
    optional logged number (reps/seconds/ml) stored on the tap for value-entry habits.
    Returns {ok, habit, direction, streak, counted, value} or {ok:False,error}."""
    if direction not in ("up", "down", "check"):
        direction = "up"
    if value is not None:
        try:
            value = float(value)
        except (TypeError, ValueError):
            value = None
    conn = await pg_conn()
    try:
        await ensure_schema(conn)
        habit = await conn.fetchrow("SELECT * FROM habits WHERE id=$1 AND active=TRUE", habit_id)
        if not habit:
            return {"ok": False, "error": "habit not found"}
        d = today_local()
        await conn.execute(
            "INSERT INTO habit_taps (habit_id, direction, tap_date, value) VALUES ($1,$2,$3,$4)",
            habit_id, direction, d, value)
        streak = await _reconcile_streak(conn, habit, d)
        counted = direction in ("up", "check")
        if counted:
            await _record_momentum(conn, f"habit:{habit_id}", "native_habit", habit["bucket"])
            # S92: XP/currency retired — no award on completion. Streaks + momentum unchanged.
        return {"ok": True, "habit": habit["name"], "direction": direction, "value": value,
                "streak": streak, "bucket": habit["bucket"], "counted": counted}
    finally:
        await conn.close()


async def set_habit_checklist_item(checklist_id, done=True):
    """Tick/untick one checklist item of a checklist habit for today; re-reconciles the streak
    (and counts a momentum event only when the whole checklist becomes complete)."""
    conn = await pg_conn()
    try:
        await ensure_schema(conn)
        row = await conn.fetchrow(
            "SELECT hc.habit_id FROM habit_checklist hc WHERE hc.id=$1", checklist_id)
        if not row:
            return {"ok": False, "error": "checklist item not found"}
        d = today_local()
        await conn.execute(
            """INSERT INTO habit_checklist_state (checklist_id, state_date, done)
               VALUES ($1,$2,$3)
               ON CONFLICT (checklist_id, state_date) DO UPDATE SET done=EXCLUDED.done""",
            checklist_id, d, bool(done))
        habit = await conn.fetchrow("SELECT * FROM habits WHERE id=$1", row["habit_id"])
        was_complete = habit["last_streak_date"] == d
        streak = await _reconcile_streak(conn, habit, d)
        now_complete = await _checklist_all_done(conn, habit["id"], d)
        if now_complete and not was_complete:
            await _record_momentum(conn, f"habit:{habit['id']}", "native_habit", habit["bucket"])
            # S92: XP/currency retired — no award on completion.
        return {"ok": True, "habit": habit["name"], "streak": streak, "complete": now_complete}
    finally:
        await conn.close()


async def complete_todo(todo_id, done=True):
    """Mark a todo done/undone. Counts a momentum event on the done transition."""
    conn = await pg_conn()
    try:
        await ensure_schema(conn)
        todo = await conn.fetchrow("SELECT * FROM todos WHERE id=$1", todo_id)
        if not todo:
            return {"ok": False, "error": "todo not found"}
        was_done = todo["done"]
        if done:
            await conn.execute(
                "UPDATE todos SET done=TRUE, completed_at=now() WHERE id=$1", todo_id)
            if not was_done:
                await _record_momentum(conn, f"todo:{todo_id}", "native_todo", todo["bucket"])
                # S92: XP/currency retired — no award on completion (momentum + streaks unchanged).
        else:
            await conn.execute(
                "UPDATE todos SET done=FALSE, completed_at=NULL WHERE id=$1", todo_id)
        return {"ok": True, "text": todo["text"], "done": bool(done)}
    finally:
        await conn.close()


async def set_todo_checklist_item(item_id, done=True):
    """Tick/untick one item of a checklist todo. S77: all items ticked -> the todo completes
    (partial ticks do not), mirroring checklist habits — and it counts a momentum event + currency
    award on the done transition, exactly like complete_todo. Un-ticking below full reverts it."""
    conn = await pg_conn()
    try:
        await ensure_schema(conn)
        row = await conn.fetchrow("SELECT todo_id FROM todo_checklist WHERE id=$1", item_id)
        if not row:
            return {"ok": False, "error": "checklist item not found"}
        tid = row["todo_id"]
        await conn.execute("UPDATE todo_checklist SET done=$1 WHERE id=$2", bool(done), item_id)
        total = await conn.fetchval("SELECT COUNT(*) FROM todo_checklist WHERE todo_id=$1", tid)
        ndone = await conn.fetchval("SELECT COUNT(*) FROM todo_checklist WHERE todo_id=$1 AND done=TRUE", tid)
        todo = await conn.fetchrow("SELECT done, bucket, is_quest FROM todos WHERE id=$1", tid)
        was_done = bool(todo["done"]) if todo else False
        complete = total > 0 and ndone >= total
        if complete and not was_done:
            await conn.execute("UPDATE todos SET done=TRUE, completed_at=now() WHERE id=$1", tid)
            await _record_momentum(conn, f"todo:{tid}", "native_todo", todo["bucket"])
            # S92: XP/currency retired — no award on completion.
        elif (not complete) and was_done:
            await conn.execute("UPDATE todos SET done=FALSE, completed_at=NULL WHERE id=$1", tid)
        return {"ok": True, "complete": complete, "done_count": ndone, "total": total}
    finally:
        await conn.close()


async def set_todo_quest(todo_id, is_quest=True):
    """S78: flag/unflag an existing todo as a quest (the per-row 'edit' path — there's no todo edit
    modal). Only affects the currency tier awarded on its next completion."""
    conn = await pg_conn()
    try:
        await ensure_schema(conn)
        status = await conn.execute("UPDATE todos SET is_quest=$1 WHERE id=$2", bool(is_quest), todo_id)
        if status.split()[-1] == "0":
            return {"ok": False, "error": "todo not found"}
        return {"ok": True, "id": todo_id, "is_quest": bool(is_quest)}
    finally:
        await conn.close()


async def add_todo_checklist_item(todo_id, text):
    """Add a checklist item to an existing checklist todo (S77 — mirrors add_habit_checklist_item;
    Session 62 built the todo_checklist model + tick endpoint but never an add path). Returns the
    new item id, or an error dict."""
    text = (text or "").strip()
    if not text:
        return {"ok": False, "error": "empty item text"}
    conn = await pg_conn()
    try:
        await ensure_schema(conn)
        t = await conn.fetchrow("SELECT id FROM todos WHERE id=$1", todo_id)
        if not t:
            return {"ok": False, "error": "todo not found"}
        pos = await conn.fetchval(
            "SELECT COALESCE(MAX(position),-1)+1 FROM todo_checklist WHERE todo_id=$1", todo_id)
        cid = await conn.fetchval(
            "INSERT INTO todo_checklist (todo_id, text, position) VALUES ($1,$2,$3) RETURNING id",
            todo_id, text, pos)
        return {"ok": True, "id": cid, "text": text}
    finally:
        await conn.close()


async def add_habit_checklist_item(habit_id, text):
    """Add a checklist item to an existing checklist habit (S64 — Session 62 only supported items
    set at create time). Returns the new item id, or None on invalid input."""
    text = (text or "").strip()
    if not text:
        return {"ok": False, "error": "empty item text"}
    conn = await pg_conn()
    try:
        await ensure_schema(conn)
        h = await conn.fetchrow("SELECT id FROM habits WHERE id=$1", habit_id)
        if not h:
            return {"ok": False, "error": "habit not found"}
        pos = await conn.fetchval(
            "SELECT COALESCE(MAX(position),-1)+1 FROM habit_checklist WHERE habit_id=$1", habit_id)
        cid = await conn.fetchval(
            "INSERT INTO habit_checklist (habit_id, text, position) VALUES ($1,$2,$3) RETURNING id",
            habit_id, text, pos)
        return {"ok": True, "id": cid, "text": text}
    finally:
        await conn.close()


# ---------------------------------------------------------------------------
# Delete = SOFT-DELETE / ARCHIVE (decided S64). Habits set active=FALSE; todos set archived=TRUE.
# Rationale: preserves habit_taps + momentum_events history so Phase 3's leveling curve can be
# calibrated against real completion volume, and it's reversible. list_habits/list_todos already
# filter these out, so an archived item vanishes from every surface immediately.
# ---------------------------------------------------------------------------
async def archive_habit(habit_id):
    conn = await pg_conn()
    try:
        await ensure_schema(conn)
        row = await conn.fetchrow("SELECT name FROM habits WHERE id=$1 AND active=TRUE", habit_id)
        if not row:
            return {"ok": False, "error": "habit not found or already archived"}
        await conn.execute("UPDATE habits SET active=FALSE WHERE id=$1", habit_id)
        return {"ok": True, "archived": "habit", "name": row["name"]}
    finally:
        await conn.close()


async def archive_todo(todo_id):
    conn = await pg_conn()
    try:
        await ensure_schema(conn)
        row = await conn.fetchrow("SELECT text FROM todos WHERE id=$1 AND archived=FALSE", todo_id)
        if not row:
            return {"ok": False, "error": "todo not found or already archived"}
        await conn.execute("UPDATE todos SET archived=TRUE WHERE id=$1", todo_id)
        return {"ok": True, "archived": "todo", "text": row["text"]}
    finally:
        await conn.close()


# ---------------------------------------------------------------------------
# Read / display
# ---------------------------------------------------------------------------
_WD = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def _freq_label(h):
    if h["frequency_kind"] == "count":
        return f"{h['frequency_count']}x / week"
    if h["frequency_kind"] == "monthly":
        days = h["frequency_days"] or []
        return "monthly" + (f" (day {', '.join(map(str, days))})" if days else "")
    days = h["frequency_days"] or []
    if len(days) == 7:
        return "every day"
    if days == [0, 1, 2, 3, 4]:
        return "weekdays"
    return " ".join(_WD[d] for d in days if 0 <= d < 7) or "weekly"


def _field_label(ft):
    return {"positive": "positive", "negative": "negative", "both": "positive + negative",
            "tickbox": "tickbox", "checklist": "checklist"}.get(ft, ft)


def _due_today(h, d):
    if h["frequency_kind"] == "count":
        return True   # flexible count — available any day
    if h["frequency_kind"] == "monthly":
        return d.day in (h["frequency_days"] or [])
    return d.weekday() in (h["frequency_days"] or list(range(7)))


async def list_habits(user_id=None, conn=None):
    own = conn is None
    if own:
        conn = await pg_conn()
    try:
        await ensure_schema(conn)
        uid = user_id if user_id is not None else DEFAULT_USER_ID
        rows = await conn.fetch(
            "SELECT * FROM habits WHERE user_id=$1 AND active=TRUE ORDER BY id", uid)
        d = today_local()
        out = []
        for h in rows:
            agg = await _today_taps(conn, h["id"], d)
            item = {
                "id": h["id"], "name": h["name"], "field_type": h["field_type"],
                "field_label": _field_label(h["field_type"]),
                "frequency_kind": h["frequency_kind"], "frequency_label": _freq_label(h),
                "bucket": h["bucket"], "streak": h["streak_count"], "streak_best": h["streak_best"],
                "streak_rule": h["streak_rule"], "streak_min_taps": h["streak_min_taps"],
                "due_today": _due_today(h, d),
                "done_today": (h["last_streak_date"] == d),
                "today_up": agg["up"], "today_down": agg["down"], "today_check": agg["check"],
                "today_value": agg["value"],                                   # S92
                "target_value": float(h["target_value"]) if h["target_value"] is not None else None,  # S92
                "unit": h["unit"],                                             # S92
                "checklist": [],
            }
            if h["field_type"] == "checklist":
                citems = await conn.fetch(
                    """SELECT hc.id, hc.text,
                              COALESCE(s.done, FALSE) AS done
                       FROM habit_checklist hc
                       LEFT JOIN habit_checklist_state s ON s.checklist_id=hc.id AND s.state_date=$2
                       WHERE hc.habit_id=$1 ORDER BY hc.position, hc.id""", h["id"], d)
                item["checklist"] = [{"id": c["id"], "text": c["text"], "done": c["done"]} for c in citems]
            out.append(item)
        return out
    finally:
        if own:
            await conn.close()


async def list_todos(user_id=None, include_done=True, conn=None):
    own = conn is None
    if own:
        conn = await pg_conn()
    try:
        await ensure_schema(conn)
        uid = user_id if user_id is not None else DEFAULT_USER_ID
        rows = await conn.fetch(
            "SELECT * FROM todos WHERE user_id=$1 AND archived=FALSE ORDER BY done, due_date NULLS LAST, id", uid)
        d = today_local()
        out = []
        for t in rows:
            if t["done"] and not include_done:
                continue
            overdue = bool(t["due_date"] and (not t["done"]) and t["due_date"] < d)
            item = {
                "id": t["id"], "text": t["text"], "kind": t["kind"], "priority": t["priority"],
                "bucket": t["bucket"], "done": t["done"], "is_quest": bool(t["is_quest"]),
                "due_date": t["due_date"].isoformat() if t["due_date"] else None,
                "overdue": overdue, "checklist": [],
            }
            if t["kind"] == "checklist":
                citems = await conn.fetch(
                    "SELECT id, text, done FROM todo_checklist WHERE todo_id=$1 ORDER BY position, id", t["id"])
                item["checklist"] = [{"id": c["id"], "text": c["text"], "done": c["done"]} for c in citems]
            out.append(item)
        return out
    finally:
        if own:
            await conn.close()


# ---------------------------------------------------------------------------
# Level / bucket stats — PLACEHOLDER math (Phase 3 replaces the curve; Phase 1 only wires
# the raw counters). Level = floor(total completions / 10) + 1. Bucket "ring" percent is a
# provisional progress-to-next-10 within each bucket. "Since last level" = simple real stats.
# ---------------------------------------------------------------------------
LEVEL_STEP = 10


async def level_stats(user_id=None, conn=None):
    own = conn is None
    if own:
        conn = await pg_conn()
    try:
        await ensure_schema(conn)
        rows = await conn.fetch(
            "SELECT bucket, COUNT(*) n FROM momentum_events WHERE source IN ('native_habit','native_todo') GROUP BY bucket")
        counts = {b: 0 for b in BUCKETS}
        total = 0
        for r in rows:
            b = r["bucket"] if r["bucket"] in counts else "unsorted"
            counts[b] += r["n"]
            total += r["n"]
        level = total // LEVEL_STEP + 1
        into = total % LEVEL_STEP
        buckets = []
        for b in BUCKETS[:6]:
            c = counts[b]
            buckets.append({"bucket": b, "count": c,
                            "level": c // LEVEL_STEP + 1,
                            "pct": int(round((c % LEVEL_STEP) / LEVEL_STEP * 100))})
        # since-last-level: real stats
        uid = user_id if user_id is not None else DEFAULT_USER_ID
        longest = await conn.fetchval(
            "SELECT COALESCE(MAX(streak_best),0) FROM habits WHERE user_id=$1", uid) or 0
        days_active = await conn.fetchval(
            "SELECT COUNT(DISTINCT tap_date) FROM habit_taps") or 0
        todos_done = await conn.fetchval(
            "SELECT COUNT(*) FROM todos WHERE user_id=$1 AND done=TRUE", uid) or 0
        return {
            "level": level, "total_completions": total,
            "into_level": into, "level_step": LEVEL_STEP,
            "pct_to_next": int(round(into / LEVEL_STEP * 100)),
            "buckets": buckets,
            "since": {"longest_streak": longest, "days_active": days_active, "todos_done": todos_done},
            "_placeholder": "level math is a Phase-1 stub (floor(completions/10)+1); Phase 3 replaces the curve",
        }
    finally:
        if own:
            await conn.close()


# ---------------------------------------------------------------------------
# Natural-language routing (deterministic, no LLM) — the shared matcher the Telegram
# pipeline uses. Returns a result dict when native claimed the message, else None so the
# caller falls through to the existing (untouched) Habitica paths. Parallel-run safe:
#   * create: explicit "create habit X" / "add|new todo X" (phrasings Habitica does NOT use).
#   * tick/complete: an explicit completion verb or +/- token that matches ONE native item.
# Ambient signals with no action verb ("drank water") are never claimed here.
# ---------------------------------------------------------------------------
_CREATE_HABIT_RE = re.compile(r'^\s*(?:create|start|track)\s+(?:a\s+)?habit[:\s]+(.+)$', re.I)
_CREATE_TODO_RE = re.compile(r'^\s*(?:create|add|new)\s+(?:a\s+)?todo[:\s]+(.+)$', re.I)
_ACTION_UP = ("did", "done", "finished", "complete", "completed", "ticked", "tick",
              "smashed", "knocked out", "wrapped up", "went to")
_TOKEN_PLUS = ("+", " plus")
_TOKEN_MINUS = ("-", " minus", "skipped")

# --- Native CREATE detection (S66 widening) -------------------------------------------------
# Goal: catch ordinary conversational creation ("add a habit for stretching", "add my exercise
# habit and a todo to call the dentist", "track reading as a habit", "add X to my todos") WITHOUT
# stealing Habitica's own openers (a message OPENING with "new habit"/"create daily"/"add task"/
# "add to habitica"/"remind me to"/"add X to my list" still routes to Habitica, untouched — those
# are handled later in process_message and native runs first only for the phrasings below).
# Fail-closed: creation intent (a create verb + the noun habit/todo) with no parseable name returns
# a needs_name marker so the caller asks, never guesses, never falls silently through to chat.
_CREATE_VERB = r'(?:add|create|make|set\s*up|start|track|log|want\s+to\s+track)'
_FIELD_KW = r'(?:positive\s*\+\s*negative|positive\s+and\s+negative|both|positive|negative|checklist|tickbox)'
# a name stops before " and [a] habit/todo" (the next create clause) or end-of-string, so one
# item's name never swallows the next in "X habit and a todo for Y".
_BOUND = r'(?=\s+and\s+(?:a\s+|an\s+|the\s+|my\s+|another\s+)?(?:new\s+)?(?:daily\s+)?(?:habit|to-?do)\b|\s*$)'
# habit, name AFTER the noun: "add a habit for/to/about/called X", "habit: X"
_H_AFTER = re.compile(r'\bhabit\b\s*(?::|\bfor\b|\bto\b|\babout\b|\bcalled\b|\bnamed\b|\bof\b|\bon\b)\s*(?P<n>.+?)' + _BOUND, re.I)
# habit, name BEFORE the noun: "add my exercise habit", "create a stretching habit"
_H_BEFORE = re.compile(r'\b' + _CREATE_VERB + r'\s+(?:a\s+|an\s+|the\s+|my\s+|another\s+)?(?P<n>.+?)\s+habit\b', re.I)
# "track/log X as a habit"
_H_ASA = re.compile(r'\b(?:track|log|count)\s+(?P<n>.+?)\s+as\s+(?:a\s+|an\s+)?(?:daily\s+)?habit\b', re.I)
# todo, name after the noun: "add a todo to/for X", "todo: X"
_T_AFTER = re.compile(r'\b(?:to-?do)\b\s*(?::|\bto\b|\bfor\b|\babout\b|\bcalled\b|\bnamed\b)\s*(?P<n>.+?)' + _BOUND, re.I)
# "add X to my todos / to-do list"
_T_TOMY = re.compile(r'\b' + _CREATE_VERB + r'\s+(?P<n>.+?)\s+to\s+(?:my|the)\s+to-?do(?:\s*list)?s?\b', re.I)
# bare creation intent with no name: "add a habit", "create a todo"
_H_INTENT = re.compile(r'\b' + _CREATE_VERB + r'\s+(?:a\s+|an\s+|another\s+)?habit\b', re.I)
_T_INTENT = re.compile(r'\b' + _CREATE_VERB + r'\s+(?:a\s+|an\s+|another\s+)?to-?do\b', re.I)


def _clean_name(s):
    s = (s or "").strip()
    s = re.sub(r'^(?:a|an|the|my|some|to|for)\s+', "", s, flags=re.I)   # leading filler
    s = re.sub(r'\s*(?:please|thanks|thank you)\s*$', "", s, flags=re.I)
    return s.strip(" .,!?:-")


def _extract_field_type(low):
    for k in ("positive + negative", "positive and negative", "both", "positive", "negative", "checklist", "tickbox"):
        if k in low:
            return {"positive + negative": "both", "positive and negative": "both"}.get(k, k)
    return "tickbox"


def detect_native_create(text):
    """Deterministic create detector. Returns:
      {"creations": [ {kind:'habit',name,field_type} | {kind:'todo',text,priority} ], "needs_name": bool}
    when a native creation is intended, else None. Handles single items and 'X and Y' conjunctions.
    Never claims a message that merely OPENS with a Habitica trigger word (those are matched later)."""
    if not text or text.rstrip().endswith("?"):
        return None
    low = text.lower()
    # Don't poach Habitica's own opening commands (S64 gate mirrors these).
    if re.match(r'^\s*(?:new\s+habit|create\s+daily|add\s+task|add\s+to\s+habitica|habit\s+(?:up|down)|scored|remind\s+me\s+to)\b', low):
        return None
    # "add X to my list" (no 'todo' word) is Habitica task-capture — leave it.
    if re.search(r'\bto\s+(?:my|the)\s+list\b', low) and "todo" not in low and "to-do" not in low:
        return None

    # Intent = a create verb somewhere AND the noun. Broad on purpose (Lea's real "add my exercise
    # habit and a todo for X" has the verb and noun non-adjacent); the Habitica guards above already
    # carved off the phrasings that belong to Habitica, and extraction below still fails closed.
    has_create_verb = bool(re.search(r'\b' + _CREATE_VERB + r'\b', low))
    has_colon = bool(re.match(r'^\s*(?:habit|to-?do)\s*:', low))
    has_asa_habit = bool(re.search(r'\bas\s+(?:a\s+|an\s+)?(?:daily\s+)?habit\b', low))
    has_habit_intent = (has_create_verb and "habit" in low) or has_asa_habit or has_colon
    has_todo_intent = (has_create_verb and ("todo" in low or "to-do" in low)) or has_colon
    if not (has_habit_intent or has_todo_intent):
        return None

    creations = []
    seen = set()

    def add_habit(raw):
        name = _clean_name(raw)
        name = re.sub(r'[\s,]*' + _FIELD_KW + r'\s*$', "", name, flags=re.I).strip(" .,!?") or name
        # drop a trailing "... as a [daily]" left by the 'X as a habit' phrasing
        name = re.sub(r'\s+as(?:\s+an?)?(?:\s+daily)?\s*$', "", name, flags=re.I).strip(" .,!?") or name
        if name and name.lower() not in seen and len(name) >= 2:
            seen.add(name.lower())
            creations.append({"kind": "habit", "name": name, "field_type": _extract_field_type(raw.lower())})

    def add_todo(raw):
        text2 = _clean_name(raw)
        pr = "high" if "high" in raw.lower() else ("low" if "low priority" in raw.lower() or " low" in raw.lower() else "med")
        text2 = re.sub(r'[\s,]*(high|medium|med|low)(\s+priority)?\s*$', "", text2, flags=re.I).strip(" .,!?") or text2
        if text2 and text2.lower() not in seen and len(text2) >= 2:
            seen.add(text2.lower())
            creations.append({"kind": "todo", "text": text2, "priority": pr})

    # finditer so multiple items in one message ("X habit and a todo for Y") are all captured;
    # the _BOUND lookahead keeps each name from swallowing the next clause.
    for rx, fn in ((_H_AFTER, add_habit), (_H_ASA, add_habit), (_H_BEFORE, add_habit),
                   (_T_AFTER, add_todo), (_T_TOMY, add_todo)):
        for m in rx.finditer(text):
            fn(m.group("n"))

    return {"creations": creations, "needs_name": len(creations) == 0}
# -------------------------------------------------------------------------------------------


def _match_items(text, items, name_key):
    """Return list of items whose name (>=4 chars, or last word >=4 chars) is a substring."""
    low = text.lower()
    matches = []
    for it in items:
        core = (it[name_key] or "").strip().lower()
        if not core:
            continue
        words = core.split()
        if len(core) >= 4 and core in low:
            matches.append(it)
        elif words and len(words[-1]) >= 4 and words[-1] in low:
            matches.append(it)
    return matches


# S79 Task 4 — conversational list edits: skip/drop/remove an item, with an optional swap-in,
# at ANY time (not just at an anchor). Native list only (habits/todos). Guarded by explicit edit
# verbs so ordinary chat never triggers it; fails closed (returns a no-match marker, never guesses).
_EDIT_REMOVE_RE = re.compile(
    r'\b(?:skip|drop|remove|cancel|delete|scrap|ditch|swap\s+out|take\s+off|get\s+rid\s+of)\b\s+'
    r'(?P<n>.+?)(?=\s*(?:[,;.]|\band\b|\bthen\b|\bswap\s+in\b|\binstead\b|\breplace\b|\badd\s+in\b|$))', re.I)
_EDIT_ADD_RE = re.compile(
    r'\b(?:swap\s+in|instead(?:\s+do)?|replace\s+(?:it\s+)?with|add\s+in|put\s+in|throw\s+in|sub\s+in)\b\s+'
    r'(?:a\s+|the\s+)?(?P<n>.+?)\s*$', re.I)

def _clean_edit_name(s):
    s = (s or "").strip()
    s = re.sub(r'^(?:the|my|a|an|some)\s+', "", s, flags=re.I)
    s = re.sub(r'\s+(?:today|tonight|this\s+(?:morning|afternoon|evening|week)|for\s+(?:today|now))\s*$',
               "", s, flags=re.I)
    # S81 fix: strip a trailing type-word so "delete the gym habit" targets "gym", not "gym habit"
    # (the stray 'habit'/'todo' token was matching any item whose NAME contained it -> false
    # ambiguity, which is why habit/todo delete "didn't work" from the chat box).
    s = re.sub(r'\s+(?:habits?|to-?dos?|tasks?|items?|notes?)\s*$', "", s, flags=re.I)
    return s.strip(" .,!?:-")

def _edit_match(target, items, key):
    """Loose match for an explicit edit ('skip the gym' -> the 'Gym' item): item core is a substring
    either way, or shares a >=3-char word. Looser than _match_items on purpose — she named it."""
    tl = (target or "").strip().lower()
    if not tl:
        return []
    tt = {w for w in re.findall(r'[a-z0-9]+', tl) if len(w) >= 3}
    out = []
    for it in items:
        core = (it[key] or "").strip().lower()
        if not core:
            continue
        ct = {w for w in re.findall(r'[a-z0-9]+', core) if len(w) >= 3}
        if core in tl or tl in core or (tt & ct):
            out.append(it)
    return out


async def route_message(text, user_id=None):
    """Deterministic native router. Returns a dict describing what was done, or None."""
    if not text or text.rstrip().endswith("?"):
        return None
    low = text.lower()

    # --- create ---
    m = _CREATE_HABIT_RE.match(text)
    if m:
        name = m.group(1).strip(" .,!?")
        ft = "tickbox"
        for k in ("positive + negative", "positive and negative", "both", "positive", "negative", "checklist", "tickbox"):
            if k in low:
                ft = {"positive + negative": "both", "positive and negative": "both"}.get(k, k)
                break
        # strip a trailing field-type keyword from the name ("...Meditate positive" -> "Meditate")
        name = re.sub(r'[\s,]*(positive\s*\+\s*negative|positive and negative|both|positive|negative|checklist|tickbox)\s*$',
                      "", name, flags=re.I).strip(" .,!?") or name
        hid = await create_habit(name, field_type=ft, user_id=user_id)
        return {"action": "create_habit", "ok": hid is not None, "name": name, "field_type": ft, "id": hid}
    m = _CREATE_TODO_RE.match(text)
    if m:
        body = m.group(1).strip(" .,!?")
        pr = "high" if "high" in low else ("low" if "low" in low else "med")
        # strip a trailing priority keyword from the text ("...Call plumber high" -> "Call plumber")
        body = re.sub(r'[\s,]*(high|medium|med|low)(\s+priority)?\s*$', "", body, flags=re.I).strip(" .,!?") or body
        tid = await create_todo(body, priority=pr, user_id=user_id)
        return {"action": "create_todo", "ok": tid is not None, "text": body, "id": tid}

    # --- list edit: skip/drop/remove (+ optional swap-in) — S79 Task 4, native list only ---
    rem_m = _EDIT_REMOVE_RE.search(text)
    if rem_m:
        uid = user_id if user_id is not None else DEFAULT_USER_ID
        conn = await pg_conn()
        try:
            await ensure_schema(conn)
            habits = await list_habits(uid, conn=conn)
            todos = await list_todos(uid, include_done=False, conn=conn)
        finally:
            await conn.close()
        target = _clean_edit_name(rem_m.group("n"))
        tmatch = _edit_match(target, todos, "text")
        hmatch = _edit_match(target, habits, "name")
        if len(tmatch) + len(hmatch) > 1:
            return {"action": "ambiguous",
                    "candidates": [t["text"] for t in tmatch] + [h["name"] for h in hmatch]}
        removed = None
        if tmatch:                                  # prefer the to-do list; habits are recurring
            await archive_todo(tmatch[0]["id"]); removed = ["todo", tmatch[0]["text"]]
        elif hmatch:
            await archive_habit(hmatch[0]["id"]); removed = ["habit", hmatch[0]["name"]]
        added = None
        add_m = _EDIT_ADD_RE.search(text)
        if add_m:
            newname = _clean_edit_name(add_m.group("n"))
            if newname and len(newname) >= 2:
                tid = await create_todo(newname, user_id=uid)
                if tid:
                    added = newname
        if removed or added:
            return {"action": "list_edit", "ok": True, "removed": removed, "added": added}
        # a remove verb fired but nothing native matched and nothing was added -> ask, never guess
        return {"action": "list_edit", "ok": False, "removed": None, "added": None, "target": target}

    # --- tick / complete (needs an explicit action verb or +/- token) ---
    has_up_verb = any(re.search(r'\b' + re.escape(w) + r'\b', low) for w in _ACTION_UP)
    has_plus = any(tok in low for tok in _TOKEN_PLUS)
    has_minus = any(tok in low for tok in _TOKEN_MINUS)
    if not (has_up_verb or has_plus or has_minus):
        return None

    uid = user_id if user_id is not None else DEFAULT_USER_ID
    conn = await pg_conn()
    try:
        await ensure_schema(conn)
        habits = await list_habits(uid, conn=conn)
        todos = [t for t in await list_todos(uid, include_done=False, conn=conn)]
    finally:
        await conn.close()

    hmatch = _match_items(text, habits, "name")
    tmatch = _match_items(text, todos, "text")
    total = len(hmatch) + len(tmatch)
    if total == 0:
        return None
    if total > 1:
        names = [h["name"] for h in hmatch] + [t["text"] for t in tmatch]
        return {"action": "ambiguous", "candidates": names}

    if hmatch:
        h = hmatch[0]
        # direction: minus token -> down; else up/check by field type
        if has_minus and h["field_type"] in ("negative", "both"):
            direction = "down"
        elif h["field_type"] == "tickbox":
            direction = "check"
        elif h["field_type"] == "negative":
            direction = "down"
        else:
            direction = "up"
        res = await tap_habit(h["id"], direction)
        res["action"] = "tap_habit"
        return res
    t = tmatch[0]
    res = await complete_todo(t["id"], True)
    res["action"] = "complete_todo"
    return res
