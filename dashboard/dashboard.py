"""Ghost dashboard + webhook service.

Serves the at-a-glance dashboard, a chatbox that reuses the full bot pipeline
(bot.py is mounted from the host at /botcode), and the Tasker scrolling webhook.
Runs from a stock python:3.12-slim image — no custom image build needed.
"""
import os
import sys
import logging
from datetime import datetime, timedelta
import zoneinfo

sys.path.insert(0, os.environ.get("BOT_CODE_PATH", "/botcode"))
import bot  # noqa: E402 — Ghost's own module, mounted read-only
import habitica  # noqa: E402

import asyncpg  # noqa: E402
import httpx  # noqa: E402
from redis.asyncio import Redis  # noqa: E402
from fastapi import FastAPI, Request  # noqa: E402
from fastapi.responses import HTMLResponse  # noqa: E402
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
    return await asyncpg.connect(host=bot.POSTGRES_HOST, port=5432, user="postgres",
                                 password=bot.POSTGRES_PASSWORD, database="ghost_db")

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

@app.get("/api/data")
async def api_data():
    now = datetime.now(LOCAL_TZ)
    out = {"time": now.strftime("%A %d %B — %I:%M %p").replace(" 0", " "), "errors": []}
    try:
        dailies = await habitica.get_dailies()
        todos = await habitica.get_todos()
        out["dailies"] = dailies
        out["todos"] = todos[:5]
    except Exception as e:
        out["errors"].append(f"habitica: {e}")
        out["dailies"] = {"done": [], "pending": []}
        out["todos"] = []
    try:
        conn = await pg_conn()
        rows = await conn.fetch("""
            SELECT bc.name, bc.monthly_limit, COALESCE(SUM(e.amount), 0) AS spent
            FROM budget_categories bc
            LEFT JOIN expenses e ON e.category = bc.name
                AND DATE_TRUNC('month', e.logged_at) = DATE_TRUNC('month', NOW())
            GROUP BY bc.name, bc.monthly_limit ORDER BY bc.name
        """)
        out["budget"] = [{"name": r["name"], "spent": float(r["spent"]),
                          "limit": float(r["monthly_limit"])} for r in rows]
        events = await conn.fetch(
            "SELECT event_type, value FROM daily_events WHERE date = $1 ORDER BY event_type", now.date())
        out["today_events"] = [{"type": r["event_type"],
                                "value": float(r["value"]) if r["value"] is not None else None} for r in events]
        await conn.close()
    except Exception as e:
        out["errors"].append(f"postgres: {e}")
        out["budget"] = []
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
            "service": r["service"], "status": r["status"], "detail": r["detail"],
            "checked_at": r["checked_at"].astimezone(LOCAL_TZ).strftime("%a %I:%M%p").replace(" 0", " ").lower(),
        } for r in rows]
    except Exception as e:
        out["errors"].append(f"health: {e}")
        out["health"] = []
    try:
        conn = await pg_conn()
        rows = await conn.fetch(
            "SELECT id, service, file_path, explanation, status FROM code_proposals "
            "WHERE status IN ('proposed','approved','apply_queued') ORDER BY id DESC LIMIT 10")
        await conn.close()
        out["proposals"] = [{
            "id": r["id"], "service": r["service"], "status": r["status"],
            "file": os.path.basename(r["file_path"]),
            "explanation": r["explanation"],
            # button only for bot/scheduler that are approved (approval stays manual, via Telegram)
            "can_apply": r["service"] in ("bot", "scheduler") and r["status"] == "approved",
        } for r in rows]
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
        async with httpx.AsyncClient() as client:
            r = await client.post(f"{CEPHALOPOD_URL}/chat", json={"message": message}, timeout=30)
        return r.json()
    except Exception as e:
        logger.error(f"cephalopod chat proxy error: {e}")
        return {"reply": "(cephalopod unreachable — its pod may be scaled down)"}

@app.get("/api/cephalopod-history")
async def api_cephalopod_history():
    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(f"{CEPHALOPOD_URL}/chat/history", timeout=15)
        return r.json()
    except Exception as e:
        return {"messages": [], "error": str(e)}

@app.get("/api/feed")
async def api_feed(after_id: int = 0):
    """Unified timeline from message_log. after_id=0 → last 50 rows; otherwise only
    rows newer than after_id (the page short-polls this every 7s — task 8. Polling
    over websockets: one user, tiny payloads, zero extra infra on this stack)."""
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
            "source": r["source"], "type": r["type"], "content": r["content"],
        } for r in rows]}
    except Exception as e:
        logger.error(f"feed error: {e}")
        return {"messages": [], "error": str(e)}

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
# 'Projects/Ghost/Ghost — App Categories.md', so edits apply retroactively.
# ---------------------------------------------------------------------------
@app.post("/webhook/app_usage")
async def app_usage_webhook(request: Request):
    """Tasker reports an app session: {"app": name, "duration_minutes": n}."""
    try:
        data = await request.json()
    except Exception:
        return {"ok": False, "error": "bad json"}
    app_name = str(data.get("app", "")).strip().lower()
    try:
        minutes = int(float(str(data.get("duration_minutes", 0)).strip() or 0))
    except ValueError:
        minutes = 0
    if not app_name or minutes <= 0:
        return {"ok": False, "error": "need app and positive duration_minutes"}
    now = datetime.now(LOCAL_TZ)
    try:
        r = redis_conn()
        await r.set("last_phone_activity", now.timestamp(), ex=3 * 86400)
        await r.aclose()
    except Exception as e:
        logger.error(f"app_usage activity signal error: {e}")
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
    """Tasker reports a screen unlock (empty body is fine). Feeds the scheduler's
    inactivity-aware escalation freeze with a much denser signal than scrolling."""
    now = datetime.now(LOCAL_TZ)
    try:
        r = redis_conn()
        await r.set("last_phone_activity", now.timestamp(), ex=3 * 86400)
        await r.aclose()
    except Exception as e:
        logger.error(f"unlock activity signal error: {e}")
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
    return {"ok": True}

@app.post("/webhook/wifi")
async def wifi_webhook(request: Request):
    """Tasker reports Wi-Fi state: {"ssid": name} on connect, {"ssid": ""} on
    disconnect. Stores the raw SSID only — the scheduler maps it to home/work/neither
    against the vault note each tick. Deliberately does NOT count as phone activity
    (the phone joins networks on its own; that's presence, not her using it)."""
    try:
        data = await request.json()
    except Exception:
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

PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Ghost</title><style>
body{background:#0e0f12;color:#e8e6e0;font-family:system-ui,sans-serif;margin:0;padding:24px}
h1{font-size:20px;letter-spacing:3px;color:#9aa0a6;margin:0 0 4px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:16px;margin-top:20px}
.card{background:#16181d;border:1px solid #23262d;border-radius:12px;padding:16px}
.card h2{font-size:12px;text-transform:uppercase;letter-spacing:1.5px;color:#8b93a1;margin:0 0 12px}
.daily{padding:4px 0;font-size:15px}
.done{color:#5f6672;text-decoration:line-through}
.bar{background:#23262d;border-radius:6px;height:10px;margin:4px 0 10px;overflow:hidden}
.bar i{display:block;height:100%}
.green{background:#3fb950}.amber{background:#d29922}.red{background:#f85149}
.stat{font-size:15px;padding:3px 0}
#feed{height:320px;overflow-y:auto;font-size:14px;margin-bottom:10px}
.msg-user{color:#7aa2f7;margin:6px 0;white-space:pre-wrap}
.msg-ghost{color:#e8e6e0;margin:6px 0;white-space:pre-wrap}
.srcbadge{display:inline-block;font-size:10px;text-transform:uppercase;letter-spacing:1px;color:#5f6672;margin-right:6px}
.msg-nudge{color:#d2a8ff;margin:6px 0;white-space:pre-wrap}
#chatin{width:100%;box-sizing:border-box;background:#0e0f12;border:1px solid #2b3039;color:#e8e6e0;border-radius:8px;padding:10px;font-size:15px}
.muted{color:#5f6672;font-size:12px}
</style></head><body>
<h1>GHOST</h1><div class="muted" id="clock"></div>
<div class="grid">
 <div class="card"><h2>Habitica</h2><div id="dailies"></div></div>
 <div class="card"><h2>Budget — this month</h2><div id="budget"></div></div>
 <div class="card"><h2>Today</h2><div id="activity"></div></div>
 <div class="card"><h2>Patterns</h2><div id="streaks"></div></div>
 <div class="card"><h2>Self-check</h2><div id="health"></div></div>
 <div class="card" style="grid-column:1/-1"><h2>Self-code proposals</h2><div id="proposals"></div></div>
 <div class="card" style="grid-column:1/-1"><h2>Messages — all sources</h2><div id="feed"></div>
  <input id="chatin" placeholder="message ghost — enter to send (money talk can take ~1 min)"></div>
 <div class="card" style="grid-column:1/-1;border-color:#3d2d52"><h2 style="color:#b48ead">Cephalopod — separate system</h2><div id="cephfeed" style="height:160px;overflow-y:auto;font-size:14px;margin-bottom:10px"></div>
  <input id="cephin" placeholder="message cephalopod — its own memory, not ghost's" style="width:100%;box-sizing:border-box;background:#0e0f12;border:1px solid #3d2d52;color:#e8e6e0;border-radius:8px;padding:10px;font-size:15px"></div>
</div>
<script>
function esc(s){const d=document.createElement('div');d.textContent=s;return d.innerHTML}
async function load(){
 try{
  const d=await (await fetch('/api/data')).json();
  document.getElementById('clock').textContent=d.time;
  const dl=document.getElementById('dailies');
  let h=d.dailies.pending.map(t=>`<div class="daily">&#9675; ${esc(t)}</div>`).join('')+
        d.dailies.done.map(t=>`<div class="daily done">&#10003; ${esc(t)}</div>`).join('');
  if(!h)h='<div class="muted">nothing due today</div>';
  if(d.todos.length)h+='<h2 style="margin-top:14px">To-dos</h2>'+d.todos.map(t=>`<div class="daily">&#8226; ${esc(t)}</div>`).join('');
  dl.innerHTML=h;
  document.getElementById('budget').innerHTML=d.budget.map(b=>{
   const r=b.limit?b.spent/b.limit:0,c=r>1?'red':(r>0.8?'amber':'green');
   return `<div class="stat">${esc(b.name)} — $${b.spent.toFixed(0)} / $${b.limit.toFixed(0)}</div><div class="bar"><i class="${c}" style="width:${Math.min(100,r*100)}%"></i></div>`;
  }).join('')||'<div class="muted">no data</div>';
  const a=d.activity;
  document.getElementById('activity').innerHTML=[
   `water confirms: ${a.water_confirms||0} / 3`,
   `water nudges sent: ${a.water_nudges||0}`,
   a.last_water_hours!=null?`last water log ~${a.last_water_hours}h ago`:'no water logged yet',
   a.exercise?'exercise logged ✓':(a.last_movement_hours!=null?`last movement log ~${a.last_movement_hours}h ago`:'no movement logged yet'),
   ...d.today_events.map(e=>`${e.type.replace(/_/g,' ')}${e.value?` (${e.value})`:''}`)
  ].map(s=>`<div class="stat">${esc(s)}</div>`).join('');
  document.getElementById('streaks').innerHTML=d.streaks.map(s=>`<div class="stat">${esc(s)}</div>`).join('')||'<div class="muted">no streaks yet</div>';
  document.getElementById('health').innerHTML=(d.health||[]).map(h=>{
   const dot=h.status==='ok'?'#3fb950':(h.status==='fail'?'#f85149':'#5f6672');
   return `<div class="stat"><span style="color:${dot}">&#9679;</span> ${esc(h.service)} — ${esc(h.status)}${h.detail?` <span class="muted">(${esc(h.detail)})</span>`:''} <span class="muted">${esc(h.checked_at)}</span></div>`;
  }).join('')||'<div class="muted">no checks recorded yet</div>';
  document.getElementById('proposals').innerHTML=(d.proposals||[]).map(p=>{
   const badge=p.status==='approved'?'#3fb950':(p.status==='apply_queued'?'#d29922':'#8b93a1');
   let row=`<div class="stat"><span style="color:${badge}">&#9679;</span> #${p.id} ${esc(p.service)}/${esc(p.file)} — ${esc(p.status)}<div class="muted">${esc(p.explanation||'')}</div>`;
   if(p.can_apply) row+=`<button onclick="applyProposal(${p.id},this)" style="margin-top:6px;background:#1f6feb;color:#fff;border:0;border-radius:6px;padding:6px 12px;cursor:pointer">Apply ${esc(p.service)} rebuild</button>`;
   else if(p.status==='apply_queued') row+=`<div class="muted" style="margin-top:4px">queued — the runner is applying this</div>`;
   else if(p.service!=='dashboard' && p.status==='proposed') row+=`<div class="muted" style="margin-top:4px">approve in Telegram first ("apply this change")</div>`;
   return row+'</div>';
  }).join('')||'<div class="muted">no open proposals</div>';
 }catch(e){}
}
async function applyProposal(id,btn){
 btn.disabled=true;btn.textContent='Queuing…';
 try{
  const r=await (await fetch('/api/apply',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id})})).json();
  btn.textContent=r.ok?'Queued — rebuilding':('Failed: '+(r.error||'?'));
  if(!r.ok)btn.disabled=false;
 }catch(e){btn.textContent='Failed';btn.disabled=false;}
 setTimeout(load,2000);
}
load();setInterval(load,300000);

// Unified feed (tasks 7-8): reads message_log, short-polls every 7s, appends only
// new rows. Persistent across refreshes because the source of truth is Postgres.
let lastFeedId=0;
const feedDiv=document.getElementById('feed');
function feedRow(m){
 const inbound=m.source.endsWith('_in');
 const cls=inbound?'msg-user':(m.type==='chat'?'msg-ghost':'msg-nudge');
 const who=inbound?'you':'ghost';
 const src=m.source.replace('_in','').replace('_out','');
 return `<div class="${cls}"><span class="srcbadge">${esc(m.ts)} · ${esc(src)}${m.type!=='chat'?' · '+esc(m.type):''}</span>${esc(who)}: ${esc(m.content)}</div>`;
}
async function pollFeed(){
 try{
  const d=await (await fetch('/api/feed?after_id='+lastFeedId)).json();
  if(!d.messages||!d.messages.length)return;
  const atBottom=feedDiv.scrollHeight-feedDiv.scrollTop-feedDiv.clientHeight<60;
  for(const m of d.messages){feedDiv.innerHTML+=feedRow(m);lastFeedId=Math.max(lastFeedId,m.id);}
  if(atBottom||lastFeedId<=50)feedDiv.scrollTop=feedDiv.scrollHeight;
 }catch(e){}
}
pollFeed();setInterval(pollFeed,7000);

// Cephalopod card: separate system, separate colours, its own history endpoint.
const cephFeed=document.getElementById('cephfeed'),cephIn=document.getElementById('cephin');
function cephRow(m){const who=m.role==='cephalopod'?'cephalopod':'you';const cls=m.role==='cephalopod'?'msg-nudge':'msg-user';return `<div class="${cls}">${esc(who)}: ${esc(m.content)}</div>`}
(async()=>{try{const d=await (await fetch('/api/cephalopod-history')).json();(d.messages||[]).forEach(m=>cephFeed.innerHTML+=cephRow(m));cephFeed.scrollTop=cephFeed.scrollHeight;}catch(e){}})();
cephIn.addEventListener('keydown',async ev=>{
 if(ev.key!=='Enter'||!cephIn.value.trim())return;
 const msg=cephIn.value.trim();cephIn.value='';
 cephFeed.innerHTML+=cephRow({role:'you',content:msg});cephFeed.scrollTop=cephFeed.scrollHeight;
 try{
  const d=await (await fetch('/api/cephalopod-chat',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({message:msg})})).json();
  cephFeed.innerHTML+=cephRow({role:'cephalopod',content:d.reply});
 }catch(e){cephFeed.innerHTML+='<div class="muted">cephalopod unreachable</div>'}
 cephFeed.scrollTop=cephFeed.scrollHeight;
});

const input=document.getElementById('chatin');
input.addEventListener('keydown',async ev=>{
 if(ev.key!=='Enter'||!input.value.trim())return;
 const msg=input.value.trim();input.value='';
 try{
  await fetch('/api/chat',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({message:msg})});
  pollFeed();
 }catch(e){feedDiv.innerHTML+='<div class="msg-ghost muted">ghost unreachable</div>'}
 feedDiv.scrollTop=feedDiv.scrollHeight;
});
</script></body></html>"""

@app.get("/", response_class=HTMLResponse)
async def index():
    return PAGE

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8080)
