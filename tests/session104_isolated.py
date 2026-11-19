#!/usr/bin/env python3
"""Session 104 tests: chat trackers, delete-from-chat, todo due/notes parsing, todo_create endpoint
fields. Pure checks always run; the DB half runs in a throwaway DATABASE (zzt_s104_<pid>, dropped
at the end) when Postgres creds are available (POSTGRES_HOST/POSTGRES_PASSWORD env, or
~/.ghost-runner/host.env). Never touches ghost_db.
"""
import asyncio
import os
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bot"))
FAILS, PASSES = [], 0


def check(name, cond, extra=""):
    global PASSES
    if cond:
        PASSES += 1
    else:
        FAILS.append(name)
        print(f"FAIL: {name} {extra}")


def load_creds():
    if os.environ.get("POSTGRES_PASSWORD"):
        return True
    f = Path.home() / ".ghost-runner" / "host.env"
    if not f.exists():
        return False
    for line in f.read_text().splitlines():
        k, sep, v = line.strip().partition("=")
        if sep and k in ("POSTGRES_HOST", "POSTGRES_PASSWORD"):
            os.environ[k] = v.strip('"\'')
    return bool(os.environ.get("POSTGRES_PASSWORD"))


HAVE_DB = load_creds()
DBNAME = f"zzt_s104_{os.getpid()}"
if HAVE_DB:
    os.environ["GHOST_DB_NAME"] = DBNAME
import native  # noqa: E402

FRI = date(2026, 11, 6)


def pure():
    p = native.parse_due_phrase
    check("due: tomorrow", p("tomorrow", FRI) == date(2026, 11, 7))
    check("due: weekday = next occurrence", p("monday", FRI) == date(2026, 11, 9))
    check("due: same weekday = a week out", p("friday", FRI) == date(2026, 11, 13))
    check("due: dd/mm day-first", p("12/11", FRI) == date(2026, 11, 12))
    check("due: dd/mm already past -> next year", p("1/11", FRI) == date(2027, 11, 1))
    check("due: iso", p("2026-12-01", FRI) == date(2026, 12, 1))
    check("due: garbage -> None", p("foo", FRI) is None and p("31/02", FRI) is None)
    t = native.parse_todo_extras
    check("extras: due + notes", t("call plumber due tomorrow notes: ask about the tap", FRI)
          == ("call plumber", date(2026, 11, 7), "ask about the tap"))
    check("extras: 'by card' is not a date", t("pay by card", FRI) == ("pay by card", None, None))
    check("extras: priority before due kept for caller", t("email Sam high priority by friday", FRI)[1] == date(2026, 11, 13)
          and "high" in t("email Sam high priority by friday", FRI)[0])
    check("extras: due before priority", t("email Sam by friday high", FRI)[1] == date(2026, 11, 13))
    check("extras: notes may contain 'high' and 'due' safely",
          t("tidy desk notes: high shelf, due soon", FRI) == ("tidy desk", None, "high shelf, due soon"))
    det = native.detect_native_create("add a todo to call the dentist due 12/11 high priority notes: ask about the crown")
    c = (det or {}).get("creations") or [{}]
    check("detect: todo carries priority + due + notes",
          c[0].get("text") == "call the dentist" and c[0].get("priority") == "high"
          and c[0].get("due_date") and c[0].get("notes") == "ask about the crown", str(c))
    check("detect: plain todo unchanged (no due, no notes)",
          (native.detect_native_create("add a todo to buy milk") or {}).get("creations") == [
              {"kind": "todo", "text": "buy milk", "priority": "med", "due_date": None, "notes": None}])
    check("tracker regex: 'start a tracker for X'", native._CREATE_TRACKER_RE.match("start a tracker for Dentist visits").group("n") == "Dentist visits")
    check("tracker regex: 'track X once'", native._TRACK_ONCE_RE.match("track the plumber visit once").group("n") == "the plumber visit")
    check("tracker regex: habit phrasing NOT claimed", not native._CREATE_TRACKER_RE.match("track reading as a habit")
          and not native._CREATE_TRACKER_RE.match("add a habit for stretching"))


async def db_half():
    import asyncpg
    adm = await asyncpg.connect(host=os.environ["POSTGRES_HOST"], port=5432, user="postgres",
                                password=os.environ["POSTGRES_PASSWORD"], database="postgres")
    await adm.execute(f'CREATE DATABASE "{DBNAME}"')
    await adm.close()
    try:
        U = 990104
        # --- tracker lifecycle through chat ---
        r = await native.route_message("start a tracker for Zebra clinic visits", U)
        check("chat: tracker created", r and r["action"] == "create_tracker" and r["ok"], str(r))
        hid = r["id"]
        check("tracker hidden from default list_habits", all(h["id"] != hid for h in await native.list_habits(U)))
        shown = [h for h in await native.list_habits(U, include_trackers=True) if h["id"] == hid]
        check("tracker visible with include_trackers, flagged", shown and shown[0]["is_tracker"])
        r = await native.route_message("did Zebra clinic visits", U)
        check("chat: 'did X' counts the tracker", r and r["action"] == "tap_habit" and r["ok"], str(r))
        shown = [h for h in await native.list_habits(U, include_trackers=True) if h["id"] == hid][0]
        check("tracker count recorded (today_up=1)", shown["today_up"] == 1, str(shown["today_up"]))
        r = await native.route_message("track the second thing once", U)
        check("chat: 'track X once' creates a tracker", r and r["action"] == "create_tracker" and r["ok"])
        # --- delete: single match archives (soft), ambiguity deletes nothing ---
        r = await native.route_message("delete Zebra clinic", U)
        check("chat: delete tracker by name -> archived", r and r["action"] == "list_edit" and r["removed"] == ["tracker", "Zebra clinic visits"], str(r))
        conn = await native.pg_conn()
        row = await conn.fetchrow("SELECT active, is_tracker FROM habits WHERE id=$1", hid)
        taps = await conn.fetchval("SELECT count(*) FROM habit_taps WHERE habit_id=$1", hid)
        check("soft delete: row kept, inactive; taps kept", row and row["active"] is False and row["is_tracker"] and taps == 1, str(dict(row or {})))
        await conn.close()
        check("archived tracker no longer listed", all(h["id"] != hid for h in await native.list_habits(U, include_trackers=True)))
        a = await native.create_todo("Pay zorp invoice", user_id=U)
        b = await native.create_todo("Pay zorp rent", user_id=U)
        r = await native.route_message("delete zorp", U)
        check("chat: ambiguous delete asks, intent=delete", r and r["action"] == "ambiguous" and r.get("intent") == "delete" and len(r["candidates"]) == 2, str(r))
        todos = [t["id"] for t in await native.list_todos(U, include_done=False)]
        check("ambiguous delete removed NOTHING", a in todos and b in todos)
        r = await native.route_message("delete the zorp rent", U)
        check("chat: specific name deletes exactly one todo", r and r["removed"] == ["todo", "Pay zorp rent"], str(r))
        todos = [t["id"] for t in await native.list_todos(U, include_done=False)]
        conn = await native.pg_conn()
        archived = await conn.fetchval("SELECT archived FROM todos WHERE id=$1", b)
        await conn.close()
        check("todo delete is soft (archived flag, row kept); sibling untouched", archived is True and a in todos and b not in todos)
        # exact name in one list is NOT made ambiguous by a loose word hit in another list
        th = await native.create_todo("Quux dentist booking", user_id=U)
        hh = await native.create_habit("Quux stretching", user_id=U)
        r = await native.route_message("delete Quux dentist booking", U)
        check("chat: exact todo name wins over loose habit word-overlap", r and r.get("removed") == ["todo", "Quux dentist booking"], str(r))
        conn = await native.pg_conn()
        ha = await conn.fetchval("SELECT active FROM habits WHERE id=$1", hh)
        await conn.close()
        check("...and the habit sharing a word is untouched", ha is True)
        await native.create_todo("Quux other errand", user_id=U)
        r = await native.route_message("remove quux", U)
        check("chat: loose shared word across lists asks which (nothing deleted)", r and r["action"] == "ambiguous", str(r))
        # habits are not tracker-polluted: a normal habit with a similar name still works
        # --- todo create via chat with due + notes ---
        r = await native.route_message("add todo: Book vet due 20/11 high notes: ask about shots", U)
        check("chat: create_todo returns due + notes", r and r["ok"] and r["due_date"] and r["notes"] == "ask about shots", str(r))
        t = [x for x in await native.list_todos(U) if x["id"] == r["id"]][0]
        check("todo stored with priority/due/notes", t["priority"] == "high" and t["due_date"].endswith("-11-20") and t["notes"] == "ask about shots", str(t))
        tid = await native.create_todo("No-notes todo", user_id=U)
        t2 = [x for x in await native.list_todos(U) if x["id"] == tid][0]
        check("todo without notes: notes is None, nothing else changed", t2["notes"] is None and t2["priority"] == "med" and t2["due_date"] is None)
        # list order contract untouched (done, priority, due date): high before med
        order = [x["id"] for x in await native.list_todos(U) if not x["done"]]
        check("todo sort unchanged (high before med)", order.index(r["id"]) < order.index(tid))
    finally:
        adm = await asyncpg.connect(host=os.environ["POSTGRES_HOST"], port=5432, user="postgres",
                                    password=os.environ["POSTGRES_PASSWORD"], database="postgres")
        await adm.execute(f'DROP DATABASE IF EXISTS "{DBNAME}" WITH (FORCE)')
        await adm.close()


pure()
if HAVE_DB:
    asyncio.run(db_half())
else:
    print("SKIP db half: no Postgres creds")
print(f"{PASSES} passed, {len(FAILS)} failed")
sys.exit(1 if FAILS else 0)
