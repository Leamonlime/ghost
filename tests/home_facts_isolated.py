#!/usr/bin/env python3
"""Ghost Home P1 tests: native.next_task golden cases (tests/fixtures/next_task_golden.json, shared
with the Android app), GET /api/app/v1/calendar/next with gcal + Redis mocked, and the /today
next_task key against the REAL handler in a throwaway schema (zzt_home_<pid>, dropped at the end)
with a throwaway uid. The DB half runs only when Postgres creds are available (POSTGRES_HOST /
POSTGRES_PASSWORD env, or ~/.ghost-runner/host.env). Never touches real rows or real Google.
"""
import asyncio
import json
import logging
import os
import sys
import time
import zoneinfo
from datetime import datetime, date, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
os.environ.setdefault("BOT_CODE_PATH", str(HERE.parent / "bot"))
os.environ["GHOST_TZ"] = "Australia/Melbourne"
sys.path.insert(0, str(HERE.parent / "bot"))
sys.path.insert(0, str(HERE.parent / "dashboard"))
MEL = zoneinfo.ZoneInfo("Australia/Melbourne")
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

import native  # noqa: E402
import httpx  # noqa: E402
import dashboard as dash  # noqa: E402

TOKEN = "zzt-home-token"
dash.APP_API_TOKEN = TOKEN
AUTH = {"Authorization": f"Bearer {TOKEN}"}
SECRET_TITLE = "ZZT Secret Dentist Title"


# --------------------------------------------------------------------------- next_task golden
def golden():
    cases = json.loads((HERE / "fixtures" / "next_task_golden.json").read_text())
    check("golden: has cases", len(cases) >= 12)
    reasons = set()
    for c in cases:
        got = native.next_task(c["todos"], c["now"], tz=MEL)
        gid = got["id"] if got else None
        check(f"golden: {c['name']}", gid == c["expected_id"], f"got {got}")
        if "expected_reason" in c:
            check(f"golden reason: {c['name']}", (got or {}).get("reason") == c["expected_reason"], f"got {got}")
        if got:
            reasons.add(got["reason"])
            check(f"golden shape: {c['name']}",
                  set(got) == {"id", "title", "due_date", "due_time", "priority", "reason"}, str(got))
    check("golden: every reason branch covered",
          reasons == {"overdue", "today_timed", "today", "high_priority"}, str(reasons))
    # extra unit checks not in the shared fixture
    got = native.next_task([{"id": 1, "text": "from list_todos", "priority": "high", "due_date": None,
                             "notes": "private", "done": False}], datetime(2026, 11, 19, 9, tzinfo=MEL), tz=MEL)
    check("unit: accepts list_todos 'text' key, no notes leak", got and got["title"] == "from list_todos"
          and "notes" not in got and got["reason"] == "high_priority", str(got))
    got = native.next_task([{"id": 1, "title": "x", "priority": "high", "due_date": date(2026, 11, 18),
                             "created_at": datetime(2026, 11, 1, tzinfo=MEL), "done": False}],
                           datetime(2026, 11, 19, 9, tzinfo=MEL), tz=MEL)
    check("unit: real date/datetime objects", got and got["due_date"] == "2026-11-18" and got["reason"] == "overdue", str(got))
    check("unit: archived skipped", native.next_task([{"id": 1, "priority": "high", "archived": True}],
                                                     datetime(2026, 11, 19, 9, tzinfo=MEL), tz=MEL) is None)


# --------------------------------------------------------------------------- calendar/next
class FakeRedis:
    store = {}
    fail = False

    async def get(self, k):
        if FakeRedis.fail:
            raise ConnectionError("down")
        return FakeRedis.store.get(k)

    async def set(self, k, v, ex=None):
        if FakeRedis.fail:
            raise ConnectionError("down")
        FakeRedis.store[k] = v

    async def aclose(self):
        pass


GCAL = {"mode": "ok", "events": [], "calls": 0, "window": None}


async def fake_get_events(time_min, time_max, query=None, max_results=50):
    GCAL["calls"] += 1
    GCAL["window"] = (time_min, time_max)
    if GCAL["mode"] == "raise":
        raise RuntimeError("google exploded")
    if GCAL["mode"] == "none":
        return None
    if GCAL["mode"] == "slow":
        await asyncio.sleep(5)
    return GCAL["events"]


def ev(id_, title, start, end, all_day=False):
    return {"id": id_, "summary": title, "location": "ZZT Secret Place", "all_day": all_day,
            "start": start, "end": end, "html_link": "https://example.invalid/x", "status": "confirmed"}


def reset(mode="ok", events=None, clear_cache=True):
    GCAL.update(mode=mode, events=events or [], calls=0, window=None)
    FakeRedis.fail = False
    if clear_cache:
        FakeRedis.store.clear()


async def calendar():
    dash.redis_conn = lambda: FakeRedis()
    dash.gcal.get_events = fake_get_events
    now = datetime.now(MEL)
    tr = httpx.ASGITransport(app=dash.app)
    async with httpx.AsyncClient(transport=tr, base_url="http://t") as h:
        url = "/api/app/v1/calendar/next"
        r = await h.get(url)
        check("cal: no token -> 401", r.status_code == 401)
        r = await h.get(url, headers={"Authorization": "Bearer wrong"})
        check("cal: bad token -> 401", r.status_code == 401)

        # event found: an in-progress event (not ended) comes first; ended ones skipped
        reset(events=[ev("e0", "ended", now - timedelta(hours=2), now - timedelta(minutes=1)),
                      ev("e1", SECRET_TITLE, now + timedelta(hours=1), now + timedelta(hours=2))])
        r = await h.get(url, headers=AUTH)
        j = r.json()
        check("cal: found 200", r.status_code == 200)
        check("cal: found live", j.get("source") == "live" and j.get("fetched_at"), str(j))
        e = j.get("event") or {}
        check("cal: found right event", e.get("id") == "e1" and e.get("title") == SECRET_TITLE, str(j))
        check("cal: event shape title+times only",
              set(e) == {"id", "title", "start", "end", "all_day", "calendar"} and e["all_day"] is False, str(e))
        check("cal: no location/link leak", "Secret Place" not in r.text and "example.invalid" not in r.text)
        check("cal: default window 36h", abs((GCAL["window"][1] - GCAL["window"][0]) - timedelta(hours=36)) < timedelta(seconds=5))

        # warm cache: second call does not hit Google
        GCAL["calls"] = 0
        t0 = time.monotonic()
        r = await h.get(url, headers=AUTH)
        check("cal: warm cache no google call", GCAL["calls"] == 0 and r.json()["event"]["id"] == "e1", str(r.json()))
        check("cal: warm cache source", r.json()["source"] == "cache")
        check("cal: warm cache fast", time.monotonic() - t0 < 2)

        # nothing in the window
        reset(events=[])
        r = await h.get(url + "?within_hours=12", headers=AUTH)
        check("cal: empty window -> null live", r.status_code == 200 and r.json() == {
            "event": None, "fetched_at": r.json()["fetched_at"], "source": "live"}, r.text)
        check("cal: within_hours honoured", abs((GCAL["window"][1] - GCAL["window"][0]) - timedelta(hours=12)) < timedelta(seconds=5))

        # clamp
        reset(events=[])
        await h.get(url + "?within_hours=999", headers=AUTH)
        check("cal: within_hours capped at 168", abs((GCAL["window"][1] - GCAL["window"][0]) - timedelta(hours=168)) < timedelta(seconds=5))
        reset(events=[])
        r = await h.get(url + "?within_hours=abc", headers=AUTH)
        check("cal: junk within_hours -> default, 200", r.status_code == 200 and
              abs((GCAL["window"][1] - GCAL["window"][0]) - timedelta(hours=36)) < timedelta(seconds=5))

        # all-day event
        today = now.date()
        reset(events=[ev("ad", "Holiday", today, today + timedelta(days=1), all_day=True)])
        r = await h.get(url, headers=AUTH)
        e = r.json().get("event") or {}
        check("cal: all-day event", e.get("all_day") is True and e.get("start") == today.isoformat()
              and e.get("end") == (today + timedelta(days=1)).isoformat(), r.text)

        # Google raising, no cache -> unavailable, 200
        reset(mode="raise")
        r = await h.get(url, headers=AUTH)
        check("cal: google raising no cache -> 200 unavailable", r.status_code == 200 and r.json() == {
            "event": None, "fetched_at": None, "source": "unavailable"}, r.text)
        reset(mode="none")
        r = await h.get(url, headers=AUTH)
        check("cal: google None no cache -> unavailable", r.status_code == 200 and r.json()["source"] == "unavailable")

        # Google raising WITH a stale cached value -> that value, source cache
        reset(events=[ev("e2", SECRET_TITLE, now + timedelta(hours=3), now + timedelta(hours=4))])
        await h.get(url, headers=AUTH)
        k = "calendar_next:36"
        stale = json.loads(FakeRedis.store[k])
        stale["ts"] -= 3600                      # older than the 5-min freshness window
        FakeRedis.store[k] = json.dumps(stale)
        reset(mode="raise", clear_cache=False)
        r = await h.get(url, headers=AUTH)
        j = r.json()
        check("cal: google raising with cache -> cache", r.status_code == 200 and j["source"] == "cache"
              and (j["event"] or {}).get("id") == "e2" and j["fetched_at"] == stale["fetched_at"], r.text)
        check("cal: stale cache did call google", GCAL["calls"] == 1)

        # cached event that has since ended -> null, still cache
        stale["event"]["end"] = (now - timedelta(minutes=5)).isoformat()
        FakeRedis.store[k] = json.dumps(stale)
        reset(mode="raise", clear_cache=False)
        r = await h.get(url, headers=AUTH)
        check("cal: cached-but-ended -> null", r.status_code == 200 and r.json()["event"] is None
              and r.json()["source"] == "cache", r.text)

        # Google hanging -> 1.5s timeout, never waits the 15s gcal timeout
        reset(mode="slow", events=[ev("slow", "x", now + timedelta(hours=1), now + timedelta(hours=2))])
        t0 = time.monotonic()
        r = await h.get(url, headers=AUTH)
        dt = time.monotonic() - t0
        check("cal: slow google times out ~1.5s", r.status_code == 200 and r.json()["source"] == "unavailable"
              and dt < 2.0, f"{dt:.2f}s {r.text}")

        # Redis down too -> still 200
        reset(mode="raise")
        FakeRedis.fail = True
        r = await h.get(url, headers=AUTH)
        check("cal: redis + google down -> 200 unavailable", r.status_code == 200 and r.json()["source"] == "unavailable")
        reset(events=[ev("e3", SECRET_TITLE, now + timedelta(hours=1), now + timedelta(hours=2))])
        FakeRedis.fail = True
        r = await h.get(url, headers=AUTH)
        check("cal: redis down, google ok -> live", r.status_code == 200 and r.json()["source"] == "live")
        FakeRedis.fail = False


# --------------------------------------------------------------------------- /today (DB half)
SCHEMA = f"zzt_home_{os.getpid()}"
UID = 990095


async def _conn():
    import asyncpg
    return await asyncpg.connect(
        host=os.environ.get("POSTGRES_HOST", "localhost"), user="postgres",
        password=os.environ["POSTGRES_PASSWORD"], database="ghost_db",
        server_settings={"search_path": SCHEMA})


async def today_half():
    import asyncpg
    c = await asyncpg.connect(host=os.environ.get("POSTGRES_HOST", "localhost"), user="postgres",
                              password=os.environ["POSTGRES_PASSWORD"], database="ghost_db")
    await c.execute(f"CREATE SCHEMA {SCHEMA}")
    await c.close()
    try:
        dash.pg_conn = _conn
        dash.OWNER_ID = UID
        dash._APP_SCHEMA_READY = False
        tr = httpx.ASGITransport(app=dash.app)
        async with httpx.AsyncClient(transport=tr, base_url="http://t") as h:
            r = await h.get("/api/app/v1/today")
            check("today: no token -> 401", r.status_code == 401)
            r = await h.get("/api/app/v1/today", headers=AUTH)
            j = r.json()
            check("today: 200 empty", r.status_code == 200 and j.get("ok"), r.text[:300])
            check("today: keys = old keys + next_task",
                  sorted(j) == sorted(["ok", "date", "habits", "todos", "nutrition", "next_task"]), str(sorted(j)))
            check("today: next_task null with no todos", j.get("next_task") is None)
            c = await _conn()
            today = datetime.now(MEL).date()
            await c.execute(
                "INSERT INTO todos (user_id, text, priority, due_date, notes) VALUES "
                "($1,'zzt low today','low',$2,NULL),($1,'zzt high today','high',$2,'zzt private note'),"
                "($1,'zzt overdue done','high',$3,NULL),($1,'zzt undated high','high',NULL,NULL)",
                UID, today, today - timedelta(days=3))
            await c.execute("UPDATE todos SET done=TRUE WHERE text='zzt overdue done'")
            await c.execute("INSERT INTO todos (user_id, text, priority, due_date) VALUES ($1,'zzt other uid','high',$2)",
                            UID + 1, today - timedelta(days=9))
            hid = await c.fetchval("SELECT id FROM todos WHERE text='zzt high today'")
            await c.close()
            r = await h.get("/api/app/v1/today", headers=AUTH)
            j = r.json()
            nt = j.get("next_task") or {}
            check("today: next_task picks high due today", nt.get("id") == hid and nt.get("reason") == "today", str(nt))
            check("today: next_task shape, no notes",
                  set(nt) == {"id", "title", "due_date", "due_time", "priority", "reason"}
                  and "zzt private note" not in json.dumps(nt), str(nt))
            check("today: other fields unchanged shape",
                  all(set(t) == {"id", "text", "priority", "due_date", "overdue", "checklist"} for t in j["todos"]), str(j["todos"]))
    finally:
        c = await asyncpg.connect(host=os.environ.get("POSTGRES_HOST", "localhost"), user="postgres",
                                  password=os.environ["POSTGRES_PASSWORD"], database="ghost_db")
        await c.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        await c.close()


class _Grab(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage())


async def main():
    grab = _Grab()
    logging.getLogger().addHandler(grab)
    logging.getLogger().setLevel(logging.DEBUG)
    golden()
    await calendar()
    if HAVE_DB:
        await today_half()
    else:
        print("SKIP: /today DB half (no Postgres creds)")
    logged = "\n".join(grab.lines)
    check("logs: no event titles", SECRET_TITLE not in logged and "Holiday" not in logged)
    check("logs: no todo titles", "zzt high today" not in logged and "zzt low today" not in logged)
    print(f"home_facts: {PASSES} passed, {len(FAILS)} failed")
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    asyncio.run(main())
