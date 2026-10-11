import asyncio
import os
import json
import logging
import random
import re
import time
from datetime import datetime, timedelta
import zoneinfo
LOCAL_TZ = zoneinfo.ZoneInfo(os.environ.get("GHOST_TZ", "UTC"))
import httpx
import asyncpg
from redis.asyncio import Redis

import decisions  # decision loop: pending-decision reminders in the evening summary

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN")
TELEGRAM_USER_ID = os.environ.get("TELEGRAM_USER_ID", "")
REDIS_HOST = os.environ.get("REDIS_HOST", "ghost-redis-master.ghost.svc.cluster.local")
REDIS_PASSWORD = os.environ.get("REDIS_PASSWORD", "")
POSTGRES_HOST = os.environ.get("POSTGRES_HOST", "ghost-postgres-postgresql.ghost.svc.cluster.local")
POSTGRES_PASSWORD = os.environ.get("POSTGRES_PASSWORD", "")
USER_NAME = os.environ.get("GHOST_USER_NAME", "the user")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
# Ollama generate endpoint (money path). Was hardcoded here AND in bot.py (S27 diagnosed the
# 10.42.0.1 node-gateway IP as load-bearing); moved to env S56 so a CNI/node change is one config
# edit, not a silent break in two files. Same var name as bot.py = single source of truth (the env).
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://10.42.0.1:11434/api/generate")

RECENT_PHRASES_KEY = "recent_ghost_phrases"

SYSTEM_PROMPT = f"""You are Ghost. Dry, blunt, minimal. Dark humour when it fits.
You are sending a proactive check-in message. Keep it to one line unless told otherwise.
You are a person who noticed something, not a timer going off. Vary how you open — never the same angle twice in a row.
No filler words. Exclamation marks almost never.
NAME: do NOT open with her name, and do not use it by default. There is one person you talk to and she knows who she is — opening with a name every time reads as a mechanical tic, not warmth. Use her name ONLY when it does real work: re-getting her attention after a long silence, or a moment that genuinely calls for it (something serious, a real celebration). Rare, not routine.
NEVER INVENT: never state a specific event, place, trip, plan, purchase or fact that isn't in the data you were given. You know nothing about {USER_NAME}'s life beyond that data. If something's unclear, ask.
A missing log is not a fact: never claim {USER_NAME} didn't do something because nothing was logged — say "nothing logged" or ask.
Her routine (office days, meal times) is a typical pattern, not live knowledge — "usually", never "you are".
NEVER use "qualify as furniture" or "long enough to fossilize", or any variation of either."""

def seconds_to_midnight() -> int:
    """Expiry for 'today' keys — reset at local midnight, not on a rolling 24h window."""
    now = datetime.now(LOCAL_TZ)
    midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return int((midnight - now).total_seconds())

def is_quiet_hours() -> bool:
    now = datetime.now(LOCAL_TZ)
    return now.hour >= 22 or now.hour < 6 or (now.hour == 6 and now.minute < 30)

# Routine pattern (confirmed 19/07/2026): office Monday-Wednesday, WFH Thursday-Friday.
# A chat override ("I'm home today" / "I'm at the office today") sets Redis
# `location_override` until local midnight; refreshed into this global each
# main-loop tick so prompts and movement logic agree.
_location_override = None

def location_today() -> tuple:
    """(location, source). Source is 'manual' (chat override — always wins),
    'wifi' (phone on a named network right now), or False (pattern assumption).
    Wi-Fi 'neither' (unknown network) falls back to the pattern — being at a
    cafe says nothing about whether today is an office day."""
    if _location_override in ("office", "wfh"):
        return _location_override, "manual"
    if _wifi_location == "work":
        return "office", "wifi"
    if _wifi_location == "home":
        return "wfh", "wifi"
    wd = datetime.now(LOCAL_TZ).weekday()
    if wd <= 2:
        return "office", False
    if wd <= 4:
        return "wfh", False
    return "weekend", False

async def refresh_location_override():
    global _location_override
    try:
        r = await get_redis()
        _location_override = await r.get("location_override")
        await r.aclose()
    except Exception as e:
        logger.error(f"location override read error: {e}")

# ---------------------------------------------------------------------------
# App categories + named Wi-Fi networks (session 17, task 3). Parsed from the
# User-editable vault note — same manifest pattern as Cephalopod's vault scope:
# edit the note, no redeploy. Informational only; never touches Habitica.
# ---------------------------------------------------------------------------
VAULT_PATH = os.environ.get("VAULT_PATH", "/vault")
APP_CATEGORIES_REL = os.environ.get("GHOST_APP_CATEGORIES_REL", "Projects/Tech/Ghost/Ghost — App Categories.md")  # single-sourced via env S56 (was the same literal pasted here + in dashboard.py)
APP_CATEGORIES_CACHE_SECONDS = 300
_app_categories_cache = {"ts": 0.0, "data": None}

def load_app_categories() -> dict:
    """{'not_productive': set, 'streaming': set, 'excluded': set, 'ignored': set,
    'display': {package_lower: friendly}, 'wifi': {ssid_lower: 'home'|'work'}}.
    Unknown apps default to productive. Placeholder SSIDs in <angle brackets> are
    ignored. Missing note = empty config (everything productive, no wifi
    detection) — never an error state. The 'display' map doubles as the
    categorisation join key (see categorize_app) so the note's short-name lists
    resolve against full Android package names (Session 38 fix)."""
    now = time.monotonic()
    if _app_categories_cache["data"] is not None and \
            now - _app_categories_cache["ts"] < APP_CATEGORIES_CACHE_SECONDS:
        return _app_categories_cache["data"]
    data = {"not_productive": set(), "streaming": set(), "excluded": set(),
            "ignored": set(), "display": {}, "wifi": {}}
    section_map = {"not productive": "not_productive", "streaming / neutral": "streaming",
                   "excluded / unclassified": "excluded", "ignored": "ignored",
                   "display names": "display", "wi-fi networks": "wifi"}
    try:
        text = open(os.path.join(VAULT_PATH, APP_CATEGORIES_REL), encoding="utf-8").read()
        section = None
        for line in text.splitlines():
            if line.startswith("## "):
                title = line[3:].strip().lower()
                section = next((v for k, v in section_map.items() if title.startswith(k)), None)
                continue
            if section == "wifi":
                m = re.match(r"\s*-\s*(home|work)\s*:\s*`([^`]+)`", line, re.I)
                if m and not m.group(2).startswith("<"):
                    data["wifi"][m.group(2).strip().lower()] = m.group(1).lower()
            elif section == "display":
                # `package`: Friendly Name  -> the package->friendly map, reused as
                # the categorisation join key (Session 38).
                m = re.match(r"\s*-\s*`([^`]+)`\s*:\s*(.+?)\s*$", line)
                if m:
                    data["display"][m.group(1).strip().lower()] = m.group(2).strip()
            elif section:
                m = re.match(r"\s*-\s*`([^`]+)`", line)
                if m:
                    data[section].add(m.group(1).strip().lower())
    except OSError as e:
        logger.error(f"app categories note unreadable ({e}) — defaults apply")
    _app_categories_cache["ts"] = now
    _app_categories_cache["data"] = data
    return data

def _friendly_name(pkg: str, display: dict) -> str:
    """Package -> friendly name via the note's display map, else a cleaned last
    dotted segment (com.rotatelab.cubeblast -> 'Cubeblast'). Same resolution the
    dashboard uses for display, reused here so display and categorisation share
    one source and can't drift (Session 38)."""
    p = pkg.lower()
    if p in display:
        return display[p]
    seg = p.rsplit(".", 1)[-1] if "." in p else p
    return seg.replace("_", " ").title() or pkg

def categorize_app(pkg: str, cats: dict) -> str:
    """'ignore' | 'excluded' | 'streaming' | 'not_productive' | 'productive'.
    Matches on BOTH the raw package AND its friendly name (the display-map join
    key), so a list entry written either as a short name ('instagram') or a full
    package resolves against real app_usage data — fixes the Session-31 mismatch
    where full packages never matched the short-name lists. Unknown -> productive."""
    p = pkg.lower()
    name = _friendly_name(pkg, cats.get("display", {})).lower()
    keys = {p, name}
    if keys & cats.get("ignored", set()):
        return "ignore"
    if keys & cats["excluded"]:
        return "excluded"
    if keys & cats["streaming"]:
        return "streaming"
    if keys & cats["not_productive"]:
        return "not_productive"
    return "productive"

_wifi_location = None  # 'home' | 'work' | 'neither' | None (no wifi signal)

async def refresh_wifi_location():
    """Map the raw SSID the dashboard webhook stored to home/work/neither via the
    note. Unknown SSID -> 'neither' (out somewhere); no SSID -> None (no signal)."""
    global _wifi_location
    try:
        r = await get_redis()
        ssid = await r.get("wifi_ssid_current")
        await r.aclose()
    except Exception as e:
        logger.error(f"wifi location read error: {e}")
        return
    if not ssid:
        _wifi_location = None
        return
    _wifi_location = load_app_categories()["wifi"].get(ssid.strip().lower(), "neither")

def time_context() -> str:
    now = datetime.now(LOCAL_TZ)
    day = now.strftime("%A")
    clock = now.strftime("%I:%M%p").lower().lstrip("0")
    if now.hour < 9:
        part = "early morning"
    elif now.hour < 12:
        part = "mid-morning"
    elif now.hour < 14:
        part = "midday"
    elif now.hour < 17:
        part = "afternoon"
    elif now.hour < 20:
        part = "evening"
    else:
        part = "late evening"
    loc, source = location_today()
    if source == "manual":
        location = f"she said she's {'at the office' if loc == 'office' else 'working from home'} today (her word, today only)"
    elif source == "wifi":
        location = f"her phone is on the {'work' if loc == 'office' else 'home'} wifi (auto-detected, current)"
    elif loc == "office":
        location = "usually an office day (Mon-Wed pattern, not confirmed)"
    elif loc == "wfh":
        location = "usually a WFH day (Thu-Fri pattern, not confirmed)"
    else:
        location = "It's the weekend"
    return f"It is {part}, {clock} on {day} local time. {location}."

async def log_message(source: str, msg_type: str, content: str):
    """Unified message log (session 15) — same table the bot and dashboard write to."""
    try:
        conn = await asyncpg.connect(
            host=POSTGRES_HOST, port=5432, user="postgres",
            password=POSTGRES_PASSWORD, database="ghost_db")
        await conn.execute(
            "INSERT INTO message_log (source, type, content) VALUES ($1, $2, $3)",
            source, msg_type, content)
        await conn.close()
    except Exception as e:
        logger.error(f"message_log error: {e}")

NTFY_URL = os.environ.get("NTFY_URL", "")          # internal svc, e.g. http://ntfy.ghost.svc.cluster.local
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")
NTFY_TOKEN = os.environ.get("NTFY_TOKEN", "")
_NTFY_TITLES = {"briefing": "Ghost — morning", "summary": "Ghost — day's end",
                "nudge": "Ghost", "alert": "Ghost — heads up"}


def _internal_push_url(endpoint: str) -> str:
    """UnifiedPush endpoints point at ntfy's tailnet URL (tailscale serve :10443), which the pod
    can't necessarily resolve. Same ntfy server, so post to its in-cluster service with the
    endpoint's path instead. Endpoints on any other host are used as-is."""
    from urllib.parse import urlsplit
    u = urlsplit(endpoint)
    if NTFY_URL and (u.hostname or "").endswith(".ts.net") and u.port == 10443:
        return f"{NTFY_URL.rstrip('/')}{u.path}" + (f"?{u.query}" if u.query else "")
    return endpoint


async def push_app_endpoints(message: str, title: str = "Ghost") -> int:
    """S93: deliver one message to every UnifiedPush endpoint the Ghost app registered
    (app_push_endpoints, written by the dashboard's /api/app/v1/push/register). Body is JSON
    {title, message}; the app also accepts plain text. Endpoints ntfy reports gone (404/410) are
    dropped. Best-effort; returns how many deliveries succeeded."""
    if not NTFY_TOKEN:
        return 0
    try:
        conn = await asyncpg.connect(host=POSTGRES_HOST, port=5432, user="postgres",
                                     password=POSTGRES_PASSWORD, database="ghost_db")
    except Exception as e:
        logger.error(f"app push: db connect error: {e}")
        return 0
    ok = 0
    try:
        exists = await conn.fetchval("SELECT to_regclass('public.app_push_endpoints') IS NOT NULL")
        if not exists:
            return 0
        rows = await conn.fetch("SELECT endpoint FROM app_push_endpoints")
        body = json.dumps({"title": title, "message": message}).encode("utf-8")
        async with httpx.AsyncClient() as client:
            for r in rows:
                ep = r["endpoint"]
                try:
                    resp = await client.post(_internal_push_url(ep), content=body,
                                             headers={"Authorization": f"Bearer {NTFY_TOKEN}"}, timeout=10.0)
                    if resp.status_code in (404, 410):
                        await conn.execute("DELETE FROM app_push_endpoints WHERE endpoint=$1", ep)
                        logger.info("app push: endpoint gone, removed")
                    elif resp.status_code >= 300:
                        logger.error(f"app push FAILED ({resp.status_code}): {resp.text[:160]}")
                    else:
                        ok += 1
                        await conn.execute("UPDATE app_push_endpoints SET last_ok_at=now() WHERE endpoint=$1", ep)
                except Exception as e:
                    logger.error(f"app push error: {e}")
    finally:
        await conn.close()
    return ok


async def publish_ntfy(message: str, msg_type: str = "nudge"):
    """S92: push Ghost's proactive message to Lea's private ntfy topic (app push), IN ADDITION to
    Telegram for now. Best-effort — a push failure never blocks the Telegram send. No-op if unset.
    S93: also delivers to the Ghost app's UnifiedPush endpoints."""
    try:
        await push_app_endpoints(message, _NTFY_TITLES.get(msg_type, "Ghost"))
    except Exception as e:
        logger.error(f"app push error: {e}")
    if not (NTFY_URL and NTFY_TOPIC and NTFY_TOKEN):
        return
    try:
        async with httpx.AsyncClient() as client:
            r = await client.post(f"{NTFY_URL.rstrip('/')}/{NTFY_TOPIC}", content=message.encode("utf-8"),
                                  headers={"Authorization": f"Bearer {NTFY_TOKEN}",
                                           "Title": _NTFY_TITLES.get(msg_type, "Ghost")}, timeout=10.0)
        if r.status_code != 200:
            logger.error(f"ntfy publish FAILED ({r.status_code}): {r.text[:160]}")
    except Exception as e:
        logger.error(f"ntfy publish error: {e}")


async def send_telegram(message: str, msg_type: str = "nudge"):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    async with httpx.AsyncClient() as client:
        response = await client.post(url, json={
            "chat_id": TELEGRAM_USER_ID,
            "text": message
        })
        if response.status_code != 200:
            logger.error(f"Telegram send FAILED ({response.status_code}): {response.text[:200]}")
        else:
            await log_message("scheduler", msg_type, message)
    await publish_ntfy(message, msg_type)   # S92: also push to the app (best-effort)

async def get_redis():
    return Redis(host=REDIS_HOST, port=6379, password=REDIS_PASSWORD, decode_responses=True)

async def get_recent_phrases() -> list:
    try:
        r = await get_redis()
        phrases = await r.lrange(RECENT_PHRASES_KEY, 0, 14)
        await r.aclose()
        return phrases
    except Exception as e:
        logger.error(f"Recent phrases get error: {e}")
        return []

async def save_phrase(reply: str):
    try:
        r = await get_redis()
        await r.lpush(RECENT_PHRASES_KEY, reply)
        await r.ltrim(RECENT_PHRASES_KEY, 0, 14)
        await r.expire(RECENT_PHRASES_KEY, 172800)
        await r.aclose()
    except Exception as e:
        logger.error(f"Recent phrases save error: {e}")

async def call_claude(prompt: str, max_tokens: int = 80) -> str:
    system = SYSTEM_PROMPT
    recent_phrases = await get_recent_phrases()
    if recent_phrases:
        system += (
            "\n\nLINES YOU USED RECENTLY — do not reuse these jokes, phrasings, or joke structures:\n"
            + "\n".join(f"- {p}" for p in recent_phrases)
        )
    headers = {
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json"
    }
    payload = {
        "model": "claude-sonnet-4-6",
        "max_tokens": max_tokens,
        "system": system,
        "messages": [{"role": "user", "content": prompt}]
    }
    async with httpx.AsyncClient() as client:
        response = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers=headers,
            json=payload,
            timeout=30.0
        )
        data = response.json()
        reply = data["content"][0]["text"]
    await save_phrase(reply)
    return reply

async def call_ollama(prompt: str) -> str:
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                OLLAMA_URL,
                json={
                    # Session 27 (30/07/2026): CPU-only + 1b model — see bot.py
                    # call_ollama. The 940MX/NVK-Vulkan offload was ~14-16x slower
                    # than CPU-only on this host; num_gpu:0 forces CPU-only.
                    "model": "llama3.2:1b",
                    "prompt": prompt,
                    "stream": False,
                    "keep_alive": "1h",
                    "options": {
                        "temperature": 0.7,
                        "num_gpu": 0,
                        "num_predict": 80
                    }
                },
                timeout=180.0
            )
            data = response.json()
            return data.get("response", "").strip()
    except Exception as e:
        logger.error(f"Ollama error: {e}")
        return ""

# ---------------------------------------------------------------------------
# Daily anchors (Session 79 — messaging cadence v2). The old per-category nudge sprawl
# (independent water/breakfast/lunch/dinner/movement timers, tier-1 focus-todo + dailies
# nudges, board review) and its gentle->firmer->crude->dormant escalation ladder are
# RETIRED. Ghost's proactive job is now three structured, two-way touchpoints a day —
# morning debrief, lunch check-in, dinner catch-up+summary — plus at most ONE follow-up
# per anchor if it goes unanswered. Quiet hours (10pm-6:30am, is_quiet_hours) still
# suppress everything. Lea replies via Telegram at any point (handled in
# bot.process_message: done / swap / edit -> native items), so the anchors only SEND;
# reply-detection is `last_user_activity` (stamped by the bot on every inbound message)
# vs the anchor's send time. Voice: sharp, sassy assistant who's got her back — dry,
# blunt, dark humour where it fits, never exclamation marks, never motivational-poster,
# never claims an unconfirmed action.
# ---------------------------------------------------------------------------
# One follow-up if an anchor goes unanswered this long, then silence until the next
# anchor (whose opener acknowledges the quiet). Single named constant, easy to retune.
SILENCE_FOLLOWUP_SECONDS = 2 * 3600
# Morning debrief fires on the first phone-unlock of the day (Task 5 webhook signal);
# this is the fallback send time if no unlock signal has landed, so a Tasker/network
# hiccup never means total morning silence.
MORNING_FALLBACK_H, MORNING_FALLBACK_M = 7, 30
LUNCH_ANCHOR_H, LUNCH_ANCHOR_M = 12, 30       # lunchtime progress check-in
DINNER_ANCHOR_H, DINNER_ANCHOR_M = 18, 30     # dinner catch-up; summary follows once she replies

MEAL_WINDOWS = {"breakfast": (7, 9), "lunch": (12, 14), "dinner": (18, 20)}


async def _meal_logged(meal: str):
    """True if the meal is verifiably logged (FatSecret or a daily_events row)."""
    try:
        import fatsecret
        logged = await fatsecret.has_logged_meal(meal)
        if logged:
            return True
    except Exception as e:
        logger.error(f"FatSecret meal check error: {e}")
    try:
        conn = await asyncpg.connect(
            host=POSTGRES_HOST, port=5432, user="postgres",
            password=POSTGRES_PASSWORD, database="ghost_db")
        row = await conn.fetchrow(
            "SELECT 1 FROM daily_events WHERE date = $1 AND event_type = $2",
            datetime.now(LOCAL_TZ).date(), f"{meal}_done")
        await conn.close()
        return row is not None
    except Exception as e:
        logger.error(f"meal daily_events check error: {e}")
        return False




async def expire_commitments():
    """Session 19: commitments must not pile up. Unanswered confirmations die after
    an hour (she ignored the question — asking again would be nagging); tracked
    commitments go stale after their window. Pure bookkeeping, sends nothing."""
    try:
        conn = await asyncpg.connect(host=POSTGRES_HOST, port=5432, user="postgres",
                                     password=POSTGRES_PASSWORD, database="ghost_db")
        try:
            result = await conn.execute(
                "UPDATE commitments SET status = 'expired' "
                "WHERE status IN ('pending_confirmation', 'active') AND expires_at <= NOW()")
            n = int(result.split()[-1]) if result.split()[-1].isdigit() else 0
            if n:
                logger.info(f"Expired {n} stale commitment(s)")
        finally:
            await conn.close()
    except Exception as e:
        logger.error(f"Commitment expiry error: {e}")

async def get_active_commitments() -> list:
    """Active, unexpired commitments for the evening summary."""
    try:
        conn = await asyncpg.connect(host=POSTGRES_HOST, port=5432, user="postgres",
                                     password=POSTGRES_PASSWORD, database="ghost_db")
        try:
            rows = await conn.fetch(
                "SELECT text FROM commitments WHERE status = 'active' AND expires_at > NOW() "
                "ORDER BY confirmed_at")
            return [r["text"] for r in rows]
        finally:
            await conn.close()
    except Exception as e:
        logger.error(f"Active commitments read error: {e}")
        return []



# Session 41: don't fire proactive nudges while Lea is actively in conversation with
# Ghost. The bot/dashboard stamp `last_user_activity` on every inbound message; if she
# messaged within this window she is present and engaged, so a "you've gone quiet / glued
# to the chair" nudge (the 10:58 incident, where a fresh movement nudge fired 10 min into
# a live chat) is exactly wrong. Missing key = no recent message on record = normal
# behaviour (absence is not evidence she is away).







# Session 33 — full-board awareness with a NON-overwhelming cadence. The other
# collectors only ever cover water/meals/movement/focus-todos/exercise+evening dailies.
# This surfaces ONE rotating item from the rest of the board per day, so nothing is
# permanently invisible, without dumping the whole board into a nudge. Ask-don't-assert,
# no escalation ladder, and it flows through the same consolidation + min-gap as the rest.
# Dailies already owned by other collectors / daily anchors — never double-nudge them.



# ---------------------------------------------------------------------------
# Goal next-actions for the morning briefing (Session 38). Live vault read every
# call, NO cache — the briefing must always reflect the current frontmatter, not
# a stale copy. Mirrors the set Home.md's Areas.base shows: every note under
# Areas/ whose YAML frontmatter carries both `area` and `next_action`. An
# unreadable vault or a note missing next_action just yields fewer rows, never a
# fabricated one (anti-fabrication: a missing field is not evidence of anything).
# ---------------------------------------------------------------------------
AREAS_DIR_REL = "Areas"   # folder Goals/->Areas/ (rename S52, 31/08); fallback below keeps it working either way

def get_goal_next_actions() -> list:
    """Return [(area, next_action), ...] read live from the Areas-folder frontmatter,
    sorted by area. Read fresh every call — never cached. Folder-tolerant: falls back to the
    old Goals/ path if Areas/ isn't present yet, so a deploy either side of the rename works."""
    out = []
    root = os.path.join(VAULT_PATH, AREAS_DIR_REL)
    if not os.path.isdir(root):
        root = os.path.join(VAULT_PATH, "Goals")
    try:
        for dirpath, _dirs, files in os.walk(root):
            for fn in files:
                if not fn.endswith(".md"):
                    continue
                try:
                    text = open(os.path.join(dirpath, fn), encoding="utf-8").read()
                except OSError:
                    continue
                if not text.startswith("---"):
                    continue
                end = text.find("\n---", 3)
                if end == -1:
                    continue
                area = na = None
                for line in text[3:end].splitlines():
                    m = re.match(r"\s*area:\s*(.+?)\s*$", line)
                    if m:
                        area = m.group(1).strip().strip('"').strip("'")
                    m = re.match(r"\s*next_action:\s*(.+?)\s*$", line)
                    if m:
                        na = m.group(1).strip().strip('"').strip("'")
                if area and na:
                    out.append((area, na))
    except OSError:
        return []
    out.sort(key=lambda x: x[0].lower())
    return out

# ===========================================================================
# Daily anchors + silence handling (Session 79). Three two-way touchpoints; the
# scheduler SENDS, bot.process_message handles replies (done/swap/edit -> native).
# ===========================================================================
async def _anchor_open(r, name: str, label: str, now: float):
    """Record the just-sent anchor as the one awaiting a reply. 16h TTL so a quiet dinner
    can still be acknowledged by the next morning's debrief."""
    await r.set("anchor:current",
                json.dumps({"name": name, "label": label, "sent_at": now, "followed_up": 0}),
                ex=16 * 3600)

async def _anchor_replied(r, sent_at: float) -> bool:
    """True if Lea has sent Ghost anything since this anchor went out (bot stamps
    last_user_activity on every inbound message). Absence = no reply on record."""
    last = await r.get("last_user_activity")
    return last is not None and float(last) > float(sent_at)

async def _prev_anchor_silence(r) -> str:
    """If the most recent anchor was never replied to, return its short label so the NEXT
    anchor's opener can acknowledge the quiet (Task 3 memory). '' otherwise. Doesn't clear —
    the next _anchor_open overwrites anchor:current, consuming the memory."""
    raw = await r.get("anchor:current")
    if not raw:
        return ""
    cur = json.loads(raw)
    if await _anchor_replied(r, cur["sent_at"]):
        return ""
    return cur.get("label", "earlier")

async def _anchor_silence_followup():
    """Tick task (Task 3): if the current anchor has gone unanswered past
    SILENCE_FOLLOWUP_SECONDS and hasn't already had its one follow-up, send EXACTLY ONE nudge
    that plainly references the silence — then nothing more until the next anchor. On reply,
    clear the anchor (resolved)."""
    if is_quiet_hours():
        return
    r = await get_redis()
    try:
        raw = await r.get("anchor:current")
        if not raw:
            return
        cur = json.loads(raw)
        now = datetime.now(LOCAL_TZ).timestamp()
        if await _anchor_replied(r, cur["sent_at"]):
            await r.delete("anchor:current")            # she replied — resolved
            return
        if cur.get("followed_up") or (now - cur["sent_at"]) < SILENCE_FOLLOWUP_SECONDS:
            return
        prompt = (f"{time_context()} You sent a {cur['label']} check-in about two hours ago and got "
                  "nothing back. Send ONE short line that plainly acknowledges being left on read — "
                  "dry and a touch pointed, not a scold, no guilt-trip. A single nudge; you will not "
                  "chase again after this. Ask, never assume what she has or hasn't done. No "
                  "exclamation marks.")
        msg = await call_claude(prompt)
        await send_telegram(msg, msg_type="nudge")
        cur["followed_up"] = 1
        await r.set("anchor:current", json.dumps(cur), ex=16 * 3600)
        logger.info(f"Anchor silence follow-up sent for {cur['name']}")
    finally:
        await r.aclose()

async def _native_task_list(limit=8):
    """Today's task list from the NATIVE system (post-S78): open todos, quest-flagged first
    then by priority. Read-only direct SQL (the scheduler doesn't import native). Only real
    rows — never fabricated. [] on any error."""
    try:
        conn = await asyncpg.connect(host=POSTGRES_HOST, port=5432, user="postgres",
                                     password=POSTGRES_PASSWORD, database="ghost_db")
        try:
            rows = await conn.fetch(
                "SELECT text, is_quest FROM todos WHERE NOT done AND NOT archived "
                "ORDER BY is_quest DESC, "
                "CASE priority WHEN 'high' THEN 0 WHEN 'med' THEN 1 ELSE 2 END, id LIMIT $1", limit)
        finally:
            await conn.close()
        return [("[quest] " if r["is_quest"] else "") + r["text"] for r in rows]
    except Exception as e:
        logger.error(f"_native_task_list error: {e}")
        return []

# Google Calendar wiring (Session 82). Read-only from the scheduler. Morning debrief always
# carries a calendar line (an empty day says so plainly — never silently omitted); lunch/dinner
# reference an event ONLY when a timed one actually falls near that anchor's window, staying
# silent otherwise. All degrade to '' when the calendar isn't connected or the fetch fails —
# a calendar hiccup never blocks the anchor itself.
CAL_LUNCH_WINDOW = (11, 14)      # local hours; a timed event starting in here is "near lunch"
CAL_DINNER_WINDOW = (17, 21)

async def _calendar_morning_part() -> str:
    """Prompt fragment listing today's events for the morning debrief. '' when unconfigured.
    A connected-but-empty day yields an explicit 'nothing on the calendar' instruction."""
    try:
        import gcal
        if not gcal.is_configured():
            return ""
        events = await gcal.get_today_events()
        if events is None:
            return ""                         # fetch failed — don't fabricate a calendar line
        if not events:
            return ("Calendar today: nothing scheduled. State that plainly (e.g. 'calendar's clear "
                    "today') — do not invent any events. ")
        listing = "; ".join(gcal.fmt_event(e) for e in events[:10])
        return (f"Today's calendar: {listing}. Fold these into the debrief as real fixed points; "
                "use only these, invent nothing. ")
    except Exception as e:
        logger.error(f"_calendar_morning_part error: {e}")
        return ""

async def _calendar_meal_part(window: tuple, meal: str) -> str:
    """Prompt fragment for the lunch/dinner anchors: mention a timed event ONLY if one starts
    within `window` today. '' otherwise (and when unconfigured/failed) — no forced calendar line."""
    try:
        import gcal
        if not gcal.is_configured():
            return ""
        now = datetime.now(LOCAL_TZ)
        today = now.date()
        start = datetime.combine(today, datetime.min.time(), tzinfo=LOCAL_TZ).replace(hour=window[0])
        end = datetime.combine(today, datetime.min.time(), tzinfo=LOCAL_TZ).replace(hour=window[1])
        events = await gcal.get_events_in_window(start, end)
        if not events:
            return ""
        timed = [e for e in events if not e["all_day"] and isinstance(e["start"], datetime)
                 and window[0] <= e["start"].hour < window[1]]
        if not timed:
            return ""
        listing = "; ".join(gcal.fmt_event(e) for e in timed[:4])
        return (f"There's something on her calendar around {meal}: {listing}. Reference it naturally "
                "(one line), don't turn the whole message into a calendar readout. ")
    except Exception as e:
        logger.error(f"_calendar_meal_part error: {e}")
        return ""

async def morning_debrief():
    """Anchor 1. Fires on the first phone-unlock of the day (Task 5 webhook sets
    morning_unlock_signal), else at the MORNING_FALLBACK time so a network hiccup never means
    total silence. Morning habits status + today's task list; she can reshape it by replying."""
    if is_quiet_hours():
        return
    now = datetime.now(LOCAL_TZ)
    r = await get_redis()
    try:
        if await r.get("morning_debrief_sent"):
            return
        unlocked = await r.get("morning_unlock_signal")
        now_hm = now.hour * 60 + now.minute
        past_fallback = now_hm >= (MORNING_FALLBACK_H * 60 + MORNING_FALLBACK_M)
        if not (unlocked or past_fallback):
            return
        silence = await _prev_anchor_silence(r)
    finally:
        await r.aclose()
    dailies_part = ""
    try:
        from habitica import get_dailies
        d = await get_dailies()
        dailies_part = (f"Habits/dailies still pending: {', '.join(d['pending']) if d['pending'] else 'none'}. "
                        f"Already done: {', '.join(d['done']) if d['done'] else 'none'}. ")
    except Exception as e:
        logger.error(f"morning_debrief habitica fetch: {e}")
    tasks = await _native_task_list()
    tasks_part = ("Today's task list: " + "; ".join(tasks) + ". ") if tasks else "The task list is empty right now. "
    goals = get_goal_next_actions()
    goal_part = (" Across her goals the next actions are: "
                 + "; ".join(f"{a} — {na}" for a, na in goals) + ".") if goals else ""
    cal_part = await _calendar_morning_part()
    sil = (f"Open by acknowledging she went quiet {silence} — one dry aside, then move on. "
           if silence else "")
    prompt = (f"{time_context()} {sil}Generate a MORNING DEBRIEF, the first touchpoint of the day. "
              f"{cal_part}{dailies_part}{tasks_part}{goal_part} "
              "Give her the state of play — habits status and today's list — then invite her to reshape "
              "it (add / drop / reorder / reprioritise, or just say what changed); she can reply. "
              "Voice: a sharp, sassy assistant who has her back — dry, collaborative, a little wry, never "
              "a drill sergeant, never a cheerleader, no exclamation marks. Under 5 lines. Use only these "
              "facts, invent nothing; phrase anything unlogged as 'nothing logged', not as a failure.")
    message = await call_claude(prompt, max_tokens=240)
    await send_telegram(message, msg_type="briefing")
    r = await get_redis()
    try:
        await r.set("morning_debrief_sent", "1", ex=seconds_to_midnight())
        await _anchor_open(r, "morning", "this morning", datetime.now(LOCAL_TZ).timestamp())
    finally:
        await r.aclose()
    logger.info("Morning debrief sent " + ("(first unlock)" if unlocked else "(fallback time)"))

async def lunch_checkin():
    """Anchor 2 (~LUNCH_ANCHOR). Assess real progress since the morning debrief. No progress ->
    a blunt get-back-on-track nudge (a push, not a scold). Progress -> a dry nod + what's next."""
    if is_quiet_hours():
        return
    now = datetime.now(LOCAL_TZ)
    now_hm = now.hour * 60 + now.minute
    start = LUNCH_ANCHOR_H * 60 + LUNCH_ANCHOR_M
    if not (start <= now_hm < start + 30):
        return
    r = await get_redis()
    try:
        if await r.get("lunch_checkin_sent"):
            return
        silence = await _prev_anchor_silence(r)
    finally:
        await r.aclose()
    completions_today = 0
    try:
        conn = await asyncpg.connect(host=POSTGRES_HOST, port=5432, user="postgres",
                                     password=POSTGRES_PASSWORD, database="ghost_db")
        try:
            completions_today = await conn.fetchval(
                "SELECT COUNT(*) FROM momentum_events WHERE (completed_at AT TIME ZONE $1)::date = $2",
                str(LOCAL_TZ), now.date()) or 0
        finally:
            await conn.close()
    except Exception as e:
        logger.error(f"lunch_checkin momentum: {e}")
    dailies_done = 0
    try:
        from habitica import get_dailies
        d = await get_dailies()
        dailies_done = len(d["done"])
    except Exception as e:
        logger.error(f"lunch_checkin habitica: {e}")
    tasks = await _native_task_list(limit=5)
    cal_part = await _calendar_meal_part(CAL_LUNCH_WINDOW, "lunchtime")
    made_progress = (completions_today > 0) or (dailies_done > 0)
    sil = (f"Open by noting she went quiet {silence} — one dry line, then carry on. " if silence else "")
    if made_progress:
        body = (f"She's moving — {completions_today} things logged and {dailies_done} dailies done since "
                "this morning. Give that a quick dry nod, then surface what's next, one line. "
                + ("Next up: " + "; ".join(tasks) + "." if tasks else ""))
    else:
        body = ("Nothing's been logged since this morning. Give a direct, blunt nudge to take some time "
                "and get back on track — a push in your usual voice, not a scold, phrased as 'nothing "
                "logged yet' not 'you failed'. "
                + ("Still on the list: " + "; ".join(tasks) + "." if tasks else ""))
    prompt = (f"{time_context()} {sil}Generate a LUNCHTIME CHECK-IN. {body} {cal_part}"
              "Voice: sharp, sassy assistant on her side — dry, collaborative, no exclamation marks, no "
              "motivational-poster energy, never claim she did or didn't do something you can't see. "
              "Under 4 lines. She can reply to redirect or mark things done.")
    message = await call_claude(prompt, max_tokens=200)
    await send_telegram(message, msg_type="nudge")
    r = await get_redis()
    try:
        await r.set("lunch_checkin_sent", "1", ex=seconds_to_midnight())
        await _anchor_open(r, "lunch", "at lunchtime", datetime.now(LOCAL_TZ).timestamp())
    finally:
        await r.aclose()
    logger.info(f"Lunch check-in sent (progress={made_progress})")

async def _build_daily_summary(now):
    """Grounded end-of-day summary from REAL logs only (never fabricates): dailies done/open,
    to-dos knocked off, app-usage split, commitments (as open questions), water/exercise, plus
    tomorrow's priority (absorbs the old board-review job). Anti-fabrication: gaps read as
    'nothing logged', commitments are asked about, never asserted."""
    r = await get_redis()
    try:
        water_count = await r.get("water_nudge_count") or 0
        last_water = await r.get("last_water_confirmed")
        exercise_done = await r.get("exercise_confirmed")
    finally:
        await r.aclose()
    habitica_part = ""
    try:
        from habitica import get_dailies, get_completed_todos_today
        dailies = await get_dailies()
        done_todos = await get_completed_todos_today(LOCAL_TZ)
        habitica_part = (
            f"Dailies done: {', '.join(dailies['done']) if dailies['done'] else 'none logged'}. "
            f"Dailies still open: {', '.join(dailies['pending']) if dailies['pending'] else 'none'}. "
            f"To-dos knocked off today: {', '.join(done_todos) if done_todos else 'none'}. ")
    except Exception as e:
        logger.error(f"daily summary habitica fetch: {e}")
    usage_part = ""
    try:
        cats = load_app_categories()
        conn = await asyncpg.connect(host=POSTGRES_HOST, port=5432, user="postgres",
                                     password=POSTGRES_PASSWORD, database="ghost_db")
        try:
            rows = await conn.fetch("SELECT app, minutes FROM app_usage WHERE date = $1", now.date())
        finally:
            await conn.close()
        if rows:
            prod = notprod = stream = 0
            for row in rows:
                cat = categorize_app(row["app"], cats)
                if cat in ("ignore", "excluded"):
                    continue
                elif cat == "streaming":
                    stream += row["minutes"]
                elif cat == "not_productive":
                    notprod += row["minutes"]
                else:
                    prod += row["minutes"]
            usage_part = (f"Phone use today: {prod}min productive, {notprod}min not-productive"
                          + (f", {stream}min streaming (neutral)" if stream else "")
                          + ". Mention it plainly, no verdict from it. ")
    except Exception as e:
        logger.error(f"daily summary app-usage fetch: {e}")
    commitments_part = ""
    try:
        open_commitments = await get_active_commitments()
        if open_commitments:
            commitments_part = ("Commitments she asked you to track: " + "; ".join(open_commitments)
                                + ". Ask about them ONCE as an open question — you do not know whether "
                                "they happened, never assert she didn't. ")
    except Exception as e:
        logger.error(f"daily summary commitments fetch: {e}")
    tasks = await _native_task_list(limit=5)
    goals = get_goal_next_actions()
    tomorrow_part = ""
    if tasks or goals:
        tomorrow_part = ("For tomorrow's priority, the open list is: "
                         + ("; ".join(tasks) if tasks else "empty")
                         + (". Goal next-actions: " + "; ".join(f"{a} — {na}" for a, na in goals) if goals else "")
                         + ". Name ONE clear priority for tomorrow from these, not a pile. ")
    prompt = (f"{time_context()} Generate the DAY'S SUMMARY (she has just confirmed/corrected the record). "
              f"{commitments_part}{habitica_part}{usage_part}"
              f"Water logged at some point today: {'yes' if last_water else 'no'}. "
              f"Exercise logged today: {'yes' if exercise_done else 'no'}. "
              f"{tomorrow_part}"
              "Cover: what got done, progress against goals, what needs improvement, ONE priority for "
              "tomorrow. Unlogged is not undone — phrase gaps as 'nothing logged', never as failures; a "
              "completed to-do earns a dry nod. Voice: sharp, sassy assistant on her side, dry, no "
              "exclamation marks, never claim an unconfirmed action. Up to 6 lines.")
    return await call_claude(prompt, max_tokens=280)

async def _pending_decisions_footer() -> str:
    """Unanswered overnight-runner decisions, appended verbatim (deterministic, not model-written)
    to the evening summary until answered. Bumps reminder_count; never answers or expires anything."""
    try:
        conn = await asyncpg.connect(host=POSTGRES_HOST, port=5432, user="postgres",
                                     password=POSTGRES_PASSWORD, database="ghost_db")
        try:
            block = await decisions.reminder_block(conn)
        finally:
            await conn.close()
        return ("\n\n" + block) if block else ""
    except Exception as e:
        logger.error(f"decisions reminder error: {e}")
        return ""

async def dinner_catchup():
    """Anchor 3, two steps. Step 1 (~DINNER_ANCHOR): ask about anything unlogged (food/exercise/
    habits) and invite corrections — get her in the loop BEFORE summarising. Step 2 (a later tick,
    once she has replied): send the day's summary from real logs only. If she never replies, the
    silence follow-up fires and NO summary is sent (anti-fabrication); tomorrow's debrief
    acknowledges the quiet. Absorbs the old board-review's what's-next job into the summary."""
    if is_quiet_hours():
        return
    now = datetime.now(LOCAL_TZ)
    now_hm = now.hour * 60 + now.minute
    r = await get_redis()
    send_summary = False
    silence = ""
    try:
        step = await r.get("dinner_step")
        if step == "summarised":
            return
        if step == "asked":
            raw = await r.get("anchor:current")
            cur = json.loads(raw) if raw else None
            if cur and cur.get("name") == "dinner" and await _anchor_replied(r, cur["sent_at"]):
                await r.set("dinner_step", "summarised", ex=seconds_to_midnight())
                await r.delete("anchor:current")     # resolved by her reply
                send_summary = True
            else:
                return                                # still waiting (silence handler owns the follow-up)
        else:
            start = DINNER_ANCHOR_H * 60 + DINNER_ANCHOR_M
            if not (start <= now_hm < start + 30):
                return
            silence = await _prev_anchor_silence(r)
    finally:
        await r.aclose()

    if send_summary:
        message = await _build_daily_summary(now)
        message += await _pending_decisions_footer()
        await send_telegram(message, msg_type="summary")
        logger.info("Dinner summary (step 2) sent")
        return

    # STEP 1 — the catch-up question. No summary yet; only tallies she confirms.
    gaps = []
    try:
        for meal in ("breakfast", "lunch", "dinner"):
            if not await _meal_logged(meal):
                gaps.append(meal)
    except Exception:
        pass
    r2 = await get_redis()
    try:
        ex_done = await r2.get("exercise_confirmed")
    finally:
        await r2.aclose()
    gap_part = (f"Nothing logged yet for: {', '.join(gaps)}. " if gaps else "Meals look logged. ")
    gap_part += ("No exercise logged today. " if not ex_done else "Exercise is logged. ")
    cal_part = await _calendar_meal_part(CAL_DINNER_WINDOW, "dinnertime")
    sil = (f"Open by noting she went quiet {silence} — one line, then move on. " if silence else "")
    prompt = (f"{time_context()} {sil}Generate a DINNER-TIME CATCH-UP — the FIRST of two messages, not "
              f"the summary yet. {gap_part}{cal_part}"
              "Ask her to confirm or correct today's record: anything unlogged she actually did (food, "
              "exercise, habits) or anything to tweak, before you tally the day. Make clear you'll only "
              "count what she confirms — do not assume or assert completion. Voice: sharp, sassy "
              "assistant on her side, dry, no exclamation marks. Two to three lines. She replies to set "
              "the record straight and the summary follows.")
    message = await call_claude(prompt, max_tokens=200)
    await send_telegram(message, msg_type="summary")
    rr = await get_redis()
    try:
        await rr.set("dinner_step", "asked", ex=seconds_to_midnight())
        await _anchor_open(rr, "dinner", "at dinner", datetime.now(LOCAL_TZ).timestamp())
    finally:
        await rr.aclose()
    logger.info("Dinner catch-up (step 1: ask) sent")

async def check_missed_meals():
    """After each meal window closes, log meal_skipped for today unless the bot
    logged meal_done. Conservative: only fires once the window has fully passed.
    A late confirmation makes the bot delete the skip row."""
    now = datetime.now(LOCAL_TZ)
    today = now.date()
    windows = {"breakfast": 9, "lunch": 14, "dinner": 20}
    due = [meal for meal, end_hour in windows.items() if now.hour >= end_hour]
    if not due:
        return
    try:
        conn = await asyncpg.connect(
            host=POSTGRES_HOST, port=5432, user="postgres",
            password=POSTGRES_PASSWORD, database="ghost_db"
        )
        for meal in due:
            done = await conn.fetchrow(
                "SELECT 1 FROM daily_events WHERE date = $1 AND event_type = $2",
                today, f"{meal}_done"
            )
            if not done:
                inserted = await conn.execute("""
                    INSERT INTO daily_events (date, event_type)
                    VALUES ($1, $2)
                    ON CONFLICT (date, event_type) DO NOTHING
                """, today, f"{meal}_skipped")
                if inserted == "INSERT 0 1":
                    logger.info(f"Daily event logged: {meal}_skipped")
        await conn.close()
    except Exception as e:
        logger.error(f"check_missed_meals error: {e}")

FOOD_TRACK_INTERVAL = 1800
_last_food_track_check = 0.0

async def check_food_tracked():
    """Once FatSecret has a real entry for today, check off the 'Track food'
    checklist item nested under the 'Bedtime' daily (NOT a same-named top-level
    daily, which does not exist). Checks at most every 30 min, once per day."""
    global _last_food_track_check
    import time
    if is_quiet_hours() or time.time() - _last_food_track_check < FOOD_TRACK_INTERVAL:
        return
    _last_food_track_check = time.time()
    try:
        import fatsecret
        if not fatsecret.is_configured():
            return
        r = await get_redis()
        already = await r.get("food_daily_completed")
        await r.aclose()
        if already:
            return
        summary = await fatsecret.get_today_summary()
        if not summary or not summary["entries"]:
            return
        from habitica import complete_checklist_item
        ok = await complete_checklist_item("Bedtime", "Track food")
        r = await get_redis()
        await r.set("food_daily_completed", "1", ex=seconds_to_midnight())
        await r.aclose()
        if ok:
            logger.info("'Track food' checklist item (under 'Bedtime') auto-checked — FatSecret has entries today")
        else:
            logger.info("FatSecret has entries but 'Track food' item not checkable (already done / not found) — nothing scored")
    except Exception as e:
        logger.error(f"check_food_tracked error: {e}")

# ---------------------------------------------------------------------------
# Self-health check (every 2h outside quiet hours).
# Checks use whatever credentials THIS pod actually has loaded — a live call,
# not a config comparison — so stale env (the 5-day silent outage) gets caught.
# Gate and postgres-down dedup are in-process on purpose: a Redis outage must
# not be able to disable the health check itself.
# ---------------------------------------------------------------------------
HEALTH_INTERVAL_SECONDS = 7200
_last_health_run = 0.0
_pg_down_alerted = False

SERVICE_HINTS = {
    "telegram": "the bot token may have been revoked or rotated",
    "habitica": "the API token may be stale",
    "postgres": "database unreachable — check the pod or the password secret",
    "redis": "redis unreachable — check the pod or the password secret",
    "fatsecret": "API auth failing — tokens may have been revoked",
    "anthropic": "the Claude API is refusing calls — Ghost's brain is down",
}

async def check_telegram_health():
    async with httpx.AsyncClient() as client:
        r = await client.get(f"https://api.telegram.org/bot{BOT_TOKEN}/getMe", timeout=15)
        return None if r.status_code == 200 else f"getMe HTTP {r.status_code}"

async def check_habitica_health():
    user_id = os.environ.get("HABITICA_USER_ID", "")
    headers = {
        "x-api-user": user_id,
        "x-api-key": os.environ.get("HABITICA_API_TOKEN", ""),
        "x-client": f"{user_id}-ghost-hivequeen",
    }
    async with httpx.AsyncClient() as client:
        r = await client.get("https://habitica.com/api/v3/user", headers=headers, timeout=20)
        return None if r.status_code == 200 else f"/user HTTP {r.status_code}"

async def check_postgres_health():
    conn = await asyncpg.connect(
        host=POSTGRES_HOST, port=5432, user="postgres",
        password=POSTGRES_PASSWORD, database="ghost_db", timeout=15
    )
    await conn.fetchval("SELECT 1")
    await conn.close()
    return None

async def check_redis_health():
    r = await get_redis()
    try:
        await r.ping()
    finally:
        await r.aclose()
    return None

async def check_fatsecret_health():
    import fatsecret
    if not fatsecret.is_configured():
        return "unconfigured"
    summary = await fatsecret.get_today_summary()
    return None if summary is not None else "fetch failed (see pod log for detail)"

async def check_anthropic_health():
    """Anthropic has no balance endpoint for a standard API key (the usage/cost
    reports need an Admin key), so we probe with the cheapest possible call and
    classify the failure. A 'BILLING:' detail means out of credit — a full outage,
    alerted on the FIRST occurrence rather than the usual 2-in-a-row rule."""
    async with httpx.AsyncClient() as client:
        r = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": "claude-haiku-4-5-20251001", "max_tokens": 1,
                  "messages": [{"role": "user", "content": "hi"}]},
            timeout=30)
    if r.status_code == 200:
        return None
    try:
        err = r.json().get("error", {})
        etype, emsg = err.get("type", ""), err.get("message", "")
    except Exception:
        etype, emsg = "", r.text[:120]
    low = emsg.lower()
    if "credit balance" in low or "billing" in low or "quota" in low or "insufficient" in low:
        return f"BILLING: out of credit — {emsg[:120]}"
    if r.status_code == 429 or etype == "rate_limit_error":
        return f"rate limited (transient) — {emsg[:80]}"
    if r.status_code == 401:
        return f"auth failed — the API key may be revoked: {emsg[:80]}"
    if r.status_code == 529:
        return f"Anthropic overloaded (transient) — {emsg[:80]}"
    return f"HTTP {r.status_code} {etype}: {emsg[:100]}"

HEALTH_CHECKS = [
    ("telegram", check_telegram_health),
    ("habitica", check_habitica_health),
    ("postgres", check_postgres_health),
    ("redis", check_redis_health),
    ("fatsecret", check_fatsecret_health),
    ("anthropic", check_anthropic_health),
]

async def run_health_checks() -> dict:
    """Run every probe. Returns {service: (status, detail)} where status is
    ok / fail / unconfigured."""
    results = {}
    for service, probe in HEALTH_CHECKS:
        try:
            detail = await probe()
        except Exception as e:
            detail = f"{type(e).__name__}: {str(e)[:150]}"
        if detail == "unconfigured":
            results[service] = ("unconfigured", None)
        elif detail is None:
            results[service] = ("ok", None)
        else:
            results[service] = ("fail", detail)
    return results

async def send_alert(text: str, reply_markup: dict | None = None) -> bool:
    """Telegram send that reports success instead of assuming it. `reply_markup` (an inline
    keyboard dict) is used by the ghost-runner's decision cards; the bot handles the taps."""
    try:
        payload = {"chat_id": TELEGRAM_USER_ID, "text": text}
        if reply_markup:
            payload["reply_markup"] = reply_markup
        async with httpx.AsyncClient() as client:
            r = await client.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                json=payload, timeout=15
            )
            if r.status_code == 200:
                await log_message("scheduler", "alert", text)
            return r.status_code == 200
    except Exception as e:
        logger.error(f"send_alert error: {e}")
        return False

def _fmt(ts) -> str:
    local = ts.astimezone(LOCAL_TZ)
    return local.strftime("%a %I:%M%p").replace(" 0", " ").lower()

async def health_check():
    global _last_health_run, _pg_down_alerted
    import time
    if time.time() - _last_health_run < HEALTH_INTERVAL_SECONDS or is_quiet_hours():
        return
    _last_health_run = time.time()

    results = await run_health_checks()
    telegram_ok = results["telegram"][0] == "ok"
    bad = {s: d for s, (st, d) in results.items() if st == "fail"}
    logger.info(f"Health check: {'all ok' if not bad else 'FAILING: ' + ', '.join(bad)}"
                + (" (fatsecret unconfigured)" if results["fatsecret"][0] == "unconfigured" else ""))

    try:
        conn = await asyncpg.connect(
            host=POSTGRES_HOST, port=5432, user="postgres",
            password=POSTGRES_PASSWORD, database="ghost_db", timeout=15
        )
    except Exception as e:
        # Can't record state. Alert directly (deduped in-process) if Telegram works.
        logger.error(f"Health check: postgres unreachable, state not recorded: {e}")
        if telegram_ok and not _pg_down_alerted:
            if await send_alert("Postgres is unreachable — I can't record health state. "
                                "Check the pod or the password secret."):
                _pg_down_alerted = True
        return
    _pg_down_alerted = False

    alerts = []
    try:
        for service, (status, detail) in results.items():
            prev = await conn.fetch(
                "SELECT status, checked_at FROM health_checks WHERE service = $1 "
                "ORDER BY checked_at DESC LIMIT 6", service)
            await conn.execute(
                "INSERT INTO health_checks (service, status, detail) VALUES ($1, $2, $3)",
                service, status, detail)
            prev_fail_run = [r for r in prev]
            if status == "fail":
                streak = 1
                for row in prev_fail_run:
                    if row["status"] == "fail":
                        streak += 1
                    else:
                        break
                # Running out of credit is a full outage, not a blip — alert on the
                # first occurrence. Everything else waits for 2 in a row.
                is_billing = bool(detail and detail.startswith("BILLING:"))
                if is_billing and streak == 1:
                    alerts.append(
                        f"Claude API is out of credit — Ghost's brain is down until you top it up. "
                        f"Nudges and chat will fail. Detail: {detail[9:]}")
                elif streak == 2 and not is_billing:  # confirmed failure — once per episode
                    alerts.append(
                        f"{service} has been failing since {_fmt(prev_fail_run[0]['checked_at'])} — "
                        f"{SERVICE_HINTS.get(service, 'check the pod')}. Latest error: {detail}")
            elif status == "ok" and len(prev_fail_run) >= 2 \
                    and prev_fail_run[0]["status"] == "fail" and prev_fail_run[1]["status"] == "fail":
                first_fail = prev_fail_run[0]["checked_at"]
                for row in prev_fail_run:
                    if row["status"] != "fail":
                        break
                    first_fail = row["checked_at"]
                alerts.append(f"{service} recovered — had been failing since {_fmt(first_fail)}.")

        if telegram_ok:
            pending = await conn.fetch(
                "SELECT id, message FROM pending_alerts WHERE sent_at IS NULL ORDER BY created_at")
            if pending:
                summary = "While Telegram was unreachable:\n" + "\n".join(
                    f"- {p['message']}" for p in pending)
                if await send_alert(summary):
                    await conn.execute(
                        "UPDATE pending_alerts SET sent_at = NOW() WHERE id = ANY($1)",
                        [p["id"] for p in pending])
                    logger.info(f"Health: flushed {len(pending)} pending alert(s)")

        for alert in alerts:
            if telegram_ok and await send_alert(alert):
                logger.info(f"Health alert sent: {alert}")
            else:
                await conn.execute("INSERT INTO pending_alerts (message) VALUES ($1)", alert)
                logger.warning(f"Health alert queued (telegram down): {alert}")
    finally:
        await conn.close()

async def monthly_csv_upload_nudge():
    """S70: once-a-month plain reminder to export + upload the latest bank CSV on the Bills dashboard,
    so the spending picture stays current (her bank export caps at ~120 days with no custom range, so
    a monthly upload keeps it from missing transactions). Deliberately a SINGLE plain reminder, not an
    escalating nag — housekeeping, not a tracked habit with streaks. Fires on the 1st ~10:00 local,
    once per calendar month (Redis-guarded). Uses the normal Telegram nudge channel."""
    now = datetime.now(LOCAL_TZ)
    if not (now.day == 1 and now.hour == 10 and now.minute < 10):
        return
    r = await get_redis()
    try:
        key = f"csv_upload_nudge:{now.strftime('%Y-%m')}"
        if await r.get(key):
            return
        await r.set(key, "1", ex=40 * 86400)   # ~40 days, comfortably past the month
    finally:
        await r.aclose()
    msg = ("Start-of-month check: export your latest bank CSV and upload it on the Bills dashboard so "
           "your spending picture stays current. Your bank export only goes back about 120 days, so a "
           "monthly upload keeps it from missing anything.")
    await send_telegram(msg, msg_type="nudge")
    logger.info("Monthly CSV-upload nudge sent")

async def run_habitica_sync():
    """Session 23: Habitica<->vault sync on its own jittered 15-30 min cadence
    (separate from this 300s loop). Ships DRY-RUN (writes nothing) until the flip
    to live after Lea reviews a day's log. The sync module owns all read/write
    logic; this is just the scheduling gate."""
    r = await get_redis()
    try:
        now = datetime.now(LOCAL_TZ).timestamp()
        nxt = await r.get("habitica_sync_next_due")
        if nxt and now < float(nxt):
            return
        await r.set("habitica_sync_next_due", now + random.uniform(900, 1800), ex=86400)
    finally:
        await r.aclose()
    try:
        import habitica_sync
        await habitica_sync.run_once(get_redis)
    except Exception as e:
        logger.error(f"habitica_sync error: {e}")

# ---------------------------------------------------------------------------
# Session 76 — "bad guys": three named recurring obstacles surfaced as READ-ONLY threshold
# checks against data that already exists (momentum_events, native todos, app_usage). No new
# "has-a-bad-guy-appeared" table — the fired/cleared edge state lives in Redis. One mention when it
# first triggers (consolidated if several trigger at once), then silence; it clears itself when the
# underlying signal recovers (hysteresis so it can't flap). Waking-hours only. All thresholds are
# biased CONSERVATIVE so these don't fire constantly.
#   - burnout: 3 consecutive completed days each < 40% of the 14-day median daily completions,
#     and only when that median is >= 3/day (an established routine to drop from). Clears when
#     yesterday returns to >= 60% of the median.
#   - decision_fatigue: open native-todo backlog >= 12 (her current 7 won't fire). Clears < 10.
#   - doomscrolling: distraction (not_productive) minutes this week >= 1.5x last week, with a
#     >=420min/prior-week floor. RELATIVE by design — app_usage minute magnitudes are inflated
#     (a known Tasker duration quirk), so comparing this-week vs last-week (like-to-like) is far
#     more trustworthy than any absolute cutoff. Clears when back under 1.2x.
# ---------------------------------------------------------------------------
BADGUY_LABELS = {"burnout": "Burnout", "decision_fatigue": "Decision fatigue",
                 "doomscrolling": "Doomscrolling"}

async def check_bad_guys():
    now = datetime.now(LOCAL_TZ)
    if not (8 <= now.hour < 21):          # a first-trigger heads-up, never a 3am buzz
        return
    today = now.date()
    try:
        conn = await asyncpg.connect(host=POSTGRES_HOST, port=5432, user="postgres",
                                     password=POSTGRES_PASSWORD, database="ghost_db")
    except Exception as e:
        logger.error(f"bad_guys pg connect: {e}")
        return
    results = {}   # name -> (triggered, recovered, message)
    try:
        # --- burnout: sustained drop in daily completions ---
        rows = await conn.fetch(
            "SELECT (completed_at AT TIME ZONE $1)::date d, COUNT(*) n FROM momentum_events "
            "WHERE completed_at >= now() - interval '18 days' GROUP BY d", str(LOCAL_TZ))
        byday = {r["d"]: r["n"] for r in rows}
        cnt = lambda off: byday.get(today - timedelta(days=off), 0)
        recent3 = [cnt(1), cnt(2), cnt(3)]                       # 3 completed days (excl. partial today)
        base_days = sorted(byday.get(today - timedelta(days=k), 0) for k in range(4, 18))
        baseline = base_days[len(base_days) // 2] if base_days else 0    # 14-day median
        burnout_trig = baseline >= 3 and all(x < 0.4 * baseline for x in recent3)
        burnout_recov = (baseline == 0) or (cnt(1) >= 0.6 * baseline)
        results["burnout"] = (burnout_trig, burnout_recov,
            "Burnout watch: momentum's dropped off for a few days straight, well below your usual pace. "
            "Not a nag — just noticing. Rest is allowed to count.")
        # --- decision fatigue: open native-todo backlog ---
        openn = await conn.fetchval(
            "SELECT COUNT(*) FROM todos WHERE NOT done AND NOT archived") or 0
        results["decision_fatigue"] = (openn >= 12, openn < 10,
            f"Decision fatigue: {openn} open to-dos are stacking up. Maybe pick the three that "
            "actually matter today and let the rest wait.")
        # --- doomscrolling: distraction minutes elevated vs the prior week ---
        au = await conn.fetch(
            "SELECT date, app, minutes FROM app_usage WHERE date >= CURRENT_DATE - 14")
        cats = load_app_categories()
        last7 = prior7 = 0
        for a in au:
            if categorize_app(a["app"], cats) != "not_productive":
                continue
            age = (today - a["date"]).days
            if 0 <= age < 7:
                last7 += a["minutes"]
            elif 7 <= age < 14:
                prior7 += a["minutes"]
        ds_trig = prior7 >= 420 and last7 >= 1.5 * prior7
        ds_recov = (prior7 == 0) or (last7 < 1.2 * prior7)
        results["doomscrolling"] = (ds_trig, ds_recov,
            "Doomscrolling's up: distraction-app time has climbed noticeably over the last week. "
            "A gentle heads-up, no judgement.")
    finally:
        await conn.close()

    try:
        r = await get_redis()
    except Exception as e:
        logger.error(f"bad_guys redis: {e}")
        return
    try:
        newly = []
        for name, (trig, recov, _msg) in results.items():
            active = await r.get(f"badguy:{name}:active")
            if trig and not active:
                newly.append(name)
                await r.set(f"badguy:{name}:active", "1", ex=30 * 86400)
            elif active and recov:
                await r.delete(f"badguy:{name}:active")
                logger.info(f"bad guy cleared: {name}")
            elif active:
                await r.set(f"badguy:{name}:active", "1", ex=30 * 86400)   # keep, no repeat send
        if newly:
            if len(newly) == 1:
                msg = results[newly[0]][2]
            else:
                names = ", ".join(BADGUY_LABELS[n] for n in newly)
                msg = (f"A few obstacles showing up at once — {names}. One mention, not a nag: "
                       "worth a pause and a small reset when you can.")
            await send_telegram(msg, msg_type="nudge")
            logger.info(f"bad guys triggered: {newly}")
    finally:
        await r.aclose()


async def main():
    logger.info("Ghost scheduler starting...")
    while True:
        try:
            await refresh_location_override()
            await refresh_wifi_location()
            # Messaging cadence v2 (S79): three daily anchors + one silence follow-up each.
            await morning_debrief()
            await lunch_checkin()
            await dinner_catchup()
            await _anchor_silence_followup()
            await monthly_csv_upload_nudge()
            await check_missed_meals()
            await check_food_tracked()
            await expire_commitments()
            await run_habitica_sync()
            await check_bad_guys()
            await health_check()
        except Exception as e:
            logger.error(f"Scheduler error: {e}")
        await asyncio.sleep(300)

if __name__ == "__main__":
    asyncio.run(main())
