#!/usr/bin/env python3
"""Ghost Home P2 tests: GET /api/app/v1/home (full fetch, stateless section delta, ETag/304) against
the REAL handler in a throwaway schema (zzt_homep2_<pid>, dropped at the end) with a throwaway uid.
Redis and Google are faked (no real calendar, no real cache keys). Needs Postgres creds
(POSTGRES_HOST / POSTGRES_PASSWORD env, or ~/.ghost-runner/host.env); without them only the
DB-free cursor/auth checks run. Never touches real rows.
"""
import asyncio
import json
import logging
import os
import sys
import time
import zoneinfo
from datetime import datetime, timedelta
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

TOKEN = "zzt-homep2-token"
dash.APP_API_TOKEN = TOKEN
AUTH = {"Authorization": f"Bearer {TOKEN}"}
URL = "/api/app/v1/home"
SECRET_EVENT = "ZZT Secret Event Title"
SECTIONS = {"habits", "todos", "next_task", "nutrition", "next_event", "counts"}


class FakeRedis:
    store = {}

    async def get(self, k):
        return FakeRedis.store.get(k)

    async def set(self, k, v, ex=None):
        FakeRedis.store[k] = v

    async def aclose(self):
        pass


GCAL = {"mode": "ok", "calls": 0}


async def fake_get_events(time_min, time_max, query=None, max_results=50):
    GCAL["calls"] += 1
    if GCAL["mode"] == "slow":
        await asyncio.sleep(5)
    if GCAL["mode"] == "none":
        return None
    return [{"id": "ev1", "summary": SECRET_EVENT, "location": "ZZT place", "all_day": False,
             "start": time_min + timedelta(hours=2), "end": time_min + timedelta(hours=3)}]


def client():
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=dash.app), base_url="http://t")


# --------------------------------------------------------------------------- DB-free
async def no_db():
    check("cursor: garbage decodes to None", dash._home_cursor_decode("!!!not-base64") is None)
    check("cursor: non-dict decodes to None", dash._home_cursor_decode(dash._home_cursor_encode([1, 2])) is None)
    check("cursor: empty decodes to None", dash._home_cursor_decode("") is None)
    cur = {"v": 1, "date": "2026-11-19", "h": {"habits": "abc"}}
    check("cursor: round trip", dash._home_cursor_decode(dash._home_cursor_encode(cur)) == cur)
    check("hash: key order independent", dash._home_hash({"a": 1, "b": 2}) == dash._home_hash({"b": 2, "a": 1}))
    check("hash: 16 hex", len(dash._home_hash([])) == 16)
    async with client() as h:
        r = await h.get(URL)
        check("auth: no token -> 401", r.status_code == 401)
        r = await h.get(URL, headers={"Authorization": "Bearer nope"})
        check("auth: wrong token -> 401", r.status_code == 401)


# --------------------------------------------------------------------------- DB half
SCHEMA = f"zzt_homep2_{os.getpid()}"
UID = 990096


async def _admin():
    import asyncpg
    return await asyncpg.connect(host=os.environ.get("POSTGRES_HOST", "localhost"), user="postgres",
                                 password=os.environ["POSTGRES_PASSWORD"], database="ghost_db")


async def _conn():
    import asyncpg
    return await asyncpg.connect(
        host=os.environ.get("POSTGRES_HOST", "localhost"), user="postgres",
        password=os.environ["POSTGRES_PASSWORD"], database="ghost_db",
        server_settings={"search_path": SCHEMA})


async def db_half():
    c = await _admin()
    await c.execute(f"CREATE SCHEMA {SCHEMA}")
    await c.close()
    real_now = dash._home_now
    try:
        dash.pg_conn = _conn
        native.pg_conn = _conn
        dash.OWNER_ID = UID
        dash._APP_SCHEMA_READY = False
        dash.redis_conn = lambda: FakeRedis()
        dash.gcal.get_events = fake_get_events
        today = datetime.now(MEL).date()

        hid = await native.create_habit("zzt water habit", user_id=UID)
        await native.create_habit("zzt walk habit", field_type="counter", target_value=3, unit="laps", user_id=UID)
        t_over = await native.create_todo("zzt overdue todo", priority="low", due_date=today - timedelta(days=2),
                                          user_id=UID, notes="zzt private note")
        t_high = await native.create_todo("zzt undated high", priority="high", user_id=UID)
        t_later = await native.create_todo("zzt later todo", priority="med", due_date=today + timedelta(days=5),
                                           user_id=UID)
        t_none = await native.create_todo("zzt undated low", priority="low", user_id=UID)

        async with client() as h:
            # ---- full fetch shape
            r = await h.get(URL, headers=AUTH)
            j = r.json()
            check("full: 200", r.status_code == 200, r.text[:300])
            check("full: top keys", set(j) == {"v", "generated_at", "date", "cursor", "full", "sections"}, str(set(j)))
            check("full: v/date/full", j["v"] == 1 and j["date"] == today.isoformat() and j["full"] is True)
            s = j["sections"]
            check("full: every section", set(s) == SECTIONS, str(set(s)))
            check("full: habit shape", s["habits"] and all(set(x) == {
                "id", "name", "field_type", "done", "today_count", "target_value", "unit"} for x in s["habits"]), str(s["habits"]))
            check("full: todo shape", all(set(x) == {"id", "title", "due_date", "due_time", "priority"}
                                          for x in s["todos"]), str(s["todos"]))
            check("full: todos in next_task order then due",
                  [x["id"] for x in s["todos"]] == [t_over, t_high, t_later, t_none], str([x["id"] for x in s["todos"]]))
            check("full: next_task overdue", (s["next_task"] or {}).get("id") == t_over
                  and s["next_task"]["reason"] == "overdue", str(s["next_task"]))
            check("full: nutrition keys", set(s["nutrition"]) == {
                "calories_target", "eaten_calories", "protein_target", "eaten_protein"}, str(s["nutrition"]))
            check("full: next_event from calendar helper", (s["next_event"] or {}).get("id") == "ev1"
                  and set(s["next_event"]) == {"id", "title", "start", "end", "all_day", "calendar"}, str(s["next_event"]))
            check("full: counts", s["counts"] == {"open_todos": 4, "habits_left": 2}, str(s["counts"]))
            check("full: no notes / location / encouragement", "zzt private note" not in r.text
                  and "ZZT place" not in r.text and "encouragement" not in r.text and "fuelling" not in r.text)
            check("headers: cache-control", r.headers.get("cache-control") == "private, max-age=0")
            etag = r.headers.get("etag")
            check("headers: etag quoted", bool(etag) and etag.startswith('"') and etag.endswith('"'))
            cur = j["cursor"]

            # ---- unchanged since -> full=false, empty sections; calendar served from cache
            GCAL["calls"] = 0
            r = await h.get(URL, params={"since": cur}, headers=AUTH)
            j = r.json()
            check("since: unchanged -> full=false empty", j["full"] is False and j["sections"] == {}, str(j))
            check("since: same cursor back", j["cursor"] == cur)
            check("since: calendar cached, no google call", GCAL["calls"] == 0)

            # ---- If-None-Match -> 304, no body
            r = await h.get(URL, headers={**AUTH, "If-None-Match": etag})
            check("etag: 304", r.status_code == 304 and r.content == b"", str(r.status_code))
            check("etag: 304 keeps etag header", r.headers.get("etag") == etag)
            r = await h.get(URL, headers={**AUTH, "If-None-Match": '"0000000000000000"'})
            check("etag: mismatch -> 200", r.status_code == 200)

            # ---- garbage cursors -> full=true, never 400
            for bad in ("garbage", "e30", dash._home_cursor_encode({"v": 2, "date": today.isoformat(), "h": {}}),
                        "x" * 5000):
                r = await h.get(URL, params={"since": bad}, headers=AUTH)
                check(f"garbage cursor -> full ({bad[:12]})", r.status_code == 200 and r.json()["full"] is True
                      and set(r.json()["sections"]) == SECTIONS, r.text[:200])

            # ---- tick a habit: only habits + counts (next_task untouched by habits)
            await native.tap_habit(hid, direction="check")
            r = await h.get(URL, params={"since": cur}, headers=AUTH)
            j = r.json()
            check("tick: full=false", j["full"] is False)
            check("tick: only habits+counts", set(j["sections"]) == {"habits", "counts"}, str(set(j["sections"])))
            hb = {x["id"]: x for x in j["sections"].get("habits", [])}
            check("tick: habit done", hb.get(hid, {}).get("done") is True, str(hb.get(hid)))
            check("tick: habits_left 1", j["sections"].get("counts", {}).get("habits_left") == 1)
            check("tick: new etag", r.headers.get("etag") != etag)
            cur = j["cursor"]

            # ---- complete a todo: todos, next_task, counts; completed id absent
            await native.complete_todo(t_over)
            r = await h.get(URL, params={"since": cur}, headers=AUTH)
            j = r.json()
            check("complete: sections", set(j["sections"]) == {"todos", "next_task", "counts"}, str(set(j["sections"])))
            ids = [x["id"] for x in j["sections"].get("todos", [])]
            check("complete: id absent", t_over not in ids and ids == [t_high, t_later, t_none], str(ids))
            check("complete: next_task moved", (j["sections"].get("next_task") or {}).get("id") == t_high)
            check("complete: open_todos 3", j["sections"].get("counts", {}).get("open_todos") == 3)
            cur = j["cursor"]

            # ---- new day (frozen clock past midnight) -> full=true
            tomorrow0 = datetime.combine(today + timedelta(days=1), datetime.min.time(), MEL) + timedelta(minutes=5)
            dash._home_now = lambda: tomorrow0
            r = await h.get(URL, params={"since": cur}, headers=AUTH)
            j = r.json()
            check("new day: full=true all sections", j["full"] is True and set(j["sections"]) == SECTIONS, str(j)[:200])
            check("new day: date rolled", j["date"] == (today + timedelta(days=1)).isoformat())
            dash._home_now = real_now

            # ---- calendar: Google hanging never blocks past 1.5s; backoff skips the next attempt
            FakeRedis.store.clear()
            GCAL.update(mode="slow", calls=0)
            t0 = time.monotonic()
            r = await h.get(URL, headers=AUTH)
            dt = time.monotonic() - t0
            check("cal slow: 200 null event < 2s", r.status_code == 200 and r.json()["sections"]["next_event"] is None
                  and dt < 2.0, f"{dt:.2f}s")
            t0 = time.monotonic()
            r = await h.get(URL, headers=AUTH)
            dt = time.monotonic() - t0
            check("cal backoff: no google retry, under the 1.5 s timeout", GCAL["calls"] == 1 and dt < 1.5,
                  f"{GCAL} {dt:.2f}s")
            GCAL.update(mode="ok", calls=0)
            FakeRedis.store.clear()

            # ---- reused connection: killed underneath (e.g. Postgres restart) -> reconnects, still 200
            check("conn: one reused connection held", dash._HOME_CONN is not None)
            c = await _admin()
            await c.execute("SELECT pg_terminate_backend($1)", dash._HOME_CONN.get_server_pid())
            await c.close()
            await asyncio.sleep(0.2)
            r = await h.get(URL, headers=AUTH)
            check("conn: terminated backend -> transparent reconnect", r.status_code == 200
                  and r.json()["full"] is True, r.text[:200])
            await dash._HOME_CONN.close()
            r = await h.get(URL, headers=AUTH)
            check("conn: closed connection -> reopened", r.status_code == 200)

            # ---- /today unchanged by this work
            r = await h.get("/api/app/v1/today", headers=AUTH)
            check("today: keys unchanged", sorted(r.json()) == sorted(
                ["ok", "date", "habits", "todos", "nutrition", "next_task"]), str(sorted(r.json())))

            # ---- realistic fixture: 12 habits, 60 open todos -> size + warm timing
            for i in range(10):
                await native.create_habit(f"zzt habit number {i} with a realistic name", user_id=UID)
            c = await _conn()
            for i in range(60):
                await c.execute(
                    "INSERT INTO todos (user_id, text, priority, due_date) VALUES ($1,$2,$3,$4)",
                    UID, f"zzt realistic todo {i}: book the thing and email the person about it",
                    ("high", "med", "low")[i % 3], (today + timedelta(days=i % 20 - 5)) if i % 4 else None)
            await c.close()
            await h.get(URL, headers=AUTH)                       # warm
            times = []
            for _ in range(20):
                t0 = time.monotonic()
                r = await h.get(URL, headers=AUTH)
                times.append((time.monotonic() - t0) * 1000)
            size = len(r.content)
            times.sort()
            p95 = times[int(len(times) * 0.95) - 1]
            print(f"home realistic fixture: {size} bytes, {len(r.json()['sections']['todos'])} todos, "
                  f"{len(r.json()['sections']['habits'])} habits; warm p50 {times[10]:.0f} ms p95 {p95:.0f} ms "
                  f"(host -> cluster Postgres); all: {[round(x) for x in times]}")
            # where the time goes (host -> cluster round trips dominate; in-pod is much closer)
            t0 = time.monotonic()
            c = await _conn()
            t1 = time.monotonic()
            await native.list_habits(user_id=UID, conn=c)
            t2 = time.monotonic()
            await native.list_todos(user_id=UID, include_done=False, conn=c)
            t3 = time.monotonic()
            await dash._nutrition_today(c)
            t4 = time.monotonic()
            await c.fetchval("SELECT 1")
            t5 = time.monotonic()
            await c.close()
            print(f"home breakdown: connect {1000*(t1-t0):.0f} ms, list_habits {1000*(t2-t1):.0f} ms, "
                  f"list_todos {1000*(t3-t2):.0f} ms, nutrition {1000*(t4-t3):.0f} ms, one round trip {1000*(t5-t4):.1f} ms")
            check("size: realistic payload < 16 KB", size < 16 * 1024, str(size))
            check("size: todos capped at 50", len(r.json()["sections"]["todos"]) == 50)
            check("size: open_todos counts all", r.json()["sections"]["counts"]["open_todos"] == 63)
            check("perf: warm p95 < 300 ms", p95 < 300, f"{p95:.0f} ms")
    finally:
        dash._home_now = real_now
        await dash._home_conn_reset()
        c = await _admin()
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
    await no_db()
    if HAVE_DB:
        await db_half()
    else:
        print("SKIP: /home DB half (no Postgres creds)")
    logged = "\n".join(grab.lines)
    check("logs: no titles", "zzt" not in logged and SECRET_EVENT not in logged, logged[-500:])
    print(f"home_endpoint: {PASSES} passed, {len(FAILS)} failed")
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    asyncio.run(main())
