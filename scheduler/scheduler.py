import asyncio
import os
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

RECENT_PHRASES_KEY = "recent_ghost_phrases"

SYSTEM_PROMPT = f"""You are Ghost. Dry, blunt, minimal. Dark humour when it fits.
You are sending a proactive check-in message to {USER_NAME}. Keep it to one line unless told otherwise.
You are a person who noticed something, not a timer going off. Vary how you open — never the same angle twice in a row.
No filler words. Exclamation marks almost never.
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
APP_CATEGORIES_REL = "Projects/Ghost/Ghost — App Categories.md"
APP_CATEGORIES_CACHE_SECONDS = 300
_app_categories_cache = {"ts": 0.0, "data": None}

def load_app_categories() -> dict:
    """{'not_productive': set, 'streaming': set, 'excluded': set,
    'wifi': {ssid_lower: 'home'|'work'}}. Unknown apps default to productive.
    Placeholder SSIDs in <angle brackets> are ignored. Missing note = empty
    config (everything productive, no wifi detection) — never an error state."""
    now = time.monotonic()
    if _app_categories_cache["data"] is not None and \
            now - _app_categories_cache["ts"] < APP_CATEGORIES_CACHE_SECONDS:
        return _app_categories_cache["data"]
    data = {"not_productive": set(), "streaming": set(), "excluded": set(), "wifi": {}}
    section_map = {"not productive": "not_productive", "streaming / neutral": "streaming",
                   "excluded / unclassified": "excluded", "wi-fi networks": "wifi"}
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
            elif section:
                m = re.match(r"\s*-\s*`([^`]+)`", line)
                if m:
                    data[section].add(m.group(1).strip().lower())
    except OSError as e:
        logger.error(f"app categories note unreadable ({e}) — defaults apply")
    _app_categories_cache["ts"] = now
    _app_categories_cache["data"] = data
    return data

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
        location = f"{USER_NAME} said she's {'at the office' if loc == 'office' else 'working from home'} today (her word, today only)"
    elif source == "wifi":
        location = f"her phone is on the {'work' if loc == 'office' else 'home'} wifi (auto-detected, current)"
    elif loc == "office":
        location = f"usually an office day for {USER_NAME} (Mon-Wed pattern, not confirmed)"
    elif loc == "wfh":
        location = f"usually a WFH day for {USER_NAME} (Thu-Fri pattern, not confirmed)"
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
                "http://10.42.0.1:11434/api/generate",
                json={
                    "model": "llama3.2",
                    "prompt": prompt,
                    "stream": False,
                    "keep_alive": "1h",
                    "options": {
                        "temperature": 0.7,
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
# Tier-2 nudge engine (water, meals — and debt if a debt nudge ever exists).
# Session 15, tasks 1/2/5. Before this session NO resend-if-ignored escalation
# existed anywhere (the "every 10 minutes" ladder was spec, never code) — this
# builds it, at the 25-minute interval the user chose. Tier 1 (Habitica to-dos) has
# no scheduled nudges today; the coordinator foregrounds a 'lead' item if one
# is ever added, so tier-1-first consolidation is structural, not aspirational.
#
# Shape per tick: collectors return due items instead of sending; the
# coordinator sends ONE message covering everything due (task 5), tracks
# per-category escalation state in Redis (escalation:{category} JSON), and
# resends at TIER2_RESEND_SECONDS with the tone ladder gentle → firmer → crude.
# Session 17 retune: every stage asks rather than asserts, crude fires at most
# CRUDE_CAP times then drops to the gentle stage-4 wind-down (still on the
# resend cadence), and a 75+ min phone-inactivity gap freezes the whole ladder,
# resuming at gentle. Angle rotation (NUDGE_ANGLES) keeps repeats from rhyming.
# ---------------------------------------------------------------------------
TIER2_RESEND_SECONDS = 25 * 60
WATER_ESCALATION_TTL = 9000  # stop resending once the next regular cycle is near
CRUDE_CAP = 2  # session 17 task 2: crude fires at most twice per escalation, then stage 4
INACTIVITY_FREEZE_SECONDS = 75 * 60  # no phone-activity signal for this long -> freeze escalation

MEAL_WINDOWS = {"breakfast": (7, 9), "lunch": (12, 14), "dinner": (18, 20)}

# Every stage asks, never asserts: a missing log is not knowledge that she hasn't
# done the thing, so the nudge is a question about whether it happened — the
# escalation is in tone, not in certainty.
STAGE_TONES = {
    1: ("First nudge. Dry, one line, gentle by Ghost standards. Phrase it as a question "
        "('had water yet?', 'have you eaten?') — never a claim that she hasn't."),
    2: ("Second ask — no response to the first. Firmer and more direct, still one line, "
        "no lecture, and still a QUESTION about whether it's happened, not a statement that it hasn't."),
    3: ("She has not responded to repeated asks. Go genuinely crude and loud — "
        "swearing is allowed and expected here, affectionate-abusive best-friend register. "
        "Calibration for intensity (do NOT reuse the line itself): 'GO DRINK SOME WATER YOU "
        "THIRSTY BITCH'. All-caps is fine. For THIS message the no-exclamation-marks rule is "
        "suspended. One line. Crude about the nagging, never about her worth — and the core is "
        "still asking her to do/confirm the thing, not declaring what she has or hasn't done."),
    4: ("The crude stage already made its point twice and is spent — do NOT go loud again. "
        "Noticeably gentle now: level, kind, one plain line, a simple question with no edge, "
        "no sarcasm, no reference to the earlier yelling."),
}

# Rotate the ANGLE on every repeat ask so resends don't rhyme (same fix the
# scrolling interrupts got in session 12 — the session 16 regression showed the
# tier-2 ladder needs it too: three near-identical 'X IN THE FUCKING MORNING'
# lines in one morning came from the prompt feeding the model the ask count).
NUDGE_ANGLES = [
    "how small and quick the thing itself is",
    "what she gets out of it right now — energy, headache, focus",
    "pure best-friend exasperation, no reasoning offered at all",
    "flat and practical, like ticking a checklist item",
    "one fresh absurd image, no recycled jokes",
]
NUDGE_BANS = ("Do NOT state the clock time or complain about what time it is, do NOT count or "
              "mention how many times you've asked, and do not reuse the opening shape of any "
              "recent Ghost line.")

async def _escalation_get(r, category: str):
    import json as _json
    raw = await r.get(f"escalation:{category}")
    return _json.loads(raw) if raw else None

async def _escalation_set(r, category: str, state: dict, ttl: int):
    import json as _json
    await r.set(f"escalation:{category}", _json.dumps(state), ex=ttl)

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

async def collect_water_nudge(r, now: float):
    """Initial water nudge, same gating as the old check_water. Returns item or None."""
    if await _escalation_get(r, "water"):
        return None  # an active escalation owns the resend cadence
    next_due = await r.get("water_next_due")
    if next_due and now < float(next_due):
        return None
    interval = random.uniform(9000, 12600)  # 2.5-3.5 hours, unchanged
    last_confirmed = await r.get("last_water_confirmed")
    last_drink = await r.get("last_drink_mentioned")
    if (last_confirmed and (now - float(last_confirmed)) < 10800) or \
       (last_drink and (now - float(last_drink)) < 3600):
        await r.set("water_next_due", now + interval, ex=86400)
        logger.info("Water nudge skipped — recent drink/confirmation")
        return None
    nudge_count = int(await r.get("water_nudge_count") or 0)
    return {"category": "water", "stage": 1, "tier": 2,
            "desc": f"water (target 1.5L, nudge {nudge_count + 1} today)",
            "interval": interval, "nudge_count": nudge_count}

async def collect_meal_nudge(r, now_dt):
    """Initial meal reminder, same gating as the old check_meals. Returns item or None."""
    hour = now_dt.hour
    meal = next((m for m, (a, b) in MEAL_WINDOWS.items() if a <= hour < b), None)
    if meal is None:
        return None
    if await r.get(f"{meal}_reminded"):
        return None
    if await _meal_logged(meal):
        await r.set(f"{meal}_reminded", "1", ex=seconds_to_midnight())
        logger.info(f"{meal} reminder skipped — already logged")
        return None
    return {"category": meal, "stage": 1, "tier": 2,
            "desc": f"{meal} (window open, nothing logged)"}

async def _phone_activity_age(r, now: float):
    """Seconds since the last Tasker-webhook signal, or None if no signal has ever
    been recorded. Today the only feeder is the scrolling webhook (there is no
    movement-detection webhook — checked, only /webhook/scrolling exists); task 3's
    screen-unlock events make this signal much denser."""
    last = await r.get("last_phone_activity")
    return (now - float(last)) if last else None

# ---------------------------------------------------------------------------
# Tier 1: Habitica to-dos with the 'focus' (urgent/today) badge — session 17
# task 4. Reality check first (grep, 21/07): before this, scheduler.py touched
# Habitica in exactly three places — morning briefing read, evening summary
# read, FatSecret 'Track food' auto-complete. The Integration Map's per-daily
# reminder column and tier-1 to-do nudging existed NOWHERE. This builds both.
# One escalation arc per day; escalates/freezes/caps through the same engine.
# ---------------------------------------------------------------------------
TODO_NUDGE_START_HOUR = 10
TODO_NUDGE_END_HOUR = 18
TODO_ESCALATION_TTL = 9000

async def collect_todo_nudge(r, now_dt):
    """First tier-1 nudge of the day if any focus-badged to-do is open."""
    if not (TODO_NUDGE_START_HOUR <= now_dt.hour < TODO_NUDGE_END_HOUR):
        return None
    if await _escalation_get(r, "todo") or await r.get("todo_nudged"):
        return None
    try:
        from habitica import get_focus_todos
        todos = await get_focus_todos()
    except Exception as e:
        logger.error(f"focus todo fetch error: {e}")
        return None
    if not todos:
        return None
    names = ", ".join(f"'{t}'" for t in todos[:3])
    count = f"{len(todos)} to-dos" if len(todos) > 3 else "to-dos"
    return {"category": "todo", "stage": 1, "tier": 1, "todos": todos,
            "desc": f"her own urgent-flagged {count} ({names}{', …' if len(todos) > 3 else ''})"}

# Dailies reminders — the Integration Map's timing intent applied to the dailies
# that ACTUALLY exist. Reality check 21/07 went deeper than expected: the map's
# granular dailies (Film content, Yoga M/W/F, Weights, Long yoga flow, Wellness
# protocols, Evening skincare, Track habits, Track food) do not exist in Habitica
# anymore — the live board has 7 grouped dailies (Morning/Evening Routine,
# Exercise, three meals, Bedtime). Rows are matched against the REAL names;
# the map's yoga/weights "by 7pm" timing maps to Exercise, its evening-group
# rows map to Evening Routine. One reminder per row per day, NO escalation
# (only water/meals/to-dos escalate; a 9pm routine ladder helps nobody).
# Not built: meal dailies (the tier-2 meal engine owns meal reminders),
# Morning Routine (the 7am briefing already lists pending dailies), Bedtime
# (check_wind_down owns it via the activity signal).
DAILY_REMINDERS = [
    # (key/name-substring, weekdays or None=all, (start_h, start_m), end_h, instruction)
    ("exercise", None, (19, 0), 21,
     "ask whether exercise happened today — her Habitica Exercise daily is still open"),
    ("evening routine", None, (20, 0), 22,
     "one grouped question — ask whether the evening routine is done (it's one daily "
     "covering wellness protocols, skincare, tracking) — never item by item"),
]

async def collect_daily_reminders(r, now_dt):
    """Due, unflagged Dailies-map reminders whose Habitica daily is still pending.
    One get_dailies() call covers every due row; a daily that's already done (or
    not present) is flagged silently so it isn't re-checked all window."""
    due = []
    for key, weekdays, (sh, sm), end_h, instruction in DAILY_REMINDERS:
        if weekdays is not None and now_dt.weekday() not in weekdays:
            continue
        if not ((now_dt.hour, now_dt.minute) >= (sh, sm) and now_dt.hour < end_h):
            continue
        if await r.get(f"daily_reminded:{key}"):
            continue
        due.append((key, instruction))
    if not due:
        return []
    try:
        from habitica import get_dailies
        dailies = await get_dailies()
    except Exception as e:
        logger.error(f"daily reminders habitica fetch error: {e}")
        return []
    pending = [p.lower() for p in dailies["pending"]]
    items = []
    for key, instruction in due:
        hit = any(key in p and not (key == "yoga" and "long" in p) for p in pending)
        if hit:
            items.append({"category": f"daily:{key}", "stage": 1, "tier": 2,
                          "desc": instruction, "no_escalate": True})
        else:
            # done or not due in Habitica — flag so the window stays quiet
            await r.set(f"daily_reminded:{key}", "1", ex=seconds_to_midnight())
    return items

async def check_wind_down():
    """Integration Map 'Bed by 9:30' row: if the phone is visibly active after
    9:15pm, one gentle wind-down ask. Needs the Tasker activity signal — no
    signal, no flag (a missing log is never evidence she's up)."""
    now_dt = datetime.now(LOCAL_TZ)
    if not (now_dt.hour == 21 and now_dt.minute >= 15):
        return
    r = await get_redis()
    try:
        if await r.get("wind_down_flagged"):
            return
        last = await r.get("last_phone_activity")
        if not last or (now_dt.timestamp() - float(last)) > 900:
            return
        message = await call_claude(
            f"{time_context()} Her phone has been active past 9:15pm and bed-by-9:30 is her own "
            "goal. One gentle line asking whether she's winding down — a question, no lecture, "
            "no guilt.")
        await send_telegram(message)
        await r.set("wind_down_flagged", "1", ex=seconds_to_midnight())
        logger.info("Wind-down flag sent")
    finally:
        await r.aclose()

async def collect_escalation_resends(r, now: float):
    """Ignored tier-2 nudges past the 25-min resend interval. Clears responded/expired.
    Session 17: freezes entirely during 75+ min phone-inactivity gaps (yelling at a
    phone in a drawer helps nobody), resumes at gentle; crude capped at CRUDE_CAP
    firings then drops to the stage-4 wind-down tone."""
    items = []
    activity_age = await _phone_activity_age(r, now)
    # No signal ever recorded = the feed isn't wired/flowing, NOT evidence she's away
    # (a missing log is never evidence). Freeze only on a real, stale signal.
    inactive = activity_age is not None and activity_age >= INACTIVITY_FREEZE_SECONDS
    for category in ("todo", "water", "breakfast", "lunch", "dinner"):
        state = await _escalation_get(r, category)
        if not state:
            continue
        fresh_todos = None
        if category == "todo":
            # responded = the focus list shrank (she acted) or emptied; arc dies at window end
            try:
                from habitica import get_focus_todos
                fresh_todos = await get_focus_todos()
            except Exception as e:
                logger.error(f"todo escalation habitica check error: {e}")
                continue  # can't verify — neither clear nor resend on a blind tick
            if not fresh_todos or len(fresh_todos) < state.get("baseline", 1):
                await r.delete("escalation:todo")
                logger.info("todo escalation cleared — she acted on the list")
                continue
            if datetime.now(LOCAL_TZ).hour >= TODO_NUDGE_END_HOUR:
                await r.delete("escalation:todo")
                logger.info("todo escalation expired — nudge window closed")
                continue
        # responded? water: any confirmation newer than the escalation start
        elif category == "water":
            responded = False
            for key in ("last_water_confirmed", "last_drink_mentioned"):
                v = await r.get(key)
                if v and float(v) > state["started"]:
                    responded = True
            if responded:
                await r.delete("escalation:water")
                logger.info("Water escalation cleared — she responded")
                continue
        else:
            if await _meal_logged(category):
                await r.delete(f"escalation:{category}")
                logger.info(f"{category} escalation cleared — meal logged")
                continue
            end_hour = MEAL_WINDOWS[category][1]
            if datetime.now(LOCAL_TZ).hour >= end_hour:
                await r.delete(f"escalation:{category}")
                logger.info(f"{category} escalation expired — window closed")
                continue
        ttl = await r.ttl(f"escalation:{category}")
        if ttl is None or ttl <= 0:
            ttl = 60
        if inactive:
            if not state.get("frozen"):
                state["frozen"] = True
                await _escalation_set(r, category, state, ttl)
                logger.info(f"{category} escalation FROZEN — no phone activity for "
                            f"{activity_age / 60:.0f}min")
            continue  # no resend, no stage advance while frozen
        if state.get("frozen"):
            # Activity resumed — reset to gentle rather than picking up mid-crude.
            state.update(frozen=False, stage=0, crude_sends=0)
            await _escalation_set(r, category, state, ttl)
            logger.info(f"{category} escalation unfrozen — reset to gentle")
        if now - state["last_sent"] >= TIER2_RESEND_SECONDS:
            cur = state["stage"]
            stage = 4 if cur >= 4 else min(cur + 1, 3)
            if stage == 3 and state.get("crude_sends", 0) >= CRUDE_CAP:
                stage = 4  # crude spent — gentle wind-down, still on the 25-min cadence
            if category == "todo":
                names = ", ".join(f"'{t}'" for t in fresh_todos[:3])
                count = f"{len(fresh_todos)} to-dos" if len(fresh_todos) > 3 else "to-dos"
                desc = f"her own urgent-flagged {count}, still open ({names}{', …' if len(fresh_todos) > 3 else ''})"
            else:
                desc = f"{category} (still nothing logged)"
            items.append({"category": category, "stage": stage,
                          "tier": 1 if category == "todo" else 2, "desc": desc,
                          "asks": state.get("sends", cur), "resend": True,
                          "baseline": state.get("baseline")})
    return items

async def _apply_nudge_state(r, item, now: float):
    """Post-send bookkeeping per item: flags, counters, escalation state."""
    category = item["category"]
    if item.get("no_escalate"):
        # Dailies-map reminders: one per row per day, no escalation state at all.
        await r.set(f"daily_reminded:{category.split(':', 1)[1]}", "1", ex=seconds_to_midnight())
        return
    if category == "todo":
        if not item.get("resend"):
            await r.set("todo_nudged", "1", ex=seconds_to_midnight())  # one arc per day
        ttl = TODO_ESCALATION_TTL
    elif category == "water":
        if not item.get("resend"):
            await r.set("water_nudge_count", item["nudge_count"] + 1, ex=seconds_to_midnight())
            await r.set("water_next_due", now + item["interval"], ex=86400)
        ttl = WATER_ESCALATION_TTL
    else:
        if not item.get("resend"):
            await r.set(f"{category}_reminded", "1", ex=seconds_to_midnight())
        end_hour = MEAL_WINDOWS[category][1]
        now_dt = datetime.now(LOCAL_TZ)
        window_close = now_dt.replace(hour=end_hour, minute=0, second=0, microsecond=0)
        ttl = max(int((window_close - now_dt).total_seconds()), 60)
    prev = await _escalation_get(r, category)
    started = prev["started"] if prev else now
    sends = (prev.get("sends", prev["stage"]) if prev else 0) + 1
    crude_sends = (prev.get("crude_sends", 0) if prev else 0) + (1 if item["stage"] == 3 else 0)
    state = {"stage": item["stage"], "last_sent": now, "started": started,
             "sends": sends, "crude_sends": crude_sends, "frozen": False}
    baseline = item.get("baseline") or (len(item["todos"]) if item.get("todos") else None)
    if baseline:
        state["baseline"] = baseline
    await _escalation_set(r, category, state, ttl)

async def process_tier2_nudges():
    """Tasks 1/2/5: collect everything due this tick, send ONE message, escalate on ignore."""
    if is_quiet_hours():
        return
    now_dt = datetime.now(LOCAL_TZ)
    now = now_dt.timestamp()
    r = await get_redis()
    try:
        pending = []
        todo = await collect_todo_nudge(r, now_dt)
        if todo:
            pending.append(todo)
        water = await collect_water_nudge(r, now)
        if water:
            pending.append(water)
        meal = await collect_meal_nudge(r, now_dt)
        if meal:
            pending.append(meal)
        pending.extend(await collect_daily_reminders(r, now_dt))
        pending.extend(await collect_escalation_resends(r, now))
        if not pending:
            return
        # Tier-ascending sort: tier-1 (her own urgent to-dos) leads a combined message.
        pending.sort(key=lambda p: (p["tier"], -p["stage"]))
        # Tone for the combined message: crude (3) wins if any item is there; the
        # stage-4 wind-down is GENTLER than 3 despite the higher number.
        stages = [p["stage"] for p in pending]
        stage = 3 if 3 in stages else max(stages)
        if len(pending) == 1:
            p = pending[0]
            if p["category"] == "todo" and not p.get("resend"):
                body = (f"Generate a check-in about {p['desc']} — these are to-dos SHE flagged "
                        "urgent, so ask how they're going or when she's starting, as an ally "
                        "not a taskmaster.")
            elif p["category"] == "water" and not p.get("resend"):
                body = (f"Generate a water check-in for {USER_NAME} — ask whether she's had water yet "
                        "(her target is 1.5L). Make it fit the time of day.")
            elif p["category"] in MEAL_WINDOWS and not p.get("resend"):
                body = (f"Generate a {p['category']} check-in for {USER_NAME} — ask whether she's eaten. "
                        "She tends to skip meals when busy. Make it fit the day and time, "
                        "not a generic 'eat food' ping.")
            elif p.get("no_escalate"):
                body = f"Generate a single one-line evening check-in for {USER_NAME}: {p['desc']}."
            else:
                angle = NUDGE_ANGLES[p.get("asks", 1) % len(NUDGE_ANGLES)]
                body = (f"Generate a follow-up nudge about {p['desc']} — earlier nudge(s) got no "
                        f"response, so ASK whether it's happened. "
                        f"Take THIS angle and no other: {angle}. {NUDGE_BANS}")
        else:
            body = ("One combined nudge covering everything due right now — do NOT write separate "
                    "messages, weave them into one or two lines as questions, most important first: "
                    + "; ".join(p["desc"] for p in pending) + f". {NUDGE_BANS}")
        prompt = f"{time_context()} {body} {STAGE_TONES[stage]}"
        message = await call_claude(prompt)
        await send_telegram(message)
        for p in pending:
            await _apply_nudge_state(r, p, now)
        logger.info(f"Tier-2 nudge sent — items: {[p['category'] for p in pending]}, stage {stage}"
                    + (", consolidated" if len(pending) > 1 else ""))
    finally:
        await r.aclose()

async def check_movement():
    if is_quiet_hours():
        return
    now_dt = datetime.now(LOCAL_TZ)
    now = now_dt.timestamp()
    r = await get_redis()
    next_due = await r.get("movement_next_due")
    last_confirmed = await r.get("last_movement_confirmed")
    exercise_done = await r.get("exercise_confirmed")
    await r.aclose()

    if next_due and now < float(next_due):
        return

    # Schedule-aware cadence (task 3): office days (Mon-Wed pattern) already contain
    # commute + office movement, so relax the interval 3x (~2.25-3.75h). WFH days
    # (Thu-Fri) and weekends keep the original 45-75 min — Ghost's own spec always
    # said WFH days need this more. Pattern-based, overridable by "I'm home today" /
    # "I'm at the office today" in chat (bounded to local midnight).
    loc, overridden = location_today()
    if loc == "office":
        interval = random.uniform(2700, 4500) * 3
    else:
        interval = random.uniform(2700, 4500)  # 45-75 minutes, unchanged

    if exercise_done:
        r = await get_redis()
        await r.set("movement_next_due", now + interval, ex=86400)
        await r.aclose()
        logger.info("Movement nudge skipped — exercise confirmed today")
        return

    confirmed_recently = last_confirmed and (now - float(last_confirmed)) < 3600
    if confirmed_recently:
        r = await get_redis()
        await r.set("movement_next_due", now + interval, ex=86400)
        await r.aclose()
        logger.info("Movement nudge skipped — recent movement confirmation")
        return

    if now_dt.hour < 10:
        suggestion = "Suggest something like a short morning walk before the day gets away."
    elif now_dt.hour < 17:
        suggestion = ("She's likely at a desk — suggest standing up, stretching, or a lap around the block."
                      if loc != "office" else
                      "Office day — she's had the commute at least; keep it light, a stretch or a walk at lunch.")
    else:
        suggestion = "Evening — suggest a stretch or short walk to close out the day, nothing strenuous."
    prompt = (
        f"{time_context()} Generate a movement nudge for {USER_NAME} — nothing logged for a while, so ASK "
        f"whether she's been up and moving (a question, not a claim that she hasn't). {suggestion}"
    )
    message = await call_claude(prompt)
    await send_telegram(message)
    r = await get_redis()
    await r.set("movement_next_due", now + interval, ex=86400)
    await r.aclose()
    logger.info(f"Movement nudge sent ({loc} {overridden or 'pattern'}), next due in {interval / 60:.0f}min")

async def morning_briefing():
    now = datetime.now(LOCAL_TZ)
    if now.hour == 7 and now.minute < 5:
        r = await get_redis()
        already_sent = await r.get("morning_briefing_sent")
        await r.aclose()
        if not already_sent:
            try:
                from habitica import get_dailies, get_habits, get_todos
                dailies = await get_dailies()
                pending = dailies["pending"]
                done = dailies["done"]
                todos = await get_todos()
                habits = await get_habits()
                prompt = (
                    f"{time_context()} Generate a morning briefing for {USER_NAME}. "
                    f"Pending dailies: {(', '.join(pending)) if pending else 'none'}. "
                    f"Already completed: {(', '.join(done)) if done else 'none'}. "
                    f"Outstanding to-dos: {(', '.join(todos[:5])) if todos else 'none'}. "
                    f"Habits to keep in mind: {(', '.join(habits)) if habits else 'none'}. "
                    "Lead with dailies, mention key to-dos, habits are lowest priority. "
                    "Keep it under 4 lines. Be specific."
                )
            except Exception as e:
                logger.error(f"Habitica fetch failed: {e}")
                prompt = f"{time_context()} Generate a brief morning briefing. Mention water, movement, and staying focused today. Keep it under 3 lines."
            message = await call_claude(prompt, max_tokens=150)
            await send_telegram(message, msg_type="briefing")
            r = await get_redis()
            await r.set("morning_briefing_sent", "1", ex=seconds_to_midnight())
            await r.aclose()
            logger.info("Morning briefing sent")

async def evening_summary():
    now = datetime.now(LOCAL_TZ)
    if now.hour == 21 and now.minute < 5:
        r = await get_redis()
        already_sent = await r.get("evening_summary_sent")
        water_count = await r.get("water_nudge_count") or 0
        last_water = await r.get("last_water_confirmed")
        exercise_done = await r.get("exercise_confirmed")
        await r.aclose()
        if not already_sent:
            habitica_part = ""
            try:
                from habitica import get_dailies, get_completed_todos_today
                dailies = await get_dailies()
                done_todos = await get_completed_todos_today(LOCAL_TZ)
                habitica_part = (
                    f"Dailies done: {', '.join(dailies['done']) if dailies['done'] else 'none logged'}. "
                    f"Dailies still open: {', '.join(dailies['pending']) if dailies['pending'] else 'none'}. "
                    f"To-dos knocked off today: {', '.join(done_todos) if done_todos else 'none'}. "
                )
            except Exception as e:
                logger.error(f"Evening summary habitica fetch failed: {e}")
            usage_part = ""
            try:
                cats = load_app_categories()
                conn = await asyncpg.connect(
                    host=POSTGRES_HOST, port=5432, user="postgres",
                    password=POSTGRES_PASSWORD, database="ghost_db")
                rows = await conn.fetch(
                    "SELECT app, minutes FROM app_usage WHERE date = $1",
                    now.date())
                unlocks = await conn.fetchval(
                    "SELECT value FROM daily_events WHERE date = $1 AND event_type = 'screen_unlocks'",
                    now.date())
                await conn.close()
                if rows:
                    prod = notprod = stream = 0
                    for row in rows:
                        a = row["app"]
                        if a in cats["excluded"]:
                            continue
                        elif a in cats["streaming"]:
                            stream += row["minutes"]
                        elif a in cats["not_productive"]:
                            notprod += row["minutes"]
                        else:
                            prod += row["minutes"]
                    usage_part = (f"Phone use logged today: {prod}min in productive apps, "
                                  f"{notprod}min in not-productive ones. ")
                    if stream:
                        usage_part += f"Streaming/video/music: {stream}min (neutral, counted in neither bucket). "
                    usage_part += ("This split is informational — mention it plainly, no verdict on her day "
                                   "from it, and it never touches the Habitica Productivity daily. ")
                if unlocks:
                    usage_part += f"Screen unlocks logged: {unlocks}. "
            except Exception as e:
                logger.error(f"Evening summary app-usage fetch failed: {e}")
            prompt = (
                f"{time_context()} Generate an evening summary for {USER_NAME}. "
                f"{habitica_part}"
                f"{usage_part}"
                f"Water nudges sent today: {water_count}. "
                f"Water logged at some point today: {'yes' if last_water else 'no'}. "
                f"Exercise logged today: {'yes' if exercise_done else 'no'}. "
                "Unlogged is not undone — phrase gaps as 'nothing logged', not as things she failed to do. "
                "Up to 5 lines. React to how the day actually went; completed to-dos deserve a dry nod."
            )
            message = await call_claude(prompt)
            await send_telegram(message, msg_type="summary")
            r = await get_redis()
            await r.set("evening_summary_sent", "1", ex=seconds_to_midnight())
            await r.aclose()
            logger.info("Evening summary sent")

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
    """Once FatSecret has a real entry for today, mark the 'Track food' Habitica
    daily complete (exact match only). Checks at most every 30 min, once per day."""
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
        from habitica import complete_daily_exact
        ok = await complete_daily_exact("Track food")
        r = await get_redis()
        await r.set("food_daily_completed", "1", ex=seconds_to_midnight())
        await r.aclose()
        if ok:
            logger.info("'Track food' daily auto-completed — FatSecret has entries today")
        else:
            logger.info("FatSecret has entries but no due/incomplete 'Track food' daily — nothing scored")
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

async def send_alert(text: str) -> bool:
    """Telegram send that reports success instead of assuming it."""
    try:
        async with httpx.AsyncClient() as client:
            r = await client.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                json={"chat_id": TELEGRAM_USER_ID, "text": text}, timeout=15
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

async def get_debt_status() -> str:
    """One line per active (balance>0) debt stage, appended to the Sunday summary."""
    try:
        conn = await asyncpg.connect(
            host=POSTGRES_HOST, port=5432, user="postgres",
            password=POSTGRES_PASSWORD, database="ghost_db"
        )
        rows = await conn.fetch(
            "SELECT account_name, balance, interest_rate, min_repayment, stage_number "
            "FROM debt_accounts WHERE balance > 0 ORDER BY stage_number")
        await conn.close()
    except Exception as e:
        logger.error(f"get_debt_status error: {e}")
        return ""
    if not rows:
        return ""
    lines = ["Debt:"]
    for r in rows:
        line = f"  Stage {r['stage_number']} {r['account_name']}: ${float(r['balance']):.2f} owing"
        if r['interest_rate'] is not None:
            line += f" @ {float(r['interest_rate']):.2f}%"
        if r['min_repayment'] is not None:
            line += f" (min ${float(r['min_repayment']):.2f}/mo)"
        lines.append(line)
    return "\n".join(lines)

async def build_budget_summary() -> str:
    try:
        conn = await asyncpg.connect(
            host=POSTGRES_HOST, port=5432, user="postgres",
            password=POSTGRES_PASSWORD, database="ghost_db"
        )
        rows = await conn.fetch("""
            SELECT bc.name, bc.monthly_limit, COALESCE(SUM(e.amount), 0) AS spent
            FROM budget_categories bc
            LEFT JOIN expenses e ON e.category = bc.name
                AND DATE_TRUNC('month', e.logged_at) = DATE_TRUNC('month', NOW())
            GROUP BY bc.name, bc.monthly_limit
            ORDER BY bc.name
        """)
        await conn.close()
        lines = []
        for row in rows:
            spent = float(row['spent'])
            limit = float(row['monthly_limit'])
            status = "OVER" if spent > limit else "under"
            lines.append(f"{row['name']}: ${spent:.2f} / ${limit:.2f} ({status})")
        summary = "\n".join(lines)
        debt = await get_debt_status()
        if debt:
            summary += "\n\n" + debt
        return summary
    except Exception as e:
        logger.error(f"Budget summary error: {e}")
        return ""

async def weekly_budget_summary():
    now = datetime.now(LOCAL_TZ)
    if now.weekday() == 6 and now.hour == 21 and now.minute < 10:
        r = await get_redis()
        already_sent = await r.get("budget_summary_sent")
        await r.aclose()
        if already_sent:
            return
        summary = await build_budget_summary()
        if not summary:
            logger.error("Weekly budget summary skipped — no data")
            return
        commentary = await call_ollama(
            f"You are Ghost, a dry, blunt accountability partner tracking {USER_NAME}'s money. "
            f"Here is her budget for the month so far:\n{summary}\n\n"
            "Give exactly one dry line of commentary on the overall picture. "
            "No exclamation marks, no filler. Don't repeat the table.\nGhost:"
        )
        message = "Weekly budget check.\n" + summary
        if commentary:
            message += "\n\n" + commentary
        await send_telegram(message, msg_type="summary")
        r = await get_redis()
        await r.set("budget_summary_sent", "1", ex=seconds_to_midnight())
        await r.aclose()
        logger.info("Weekly budget summary sent")

async def main():
    logger.info("Ghost scheduler starting...")
    while True:
        try:
            await refresh_location_override()
            await refresh_wifi_location()
            await process_tier2_nudges()
            await check_movement()
            await check_wind_down()
            await morning_briefing()
            await evening_summary()
            await weekly_budget_summary()
            await check_missed_meals()
            await check_food_tracked()
            await health_check()
        except Exception as e:
            logger.error(f"Scheduler error: {e}")
        await asyncio.sleep(300)

if __name__ == "__main__":
    asyncio.run(main())
