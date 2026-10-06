"""Ghost dashboard + webhook service.

Serves the at-a-glance dashboard, a chatbox that reuses the full bot pipeline
(bot.py is mounted from the host at /botcode), and the Tasker scrolling webhook.
Runs from a stock python:3.12-slim image — no custom image build needed.
"""
import os
import re
import sys
import copy
import time
import json
import hashlib
import logging
from datetime import datetime, timedelta, date
from decimal import Decimal
import zoneinfo

sys.path.insert(0, os.environ.get("BOT_CODE_PATH", "/botcode"))
import bot  # noqa: E402 — Ghost's own module, mounted read-only
import habitica  # noqa: E402
import native  # noqa: E402 — Ghost 2.0 native Habits & Todos (Phase 1), shared with the bot pipeline

import asyncpg  # noqa: E402
import httpx  # noqa: E402
from redis.asyncio import Redis  # noqa: E402
from fastapi import FastAPI, Request, UploadFile, File  # noqa: E402
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse  # noqa: E402
import uvicorn  # noqa: E402

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger("dashboard")

LOCAL_TZ = zoneinfo.ZoneInfo(os.environ.get("GHOST_TZ", "UTC"))
OWNER_ID = int(os.environ.get("TELEGRAM_USER_ID") or "0")
BOT_TOKEN = os.environ.get("BOT_TOKEN")

app = FastAPI()

# Rotating angles for scrolling interrupts so escalating ones don't all rhyme.
SCROLL_ANGLES = [
    "name what she's likely avoiding by scrolling, without asking a question",
    "the concrete time cost — what those minutes could have been instead",
    "one flat, dry observation about the app or the feed itself",
    "the gap between what she opened it for and what she's doing now",
    "a plain 'put it down' with a fresh image, no cliche",
    "what's waiting for her the moment she stops",
]

def redis_conn():
    return Redis(host=bot.REDIS_HOST, port=6379, password=bot.REDIS_PASSWORD, decode_responses=True)

async def pg_conn():
    # Session-55 dedupe (folded into the 2.0 build): delegate to the one shared helper in
    # native.py so bot, dashboard and the native model all open connections the same way.
    return await native.pg_conn()

async def _bust_native():
    """Drop bot's cached native-state grounding block after a dashboard write, so a chat turn
    right after reflects the change (S66)."""
    try:
        await bot._bust_native_cache()
    except Exception:
        pass

# --------------------------------------------------------------------------
# Display-layer helpers (Session 30, 05/08/2026). Demo mode, escalation theme,
# and liveness are DISPLAY ONLY — nothing here writes, nudges, or routes. Demo
# masking happens SERVER-SIDE so real figures never reach the browser at all
# (a client-only mask would still ship the real numbers in the network tab —
# useless for footage privacy).
# --------------------------------------------------------------------------
DEMO_BUDGET = [
    {"name": "eating out", "spent": 42.0, "limit": 80.0},
    {"name": "groceries", "spent": 180.0, "limit": 300.0},
    {"name": "other", "spent": 65.0, "limit": 200.0},
]
DEMO_DAILIES = {"pending": ["Morning routine", "Exercise", "Evening routine"],
                "done": ["Breakfast", "Lunch"]}
DEMO_TODOS = ["Sample project task", "Draft a blog post", "Errand to run"]
DEMO_APPS = {}  # real package -> generic label, filled deterministically per request

_MONEY_RE = re.compile(r"\$\s?\d[\d,]*(?:\.\d+)?")

# Visible build stamp so a stale cached page is obvious at a glance (Session 31). Bump
# this string on any dashboard deploy; it is injected into the page at serve time.
BUILD_ID = "S57 · momentum + data-role hooks · 2026-09-02"

# Session 44: md5 of the dashboard.py that THIS process is running, computed once at import.
# The deploy verifier (selfcode._verify_dashboard_deploy) polls /api/version until this
# equals md5(disk dashboard.py) — proving the NEW code is actually being served, not the
# old process during the pip-install startup overlap (the Session-31 caching confusion).
try:
    _SOURCE_MD5 = hashlib.md5(open(__file__, "rb").read()).hexdigest()
except Exception:
    _SOURCE_MD5 = "unknown"

def is_demo(request) -> bool:
    return request.query_params.get("demo") == "1"

def mask_money(text: str) -> str:
    """Replace $-amounts with a placeholder. Display-only, demo/footage mode."""
    if not text:
        return text
    return _MONEY_RE.sub("$•••", text)

def demo_app_label(pkg: str, seen: dict) -> str:
    """Deterministic generic label for a real app package in demo mode, stable
    within one response so the same app keeps the same fake name."""
    if pkg not in seen:
        seen[pkg] = f"App {chr(ord('A') + len(seen))}"
    return seen[pkg]

def _demo_msg_label(msg_type: str) -> str:
    """Generic placeholder for a message_log row's content in demo mode — real chat/nudge
    text (names, plans) replaced wholesale, keeping the row's type so the feed still reads
    as lived-in."""
    return "Quick note" if msg_type == "chat" else "Ghost check-in"

# Accent "heat" per escalation stage. Stage 4 is the session-17 wind-down — GENTLER
# than crude despite the higher number — so it must read as cooler than stage 3, not
# hotter. The accent shows the HOTTEST active category, so a category still at crude
# is never masked by another that has wound down.
_STAGE_HEAT = {0: 0.0, 1: 1.0, 2: 2.0, 3: 3.0, 4: 1.5}

async def read_escalation_stage() -> int:
    """The hottest CURRENT escalation stage across every category, read from the REAL
    Redis escalation:{category} state the scheduler writes (0 = calm, 1 gentle, 2 firmer,
    3 crude, 4 wind-down). Read-only; never writes. Missing keys = calm, which is correct
    (no active escalation), not an assumption about her behaviour."""
    best_stage, best_heat = 0, 0.0
    try:
        r = redis_conn()
        for k in await r.keys("escalation:*"):
            raw = await r.get(k)
            if not raw:
                continue
            try:
                st = int(json.loads(raw).get("stage", 0) or 0)
            except Exception:
                st = 0
            heat = _STAGE_HEAT.get(st, float(st))
            if heat > best_heat:
                best_heat, best_stage = heat, st
        await r.aclose()
    except Exception as e:
        logger.error(f"escalation stage read error: {e}")
    return best_stage

def seconds_to_midnight() -> int:
    now = datetime.now(LOCAL_TZ)
    midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return int((midnight - now).total_seconds())

async def send_telegram(message: str, silent: bool = False):
    async with httpx.AsyncClient() as client:
        response = await client.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                                     json={"chat_id": OWNER_ID, "text": message,
                                           "disable_notification": silent}, timeout=30.0)
        if response.status_code != 200:
            logger.error(f"Telegram send FAILED ({response.status_code}): {response.text[:200]}")
            raise RuntimeError(f"telegram send failed: {response.status_code}")

async def claude_line(prompt: str) -> str:
    """One-line Ghost message via Claude, with recent-phrase avoidance."""
    system = ("You are Ghost. Dry, blunt, minimal. One line only. "
              "You noticed something — you are not a system alert. No lectures, no filler. "
              "Do NOT open with her name and do not use it by default — a name-opener every time "
              "reads as a mechanical tic. Use it only when it does real work, which is rare. "
              "NEVER INVENT: state only what's in the prompt below. No made-up events, places, or details. "
              "NEVER use \"qualify as furniture\" or \"long enough to fossilize\", or any variation of either.")
    try:
        phrases = await bot.get_recent_phrases()
        if phrases:
            system += ("\n\nLINES YOU USED RECENTLY — do not reuse these, and do not reuse their SHAPE: "
                       "if a recent line counted her actions, opened with 'Still', or used the same sentence "
                       "template, take a completely different angle. Vary the structure, not just the words:\n"
                       + "\n".join(f"- {p}" for p in phrases))
    except Exception:
        pass
    async with httpx.AsyncClient() as client:
        r = await client.post("https://api.anthropic.com/v1/messages",
                              headers={"x-api-key": os.environ.get("ANTHROPIC_API_KEY"),
                                       "anthropic-version": "2023-06-01",
                                       "content-type": "application/json"},
                              json={"model": "claude-sonnet-4-6", "max_tokens": 80,
                                    "system": system,
                                    "messages": [{"role": "user", "content": prompt}]},
                              timeout=30.0)
        reply = r.json()["content"][0]["text"]
    try:
        await bot.save_phrase(reply)
    except Exception:
        pass
    return reply


async def _open_proposals(demo=False):
    """Open (proposed/approved/apply_queued) self-code proposals, newest first, capped at 10.
    S78: this exact query + row-shaping was duplicated verbatim in /api/data and /api/command-center
    (the budget-summary query the retirement pass named was already removed in S70, so THIS is the
    real remaining duplication) — now one shared function both endpoints call. `demo` reproduces
    /api/data's masking; the command center passes demo=False and masks on egress instead."""
    conn = await pg_conn()
    try:
        rows = await conn.fetch(
            "SELECT id, service, file_path, explanation, status FROM code_proposals "
            "WHERE status IN ('proposed','approved','apply_queued') ORDER BY id DESC LIMIT 10")
    finally:
        await conn.close()
    return [{
        "id": r["id"], "service": r["service"], "status": r["status"],
        "file": "example_module.py" if demo else os.path.basename(r["file_path"]),
        "explanation": "Sample proposed change" if demo else r["explanation"],
        "can_apply": r["service"] in ("bot", "scheduler") and r["status"] == "approved",
    } for r in rows]


@app.get("/api/data")
async def api_data(request: Request):
    now = datetime.now(LOCAL_TZ)
    demo = is_demo(request)
    out = {"time": now.strftime("%A %d %B — %I:%M %p").replace(" 0", " "), "errors": [],
           "demo": demo, "escalation_stage": await read_escalation_stage()}
    try:
        if demo:
            out["dailies"] = dict(DEMO_DAILIES)
            out["todos"] = list(DEMO_TODOS)
        else:
            out["dailies"] = await habitica.get_dailies()
            out["todos"] = (await habitica.get_todos())[:5]
    except Exception as e:
        out["errors"].append(f"habitica: {e}")
        out["dailies"] = {"done": [], "pending": []}
        out["todos"] = []
    try:
        conn = await pg_conn()
        # S70: the conversational money pipeline (expenses/budget_categories) was removed — spending
        # is CSV-only now, on the Bills dashboard. No budget-vs-limit block here anymore.
        events = await conn.fetch(
            "SELECT event_type, value FROM daily_events WHERE date = $1 ORDER BY event_type", now.date())
        out["today_events"] = [{"type": r["event_type"],
                                "value": float(r["value"]) if r["value"] is not None else None} for r in events]
        await conn.close()
    except Exception as e:
        out["errors"].append(f"postgres: {e}")
        out["today_events"] = []
    try:
        r = redis_conn()
        activity = {
            "water_nudges": int(await r.get("water_nudge_count") or 0),
            "water_confirms": int(await r.get("water_confirm_count") or 0),
            "exercise": bool(await r.get("exercise_confirmed")),
        }
        for key, label in (("last_water_confirmed", "last_water_hours"),
                           ("last_movement_confirmed", "last_movement_hours")):
            val = await r.get(key)
            activity[label] = round((now.timestamp() - float(val)) / 3600, 1) if val else None
        await r.aclose()
        out["activity"] = activity
    except Exception as e:
        out["errors"].append(f"redis: {e}")
        out["activity"] = {}
    try:
        if demo:
            out["streaks"] = ["Habit A: 5 days in a row", "Habit B: 3 of the last 7 days"]
        else:
            pattern = await bot.get_pattern_context()
            out["streaks"] = [l[2:] for l in pattern.splitlines() if l.startswith("- ")]
    except Exception as e:
        out["errors"].append(f"patterns: {e}")
        out["streaks"] = []
    try:
        conn = await pg_conn()
        rows = await conn.fetch("""
            SELECT DISTINCT ON (service) service, status, detail, checked_at
            FROM health_checks ORDER BY service, checked_at DESC
        """)
        await conn.close()
        out["health"] = [{
            "service": r["service"], "status": r["status"],
            "detail": "" if demo else r["detail"],   # detail can carry host/error strings
            "checked_at": r["checked_at"].astimezone(LOCAL_TZ).strftime("%a %I:%M%p").replace(" 0", " ").lower(),
        } for r in rows]
    except Exception as e:
        out["errors"].append(f"health: {e}")
        out["health"] = []
    try:
        out["proposals"] = await _open_proposals(demo)   # S78: shared helper (was duplicated here)
    except Exception as e:
        out["errors"].append(f"proposals: {e}")
        out["proposals"] = []
    return out

@app.post("/api/apply")
async def api_apply(request: Request):
    """Queue an ALREADY-APPROVED bot/scheduler proposal for the host runner to apply.
    This does NOT approve anything — it only fires the apply of a proposal the user already
    approved in Telegram, replacing the manual SSH `selfcode.py apply <id>` step. The
    dashboard never touches the filesystem or runs a rebuild; it flips one DB status
    and the host-side runner (with the scoped rebuild grant) does the privileged work."""
    if is_demo(request):
        return {"ok": False, "error": "demo mode is read-only"}
    try:
        data = await request.json()
        pid = int(data.get("id"))
    except Exception:
        return {"ok": False, "error": "bad request"}
    try:
        conn = await pg_conn()
        try:
            row = await conn.fetchrow("SELECT service, status FROM code_proposals WHERE id=$1", pid)
            if not row:
                return {"ok": False, "error": f"no proposal #{pid}"}
            if row["service"] not in ("bot", "scheduler"):
                return {"ok": False, "error": "button only applies to bot/scheduler proposals"}
            if row["status"] != "approved":
                return {"ok": False, "error": f"proposal #{pid} is '{row['status']}', not 'approved' — approve it in Telegram first"}
            await conn.execute("UPDATE code_proposals SET status='apply_queued' WHERE id=$1 AND status='approved'", pid)
        finally:
            await conn.close()
        logger.info(f"Proposal #{pid} queued for apply via dashboard button")
        return {"ok": True, "queued": pid}
    except Exception as e:
        logger.error(f"api_apply error: {e}")
        return {"ok": False, "error": str(e)}

@app.post("/api/chat")
async def api_chat(request: Request):
    try:
        data = await request.json()
        message = str(data.get("message", "")).strip()
        if not message:
            return {"reply": "say something."}
        # Demo mode: never reach the real bot — that would send to Telegram, write
        # real message_log rows, and return a reply grounded in real data. Canned reply.
        if is_demo(request):
            return {"reply": "(demo mode) I'd normally answer from your real data here.", "demo": True}
        await bot.log_message("dashboard_in", "chat", message)
        reply = await bot.process_message(OWNER_ID, message, source="dashboard")
        await bot.log_message("dashboard_out", "chat", reply)
        # Mirror dashboard chat into Telegram SILENTLY (no buzz) so the Telegram
        # app's own history stays complete (session 15, task 6).
        try:
            await send_telegram(f"[dashboard] {bot.USER_NAME}: {message}", silent=True)
            await send_telegram(reply, silent=True)
        except Exception as e:
            logger.error(f"telegram mirror error: {e}")
        return {"reply": reply}
    except Exception as e:
        logger.error(f"chat error: {e}")
        return {"reply": f"(ghost hit an error: {e})"}

CEPHALOPOD_URL = os.environ.get("CEPHALOPOD_URL", "http://cephalopod-bot.cephalopod.svc.cluster.local:8081")

@app.post("/api/cephalopod-chat")
async def api_cephalopod_chat(request: Request):
    """Proxy to Cephalopod's OWN bot service over the network (session 16, task 3).
    Deliberately NOT an import of Cephalopod's code (that would collapse the
    session-13 deployment separation) and deliberately NOT written to message_log
    (that table is Ghost's mirror; Cephalopod's history lives in its own Redis db 1)."""
    try:
        data = await request.json()
        message = str(data.get("message", "")).strip()
        if not message:
            return {"reply": "say something."}
        if is_demo(request):
            return {"reply": "(demo mode) Cephalopod replies here in normal use.", "demo": True}
        async with httpx.AsyncClient() as client:
            r = await client.post(f"{CEPHALOPOD_URL}/chat", json={"message": message}, timeout=30)
        return r.json()
    except Exception as e:
        logger.error(f"cephalopod chat proxy error: {e}")
        return {"reply": "(cephalopod unreachable — its pod may be scaled down)"}

@app.get("/api/cephalopod-history")
async def api_cephalopod_history(request: Request):
    if is_demo(request):
        return {"messages": [
            {"role": "user", "content": "Draft a short intro for the new post"},
            {"role": "assistant", "content": "(demo) Here's a sample draft opening…"},
        ], "demo": True}
    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(f"{CEPHALOPOD_URL}/chat/history", timeout=15)
        return r.json()
    except Exception as e:
        return {"messages": [], "error": str(e)}

@app.get("/api/feed")
async def api_feed(request: Request, after_id: int = 0):
    """Unified timeline from message_log. after_id=0 → last 50 rows; otherwise only
    rows newer than after_id (the page short-polls this every 7s — task 8. Polling
    over websockets: one user, tiny payloads, zero extra infra on this stack).
    Under demo mode ($-amounts are masked server-side so no real figure is shipped)."""
    demo = is_demo(request)
    try:
        conn = await pg_conn()
        if after_id > 0:
            rows = await conn.fetch(
                "SELECT id, ts, source, type, content FROM message_log WHERE id > $1 ORDER BY id LIMIT 200",
                after_id)
        else:
            rows = await conn.fetch(
                "SELECT id, ts, source, type, content FROM "
                "(SELECT * FROM message_log ORDER BY id DESC LIMIT 50) sub ORDER BY id")
        await conn.close()
        return {"messages": [{
            "id": r["id"],
            "ts": r["ts"].astimezone(LOCAL_TZ).strftime("%a %H:%M"),
            "source": r["source"], "type": r["type"],
            # Demo: message_log content is real chat/nudge text (names, plans, not just $),
            # so replace it wholesale with a generic label by type, not just mask_money.
            "content": _demo_msg_label(r["type"]) if demo else r["content"],
        } for r in rows]}
    except Exception as e:
        logger.error(f"feed error: {e}")
        return {"messages": [], "error": str(e)}

@app.get("/api/version")
async def api_version():
    """Session 44: the source md5 of the dashboard.py this live process is running, so a
    deploy can be verified as actually-serving-the-new-code (not just 'a page loads')."""
    return {"source_md5": _SOURCE_MD5, "build_id": BUILD_ID}

@app.get("/api/liveness")
async def api_liveness():
    """Real-liveness feed for the pulse dot + escalation theme (Session 30). Read-only.
    Returns the latest REAL backend-activity timestamp (newest across message_log,
    health_checks, routing_log — all things the scheduler/bot actually write) plus the
    current escalation stage. The client pulses when max_msg_id increases (a genuine new
    message_log row) and cools the dot when last_event drifts stale — it is tied to real
    state, not a free-running timer. Demo-safe: carries no financial content."""
    out = {"server_now": datetime.now(LOCAL_TZ).timestamp(),
           "escalation_stage": await read_escalation_stage(),
           "max_msg_id": 0, "last_event_ts": None}
    try:
        conn = await pg_conn()
        row = await conn.fetchrow("""
            SELECT (SELECT MAX(id) FROM message_log) AS max_id,
                   GREATEST(
                     COALESCE((SELECT MAX(ts) FROM message_log), 'epoch'),
                     COALESCE((SELECT MAX(checked_at) FROM health_checks), 'epoch'),
                     COALESCE((SELECT MAX(ts) FROM routing_log), 'epoch')
                   ) AS last_event
        """)
        await conn.close()
        out["max_msg_id"] = row["max_id"] or 0
        if row["last_event"] is not None:
            out["last_event_ts"] = row["last_event"].timestamp()
    except Exception as e:
        logger.error(f"liveness error: {e}")
        out["error"] = str(e)
    return out

@app.get("/api/timeline")
async def api_timeline(request: Request):
    """One day's activity across the three real tables (Session 30, task 4). Read-only.
    ONLY message_log carries a per-event timestamp, so nudges/messages are placed at
    their true time-of-day; daily_events and app_usage are day-level in the schema
    (no per-row ts) so they are returned as untimed day markers — represented as such,
    NOT fabricated onto invented times. Demo mode masks $-amounts and genericises app
    names. Types: 'nudge' (scheduler msg), 'message' (chat), 'completion'/'miss'/'event'
    (daily_events), 'app' (app_usage)."""
    demo = is_demo(request)
    now = datetime.now(LOCAL_TZ)
    # LOCAL day boundaries, not SQL CURRENT_DATE (which is the UTC day and would make
    # "today" a 10am-10am window for a UTC+10 user). message_log is a timestamptz →
    # bound by local midnight; daily_events/app_usage store the local date (now.date())
    # → match on that same date, consistent with how the rows are written.
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = day_start + timedelta(days=1)
    today = now.date()
    out = {"date": today.isoformat(), "timed": [], "allday": []}
    try:
        conn = await pg_conn()
        msgs = await conn.fetch(
            "SELECT ts, source, type, content FROM message_log "
            "WHERE ts >= $1 AND ts < $2 ORDER BY ts", day_start, day_end)
        for m in msgs:
            t = m["ts"].astimezone(LOCAL_TZ)
            kind = "message" if m["type"] == "chat" else "nudge"
            # Demo: replace real message text wholesale (not just $-mask) — see /api/feed.
            label = _demo_msg_label(m["type"]) if demo else m["content"]
            out["timed"].append({"hour": t.hour + t.minute / 60.0,
                                 "time": t.strftime("%H:%M"), "kind": kind,
                                 "label": (label or "")[:140]})
        events = await conn.fetch(
            "SELECT event_type, value FROM daily_events WHERE date = $1 ORDER BY event_type", today)
        for e in events:
            et = e["event_type"]
            kind = "completion" if et.endswith("_done") or et.endswith("_hit") \
                else ("miss" if et.endswith("_skipped") else "event")
            out["allday"].append({"kind": kind, "label": et.replace("_", " "),
                                  "value": float(e["value"]) if e["value"] is not None else None})
        seen = {}
        ignore = load_app_ignore_list()          # Session 27c launcher/system list
        names = {} if demo else load_app_display_names()  # Session 31 friendly names
        apps = await conn.fetch(
            "SELECT app, minutes FROM app_usage WHERE date = $1 ORDER BY minutes DESC", today)
        for a in apps:
            if a["app"] in ignore:               # launcher/system excluded from the timeline too
                continue
            name = demo_app_label(a["app"], seen) if demo else friendly_app_name(a["app"], names)
            out["allday"].append({"kind": "app", "label": name, "value": float(a["minutes"])})
        await conn.close()
    except Exception as e:
        logger.error(f"timeline error: {e}")
        out["error"] = str(e)
    return out

# ---------------------------------------------------------------------------
# Command-center dashboard data (Session 35). ONE read-only endpoint serving the v8
# mockup's data contract from Ghost's real state. Cached in Redis ~25s so the 30-60s
# poll never hammers Habitica's 30-req/min limit (habits + todos + user = 3 calls).
# Fields that aren't natively real are DERIVED and flagged in `_meta.derived`, so the
# frontend/reader can tell approximations from ground truth.
# ---------------------------------------------------------------------------
CALORIE_TARGET = int(os.environ.get("GHOST_CALORIE_TARGET", "0") or 0)  # 0 => not set
TIER2_RESEND_SECONDS = 25 * 60  # mirrors scheduler.TIER2_RESEND_SECONDS (for eta only)

async def _escalation_topic() -> dict | None:
    """Hottest active escalation as {topic, stage, nextResendEta} or None. Read-only."""
    best = None
    try:
        r = redis_conn()
        for k in await r.keys("escalation:*"):
            raw = await r.get(k)
            if not raw:
                continue
            try:
                st = json.loads(raw)
            except Exception:
                continue
            stage = int(st.get("stage", 0) or 0)
            heat = _STAGE_HEAT.get(stage, float(stage))
            if best is None or heat > best[0]:
                topic = k.split(":", 1)[1]
                dormant = (st.get("crude_sends", 0) >= 2 and st.get("winddown_sends", 0) >= 1)
                eta = None
                if not dormant and st.get("last_sent"):
                    eta = max(0, int(st["last_sent"] + TIER2_RESEND_SECONDS - datetime.now(LOCAL_TZ).timestamp()))
                best = (heat, {"topic": topic, "stage": stage, "nextResendEta": eta})
        await r.aclose()
    except Exception as e:
        logger.error(f"escalation topic read error: {e}")
    return best[1] if best else None

async def _habitica_health() -> dict:
    """Session 42: LIVE Habitica ping so the Integrations badge reflects a genuine
    just-checked API response — not the up-to-2h-old health_checks row. (On 20/08 the
    badge showed 'habitica: ok' from the stale 15:31 self-diagnosis while Lea was mid-
    incident; a status badge claiming 'ok' is a claim like any other and must be live.)"""
    uid = os.environ.get("HABITICA_USER_ID"); tok = os.environ.get("HABITICA_API_TOKEN")
    if not (uid and tok):
        return {"service": "habitica", "status": "unknown", "detail": "no creds"}
    try:
        async with httpx.AsyncClient() as client:
            r = await client.get("https://habitica.com/api/v3/user?userFields=_id",
                                 headers={"x-api-user": uid, "x-api-key": tok,
                                          "x-client": f"{uid}-ghost-hivequeen"}, timeout=6)
        ok = r.status_code == 200
        return {"service": "habitica", "status": "ok" if ok else "fail",
                "detail": "live" if ok else f"http {r.status_code}"}
    except Exception as e:
        return {"service": "habitica", "status": "fail", "detail": f"live: {str(e)[:50]}"}

async def _cephalopod_health() -> dict:
    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(f"{CEPHALOPOD_URL}/health", timeout=5)
        ok = r.status_code == 200
        return {"service": "cephalopod", "status": "ok" if ok else "fail",
                "detail": "" if ok else f"http {r.status_code}"}
    except Exception as e:
        return {"service": "cephalopod", "status": "fail", "detail": str(e)[:60]}

# Demo-mode placeholders for the command center (Session 54). Structurally identical to the
# real payload, scaled to look lived-in (not empty), never derived from real data.
DEMO_VITALS = {"hp": 42.0, "hpMax": 50, "mp": 28.0, "mpMax": 40, "xp": 130.0,
               "xpMax": 250, "level": 14, "class": "warrior", "gold": 185.0}
DEMO_NUTRITION = {"caloriesLogged": 1850, "caloriesTarget": 2600,
                  "macros": {"protein": 95, "carbs": 210, "fat": 62, "fiber": 22},
                  "mealsLoggedToday": 3}

def _mask_command_center(data: dict) -> dict:
    """Return a deep-copied, demo-safe command-center payload: real names/text/figures/
    health numbers/chat content replaced with placeholders, structure + done/priority
    distribution preserved so it still reads as a lived-in dashboard (Session 54).
    Server-side — the real data never reaches the browser in demo mode."""
    d = copy.deepcopy(data)
    for i, h in enumerate(d.get("habits") or []):
        if isinstance(h, dict):
            h["name"] = f"Habit {chr(ord('A') + (i % 26))}"
    for i, t in enumerate(d.get("todos") or []):
        if isinstance(t, dict):
            t["text"] = f"Sample task {i + 1}"
    if d.get("vitals"):
        d["vitals"] = dict(DEMO_VITALS)
    # S70: budget key removed from the command-center (conversational money pipeline retired).
    if d.get("nutrition") is not None:
        d["nutrition"] = copy.deepcopy(DEMO_NUTRITION)
    for c in d.get("selfCheck") or []:
        if isinstance(c, dict):
            c["detail"] = ""            # scrub any host/error string; keep service+status
    for p in d.get("proposals") or []:
        if isinstance(p, dict):
            p["file"] = "example_module.py"
            p["explanation"] = "Sample proposed change"
    esc = d.get("escalation")
    if isinstance(esc, dict) and esc.get("topic"):
        esc["topic"] = str(esc["topic"]).split(":")[0]   # drop any board:<real item> tail
    for ev in d.get("timeline") or []:
        if isinstance(ev, dict):
            ev["label"] = "Ghost check-in" if ev.get("type") == "nudge" else "Quick note"
    d["errors"] = []
    d["demo"] = True
    return d

@app.get("/api/command-center")
async def api_command_center(request: Request):
    """Read-only. Serves the v8 dashboard data contract from real Postgres/Redis/
    Habitica/FatSecret state. Cached 25s (the real payload; demo masking is applied on
    egress to a deep copy, so both modes share one cache and demo never ships real data)."""
    demo = is_demo(request)
    now = datetime.now(LOCAL_TZ)
    try:
        r = redis_conn()
        cached = await r.get("command_center")
        await r.aclose()
        if cached:
            data = json.loads(cached)
            return _mask_command_center(data) if demo else data
    except Exception:
        pass
    out = {"time": now.strftime("%A %d %B %Y, %I:%M %p").replace(" 0", " "), "errors": [],
           "_meta": {"derived": {
               "habits.streak/doneToday/atRisk": "approximated from Habitica history/counterUp/value — habits have no native streak/done/at-risk",
               "nutrition.caloriesTarget": "from GHOST_CALORIE_TARGET env (0 = not set) — not available from FatSecret",
           }}}
    # Habitica: habits, todos, vitals
    try:
        out["habits"] = await habitica.get_habits_detailed()
    except Exception as e:
        out["errors"].append(f"habits: {e}"); out["habits"] = []
    try:
        out["todos"] = await habitica.get_todos_detailed()
    except Exception as e:
        out["errors"].append(f"todos: {e}"); out["todos"] = []
    try:
        out["vitals"] = await habitica.get_user_stats()
    except Exception as e:
        out["errors"].append(f"vitals: {e}"); out["vitals"] = {}
    # S70: conversational money pipeline removed — spending is CSV-only (Bills dashboard / spending
    # card). No budget-vs-limit block from expenses/budget_categories here anymore.
    # Nutrition (FatSecret)
    try:
        import fatsecret
        f = await fatsecret.get_today_summary()
        if f is None:
            out["nutrition"] = None
        else:
            out["nutrition"] = {
                "caloriesLogged": round(f["calories"]), "caloriesTarget": CALORIE_TARGET or None,
                "macros": {"protein": round(f["protein"]), "carbs": round(f["carbs"]),
                           "fat": round(f["fat"]), "fiber": round(f.get("fiber", 0))},
                "mealsLoggedToday": len(f.get("groups", [])) or f.get("entries", 0)}
    except Exception as e:
        out["errors"].append(f"nutrition: {e}"); out["nutrition"] = None
    # Self-check (health_checks table) + live Cephalopod ping
    try:
        conn = await pg_conn()
        rows = await conn.fetch("""
            SELECT DISTINCT ON (service) service, status, detail, checked_at
            FROM health_checks WHERE service IN ('telegram','postgres','redis','fatsecret')
            ORDER BY service, checked_at DESC
        """)
        await conn.close()
        # Session 42: habitica is now a LIVE ping (see below), removed from the table set.
        # For the rest, flag staleness — the self-diagnosis runs every 2h, so a row older
        # than ~2.5h means the checker itself hasn't run; don't present a stale 'ok' as current.
        checks = []
        for r in rows:
            status, detail = r["status"], (r["detail"] or "")
            if r["checked_at"] is not None:
                age_min = (now - r["checked_at"].astimezone(LOCAL_TZ)).total_seconds() / 60
                if age_min > 150:
                    detail = f"stale — last {r['status']} {int(age_min)}m ago"
                    status = "stale"
            checks.append({"service": r["service"], "status": status, "detail": detail})
        checks.append(await _habitica_health())   # live, authoritative — not the 2h table row
        checks.append(await _cephalopod_health())
        out["selfCheck"] = checks
    except Exception as e:
        out["errors"].append(f"selfCheck: {e}"); out["selfCheck"] = []
    # Self-code proposals (Session 42: restore to the v8 dashboard). S78: shared _open_proposals
    # with /api/data (was a verbatim copy). demo=False here — demo masking is applied on egress.
    try:
        out["proposals"] = await _open_proposals(demo=False)
    except Exception as e:
        out["errors"].append(f"proposals: {e}"); out["proposals"] = []
    # Escalation (Redis)
    try:
        out["escalation"] = await _escalation_topic()
    except Exception as e:
        out["errors"].append(f"escalation: {e}"); out["escalation"] = None
    # Timeline — today's message_log events, flat {time,type,label}
    try:
        conn = await pg_conn()
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        msgs = await conn.fetch(
            "SELECT ts, source, type, content FROM message_log WHERE ts >= $1 ORDER BY ts", day_start)
        await conn.close()
        out["timeline"] = [{"time": m["ts"].astimezone(LOCAL_TZ).strftime("%H:%M"),
                            "type": "message" if m["type"] == "chat" else "nudge",
                            "label": (m["content"] or "")[:120]} for m in msgs]
    except Exception as e:
        out["errors"].append(f"timeline: {e}"); out["timeline"] = []
    try:
        r = redis_conn()
        await r.set("command_center", json.dumps(out), ex=25)
        await r.aclose()
    except Exception:
        pass
    return _mask_command_center(out) if demo else out


# ===========================================================================
# MOMENTUM SIGNAL (Session 57, 02/09/2026)
# ---------------------------------------------------------------------------
# "Am I actually moving, over time" — one number, not an analytics dashboard.
# momentum_events captures one row per completion (from the Habitica sync and the
# Quest toggle — the two write hooks). /api/momentum returns trailing-window
# closed (completions) vs open (still-incomplete items due in the window), overall
# and per bucket, plus a per-day sparkline. Bucket crosswalk approved by Lea (S57);
# NB the routine/meal/habit board items aren't anchored in the sync files, so
# Maintenance/Habits stay sparse — this reflects the two chosen hooks, by design.
# `derive_bucket` is duplicated in scheduler/habitica_sync.py (no module both the
# scheduler and dashboard import); keep the two copies in step.
# ===========================================================================
_BUCKET_PROJECT = {
    "sidequesting": "Sidequests",
    "darkandwild": "Business", "website": "Business",
    "writing": "Creative/Relax", "novel": "Creative/Relax",
    "ghost": "Work", "hob": "Work", "rook": "Work", "cephalopod": "Work",
    "offgoogle": "Work", "3dprinting": "Work", "vinylcutter": "Work",
}
MOMENTUM_BUCKETS = ["Maintenance", "Habits", "Work", "Business", "Sidequests", "Creative/Relax", "unsorted"]

def derive_bucket(text: str, source: str = "", rel: str = "") -> str:
    """Map a completion to one of the six buckets (+ 'unsorted'), per the S57 crosswalk.
    An explicit #project/<tag> wins; then source/keyword/file heuristics."""
    t = (text or "").lower()
    m = re.search(r"#project/([a-z0-9_-]+)", t)
    if m and m.group(1) in _BUCKET_PROJECT:
        return _BUCKET_PROJECT[m.group(1)]
    if source == "quest":
        return "Sidequests"
    if any(w in t for w in ("morning routine", "evening routine", "bedtime", "breakfast",
                            "lunch", "dinner", "tidy", "skincare", "make bed", "supplements")):
        return "Maintenance"
    if any(w in t for w in ("exercise", "yoga", "weights", "handstand", "splits", "water", "workout", "habit")):
        return "Habits"
    if "career" in t or "job" in t or "resume" in t or "linkedin" in t:
        return "Work"
    if any(w in t for w in ("dark and wild", "tdaw", "lea the witch", "leathewitch", "shopify", "shop", "adsense", "substack")):
        return "Business"
    if any(w in t for w in ("witch", "spell", "ritual", "novel", "fanfic", "writing", "blog")):
        return "Creative/Relax"
    if "The Dark and Wild" in rel or "Website Index" in rel:
        return "Business"
    if "Money Moves" in rel:
        return "Work"
    return "unsorted"

_MOMENTUM_SCHEMA_READY = False

async def _ensure_momentum_schema(conn):
    global _MOMENTUM_SCHEMA_READY
    if _MOMENTUM_SCHEMA_READY:
        return
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS momentum_events (
            id BIGSERIAL PRIMARY KEY,
            item_id TEXT NOT NULL,
            source TEXT NOT NULL,
            bucket TEXT NOT NULL DEFAULT 'unsorted',
            completed_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE INDEX IF NOT EXISTS momentum_events_completed_at_idx ON momentum_events (completed_at);
    """)
    _MOMENTUM_SCHEMA_READY = True

async def record_momentum(item_id: str, source: str, bucket: str):
    """One completion -> one row. Never raises into the caller (a momentum miss must not
    break a real completion write). Cheap idempotent schema-ensure on first call."""
    try:
        conn = await pg_conn()
        try:
            await _ensure_momentum_schema(conn)
            await conn.execute(
                "INSERT INTO momentum_events (item_id, source, bucket) VALUES ($1,$2,$3)",
                str(item_id), source, bucket)
        finally:
            await conn.close()
    except Exception as e:
        logger.error(f"record_momentum error ({source}/{item_id}): {e}")

@app.get("/api/momentum")
async def api_momentum(request: Request, window: int = 60):
    """Trailing-window momentum: closed (completions logged in momentum_events) vs open
    (incomplete Habitica to-dos whose due date falls in the window), overall + per bucket,
    plus a per-day completions sparkline. Raw counts returned alongside the ratio so the
    number stays interpretable when open is small. Read-only; Redis-cached ~60s.

    Scope (v1, honest): 'closed' is the real movement signal (every sync/quest completion).
    'open' uses Habitica to-dos with a structured due date — dailies (no single due date),
    undated to-dos, and free-text Quest dates are NOT in 'open'. Documented, not a bug."""
    demo = is_demo(request)
    try:
        window = max(1, min(365, int(window)))
    except Exception:
        window = 60
    if demo:
        # behavioural counts are personal — placeholder, lived-in, for footage privacy
        import random as _r
        spark = [{"date": (datetime.now(LOCAL_TZ).date() - timedelta(days=window - 1 - i)).isoformat(),
                  "closed": _r.randint(0, 4)} for i in range(min(window, 30))]
        return {"window": window, "demo": True,
                "overall": {"closed": 48, "open": 12, "ratio": 0.8},
                "buckets": {b: {"closed": 8, "open": 2, "ratio": 0.8} for b in MOMENTUM_BUCKETS[:6]},
                "sparkline": spark}
    ck = f"momentum:{window}"
    try:
        r = redis_conn(); cached = await r.get(ck); await r.aclose()
        if cached:
            return json.loads(cached)
    except Exception:
        pass
    now = datetime.now(LOCAL_TZ)
    since = now - timedelta(days=window)
    out = {"window": window, "overall": {"closed": 0, "open": 0, "ratio": None},
           "buckets": {b: {"closed": 0, "open": 0, "ratio": None} for b in MOMENTUM_BUCKETS},
           "sparkline": [], "generated_at": now.isoformat(),
           "_note": "closed = logged completions (sync+quest); open = incomplete Habitica to-dos due in window"}
    # closed (from momentum_events)
    try:
        conn = await pg_conn()
        try:
            await _ensure_momentum_schema(conn)
            rows = await conn.fetch(
                "SELECT bucket, COUNT(*) AS n FROM momentum_events WHERE completed_at >= $1 GROUP BY bucket", since)
            for row in rows:
                b = row["bucket"] if row["bucket"] in out["buckets"] else "unsorted"
                out["buckets"][b]["closed"] += row["n"]
                out["overall"]["closed"] += row["n"]
            spark = await conn.fetch(
                "SELECT (completed_at AT TIME ZONE $2)::date AS d, COUNT(*) AS n "
                "FROM momentum_events WHERE completed_at >= $1 GROUP BY d ORDER BY d", since, str(LOCAL_TZ))
            out["sparkline"] = [{"date": s["d"].isoformat(), "closed": s["n"]} for s in spark]
        finally:
            await conn.close()
    except Exception as e:
        out.setdefault("errors", []).append(f"closed: {e}")
    # open (S78: NATIVE outstanding-due work, replacing the old Habitica-todo open side which was
    # empty here — the S76 moth-caveat: the ratio read a false 1.0 not because activity was high but
    # because nothing populated open). Now: native habits due today but not done, plus native todos
    # (quest-flagged included) that carry a due date on/before today and aren't done. `closed`
    # (completions over the window) is unchanged; buckets come straight from each native item.
    try:
        uid = native.DEFAULT_USER_ID
        nconn = await pg_conn()
        try:
            nhabits = await native.list_habits(uid, conn=nconn)
            ntodos = await native.list_todos(uid, include_done=False, conn=nconn)
        finally:
            await nconn.close()
        today = now.date()
        for h in nhabits:
            if h.get("due_today") and not h.get("done_today"):
                b = h["bucket"] if h["bucket"] in out["buckets"] else "unsorted"
                out["buckets"][b]["open"] += 1
                out["overall"]["open"] += 1
        for t in ntodos:
            if t.get("done") or not t.get("due_date"):
                continue
            try:
                due = date.fromisoformat(t["due_date"])
            except Exception:
                continue
            if due <= today:                      # due or overdue = outstanding
                b = t["bucket"] if t["bucket"] in out["buckets"] else "unsorted"
                out["buckets"][b]["open"] += 1
                out["overall"]["open"] += 1
    except Exception as e:
        out.setdefault("errors", []).append(f"open: {e}")
    # ratios
    def _ratio(c, o):
        return round(c / (c + o), 3) if (c + o) > 0 else None
    out["overall"]["ratio"] = _ratio(out["overall"]["closed"], out["overall"]["open"])
    for b in out["buckets"]:
        out["buckets"][b]["ratio"] = _ratio(out["buckets"][b]["closed"], out["buckets"][b]["open"])
    try:
        r = redis_conn(); await r.set(ck, json.dumps(out), ex=60); await r.aclose()
    except Exception:
        pass
    return out


# ===========================================================================
# GHOST 2.0 — NATIVE HABITS & TODOS (Phase 1)
# ---------------------------------------------------------------------------
# These endpoints are the dashboard's tap/check/create controls for the native
# habit/todo system. They call the SAME native.py functions the Telegram pipeline
# uses (bot.process_message -> native.route_message / native.tap_habit / ...), so the
# two surfaces are one code path, not parallel logic. Deterministic writes, no LLM.
# Reads are live (no cache) so a tap reflects immediately. Runs alongside Habitica.
# ===========================================================================
def _native_uid(request: Request) -> int:
    return OWNER_ID or native.DEFAULT_USER_ID


@app.get("/api/habits")
async def api_habits(request: Request):
    try:
        return {"habits": await native.list_habits(_native_uid(request))}
    except Exception as e:
        logger.error(f"/api/habits error: {e}")
        return JSONResponse({"habits": [], "error": str(e)}, status_code=500)


@app.get("/api/todos")
async def api_todos(request: Request, include_done: int = 1):
    try:
        return {"todos": await native.list_todos(_native_uid(request), include_done=bool(include_done))}
    except Exception as e:
        logger.error(f"/api/todos error: {e}")
        return JSONResponse({"todos": [], "error": str(e)}, status_code=500)


@app.get("/api/level")
async def api_level(request: Request):
    try:
        return await native.level_stats(_native_uid(request))
    except Exception as e:
        logger.error(f"/api/level error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/currency")
async def api_currency(request: Request):
    """S76: the running currency balance + recent events (criticals shown distinctly)."""
    try:
        return await native.currency_summary()
    except Exception as e:
        logger.error(f"/api/currency error: {e}")
        return JSONResponse({"balance": 0, "error": str(e)}, status_code=500)


@app.get("/api/badguys")
async def api_badguys(request: Request):
    """S76: which 'bad guy' threshold checks are currently active (edge-state set by the scheduler
    in Redis). Read-only; surfaces the same signal on the dashboard as the one-time Telegram notice."""
    labels = {"burnout": "Burnout", "decision_fatigue": "Decision fatigue", "doomscrolling": "Doomscrolling"}
    active = []
    try:
        r = redis_conn()
        for key, label in labels.items():
            if await r.get(f"badguy:{key}:active"):
                active.append({"key": key, "label": label})
        await r.aclose()
    except Exception as e:
        logger.error(f"/api/badguys error: {e}")
    return {"active": active}


@app.get("/api/arc")
async def api_arc(request: Request):
    """S76: the active story arc + its live progress (currency earned + completions in range)."""
    try:
        return {"arc": await native.active_arc(_native_uid(request))}
    except Exception as e:
        logger.error(f"/api/arc error: {e}")
        return JSONResponse({"arc": None, "error": str(e)}, status_code=500)


@app.post("/api/arc")
async def api_arc_start(request: Request):
    """S76: start a story arc (name + optional length in days, default 14). Dashboard-driven."""
    if is_demo(request):
        return JSONResponse({"ok": False, "error": "demo mode is read-only"}, status_code=403)
    data = await _json_body(request)
    name = str(data.get("name", "")).strip()
    if not name:
        return {"ok": False, "error": "name required"}
    try:
        days = int(data.get("days") or native.ARC_DEFAULT_DAYS)
    except (TypeError, ValueError):
        days = native.ARC_DEFAULT_DAYS
    try:
        aid = await native.start_arc(name, days=days, user_id=_native_uid(request))
        return {"ok": bool(aid), "id": aid}
    except Exception as e:
        logger.error(f"/api/arc start error: {e}")
        return {"ok": False, "error": str(e)}


async def _json_body(request: Request) -> dict:
    try:
        return await request.json()
    except Exception:
        return {}


@app.post("/api/habits/tap")
async def api_habit_tap(request: Request):
    body = await _json_body(request)
    try:
        res = await native.tap_habit(int(body.get("id")), body.get("direction", "up"))
        await _bust_native()
        return res
    except Exception as e:
        logger.error(f"/api/habits/tap error: {e}")
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.post("/api/habits/checklist")
async def api_habit_checklist(request: Request):
    body = await _json_body(request)
    try:
        _r = await native.set_habit_checklist_item(int(body.get("id")), bool(body.get("done", True)))
        await _bust_native()
        return _r
    except Exception as e:
        logger.error(f"/api/habits/checklist error: {e}")
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.post("/api/habits/checklist/add")
async def api_habit_checklist_add(request: Request):
    body = await _json_body(request)
    try:
        _r = await native.add_habit_checklist_item(int(body.get("id")), body.get("text", ""))
        await _bust_native()
        return _r
    except Exception as e:
        logger.error(f"/api/habits/checklist/add error: {e}")
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.post("/api/habits/delete")
async def api_habit_delete(request: Request):
    body = await _json_body(request)
    try:
        _r = await native.archive_habit(int(body.get("id")))
        await _bust_native()
        return _r
    except Exception as e:
        logger.error(f"/api/habits/delete error: {e}")
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.post("/api/todos/delete")
async def api_todo_delete(request: Request):
    body = await _json_body(request)
    try:
        _r = await native.archive_todo(int(body.get("id")))
        await _bust_native()
        return _r
    except Exception as e:
        logger.error(f"/api/todos/delete error: {e}")
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.post("/api/habits/create")
async def api_habit_create(request: Request):
    body = await _json_body(request)
    try:
        hid = await native.create_habit(
            body.get("name", ""), field_type=body.get("field_type", "tickbox"),
            frequency_kind=body.get("frequency_kind", "weekdays"),
            frequency_days=body.get("frequency_days"), frequency_count=body.get("frequency_count"),
            bucket=body.get("bucket"), streak_rule=body.get("streak_rule", "net_positive"),
            streak_min_taps=body.get("streak_min_taps"), checklist=body.get("checklist"),
            user_id=_native_uid(request))
        await _bust_native()
        return {"ok": hid is not None, "id": hid}
    except Exception as e:
        logger.error(f"/api/habits/create error: {e}")
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.post("/api/todos/complete")
async def api_todo_complete(request: Request):
    body = await _json_body(request)
    try:
        _r = await native.complete_todo(int(body.get("id")), bool(body.get("done", True)))
        await _bust_native()
        return _r
    except Exception as e:
        logger.error(f"/api/todos/complete error: {e}")
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.post("/api/todos/checklist")
async def api_todo_checklist(request: Request):
    body = await _json_body(request)
    try:
        _r = await native.set_todo_checklist_item(int(body.get("id")), bool(body.get("done", True)))
        await _bust_native()
        return _r
    except Exception as e:
        logger.error(f"/api/todos/checklist error: {e}")
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.post("/api/todos/quest")
async def api_todo_quest(request: Request):
    """S78: flag/unflag an existing todo as a quest (per-row toggle — the todo 'edit' path)."""
    body = await _json_body(request)
    try:
        _r = await native.set_todo_quest(int(body.get("id")), bool(body.get("is_quest", True)))
        await _bust_native()
        return _r
    except Exception as e:
        logger.error(f"/api/todos/quest error: {e}")
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.post("/api/todos/checklist/add")
async def api_todo_checklist_add(request: Request):
    """S77: add an item to a checklist todo — mirrors /api/habits/checklist/add."""
    body = await _json_body(request)
    try:
        _r = await native.add_todo_checklist_item(int(body.get("id")), body.get("text", ""))
        await _bust_native()
        return _r
    except Exception as e:
        logger.error(f"/api/todos/checklist/add error: {e}")
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.post("/api/todos/create")
async def api_todo_create(request: Request):
    body = await _json_body(request)
    try:
        tid = await native.create_todo(
            body.get("text", ""), kind=body.get("kind", "tickbox"),
            priority=body.get("priority", "med"), due_date=body.get("due_date"),
            bucket=body.get("bucket"), checklist=body.get("checklist"),
            is_quest=bool(body.get("is_quest")),
            user_id=_native_uid(request))
        await _bust_native()
        return {"ok": tid is not None, "id": tid}
    except Exception as e:
        logger.error(f"/api/todos/create error: {e}")
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


# ===========================================================================
# QUEST ENGINE ↔ Obsidian sync (Session 54, 01/09/2026)
# ---------------------------------------------------------------------------
# ghost-quest-log.html reflects the checklists in `Areas/Quest Engine.md`, whose
# lines carry hidden <!--quest:ID--> anchors (same pattern as the live Habitica sync
# in Areas/Habitica — Goals & Habits.md). Two routes: read the checked-state map, and
# flip a single line's checkbox. Read fresh on every toggle (never cached) — Lea may be
# hand-editing the note in Obsidian concurrently; only the one anchored line is touched,
# every other byte (incl. the anchor comment and line endings) preserved. The pod runs
# as root, so a write's owner/mode is restored afterwards (reusing bot._preserve_vault_owner)
# so Obsidian on Lea's uid can still save the note.
# ===========================================================================
QUEST_FILE_REL = "Areas/Quest Engine.md"
QUEST_LINE_RE = re.compile(r"^- \[([ xX])\].*<!--quest:([a-zA-Z0-9\-]+)-->\s*$")
QUEST_CHECKBOX_RE = re.compile(r"^(- \[)([ xX])(\])")

@app.get("/api/quest-engine/state")
async def quest_state():
    """Read-only: {anchor_id: checked_bool} for every quest-anchored line. Non-200 on a
    read failure so the page falls back to its per-browser localStorage rather than
    treating an empty map as 'everything unchecked'."""
    path = os.path.join(bot.VAULT_PATH, QUEST_FILE_REL)
    try:
        content = open(path, encoding="utf-8").read()
    except OSError as e:
        logger.error(f"quest state read error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)
    out = {}
    for line in content.split("\n"):
        m = QUEST_LINE_RE.match(line)
        if m:
            out[m.group(2)] = m.group(1).lower() == "x"
    return out

@app.post("/api/quest-engine/toggle")
async def quest_toggle(request: Request):
    """Flip one anchored line's [ ]/[x] to match {id, checked}. Re-reads the file fresh
    (no cache), touches only the matching line, preserves every other line + line endings +
    the anchor comment. 404 if no line carries that anchor."""
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "bad request"}, status_code=400)
    if is_demo(request):
        return JSONResponse({"ok": False, "error": "demo mode is read-only"}, status_code=403)
    qid = str(data.get("id", "")).strip()
    checked = bool(data.get("checked"))
    if not qid:
        return JSONResponse({"ok": False, "error": "no id"}, status_code=400)
    path = os.path.join(bot.VAULT_PATH, QUEST_FILE_REL)
    try:
        content = open(path, encoding="utf-8").read()
    except OSError as e:
        logger.error(f"quest toggle read error: {e}")
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
    newchar = "x" if checked else " "
    lines = content.split("\n")
    found = False
    matched_raw = ""
    for i, line in enumerate(lines):
        m = QUEST_LINE_RE.match(line)
        if m and m.group(2) == qid:
            matched_raw = line  # capture before flip, for momentum bucket derivation
            lines[i] = QUEST_CHECKBOX_RE.sub(lambda mm: mm.group(1) + newchar + mm.group(3), line, count=1)
            found = True
            break
    if not found:
        return JSONResponse({"ok": False, "error": f"no line with anchor {qid}"}, status_code=404)
    try:
        st = os.stat(path)
        tmp = path + ".questtmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        os.replace(tmp, path)
        try:
            bot._preserve_vault_owner(path, st)  # pod is root; keep the note Lea-owned
        except Exception as e:
            logger.error(f"quest toggle chown-back failed: {e}")
    except OSError as e:
        logger.error(f"quest toggle write error: {e}")
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
    if checked:  # a completion -> momentum event (never fires on un-tick)
        await record_momentum(qid, "quest", derive_bucket(matched_raw, "quest"))
        # S92: XP/currency retired — no award on completion (momentum unchanged).
    logger.info(f"quest toggle: {qid} -> {'x' if checked else ' '}")
    return {"ok": True, "id": qid, "checked": checked}


# ===========================================================================
# BILLS SCREEN (Session 46, 25/08/2026)
# ---------------------------------------------------------------------------
# Recurring-bills tracker: full CRUD + server-side calculated views (monthly
# total, due-soon 7/14/30, due-before-next-payday, set-aside-per-cycle with
# sinking-fund handling for quarterly/annual bills, overdue flags, annual total).
#
# DATA BOUNDARY (hard requirement, non-negotiable): all bill data lives ONLY in
# Ghost's local Postgres on hivequeen. It is NEVER sent to Claude, Cowork, or any
# cloud service. This screen is pure CRUD + arithmetic and calls NO LLM at all.
# If a natural-language feature is ever added here, it must run against the LOCAL
# Ollama instance only, never a cloud model. Nothing in this block makes an
# outbound network call.
# ===========================================================================

BILL_FREQUENCIES = ("weekly", "fortnightly", "monthly", "quarterly", "annual")

# Monthly-equivalent multiplier per frequency (normalise everything to monthly).
_FREQ_TO_MONTHLY = {
    "weekly": 52.0 / 12.0,
    "fortnightly": 26.0 / 12.0,
    "monthly": 1.0,
    "quarterly": 1.0 / 3.0,
    "annual": 1.0 / 12.0,
}
# Occurrences per year (for the annual total — computed directly, not monthly*12,
# to avoid rounding drift).
_FREQ_PER_YEAR = {"weekly": 52, "fortnightly": 26, "monthly": 12, "quarterly": 4, "annual": 1}

# Pay cadence -> pays-per-month, for the set-aside-per-cycle math.
PAY_FREQUENCIES = ("weekly", "fortnightly", "monthly")
_PAYS_PER_MONTH = {"weekly": 52.0 / 12.0, "fortnightly": 26.0 / 12.0, "monthly": 1.0}

# Bill categories. The base buckets MIRROR csv_spending_summary.py's CATEGORY_RULES
# keys (minus income/cash_withdrawal, which aren't recurring bills) so the bills side
# ("expected") reconciles cleanly with CSV-categorised actuals later. rent and
# insurance are added here because recurring bills need them and the CSV categoriser
# doesn't have them yet — add matching keyword rules to csv_spending_summary.py when
# wiring reconciliation. Stored value = the snake_case key; label is display-only.
BILL_CATEGORIES = [
    {"key": "bills_utilities", "label": "Utilities"},
    {"key": "rent", "label": "Rent / Mortgage"},
    {"key": "insurance", "label": "Insurance"},
    {"key": "subscriptions", "label": "Subscriptions"},
    {"key": "debt_repayment", "label": "Debt repayment"},
    {"key": "transport", "label": "Transport"},
    {"key": "health", "label": "Health"},
    {"key": "groceries", "label": "Groceries"},
    {"key": "business_tdaw", "label": "Business (TDAW)"},
    {"key": "dining_delivery", "label": "Dining / Delivery"},
    {"key": "uncategorised", "label": "Other"},
]
_BILL_CATEGORY_KEYS = {c["key"] for c in BILL_CATEGORIES}
_BILL_CAT_LABELS = {c["key"]: c["label"] for c in BILL_CATEGORIES}
BILL_CURRENCY = "AUD"

# S74: every bill is tagged fixed (same charge every time — rent, a subscription) or usage-based
# (varies — electricity/gas/water). A usage bill's amount is a reference/last-known figure, not a
# firm "amount due", so it's LABELLED an estimate in the calculated views. Default 'fixed' (the
# safe assumption for the pre-S74 manual bills — a firm expected charge).
BILL_TYPES = ("fixed", "usage")

# Recurrence detection (Task 1) — surface monthly-ish recurring OUTFLOWS from imported transactions
# as candidate bills. Tuning kept here, in one place, so the heuristic is legible + adjustable.
BILL_MIN_MONTHS = 3          # need >=3 distinct months of the pattern before suggesting it
BILL_GAP_MIN, BILL_GAP_MAX = 25, 35   # ~monthly spacing between month-representatives (days)
BILL_MAX_PER_MONTH = 1.5     # avg hits/active-month above this = frequent spending, not a bill
BILL_FIXED_SPREAD = 0.05     # (max-min)/mean <= 5% across months -> suggest 'fixed', else 'usage'

# Demo/footage bills (parallels DEMO_BUDGET) — served when ?demo=1 so real figures
# never reach the browser during screen recording. Writes are refused in demo mode.
DEMO_BILLS = [
    {"name": "Electricity", "amount": 240.0, "frequency": "quarterly", "day_of_month": None,
     "category": "bills_utilities", "autopay": True, "payer": "", "notes": "", "bill_type": "usage"},
    {"name": "Rent", "amount": 480.0, "frequency": "fortnightly", "day_of_month": None,
     "category": "rent", "autopay": False, "payer": "", "notes": ""},
    {"name": "Streaming", "amount": 18.0, "frequency": "monthly", "day_of_month": 14,
     "category": "subscriptions", "autopay": True, "payer": "", "notes": ""},
]

_BILLS_SCHEMA_READY = False


async def _ensure_bills_schema(conn):
    """Idempotent create of the bills tables. Runs once per process (cheap CREATE
    TABLE IF NOT EXISTS); kept in-code so no separate migration step is needed."""
    global _BILLS_SCHEMA_READY
    if _BILLS_SCHEMA_READY:
        return
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS bills (
            id             SERIAL PRIMARY KEY,
            name           TEXT NOT NULL,
            amount         NUMERIC(10,2) NOT NULL,
            frequency      TEXT NOT NULL CHECK (frequency IN
                             ('weekly','fortnightly','monthly','quarterly','annual')),
            due_date       DATE,
            day_of_month   INTEGER CHECK (day_of_month BETWEEN 1 AND 31),
            category       TEXT,
            autopay        BOOLEAN NOT NULL DEFAULT FALSE,
            payer          TEXT,
            notes          TEXT,
            last_paid_date DATE,
            created_at     TIMESTAMPTZ DEFAULT now(),
            updated_at     TIMESTAMPTZ DEFAULT now()
        )
    """)
    # S74: fixed/usage tag on every bill; source_pattern links a bill to the recurring transaction
    # merchant it was detected from (so imports can refresh usage amounts / flag fixed mismatches);
    # price_flag is a gentle human note when a linked FIXED bill's latest charge differs. Added
    # idempotently so existing bills gain the columns with no migration step.
    await conn.execute(
        "ALTER TABLE bills ADD COLUMN IF NOT EXISTS bill_type TEXT NOT NULL DEFAULT 'fixed'")
    await conn.execute("ALTER TABLE bills ADD COLUMN IF NOT EXISTS source_pattern TEXT")
    await conn.execute("ALTER TABLE bills ADD COLUMN IF NOT EXISTS price_flag TEXT")
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS bill_settings (
            id             INTEGER PRIMARY KEY DEFAULT 1,
            pay_frequency  TEXT NOT NULL DEFAULT 'fortnightly',
            next_pay_date  DATE,
            currency       TEXT NOT NULL DEFAULT 'AUD',
            updated_at     TIMESTAMPTZ DEFAULT now(),
            CONSTRAINT bill_settings_singleton CHECK (id = 1)
        )
    """)
    # S87: subscriptions — a lighter recurring-spend tracker alongside bills (Netflix/Spotify/etc.).
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS subscriptions (
            id             SERIAL PRIMARY KEY,
            name           TEXT NOT NULL,
            amount         NUMERIC(10,2) NOT NULL,
            cycle          TEXT NOT NULL DEFAULT 'monthly'
                             CHECK (cycle IN ('weekly','fortnightly','monthly','quarterly','annual')),
            next_renewal   DATE,
            category       TEXT,
            active         BOOLEAN NOT NULL DEFAULT TRUE,
            notes          TEXT,
            created_at     TIMESTAMPTZ DEFAULT now(),
            updated_at     TIMESTAMPTZ DEFAULT now()
        )
    """)
    # S88: link a subscription to the detected recurring pattern it was added from, so that pattern
    # stops surfacing as a candidate (mirrors bills.source_pattern).
    await conn.execute("ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS source_pattern TEXT")
    # S88: candidates Lea explicitly removed — never suggested again.
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS dismissed_bill_candidates (
            source_pattern TEXT PRIMARY KEY,
            created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    _BILLS_SCHEMA_READY = True

# S87: how many times each cycle occurs per year, for the monthly-equivalent roll-up.
_CYCLE_PER_YEAR = {"weekly": 52.0, "fortnightly": 26.0, "monthly": 12.0, "quarterly": 4.0, "annual": 1.0}


# --- date maths (stdlib only; no dateutil dependency in the slim image) --------
def _days_in_month(y, m):
    if m == 12:
        return 31
    return (date(y, m + 1, 1) - timedelta(days=1)).day

def _add_months(d, n):
    m0 = d.month - 1 + n
    y = d.year + m0 // 12
    m = m0 % 12 + 1
    return date(y, m, min(d.day, _days_in_month(y, m)))

def _step_occurrence(d, freq, n):
    if freq == "weekly":
        return d + timedelta(days=7 * n)
    if freq == "fortnightly":
        return d + timedelta(days=14 * n)
    if freq == "monthly":
        return _add_months(d, n)
    if freq == "quarterly":
        return _add_months(d, 3 * n)
    if freq == "annual":
        return _add_months(d, 12 * n)
    return d

def _bill_anchor(due_date, day_of_month, today):
    if due_date is not None:
        return due_date
    if day_of_month:
        return date(today.year, today.month, min(day_of_month, _days_in_month(today.year, today.month)))
    return None

def _next_due(anchor, freq, today):
    """First occurrence on/after `today`, given a periodic anchor."""
    if anchor is None:
        return None
    d = anchor
    while d < today:                                  # anchor in the past -> roll forward
        d = _step_occurrence(d, freq, 1)
    while _step_occurrence(d, freq, -1) >= today:      # anchor in the future -> pull to earliest >= today
        d = _step_occurrence(d, freq, -1)
    return d

def _bill_cat_label(key):
    return _BILL_CAT_LABELS.get(key, (key or "Other"))


def _compute_bill(row, today):
    """Attach the derived fields (next due, monthly/annual equivalent, overdue) to a
    raw bill row. Pure arithmetic."""
    freq = row["frequency"]
    amount = float(row["amount"])
    dom = row["day_of_month"]
    due_date = row["due_date"]
    last_paid = row["last_paid_date"]
    autopay = bool(row["autopay"])
    anchor = _bill_anchor(due_date, dom, today)
    nd = _next_due(anchor, freq, today)
    prev_due = _step_occurrence(nd, freq, -1) if nd is not None else None
    # Overdue: a past occurrence exists that last_paid_date doesn't cover, and the bill
    # isn't on autopay (autopay bills settle themselves). "Past its due date and not
    # marked paid", per the spec. A brand-new bill with an unrecorded past occurrence
    # flags until Lea marks it paid / sets last_paid_date — intended, not a false alarm.
    overdue = False
    if prev_due is not None and prev_due < today and not autopay:
        if last_paid is None or last_paid < prev_due:
            overdue = True
    # S74: usage-based bills carry a reference amount, not a firm charge -> flagged as an estimate so
    # the calculated views can label their contribution. (Demo rows are dicts carrying the same keys.)
    bill_type = row["bill_type"] or "fixed"
    source_pattern = row["source_pattern"] or None
    price_flag = row["price_flag"] or None
    return {
        "id": row["id"],
        "name": row["name"],
        "amount": round(amount, 2),
        "frequency": freq,
        "due_date": due_date.isoformat() if due_date else None,
        "day_of_month": dom,
        "category": row["category"] or "uncategorised",
        "category_label": _bill_cat_label(row["category"]),
        "autopay": autopay,
        "payer": row["payer"] or "",
        "notes": row["notes"] or "",
        "last_paid_date": last_paid.isoformat() if last_paid else None,
        "bill_type": bill_type,
        "is_estimate": bill_type == "usage",
        "source_pattern": source_pattern,
        "price_flag": price_flag,
        "monthly_equiv": round(amount * _FREQ_TO_MONTHLY[freq], 2),
        "annual_equiv": round(amount * _FREQ_PER_YEAR[freq], 2),
        "next_due": nd.isoformat() if nd else None,
        "days_until": (nd - today).days if nd else None,
        "overdue": overdue,
    }


async def _get_bill_settings(conn):
    row = await conn.fetchrow(
        "SELECT pay_frequency, next_pay_date, currency FROM bill_settings WHERE id=1")
    if not row:
        return {"pay_frequency": "fortnightly", "next_pay_date": None, "currency": BILL_CURRENCY}
    return {
        "pay_frequency": row["pay_frequency"],
        "next_pay_date": row["next_pay_date"].isoformat() if row["next_pay_date"] else None,
        "currency": row["currency"] or BILL_CURRENCY,
    }


def _compute_summary(bills, settings, today):
    """The calculated views. Pure arithmetic over the already-computed bill list."""
    monthly_total = round(sum(b["monthly_equiv"] for b in bills), 2)
    annual_total = round(sum(b["annual_equiv"] for b in bills), 2)

    # S74: usage bills contribute a reference amount, not a firm charge -> track how much of each
    # figure is estimated so the UI can label it, without hiding it from the totals.
    estimated_monthly = round(sum(b["monthly_equiv"] for b in bills if b.get("is_estimate")), 2)
    has_estimates = any(b.get("is_estimate") for b in bills)

    def due_within(n):
        items = [b for b in bills if b["days_until"] is not None and 0 <= b["days_until"] <= n]
        est = round(sum(b["amount"] for b in items if b.get("is_estimate")), 2)
        return {"count": len(items), "total": round(sum(b["amount"] for b in items), 2),
                "estimated_total": est, "has_estimates": est != 0}

    overdue = [b for b in bills if b["overdue"]]

    pay_freq = settings.get("pay_frequency") or "fortnightly"
    pays_per_month = _PAYS_PER_MONTH.get(pay_freq, _PAYS_PER_MONTH["fortnightly"])
    set_aside = round(monthly_total / pays_per_month, 2) if pays_per_month else 0.0
    # Sinking-fund slice: the part of each cycle's set-aside that is pre-funding the
    # irregular (quarterly/annual) bills, so a big one never ambushes the budget.
    sinking_monthly = round(sum(b["monthly_equiv"] for b in bills
                                if b["frequency"] in ("quarterly", "annual")), 2)
    sinking_per_cycle = round(sinking_monthly / pays_per_month, 2) if pays_per_month else 0.0

    next_payday = None
    due_before = None
    npd = settings.get("next_pay_date")
    if npd:
        try:
            next_payday = _next_due(date.fromisoformat(npd), pay_freq, today)
        except ValueError:
            next_payday = None
    if next_payday is not None:
        items = [b for b in bills if b["next_due"] is not None
                 and today <= date.fromisoformat(b["next_due"]) < next_payday]
        due_before = {
            "next_payday": next_payday.isoformat(),
            "days_until_payday": (next_payday - today).days,
            "count": len(items),
            "total": round(sum(b["amount"] for b in items), 2),
            "estimated_total": round(sum(b["amount"] for b in items if b.get("is_estimate")), 2),
            "has_estimates": any(b.get("is_estimate") for b in items),
            "bills": sorted(items, key=lambda b: b["next_due"]),
        }

    return {
        "currency": settings.get("currency") or BILL_CURRENCY,
        "monthly_total": monthly_total,
        "annual_total": annual_total,
        "estimated_monthly": estimated_monthly,     # S74: portion of monthly_total that's a usage estimate
        "has_estimates": has_estimates,
        "bill_count": len(bills),
        "due_7": due_within(7),
        "due_14": due_within(14),
        "due_30": due_within(30),
        "overdue": {"count": len(overdue),
                    "total": round(sum(b["amount"] for b in overdue), 2),
                    "bills": sorted(overdue, key=lambda b: (b["next_due"] or ""))},
        "set_aside": {
            "pay_frequency": pay_freq,
            "pays_per_month": round(pays_per_month, 4),
            "per_cycle": set_aside,
            "estimated_per_cycle": round(estimated_monthly / pays_per_month, 2) if pays_per_month else 0.0,
            "has_estimates": has_estimates,
            "sinking_fund_per_cycle": sinking_per_cycle,
            "sinking_fund_monthly": sinking_monthly,
        },
        "due_before_payday": due_before,
    }


def _parse_bill_payload(data):
    """Validate + normalise a create/update payload. Returns (fields, error_str)."""
    name = str(data.get("name", "")).strip()
    if not name:
        return None, "name is required"
    try:
        amount = Decimal(str(data.get("amount"))).quantize(Decimal("0.01"))
    except Exception:
        return None, "amount must be a number"
    if amount < 0:
        return None, "amount can't be negative"
    freq = str(data.get("frequency", "")).strip().lower()
    if freq not in BILL_FREQUENCIES:
        return None, "frequency must be one of " + ", ".join(BILL_FREQUENCIES)
    due_date = None
    if data.get("due_date"):
        try:
            due_date = date.fromisoformat(str(data["due_date"]))
        except ValueError:
            return None, "due_date must be YYYY-MM-DD"
    day_of_month = None
    dom = data.get("day_of_month")
    if dom not in (None, "", 0, "0"):
        try:
            day_of_month = int(dom)
        except (TypeError, ValueError):
            return None, "day_of_month must be a whole number 1-31"
        if not (1 <= day_of_month <= 31):
            return None, "day_of_month must be between 1 and 31"
    if due_date is None and day_of_month is None:
        return None, "give either a due date or a day-of-month"
    category = (str(data.get("category", "") or "").strip() or "uncategorised")
    if category not in _BILL_CATEGORY_KEYS:
        category = "uncategorised"
    autopay = bool(data.get("autopay", False))
    payer = str(data.get("payer", "") or "").strip() or None
    notes = str(data.get("notes", "") or "").strip() or None
    last_paid = None
    if data.get("last_paid_date"):
        try:
            last_paid = date.fromisoformat(str(data["last_paid_date"]))
        except ValueError:
            return None, "last_paid_date must be YYYY-MM-DD"
    bill_type = str(data.get("bill_type", "fixed") or "fixed").strip().lower()
    if bill_type not in BILL_TYPES:
        bill_type = "fixed"
    source_pattern = str(data.get("source_pattern", "") or "").strip() or None
    return {
        "name": name, "amount": amount, "frequency": freq, "due_date": due_date,
        "day_of_month": day_of_month, "category": category, "autopay": autopay,
        "payer": payer, "notes": notes, "last_paid_date": last_paid,
        "bill_type": bill_type, "source_pattern": source_pattern,
    }, None


def _demo_bill_row(i, b):
    return {"id": i, "name": b["name"], "amount": b["amount"], "frequency": b["frequency"],
            "due_date": None, "day_of_month": b["day_of_month"], "category": b["category"],
            "autopay": b["autopay"], "payer": b["payer"], "notes": b["notes"],
            "last_paid_date": None, "bill_type": b.get("bill_type", "fixed"),
            "source_pattern": None, "price_flag": None}


def _changed_zero(status):
    """asyncpg execute() returns e.g. 'UPDATE 1' / 'DELETE 0' — True if 0 rows hit."""
    try:
        return status.split()[-1] == "0"
    except Exception:
        return False


def _merchant_key(description):
    """Stable merchant fingerprint for grouping recurring transactions and for linking a bill to
    its source (Task 2/4). Lowercase, keep alpha tokens >=3 chars (drops statement numbers, dates,
    store codes, short noise), take the first two -> 'NETFLIX.COM 4471' -> 'netflix com', 'AGL ENERGY
    LTD 88' -> 'agl energy'. Deterministic, so detection and later matching agree. '' if nothing usable."""
    words = [w for w in re.findall(r"[a-z]+", (description or "").lower()) if len(w) >= 3]
    return " ".join(words[:2])


def detect_bill_candidates(rows, existing_patterns=(), excluded=None):
    _excluded = set(excluded) if excluded is not None else EXCLUDED_FROM_TOTALS
    """Find monthly-ish recurring OUTFLOWS in already-parsed transaction rows
    [{date:'YYYY-MM-DD', description, amount(float), category}]. Pure — no DB, no model.
    Groups by _merchant_key; a group is a candidate when it recurs across >=BILL_MIN_MONTHS distinct
    months, isn't frequent spending (<=BILL_MAX_PER_MONTH hits/active-month), and its month
    representatives are spaced ~monthly (median gap in [BILL_GAP_MIN,BILL_GAP_MAX] days). Suggests
    fixed vs usage from how much the per-month amounts vary. Excludes Transfers, inflows, and any
    pattern already linked to a bill (existing_patterns)."""
    from collections import defaultdict
    groups = defaultdict(list)
    for r in rows:
        if float(r["amount"]) >= 0:                       # outflows only (bills are money out)
            continue
        if (r.get("category") or "") in _excluded:              # never own-account transfers
            continue
        key = _merchant_key(r["description"])
        if key:
            groups[key].append(r)

    existing = set(existing_patterns or ())
    out = []
    for key, occ in groups.items():
        if key in existing:
            continue
        occ = sorted(occ, key=lambda x: x["date"])
        months = {}                                        # earliest occurrence per calendar month
        for r in occ:
            months.setdefault(r["date"][:7], r)
        active = len(months)
        if active < BILL_MIN_MONTHS:
            continue
        if len(occ) / active > BILL_MAX_PER_MONTH:         # frequent spending, not a single bill
            continue
        reps = [months[m] for m in sorted(months)]
        dts = [date.fromisoformat(r["date"]) for r in reps]
        gaps = [(dts[i + 1] - dts[i]).days for i in range(len(dts) - 1)]
        med = sorted(gaps)[len(gaps) // 2] if gaps else 0
        if not (BILL_GAP_MIN <= med <= BILL_GAP_MAX):      # not a monthly cadence
            continue
        amounts = [round(abs(float(r["amount"])), 2) for r in reps]
        mean = sum(amounts) / len(amounts)
        spread = (max(amounts) - min(amounts)) / mean if mean else 0.0
        btype = "fixed" if spread <= BILL_FIXED_SPREAD else "usage"
        cats = [(r.get("category") or "uncategorised") for r in occ]
        category = max(set(cats), key=cats.count)
        if category not in _BILL_CATEGORY_KEYS:
            category = "uncategorised"
        doms = sorted(d.day for d in dts)
        out.append({
            "source_pattern": key,
            "suggested_name": key.title(),
            "months": [{"month": m, "date": months[m]["date"],
                        "amount": round(abs(float(months[m]["amount"])), 2)} for m in sorted(months)],
            "occurrences": len(occ),
            "active_months": active,
            "median_gap_days": med,
            "amount_min": min(amounts), "amount_max": max(amounts),
            "amount_avg": round(mean, 2), "amount_latest": amounts[-1],
            "amount_spread_pct": round(spread * 100, 1),
            "suggested_amount": amounts[-1],               # most-recent charge = the reference
            "suggested_bill_type": btype,
            "suggested_frequency": "monthly",
            "suggested_day_of_month": doms[len(doms) // 2],
            "suggested_category": category,
        })
    out.sort(key=lambda c: (-c["active_months"], -c["occurrences"], c["suggested_name"]))
    return out


async def _refresh_linked_bills(conn):
    """S74 Task 4 — run at the end of a CSV import. For every bill linked to a transaction pattern
    (source_pattern set): a USAGE bill's reference amount is refreshed to the latest matching charge
    (no manual retyping); a FIXED bill gets a gentle price_flag when the latest matching charge differs
    from what's recorded (a 'did the price change?' catch), and the flag clears if it matches again.
    Pure SQL + Python — no CSV re-read, no model."""
    bills = await conn.fetch(
        "SELECT id, amount::float8 amount, bill_type, source_pattern FROM bills "
        "WHERE source_pattern IS NOT NULL AND source_pattern <> ''")
    if not bills:
        return {"usage_refreshed": 0, "fixed_flagged": 0}
    excluded = await load_excluded_categories(conn)        # S90: DB-driven transfers set
    txns = await conn.fetch(
        "SELECT to_char(txn_date,'YYYY-MM-DD') d, description, amount::float8 amount, category "
        "FROM transactions WHERE amount < 0")
    latest = {}                                            # merchant_key -> (date, abs_amount)
    for t in txns:
        if (t["category"] or "") in excluded:
            continue
        k = _merchant_key(t["description"])
        if not k:
            continue
        cur = latest.get(k)
        if cur is None or t["d"] > cur[0]:
            latest[k] = (t["d"], round(abs(float(t["amount"])), 2))
    usage_n = fixed_n = 0
    for b in bills:
        m = latest.get(b["source_pattern"])
        if not m:
            continue
        obs_date, obs_amt = m
        recorded = round(float(b["amount"]), 2)
        differs = abs(obs_amt - recorded) >= 0.01
        if b["bill_type"] == "usage":
            if differs:
                await conn.execute(
                    "UPDATE bills SET amount=$1, price_flag=NULL, updated_at=now() WHERE id=$2",
                    obs_amt, b["id"])
                usage_n += 1
        else:                                              # fixed -> flag, never silently change
            if differs:
                flag = (f"Latest charge ${obs_amt:.2f} on {obs_date} differs from the recorded "
                        f"${recorded:.2f} — did the price change?")
                await conn.execute(
                    "UPDATE bills SET price_flag=$1, updated_at=now() WHERE id=$2", flag, b["id"])
                fixed_n += 1
            else:
                await conn.execute(
                    "UPDATE bills SET price_flag=NULL WHERE id=$1 AND price_flag IS NOT NULL", b["id"])
    return {"usage_refreshed": usage_n, "fixed_flagged": fixed_n}


@app.get("/api/bill-meta")
async def api_bill_meta():
    """Dropdown metadata for the bills UI, single-sourced from the server so the
    category buckets live in one place. No data, no side effects."""
    return {"frequencies": list(BILL_FREQUENCIES),
            "pay_frequencies": list(PAY_FREQUENCIES),
            "bill_types": list(BILL_TYPES),
            "categories": BILL_CATEGORIES,
            "currency": BILL_CURRENCY}


@app.get("/api/bills")
async def api_bills_list(request: Request):
    today = datetime.now(LOCAL_TZ).date()
    if is_demo(request):
        return {"bills": [_compute_bill(_demo_bill_row(i, b), today)
                          for i, b in enumerate(DEMO_BILLS, 1)], "demo": True}
    try:
        conn = await pg_conn()
        try:
            await _ensure_bills_schema(conn)
            rows = await conn.fetch("SELECT * FROM bills ORDER BY name")
        finally:
            await conn.close()
        return {"bills": [_compute_bill(r, today) for r in rows], "demo": False}
    except Exception as e:
        logger.error(f"api_bills_list error: {e}")
        return {"bills": [], "error": str(e)}


@app.post("/api/bills")
async def api_bills_create(request: Request):
    if is_demo(request):
        return {"ok": False, "error": "demo mode is read-only"}
    try:
        data = await request.json()
    except Exception:
        return {"ok": False, "error": "bad request"}
    fields, err = _parse_bill_payload(data)
    if err:
        return {"ok": False, "error": err}
    try:
        conn = await pg_conn()
        try:
            await _ensure_bills_schema(conn)
            row = await conn.fetchrow("""
                INSERT INTO bills (name, amount, frequency, due_date, day_of_month,
                                   category, autopay, payer, notes, last_paid_date,
                                   bill_type, source_pattern)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12) RETURNING id
            """, fields["name"], fields["amount"], fields["frequency"], fields["due_date"],
                 fields["day_of_month"], fields["category"], fields["autopay"],
                 fields["payer"], fields["notes"], fields["last_paid_date"],
                 fields["bill_type"], fields["source_pattern"])
        finally:
            await conn.close()
        logger.info(f"bill created #{row['id']} ({fields['name']})")
        return {"ok": True, "id": row["id"]}
    except Exception as e:
        logger.error(f"api_bills_create error: {e}")
        return {"ok": False, "error": str(e)}


@app.put("/api/bills/{bill_id}")
async def api_bills_update(bill_id: int, request: Request):
    if is_demo(request):
        return {"ok": False, "error": "demo mode is read-only"}
    try:
        data = await request.json()
    except Exception:
        return {"ok": False, "error": "bad request"}
    fields, err = _parse_bill_payload(data)
    if err:
        return {"ok": False, "error": err}
    try:
        conn = await pg_conn()
        try:
            await _ensure_bills_schema(conn)
            # Editing a bill resolves any pending price-change flag (Lea has seen it and saved).
            status = await conn.execute("""
                UPDATE bills SET name=$1, amount=$2, frequency=$3, due_date=$4,
                    day_of_month=$5, category=$6, autopay=$7, payer=$8, notes=$9,
                    last_paid_date=$10, bill_type=$11, source_pattern=$12,
                    price_flag=NULL, updated_at=now() WHERE id=$13
            """, fields["name"], fields["amount"], fields["frequency"], fields["due_date"],
                 fields["day_of_month"], fields["category"], fields["autopay"],
                 fields["payer"], fields["notes"], fields["last_paid_date"],
                 fields["bill_type"], fields["source_pattern"], bill_id)
        finally:
            await conn.close()
        if _changed_zero(status):
            return {"ok": False, "error": f"no bill #{bill_id}"}
        return {"ok": True, "id": bill_id}
    except Exception as e:
        logger.error(f"api_bills_update error: {e}")
        return {"ok": False, "error": str(e)}


@app.delete("/api/bills/{bill_id}")
async def api_bills_delete(bill_id: int, request: Request):
    if is_demo(request):
        return {"ok": False, "error": "demo mode is read-only"}
    try:
        conn = await pg_conn()
        try:
            await _ensure_bills_schema(conn)
            status = await conn.execute("DELETE FROM bills WHERE id=$1", bill_id)
        finally:
            await conn.close()
        if _changed_zero(status):
            return {"ok": False, "error": f"no bill #{bill_id}"}
        return {"ok": True, "id": bill_id}
    except Exception as e:
        logger.error(f"api_bills_delete error: {e}")
        return {"ok": False, "error": str(e)}


@app.post("/api/bills/{bill_id}/paid")
async def api_bills_mark_paid(bill_id: int, request: Request):
    """Record a bill as paid (sets last_paid_date), clearing the current-cycle overdue
    flag. Body may carry {date:'YYYY-MM-DD'}; defaults to today."""
    if is_demo(request):
        return {"ok": False, "error": "demo mode is read-only"}
    paid_on = datetime.now(LOCAL_TZ).date()
    try:
        data = await request.json()
        if data.get("date"):
            paid_on = date.fromisoformat(str(data["date"]))
    except Exception:
        pass
    try:
        conn = await pg_conn()
        try:
            await _ensure_bills_schema(conn)
            status = await conn.execute(
                "UPDATE bills SET last_paid_date=$1, updated_at=now() WHERE id=$2", paid_on, bill_id)
        finally:
            await conn.close()
        if _changed_zero(status):
            return {"ok": False, "error": f"no bill #{bill_id}"}
        return {"ok": True, "id": bill_id, "last_paid_date": paid_on.isoformat()}
    except Exception as e:
        logger.error(f"api_bills_mark_paid error: {e}")
        return {"ok": False, "error": str(e)}


# ===========================================================================
# S87 — Subscriptions: a lighter recurring-spend tracker beside Bills (Netflix/Spotify/etc.).
# Same CRUD shape as bills; the summary rolls every cycle up to a monthly-equivalent + annual total.
# ===========================================================================
def _parse_subscription_payload(data):
    """Validate + normalise a subscription create/update body. Returns (fields, error)."""
    name = str(data.get("name") or "").strip()
    if not name:
        return None, "name is required"
    try:
        amount = round(float(data.get("amount")), 2)
    except (TypeError, ValueError):
        return None, "amount must be a number"
    if amount < 0:
        return None, "amount cannot be negative"
    cycle = str(data.get("cycle") or "monthly").strip().lower()
    if cycle not in _CYCLE_PER_YEAR:
        return None, f"cycle must be one of {sorted(_CYCLE_PER_YEAR)}"
    next_renewal = None
    if data.get("next_renewal"):
        try:
            next_renewal = date.fromisoformat(str(data["next_renewal"]))
        except ValueError:
            return None, "next_renewal must be YYYY-MM-DD"
    category = (str(data.get("category")).strip() or None) if data.get("category") else None
    notes = (str(data.get("notes")).strip() or None) if data.get("notes") else None
    active = bool(data.get("active", True))
    source_pattern = (str(data.get("source_pattern")).strip() or None) if data.get("source_pattern") else None
    return {"name": name, "amount": amount, "cycle": cycle, "next_renewal": next_renewal,
            "category": category, "notes": notes, "active": active,
            "source_pattern": source_pattern}, None


def _sub_monthly(amount, cycle):
    """Monthly-equivalent cost of a subscription."""
    return round(float(amount) * _CYCLE_PER_YEAR.get(cycle, 12.0) / 12.0, 2)


@app.get("/api/subscriptions")
async def api_subscriptions_list(request: Request):
    """Active + inactive subscriptions, plus a summary: monthly-equivalent total, annual total, count,
    and the soonest upcoming renewal (active only). Amounts are real; demo mode masks them."""
    today = datetime.now(LOCAL_TZ).date()
    try:
        conn = await pg_conn()
        try:
            await _ensure_bills_schema(conn)
            rows = await conn.fetch("SELECT * FROM subscriptions ORDER BY active DESC, name")
        finally:
            await conn.close()
        subs, monthly_total, annual_total, next_up = [], 0.0, 0.0, None
        demo = is_demo(request)
        for r in rows:
            m = _sub_monthly(r["amount"], r["cycle"])
            if r["active"]:
                monthly_total += m
                annual_total += float(r["amount"]) * _CYCLE_PER_YEAR.get(r["cycle"], 12.0)
                nr = r["next_renewal"]
                if nr and (next_up is None or nr < next_up):
                    next_up = nr
            subs.append({
                "id": r["id"], "name": r["name"],
                "amount": mask_money(f"${float(r['amount']):.2f}") if demo else float(r["amount"]),
                "cycle": r["cycle"],
                "monthly_equiv": mask_money(f"${m:.2f}") if demo else m,
                "next_renewal": r["next_renewal"].isoformat() if r["next_renewal"] else None,
                "category": r["category"], "active": r["active"], "notes": r["notes"],
            })
        return {"ok": True, "demo": demo, "subscriptions": subs,
                "summary": {"count_active": sum(1 for r in rows if r["active"]),
                            "count_total": len(rows),
                            "monthly_total": mask_money(f"${monthly_total:.2f}") if demo else round(monthly_total, 2),
                            "annual_total": mask_money(f"${annual_total:.2f}") if demo else round(annual_total, 2),
                            "next_renewal": next_up.isoformat() if next_up else None}}
    except Exception as e:
        logger.error(f"api_subscriptions_list error: {e}")
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.post("/api/subscriptions")
async def api_subscriptions_create(request: Request):
    if is_demo(request):
        return JSONResponse({"ok": False, "error": "demo mode is read-only"}, status_code=403)
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "bad request"}, status_code=400)
    fields, err = _parse_subscription_payload(data)
    if err:
        return JSONResponse({"ok": False, "error": err}, status_code=400)
    try:
        conn = await pg_conn()
        try:
            await _ensure_bills_schema(conn)
            row = await conn.fetchrow(
                "INSERT INTO subscriptions (name,amount,cycle,next_renewal,category,active,notes,source_pattern) "
                "VALUES ($1,$2,$3,$4,$5,$6,$7,$8) RETURNING id",
                fields["name"], fields["amount"], fields["cycle"], fields["next_renewal"],
                fields["category"], fields["active"], fields["notes"], fields["source_pattern"])
        finally:
            await conn.close()
        return {"ok": True, "id": row["id"]}
    except Exception as e:
        logger.error(f"api_subscriptions_create error: {e}")
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.put("/api/subscriptions/{sub_id}")
async def api_subscriptions_update(sub_id: int, request: Request):
    if is_demo(request):
        return JSONResponse({"ok": False, "error": "demo mode is read-only"}, status_code=403)
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "bad request"}, status_code=400)
    fields, err = _parse_subscription_payload(data)
    if err:
        return JSONResponse({"ok": False, "error": err}, status_code=400)
    try:
        conn = await pg_conn()
        try:
            await _ensure_bills_schema(conn)
            status = await conn.execute(
                "UPDATE subscriptions SET name=$1,amount=$2,cycle=$3,next_renewal=$4,category=$5,"
                "active=$6,notes=$7,updated_at=now() WHERE id=$8",
                fields["name"], fields["amount"], fields["cycle"], fields["next_renewal"],
                fields["category"], fields["active"], fields["notes"], sub_id)
        finally:
            await conn.close()
        if _changed_zero(status):
            return JSONResponse({"ok": False, "error": f"no subscription #{sub_id}"}, status_code=404)
        return {"ok": True, "id": sub_id}
    except Exception as e:
        logger.error(f"api_subscriptions_update error: {e}")
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.delete("/api/subscriptions/{sub_id}")
async def api_subscriptions_delete(sub_id: int, request: Request):
    if is_demo(request):
        return JSONResponse({"ok": False, "error": "demo mode is read-only"}, status_code=403)
    try:
        conn = await pg_conn()
        try:
            await _ensure_bills_schema(conn)
            status = await conn.execute("DELETE FROM subscriptions WHERE id=$1", sub_id)
        finally:
            await conn.close()
        if _changed_zero(status):
            return JSONResponse({"ok": False, "error": f"no subscription #{sub_id}"}, status_code=404)
        return {"ok": True, "id": sub_id}
    except Exception as e:
        logger.error(f"api_subscriptions_delete error: {e}")
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.get("/api/bills/summary")
async def api_bills_summary(request: Request):
    today = datetime.now(LOCAL_TZ).date()
    if is_demo(request):
        bills = [_compute_bill(_demo_bill_row(i, b), today) for i, b in enumerate(DEMO_BILLS, 1)]
        settings = {"pay_frequency": "fortnightly",
                    "next_pay_date": (today + timedelta(days=9)).isoformat(),
                    "currency": BILL_CURRENCY}
        return {"summary": _compute_summary(bills, settings, today), "settings": settings, "demo": True}
    try:
        conn = await pg_conn()
        try:
            await _ensure_bills_schema(conn)
            rows = await conn.fetch("SELECT * FROM bills")
            settings = await _get_bill_settings(conn)
        finally:
            await conn.close()
        bills = [_compute_bill(r, today) for r in rows]
        return {"summary": _compute_summary(bills, settings, today), "settings": settings, "demo": False}
    except Exception as e:
        logger.error(f"api_bills_summary error: {e}")
        return {"summary": None, "error": str(e)}


@app.get("/api/bills/candidates")
async def api_bills_candidates(request: Request):
    """S74 Task 1 — recurring-bill candidates detected from imported transactions, excluding any
    pattern already linked to an existing bill. Read-only; runs the deterministic detector over the
    live transactions table. Demo mode returns nothing (no real spending in a demo)."""
    if is_demo(request):
        return {"ok": True, "candidates": [], "demo": True}
    try:
        conn = await pg_conn()
        try:
            await _ensure_bills_schema(conn)
            await _ensure_spending_schema(conn)
            # S88: a pattern is excluded once it's linked to a bill OR a subscription, OR explicitly
            # dismissed — so "add as bill", "add as subscription" and "remove" all stop it recurring.
            existing = [r["source_pattern"] for r in await conn.fetch(
                "SELECT source_pattern FROM bills WHERE source_pattern IS NOT NULL AND source_pattern <> '' "
                "UNION SELECT source_pattern FROM subscriptions WHERE source_pattern IS NOT NULL AND source_pattern <> '' "
                "UNION SELECT source_pattern FROM dismissed_bill_candidates")]
            excluded_cats = await load_excluded_categories(conn)
            txns = await conn.fetch(
                "SELECT to_char(txn_date,'YYYY-MM-DD') date, description, amount::float8 amount, category "
                "FROM transactions")
        finally:
            await conn.close()
        rows = [{"date": t["date"], "description": t["description"],
                 "amount": t["amount"], "category": t["category"]} for t in txns]
        candidates = detect_bill_candidates(rows, existing_patterns=existing, excluded=excluded_cats)
        return {"ok": True, "candidates": candidates, "count": len(candidates), "demo": False}
    except Exception as e:
        logger.error(f"api_bills_candidates error: {e}")
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.post("/api/bills/candidates/dismiss")
async def api_bills_candidate_dismiss(request: Request):
    """S88: remove a detected recurring-bill candidate so it's never suggested again. Body
    {source_pattern}. Purely a suppression record — touches no bill, subscription or transaction."""
    if is_demo(request):
        return JSONResponse({"ok": False, "error": "demo mode is read-only"}, status_code=403)
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "bad request"}, status_code=400)
    pattern = str(data.get("source_pattern") or "").strip()
    if not pattern:
        return JSONResponse({"ok": False, "error": "missing source_pattern"}, status_code=400)
    try:
        conn = await pg_conn()
        try:
            await _ensure_bills_schema(conn)
            await conn.execute(
                "INSERT INTO dismissed_bill_candidates (source_pattern) VALUES ($1) "
                "ON CONFLICT (source_pattern) DO NOTHING", pattern)
        finally:
            await conn.close()
        return {"ok": True, "source_pattern": pattern}
    except Exception as e:
        logger.error(f"api_bills_candidate_dismiss error: {e}")
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.get("/api/bill-settings")
async def api_bill_settings_get(request: Request):
    if is_demo(request):
        # Placeholder pay cadence so real payday timing never ships during a demo.
        today = datetime.now(LOCAL_TZ).date()
        return {"ok": True, "demo": True, "settings": {
            "pay_frequency": "fortnightly",
            "next_pay_date": (today + timedelta(days=9)).isoformat(),
            "currency": BILL_CURRENCY}}
    try:
        conn = await pg_conn()
        try:
            await _ensure_bills_schema(conn)
            settings = await _get_bill_settings(conn)
        finally:
            await conn.close()
        return {"ok": True, "settings": settings}
    except Exception as e:
        logger.error(f"api_bill_settings_get error: {e}")
        return {"ok": False, "error": str(e)}


@app.post("/api/bill-settings")
async def api_bill_settings_set(request: Request):
    """Pay cadence (frequency + next pay date). Currency is fixed to AUD by spec."""
    if is_demo(request):
        return {"ok": False, "error": "demo mode is read-only"}
    try:
        data = await request.json()
    except Exception:
        return {"ok": False, "error": "bad request"}
    pay_freq = str(data.get("pay_frequency", "")).strip().lower()
    if pay_freq not in PAY_FREQUENCIES:
        return {"ok": False, "error": "pay_frequency must be one of " + ", ".join(PAY_FREQUENCIES)}
    next_pay = None
    if data.get("next_pay_date"):
        try:
            next_pay = date.fromisoformat(str(data["next_pay_date"]))
        except ValueError:
            return {"ok": False, "error": "next_pay_date must be YYYY-MM-DD"}
    try:
        conn = await pg_conn()
        try:
            await _ensure_bills_schema(conn)
            await conn.execute("""
                INSERT INTO bill_settings (id, pay_frequency, next_pay_date, currency, updated_at)
                VALUES (1, $1, $2, $3, now())
                ON CONFLICT (id) DO UPDATE SET pay_frequency=$1, next_pay_date=$2,
                    currency=$3, updated_at=now()
            """, pay_freq, next_pay, BILL_CURRENCY)
        finally:
            await conn.close()
        return {"ok": True}
    except Exception as e:
        logger.error(f"api_bill_settings_set error: {e}")
        return {"ok": False, "error": str(e)}


# ===========================================================================
# SPENDING CSV IMPORT (Session 67) — bank CSV -> deterministic summary -> Ollama narration
# ---------------------------------------------------------------------------
# Builds out Reference/csv_spending_summary.py (unwired since 29/07). HARD CONSTRAINT: this
# request path NEVER touches the Claude/Anthropic API — financial data is local-only per the
# money boundary (Session 52). ALL math is deterministic Python (pandas); the ONLY model call
# is _ollama_narrate(), which posts already-computed NUMBERS to the local Ollama at bot.OLLAMA_URL
# (never Anthropic). Categorisation rules live in the Lea-editable vault note (read live, cached).
# ===========================================================================
SPENDING_CATEGORIES_REL = os.environ.get(
    "GHOST_SPENDING_CATEGORIES_REL", "Projects/Tech/Ghost/Ghost — Spending Categories.md")
SPENDING_CACHE_SECONDS = 120
_spending_rules_cache = {"ts": 0.0, "data": None}
UNCATEGORISED = "uncategorised"
# S68: categories that are money moving between Lea's OWN accounts (savings transfers etc.) —
# real transactions, but not real external income or real spending, so they're kept OUT of the
# headline income/spend totals and shown as a separate "Transfers" figure instead. This is the
# NORMALISED form of a `## Transfers` heading in Ghost — Spending Categories.md (the note's own
# heading→key rule lowercases + underscores), so the moment Lea adds a `## Transfers` section with
# her transfer keywords, matching rows are categorised `transfers` and excluded automatically — no
# code change needed. Defined ONCE here rather than sprinkled as a literal through the maths.
TRANSFERS_CATEGORY = os.environ.get("GHOST_TRANSFERS_CATEGORY", "transfers")
# S85: "Savings & Transfers" is the new manual home for own-account money movement, kept out of the
# income/spent headline the same way the legacy `transfers` category was.
EXCLUDED_FROM_TOTALS = {TRANSFERS_CATEGORY, "savings_transfers"}
_SPENDING_SCHEMA_READY = False

# ===========================================================================
# S85 — MANUAL categorisation model (replaces the keyword-note heuristic as what drives the
# spending summary + review queue). Categorisation now comes from exactly two places:
#   1. a per-transaction manual pick (sticky, manual=TRUE), and
#   2. merchant -> category rules (the "always categorise this merchant" checkbox).
# Anything neither touched nor covered by a rule is UNCATEGORISED (the fallback STATE, never a
# pickable option). The category picker is a FIXED list, not the vault note's dynamic headings.
# ===========================================================================
SPENDING_FIXED_CATEGORIES = [
    ("groceries", "Groceries"),
    ("dining_takeaway", "Dining & Takeaway"),
    ("cafe", "Cafe"),
    ("social", "Social"),
    ("subscriptions", "Subscriptions"),
    ("utilities", "Utilities"),
    ("rent_mortgage", "Rent/Mortgage"),
    ("transport", "Transport"),
    ("insurance", "Insurance"),
    ("health_medical", "Health & Medical"),
    ("shopping_retail", "Shopping & Retail"),
    ("entertainment_hobbies", "Entertainment & Hobbies"),
    ("personal_care_beauty", "Personal Care & Beauty"),
    ("business_side_projects", "Business/Side Projects"),
    ("savings_transfers", "Savings & Transfers"),
    ("gifts_donations", "Gifts & Donations"),
    ("travel", "Travel"),
    ("debt_loan_repayments", "Debt/Loan Repayments"),
]
SPENDING_FIXED_LABELS = {k: v for k, v in SPENDING_FIXED_CATEGORIES}
SPENDING_FIXED_KEYS = set(SPENDING_FIXED_LABELS)

# Payment/bank noise stripped when deriving a merchant identifier from a description.
_MERCHANT_NOISE = re.compile(
    r'\b(?:eftpos|visa|mastercard|amex|debit|credit|purchase|payment|pos|pty|ltd|inc|australia|'
    r'aus|au|card|ref|tap|paywave|paypass|contactless|value|date|dep|withdrawal|transfer|direct)\b',
    re.I)


def _merchant_key(description: str) -> str:
    """Stable merchant identifier from a messy bank description: lowercase, drop masked card numbers,
    digit runs/amounts and common payment noise, then take the leading BRAND token. AU bank exports
    typically read "<BRAND> <ref#> <SUBURB>", so keying on the first token collapses a chain across
    suburbs (what "always categorise Woolworths" should do); a very short first token (<=3 chars, e.g.
    "dan murphys", "kfc") takes two so it stays distinctive. The review UI shows exactly this string
    beside the checkbox, so a rule matches precisely what Lea sees. Known v1 limit: two-word brands
    like "uber eats" collapse to their first word ("uber") — override those individually if needed.
    '' when nothing usable remains."""
    s = (description or "").lower()
    s = re.sub(r'\bx+\d+\b', ' ', s)             # masked card no. (xx1234 / x4321)
    s = re.sub(r'\d[\d.,]*', ' ', s)             # digit runs, amounts, dates
    s = _MERCHANT_NOISE.sub(' ', s)
    s = re.sub(r'[^a-z& ]+', ' ', s)
    toks = [t for t in s.split() if len(t) > 1]
    if not toks:
        return ""
    if len(toks[0]) <= 3 and len(toks) > 1:
        return toks[0] + " " + toks[1]
    return toks[0]


async def load_merchant_rules(conn) -> dict:
    """{merchant_key: category} — every stored merchant rule. Small table, read fresh."""
    rows = await conn.fetch("SELECT merchant_key, category FROM merchant_category_rules")
    return {r["merchant_key"]: r["category"] for r in rows}


async def load_categories(conn) -> list:
    """S87: the DB-backed category list (the picker's source of truth), ordered.
    [{key,label,exclude_from_totals}]."""
    rows = await conn.fetch(
        "SELECT key, label, exclude_from_totals FROM spending_categories ORDER BY sort_order, label")
    return [{"key": r["key"], "label": r["label"],
             "exclude_from_totals": r["exclude_from_totals"]} for r in rows]


async def category_exists(conn, key) -> bool:
    return bool(await conn.fetchval("SELECT 1 FROM spending_categories WHERE key=$1", key))


def resolve_category(description, manual, stored_category, merchant_rules) -> str:
    """Effective category under the S85 model: a manual per-transaction pick wins; else a matching
    merchant rule; else uncategorised. The keyword note no longer participates in this resolution."""
    if manual:
        return stored_category or UNCATEGORISED
    mk = _merchant_key(description)
    if mk and mk in merchant_rules:
        return merchant_rules[mk]
    return UNCATEGORISED


def load_spending_categories() -> "dict":
    """Ordered {category: [keywords]} from the vault note (## heading = category, backtick bullets
    = keywords). Read live, cached SPENDING_CACHE_SECONDS. Fail-safe: unreadable note -> last cache
    or empty (everything then lands 'uncategorised', flagged — never a wrong silent category)."""
    now = time.monotonic()
    cached = _spending_rules_cache["data"]
    if cached is not None and now - _spending_rules_cache["ts"] < SPENDING_CACHE_SECONDS:
        return cached
    try:
        rules = {}
        path = os.path.join(bot.VAULT_PATH, SPENDING_CATEGORIES_REL)
        cat = None
        for line in open(path, encoding="utf-8").read().splitlines():
            if line.startswith("## "):
                name = line[3:].strip()
                if name.lower() == "related":
                    cat = None
                    continue
                cat = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
                rules.setdefault(cat, [])
                continue
            if cat:
                m = re.match(r"\s*-\s*`([^`]+)`", line)
                if m:
                    rules[cat].append(m.group(1).strip().lower())
        _spending_rules_cache["ts"] = now
        _spending_rules_cache["data"] = rules
        return rules
    except OSError as e:
        logger.error(f"spending categories note unreadable ({e}) — nothing categorised this cycle")
        return cached if cached is not None else {}


def categorise_txn(description: str, rules: dict) -> str:
    text = (description or "").lower()
    for category, keywords in rules.items():
        if any(kw and kw in text for kw in keywords):
            return category
    return UNCATEGORISED


async def _ensure_spending_schema(conn):
    global _SPENDING_SCHEMA_READY
    if _SPENDING_SCHEMA_READY:
        return
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS transactions (
            id           BIGSERIAL PRIMARY KEY,
            txn_date     DATE NOT NULL,
            description  TEXT NOT NULL,
            amount       NUMERIC(12,2) NOT NULL,
            category     TEXT NOT NULL DEFAULT 'uncategorised',
            source_file  TEXT,
            imported_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE (txn_date, description, amount)
        );
        CREATE TABLE IF NOT EXISTS spending_summary (
            id           INTEGER PRIMARY KEY DEFAULT 1,
            summary      JSONB NOT NULL,
            narration    TEXT,
            generated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT spending_summary_singleton CHECK (id = 1)
        );
    """)
    # S71: a transaction Lea has hand-categorised on the dashboard carries manual=TRUE. Such a row
    # keeps its stored `category` verbatim — the live re-categorisation (S70) is SKIPPED for it, and a
    # re-import of the overlapping days never overwrites it (see the import ON CONFLICT below). Added
    # idempotently so an already-created transactions table gains the column with no migration step.
    await conn.execute(
        "ALTER TABLE transactions ADD COLUMN IF NOT EXISTS manual BOOLEAN NOT NULL DEFAULT FALSE")
    # S85: merchant -> category rules. Keyed by the normalised merchant (see _merchant_key); applied
    # LIVE to every matching non-manual transaction (backfill and future) by resolve_category.
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS merchant_category_rules (
            merchant_key   TEXT PRIMARY KEY,
            category       TEXT NOT NULL,
            created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
        );
    """)
    # S87: categories are now DB-backed (the picker's source of truth) so they can be added/removed.
    # The S85 fixed 18 are the one-time SEED; a `categories_seeded` marker guards it so a category
    # Lea DELETES can't resurrect on the next process start.
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS spending_categories (
            key         TEXT PRIMARY KEY,
            label       TEXT NOT NULL,
            sort_order  INTEGER NOT NULL DEFAULT 100,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE TABLE IF NOT EXISTS spending_meta (k TEXT PRIMARY KEY, v TEXT);
    """)
    # S90: which categories are own-account transfers (excluded from income/spent totals) is now a
    # DB flag on the category itself — the summary derives its "Transfers" bucket from THIS, not from
    # the hardcoded EXCLUDED_FROM_TOTALS set, so it's entirely driven by Lea's real category list.
    await conn.execute(
        "ALTER TABLE spending_categories ADD COLUMN IF NOT EXISTS exclude_from_totals BOOLEAN NOT NULL DEFAULT FALSE")
    seeded = await conn.fetchval("SELECT v FROM spending_meta WHERE k='categories_seeded'")
    if not seeded:
        for i, (k, label) in enumerate(SPENDING_FIXED_CATEGORIES):
            await conn.execute(
                "INSERT INTO spending_categories (key,label,sort_order,exclude_from_totals) "
                "VALUES ($1,$2,$3,$4) ON CONFLICT (key) DO NOTHING",
                k, label, i, k in EXCLUDED_FROM_TOTALS)
        await conn.execute(
            "INSERT INTO spending_meta (k,v) VALUES ('categories_seeded','1') ON CONFLICT (k) DO NOTHING")
    # One-time migration for an already-seeded DB (column added after seeding): flag the transfer
    # categories that the old hardcoded set used to exclude, so behaviour is unchanged.
    flagmig = await conn.fetchval("SELECT v FROM spending_meta WHERE k='exclude_flag_migrated'")
    if not flagmig:
        await conn.execute(
            "UPDATE spending_categories SET exclude_from_totals=TRUE WHERE key = ANY($1::text[])",
            list(EXCLUDED_FROM_TOTALS))
        await conn.execute(
            "INSERT INTO spending_meta (k,v) VALUES ('exclude_flag_migrated','1') ON CONFLICT (k) DO NOTHING")
    _SPENDING_SCHEMA_READY = True


async def load_excluded_categories(conn) -> set:
    """S90: category keys flagged exclude_from_totals — the DB-driven 'own-account transfers' set that
    replaces the hardcoded EXCLUDED_FROM_TOTALS. Fully DB-driven: an EMPTY result means Lea has flagged
    nothing as a transfer (her choice), so nothing is excluded — no fallback to the old constant, which
    would override that control. Seeding/migration flags savings_transfers by default."""
    rows = await conn.fetch("SELECT key FROM spending_categories WHERE exclude_from_totals")
    return {r["key"] for r in rows}


# Column auto-detect. AU bank exports vary (CommBank/ANZ/Westpac/NAB); guessing wrong silently
# produces wrong totals, so this maps only well-known variants and FAILS CLOSED otherwise.
_DATE_COLS = ["date", "transaction date", "txn date", "posting date", "processed date", "value date"]
_DESC_COLS = ["description", "narrative", "details", "transaction details", "merchant", "memo", "reference"]
_AMOUNT_COLS = ["amount", "transaction amount", "value"]
_DEBIT_COLS = ["debit", "debit amount", "withdrawal", "withdrawals", "money out", "paid out"]
_CREDIT_COLS = ["credit", "credit amount", "deposit", "deposits", "money in", "paid in"]
_DATE_FORMATS = ["%d/%m/%Y", "%d/%m/%y", "%Y-%m-%d", "%d-%m-%Y", "%d %b %Y", "%d %B %Y", "%m/%d/%Y"]


def _pick(cols_lower, candidates):
    for c in candidates:
        if c in cols_lower:
            return cols_lower[c]
    return None


def detect_and_normalise(df, pd):
    """Return (normalised_df[date,description,amount], detected_mapping) or raise ValueError with a
    clear, column-naming message when it can't confidently map. Handles a single signed Amount OR
    separate Debit/Credit columns (debit = money out -> negative)."""
    cols_lower = {str(c).strip().lower(): c for c in df.columns}
    date_col = _pick(cols_lower, _DATE_COLS)
    desc_col = _pick(cols_lower, _DESC_COLS)
    amt_col = _pick(cols_lower, _AMOUNT_COLS)
    debit_col = _pick(cols_lower, _DEBIT_COLS)
    credit_col = _pick(cols_lower, _CREDIT_COLS)

    problems = []
    if not date_col:
        problems.append(f"no date column (looked for {_DATE_COLS})")
    if not desc_col:
        problems.append(f"no description column (looked for {_DESC_COLS})")
    if not amt_col and not (debit_col or credit_col):
        problems.append(f"no amount column (looked for a single {_AMOUNT_COLS}, or "
                        f"Debit/Credit pair {_DEBIT_COLS} / {_CREDIT_COLS})")
    if problems:
        raise ValueError(
            "Couldn't confidently read this CSV — " + "; ".join(problems)
            + f". Columns found were: {list(df.columns)}. "
            "Rename the columns to a recognised name, or tell me which is which, so I don't total "
            "the wrong thing.")

    # amount: single signed column, else credit - debit
    if amt_col:
        amount = pd.to_numeric(df[amt_col].astype(str).str.replace(r"[,$]", "", regex=True), errors="coerce")
        amt_desc = amt_col
    else:
        deb = pd.to_numeric(df[debit_col].astype(str).str.replace(r"[,$]", "", regex=True), errors="coerce").fillna(0) if debit_col else 0
        cred = pd.to_numeric(df[credit_col].astype(str).str.replace(r"[,$]", "", regex=True), errors="coerce").fillna(0) if credit_col else 0
        amount = cred - deb   # money out (debit) -> negative, money in (credit) -> positive
        amt_desc = f"credit({credit_col}) - debit({debit_col})"

    # date: try known formats, then a last-resort dayfirst parse
    raw_date = df[date_col].astype(str)
    parsed = None
    used_fmt = None
    for fmt in _DATE_FORMATS:
        p = pd.to_datetime(raw_date, format=fmt, errors="coerce")
        if p.notna().sum() >= max(1, int(len(df) * 0.8)):
            parsed, used_fmt = p, fmt
            break
    if parsed is None:
        parsed = pd.to_datetime(raw_date, errors="coerce", dayfirst=True)
        used_fmt = "auto(dayfirst)"
    if parsed.notna().sum() < max(1, int(len(df) * 0.5)):
        raise ValueError(
            f"Found a date column ('{date_col}') but couldn't parse most of its values as dates "
            f"(sample: {list(raw_date.head(3))}). Tell me the date format so I don't misread it.")

    out = pd.DataFrame({"date": parsed, "description": df[desc_col].astype(str).str.strip(), "amount": amount})
    out = out.dropna(subset=["date", "amount"])
    return out, {"date": date_col, "description": desc_col, "amount": amt_desc, "date_format": used_fmt}


def _blank_month():
    from collections import defaultdict
    return {"income": 0.0, "spent": 0.0,
            "transfer_out": 0.0, "transfer_in": 0.0, "transfer_count": 0,
            "categories": defaultdict(float), "count": 0}


def compute_spending_summary(rows, excluded=None):
    _excluded = set(excluded) if excluded is not None else EXCLUDED_FROM_TOTALS
    """Deterministic aggregation over already-parsed rows [{date(str),description,amount,category}].
    Ported from csv_spending_summary.summarize(); NO model involved, numbers only. S70: everything is
    bucketed by calendar month (from txn_date) — each month carries its own income / spent / transfers
    / category breakdown, isolated from the others — plus an overall roll-up kept for the narration.
    Transfers between Lea's own accounts (S68) are excluded from every income/spent/category figure at
    both levels and tallied separately."""
    from collections import defaultdict
    per_month = defaultdict(_blank_month)
    spend_by_cat = defaultdict(float)          # overall
    merchant = defaultdict(float)
    total_spent = 0.0
    total_income = 0.0
    transfer_out = transfer_in = 0.0
    transfer_count = 0
    dates = []
    for r in rows:
        amt = float(r["amount"])
        d = r["date"]                          # 'YYYY-MM-DD'
        dates.append(d)
        month = d[:7]
        m = per_month[month]
        m["count"] += 1
        if r["category"] in _excluded:
            transfer_count += 1
            m["transfer_count"] += 1
            if amt < 0:
                transfer_out += abs(amt); m["transfer_out"] += abs(amt)
            elif amt > 0:
                transfer_in += amt; m["transfer_in"] += amt
            continue
        if amt < 0:
            a = abs(amt)
            total_spent += a; m["spent"] += a
            spend_by_cat[r["category"]] += a
            m["categories"][r["category"]] += a
            merchant[r["description"]] += a
        elif amt > 0:
            total_income += amt; m["income"] += amt

    def _month_block(mv):
        cats = {k: round(v, 2) for k, v in sorted(mv["categories"].items(), key=lambda x: -x[1])}
        spent = round(mv["spent"], 2)
        unc = round(cats.get(UNCATEGORISED, 0.0), 2)
        return {
            "income": round(mv["income"], 2),
            "spent": spent,
            "transfers": {"out": round(mv["transfer_out"], 2), "in": round(mv["transfer_in"], 2),
                          "volume": round(mv["transfer_out"] + mv["transfer_in"], 2),
                          "net": round(mv["transfer_in"] - mv["transfer_out"], 2),
                          "count": mv["transfer_count"]},
            "categories": cats,
            "count": mv["count"],
            "uncategorised_pct": round(unc / spent * 100, 1) if spent > 0 else 0.0,
        }

    by_month = {mo: _month_block(per_month[mo]) for mo in per_month}
    by_category = {k: round(v, 2) for k, v in sorted(spend_by_cat.items(), key=lambda x: -x[1])}
    top_merchants = dict(sorted(({k: round(v, 2) for k, v in merchant.items()}).items(),
                                key=lambda x: -x[1])[:15])
    unc = round(by_category.get(UNCATEGORISED, 0.0), 2)
    unc_share = round(unc / total_spent * 100, 1) if total_spent > 0 else 0.0
    return {
        "period": {"start": min(dates) if dates else None, "end": max(dates) if dates else None},
        "totals": {"total_spent": round(total_spent, 2), "total_income": round(total_income, 2),
                   "transaction_count": len(rows)},
        "transfers": {"category": TRANSFERS_CATEGORY, "count": transfer_count,
                      "out": round(transfer_out, 2), "in": round(transfer_in, 2),
                      "volume": round(transfer_out + transfer_in, 2),
                      "net": round(transfer_in - transfer_out, 2)},
        "by_category": by_category,
        "by_month": by_month,                  # S70: per-month income/spent/transfers/categories
        "top_merchants": top_merchants,
        "months": sorted(per_month.keys()),
        "data_quality": {"uncategorised_total": unc, "uncategorised_share_pct": unc_share},
        "generated_at": datetime.now(LOCAL_TZ).isoformat(timespec="seconds"),
    }


async def _live_spending_summary(conn):
    """Compute the spending summary FRESH from the current contents of the transactions table on
    every read. S69 made the AGGREGATION live; S70 makes the CATEGORISATION live too — each row is
    re-categorised against the CURRENT vault note here rather than trusting the `category` column
    frozen at import time. That was the by_category bug: totals don't depend on WHICH non-transfer
    category a row sits in (so they looked right), but by_category does — so a note edit after import
    left the breakdown stale. Now a keyword change in Ghost — Spending Categories.md (incl. the S68
    transfers exclusion) shows up on the next page load with no re-import. Deterministic; NO model."""
    # S85: categorisation is now MANUAL pick (sticky) + merchant rules, resolved live per row.
    # The keyword note (categorise_txn / load_spending_categories) no longer drives the summary —
    # untouched rows without a matching merchant rule are uncategorised, so the review queue reflects
    # exactly what still needs doing.
    merchant_rules = await load_merchant_rules(conn)
    excluded = await load_excluded_categories(conn)      # S90: DB-driven transfers set
    all_rows = await conn.fetch(
        "SELECT to_char(txn_date,'YYYY-MM-DD') d, description, amount::float8 amount, category, manual "
        "FROM transactions ORDER BY txn_date")
    db_rows = [{"date": r["d"], "description": r["description"], "amount": r["amount"],
                "category": resolve_category(r["description"], r["manual"], r["category"], merchant_rules)}
               for r in all_rows]
    return compute_spending_summary(db_rows, excluded=excluded), len(db_rows)


async def _ollama_narrate(summary: dict) -> str:
    """Narrate an ALREADY-COMPUTED numbers-only summary via the LOCAL Ollama at bot.OLLAMA_URL.
    NEVER calls Claude/Anthropic — this is the only model touch in the import path and it is local.
    Returns '' on any error (the numbers still stand on their own)."""
    prompt = (
        "You are Ghost, a dry, blunt money tracker. Below is an ALREADY-CORRECT spending summary "
        "computed deterministically. Do NOT recalculate or invent any number — just summarise what "
        "stands out in plain language, 4-6 sentences, for Lea. Mention the biggest categories, "
        "anything notable month to month, and flag it if the uncategorised share is high. No "
        "exclamation marks, no emoji, no praise.\n\n"
        + json.dumps(summary, indent=2)
    )
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                bot.OLLAMA_URL,
                json={"model": "llama3.2:1b", "prompt": prompt, "stream": False,
                      "keep_alive": "1h", "options": {"temperature": 0.6, "num_gpu": 0, "num_predict": 320}},
                timeout=180.0)
            return (resp.json().get("response", "") or "").strip()
    except Exception as e:
        logger.error(f"_ollama_narrate error: {e}")
        return ""


@app.post("/api/bills/import-csv")
async def api_import_csv(file: UploadFile = File(...)):
    """Upload a bank CSV -> parse (pandas) -> categorise (vault note) -> aggregate (Python) ->
    narrate (LOCAL Ollama) -> store. Dedupes on (date, description, amount). NO Claude in this path."""
    try:
        import pandas as pd
        import io as _io
        raw = await file.read()
        if not raw:
            return JSONResponse({"ok": False, "error": "empty file"}, status_code=400)
        try:
            df = pd.read_csv(_io.BytesIO(raw))
        except Exception as e:
            return JSONResponse({"ok": False, "error": f"couldn't read CSV: {e}"}, status_code=400)
        if df.empty:
            return JSONResponse({"ok": False, "error": "CSV has no data rows"}, status_code=400)
        try:
            norm, mapping = detect_and_normalise(df, pd)
        except ValueError as e:
            return JSONResponse({"ok": False, "error": str(e), "columns_found": list(df.columns)},
                                status_code=422)
        if norm.empty:
            return JSONResponse({"ok": False, "error": "no rows had a parseable date AND amount",
                                 "mapping": mapping}, status_code=422)

        rules = load_spending_categories()
        fname = (file.filename or "upload.csv")
        rows = []
        for _, r in norm.iterrows():
            d = r["date"].date().isoformat()
            desc = str(r["description"])[:500]
            amt = round(float(r["amount"]), 2)
            rows.append({"date": d, "description": desc, "amount": amt,
                         "category": categorise_txn(desc, rules)})

        inserted = updated = 0
        conn = await pg_conn()
        try:
            await _ensure_spending_schema(conn)
            for row in rows:
                # (xmax = 0) is TRUE for a fresh insert, FALSE when ON CONFLICT did an update —
                # so we count new vs deduped rows precisely (the dedupe key is date+description+amount).
                # S71: a re-import of the overlapping ~90-120 days must NOT clobber a category Lea
                # hand-set on the dashboard. When the existing row is manual=TRUE we keep its category
                # (and leave manual untouched); otherwise the freshly-computed category wins as before.
                was_insert = await conn.fetchval(
                    """INSERT INTO transactions (txn_date, description, amount, category, source_file)
                       VALUES ($1,$2,$3,$4,$5)
                       ON CONFLICT (txn_date, description, amount)
                       DO UPDATE SET category = CASE WHEN transactions.manual
                                                     THEN transactions.category
                                                     ELSE EXCLUDED.category END,
                                     source_file = EXCLUDED.source_file,
                                     imported_at = now()
                       RETURNING (xmax = 0)""",
                    date.fromisoformat(row["date"]), row["description"], row["amount"], row["category"], fname)
                if was_insert:
                    inserted += 1
                else:
                    updated += 1
        finally:
            await conn.close()

        # Recompute the whole summary from ALL rows (same live path the GET uses) and refresh the
        # stored narration — the ONLY reason to store anything now is the local-Ollama narration,
        # which is too slow to regenerate on every page load. The card's numbers come live from
        # _live_spending_summary on each GET, not from this stored copy.
        conn = await pg_conn()
        try:
            summary, _ = await _live_spending_summary(conn)
            narration = await _ollama_narrate(summary)
            await conn.execute(
                """INSERT INTO spending_summary (id, summary, narration, generated_at)
                   VALUES (1, $1::jsonb, $2, now())
                   ON CONFLICT (id) DO UPDATE SET summary=EXCLUDED.summary,
                       narration=EXCLUDED.narration, generated_at=now()""",
                json.dumps(summary), narration)
            # S74: refresh usage-based linked bills / flag fixed-bill price changes from the new rows.
            await _ensure_bills_schema(conn)
            bill_refresh = await _refresh_linked_bills(conn)
        finally:
            await conn.close()

        return {"ok": True, "mapping": mapping, "rows_in_file": len(rows),
                "inserted": inserted, "updated_deduped": updated,
                "total_in_db": summary["totals"]["transaction_count"],
                "uncategorised_share_pct": summary["data_quality"]["uncategorised_share_pct"],
                "bill_refresh": bill_refresh,
                "narration": narration, "summary": summary}
    except Exception as e:
        logger.error(f"/api/bills/import-csv error: {e}")
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.get("/api/bills/spending")
async def api_spending(request: Request):
    """Read-only card feed. S69: the NUMBERS are computed LIVE from the transactions table on every
    request (via _live_spending_summary) — never a stored snapshot — so a categorisation-rule change
    or a corrected aggregation shows up on the next page load with no re-import. Only the narration
    (the local-Ollama commentary, too slow to regenerate per GET) is read from storage. No model
    call here."""
    try:
        conn = await pg_conn()
        try:
            await _ensure_spending_schema(conn)
            summary, n_rows = await _live_spending_summary(conn)
            stored = await conn.fetchrow("SELECT narration, generated_at FROM spending_summary WHERE id=1")
        finally:
            await conn.close()
        if n_rows == 0:
            return {"ok": True, "has_data": False}
        narration = stored["narration"] if stored else ""
        generated_at = stored["generated_at"] if stored else None
        # S70: monthly buckets, newest first. Card defaults to the last 3; older months are returned
        # too (under "all") so the page can offer a "show all months" toggle.
        months_desc = sorted(summary.get("months") or [], reverse=True)
        by_month = summary.get("by_month") or {}
        return {"ok": True, "has_data": True,
                "months": months_desc,
                "default_months": months_desc[:3],
                "by_month": by_month,
                "overall": {"totals": summary.get("totals", {}),
                            "transfers": summary.get("transfers", {}),
                            "by_category": summary.get("by_category", {})},
                "data_quality": summary.get("data_quality", {}),
                "narration": narration or "",
                "generated_at": generated_at.isoformat() if generated_at else None}
    except Exception as e:
        logger.error(f"/api/bills/spending error: {e}")
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


# ===========================================================================
# S71 — manual category review / correction (the "some transactions are wrong" fix)
# ---------------------------------------------------------------------------
# Keyword auto-categorisation (Ghost — Spending Categories.md) mislabels or misses some rows, which
# threw the Income/Transfers figures. Rather than hand-editing a CSV Category column every month (the
# bank re-includes ~120 overlapping days each export, so that never stops), Lea corrects a row IN THE
# DASHBOARD: a review list surfaces uncategorised (and, on request, all) transactions with an inline
# picker; one Save sets that row's category and marks it manual=TRUE (sticky — see _live_spending_summary
# and the import ON CONFLICT). An optional "teach" flag also appends the keyword to the note so every
# future transaction from that merchant auto-categorises. No CSV, no re-upload, no Claude — a UPDATE
# plus a note append, both against Lea's own local Postgres/vault.
# ===========================================================================

def _norm_category_key(text: str) -> str:
    """Normalise a category name to the note's heading→key form (lowercase, non-alnum→underscore)."""
    return re.sub(r"[^a-z0-9]+", "_", (text or "").lower()).strip("_")


@app.get("/api/bills/categories")
async def api_bills_categories():
    """S87: the DB-backed category list for the categorise picker (add/remove supported). Uncategorised
    is the fallback STATE, never returned here (not pickable). `details` carries key+label so the UI
    shows exact labels ('Dining & Takeaway', 'Rent/Mortgage', …)."""
    try:
        conn = await pg_conn()
        try:
            await _ensure_spending_schema(conn)
            details = await load_categories(conn)
        finally:
            await conn.close()
        return {"ok": True, "categories": [d["key"] for d in details], "details": details}
    except Exception as e:
        logger.error(f"/api/bills/categories error: {e}")
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.get("/api/bills/transactions")
async def api_bills_transactions(request: Request, filter: str = "uncategorized", limit: int = 5000):
    """Rows for the categorise screen. filter=uncategorized (default) returns only rows whose EFFECTIVE
    category is uncategorised — the queue to clear; filter=all returns every row so a category can be
    corrected too. Effective category (S85) = manual pick, else a matching merchant rule, else
    uncategorised — exactly what the summary counts. Each row also carries `merchant` (the normalised
    identifier a rule keys on) and whether a merchant rule currently covers it, so the UI can show what
    'always categorise this merchant' would match. Returns ALL matching rows (full backfill), capped."""
    try:
        conn = await pg_conn()
        try:
            await _ensure_spending_schema(conn)
            merchant_rules = await load_merchant_rules(conn)
            rows = await conn.fetch(
                "SELECT id, to_char(txn_date,'YYYY-MM-DD') d, description, amount::float8 amount, "
                "category, manual FROM transactions ORDER BY txn_date DESC, id DESC")
        finally:
            await conn.close()
        out = []
        cap = max(1, min(limit, 20000))
        for r in rows:
            eff = resolve_category(r["description"], r["manual"], r["category"], merchant_rules)
            if filter == "uncategorized" and eff != UNCATEGORISED:
                continue
            mk = _merchant_key(r["description"])
            out.append({"id": r["id"], "date": r["d"], "description": r["description"],
                        "amount": r["amount"], "category": eff, "manual": r["manual"],
                        "merchant": mk, "rule": mk in merchant_rules})
            if len(out) >= cap:
                break
        return {"ok": True, "filter": filter, "count": len(out), "transactions": out}
    except Exception as e:
        logger.error(f"/api/bills/transactions error: {e}")
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


# ---------------------------------------------------------------------------
# S73 — Manage Categories: edit the note's keyword RULES themselves from the dashboard.
# The note (Ghost — Spending Categories.md) is markdown: a preamble, `## Heading` sections whose
# backtick bullets are keywords, some sections carry explanatory prose (e.g. Transfers), and a final
# `## Related`. Every edit here is a minimal, structure-preserving line transform — the transforms are
# PURE (lines in, lines out) so they're unit-testable against a synthetic note with no real file, and
# the single write path (_write_spending_note_lines) does the atomic replace + owner restore (pod runs
# as root) + rules-cache bust that the live recompute depends on. No CSV, no chat, no Claude.
# ---------------------------------------------------------------------------
_KW_RE = re.compile(r"\s*-\s*`([^`]+)`")


def _read_spending_note_lines():
    path = os.path.join(bot.VAULT_PATH, SPENDING_CATEGORIES_REL)
    return open(path, encoding="utf-8").read().split("\n")


def _write_spending_note_lines(lines):
    """Atomic write of the categories note, owner/mode restored, rules cache busted so the live
    recompute (_live_spending_summary) reflects the edit on the very next read."""
    path = os.path.join(bot.VAULT_PATH, SPENDING_CATEGORIES_REL)
    st = os.stat(path)
    tmp = path + ".spendtmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    os.replace(tmp, path)
    try:
        bot._preserve_vault_owner(path, st)
    except Exception as e:
        logger.error(f"spending note chown-back failed: {e}")
    _spending_rules_cache["ts"] = 0.0


def _section_span(lines, category_key):
    """Return (head_idx, end) for the `## heading` whose normalised text == category_key; the section
    body is lines[head_idx+1:end], end being the next `## ` line or EOF. (-1, -1) if not found."""
    head_idx = -1
    for i, ln in enumerate(lines):
        if ln.startswith("## ") and _norm_category_key(ln[3:].strip()) == category_key:
            head_idx = i
            break
    if head_idx < 0:
        return -1, -1
    end = len(lines)
    for j in range(head_idx + 1, len(lines)):
        if lines[j].startswith("## "):
            end = j
            break
    return head_idx, end


def _parse_spending_categories(lines):
    """Ordered [{key, label, keywords:[...]}] for every `## heading` except Related — preserving the
    heading's exact label text (which load_spending_categories throws away)."""
    out, cur = [], None
    for ln in lines:
        if ln.startswith("## "):
            label = ln[3:].strip()
            if label.lower() == "related":
                cur = None
                continue
            cur = {"key": _norm_category_key(label), "label": label, "keywords": []}
            out.append(cur)
            continue
        if cur is not None:
            m = _KW_RE.match(ln)
            if m:
                cur["keywords"].append(m.group(1).strip().lower())
    return out


def _lines_add_keyword(lines, category_key, keyword, category_label=""):
    """Insert `- `keyword`` under category_key, creating a `## label` section before Related if the
    category is new. Idempotent. Returns (lines, status)."""
    keyword = (keyword or "").strip().lower()
    if not keyword:
        return lines, "no keyword"
    head_idx, end = _section_span(lines, category_key)
    bullet = f"- `{keyword}`"
    if head_idx >= 0:
        for s in lines[head_idx + 1:end]:
            m = _KW_RE.match(s)
            if m and m.group(1).strip().lower() == keyword:
                return lines, "already present"
        last_bullet = head_idx
        for j in range(head_idx + 1, end):
            if _KW_RE.match(lines[j]):
                last_bullet = j
        insert_at = (last_bullet + 1) if last_bullet != head_idx else (head_idx + 1)
        lines = lines[:insert_at] + [bullet] + lines[insert_at:]
        return lines, "appended"
    label = (category_label or "").strip() or category_key.replace("_", " ").title()
    block = [f"## {label}", bullet, ""]
    rel_idx = next((i for i, ln in enumerate(lines)
                    if ln.startswith("## ") and ln[3:].strip().lower() == "related"), -1)
    if rel_idx >= 0:
        lines = lines[:rel_idx] + block + lines[rel_idx:]
    else:
        if lines and lines[-1].strip():
            lines = lines + [""]
        lines = lines + block
    return lines, "created"


def _lines_remove_keyword(lines, category_key, keyword):
    """Delete the single bullet line for `keyword` within category_key. Returns (lines, status)."""
    keyword = (keyword or "").strip().lower()
    head_idx, end = _section_span(lines, category_key)
    if head_idx < 0:
        return lines, "category not found"
    for j in range(head_idx + 1, end):
        m = _KW_RE.match(lines[j])
        if m and m.group(1).strip().lower() == keyword:
            return lines[:j] + lines[j + 1:], "removed"
    return lines, "keyword not present"


def _lines_rename_category(lines, category_key, new_label):
    """Rewrite only the `## heading` label text. Returns (lines, status, new_key)."""
    new_label = (new_label or "").strip()
    new_key = _norm_category_key(new_label)
    if not new_key:
        return lines, "empty name", ""
    head_idx, _ = _section_span(lines, category_key)
    if head_idx < 0:
        return lines, "category not found", ""
    if new_key != category_key and _section_span(lines, new_key)[0] >= 0:
        return lines, "target name already exists", ""
    lines = list(lines)
    lines[head_idx] = f"## {new_label}"
    return lines, "renamed", new_key


def _lines_add_category(lines, label):
    """Create an empty `## label` section before Related. Returns (lines, status, key)."""
    label = (label or "").strip()
    key = _norm_category_key(label)
    if not key:
        return lines, "empty name", ""
    if _section_span(lines, key)[0] >= 0:
        return lines, "already exists", key
    block = [f"## {label}", ""]
    rel_idx = next((i for i, ln in enumerate(lines)
                    if ln.startswith("## ") and ln[3:].strip().lower() == "related"), -1)
    if rel_idx >= 0:
        lines = lines[:rel_idx] + block + lines[rel_idx:]
    else:
        if lines and lines[-1].strip():
            lines = lines + [""]
        lines = lines + block
    return lines, "created", key


def _lines_delete_category(lines, category_key):
    """Remove the whole `## heading` section (heading + prose + bullets + trailing blanks up to the
    next heading). The preceding separator blank stays, so spacing between the surviving sections is
    preserved. Returns (lines, status)."""
    head_idx, end = _section_span(lines, category_key)
    if head_idx < 0:
        return lines, "category not found"
    return lines[:head_idx] + lines[end:], "deleted"


def _append_spending_keyword(category_key: str, keyword: str, category_label: str = "") -> str:
    """Back-compat wrapper used by /api/bills/categorize's teach path: read → add keyword → write."""
    keyword = (keyword or "").strip().lower()
    if not keyword:
        return "no keyword"
    lines, status = _lines_add_keyword(_read_spending_note_lines(), category_key, keyword, category_label)
    if status in ("appended", "created"):
        _write_spending_note_lines(lines)
    return status


@app.post("/api/bills/categorize")
async def api_bills_categorize(request: Request):
    """S85: categorise one transaction. Body: {id, category, repeat_merchant?}.
      - Always sets THIS row's category + manual=TRUE (a specific, sticky pick that survives
        re-import and overrides any merchant rule).
      - repeat_merchant=TRUE additionally stores a merchant->category rule keyed on the row's
        normalised merchant, so every OTHER transaction from that merchant (backfill and future)
        auto-applies the category with no further touches. repeat_merchant=FALSE leaves this one row
        categorised and future same-merchant rows in the uncategorised queue.
    `category` must be one of the fixed keys (Uncategorised is not pickable). No model, no CSV."""
    if is_demo(request):
        return JSONResponse({"ok": False, "error": "demo mode is read-only"}, status_code=403)
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "bad request"}, status_code=400)
    try:
        txn_id = int(data.get("id"))
    except (TypeError, ValueError):
        return JSONResponse({"ok": False, "error": "missing/invalid id"}, status_code=400)
    category_key = _norm_category_key(str(data.get("category") or ""))
    if not category_key or category_key == UNCATEGORISED:
        return JSONResponse({"ok": False, "error": "no category"}, status_code=400)
    repeat_merchant = bool(data.get("repeat_merchant") or data.get("teach"))

    conn = await pg_conn()
    try:
        await _ensure_spending_schema(conn)
        if not await category_exists(conn, category_key):
            return JSONResponse({"ok": False, "error": "unknown category — add it first"}, status_code=400)
        row = await conn.fetchrow(
            "UPDATE transactions SET category=$1, manual=TRUE WHERE id=$2 RETURNING description",
            category_key, txn_id)
        if row is None:
            return JSONResponse({"ok": False, "error": f"no transaction with id {txn_id}"}, status_code=404)
        rule_merchant = ""
        if repeat_merchant:
            rule_merchant = _merchant_key(row["description"])
            if rule_merchant:
                await conn.execute(
                    """INSERT INTO merchant_category_rules (merchant_key, category) VALUES ($1,$2)
                       ON CONFLICT (merchant_key) DO UPDATE SET category=EXCLUDED.category,
                                                               created_at=now()""",
                    rule_merchant, category_key)
    finally:
        await conn.close()
    return {"ok": True, "id": txn_id, "category": category_key, "manual": True,
            "rule_created": bool(rule_merchant), "merchant": rule_merchant}


# --- S85: merchant rule list + delete (undo a rule; matching non-manual rows revert to uncategorised) ---
@app.get("/api/bills/merchant-rules")
async def api_merchant_rules():
    """List merchant->category rules with how many current transactions each covers. Read-only."""
    try:
        conn = await pg_conn()
        try:
            await _ensure_spending_schema(conn)
            rules = await conn.fetch(
                "SELECT merchant_key, category, created_at FROM merchant_category_rules ORDER BY merchant_key")
            all_desc = await conn.fetch("SELECT description FROM transactions")
        finally:
            await conn.close()
        from collections import Counter
        counts = Counter(_merchant_key(r["description"]) for r in all_desc)
        out = [{"merchant": r["merchant_key"], "category": r["category"],
                "label": SPENDING_FIXED_LABELS.get(r["category"], r["category"]),
                "matches": counts.get(r["merchant_key"], 0)} for r in rules]
        return {"ok": True, "count": len(out), "rules": out}
    except Exception as e:
        logger.error(f"/api/bills/merchant-rules error: {e}")
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.post("/api/bills/merchant-rules/delete")
async def api_merchant_rules_delete(request: Request):
    """Remove a merchant rule. Matching non-manual transactions revert to uncategorised on the next
    live recompute; manual per-transaction picks are untouched. Body: {merchant}."""
    if is_demo(request):
        return JSONResponse({"ok": False, "error": "demo mode is read-only"}, status_code=403)
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "bad request"}, status_code=400)
    merchant = str(data.get("merchant") or "").strip()
    if not merchant:
        return JSONResponse({"ok": False, "error": "missing merchant"}, status_code=400)
    conn = await pg_conn()
    try:
        await _ensure_spending_schema(conn)
        deleted = await conn.fetchval(
            "DELETE FROM merchant_category_rules WHERE merchant_key=$1 RETURNING merchant_key", merchant)
    finally:
        await conn.close()
    return {"ok": bool(deleted), "deleted": deleted or ""}


# --- S73: manage the note's rules + preview a change's blast radius ---------

@app.get("/api/bills/keyword-impact")
async def api_bills_keyword_impact(request: Request, keyword: str = ""):
    """Blast-radius preview for a keyword BEFORE committing it: how many current transactions its
    substring would match and how they're categorised right now. AGGREGATE COUNTS ONLY — never any
    transaction-level detail. MANUAL (sticky) rows are EXCLUDED from the match count (their category
    won't change) and reported separately so it's clear why an already-fixed row doesn't move."""
    kw = (keyword or "").strip().lower()
    if not kw:
        return {"ok": True, "keyword": "", "matches": 0, "by_current_category": {}, "manual_excluded": 0}
    try:
        rules = load_spending_categories()
        conn = await pg_conn()
        try:
            await _ensure_spending_schema(conn)
            rows = await conn.fetch("SELECT description, category, manual FROM transactions")
        finally:
            await conn.close()
        by_cat = {}
        matches = manual_excluded = 0
        for r in rows:
            if kw not in (r["description"] or "").lower():
                continue
            if r["manual"]:
                manual_excluded += 1
                continue
            matches += 1
            eff = categorise_txn(r["description"], rules)   # current (pre-change) category
            by_cat[eff] = by_cat.get(eff, 0) + 1
        return {"ok": True, "keyword": kw, "matches": matches,
                "by_current_category": dict(sorted(by_cat.items(), key=lambda x: -x[1])),
                "manual_excluded": manual_excluded}
    except Exception as e:
        logger.error(f"/api/bills/keyword-impact error: {e}")
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


async def _migrate_category_rows(old_key, new_key):
    """Sticky-row bookkeeping when a category is renamed: carry manual rows from old_key to new_key so
    a fixed transaction follows the rename instead of orphaning under a heading that no longer exists.
    (Non-manual rows re-derive live from the note, so they need no DB touch.) Returns rows changed."""
    conn = await pg_conn()
    try:
        await _ensure_spending_schema(conn)
        n = await conn.fetchval(
            "WITH u AS (UPDATE transactions SET category=$2 WHERE category=$1 RETURNING 1) "
            "SELECT count(*) FROM u", old_key, new_key)
    finally:
        await conn.close()
    return n or 0


async def _unstick_category_rows(category_key):
    """When a category is DELETED, its sticky rows can't keep pointing at a gone heading — clear
    manual so they re-derive against the remaining rules (landing on the next match or Uncategorised),
    exactly like the live recompute. Returns rows changed."""
    conn = await pg_conn()
    try:
        await _ensure_spending_schema(conn)
        n = await conn.fetchval(
            "WITH u AS (UPDATE transactions SET manual=FALSE, category=$2 WHERE category=$1 "
            "RETURNING 1) SELECT count(*) FROM u", category_key, UNCATEGORISED)
    finally:
        await conn.close()
    return n or 0


@app.post("/api/bills/category/keyword/add")
async def api_bills_keyword_add(request: Request):
    """Add a keyword to a category (creating the category if new). Body {category, keyword, category_label?}."""
    if is_demo(request):
        return JSONResponse({"ok": False, "error": "demo mode is read-only"}, status_code=403)
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "bad request"}, status_code=400)
    key = _norm_category_key(str(data.get("category") or ""))
    keyword = str(data.get("keyword") or "").strip().lower()
    label = str(data.get("category_label") or data.get("category") or "").strip()
    if not key or key == "related":
        return JSONResponse({"ok": False, "error": "invalid category"}, status_code=400)
    if not keyword:
        return JSONResponse({"ok": False, "error": "no keyword"}, status_code=400)
    try:
        lines, status = _lines_add_keyword(_read_spending_note_lines(), key, keyword, label)
        if status in ("appended", "created"):
            _write_spending_note_lines(lines)
        return {"ok": True, "status": status, "category": key, "keyword": keyword}
    except OSError as e:
        logger.error(f"/api/bills/category/keyword/add error: {e}")
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.post("/api/bills/category/keyword/remove")
async def api_bills_keyword_remove(request: Request):
    """Remove one keyword from a category. Body {category, keyword}. Non-manual transactions that only
    matched via this keyword re-derive live to their next matching rule (or Uncategorised) on next read."""
    if is_demo(request):
        return JSONResponse({"ok": False, "error": "demo mode is read-only"}, status_code=403)
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "bad request"}, status_code=400)
    key = _norm_category_key(str(data.get("category") or ""))
    keyword = str(data.get("keyword") or "").strip().lower()
    if not key or not keyword:
        return JSONResponse({"ok": False, "error": "category and keyword required"}, status_code=400)
    try:
        lines, status = _lines_remove_keyword(_read_spending_note_lines(), key, keyword)
        if status == "removed":
            _write_spending_note_lines(lines)
        return {"ok": True, "status": status, "category": key, "keyword": keyword}
    except OSError as e:
        logger.error(f"/api/bills/category/keyword/remove error: {e}")
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.post("/api/bills/category/rename")
async def api_bills_category_rename(request: Request):
    """S90: rename a category's LABEL in place. Body {category, new_label}. The key is UNCHANGED, so
    every transaction and merchant rule stays linked with zero data movement — only the display label
    changes."""
    if is_demo(request):
        return JSONResponse({"ok": False, "error": "demo mode is read-only"}, status_code=403)
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "bad request"}, status_code=400)
    key = _norm_category_key(str(data.get("category") or ""))
    new_label = str(data.get("new_label") or data.get("new_name") or "").strip()
    if not key:
        return JSONResponse({"ok": False, "error": "invalid category"}, status_code=400)
    if not new_label:
        return JSONResponse({"ok": False, "error": "new_label required"}, status_code=400)
    try:
        conn = await pg_conn()
        try:
            await _ensure_spending_schema(conn)
            updated = await conn.fetchval(
                "UPDATE spending_categories SET label=$2 WHERE key=$1 RETURNING key", key, new_label)
        finally:
            await conn.close()
        if updated is None:
            return JSONResponse({"ok": False, "error": f"no category '{key}'"}, status_code=404)
        return {"ok": True, "category": key, "label": new_label}
    except Exception as e:
        logger.error(f"/api/bills/category/rename error: {e}")
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.post("/api/bills/category/merge")
async def api_bills_category_merge(request: Request):
    """S90: merge category `source` INTO `target` — reassign every transaction in source to target,
    repoint every merchant rule from source to target, then delete source. Unlike delete, this
    PRESERVES categorisation (nothing goes back to uncategorised). Body {source, target}. Destructive
    and hard to undo — the UI confirms first."""
    if is_demo(request):
        return JSONResponse({"ok": False, "error": "demo mode is read-only"}, status_code=403)
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "bad request"}, status_code=400)
    source = _norm_category_key(str(data.get("source") or ""))
    target = _norm_category_key(str(data.get("target") or ""))
    if not source or not target:
        return JSONResponse({"ok": False, "error": "source and target required"}, status_code=400)
    if source == target:
        return JSONResponse({"ok": False, "error": "source and target are the same"}, status_code=400)
    if source == UNCATEGORISED:
        return JSONResponse({"ok": False, "error": "cannot merge the uncategorised bucket"}, status_code=400)
    try:
        conn = await pg_conn()
        try:
            await _ensure_spending_schema(conn)
            if not await category_exists(conn, source):
                return JSONResponse({"ok": False, "error": f"no category '{source}'"}, status_code=404)
            if not await category_exists(conn, target):
                return JSONResponse({"ok": False, "error": f"no target category '{target}'"}, status_code=404)
            async with conn.transaction():
                txns_moved = await conn.fetchval(
                    "WITH u AS (UPDATE transactions SET category=$2 WHERE category=$1 RETURNING 1) "
                    "SELECT count(*) FROM u", source, target) or 0
                rules_moved = await conn.fetchval(
                    "WITH u AS (UPDATE merchant_category_rules SET category=$2 WHERE category=$1 RETURNING 1) "
                    "SELECT count(*) FROM u", source, target) or 0
                await conn.execute("DELETE FROM spending_categories WHERE key=$1", source)
        finally:
            await conn.close()
        return {"ok": True, "source": source, "target": target,
                "transactions_moved": txns_moved, "rules_moved": rules_moved}
    except Exception as e:
        logger.error(f"/api/bills/category/merge error: {e}")
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.post("/api/bills/category/exclude")
async def api_bills_category_exclude(request: Request):
    """S90: flag/unflag a category as an own-account transfer (excluded from income/spent totals).
    Body {category, exclude:bool}. Drives the summary's Transfers bucket — DB-driven, not hardcoded."""
    if is_demo(request):
        return JSONResponse({"ok": False, "error": "demo mode is read-only"}, status_code=403)
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "bad request"}, status_code=400)
    key = _norm_category_key(str(data.get("category") or ""))
    exclude = bool(data.get("exclude"))
    if not key:
        return JSONResponse({"ok": False, "error": "invalid category"}, status_code=400)
    try:
        conn = await pg_conn()
        try:
            await _ensure_spending_schema(conn)
            updated = await conn.fetchval(
                "UPDATE spending_categories SET exclude_from_totals=$2 WHERE key=$1 RETURNING key", key, exclude)
        finally:
            await conn.close()
        if updated is None:
            return JSONResponse({"ok": False, "error": f"no category '{key}'"}, status_code=404)
        return {"ok": True, "category": key, "exclude_from_totals": exclude}
    except Exception as e:
        logger.error(f"/api/bills/category/exclude error: {e}")
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.post("/api/bills/category/add")
async def api_bills_category_add(request: Request):
    """S87: create a new category in the DB-backed list. Body {name}. The label is kept verbatim; the
    key is its normalised form. Rejects an empty/reserved name or a duplicate key."""
    if is_demo(request):
        return JSONResponse({"ok": False, "error": "demo mode is read-only"}, status_code=403)
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "bad request"}, status_code=400)
    name = str(data.get("name") or "").strip()
    key = _norm_category_key(name)
    if not key or key in (UNCATEGORISED, "related"):
        return JSONResponse({"ok": False, "error": "invalid name"}, status_code=400)
    try:
        conn = await pg_conn()
        try:
            await _ensure_spending_schema(conn)
            if await category_exists(conn, key):
                return JSONResponse({"ok": False, "error": "category already exists"}, status_code=400)
            nxt = await conn.fetchval("SELECT COALESCE(MAX(sort_order),0)+1 FROM spending_categories")
            await conn.execute(
                "INSERT INTO spending_categories (key,label,sort_order) VALUES ($1,$2,$3)",
                key, name, nxt)
        finally:
            await conn.close()
        return {"ok": True, "status": "created", "category": key, "name": name}
    except Exception as e:
        logger.error(f"/api/bills/category/add error: {e}")
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.post("/api/bills/category/delete")
async def api_bills_category_delete(request: Request):
    """S87: delete a category. Every transaction in it becomes UNDEFINED (manual cleared, category ->
    uncategorised, so it re-enters the queue) and any merchant rule pointing at it is removed. Body
    {category}."""
    if is_demo(request):
        return JSONResponse({"ok": False, "error": "demo mode is read-only"}, status_code=403)
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "bad request"}, status_code=400)
    key = _norm_category_key(str(data.get("category") or ""))
    if not key or key == UNCATEGORISED:
        return JSONResponse({"ok": False, "error": "invalid category"}, status_code=400)
    try:
        conn = await pg_conn()
        try:
            await _ensure_spending_schema(conn)
            deleted = await conn.fetchval(
                "DELETE FROM spending_categories WHERE key=$1 RETURNING key", key)
            if deleted is None:
                return JSONResponse({"ok": False, "error": f"no category '{key}'"}, status_code=404)
            rules_removed = await conn.fetchval(
                "WITH d AS (DELETE FROM merchant_category_rules WHERE category=$1 RETURNING 1) "
                "SELECT count(*) FROM d", key) or 0
        finally:
            await conn.close()
        unstuck = await _unstick_category_rows(key)   # its transactions -> undefined (uncategorised)
        return {"ok": True, "status": "deleted", "category": key,
                "rows_undefined": unstuck, "rules_removed": rules_removed}
    except Exception as e:
        logger.error(f"/api/bills/category/delete error: {e}")
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.post("/webhook/scrolling")
async def scrolling_webhook(request: Request):
    try:
        data = await request.json()
    except Exception:
        return {"ok": False, "error": "bad json"}
    app_name = str(data.get("app", "unknown")).strip() or "unknown"
    try:
        duration = int(float(str(data.get("duration_minutes", 0)).strip() or 0))
    except ValueError:
        duration = 0
    now = datetime.now(LOCAL_TZ)

    # Any webhook hit is phone activity, regardless of suppression/grace outcome —
    # the scheduler's inactivity-aware escalation freeze (session 17) reads this.
    try:
        r = redis_conn()
        await r.set("last_phone_activity", now.timestamp(), ex=3 * 86400)
        await r.aclose()
    except Exception as e:
        logger.error(f"activity signal error: {e}")

    # Suppression window: if the user told Ghost she's messaging/researching/posting (not
    # doom-scrolling), skip the interrupt entirely — no nudge, and don't count it as a
    # scrolling event, because it isn't one. Only the scrolling interrupt is affected;
    # scheduler nudges (water/meal/movement) never look at this key.
    try:
        r = redis_conn()
        until = await r.get("scroll_suppressed_until")
        await r.aclose()
        if until and now.timestamp() < float(until):
            logger.info(f"Scrolling interrupt SUPPRESSED ({app_name} {duration}min) — active window")
            return {"ok": True, "suppressed": True}
    except Exception as e:
        logger.error(f"scroll suppression check error: {e}")

    # Task 4 (session 15): 12-minute grace floor. NOTE the first-fire threshold really
    # lives in Tasker on the phone (currently >15 min) — this server-side floor only
    # matters if that profile is ever lowered; it guarantees nothing fires before ~12min.
    # Below the floor: no message, no count, no event — it's grace, not scrolling yet.
    MIN_INTERRUPT_MINUTES = 12
    if duration < MIN_INTERRUPT_MINUTES:
        logger.info(f"Scrolling webhook below {MIN_INTERRUPT_MINUTES}min grace ({app_name} {duration}min) — ignored")
        return {"ok": True, "below_grace": True}

    count = 1
    try:
        r = redis_conn()
        key = f"scroll_count:{app_name.lower()}"
        count = await r.incr(key)
        await r.expire(key, seconds_to_midnight())
        await r.aclose()
    except Exception as e:
        logger.error(f"webhook redis error: {e}")

    try:
        conn = await pg_conn()
        await conn.execute("""
            INSERT INTO daily_events (date, event_type, value) VALUES ($1, 'scrolling_interrupt', $2)
            ON CONFLICT (date, event_type)
            DO UPDATE SET value = COALESCE(daily_events.value, 0) + EXCLUDED.value
        """, now.date(), duration)
        await conn.close()
    except Exception as e:
        logger.error(f"webhook postgres error: {e}")

    # Task 4: the ladder is now TWO steps total per app per day, then silence.
    # Step 1 dry, step 2 firmer and explicitly the last word on it — after that,
    # further sessions are still recorded (above) but send nothing. Ghost making
    # the point twice and then shutting up is the design, not a failure.
    if count > 2:
        logger.info(f"Scrolling interrupt CAPPED ({app_name} {duration}min, session #{count} today) — two-step ladder spent, logged only")
        return {"ok": True, "capped": True, "count_today": count}

    # Rotate the ANGLE so interrupts don't rhyme (session 12 fix — keep it).
    angle = SCROLL_ANGLES[(count - 1) % len(SCROLL_ANGLES)]
    if count == 1:
        tone = "Dry, light, not a lecture."
    else:
        tone = ("Firmer, pointed but not cruel — and make it read as final: this is the last "
                "she'll hear from you about scrolling today, without saying 'last warning' like a cop.")
    hint = (f"{tone} Take THIS angle and no other: {angle} "
            "Do NOT mention how many times she's checked, do not say 'still there' or 'you keep going back', "
            "do not count. One line.")
    fallback = (f"{duration} minutes on {app_name}. Put it down." if count == 1
                else f"{app_name} again. Whatever you're avoiding is still going to be there.")
    try:
        message = await claude_line(
            f"{bot.USER_NAME} has been scrolling {app_name} on her phone for {duration} minutes straight. {hint}")
    except Exception as e:
        logger.error(f"webhook claude error: {e}")
        message = fallback

    try:
        await send_telegram(message)
    except Exception as e:
        logger.error(f"webhook telegram error: {e}")
        return {"ok": False, "error": "telegram send failed"}
    try:
        # source 'webhook' (a sixth source beyond the original five) — these are real
        # Telegram sends and belong in the unified feed, but attributing them to
        # 'scheduler' would be false.
        await bot.log_message("webhook", "nudge", message)
    except Exception as e:
        logger.error(f"webhook message_log error: {e}")
    logger.info(f"Scrolling interrupt sent: {app_name} {duration}min (step {count} of 2 today)")
    return {"ok": True, "count_today": count}

# ---------------------------------------------------------------------------
# Passive Tasker webhooks (session 17, task 3). All three are RECORD-ONLY —
# they never send a message. Categorization happens at read time in the
# scheduler's evening summary, against the user-editable vault note
# 'Projects/Tech/Ghost/Ghost — App Categories.md', so edits apply retroactively.
# ---------------------------------------------------------------------------
# Server-side debounce for the app_usage webhook (Session 33, 04/08/2026). Android/
# Tasker's "App Changed" trigger fires more than once per real switch — a transient
# launcher / system-UI foreground pass registers as its own foreground-app event.
# Observed: 3 real switches -> 13 POSTs, several with identical source ports firing
# back-to-back within the same second. A window this small collapses those duplicates
# while staying far below any legitimate re-open of the same app later in the day
# (minutes apart), so genuine separate sessions still count.
APP_USAGE_DEBOUNCE_SECONDS = 3

# Lea-editable ignore-list for launcher/system packages (Session 34, 05/08/2026).
# These fire as their own foreground-app event on every real app switch (the same
# Tasker double-firing behind the debounce above) and must never be counted at all —
# not even as a neutral row. The list lives in the "Ignored" section of the App
# Categories vault note so Lea can edit it without a redeploy, same manifest pattern
# as the not-productive/streaming lists the scheduler reads. Re-read at most every
# APP_IGNORE_CACHE_SECONDS; only successful reads are cached, so a transient unreadable
# note fails open (nothing ignored) and recovers on the very next request.
APP_CATEGORIES_REL = os.environ.get("GHOST_APP_CATEGORIES_REL", "Projects/Tech/Ghost/Ghost — App Categories.md")  # single-sourced via env S56 (was the same literal pasted here + in scheduler.py)
APP_IGNORE_CACHE_SECONDS = 120
_app_ignore_cache = {"ts": 0.0, "data": None}

def load_app_ignore_list() -> set:
    now = time.monotonic()
    cached = _app_ignore_cache["data"]
    if cached is not None and now - _app_ignore_cache["ts"] < APP_IGNORE_CACHE_SECONDS:
        return cached
    try:
        ignore = set()
        path = os.path.join(bot.VAULT_PATH, APP_CATEGORIES_REL)
        section = False
        for line in open(path, encoding="utf-8").read().splitlines():
            if line.startswith("## "):
                section = line[3:].strip().lower().startswith("ignored")
                continue
            if section:
                m = re.match(r"\s*-\s*`([^`]+)`", line)
                if m:
                    ignore.add(m.group(1).strip().lower())
        _app_ignore_cache["ts"] = now
        _app_ignore_cache["data"] = ignore
        return ignore
    except OSError as e:
        logger.error(f"app ignore-list note unreadable ({e}) — nothing ignored this cycle")
        return cached if cached is not None else set()

# Package -> friendly display name for the timeline (Session 31). Lea-editable "Display
# names" section of the App Categories note, same manifest+cache pattern as the ignore
# list. Data was verified clean (real package strings, no garbled captures), so this is
# purely a DISPLAY mapping — the raw dotted package is never shown; unmapped packages get
# a cleaned last-segment fallback via friendly_app_name().
_app_names_cache = {"ts": 0.0, "data": None}
_APP_GENERIC = {"com", "org", "net", "io", "ch", "se", "app", "apps", "android"}

def load_app_display_names() -> dict:
    now = time.monotonic()
    cached = _app_names_cache["data"]
    if cached is not None and now - _app_names_cache["ts"] < APP_IGNORE_CACHE_SECONDS:
        return cached
    try:
        names = {}
        path = os.path.join(bot.VAULT_PATH, APP_CATEGORIES_REL)
        section = False
        for line in open(path, encoding="utf-8").read().splitlines():
            if line.startswith("## "):
                section = line[3:].strip().lower().startswith("display names")
                continue
            if section:
                m = re.match(r"\s*-\s*`([^`]+)`\s*:\s*(.+?)\s*$", line)
                if m:
                    names[m.group(1).strip().lower()] = m.group(2).strip()
        _app_names_cache["ts"] = now
        _app_names_cache["data"] = names
        return names
    except OSError as e:
        logger.error(f"app display-names note unreadable ({e}) — fallback naming only")
        return cached if cached is not None else {}

def friendly_app_name(pkg: str, mapping: dict) -> str:
    """Mapped name if present, else a readable fallback (last meaningful package segment,
    camelCase/underscore split, title-cased). NEVER returns the raw dotted package."""
    key = (pkg or "").strip().lower()
    if key in mapping:
        return mapping[key]
    segs = [s for s in key.split(".") if s]
    meaningful = [s for s in segs if s not in _APP_GENERIC] or segs
    name = meaningful[-1] if meaningful else key
    name = re.sub(r"([a-z])([A-Z])", r"\1 \2", name).replace("_", " ")
    return (name[:1].upper() + name[1:]) if name else (pkg or "app")

@app.post("/webhook/app_usage")
async def app_usage_webhook(request: Request):
    """Tasker reports an app session: {"app": name, "duration_minutes": n}.

    Always returns HTTP 200 (even on reject) so Tasker never retries and inflates
    counts — but EVERY reject/failure path now logs loudly. Session 33 (04/08/2026)
    fixed the silent-failure bug: the bad-json and validation guards used to return
    200 with no log at all, so malformed Tasker payloads vanished without a trace
    (13 real POSTs -> 0 rows -> 0 log lines, no error to grep for). Duration is now
    rounded, not truncated, so a real sub-minute session (e.g. "0.75") no longer
    silently becomes 0 and gets dropped."""
    raw = await request.body()
    try:
        data = json.loads(raw)
    except Exception:
        logger.error(f"app_usage REJECTED: bad json (raw body {raw[:200]!r})")
        return {"ok": False, "error": "bad json"}
    app_name = str(data.get("app", "")).strip().lower()
    # Ignore-list check runs BEFORE validation or any write: a launcher/system
    # package is dropped cleanly (no row, not even neutral) but logged at INFO so
    # it's visible, not silent (consistent with session 28's loud-not-silent fix).
    if app_name and app_name in load_app_ignore_list():
        logger.info(f"app_usage IGNORED: {app_name} (launcher/system list)")
        return {"ok": True, "ignored": True}
    try:
        minutes = round(float(str(data.get("duration_minutes", 0)).strip() or 0))
    except ValueError:
        minutes = 0
    if not app_name or minutes <= 0:
        logger.warning(f"app_usage REJECTED: need app + positive duration_minutes "
                       f"(payload={data!r} -> app={app_name!r} minutes={minutes})")
        return {"ok": False, "error": "need app and positive duration_minutes"}
    now = datetime.now(LOCAL_TZ)
    debounced = False
    try:
        r = redis_conn()
        await r.set("last_phone_activity", now.timestamp(), ex=3 * 86400)
        # SET NX EX: only the FIRST POST for this app inside the window acquires the
        # key and proceeds to write; near-simultaneous duplicates fail NX and are
        # collapsed (dropped, not summed). Fail-open below if Redis is unreachable.
        acquired = await r.set(f"appusage:debounce:{app_name}", now.timestamp(),
                               nx=True, ex=APP_USAGE_DEBOUNCE_SECONDS)
        debounced = not acquired
        await r.aclose()
    except Exception as e:
        logger.error(f"app_usage activity/debounce redis error: {e}")
        debounced = False  # fail-open: never drop a real write because Redis is down
    if debounced:
        logger.info(f"app_usage DEBOUNCED: {app_name} +{minutes}min dropped "
                    f"(duplicate within {APP_USAGE_DEBOUNCE_SECONDS}s of prior POST)")
        return {"ok": True, "debounced": True}
    try:
        conn = await pg_conn()
        await conn.execute("""
            INSERT INTO app_usage (date, app, minutes) VALUES ($1, $2, $3)
            ON CONFLICT (date, app) DO UPDATE SET minutes = app_usage.minutes + EXCLUDED.minutes
        """, now.date(), app_name, minutes)
        await conn.close()
    except Exception as e:
        logger.error(f"app_usage postgres error: {e}")
        return {"ok": False, "error": "db write failed"}
    logger.info(f"App usage recorded: {app_name} +{minutes}min")
    return {"ok": True}

@app.post("/webhook/unlock")
async def unlock_webhook(request: Request):
    """Tasker's "Display Unlocked" profile POSTs here (empty body is fine). Two jobs:
    (1) count screen unlocks for the day; (2) S79 Task 5 — on the FIRST unlock of the
    Melbourne calendar day, set `morning_unlock_signal` so the scheduler fires the morning
    debrief the moment she wakes. Gated to exactly one trigger/day via SET NX with a
    midnight TTL; the scheduler's 7:30 fallback still covers a day with no unlock signal."""
    now = datetime.now(LOCAL_TZ)
    midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    ttl = max(int((midnight - now).total_seconds()), 60)
    first_today = False
    try:
        r = redis_conn()
        await r.set("last_phone_activity", now.timestamp(), ex=3 * 86400)
        # nx=True -> only the first unlock of the day writes it (returns True); later unlocks no-op.
        first_today = bool(await r.set("morning_unlock_signal", now.timestamp(), nx=True, ex=ttl))
        await r.aclose()
    except Exception as e:
        logger.error(f"unlock signal error: {e}")
    try:
        conn = await pg_conn()
        await conn.execute("""
            INSERT INTO daily_events (date, event_type, value) VALUES ($1, 'screen_unlocks', 1)
            ON CONFLICT (date, event_type)
            DO UPDATE SET value = COALESCE(daily_events.value, 0) + 1
        """, now.date())
        await conn.close()
    except Exception as e:
        logger.error(f"unlock postgres error: {e}")
    return {"ok": True, "first_unlock_today": first_today}

@app.post("/webhook/wifi")
async def wifi_webhook(request: Request):
    """Tasker reports Wi-Fi state: {"ssid": name} on connect, {"ssid": ""} on
    disconnect. Stores the raw SSID only — the scheduler maps it to home/work/neither
    against the vault note each tick. Deliberately does NOT count as phone activity
    (the phone joins networks on its own; that's presence, not her using it)."""
    raw = await request.body()
    try:
        data = json.loads(raw)
    except Exception:
        logger.error(f"wifi REJECTED: bad json (raw body {raw[:200]!r})")
        return {"ok": False, "error": "bad json"}
    ssid = str(data.get("ssid", "")).strip()
    try:
        r = redis_conn()
        if ssid:
            await r.set("wifi_ssid_current", ssid, ex=12 * 3600)
        else:
            await r.delete("wifi_ssid_current")
        await r.aclose()
    except Exception as e:
        logger.error(f"wifi webhook redis error: {e}")
        return {"ok": False, "error": "redis write failed"}
    logger.info(f"Wi-Fi state recorded: {ssid or '(disconnected)'}")
    return {"ok": True, "ssid": ssid or None}


# Session 36: the v8 command-center page is served here at "/", SAME-ORIGIN with the
# API, so its relative fetch("/api/command-center") needs no CORS. It's read from the
# vault mount at request time (no new configmap/mount — least surface), so editing the
# .html in Obsidian updates the live page. no-store so a fresh copy is always served.
# Candidate locations for the served page — the vault Ghost folder got reorganised
# (Projects/Ghost -> Projects/Tech/Ghost), so try both rather than hardcode one and
# 500 when it moves again. First one that exists wins.
DASHBOARD_HTML_CANDIDATES = [
    "Projects/Tech/Ghost/ghost-dashboard-live.html",
    "Projects/Ghost/ghost-dashboard-live.html",
]

# ===========================================================================
# S92 — Ghost App v1 backend. The native Android app's API, under /api/app/v1/.
# Single user (OWNER_ID). Auth: a per-device bearer token (k8s secret GHOST_APP_API_TOKEN),
# ON TOP of tailnet-only — a leaked tailnet is not enough on its own. Every write accepts an
# optional client_id (UUID) and is idempotent (app_idempotency table) so the phone's offline
# queue can retry safely. Nutrition is calories+protein only; food lookup = my-foods -> Open
# Food Facts -> USDA (key from secret), cached into `foods`. Chat routes through the SAME brain
# (bot.process_message) + history (message_log) as Telegram — one conversation, two front ends.
# ===========================================================================
APP_API_TOKEN = os.environ.get("GHOST_APP_API_TOKEN", "")
USDA_API_KEY = os.environ.get("GHOST_USDA_API_KEY", "")
APP_CAL_TARGET_DEFAULT = float(os.environ.get("GHOST_CALORIE_TARGET") or 2600)
APP_PROTEIN_TARGET_DEFAULT = float(os.environ.get("GHOST_PROTEIN_TARGET") or 110)
_APP_SCHEMA_READY = False
_OFF_SEARCH = "https://search.openfoodfacts.org/search"
_USDA_SEARCH = "https://api.nal.usda.gov/fdc/v1/foods/search"
_OFF_PRODUCT = "https://world.openfoodfacts.org/api/v2/product/"


async def _ensure_app_schema(conn):
    global _APP_SCHEMA_READY
    if _APP_SCHEMA_READY:
        return
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS foods (
            id BIGSERIAL PRIMARY KEY,
            user_id BIGINT,
            name TEXT NOT NULL,
            brand TEXT,
            source TEXT NOT NULL DEFAULT 'manual',   -- manual | off | usda
            source_id TEXT,                          -- OFF barcode / USDA fdcId
            serving_desc TEXT,
            calories NUMERIC NOT NULL DEFAULT 0,      -- per serving
            protein  NUMERIC NOT NULL DEFAULT 0,      -- per serving (g)
            is_mine  BOOLEAN NOT NULL DEFAULT FALSE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE UNIQUE INDEX IF NOT EXISTS foods_source_idx ON foods(source, source_id) WHERE source_id IS NOT NULL;
        CREATE TABLE IF NOT EXISTS food_log (
            id BIGSERIAL PRIMARY KEY,
            user_id BIGINT NOT NULL,
            food_id BIGINT REFERENCES foods(id) ON DELETE SET NULL,
            food_name TEXT NOT NULL,                 -- snapshot, survives food edits/deletes
            servings NUMERIC NOT NULL DEFAULT 1,
            calories NUMERIC NOT NULL DEFAULT 0,      -- total for this entry
            protein  NUMERIC NOT NULL DEFAULT 0,
            log_date DATE NOT NULL,
            logged_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            client_id TEXT
        );
        CREATE INDEX IF NOT EXISTS food_log_user_date_idx ON food_log(user_id, log_date);
        CREATE TABLE IF NOT EXISTS nutrition_settings (
            user_id BIGINT PRIMARY KEY,
            calorie_target NUMERIC NOT NULL DEFAULT 2600,
            protein_target NUMERIC NOT NULL DEFAULT 110
        );
        CREATE TABLE IF NOT EXISTS app_idempotency (
            client_id  TEXT PRIMARY KEY,
            endpoint   TEXT,
            response   TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
    """)
    _APP_SCHEMA_READY = True


def _app_auth_ok(request) -> bool:
    """Bearer-token check. Fails CLOSED when no token is configured (deny), constant-time compare."""
    if not APP_API_TOKEN:
        return False
    h = request.headers.get("authorization") or ""
    if not h.lower().startswith("bearer "):
        return False
    import hmac
    return hmac.compare_digest(h[7:].strip(), APP_API_TOKEN)


def _app_401():
    return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)


async def _idem_get(conn, client_id):
    if not client_id:
        return None
    row = await conn.fetchrow("SELECT response FROM app_idempotency WHERE client_id=$1", client_id)
    if row and row["response"]:
        try:
            return json.loads(row["response"])
        except Exception:
            return None
    return None


async def _idem_put(conn, client_id, endpoint, response):
    if not client_id:
        return
    try:
        await conn.execute(
            "INSERT INTO app_idempotency (client_id, endpoint, response) VALUES ($1,$2,$3) "
            "ON CONFLICT (client_id) DO NOTHING", client_id, endpoint, json.dumps(response))
    except Exception as e:
        logger.error(f"app idempotency put error: {e}")


async def _nutrition_targets(conn):
    row = await conn.fetchrow(
        "SELECT calorie_target::float8 c, protein_target::float8 p FROM nutrition_settings WHERE user_id=$1", OWNER_ID)
    if row:
        return float(row["c"]), float(row["p"])
    return APP_CAL_TARGET_DEFAULT, APP_PROTEIN_TARGET_DEFAULT


async def _nutrition_today(conn):
    """{eaten_calories, eaten_protein, calorie_target, protein_target, calories_remaining,
    protein_remaining, encouragement} for today (local)."""
    d = datetime.now(LOCAL_TZ).date()
    row = await conn.fetchrow(
        "SELECT COALESCE(SUM(calories),0)::float8 c, COALESCE(SUM(protein),0)::float8 p "
        "FROM food_log WHERE user_id=$1 AND log_date=$2", OWNER_ID, d)
    eaten_c, eaten_p = float(row["c"]), float(row["p"])
    tc, tp = await _nutrition_targets(conn)
    rem_c, rem_p = round(tc - eaten_c, 1), round(tp - eaten_p, 1)
    # encouraging wording — she is working towards weight gain, so "to go" is a good thing
    if rem_c > 0:
        enc = f"{int(round(rem_c))} cal and {int(round(max(rem_p,0)))}g protein to go — keep fuelling."
    else:
        enc = "Target smashed — great fuelling today."
    return {"eaten_calories": round(eaten_c, 1), "eaten_protein": round(eaten_p, 1),
            "calorie_target": tc, "protein_target": tp,
            "calories_remaining": rem_c, "protein_remaining": rem_p, "encouragement": enc}


# ---- food lookup (my foods -> Open Food Facts -> USDA), results cacheable into `foods` --------
def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


async def _search_off(q, limit=5):
    # OFF Search-a-licious (fast full-text); the legacy search.pl is slow/flaky for multi-word queries.
    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(_OFF_SEARCH, params={
                "q": q, "page_size": limit, "fields": "code,product_name,brands,nutriments"},
                headers={"User-Agent": "GhostApp/1.0 (hivequeen, personal)"}, timeout=12.0)
        out = []
        for p in (r.json().get("hits") or [])[:limit]:
            name = p.get("product_name") or ""
            if isinstance(name, dict):          # localised name maps can appear
                name = name.get("en") or next(iter(name.values()), "")
            name = str(name).strip()
            if not name:
                continue
            br = p.get("brands")
            brand = (br[0] if isinstance(br, list) and br
                     else (str(br).split(",")[0].strip() if isinstance(br, str) and br else None))
            nut = p.get("nutriments") or {}
            out.append({"name": name, "brand": brand,
                        "serving_desc": "per 100 g", "calories": round(_f(nut.get("energy-kcal_100g")), 1),
                        "protein": round(_f(nut.get("proteins_100g")), 1), "source": "off",
                        "source_id": str(p.get("code") or "") or None})
        return [x for x in out if x["calories"] or x["protein"]]
    except Exception as e:
        logger.error(f"OFF search error: {e}")
        return []


async def _search_usda(q, limit=5):
    if not USDA_API_KEY:
        return []
    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(_USDA_SEARCH, params={
                "api_key": USDA_API_KEY, "query": q, "pageSize": limit,
                "dataType": "Foundation,SR Legacy,Branded"}, timeout=12.0)
        out = []
        for fd in (r.json().get("foods") or [])[:limit]:
            name = (fd.get("description") or "").strip().title()
            if not name:
                continue
            cal = prot = 0.0
            for n in (fd.get("foodNutrients") or []):
                nm = (n.get("nutrientName") or "").lower()
                val = _f(n.get("value"))
                if "energy" in nm and (n.get("unitName", "").upper() == "KCAL" or cal == 0):
                    cal = val
                elif nm == "protein":
                    prot = val
            out.append({"name": name, "brand": (fd.get("brandOwner") or None),
                        "serving_desc": "per 100 g", "calories": round(cal, 1), "protein": round(prot, 1),
                        "source": "usda", "source_id": str(fd.get("fdcId") or "") or None})
        return [x for x in out if x["calories"] or x["protein"]]
    except Exception as e:
        logger.error(f"USDA search error: {e}")
        return []


async def _cache_food(conn, item):
    """Upsert a picked food into `foods`, return its id. Dedupes external foods by (source, source_id)."""
    src = item.get("source") or "manual"
    sid = item.get("source_id")
    if sid:
        existing = await conn.fetchval("SELECT id FROM foods WHERE source=$1 AND source_id=$2", src, sid)
        if existing:
            return existing
    return await conn.fetchval(
        "INSERT INTO foods (user_id, name, brand, source, source_id, serving_desc, calories, protein, is_mine) "
        "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9) RETURNING id",
        OWNER_ID, item.get("name") or "food", item.get("brand"), src, sid,
        item.get("serving_desc"), _f(item.get("calories")), _f(item.get("protein")),
        bool(item.get("mine")))


@app.get("/api/app/v1/today")
async def app_today(request: Request):
    if not _app_auth_ok(request):
        return _app_401()
    try:
        conn = await pg_conn()
        try:
            await native.ensure_schema(conn)
            await _ensure_app_schema(conn)
            habits = await native.list_habits(user_id=OWNER_ID, conn=conn)
            todos_all = await native.list_todos(user_id=OWNER_ID, include_done=False, conn=conn)
            nutrition = await _nutrition_today(conn)
        finally:
            await conn.close()
        habits_due = [{
            "id": h["id"], "name": h["name"], "type": h["field_type"],
            "target": h.get("target_value"), "unit": h.get("unit"),
            "streak": h["streak"], "logged_value": h.get("today_value", 0),
            "today_count": h.get("today_up", 0), "done_today": h["done_today"],
            "checklist": h.get("checklist", []),
        } for h in habits if h.get("due_today")]
        todos_open = [{
            "id": t["id"], "text": t["text"], "priority": t["priority"],
            "due_date": t["due_date"], "overdue": t["overdue"], "checklist": t.get("checklist", []),
        } for t in todos_all]  # already due-date-first, done excluded
        return {"ok": True, "date": datetime.now(LOCAL_TZ).date().isoformat(),
                "habits": habits_due, "todos": todos_open, "nutrition": nutrition}
    except Exception as e:
        logger.error(f"/api/app/v1/today error: {e}")
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.post("/api/app/v1/habits/{habit_id}/log")
async def app_habit_log(habit_id: int, request: Request):
    """Tick / value-entry / counter-increment a habit. Body {direction?:up|down|check, value?, client_id?}."""
    if not _app_auth_ok(request):
        return _app_401()
    try:
        data = await request.json()
    except Exception:
        data = {}
    client_id = data.get("client_id")
    direction = data.get("direction") or "up"
    value = data.get("value")
    conn = await pg_conn()
    try:
        await _ensure_app_schema(conn)
        cached = await _idem_get(conn, client_id)
        if cached is not None:
            return cached
    finally:
        await conn.close()
    res = await native.tap_habit(habit_id, direction=direction, value=value)
    result = {"ok": bool(res.get("ok")), **res}
    if result["ok"] and client_id:
        conn = await pg_conn()
        try:
            await _idem_put(conn, client_id, f"habit_log:{habit_id}", result)
        finally:
            await conn.close()
    return result if result["ok"] else JSONResponse(result, status_code=404)


@app.post("/api/app/v1/todos")
async def app_todo_create(request: Request):
    """Quick-add a todo. Body {text, priority?, due_date?:YYYY-MM-DD, client_id?}."""
    if not _app_auth_ok(request):
        return _app_401()
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "bad request"}, status_code=400)
    text = (data.get("text") or "").strip()
    if not text:
        return JSONResponse({"ok": False, "error": "text required"}, status_code=400)
    client_id = data.get("client_id")
    conn = await pg_conn()
    try:
        await _ensure_app_schema(conn)
        cached = await _idem_get(conn, client_id)
        if cached is not None:
            return cached
    finally:
        await conn.close()
    due = None
    if data.get("due_date"):
        try:
            due = date.fromisoformat(str(data["due_date"]))
        except ValueError:
            return JSONResponse({"ok": False, "error": "due_date must be YYYY-MM-DD"}, status_code=400)
    tid = await native.create_todo(text, priority=(data.get("priority") or "med"),
                                   due_date=due, user_id=OWNER_ID)
    result = {"ok": tid is not None, "id": tid, "text": text}
    if result["ok"] and client_id:
        conn = await pg_conn()
        try:
            await _idem_put(conn, client_id, "todo_create", result)
        finally:
            await conn.close()
    return result


@app.post("/api/app/v1/todos/{todo_id}/complete")
async def app_todo_complete(todo_id: int, request: Request):
    if not _app_auth_ok(request):
        return _app_401()
    try:
        data = await request.json()
    except Exception:
        data = {}
    client_id = data.get("client_id")
    done = data.get("done", True)
    conn = await pg_conn()
    try:
        await _ensure_app_schema(conn)
        cached = await _idem_get(conn, client_id)
        if cached is not None:
            return cached
    finally:
        await conn.close()
    res = await native.complete_todo(todo_id, done=bool(done))
    result = {"ok": bool(res.get("ok")), **res}
    if result["ok"] and client_id:
        conn = await pg_conn()
        try:
            await _idem_put(conn, client_id, f"todo_complete:{todo_id}", result)
        finally:
            await conn.close()
    return result if result["ok"] else JSONResponse(result, status_code=404)


@app.patch("/api/app/v1/todos/{todo_id}")
async def app_todo_patch(todo_id: int, request: Request):
    """Edit a todo. Body any of {text, priority, due_date|null, client_id?}."""
    if not _app_auth_ok(request):
        return _app_401()
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "bad request"}, status_code=400)
    client_id = data.get("client_id")
    conn = await pg_conn()
    try:
        await _ensure_app_schema(conn)
        cached = await _idem_get(conn, client_id)
        if cached is not None:
            return cached
        sets, args = [], []
        if "text" in data and str(data["text"]).strip():
            args.append(str(data["text"]).strip()); sets.append(f"text=${len(args)}")
        if "priority" in data and data["priority"] in ("high", "med", "low"):
            args.append(data["priority"]); sets.append(f"priority=${len(args)}")
        if "due_date" in data:
            if data["due_date"] in (None, ""):
                sets.append("due_date=NULL")
            else:
                try:
                    args.append(date.fromisoformat(str(data["due_date"]))); sets.append(f"due_date=${len(args)}")
                except ValueError:
                    return JSONResponse({"ok": False, "error": "due_date must be YYYY-MM-DD"}, status_code=400)
        if not sets:
            return JSONResponse({"ok": False, "error": "nothing to update"}, status_code=400)
        args.append(todo_id)
        status = await conn.execute(f"UPDATE todos SET {', '.join(sets)} WHERE id=${len(args)} AND archived=FALSE", *args)
        ok = not _changed_zero(status)
        result = {"ok": ok, "id": todo_id}
        if ok and client_id:
            await _idem_put(conn, client_id, f"todo_patch:{todo_id}", result)
    finally:
        await conn.close()
    return result if result["ok"] else JSONResponse(result, status_code=404)


@app.get("/api/app/v1/foods/search")
async def app_foods_search(request: Request, q: str = ""):
    if not _app_auth_ok(request):
        return _app_401()
    q = (q or "").strip()
    if len(q) < 2:
        return {"ok": True, "query": q, "results": []}
    try:
        conn = await pg_conn()
        try:
            await _ensure_app_schema(conn)
            mine = await conn.fetch(
                "SELECT f.id, f.name, f.brand, f.serving_desc, f.calories::float8 c, f.protein::float8 p, "
                "f.source, f.source_id, COUNT(l.id) uses "
                "FROM foods f LEFT JOIN food_log l ON l.food_id=f.id "
                "WHERE f.is_mine AND f.name ILIKE $1 GROUP BY f.id ORDER BY uses DESC, f.name LIMIT 8",
                f"%{q}%")
        finally:
            await conn.close()
        results = [{"food_id": m["id"], "name": m["name"], "brand": m["brand"],
                    "serving_desc": m["serving_desc"], "calories": round(m["c"], 1),
                    "protein": round(m["p"], 1), "source": m["source"], "source_id": m["source_id"],
                    "mine": True} for m in mine]
        seen = {(m["source"], m["source_id"]) for m in mine if m["source_id"]}
        for ext in (await _search_off(q)) + (await _search_usda(q)):
            key = (ext["source"], ext["source_id"])
            if ext["source_id"] and key in seen:
                continue
            seen.add(key)
            results.append({**ext, "food_id": None, "mine": False})
        return {"ok": True, "query": q, "results": results[:16]}
    except Exception as e:
        logger.error(f"/api/app/v1/foods/search error: {e}")
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.get("/api/app/v1/foods/mine")
async def app_foods_mine(request: Request):
    if not _app_auth_ok(request):
        return _app_401()
    try:
        conn = await pg_conn()
        try:
            await _ensure_app_schema(conn)
            rows = await conn.fetch(
                "SELECT f.id, f.name, f.brand, f.serving_desc, f.calories::float8 c, f.protein::float8 p, "
                "COUNT(l.id) uses, MAX(l.logged_at) last_used "
                "FROM foods f LEFT JOIN food_log l ON l.food_id=f.id "
                "WHERE f.is_mine OR f.id IN (SELECT DISTINCT food_id FROM food_log WHERE food_id IS NOT NULL) "
                "GROUP BY f.id ORDER BY uses DESC, last_used DESC NULLS LAST, f.name LIMIT 50")
        finally:
            await conn.close()
        return {"ok": True, "foods": [{
            "food_id": r["id"], "name": r["name"], "brand": r["brand"], "serving_desc": r["serving_desc"],
            "calories": round(r["c"], 1), "protein": round(r["p"], 1), "uses": r["uses"]} for r in rows]}
    except Exception as e:
        logger.error(f"/api/app/v1/foods/mine error: {e}")
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.post("/api/app/v1/food-log")
async def app_food_log(request: Request):
    """Log a food. Body: {food_id} OR {name,calories,protein,source?,source_id?,serving_desc?,brand?,mine?},
    plus servings? (default 1), client_id?. A picked external result is cached into `foods` on first log."""
    if not _app_auth_ok(request):
        return _app_401()
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "bad request"}, status_code=400)
    client_id = data.get("client_id")
    try:
        servings = float(data.get("servings", 1) or 1)
    except (TypeError, ValueError):
        servings = 1.0
    conn = await pg_conn()
    try:
        await _ensure_app_schema(conn)
        cached = await _idem_get(conn, client_id)
        if cached is not None:
            return cached
        if data.get("food_id"):
            food = await conn.fetchrow(
                "SELECT id, name, calories::float8 c, protein::float8 p FROM foods WHERE id=$1", int(data["food_id"]))
            if not food:
                return JSONResponse({"ok": False, "error": "food not found"}, status_code=404)
            fid, fname, per_c, per_p = food["id"], food["name"], food["c"], food["p"]
        else:
            if not (data.get("name")):
                return JSONResponse({"ok": False, "error": "food_id or name required"}, status_code=400)
            fid = await _cache_food(conn, data)
            fname, per_c, per_p = data.get("name"), _f(data.get("calories")), _f(data.get("protein"))
        tot_c, tot_p = round(per_c * servings, 1), round(per_p * servings, 1)
        d = datetime.now(LOCAL_TZ).date()
        log_id = await conn.fetchval(
            "INSERT INTO food_log (user_id, food_id, food_name, servings, calories, protein, log_date, client_id) "
            "VALUES ($1,$2,$3,$4,$5,$6,$7,$8) RETURNING id",
            OWNER_ID, fid, fname, servings, tot_c, tot_p, d, client_id)
        nutrition = await _nutrition_today(conn)
        result = {"ok": True, "id": log_id, "food_name": fname, "servings": servings,
                  "calories": tot_c, "protein": tot_p, "nutrition": nutrition}
        await _idem_put(conn, client_id, "food_log", result)
    finally:
        await conn.close()
    return result


@app.delete("/api/app/v1/food-log/{log_id}")
async def app_food_log_delete(log_id: int, request: Request):
    if not _app_auth_ok(request):
        return _app_401()
    conn = await pg_conn()
    try:
        await _ensure_app_schema(conn)
        status = await conn.execute("DELETE FROM food_log WHERE id=$1 AND user_id=$2", log_id, OWNER_ID)
        if _changed_zero(status):
            return JSONResponse({"ok": False, "error": f"no food-log #{log_id}"}, status_code=404)
        nutrition = await _nutrition_today(conn)
    finally:
        await conn.close()
    return {"ok": True, "id": log_id, "nutrition": nutrition}


@app.get("/api/app/v1/foods/barcode/{code}")
async def app_food_barcode(code: str, request: Request):
    """v1.1 stub — Open Food Facts product-by-barcode lookup. Returns the product if OFF knows it,
    else not_found; the app doesn't ship barcode scanning until v1.1."""
    if not _app_auth_ok(request):
        return _app_401()
    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(_OFF_PRODUCT + f"{code}.json",
                                  params={"fields": "code,product_name,brands,nutriments"},
                                  headers={"User-Agent": "GhostApp/1.0 (hivequeen, personal)"}, timeout=12.0)
        j = r.json()
        if j.get("status") != 1 and not (j.get("product")):
            return {"ok": True, "found": False, "code": code}
        p = j.get("product") or {}
        nut = p.get("nutriments") or {}
        return {"ok": True, "found": True, "code": code, "product": {
            "name": (p.get("product_name") or "").strip(), "brand": (p.get("brands") or "").split(",")[0].strip() or None,
            "serving_desc": "per 100 g", "calories": round(_f(nut.get("energy-kcal_100g")), 1),
            "protein": round(_f(nut.get("proteins_100g")), 1), "source": "off", "source_id": code}}
    except Exception as e:
        logger.error(f"/api/app/v1/foods/barcode error: {e}")
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.get("/api/app/v1/nutrition/settings")
async def app_nutrition_settings_get(request: Request):
    if not _app_auth_ok(request):
        return _app_401()
    conn = await pg_conn()
    try:
        await _ensure_app_schema(conn)
        tc, tp = await _nutrition_targets(conn)
    finally:
        await conn.close()
    return {"ok": True, "calorie_target": tc, "protein_target": tp}


@app.patch("/api/app/v1/nutrition/settings")
async def app_nutrition_settings_set(request: Request):
    if not _app_auth_ok(request):
        return _app_401()
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "bad request"}, status_code=400)
    new_c = new_p = None
    conn = await pg_conn()
    try:
        await _ensure_app_schema(conn)
        cur_c, cur_p = await _nutrition_targets(conn)
        new_c = float(data["calorie_target"]) if data.get("calorie_target") is not None else cur_c
        new_p = float(data["protein_target"]) if data.get("protein_target") is not None else cur_p
        await conn.execute(
            "INSERT INTO nutrition_settings (user_id, calorie_target, protein_target) VALUES ($1,$2,$3) "
            "ON CONFLICT (user_id) DO UPDATE SET calorie_target=EXCLUDED.calorie_target, "
            "protein_target=EXCLUDED.protein_target", OWNER_ID, new_c, new_p)
    except (TypeError, ValueError):
        return JSONResponse({"ok": False, "error": "targets must be numbers"}, status_code=400)
    finally:
        await conn.close()
    return {"ok": True, "calorie_target": new_c, "protein_target": new_p}


@app.post("/api/app/v1/chat")
async def app_chat(request: Request):
    """Chat through the SAME brain + history as Telegram (source='app'). Body {message, client_id?}."""
    if not _app_auth_ok(request):
        return _app_401()
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "bad request"}, status_code=400)
    message = (data.get("message") or "").strip()
    if not message:
        return JSONResponse({"ok": False, "error": "message required"}, status_code=400)
    client_id = data.get("client_id")
    conn = await pg_conn()
    try:
        await _ensure_app_schema(conn)
        cached = await _idem_get(conn, client_id)
        if cached is not None:
            return cached
    finally:
        await conn.close()
    await bot.log_message("app_in", "chat", message)
    reply = await bot.process_message(OWNER_ID, message, source="app")
    await bot.log_message("app_out", "chat", reply)
    result = {"ok": True, "reply": reply}
    if client_id:
        conn = await pg_conn()
        try:
            await _idem_put(conn, client_id, "chat", result)
        finally:
            await conn.close()
    return result


@app.get("/api/app/v1/chat/history")
async def app_chat_history(request: Request, limit: int = 50):
    if not _app_auth_ok(request):
        return _app_401()
    limit = max(1, min(limit, 200))
    try:
        conn = await pg_conn()
        try:
            rows = await conn.fetch(
                "SELECT id, ts, source, type, content FROM "
                "(SELECT * FROM message_log ORDER BY id DESC LIMIT $1) sub ORDER BY id", limit)
        finally:
            await conn.close()
        msgs = []
        for r in rows:
            src = r["source"] or ""
            role = "user" if src.endswith("_in") else "ghost"
            msgs.append({"id": r["id"], "role": role, "source": src, "type": r["type"],
                         "text": r["content"], "ts": r["ts"].astimezone(LOCAL_TZ).isoformat()})
        return {"ok": True, "messages": msgs}
    except Exception as e:
        logger.error(f"/api/app/v1/chat/history error: {e}")
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.get("/", response_class=HTMLResponse)
async def index():
    for rel in DASHBOARD_HTML_CANDIDATES:
        p = os.path.join(bot.VAULT_PATH, rel)
        try:
            html = open(p, encoding="utf-8").read()
            return HTMLResponse(content=html, headers={"Cache-Control": "no-store"})
        except OSError:
            continue
    logger.error(f"dashboard html not found in any of {DASHBOARD_HTML_CANDIDATES}")
    return HTMLResponse(content="<h1>Ghost dashboard HTML not found</h1>"
                        f"<p>tried: {DASHBOARD_HTML_CANDIDATES}</p>", status_code=500)

# Session 78: the legacy /console interactive dashboard (the pre-S36 feed/chat/Cephalopod/apply
# page, served from the inline PAGE constant) was retired — nothing linked it (no nav pill, not in
# the smoke contract, no bot/scheduler reference), superseded by the v8 command center at "/". Route
# and PAGE both removed.


# Session 47: serve static assets (tarot card art) referenced by the ghost-*.html pages
# relative to themselves. URL /assets/... -> the vault's Projects/Tech/Ghost/assets folder,
# i.e. "next to" the served page, as ghost-tarot.html's own comment intends. Whitelisted
# extensions + no '..' traversal; long cache because the HTML versions the URL (?v=).
# S57: css/js added so the shared design-token layer (ghost-common.css) + any shared JS can be
# served here too, same-origin, next to the pages. Short cache for css/js so token edits land fast.
_SAFE_ASSET = re.compile(r"^[a-z0-9][a-z0-9/_.-]*\.(jpg|jpeg|png|webp|gif|svg|css|js)$", re.I)
_CTYPE = {"css": "text/css", "js": "application/javascript"}
ASSET_DIRS = ["Projects/Tech/Ghost/assets", "Projects/Ghost/assets"]

@app.get("/assets/{path:path}")
async def serve_asset(path: str):
    if ".." in path or not _SAFE_ASSET.match(path):
        return HTMLResponse(content="<h1>Not found</h1>", status_code=404)
    ext = path.rsplit(".", 1)[-1].lower()
    cache = "public, max-age=300" if ext in ("css", "js") else "public, max-age=604800"
    for d in ASSET_DIRS:
        full = os.path.join(bot.VAULT_PATH, d, path)
        if os.path.isfile(full):
            kw = {"media_type": _CTYPE[ext]} if ext in _CTYPE else {}
            return FileResponse(full, headers={"Cache-Control": cache}, **kw)
    return HTMLResponse(content=f"<h1>{path} not found</h1>", status_code=404)


# Session 83: PWA plumbing for ghost-habits.html (home-screen install). Both files live in the
# vault assets dir (edit-without-redeploy, same as the pages/css). Served from ROOT paths on
# purpose: a service worker's scope can't exceed its own URL path, so /sw.js is what lets it
# control /ghost-habits.html (Service-Worker-Allowed:/ is belt-and-braces). The manifest needs
# its own content-type (application/manifest+json) that the /assets whitelist doesn't grant.
# Declared before the catch-all /{page} route so they win by order.
@app.get("/manifest.webmanifest")
async def pwa_manifest():
    for d in ASSET_DIRS:
        full = os.path.join(bot.VAULT_PATH, d, "manifest.webmanifest")
        if os.path.isfile(full):
            return FileResponse(full, media_type="application/manifest+json",
                                headers={"Cache-Control": "public, max-age=300"})
    return HTMLResponse(content="<h1>manifest not found</h1>", status_code=404)

@app.get("/sw.js")
async def pwa_service_worker():
    for d in ASSET_DIRS:
        full = os.path.join(bot.VAULT_PATH, d, "sw.js")
        if os.path.isfile(full):
            # no-store so the browser always re-checks the SW for updates; root scope allowed.
            return FileResponse(full, media_type="application/javascript",
                                headers={"Cache-Control": "no-store",
                                         "Service-Worker-Allowed": "/"})
    return HTMLResponse(content="<h1>sw not found</h1>", status_code=404)


# Session 46: serve sibling ghost-*.html feature pages (the new Bills page, and the
# pre-existing horoscope/tarot pages which had NO route and 404'd on the live
# Tailscale dashboard) from the vault Ghost folder, same-origin with the API so their
# relative fetch("/api/...") calls need no CORS. Whitelisted basename only — the regex
# forbids '/' and any path separators, so no traversal. MUST stay the LAST GET route:
# "/" and every /api|/webhook route are declared earlier and win by order;
# multi-segment paths never match this single path param anyway.
_SAFE_GHOST_PAGE = re.compile(r"^ghost-[a-z0-9-]+\.html$")
GHOST_PAGE_DIRS = ["Projects/Tech/Ghost", "Projects/Ghost"]

@app.get("/{page}", response_class=HTMLResponse)
async def ghost_feature_page(page: str):
    if not _SAFE_GHOST_PAGE.match(page):
        return HTMLResponse(content="<h1>Not found</h1>", status_code=404)
    for d in GHOST_PAGE_DIRS:
        p = os.path.join(bot.VAULT_PATH, d, page)
        try:
            html = open(p, encoding="utf-8").read()
            return HTMLResponse(content=html, headers={"Cache-Control": "no-store"})
        except OSError:
            continue
    return HTMLResponse(content=f"<h1>{page} not found</h1>", status_code=404)


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8080)
