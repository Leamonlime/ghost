#!/usr/bin/env python3
"""Ghost Home P3 tests: app_idempotency retention (prune_idempotency + the hourly Redis-locked hook)
against the REAL handlers in a throwaway schema (zzt_idemprune_<pid>, dropped at the end) with a
throwaway uid. Redis is faked. Needs Postgres creds (POSTGRES_HOST / POSTGRES_PASSWORD env, or
~/.ghost-runner/host.env); without them only the DB-free checks run. Never touches real rows.
"""
import asyncio
import json
import logging
import os
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
os.environ.setdefault("BOT_CODE_PATH", str(HERE.parent / "bot"))
os.environ["GHOST_TZ"] = "Australia/Melbourne"
os.environ.pop("GHOST_IDEM_PRUNE", None)
sys.path.insert(0, str(HERE.parent / "bot"))
sys.path.insert(0, str(HERE.parent / "dashboard"))
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

TOKEN = "zzt-idemprune-token"
dash.APP_API_TOKEN = TOKEN
AUTH = {"Authorization": f"Bearer {TOKEN}"}


class FakeRedis:
    store = {}

    async def get(self, k):
        return FakeRedis.store.get(k)

    async def set(self, k, v, ex=None, nx=False):
        if nx and k in FakeRedis.store:
            return None
        FakeRedis.store[k] = v
        return True

    async def aclose(self):
        pass


def client():
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=dash.app), base_url="http://t")


# --------------------------------------------------------------------------- DB-free
async def no_db():
    check("default mode is dry", dash.IDEM_PRUNE_MODE == "dry", dash.IDEM_PRUNE_MODE)
    check("retention constants", (dash.IDEM_BODY_DAYS, dash.IDEM_KEY_DAYS, dash.IDEM_PRUNE_BATCH,
                                  dash.IDEM_PRUNE_LOCK_TTL) == (7, 90, 5000, 3600))
    check("status count parse", dash._status_count("DELETE 12") == 12 and dash._status_count("x") == 0)
    dash.IDEM_PRUNE_MODE = "0"
    check("mode 0: hook does nothing", await dash._maybe_prune_idempotency() is None)
    dash.IDEM_PRUNE_MODE = "dry"


# --------------------------------------------------------------------------- DB half
SCHEMA = f"zzt_idemprune_{os.getpid()}"
UID = 990097


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


async def _q(sql, *args, fetch="fetch"):
    c = await _conn()
    try:
        return await getattr(c, fetch)(sql, *args)
    finally:
        await c.close()


def _open_gates(mode):
    dash.IDEM_PRUNE_MODE = mode
    dash._IDEM_PRUNE_NEXT = 0.0
    FakeRedis.store.clear()


async def db_half():
    c = await _admin()
    await c.execute(f"CREATE SCHEMA {SCHEMA}")
    await c.close()
    try:
        dash.pg_conn = _conn
        native.pg_conn = _conn
        dash.OWNER_ID = UID
        dash._APP_SCHEMA_READY = False
        dash.redis_conn = lambda: FakeRedis()
        dash._IDEM_PRUNE_NEXT = float("inf")      # no background prune during setup

        async def todos():
            return await _q("SELECT count(*) FROM todos", fetch="fetchval")

        async with client() as h:
            orig = {}
            for cid, txt in (("zzt-c1", "zzt one day"), ("zzt-c8", "zzt eight days"), ("zzt-c91", "zzt ninety-one")):
                r = await h.post("/api/app/v1/todos", json={"text": txt, "client_id": cid}, headers=AUTH)
                check(f"setup write {cid}", r.status_code == 200 and r.json()["ok"], r.text[:200])
                orig[cid] = r.json()
            check("schema: created_at index", await _q(
                "SELECT 1 FROM pg_indexes WHERE schemaname=$1 AND indexname='app_idempotency_created_at_idx'",
                SCHEMA, fetch="fetchval") == 1)
            check("schema: response nullable", await _q(
                "SELECT is_nullable FROM information_schema.columns WHERE table_schema=$1 "
                "AND table_name='app_idempotency' AND column_name='response'", SCHEMA, fetch="fetchval") == "YES")
            for cid, days in (("zzt-c1", 1), ("zzt-c8", 8), ("zzt-c91", 91)):
                await _q("UPDATE app_idempotency SET created_at = now() - make_interval(days => $2) "
                         "WHERE client_id=$1", cid, days, fetch="execute")
            n_todos = await todos()

            # ---- dry run: right counts, nothing changes
            before = [dict(r) for r in await _q("SELECT * FROM app_idempotency ORDER BY client_id")]
            dry = await dash.prune_idempotency(dry_run=True)
            check("dry: counts", dry == {"nulled": 1, "deleted": 1, "dry_run": True}, str(dry))
            after = [dict(r) for r in await _q("SELECT * FROM app_idempotency ORDER BY client_id")]
            check("dry: nothing changed", before == after)

            # ---- hook (live) with a write landing during the prune
            _open_gates("1")
            res, w = await asyncio.gather(
                dash._maybe_prune_idempotency(),
                h.post("/api/app/v1/todos", json={"text": "zzt during prune", "client_id": "zzt-cnew"}, headers=AUTH))
            check("live: counts match dry run", res == {"nulled": 1, "deleted": 1, "dry_run": False}, str(res))
            check("during: write ok", w.status_code == 200 and w.json()["ok"], w.text[:200])
            row = await _q("SELECT response FROM app_idempotency WHERE client_id='zzt-cnew'", fetch="fetchrow")
            check("during: key stored with body", row is not None and json.loads(row["response"]) == w.json())
            n_todos += 1

            rows = {r["client_id"]: r for r in await _q("SELECT * FROM app_idempotency")}
            check("1d: untouched", rows.get("zzt-c1") is not None
                  and json.loads(rows["zzt-c1"]["response"]) == orig["zzt-c1"])
            check("8d: body null, metadata kept", rows.get("zzt-c8") is not None
                  and rows["zzt-c8"]["response"] is None and rows["zzt-c8"]["endpoint"] == "todo_create"
                  and rows["zzt-c8"]["created_at"] is not None)
            check("91d: gone", "zzt-c91" not in rows)

            # ---- lock: a second run in the same hour does nothing (same pod, then a "second pod")
            check("lock: same pod gated", await dash._maybe_prune_idempotency() is None)
            dash._IDEM_PRUNE_NEXT = 0.0
            check("lock: other pod blocked by redis lock", await dash._maybe_prune_idempotency() is None)
            check("lock: redis key set", FakeRedis.store.get(dash.IDEM_PRUNE_LOCK) == "1")

            # ---- replays
            r = await h.post("/api/app/v1/todos", json={"text": "zzt eight days", "client_id": "zzt-c8"}, headers=AUTH)
            check("replay 8d: 409 duplicate", r.status_code == 409 and r.json() == {"status": "duplicate"}, r.text)
            check("replay 8d: no new todo", await todos() == n_todos)
            r = await h.post("/api/app/v1/todos/1/complete", json={"client_id": "zzt-c8"}, headers=AUTH)
            check("replay 8d other route: 409", r.status_code == 409)
            r = await h.post("/api/app/v1/todos", json={"text": "zzt one day", "client_id": "zzt-c1"}, headers=AUTH)
            check("replay 1d: original response", r.status_code == 200 and r.json() == orig["zzt-c1"], r.text)
            check("replay 1d: no new todo", await todos() == n_todos)

            # ---- batching: more rows than one batch
            dash.IDEM_PRUNE_BATCH = 2
            for i in range(5):
                await _q("INSERT INTO app_idempotency (client_id, endpoint, response, created_at) VALUES "
                         "($1,'todo_create','{}', now() - interval '10 days')", f"zzt-b8-{i}", fetch="execute")
                await _q("INSERT INTO app_idempotency (client_id, endpoint, response, created_at) VALUES "
                         "($1,'todo_create','{}', now() - interval '100 days')", f"zzt-b91-{i}", fetch="execute")
            dry = await dash.prune_idempotency(dry_run=True)
            real = await dash.prune_idempotency(dry_run=False)
            check("batch: dry counts", dry == {"nulled": 5, "deleted": 5, "dry_run": True}, str(dry))
            check("batch: real counts match", real == {"nulled": 5, "deleted": 5, "dry_run": False}, str(real))
            check("batch: idempotent second run", await dash.prune_idempotency(dry_run=False)
                  == {"nulled": 0, "deleted": 0, "dry_run": False})
            dash.IDEM_PRUNE_BATCH = 5000
            check("batch: survivors", await _q("SELECT count(*) FROM app_idempotency", fetch="fetchval") == 8)

            # ---- the write path schedules the hook (dry mode changes nothing)
            _open_gates("dry")
            before = [dict(r) for r in await _q("SELECT client_id, response FROM app_idempotency ORDER BY client_id")]
            r = await h.post("/api/app/v1/todos", json={"text": "zzt hook", "client_id": "zzt-chook"}, headers=AUTH)
            check("hook: write ok", r.status_code == 200)
            await asyncio.gather(*list(dash._IDEM_PRUNE_TASKS))
            check("hook: lock taken by the scheduled run", dash.IDEM_PRUNE_LOCK in FakeRedis.store)
            after = [dict(r) for r in await _q("SELECT client_id, response FROM app_idempotency "
                                               "WHERE client_id <> 'zzt-chook' ORDER BY client_id")]
            check("hook dry: nothing changed", before == after)
    finally:
        dash._IDEM_PRUNE_NEXT = float("inf")
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
        prune_lines = [x for x in grab.lines if x.startswith("idempotency prune")]
        check("logs: one line per run", len(prune_lines) == 2, str(prune_lines))
        check("logs: counts only", all(re.fullmatch(
            r"idempotency prune \((dry|live)\): nulled=\d+ deleted=\d+ in \d+ ms", x) for x in prune_lines),
            str(prune_lines))
    else:
        print("SKIP: idempotency prune DB half (no Postgres creds)")
    logged = "\n".join(grab.lines)
    check("logs: no bodies or titles", "zzt" not in logged, logged[-500:])
    print(f"idempotency_prune: {PASSES} passed, {len(FAILS)} failed")
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    asyncio.run(main())
