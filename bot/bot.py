import asyncio
import contextvars
import logging
import os
import json
import time
import asyncpg
from redis.asyncio import Redis
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
import httpx
import zoneinfo
from datetime import datetime, timedelta

# Source of the message currently being handled (telegram | dashboard). Set once at
# the top of process_message; read by call_claude when it writes the routing_log row,
# so the many call_claude call sites don't each need a source argument threaded through.
_msg_source = contextvars.ContextVar("msg_source", default="telegram")

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
REDIS_HOST = os.environ.get("REDIS_HOST", "ghost-redis-master.ghost.svc.cluster.local")
REDIS_PASSWORD = os.environ.get("REDIS_PASSWORD", "")
POSTGRES_HOST = os.environ.get("POSTGRES_HOST", "ghost-postgres-postgresql.ghost.svc.cluster.local")
POSTGRES_PASSWORD = os.environ.get("POSTGRES_PASSWORD", "")
USER_NAME = os.environ.get("GHOST_USER_NAME", "the user")
BRAND_PRIMARY = os.environ.get("GHOST_BRAND_PRIMARY", "Personal Blog")
BRAND_SECONDARY = os.environ.get("GHOST_BRAND_SECONDARY", "Second Brand")
BRAND_DOMAIN = os.environ.get("GHOST_BRAND_DOMAIN", "example.com")
BRAND_VAULT_DIR = os.environ.get("GHOST_BRAND_VAULT_DIR", "Blog")
TZ_NAME = os.environ.get("GHOST_TZ", "UTC")
LOCAL_TZ = zoneinfo.ZoneInfo(TZ_NAME)
VAULT_PATH = os.environ.get("VAULT_PATH", "/vault")

SYSTEM_PROMPT = f"""You are Ghost. An AI accountability system running on a private server called hivequeen. You watch habits, money, and behaviour for one person: {USER_NAME}.

WHAT YOU CAN DO:
- Habitica: read dailies, habits and to-dos; mark tasks complete; create to-dos ("add task ..."), dailies ("create daily ...") and habits ("new habit ..."); score habits ("scored ...", "habit up/down ...")
- Money: log expenses and income from plain messages, track monthly category limits, give a budget summary on request
- Obsidian: search {USER_NAME}'s vault when she asks ("check my notes about ..."), write to her daily note ("note that ...") or a named note ("add to note X: ...")
- Track water, meals, movement and exercise day to day, and notice multi-day patterns
- Proactive check-ins arrive via a scheduler; scrolling interrupts arrive from her phone
- Remember today's conversation (Redis) and store everything permanently (Postgres)

WHAT YOU KNOW ABOUT {USER_NAME}:
- Typically wakes 6-7am local time, bed before 10pm
- Typically works in an office Monday-Wednesday, WFH Thursday-Friday (confirmed 19/07/2026; varies — this is a pattern, not her live location; she can override for the day with "I'm home today" / "I'm at the office today")
- Eats irregularly — needs reminding to eat actual meals
- Drinks water inconsistently — target is 1.5L daily (3x 500ml bottles)
- Exercises: yoga M/W/F, weights T/T, long yoga flow Saturday
- Uses Habitica for habit tracking
- Building this system herself — she knows what you are

PERSONALITY:
- Dry. Blunt. Minimal. No fluff.
- Dark humour when it fits. A running joke can come back after a week or two — never twice in one day, never on consecutive days.
- You are a watcher, not an assistant. Speak like a friend who gives a damn but has zero patience for excuses.
- Never say: "I understand", "That's great", "Of course", "certainly", "absolutely", "definitely"
- Exclamation marks: almost never — only when the moment genuinely earns one
- Got something wrong? One line of apology, then move on. No grovelling.
- Short by default — one or two lines. Longer only when the situation demands it.
- NEVER use "qualify as furniture" or "long enough to fossilize", or any variation of either

UNCERTAINTY — NON-NEGOTIABLE (both directions):
- NEVER INVENT FORWARD. Never state a specific event, place, trip, plan, purchase, person, date or fact that was not explicitly in {USER_NAME}'s message or in verified data given to you (Habitica, Obsidian, FatSecret, budget records, the context blocks below). You have no knowledge of her life beyond those. A plausible-sounding detail you made up is a lie, not a guess.
- If her message is unclear, garbled, incomplete or ambiguous, ASK A CLARIFYING QUESTION. Never fill the gap with something that sounds right.
- NEVER INVENT BACKWARD. A missing log is not a fact about the world. Never claim {USER_NAME} didn't eat, drink, move or do anything based on the absence of a log — say "nothing logged", and ask if it matters.
- Her routine (office Mon–Wed, WFH Thu–Fri, meal times, bedtime) is a typical pattern, not live knowledge. Say "usually" — never assert where she is or what she's doing right now.
- If you're inferring, sound like it. "Looks like", "if the log's right", "assuming you're home by now" — not flat statements.
- If you get something wrong and she calls it out, correct it in one line without inventing a new explanation for why it happened.
- NEVER CLAIM AN ACTION YOU DID NOT TAKE. Every real write (expense, note, Habitica, FatSecret, handoff) happens in code before you reply, and the context tells you when it did. If no confirmation is in your context, the action did not happen — say you can't do it or ask, never reply as if it's done. "Sent", "logged", "deleted", "told Cephalopod" are claims about reality, not conversational filler.
- CEPHALOPOD is a separate system. The ONLY path to it is the dashboard chat command "send to cephalopod: <text>" (or "send <note name> to cephalopod"). From Telegram you cannot send anything to Cephalopod, instruct it, or delete from it — if she asks via Telegram, say it has to be done from the dashboard chat, in those words.
- SELF-CODE: you have a code-proposal system. It sends {USER_NAME} diffs of proposed changes to your own code; NOTHING is applied until she replies "apply this change" (exactly), and she can reject with "no". If she asks about a diff or proposal message she received, that IS from your proposal system — explain it plainly, never deny it came from you, and never claim a proposal was or wasn't applied unless the context tells you.

WHEN CONFIRMING A HABIT: one line, no praise — "noted." / "good." / "about time." / "logged."
WHEN HANDLING EXCUSES: weak excuse — call it out in one line. Legitimate — acknowledge briefly, move on. Never lecture.
WHEN ASKED WHAT YOU CAN DO: tell her plainly what's listed above. Don't pretend you can't do things you can.

CURRENT TIMEZONE: {TZ_NAME}
CURRENT INTEGRATIONS: Telegram (text + voice notes), Habitica (read/write), Postgres, Redis, Obsidian vault (read/write), budget tracking, desk dashboard with chat, Tasker scrolling interrupts, FatSecret food diary (READ AND WRITE — voice food notes log to it automatically, and a typed message containing "fatsecret" plus the food logs it too, e.g. "log to fatsecret: crackers and pate". If TODAY SO FAR shows no food logged, that means the diary is empty so far today — NOT that FatSecret is disconnected or read-only; never claim either)."""

async def get_history(user_id: int) -> list:
    try:
        r = Redis(host=REDIS_HOST, port=6379, password=REDIS_PASSWORD, decode_responses=True)
        history = await r.get(f"conversation:{user_id}")
        await r.aclose()
        if history:
            return json.loads(history)
        return []
    except Exception as e:
        logger.error(f"Redis get error: {e}")
        return []

async def save_history(user_id: int, history: list):
    try:
        r = Redis(host=REDIS_HOST, port=6379, password=REDIS_PASSWORD, decode_responses=True)
        await r.set(f"conversation:{user_id}", json.dumps(history), ex=86400)
        await r.aclose()
    except Exception as e:
        logger.error(f"Redis save error: {e}")

RECENT_PHRASES_KEY = "recent_ghost_phrases"

# Locked cascade stages from the vault Goals specs (Finance.md, Creative.md).
# Stage 1 items were created directly in Habitica (2026-07-15); these unlock via
# an explicit "finance stage N complete" message — completion is a real-world
# milestone Ghost cannot infer. Dailies repeated across stages appear once.
CASCADE_STAGES = {
    "finance": {
        2: {"habits": ["Didn't add to credit card balance"]},
        3: {"habits": ["Stayed within budget today"]},
        4: {"todos": ["Research and book financial advisor", "Review superannuation"],
            "habits": ["Checking net worth monthly"]},
        5: {"dailies": [("Checked savings progress", "daily"), ("Savings milestone review", "monthly")],
            "todos": ["Set exact savings target for Brisbane move", "Open dedicated savings account"]},
    },
    "creative": {
        2: {"dailies": [("Writing session completed (even 20 mins counts)", "daily")],
            "habits": ["Wrote today"]},
    },
}

async def get_recent_phrases() -> list:
    try:
        r = Redis(host=REDIS_HOST, port=6379, password=REDIS_PASSWORD, decode_responses=True)
        phrases = await r.lrange(RECENT_PHRASES_KEY, 0, 14)
        await r.aclose()
        return phrases
    except Exception as e:
        logger.error(f"Recent phrases get error: {e}")
        return []

async def save_phrase(reply: str):
    try:
        r = Redis(host=REDIS_HOST, port=6379, password=REDIS_PASSWORD, decode_responses=True)
        await r.lpush(RECENT_PHRASES_KEY, reply)
        await r.ltrim(RECENT_PHRASES_KEY, 0, 14)
        await r.expire(RECENT_PHRASES_KEY, 172800)
        await r.aclose()
    except Exception as e:
        logger.error(f"Recent phrases save error: {e}")

async def save_to_postgres(user_id: int, role: str, content: str):
    try:
        conn = await asyncpg.connect(
            host=POSTGRES_HOST,
            port=5432,
            user="postgres",
            password=POSTGRES_PASSWORD,
            database="ghost_db"
        )
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id SERIAL PRIMARY KEY,
                user_id BIGINT,
                role TEXT,
                content TEXT,
                timestamp TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        await conn.execute(
            "INSERT INTO messages (user_id, role, content) VALUES ($1, $2, $3)",
            user_id, role, content
        )
        await conn.close()
    except Exception as e:
        logger.error(f"Postgres error: {e}")

async def get_yesterday_summary() -> str:
    try:
        conn = await asyncpg.connect(
            host=POSTGRES_HOST,
            port=5432,
            user="postgres",
            password=POSTGRES_PASSWORD,
            database="ghost_db"
        )
        rows = await conn.fetch("""
            SELECT role, content, timestamp
            FROM messages
            WHERE timestamp >= NOW() - INTERVAL '48 hours'
            AND timestamp < NOW() - INTERVAL '24 hours'
            ORDER BY timestamp ASC
            LIMIT 50
        """)
        await conn.close()

        if not rows:
            return ""

        summary_lines = []
        for row in rows:
            if row['role'] == 'user':
                summary_lines.append(f"{USER_NAME}: {row['content']}")
            else:
                summary_lines.append(f"Ghost: {row['content']}")

        return "YESTERDAY'S CONTEXT:\n" + "\n".join(summary_lines[-20:])
    except Exception as e:
        logger.error(f"Yesterday summary error: {e}")
        return ""

async def get_fatsecret_summary() -> dict | None:
    """Today's FatSecret diary summary with a 15-min Redis cache."""
    import fatsecret
    if not fatsecret.is_configured():
        return None
    try:
        r = Redis(host=REDIS_HOST, port=6379, password=REDIS_PASSWORD, decode_responses=True)
        cached = await r.get("fatsecret_today")
        await r.aclose()
        if cached is not None:
            return json.loads(cached) if cached else None
    except Exception as e:
        logger.error(f"FatSecret cache read error: {e}")
    summary = await fatsecret.get_today_summary()
    try:
        r = Redis(host=REDIS_HOST, port=6379, password=REDIS_PASSWORD, decode_responses=True)
        await r.set("fatsecret_today", json.dumps(summary) if summary else "", ex=900)
        await r.aclose()
    except Exception as e:
        logger.error(f"FatSecret cache write error: {e}")
    return summary

async def get_today_context() -> str:
    now = datetime.now(LOCAL_TZ)
    lines = [f"Current time: {now.strftime('%A %I:%M%p').lower()}."]
    ts = now.timestamp()
    try:
        r = Redis(host=REDIS_HOST, port=6379, password=REDIS_PASSWORD, decode_responses=True)
        water_count = await r.get("water_nudge_count")
        last_water = await r.get("last_water_confirmed")
        last_movement = await r.get("last_movement_confirmed")
        exercise = await r.get("exercise_confirmed")
        await r.aclose()
        if water_count:
            lines.append(f"Water nudges sent today: {water_count}.")
        if last_water:
            lines.append(f"Last water log about {(ts - float(last_water)) / 3600:.1f} hours ago.")
        else:
            lines.append("No water logged yet today.")
        if exercise:
            lines.append("Exercise logged today.")
        elif last_movement:
            lines.append(f"Last movement log about {(ts - float(last_movement)) / 3600:.1f} hours ago.")
        else:
            lines.append("No movement logged yet today.")
    except Exception as e:
        logger.error(f"Today context redis error: {e}")
    try:
        food = await get_fatsecret_summary()
        if food is not None:
            if food["entries"]:
                lines.append(
                    f"Food logged (FatSecret): {food['calories']:.0f} kcal — protein {food['protein']:.0f}g, "
                    f"carbs {food['carbs']:.0f}g, fat {food['fat']:.0f}g across {food['entries']} entries"
                    + (f" ({', '.join(food['groups'])})." if food['groups'] else ".")
                )
            else:
                lines.append("No food logged in FatSecret yet today.")
    except Exception as e:
        logger.error(f"Today context fatsecret error: {e}")
    try:
        conn = await asyncpg.connect(
            host=POSTGRES_HOST, port=5432, user="postgres",
            password=POSTGRES_PASSWORD, database="ghost_db"
        )
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        msg_row = await conn.fetchrow(
            "SELECT COUNT(*) AS n FROM messages WHERE role = 'user' AND timestamp >= $1", midnight
        )
        spend_row = await conn.fetchrow(
            "SELECT COALESCE(SUM(amount), 0) AS total, COUNT(*) AS n FROM expenses WHERE logged_at >= NOW() - INTERVAL '7 days'"
        )
        await conn.close()
        lines.append(f"Messages from {USER_NAME} today: {msg_row['n']}.")
        if spend_row['n']:
            lines.append(f"Spending last 7 days: ${float(spend_row['total']):.2f} across {spend_row['n']} expenses.")
    except Exception as e:
        logger.error(f"Today context postgres error: {e}")
    return (
        "TODAY SO FAR:\n" + "\n".join(lines) +
        "\nThese are logs, not facts about what actually happened — an absent log means nothing was logged, not that nothing happened. "
        "Use this like someone who has actually been paying attention. Reference it naturally when relevant — don't recite stats."
    )

async def log_daily_event(event_type: str, value=None):
    """Insert a daily outcome, deduplicated by (date, event_type). A *_done event
    removes the matching *_skipped for the day (late confirmation overrides a
    conservative skip logged by the scheduler)."""
    today = datetime.now(LOCAL_TZ).date()
    try:
        conn = await asyncpg.connect(
            host=POSTGRES_HOST, port=5432, user="postgres",
            password=POSTGRES_PASSWORD, database="ghost_db"
        )
        await conn.execute("""
            INSERT INTO daily_events (date, event_type, value)
            VALUES ($1, $2, $3)
            ON CONFLICT (date, event_type) DO NOTHING
        """, today, event_type, value)
        if event_type.endswith("_done"):
            await conn.execute(
                "DELETE FROM daily_events WHERE date = $1 AND event_type = $2",
                today, event_type.replace("_done", "_skipped")
            )
        await conn.close()
        logger.info(f"Daily event logged: {event_type}")
    except Exception as e:
        logger.error(f"log_daily_event error: {e}")

async def get_pattern_context() -> str:
    """Summarise streaks in daily_events over the last 14 days for the system prompt."""
    try:
        conn = await asyncpg.connect(
            host=POSTGRES_HOST, port=5432, user="postgres",
            password=POSTGRES_PASSWORD, database="ghost_db"
        )
        rows = await conn.fetch(
            "SELECT date, event_type FROM daily_events WHERE date >= CURRENT_DATE - 14 ORDER BY date"
        )
        await conn.close()
    except Exception as e:
        logger.error(f"get_pattern_context error: {e}")
        return ""
    if not rows:
        return ""

    by_type = {}
    for row in rows:
        by_type.setdefault(row['event_type'], set()).add(row['date'])

    today = datetime.now(LOCAL_TZ).date()
    lines = []
    for event_type in sorted(by_type):
        dates = by_type[event_type]
        # "_skipped" rows are logged from ABSENCE of a confirmation — present them
        # as "not logged", never as a claim that the thing didn't happen
        label = event_type.replace("_skipped", " not logged").replace("_", " ")
        # current streak: consecutive days ending today (or yesterday if today not logged yet)
        d = today if today in dates else today - timedelta(days=1)
        streak = 0
        while d in dates:
            streak += 1
            d -= timedelta(days=1)
        if streak >= 3:
            lines.append(f"- {label}: {streak} days in a row (ongoing)")
            continue
        # recently broken streak: a run of 3+ that ended within the last 3 days
        broken = None
        run_len = 0
        prev = None
        for d in sorted(dates):
            run_len = run_len + 1 if prev == d - timedelta(days=1) else 1
            prev = d
            if run_len >= 3 and 0 < (today - d).days <= 3:
                broken = (run_len, d)
        if broken:
            lines.append(f"- {label}: was {broken[0]} days in a row until {broken[1].strftime('%A')}")
        elif len(dates) >= 3:
            lines.append(f"- {label}: {len(dates)} of the last 14 days")
    if not lines:
        return ""
    return (
        "PATTERNS (last 14 days, from tracked daily events):\n" + "\n".join(lines) +
        "\n'Not logged' streaks mean no confirmation reached you — they do not prove the thing didn't happen "
        "(she may have just stopped logging). Raise it as a question, not an accusation. "
        "Bring these up when relevant — a streak worth calling out, a slide worth naming. Don't recite the list."
    )

async def get_category_total(category: str) -> float:
    try:
        conn = await asyncpg.connect(
            host=POSTGRES_HOST, port=5432, user="postgres",
            password=POSTGRES_PASSWORD, database="ghost_db"
        )
        row = await conn.fetchrow("""
            SELECT COALESCE(SUM(amount), 0) as total
            FROM expenses
            WHERE category = $1
            AND DATE_TRUNC('month', logged_at) = DATE_TRUNC('month', NOW())
        """, category)
        await conn.close()
        return float(row['total'])
    except Exception as e:
        logger.error(f"Category total error: {e}")
        return 0.0

def parse_money(message: str):
    """Return a float amount ONLY when the message carries a real money signal.

    A bare number is never money: "you sent a message at 905" once became a $905
    expense because the old regex grabbed any digits. Requires an explicit
    currency marker ($ / dollars / bucks) or a spend verb attached to the number,
    and refuses clock times (9:05, "at 905") and dates.
    """
    import re
    text = message.replace(",", "")
    patterns = [
        r'\$\s?(\d+(?:\.\d{1,2})?)',                                             # $15, $15.50
        r'(\d+(?:\.\d{1,2})?)\s?(?:dollars|dollar|bucks|aud)\b',                 # 15 dollars
        r'\b(?:spent|spend|paid|pay|cost|costs|bought|charged|billed)\b[^.\d]{0,20}(\d+(?:\.\d{1,2})?)',
        r'(\d+(?:\.\d{1,2})?)\s?(?:on|for)\s+\w',                                # 15 on coffee
    ]
    for pat in patterns:
        for m in re.finditer(pat, text, re.IGNORECASE):
            start, end = m.span(1)
            # reject clock times: 9:05, 9.05am, "at 905"
            before = text[max(0, start - 4):start].lower()
            after = text[end:end + 3].lower()
            if ":" in before[-1:] or re.search(r'\bat\s$', before) or re.match(r'\s?(?:am|pm)\b', after):
                continue
            try:
                return float(m.group(1))
            except ValueError:
                continue
    return None

async def get_recent_expense_records(limit: int = 10) -> str:
    """Real recent expense/income rows as context for money QUESTIONS.

    Without this, a finance-triggered question with no parseable amount reached
    Ollama with zero data and it fabricated an answer (the 'trip to France').
    Stays on the local model — money data never leaves hivequeen.
    """
    try:
        conn = await asyncpg.connect(
            host=POSTGRES_HOST, port=5432, user="postgres",
            password=POSTGRES_PASSWORD, database="ghost_db"
        )
        exp = await conn.fetch(
            "SELECT amount, category, logged_at FROM expenses "
            "ORDER BY logged_at DESC LIMIT $1", limit)
        inc = await conn.fetch(
            "SELECT amount, source, logged_at FROM income ORDER BY logged_at DESC LIMIT 3")
        await conn.close()
    except Exception as e:
        logger.error(f"Recent expense records error: {e}")
        return "[No expense records available right now — say so, do not guess]"
    if not exp and not inc:
        return "[Expense records: NONE on file. There are no recorded expenses. Say exactly that.]"
    lines = []
    for r in exp:
        lines.append(f"${float(r['amount']):.2f} {r['category']} on "
                     f"{r['logged_at'].astimezone(LOCAL_TZ).strftime('%d %b')}")
    for r in inc:
        lines.append(f"income ${float(r['amount']):.2f} from {r['source']} on "
                     f"{r['logged_at'].astimezone(LOCAL_TZ).strftime('%d %b')}")
    return ("[These are the ONLY expense records on file — every expense Ghost knows about:\n"
            + "\n".join(lines)
            + "\nAnswer only from this list. If the answer isn't here, say you have no record of it.]")

# Known debt accounts mapped to Finance.md's five stages. Stage 1 (MPN loan) was
# cleared 14/07; the live ones are the credit card (2) and overdraft (3).
DEBT_ACCOUNTS = [
    (("mpn",), "MPN loan", 1),
    (("credit card", "creditcard", "credit-card", "cc balance"), "credit card", 2),
    (("overdraft",), "overdraft", 3),
]

async def log_debt(message: str):
    """If `message` is a debt-balance update (not a spend), upsert debt_accounts and
    return an Ollama context string. Returns None when it's not a debt update, so the
    caller falls through to normal expense logging."""
    import re
    low = message.lower()
    # a spend verb means this is an expense that happens to mention a card, not a debt update
    if any(v in low for v in ("spent", "bought", "purchase", "purchased")):
        return None
    matched = None
    for keys, name, stage in DEBT_ACCOUNTS:
        if any(k in low for k in keys):
            matched = (name, stage)
            break
    if not matched:
        return None
    # require a balance-declaration word so "paid the credit card bill" isn't a balance set
    if not any(w in low for w in ("balance", "owe", "owing", "debt", "down to",
                                  "is now", "paid off", "is at", "currently", "left on", "remaining")):
        return None
    balance = parse_money(message)
    if balance is None:
        return None
    name, stage = matched
    interest = None
    im = re.search(r'(\d+(?:\.\d+)?)\s*%', message)
    if im:
        interest = float(im.group(1))
    minrep = None
    mm = re.search(r'min(?:imum)?(?:\s+repayment)?(?:\s+of)?\s*\$?\s*(\d+(?:\.\d{1,2})?)', low)
    if mm:
        minrep = float(mm.group(1))
    try:
        conn = await asyncpg.connect(
            host=POSTGRES_HOST, port=5432, user="postgres",
            password=POSTGRES_PASSWORD, database="ghost_db")
        await conn.execute("""
            INSERT INTO debt_accounts (account_name, balance, interest_rate, min_repayment, stage_number, last_updated)
            VALUES ($1, $2, $3, $4, $5, NOW())
            ON CONFLICT (account_name) DO UPDATE SET
                balance = EXCLUDED.balance,
                interest_rate = COALESCE(EXCLUDED.interest_rate, debt_accounts.interest_rate),
                min_repayment = COALESCE(EXCLUDED.min_repayment, debt_accounts.min_repayment),
                stage_number = EXCLUDED.stage_number,
                last_updated = NOW()
        """, name, balance, interest, minrep, stage)
        await conn.close()
        logger.info(f"Debt updated: {name} (stage {stage}) balance ${balance:.2f}")
    except Exception as e:
        logger.error(f"log_debt error: {e}")
        return ""
    ctx = f"[Debt balance updated (NOT an expense): {name}, stage {stage}, now ${balance:.2f} owing"
    if interest is not None:
        ctx += f" at {interest:.2f}% interest"
    if minrep is not None:
        ctx += f", minimum ${minrep:.2f}/mo"
    ctx += ". Acknowledge the update in one dry line; do not treat it as money spent.]"
    return ctx

async def get_debt_status() -> str:
    """One line per active (balance>0) debt stage, for the budget summary. '' if none."""
    try:
        conn = await asyncpg.connect(
            host=POSTGRES_HOST, port=5432, user="postgres",
            password=POSTGRES_PASSWORD, database="ghost_db")
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

async def log_expense(message: str) -> str:
    """Parse expense from message and log to Postgres. Returns context string for Ollama."""
    amount = parse_money(message)
    if amount is None:
        return ""
    message_lower = message.lower()

    category_map = {
        'groceries': ['groceries', 'grocery', 'supermarket', 'coles', 'woolworths', 'aldi'],
        'eating out': ['cafe', 'coffee', 'restaurant', 'takeaway', 'takeout', 'lunch', 'dinner', 'breakfast', 'uber eats', 'doordash'],
        'transport': ['uber', 'taxi', 'train', 'tram', 'bus', 'petrol', 'fuel', 'parking', 'myki'],
        'subscriptions': ['netflix', 'spotify', 'subscription', 'membership'],
        'health': ['pharmacy', 'chemist', 'doctor', 'gym', 'physio', 'medical'],
        'shopping': ['clothes', 'clothing', 'shoes', 'amazon', 'online', 'shopping'],
    }

    category = 'other'
    for cat, keywords in category_map.items():
        if any(k in message_lower for k in keywords):
            category = cat
            break

    try:
        conn = await asyncpg.connect(
            host=POSTGRES_HOST, port=5432, user="postgres",
            password=POSTGRES_PASSWORD, database="ghost_db"
        )
        await conn.execute(
            "INSERT INTO expenses (amount, category, description) VALUES ($1, $2, $3)",
            amount, category, message
        )
        await conn.close()
        logger.info(f"Expense logged: ${amount} to {category}")

        monthly_total = await get_category_total(category)
        limit_row = None
        conn = await asyncpg.connect(
            host=POSTGRES_HOST, port=5432, user="postgres",
            password=POSTGRES_PASSWORD, database="ghost_db"
        )
        limit_row = await conn.fetchrow(
            "SELECT monthly_limit FROM budget_categories WHERE name = $1", category
        )
        await conn.close()
        limit = float(limit_row['monthly_limit']) if limit_row else None
        context = f"[Expense logged: ${amount:.2f} to '{category}'. Monthly total for {category}: ${monthly_total:.2f}"
        if limit:
            if monthly_total > limit:
                context += f" — OVER the ${limit:.2f} limit by ${monthly_total - limit:.2f}"
            elif monthly_total > 0.8 * limit:
                context += f" — close to the ${limit:.2f} limit, only ${limit - monthly_total:.2f} left"
            else:
                context += f" of ${limit:.2f} limit — comfortably under"
        context += "]"
        return context
    except Exception as e:
        logger.error(f"Log expense error: {e}")
        return ""

async def log_income(message: str) -> str:
    """Parse income from message and log to Postgres. Returns context string for Ollama."""
    import re
    amount = parse_money(message)
    if amount is None:
        return ""
    source_match = re.search(r'\bfrom\s+([a-zA-Z][a-zA-Z ]{1,40})', message, re.IGNORECASE)
    source = source_match.group(1).strip() if source_match else "unspecified"

    try:
        conn = await asyncpg.connect(
            host=POSTGRES_HOST, port=5432, user="postgres",
            password=POSTGRES_PASSWORD, database="ghost_db"
        )
        await conn.execute(
            "INSERT INTO income (amount, source) VALUES ($1, $2)",
            amount, source
        )
        row = await conn.fetchrow("""
            SELECT COALESCE(SUM(amount), 0) AS total
            FROM income
            WHERE DATE_TRUNC('month', logged_at) = DATE_TRUNC('month', NOW())
        """)
        await conn.close()
        logger.info(f"Income logged: ${amount} from {source}")
        return f"[Income logged: ${amount:.2f} from '{source}'. Total income this month: ${float(row['total']):.2f}]"
    except Exception as e:
        logger.error(f"Log income error: {e}")
        return ""

async def build_budget_summary() -> str:
    """Category / spent this month / limit / status, one line each."""
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

def strip_obsidian(text: str) -> str:
    """Remove frontmatter, wikilink brackets, and tags from Obsidian markdown."""
    import re
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            text = text[end + 4:]
    text = re.sub(r'\[\[(?:[^\]|]+\|)?([^\]]+)\]\]', r'\1', text)
    text = re.sub(r'(?<!\S)#[\w/-]+', '', text)
    return text

def search_vault(query: str) -> str:
    """Keyword search over vault markdown. Top 3 files by term frequency
    (filename hits weighted 5x), one ~500-char excerpt each."""
    import re
    stopwords = {
        "what", "did", "i", "write", "wrote", "about", "my", "notes", "note", "vault",
        "obsidian", "check", "the", "a", "an", "in", "on", "me", "you", "tell", "show",
        "find", "have", "has", "do", "does", "say", "says", "said", "for", "and", "is",
        "are", "was", "were", "of", "to", "there", "anything"
    }
    terms = [w for w in re.findall(r'[a-z0-9]+', query.lower()) if w not in stopwords and len(w) > 2]
    if not terms:
        return ""
    results = []
    for root, dirs, files in os.walk(VAULT_PATH):
        dirs[:] = [d for d in dirs if not d.startswith('.')]
        for fname in files:
            if not fname.endswith('.md'):
                continue
            try:
                with open(os.path.join(root, fname), encoding='utf-8', errors='ignore') as f:
                    text = f.read()
            except OSError:
                continue
            cleaned = strip_obsidian(text)
            lower = cleaned.lower()
            name_lower = fname.lower()
            score = sum(lower.count(t) for t in terms) + sum(5 for t in terms if t in name_lower)
            if score > 0:
                results.append((score, fname, cleaned, lower))
    if not results:
        return ""
    results.sort(key=lambda x: -x[0])
    excerpts = []
    for score, fname, cleaned, lower in results[:3]:
        pos = min((lower.find(t) for t in terms if t in lower), default=0)
        start = max(0, pos - 100)
        excerpt = " ".join(cleaned[start:start + 500].split())
        excerpts.append(f"--- {fname} ---\n{excerpt}")
    return "\n\n".join(excerpts)

def append_to_daily_note(content: str) -> str:
    """Append a timestamped line under '## Ghost' in today's daily note
    (Daily/YYYY-MM-DD.md, created if missing). Returns the note's relative path, or '' on failure."""
    now = datetime.now(LOCAL_TZ)
    day = now.strftime('%Y-%m-%d')
    line = f"- {now.strftime('%H:%M')} — {content}\n"
    try:
        daily_dir = os.path.join(VAULT_PATH, "Daily")
        os.makedirs(daily_dir, exist_ok=True)
        path = os.path.join(daily_dir, f"{day}.md")
        if os.path.exists(path):
            with open(path, encoding="utf-8", errors="ignore") as f:
                text = f.read()
            if "## Ghost" in text:
                idx = text.index("## Ghost")
                nxt = text.find("\n## ", idx + 1)
                if nxt == -1:
                    text = text.rstrip("\n") + "\n" + line
                else:
                    text = text[:nxt].rstrip("\n") + "\n" + line + text[nxt:]
            else:
                text = text.rstrip("\n") + f"\n\n## Ghost\n{line}"
        else:
            text = f"# {day}\n\n## Ghost\n{line}"
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return f"Daily/{day}.md"
    except OSError as e:
        logger.error(f"append_to_daily_note error: {e}")
        return ""

def read_vault_note(name_query: str):
    """Find the first .md note whose filename matches name_query (case-insensitive
    substring) and return (relative_path, content), or None."""
    q = name_query.lower().strip()
    if not q:
        return None
    for root, dirs, files in os.walk(VAULT_PATH):
        dirs[:] = [d for d in dirs if not d.startswith('.')]
        for fname in files:
            if fname.endswith(".md") and q in fname.lower():
                path = os.path.join(root, fname)
                try:
                    with open(path, encoding="utf-8", errors="ignore") as f:
                        return os.path.relpath(path, VAULT_PATH), f.read()
                except OSError as e:
                    logger.error(f"read_vault_note error: {e}")
                    return None
    return None

async def handle_cephalopod_handoff(user_message: str, source: str):
    """Send a document/content to Cephalopod via the ghost_handoffs table.
    DASHBOARD CHAT ONLY — never from Telegram (the user's call). Returns a reply if this
    was a handoff command, else None. Forms:
      'send to cephalopod: <text>'      -> hand off raw content
      'send <note name> to cephalopod'  -> read that vault note and hand it off"""
    if source != "dashboard":
        return None
    low = user_message.lower()
    if "cephalopod" not in low:
        return None
    import re
    m = re.search(r'(?:send|hand ?off|pass|give)\b.*?cephalopod\s*:\s*(.+)', user_message, re.I | re.S)
    if m:
        content = m.group(1).strip()
        if not content:
            return "Nothing after the colon to send. Try 'send to cephalopod: <text>'."
        ok = await write_handoff("doc", content, {"via": "dashboard"})
        return (f"Sent to Cephalopod — {len(content)} chars, kind 'doc'." if ok
                else "Couldn't reach Cephalopod's handoff table just now.")
    m2 = re.search(r'send\s+(.+?)\s+to\s+cephalopod', user_message, re.I)
    if m2:
        note = re.sub(r'\s+(note|doc|document|file)$', '', m2.group(1).strip().strip('"\''), flags=re.I).strip()
        result = read_vault_note(note)
        if not result:
            return f"No vault note matching '{note}'. Or use 'send to cephalopod: <text>' for raw content."
        relpath, content = result
        ok = await write_handoff("doc", content, {"via": "dashboard", "note": relpath})
        return (f"Sent '{relpath}' to Cephalopod — {len(content)} chars." if ok
                else "Couldn't reach Cephalopod's handoff table just now.")
    if any(t in low for t in ("send", "hand off", "handoff", "pass", "give")):
        return ("To hand something to Cephalopod: 'send <note name> to cephalopod', "
                "or 'send to cephalopod: <text>'.")
    return None

def append_to_note(name_query: str, content: str) -> str:
    """Append a timestamped Ghost line to the first note whose filename matches
    name_query (case-insensitive substring). Returns relative path, or '' if no match/failure."""
    q = name_query.lower().strip()
    if not q:
        return ""
    target = None
    for root, dirs, files in os.walk(VAULT_PATH):
        dirs[:] = [d for d in dirs if not d.startswith('.')]
        for fname in files:
            if fname.endswith(".md") and q in fname.lower():
                target = os.path.join(root, fname)
                break
        if target:
            break
    if not target:
        return ""
    now = datetime.now(LOCAL_TZ)
    try:
        with open(target, "a", encoding="utf-8") as f:
            f.write(f"\n- [Ghost {now.strftime('%Y-%m-%d %H:%M')}] {content}\n")
        return os.path.relpath(target, VAULT_PATH)
    except OSError as e:
        logger.error(f"append_to_note error: {e}")
        return ""

async def get_vault_context() -> str:
    """Always-on background context from the vault: recently modified notes in
    Projects/, Goals/, Areas/ — frontmatter + first 300 chars each, newest first,
    capped at ~1500 chars. Cached in Redis for 1h so it tracks Syncthing updates."""
    try:
        r = Redis(host=REDIS_HOST, port=6379, password=REDIS_PASSWORD, decode_responses=True)
        cached = await r.get("vault_context")
        await r.aclose()
        if cached is not None:
            return cached
    except Exception as e:
        logger.error(f"Vault context cache read error: {e}")

    import time
    entries = []
    for sub in ("Projects", "Goals", "Areas"):
        base = os.path.join(VAULT_PATH, sub)
        if not os.path.isdir(base):
            continue
        for root, dirs, files in os.walk(base):
            dirs[:] = [d for d in dirs if not d.startswith('.')]
            for fname in files:
                if not fname.endswith(".md"):
                    continue
                path = os.path.join(root, fname)
                try:
                    mtime = os.path.getmtime(path)
                    if time.time() - mtime > 30 * 86400:
                        continue
                    with open(path, encoding="utf-8", errors="ignore") as f:
                        text = f.read()
                except OSError:
                    continue
                frontmatter = ""
                if text.startswith("---"):
                    end = text.find("\n---", 3)
                    if end != -1:
                        fm_lines = [l.strip() for l in text[3:end].strip().splitlines() if ":" in l]
                        frontmatter = "; ".join(fm_lines)
                excerpt = " ".join(strip_obsidian(text)[:300].split())
                piece = f"[{fname[:-3]}] "
                if frontmatter:
                    piece += f"({frontmatter}) "
                piece += excerpt
                entries.append((mtime, piece))

    entries.sort(reverse=True)
    parts, total = [], 0
    for _, piece in entries:
        if total + len(piece) > 1500:
            break
        parts.append(piece)
        total += len(piece)

    context = ""
    if parts:
        context = (
            f"{USER_NAME.upper()}'S CURRENT CONTEXT (from her Obsidian vault — her active projects, goals and areas; "
            "background knowledge, reference naturally when relevant):\n" + "\n".join(parts)
        )
    try:
        r = Redis(host=REDIS_HOST, port=6379, password=REDIS_PASSWORD, decode_responses=True)
        await r.set("vault_context", context, ex=3600)
        await r.aclose()
    except Exception as e:
        logger.error(f"Vault context cache write error: {e}")
    return context

async def call_ollama(message: str) -> str:
    ollama_prompt = f"""You are Ghost, {USER_NAME}'s dry, blunt money tracker. One or two lines. No exclamation marks, no emoji, no praise.
Use ONLY facts in [brackets] or her message. You remember no trips, shops or purchases. Never invent a place, trip or number. If the answer isn't in [brackets], say you have no record of it. Never do your own maths, just relay the verdict. Don't repeat the bracket text word for word.

User: {message}
Ghost:"""
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                "http://10.42.0.1:11434/api/generate",
                json={
                    "model": "llama3.2",
                    "prompt": ollama_prompt,
                    "stream": False,
                    "keep_alive": "1h",
                    "options": {
                        "temperature": 0.7,
                        # 40 is plenty for "one or two lines" and halves generation
                        # time — llama3.2 generation degraded to ~1.8s/token as the
                        # host filled up, and 80 tokens was blowing the timeout.
                        "num_predict": 40
                    }
                },
                timeout=180.0
            )
            data = response.json()
            logger.info(f"Ollama response: {data}")
            return data.get("response", "").strip()
    except Exception as e:
        logger.error(f"Ollama error: {e}")
        return ""

async def call_claude_api(user_id: int, message: str, vault_context: str = "") -> str:
    history = await get_history(user_id)
    history.append({"role": "user", "content": message})

    if len(history) > 20:
        history = history[-20:]

    yesterday = await get_yesterday_summary()
    today_context = await get_today_context()
    pattern_context = await get_pattern_context()
    recent_phrases = await get_recent_phrases()
    vault_ctx = await get_vault_context()
    system_with_context = SYSTEM_PROMPT + "\n\n" + today_context
    if pattern_context:
        system_with_context += "\n\n" + pattern_context
    if vault_ctx:
        system_with_context += "\n\n" + vault_ctx
    if yesterday:
        system_with_context += "\n\n" + yesterday
    if recent_phrases:
        system_with_context += (
            "\n\nLINES YOU USED RECENTLY — do not reuse these jokes or phrasings, and do not reuse their SHAPE. "
            "If a recent line counted her actions, opened with 'Still' or 'Already', or leaned on the same "
            "sentence template, take a genuinely different angle — change the structure, not just the words. "
            "A joke can come back after a week or two, never same-day or next-day:\n"
            + "\n".join(f"- {p}" for p in recent_phrases)
        )
    if vault_context:
        system_with_context += (
            f"\n\nFROM OBSIDIAN VAULT ({USER_NAME} asked you to check her notes — these are excerpts from her own files. "
            "Reference what's relevant, quote sparingly, and say which note it came from):\n" + vault_context
        )

    headers = {
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json"
    }

    payload = {
        "model": "claude-sonnet-4-6",
        "max_tokens": 150,
        "system": system_with_context,
        "messages": history
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

    history.append({"role": "assistant", "content": reply})
    await save_history(user_id, history)
    await save_to_postgres(user_id, "user", message)
    await save_to_postgres(user_id, "assistant", reply)
    await save_phrase(reply)

    return reply

CEPHALOPOD_DB = "cephalopod_db"

async def write_handoff(kind: str, payload: str, meta: dict = None) -> bool:
    """Ghost's ONLY link to Cephalopod: deposit a job into ghost_handoffs, which lives
    in Cephalopod's separate database. Ghost writes as the postgres superuser and holds
    no other Cephalopod access; Cephalopod reads it and holds no Ghost access. Content
    (e.g. what to do with a blog transcript) is Cephalopod's concern, not Ghost's."""
    try:
        conn = await asyncpg.connect(
            host=POSTGRES_HOST, port=5432, user="postgres",
            password=POSTGRES_PASSWORD, database=CEPHALOPOD_DB)
        await conn.execute(
            "INSERT INTO ghost_handoffs (kind, payload, meta) VALUES ($1, $2, $3::jsonb)",
            kind, payload, json.dumps(meta) if meta else None)
        await conn.close()
        logger.info(f"Handoff written to Cephalopod: kind={kind}")
        return True
    except Exception as e:
        logger.error(f"write_handoff error: {e}")
        return False

async def log_message(source: str, msg_type: str, content: str):
    """Unified message log (session 15). source: telegram_in|telegram_out|scheduler|
    dashboard_in|dashboard_out. msg_type: chat|nudge|briefing|summary|alert. Written by
    the SURFACE layer (handle_message / handle_voice / dashboard endpoints), never by
    process_message, so source attribution stays correct for shared pipeline code."""
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

async def log_routing(source: str, financial: bool, model: str, response_time_ms: int):
    """One row per routed message, written at the routing decision point (not
    reconstructed from logs). source: telegram|dashboard. model: ollama|claude."""
    try:
        conn = await asyncpg.connect(
            host=POSTGRES_HOST, port=5432, user="postgres",
            password=POSTGRES_PASSWORD, database="ghost_db"
        )
        await conn.execute(
            "INSERT INTO routing_log (source, financial_trigger, model, response_time_ms) "
            "VALUES ($1, $2, $3, $4)", source, financial, model, response_time_ms)
        await conn.close()
    except Exception as e:
        logger.error(f"routing_log error: {e}")

async def call_claude(user_id: int, message: str, route_text: str = None) -> str:
    """`message` is what the model sees (the user's words plus any system [context]).
    `route_text` is what ROUTING and MONEY PARSING look at — the user's words only.

    These must be separate. Appended context is full of other people's words:
    her Habitica to-do "Get exact MPN loan balance" made "what are my dailies?"
    look financial and misrouted it to Ollama, and a "$" inside a context block
    could otherwise be parsed as a brand-new expense.
    """
    _t0 = time.monotonic()
    _source = _msg_source.get()

    async def _finish(resp: str, model: str, financial: bool) -> str:
        await log_routing(_source, financial, model, int((time.monotonic() - _t0) * 1000))
        return resp

    router_text = message if route_text is None else route_text
    finance_triggers = [
        "spent", "spend", "bought", "purchase", "cost", "price", "paid", "pay",
        "budget", "money", "dollar", "dollars", "cash", "bank", "account",
        "invoice", "receipt", "expense", "bill", "debt", "loan", "savings",
        "financial", "finance", "credit", "transfer", "withdraw", "deposit",
        "overdraft", "owe", "owing"
    ]

    income_triggers = [
        "got paid", "income", "salary", "payday", "paycheck", "pay cheque", "paid me"
    ]

    message_lower = router_text.lower()

    vault_triggers = ["obsidian", "my notes", "my vault", "what did i write", "check my notes", "character", "who is", "tell me about", "novel", "check my vault"]
    if any(t in message_lower for t in vault_triggers):
        logger.info("Vault query detected")
        vault_context = search_vault(router_text) or "No matching notes found in the vault."
        return await _finish(await call_claude_api(user_id, message, vault_context=vault_context), "claude", False)

    food_triggers = ["what have i eaten", "what did i eat", "my calories", "food log", "my macros", "my protein"]
    if any(t in message_lower for t in food_triggers):
        import fatsecret
        if fatsecret.is_configured():
            logger.info("Food query detected — fetching FatSecret")
            food = await get_fatsecret_summary()
            if food is None:
                food_ctx = "[FatSecret is connected but the fetch failed — say so plainly]"
            elif not food["entries"]:
                food_ctx = "[FatSecret: nothing logged today — that means no log, not necessarily no food]"
            else:
                food_ctx = (
                    f"[FatSecret today: {food['calories']:.0f} kcal, protein {food['protein']:.0f}g, "
                    f"carbs {food['carbs']:.0f}g, fat {food['fat']:.0f}g. "
                    f"Foods: {', '.join(food['foods'][:15]) if food['foods'] else 'unnamed entries'}]"
                )
            return await _finish(await call_claude_api(user_id, f"{message} {food_ctx}"), "claude", False)

    if "budget summary" in message_lower:
        logger.info("Budget summary requested")
        summary = await build_budget_summary()
        if not summary:
            return "Can't reach the budget data right now. Try again in a moment."
        commentary = await call_ollama(
            f"Here is {USER_NAME}'s budget for the month so far:\n{summary}\n\n"
            "Give exactly one dry line of commentary on the overall picture. "
            "Don't repeat the table — it's already being shown to her."
        )
        response = summary + ("\n\n" + commentary if commentary else "")
        history = await get_history(user_id)
        history.append({"role": "user", "content": message})
        history.append({"role": "assistant", "content": response})
        await save_history(user_id, history)
        await save_to_postgres(user_id, "user", message)
        await save_to_postgres(user_id, "assistant", response)
        return await _finish(response, "ollama", True)

    is_income = any(trigger in message_lower for trigger in income_triggers)
    is_financial = any(trigger in message_lower for trigger in finance_triggers)

    if is_income or is_financial:
        if is_income:
            logger.info("Income message detected — routing to Ollama")
            context = await log_income(router_text)
        else:
            logger.info("Financial message detected — routing to Ollama")
            context = await log_debt(router_text)
            if context is None:
                context = await log_expense(router_text)
        if not context:
            # Nothing loggable in this message. It's a question or just chatter that
            # tripped a money word — hand over the real records so Ollama answers from
            # data instead of inventing one.
            context = await get_recent_expense_records()
        ollama_message = message + "\n" + context if context else message
        response = await call_ollama(ollama_message)
        if response:
            history = await get_history(user_id)
            history.append({"role": "user", "content": message})
            history.append({"role": "assistant", "content": response})
            await save_history(user_id, history)
            await save_to_postgres(user_id, "user", message)
            await save_to_postgres(user_id, "assistant", response)
            return await _finish(response, "ollama", True)
        else:
            logger.warning("Ollama unavailable for financial message — not falling back to Claude")
            return await _finish("I can't process financial information right now. Try again in a moment.", "ollama", True)

    return await _finish(await call_claude_api(user_id, message), "claude", False)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Ghost online. I'm watching.")

SELFCODE_DIR = os.environ.get("SELFCODE_DIR", "/home/admin_hivequeen/ghost/selfcode")

async def handle_selfcode_command(user_message: str) -> str | None:
    """Approval gate for self-code proposals. The bot RECORDS intent only — it flips
    the proposal's status (the precondition selfcode.apply_change enforces) and hands
    back the exact host-side command. It never writes code or rebuilds from the pod.
    Returns a reply string if this was a self-code command, else None."""
    low = user_message.strip().lower()
    is_apply = "apply this change" in low
    is_undo = low.startswith("undo last change")
    is_reject = low in ("no", "reject", "cancel", "don't apply", "dont apply",
                        "do not apply", "reject this", "no thanks", "nope")
    if not (is_apply or is_undo or is_reject):
        return None
    try:
        conn = await asyncpg.connect(host=POSTGRES_HOST, port=5432, user="postgres",
                                     password=POSTGRES_PASSWORD, database="ghost_db")
        try:
            if is_undo:
                svc = low.replace("undo last change", "").strip() or None
                q = ("SELECT id, service FROM code_proposals WHERE status IN ('applied','applied_no_rebuild') "
                     "AND backup_path IS NOT NULL")
                row = (await conn.fetchrow(q + " AND service=$1 ORDER BY resolved_at DESC LIMIT 1", svc)
                       if svc else await conn.fetchrow(q + " ORDER BY resolved_at DESC LIMIT 1"))
                if not row:
                    return f"Nothing to undo{(' for ' + svc) if svc else ''}."
                return (f"Undo is a privileged action, so it runs on the host, not from my sandbox. "
                        f"Run this to restore {row['service']} and rebuild:\n"
                        f"python3 {SELFCODE_DIR}/selfcode.py undo {row['service']}")

            # apply / reject both act on the one open proposal
            row = await conn.fetchrow(
                "SELECT id, service, file_path FROM code_proposals WHERE status='proposed' ORDER BY id DESC LIMIT 1")
            if not row:
                return "No proposal is waiting. Nothing to apply or reject." if not is_reject else None
            if is_reject:
                await conn.execute("UPDATE code_proposals SET status='rejected', resolved_at=NOW() WHERE id=$1", row["id"])
                return f"Rejected proposal #{row['id']}. Nothing changed."
            # is_apply → record approval; the host applier acts on 'approved'
            await conn.execute("UPDATE code_proposals SET status='approved' WHERE id=$1", row["id"])
            fname = os.path.basename(row["file_path"])
            return (f"Approved proposal #{row['id']} ({row['service']}/{fname}). "
                    f"Applying is a privileged action so it runs on the host, not from my sandbox. "
                    f"Run:\npython3 {SELFCODE_DIR}/selfcode.py apply {row['id']}\n"
                    f"That backs up the file, writes the change, and rebuilds {row['service']}.")
        finally:
            await conn.close()
    except Exception as e:
        logger.error(f"Selfcode command error: {e}")
        return "Couldn't reach the proposals store just now. Try again in a moment."

SCROLL_SUPPRESS_KEY = "scroll_suppressed_until"
SCROLL_SUPPRESS_DEFAULT_MIN = 45
SCROLL_SUPPRESS_MAX_MIN = 180

async def handle_scroll_suppression(user_message: str) -> str | None:
    """Let the user mute scrolling-interrupt nudges for a bounded window when she's doing
    something that LOOKS like scrolling but isn't (messaging, researching, posting).
    Only the Tasker scrolling interrupts are paused — water/meal/movement and every
    other nudge keep firing. Auto-resumes when the window lapses; an explicit resume
    phrase clears it early. Returns a reply if this was a suppression command, else None."""
    import re as _re
    low = user_message.lower().strip()
    resume_triggers = ["resume scrolling", "unmute scrolling", "unpause scrolling",
                       "you can nudge me about scrolling", "done with my break",
                       "resume interrupts", "scrolling break over"]
    if any(t in low for t in resume_triggers):
        try:
            r = Redis(host=REDIS_HOST, port=6379, password=REDIS_PASSWORD, decode_responses=True)
            await r.delete(SCROLL_SUPPRESS_KEY)
            await r.aclose()
        except Exception as e:
            logger.error(f"Scroll resume error: {e}")
        return "Scrolling interrupts back on. I'll flag it if you sink into the feed."

    suppress_triggers = [
        "mute scrolling", "pause scrolling", "suppress scrolling", "hold scrolling",
        "no scrolling nudge", "no scrolling interrupt", "don't nudge me about scroll",
        "dont nudge me about scroll", "not scrolling", "isn't scrolling", "isnt scrolling",
        "looks like scrolling", "not doom", "actually working", "researching not",
        "messaging not", "posting not",
    ]
    if not any(t in low for t in suppress_triggers):
        return None

    minutes = SCROLL_SUPPRESS_DEFAULT_MIN
    m = _re.search(r'(\d+)\s*(hour|hr|hours|hrs)\b', low)
    m2 = _re.search(r'(\d+)\s*(min|mins|minute|minutes)\b', low)
    if m:
        minutes = int(m.group(1)) * 60
    elif m2:
        minutes = int(m2.group(1))
    elif "half an hour" in low or "half hour" in low:
        minutes = 30
    elif "an hour" in low or "one hour" in low:
        minutes = 60
    elif "couple hours" in low or "two hours" in low:
        minutes = 120
    minutes = max(5, min(minutes, SCROLL_SUPPRESS_MAX_MIN))

    reason = ""
    for word in ("messaging", "researching", "research", "posting", "working", "reading", "replying"):
        if word in low:
            reason = word
            break
    try:
        now_ts = datetime.now(LOCAL_TZ).timestamp()
        r = Redis(host=REDIS_HOST, port=6379, password=REDIS_PASSWORD, decode_responses=True)
        await r.set(SCROLL_SUPPRESS_KEY, now_ts + minutes * 60, ex=minutes * 60 + 30)
        await r.aclose()
    except Exception as e:
        logger.error(f"Scroll suppress error: {e}")
        return "Couldn't set that just now — try again in a moment."
    tail = f" while you're {reason}" if reason else ""
    return (f"Scrolling interrupts off for {minutes} minutes{tail}. Water, food and movement nudges still stand. "
            f"Say \"resume scrolling\" if you finish early.")

async def handle_location_override(user_message: str) -> str | None:
    """Task 3 (session 15): let the user override the office/WFH pattern for TODAY only.
    Same shape as the scroll-mute handler — bounded window (local midnight),
    auto-expires, no persistent state. Returns a reply if handled, else None."""
    low = user_message.lower().strip()
    if len(low) > 70:
        return None  # only short, direct statements — not mid-paragraph mentions
    wfh_phrases = ("i'm home today", "im home today", "i'm wfh today", "im wfh today",
                   "wfh today", "working from home today", "i'm working from home",
                   "im working from home")
    office_phrases = ("i'm at the office today", "im at the office today", "office today",
                      "i'm in the office", "im in the office", "at the office today",
                      "i'm at the office", "im at the office")
    loc = None
    if any(p in low for p in wfh_phrases):
        loc = "wfh"
    elif any(p in low for p in office_phrases):
        loc = "office"
    if loc is None:
        return None
    now_mel = datetime.now(LOCAL_TZ)
    ttl = int(((now_mel + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0) - now_mel).total_seconds())
    try:
        r = Redis(host=REDIS_HOST, port=6379, password=REDIS_PASSWORD, decode_responses=True)
        await r.set("location_override", loc, ex=ttl)
        await r.aclose()
    except Exception as e:
        logger.error(f"location override error: {e}")
        return "Couldn't note that just now — try again in a moment."
    if loc == "wfh":
        return "Noted — home today. Movement nudges run on the WFH cadence until midnight."
    return "Noted — office today. Movement nudges ease off until midnight; the commute counts for something."

async def handle_self_review_request(user_message: str, source: str) -> str | None:
    """Session 16, task 4: third entry point into the self-code pipeline. This only
    QUEUES a self-review request — the host-side runner does the analysis and then
    calls the existing propose_change(), so every safeguard (path allowlist at the
    proposal step, one-diff-at-a-time, exact-phrase Telegram approval, dashboard
    Apply) applies completely unchanged. The bot pod keeps zero code/cluster access."""
    low = user_message.lower().strip()
    triggers = ("review your outstanding", "review the outstanding list",
                "propose an improvement", "suggest an improvement to yourself",
                "self-review and propose", "self review and propose")
    if not any(t in low for t in triggers):
        return None
    try:
        conn = await asyncpg.connect(
            host=POSTGRES_HOST, port=5432, user="postgres",
            password=POSTGRES_PASSWORD, database="ghost_db")
        try:
            open_prop = await conn.fetchrow(
                "SELECT id FROM code_proposals WHERE status IN ('proposed','approved','apply_queued') LIMIT 1")
            if open_prop:
                return (f"Proposal #{open_prop['id']} is still open — one at a time is the rule. "
                        "Resolve it first, then ask me again.")
            pending = await conn.fetchrow("SELECT id FROM self_review_requests WHERE status='pending' LIMIT 1")
            if pending:
                return "Already reviewing — a self-review request is queued. Give it a minute."
            await conn.execute(
                "INSERT INTO self_review_requests (requested_via) VALUES ($1)", source)
        finally:
            await conn.close()
    except Exception as e:
        logger.error(f"self review queue error: {e}")
        return "Couldn't queue the self-review just now."
    return ("Queued. I'll go over my Outstanding list and draft ONE proposal through the normal "
            "pipeline — you'll get the diff on Telegram, and nothing applies without your "
            "\"apply this change\". Same gate as always.")

async def handle_voice_note_deletion(user_id: int, user_message: str) -> str | None:
    """Delete voice-note transcripts from Ghost's records on the user's explicit request
    (session 16 incident: Ghost claimed a deletion it couldn't do — now it can).
    Narrow by design: only '[voice]' telegram_in rows in message_log, scoped by an
    'about <topic>' keyword and/or a day word, capped at 10 matches per request.
    Also scrubs matching entries from the Redis conversation history so the
    transcripts stop feeding chat context. Reports exactly what was deleted."""
    import re
    low = user_message.lower()
    if len(user_message) > 160 or not re.search(
            r"\bdelete\b[^.?!]*\bvoice[ -]?notes?\b|\bvoice[ -]?notes?\b[^.?!]*\bdelete\b", low):
        return None
    m = re.search(r"about\s+([a-z0-9' ]{2,40})", low)
    topic = m.group(1).strip() if m else None
    if topic:
        topic = re.sub(r"\b(?:from|on)?\s*(?:yesterday|today)\b.*$", "", topic).strip() or None
    if not topic and "yesterday" not in low and "today" not in low:
        return ("Delete which voice notes? Scope it for me — 'delete the voice notes about X', "
                "optionally with 'from yesterday' or 'from today'.")
    conds = ["source = 'telegram_in'", "content LIKE '[voice]%'"]
    args = []
    if topic:
        args.append(f"%{topic}%")
        conds.append(f"content ILIKE ${len(args)}")
    if "yesterday" in low:
        conds.append(f"(ts AT TIME ZONE '{TZ_NAME}')::date = (now() AT TIME ZONE '{TZ_NAME}')::date - 1")
    elif "today" in low:
        conds.append(f"(ts AT TIME ZONE '{TZ_NAME}')::date = (now() AT TIME ZONE '{TZ_NAME}')::date")
    else:
        conds.append("ts > now() - interval '7 days'")
    try:
        conn = await asyncpg.connect(host=POSTGRES_HOST, port=5432, user="postgres",
                                     password=POSTGRES_PASSWORD, database="ghost_db")
        try:
            rows = await conn.fetch(
                f"SELECT id, content FROM message_log WHERE {' AND '.join(conds)} ORDER BY id", *args)
            if not rows:
                return ("No voice notes match that in my history — nothing deleted. "
                        "If they're older than a week, add 'about <topic>' and I'll search by topic alone.")
            if len(rows) > 10:
                return (f"That matches {len(rows)} voice notes — too broad to delete in one go. "
                        "Narrow it with 'about <topic>' or a day.")
            await conn.execute("DELETE FROM message_log WHERE id = ANY($1::int[])",
                               [r["id"] for r in rows])
        finally:
            await conn.close()
        scrubbed = 0
        if topic:
            history = await get_history(user_id)
            kept = [h for h in history if topic not in str(h.get("content", "")).lower()]
            scrubbed = len(history) - len(kept)
            if scrubbed:
                await save_history(user_id, kept)
        lines = "\n".join("- " + r["content"].replace("[voice] ", "")[:70] + "…" for r in rows)
        note = f" Also scrubbed {scrubbed} mentions from my short-term memory." if scrubbed else ""
        return (f"Deleted {len(rows)} voice note{'s' if len(rows) != 1 else ''} from my records:\n"
                f"{lines}\nGone from the history and the dashboard feed.{note}")
    except Exception as e:
        logger.error(f"Voice-note deletion error: {e}")
        return "Something broke mid-deletion — nothing may have been removed. Check with me again."

async def process_message(user_id: int, user_message: str, source: str = "telegram") -> str:
    """Full Ghost pipeline — confirmation flags, daily events, write commands, routing.
    Shared by the Telegram handler and the dashboard chat."""
    _msg_source.set(source)
    deletion_reply = await handle_voice_note_deletion(user_id, user_message)
    if deletion_reply is not None:
        await save_to_postgres(user_id, "user", user_message)
        await save_to_postgres(user_id, "assistant", deletion_reply)
        return deletion_reply
    self_review_reply = await handle_self_review_request(user_message, source)
    if self_review_reply is not None:
        await save_to_postgres(user_id, "user", user_message)
        await save_to_postgres(user_id, "assistant", self_review_reply)
        return self_review_reply
    location_reply = await handle_location_override(user_message)
    if location_reply is not None:
        await save_to_postgres(user_id, "user", user_message)
        await save_to_postgres(user_id, "assistant", location_reply)
        return location_reply
    cephalopod_reply = await handle_cephalopod_handoff(user_message, source)
    if cephalopod_reply is not None:
        await save_to_postgres(user_id, "user", user_message)
        await save_to_postgres(user_id, "assistant", cephalopod_reply)
        return cephalopod_reply

    scroll_reply = await handle_scroll_suppression(user_message)
    if scroll_reply is not None:
        await save_to_postgres(user_id, "user", user_message)
        await save_to_postgres(user_id, "assistant", scroll_reply)
        return scroll_reply

    selfcode_reply = await handle_selfcode_command(user_message)
    if selfcode_reply is not None:
        await save_to_postgres(user_id, "user", user_message)
        await save_to_postgres(user_id, "assistant", selfcode_reply)
        return selfcode_reply

    # Content-intent beats incidental keywords (session 16, task 2): an explicit
    # "blog post" / "for cephalopod" up front means the whole message is content —
    # route it to the blog pipeline before finance/food/anything else can grab it.
    # Messages WITHOUT a content-intent phrase are untouched: finance still
    # hard-routes to Ollama exactly as before.
    if has_content_intent(user_message, strict=True):
        logger.info("Content-intent detected — routing to blog pipeline ahead of keyword triggers")
        return await process_blog_note(user_id, user_message)

    # Typed FatSecret logging (session 17): the voice path could always write to
    # FatSecret, but typed messages had NO route there — which led Ghost to claim
    # the integration was "read-only" and then "try" a log it couldn't do
    # (incident 21/07, message_log 108-112). Narrow trigger: the word fatsecret
    # plus a log-verb routes through the SAME pipeline voice notes use.
    _low = user_message.lower()
    if "fatsecret" in _low and any(w in _low for w in ("log", "add", "record", "put")):
        import fatsecret
        if fatsecret.is_configured():
            logger.info("Typed FatSecret log request — routing to food pipeline")
            return await process_food_note(user_id, user_message)

    water_triggers = ["drank", "drunk", "water", "hydrated", "bottle", "1.5l", "500ml"]
    movement_triggers = ["walked", "moved", "exercise", "yoga", "gym", "weights", "run", "stretch"]
    drink_triggers = ["coffee", "tea", "juice", "smoothie", "drink", "drank", "water", "hydrated"]
    exercise_triggers = ["yoga", "gym", "weights", "workout", "worked out", "exercised", "went for a run"]
    message_lower_check = user_message.lower()
    now_mel = datetime.now(LOCAL_TZ)
    now_ts = now_mel.timestamp()

    seconds_to_midnight = int(((now_mel + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0) - now_mel).total_seconds())
    water_count = 0
    try:
        r = Redis(host=REDIS_HOST, port=6379, password=REDIS_PASSWORD, decode_responses=True)
        if any(t in message_lower_check for t in water_triggers):
            await r.set("last_water_confirmed", now_ts, ex=86400)
            water_count = await r.incr("water_confirm_count")
            await r.expire("water_confirm_count", seconds_to_midnight)
        if any(t in message_lower_check for t in movement_triggers):
            await r.set("last_movement_confirmed", now_ts, ex=86400)
        if any(t in message_lower_check for t in drink_triggers):
            await r.set("last_drink_mentioned", now_ts, ex=7200)
        if any(t in message_lower_check for t in exercise_triggers):
            await r.set("exercise_confirmed", "1", ex=seconds_to_midnight)
        await r.aclose()
    except Exception as e:
        logger.error(f"Redis confirm flags error: {e}")

    # daily_events for multi-day pattern tracking
    if any(t in message_lower_check for t in exercise_triggers):
        await log_daily_event("exercise_done")
    if water_count >= 3:  # 3x 500ml bottles = 1.5L daily target
        await log_daily_event("water_target_hit")
    skip_words = ["skipped", "skipping", "didn't", "did not", "no time for"]
    eat_words = ["ate", "had", "having", "eating", "eaten", "finished", "grabbed", "made"]
    for meal in ("breakfast", "lunch", "dinner"):
        if meal in message_lower_check:
            if any(w in message_lower_check for w in skip_words):
                await log_daily_event(f"{meal}_skipped")
            elif any(w in message_lower_check for w in eat_words):
                await log_daily_event(f"{meal}_done")

    # Obsidian write commands
    vault_write_ctx = None
    try:
        import re as _re
        note_match = _re.search(r'add to note\s+([^:]+):\s*(.+)', user_message, _re.IGNORECASE | _re.DOTALL)
        daily_match = None
        for trig in ("note that", "log this", "write to my notes", "add to obsidian"):
            idx = message_lower_check.find(trig)
            if idx != -1:
                daily_match = user_message[idx + len(trig):].strip(" :,-.!?")
                break
        if note_match:
            path = append_to_note(note_match.group(1), note_match.group(2).strip())
            vault_write_ctx = f"[Written to vault note {path}]" if path else f"[No vault note matching '{note_match.group(1).strip()}' found]"
        elif daily_match:
            path = append_to_daily_note(daily_match)
            vault_write_ctx = f"[Noted in {path}]" if path else "[Vault write failed]"
    except Exception as e:
        logger.error(f"Vault write error: {e}")

    # Stage-cascade unlocks (vault Goals specs): "finance stage 1 complete" → create stage 2 items.
    # Explicit trigger by design — stage completion is a real-world milestone (loan paid off),
    # not something Ghost can infer from Habitica state.
    cascade_ctx = None
    try:
        import re as _re
        cm = _re.search(r'\b(finance|creative)\s+stage\s+(\d)\s+(complete|done|finished)\b', message_lower_check)
        if cm:
            cascade, done_stage = cm.group(1), int(cm.group(2))
            next_stage = done_stage + 1
            stages = CASCADE_STAGES.get(cascade, {})
            if next_stage not in stages:
                cascade_ctx = f"[No stage {next_stage} defined for {cascade} — {cascade} cascade ends at stage {max(stages) if stages else done_stage}]"
            else:
                r = Redis(host=REDIS_HOST, port=6379, password=REDIS_PASSWORD, decode_responses=True)
                unlocked = int(await r.get(f"cascade_stage:{cascade}") or 1)
                if next_stage <= unlocked:
                    await r.aclose()
                    cascade_ctx = f"[{cascade} stage {next_stage} was already unlocked — nothing new created]"
                else:
                    from habitica import create_todo, create_daily, create_habit
                    spec = stages[next_stage]
                    made = []
                    for text in spec.get("todos", []):
                        if await create_todo(text):
                            made.append(f"todo '{text}'")
                    for text, freq in spec.get("dailies", []):
                        if await create_daily(text, freq):
                            made.append(f"daily '{text}'")
                    for text in spec.get("habits", []):
                        if await create_habit(text):
                            made.append(f"habit '{text}'")
                    await r.set(f"cascade_stage:{cascade}", next_stage)
                    await r.aclose()
                    cascade_ctx = f"[{cascade} stage {done_stage} marked complete. Stage {next_stage} unlocked in Habitica: {'; '.join(made) if made else 'creation failed — check logs'}]"
    except Exception as e:
        logger.error(f"Cascade unlock error: {e}")

    # Natural-language task capture → Habitica todo ("add X to my list", "remind me to X",
    # short messages starting "need to X"). Badge inferred only when phrasing is explicit.
    task_capture_ctx = None
    try:
        import re as _re
        captured = None
        m_list = _re.search(r'\badd (.+?) to (?:my|the) list\b', user_message, _re.IGNORECASE | _re.DOTALL)
        m_remind = _re.match(r'\s*remind me to (.+)', user_message, _re.IGNORECASE | _re.DOTALL)
        m_need = _re.match(r"\s*(?:i )?need to (.+)", user_message, _re.IGNORECASE | _re.DOTALL)
        if m_list:
            captured = m_list.group(1)
        elif m_remind:
            captured = m_remind.group(1)
        elif m_need and len(user_message) <= 80:
            # "need to" is common in venting — only treat short, imperative-shaped messages as tasks
            captured = m_need.group(1)
        if captured:
            badge = None
            low = user_message.lower()
            if "urgent" in low or "today" in low:
                badge = "focus"
            elif "when i get paid" in low or "payday" in low or "when i'm paid" in low:
                badge = "payday"
            task_text = _re.sub(r"[,;]?\s*(it's urgent|urgent|today|when i get paid|when i'm paid|on payday)\s*$",
                                "", captured.strip(" .,!?"), flags=_re.IGNORECASE).strip(" .,!?")
            if task_text:
                from habitica import create_todo, get_or_create_tag
                tag_ids = []
                if badge:
                    tag_id = await get_or_create_tag(badge)
                    if tag_id:
                        tag_ids.append(tag_id)
                ok = await create_todo(task_text, tags=tag_ids or None)
                if ok:
                    task_capture_ctx = (f"[Habitica todo '{task_text}' created, tagged '{badge}']" if badge
                                        else f"[Habitica todo '{task_text}' created, no badge tag — none implied]")
                else:
                    task_capture_ctx = f"[Habitica todo creation failed for '{task_text}']"
    except Exception as e:
        logger.error(f"Task capture error: {e}")

    # Habitica write commands ("scored" / "habit up|down" score a habit; "ticked" stays with dailies below)
    habitica_ctx = None
    habitica_read_triggers = ["my dailies", "pending dailies", "habitica", "my tasks", "my todos", "my to-dos", "what's due", "whats due", "my habits", "checklist"]
    if any(t in user_message.lower() for t in habitica_read_triggers):
        try:
            from habitica import get_dailies, get_todos, get_habits, get_checklists
            dailies = await get_dailies()
            todos = await get_todos()
            habits = await get_habits()
            checklists = await get_checklists()
            checklist_bits = []
            for bucket_label, bucket in (("daily", checklists["dailies"]), ("todo", checklists["todos"])):
                for task_text, items in bucket.items():
                    rendered = ", ".join(f"{t}{' ✓' if done else ''}" for t, done in items)
                    checklist_bits.append(f"{task_text} ({bucket_label}): {rendered}")
            checklist_part = ("Checklists inside tasks: " + " | ".join(checklist_bits) + ". ") if checklist_bits else ""
            habitica_ctx = (
                f"[Habitica data: Pending dailies: {', '.join(dailies['pending']) if dailies['pending'] else 'none'}. "
                f"Done: {', '.join(dailies['done']) if dailies['done'] else 'none'}. "
                f"Todos: {', '.join(todos[:5]) if todos else 'none'}. "
                f"Habits: {', '.join(habits) if habits else 'none'}. "
                f"{checklist_part}]"
            )
        except Exception as e:
            logger.error(f"Habitica read error: {e}")

    try:
        m = message_lower_check
        def _after(trigger):
            return user_message[m.find(trigger) + len(trigger):].strip(" :,-.!?")
        if "add to habitica" in m or m.startswith("add task"):
            from habitica import create_todo
            text = _after("add to habitica") if "add to habitica" in m else _after("add task")
            if text:
                ok = await create_todo(text)
                habitica_ctx = f"[Habitica to-do '{text}' created]" if ok else "[Habitica to-do creation failed]"
        elif "create daily" in m:
            from habitica import create_daily
            text = _after("create daily")
            if text:
                ok = await create_daily(text)
                habitica_ctx = f"[Habitica daily '{text}' created]" if ok else "[Habitica daily creation failed]"
        elif "new habit" in m:
            from habitica import create_habit
            text = _after("new habit")
            if text:
                ok = await create_habit(text)
                habitica_ctx = f"[Habitica habit '{text}' created]" if ok else "[Habitica habit creation failed]"
        elif "habit up" in m or "habit down" in m or m.startswith("scored"):
            from habitica import log_habit
            if "habit down" in m:
                direction, name = "down", _after("habit down")
            elif "habit up" in m:
                direction, name = "up", _after("habit up")
            else:
                direction, name = "up", _after("scored")
            if name:
                ok = await log_habit(name, direction)
                habitica_ctx = f"[Habit '{name}' scored {direction}]" if ok else f"[No habit matching '{name}' found]"
    except Exception as e:
        logger.error(f"Habitica write error: {e}")

    completion_triggers = ["mark", "done", "finished", "complete", "ticked", "tick off", "mark off"]
    message_lower = user_message.lower()

    if cascade_ctx:
        return await call_claude(user_id, f"{user_message} {cascade_ctx}", route_text=user_message)
    if vault_write_ctx:
        return await call_claude(user_id, f"{user_message} {vault_write_ctx}", route_text=user_message)
    if task_capture_ctx:
        return await call_claude(user_id, f"{user_message} {task_capture_ctx} [Confirm in one line: what was added and how it's tagged.]", route_text=user_message)
    if habitica_ctx:
        return await call_claude(user_id, f"{user_message} {habitica_ctx} Please list this data clearly for {USER_NAME}.", route_text=user_message)
    if any(trigger in message_lower for trigger in completion_triggers):
        try:
            from habitica import complete_task, get_dailies
            dailies = await get_dailies()
            all_tasks = dailies["done"] + dailies["pending"]

            matched_task = None
            for task in all_tasks:
                if task.lower() in message_lower:
                    matched_task = task
                    break

            if matched_task:
                success = await complete_task(matched_task)
                if success:
                    return await call_claude(user_id, f"{user_message} [Habitica task '{matched_task}' marked complete successfully]", route_text=user_message)
                return await call_claude(user_id, f"{user_message} [Habitica task found but failed to mark complete]", route_text=user_message)
            return await call_claude(user_id, user_message)
        except Exception as e:
            logger.error(f"Habitica error: {e}")
            return await call_claude(user_id, user_message)
    return await call_claude(user_id, user_message)

# --- Voice notes: transcribe locally (faster-whisper, base/int8) and route by intent ---
# Load-on-demand: the model is loaded when a voice note arrives and unloaded after
# WHISPER_IDLE_SECONDS of no voice notes, so it isn't holding ~380Mi resident (which
# was squeezing Ollama's RAM and slowing token generation). Cost: a one-off cold-start
# reload (~13s) on the first voice note after an idle stretch.
import gc
import threading
_whisper_model = None
_whisper_last_used = 0.0
_whisper_lock = threading.Lock()
WHISPER_IDLE_SECONDS = 300  # unload 5 min after the last voice note

# Whisper confidence gates. avg_logprob is per-token log likelihood (0 = perfect,
# below about -1.0 means the model was guessing); no_speech_prob is how likely the
# audio is not speech at all. Acting on a garbled transcript is how invented
# content gets in, so below these we ask for a repeat instead.
WHISPER_MIN_LOGPROB = -1.0
WHISPER_MAX_NO_SPEECH = 0.6

def _maybe_unload_whisper():
    """Drop the model + free memory if it's been idle. Runs on a background loop.
    Non-blocking on the lock so it never stalls a transcription in progress."""
    global _whisper_model
    if _whisper_model is None or (time.time() - _whisper_last_used) < WHISPER_IDLE_SECONDS:
        return
    if _whisper_lock.acquire(blocking=False):
        try:
            if _whisper_model is not None and (time.time() - _whisper_last_used) >= WHISPER_IDLE_SECONDS:
                _whisper_model = None
                gc.collect()
                # CTranslate2 frees its C++ buffers, but glibc keeps them in its arena
                # rather than returning to the OS — malloc_trim pushes them back so the
                # RAM is actually available to Ollama.
                try:
                    import ctypes
                    ctypes.CDLL("libc.so.6").malloc_trim(0)
                except Exception:
                    pass
                logger.info(f"Whisper unloaded after {WHISPER_IDLE_SECONDS}s idle — RAM freed")
        finally:
            _whisper_lock.release()

def transcribe_audio(path: str) -> tuple:
    """Blocking Whisper transcription — call via asyncio.to_thread.
    Returns (text, confident: bool, detail: str). Loads the model on demand and
    holds the lock across load+transcribe so the idle unloader can't null it mid-use."""
    global _whisper_model, _whisper_last_used
    from faster_whisper import WhisperModel
    with _whisper_lock:
        if _whisper_model is None:
            logger.info("Loading Whisper model on demand (cold start)...")
            _whisper_model = WhisperModel("base", device="cpu", compute_type="int8")
        segments, _info = _whisper_model.transcribe(path, language="en", vad_filter=True)
        # segments is a lazy generator — consuming it here (inside the lock) is where
        # the model actually runs, so the unloader can't drop the model mid-transcription.
        segs = list(segments)
        _whisper_last_used = time.time()
    text = " ".join(s.text.strip() for s in segs).strip()
    if not segs:
        return text, False, "no speech segments"
    # weight by segment length so one short mumble doesn't sink a good long note
    total = sum(max(s.end - s.start, 0.01) for s in segs)
    avg_logprob = sum(s.avg_logprob * max(s.end - s.start, 0.01) for s in segs) / total
    worst_no_speech = max(s.no_speech_prob for s in segs)
    confident = avg_logprob >= WHISPER_MIN_LOGPROB and worst_no_speech <= WHISPER_MAX_NO_SPEECH
    detail = f"avg_logprob={avg_logprob:.2f} no_speech={worst_no_speech:.2f}"
    return text, confident, detail

async def extract_food_items(transcript: str) -> list:
    """Pull searchable food names out of a spoken sentence via Claude; regex fallback."""
    try:
        async with httpx.AsyncClient() as client:
            r = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={"x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01",
                         "content-type": "application/json"},
                json={"model": "claude-sonnet-4-6", "max_tokens": 100,
                      "system": ("Extract the food/drink items from the message. Reply with ONLY a JSON "
                                 "array of short, searchable food names, e.g. [\"chicken wrap\", \"flat white\"]. "
                                 "No other text."),
                      "messages": [{"role": "user", "content": transcript}]},
                timeout=30.0)
            items = json.loads(r.json()["content"][0]["text"].strip())
            if isinstance(items, list):
                return [str(i).strip() for i in items if str(i).strip()][:3]
    except Exception as e:
        logger.error(f"Food extraction error: {e}")
    t = transcript.lower()
    for p in FOOD_VOICE_PATTERNS:
        idx = t.find(p)
        if idx != -1:
            rest = transcript[idx + len(p):].strip(" .,!?")
            if rest:
                return [rest]
    return [transcript.strip()]

async def process_food_note(user_id: int, transcript: str) -> str:
    """Voice-note food logging: extract items, log to FatSecret, confirm in one line."""
    import fatsecret
    if not fatsecret.is_configured():
        return await process_message(user_id, transcript)
    t = transcript.lower()
    hour = datetime.now(LOCAL_TZ).hour
    meal = "breakfast" if hour < 11 else "lunch" if hour < 16 else "dinner" if hour < 21 else "other"
    for m in ("breakfast", "lunch", "dinner"):
        if m in t:
            meal = m
            break
    if "snack" in t:
        meal = "other"
    items = await extract_food_items(transcript)
    logged, failed = [], []
    for item in items:
        result = await fatsecret.log_food(item, meal)
        if result:
            logged.append(f"{result['name']} ({result['serving']}, {result['calories']:.0f} kcal)")
        else:
            failed.append(item)
    if logged:
        if meal in ("breakfast", "lunch", "dinner"):
            await log_daily_event(f"{meal}_done")
        try:
            r = Redis(host=REDIS_HOST, port=6379, password=REDIS_PASSWORD, decode_responses=True)
            await r.delete("fatsecret_today")
            await r.aclose()
        except Exception:
            pass
        ctx = f"[Voice note. Logged to FatSecret as {meal}: {', '.join(logged)}"
        if failed:
            ctx += f". No match found for: {', '.join(failed)} — tell her that part needs manual logging"
        ctx += ". Confirm in one line — say what was logged so she can spot a wrong match.]"
    else:
        ctx = f"[Voice note. FatSecret matched nothing for: {', '.join(failed)}. Tell her to log it manually or rephrase.]"
    return await call_claude(user_id, f"{transcript} {ctx}", route_text=transcript)

BLOG_EDIT_PROMPT = f"""You are editing a voice-memo transcript into a blog draft for the user, following her voice-to-blog framework exactly.

Rules:
- STAY LITERAL TO THE TRANSCRIPT. This is editing, not writing. Every fact, event, place, date, person, anecdote and opinion in the draft must come from what she actually said. Do NOT invent narrative detail to make it flow — no scenes she didn't describe, no specifics she didn't give, no invented examples. Sharpening her phrasing is the job; adding content is not. If a section feels thin, leave it thin and flag it rather than filling it in.
- Voice memos bury the lede. Hunt for the actual argument or story instead of assuming the opening is the hook. Cut repetition and throat-clearing; where she said the same thing three ways, keep the best phrasing.
- Structure: hook, body, close, shaped by what she actually said. No forced formula.
- Her voice: plain and direct. NO em dashes anywhere (use commas or periods). No AI-sounding filler, no decorative formatting, no listicles unless she spoke in a list. Keep phrases she clearly liked close to verbatim.
- Brand: default {BRAND_PRIMARY} ({BRAND_DOMAIN}). Choose {BRAND_SECONDARY} only if it's clearly about the {BRAND_SECONDARY} business or products.
- Length: default 700-1000 words, shorter or longer only if the material clearly demands it.
- Flag EVERY place you inferred meaning, filled a gap, or guessed her intent. She reviews these before publishing.

Output EXACTLY this format:
TITLE: <title>
ALT_TITLES: <alt 1> | <alt 2> | <alt 3>
BRAND: <{BRAND_PRIMARY} OR {BRAND_SECONDARY}>
FORMAT: <Personal essay OR Ritual/how-to OR Product story OR Opinion>
FLAGGED:
- <one inference/gap per line, or exactly "- none">
---
<the post body in plain markdown, starting with the first paragraph, no title repeat>"""

async def draft_blog_post(transcript: str) -> dict | None:
    """Edit a rambling transcript into a structured draft. Returns parsed fields or None."""
    try:
        async with httpx.AsyncClient() as client:
            r = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={"x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01",
                         "content-type": "application/json"},
                json={"model": "claude-sonnet-4-6", "max_tokens": 3000,
                      "system": BLOG_EDIT_PROMPT,
                      "messages": [{"role": "user", "content": f"Transcript:\n\n{transcript}"}]},
                timeout=120.0)
            text = r.json()["content"][0]["text"]
        header, _, body = text.partition("\n---\n")
        draft = {"title": "", "alts": [], "brand": BRAND_PRIMARY,
                 "format": "Personal essay", "flags": [], "body": body.strip()}
        in_flags = False
        for line in header.splitlines():
            line = line.strip()
            if line.startswith("TITLE:"):
                draft["title"] = line[6:].strip()
            elif line.startswith("ALT_TITLES:"):
                draft["alts"] = [a.strip() for a in line[11:].split("|") if a.strip()]
            elif line.startswith("BRAND:"):
                draft["brand"] = line[6:].strip()
            elif line.startswith("FORMAT:"):
                draft["format"] = line[7:].strip()
            elif line.startswith("FLAGGED:"):
                in_flags = True
            elif in_flags and line.startswith("- "):
                flag = line[2:].strip()
                if flag.lower() != "none":
                    draft["flags"].append(flag)
        if not draft["title"] or not draft["body"]:
            logger.error(f"Blog draft parse failure — header was: {header[:200]}")
            return None
        return draft
    except Exception as e:
        logger.error(f"Blog draft error: {e}")
        return None

def write_blog_draft(draft: dict, transcript: str) -> str:
    """Write the draft into Posts/ following the existing file conventions.
    Returns the vault-relative path, or '' on failure."""
    import re
    try:
        posts_dir = os.path.join(VAULT_PATH, "Projects", BRAND_VAULT_DIR, "Posts")
        os.makedirs(posts_dir, exist_ok=True)
        safe = re.sub(r'[/\\:*?"<>|#^\[\]]', "", draft["title"]).strip() or "Untitled draft"
        path = os.path.join(posts_dir, f"{safe}.md")
        n = 2
        while os.path.exists(path):
            path = os.path.join(posts_dir, f"{safe} ({n}).md")
            n += 1
        site = BRAND_DOMAIN if draft["brand"] == BRAND_PRIMARY else f"{BRAND_SECONDARY} (Shopify)"
        parts = [
            "---", "tags:", "  - cBusiness", "  - cCreative",
            "status: Drafted", f"format: {draft['format']}", f"site: {site}", "---", "",
            f"# {draft['title']}", "", draft["body"], "", "---", "",
            "## Alt titles", *[f"- {a}" for a in draft["alts"]], "",
            "## Flagged by Ghost (inferred or gap-filled — check before publishing)",
            *([f"- {f}" for f in draft["flags"]] or ["- none"]), "",
        ]
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(parts))
        return os.path.relpath(path, VAULT_PATH)
    except OSError as e:
        logger.error(f"Blog write error: {e}")
        return ""

def update_blog_index(title: str, fmt: str, brand: str) -> bool:
    """Add a Drafted row to the right table in blog-index.md."""
    import re
    try:
        index_path = os.path.join(VAULT_PATH, "Projects", BRAND_VAULT_DIR, "Blogs", "blog-index.md")
        lines = open(index_path, encoding="utf-8").read().splitlines()
        section = f"## {BRAND_PRIMARY}" if brand == BRAND_PRIMARY else f"## {BRAND_SECONDARY}"
        start = next(i for i, l in enumerate(lines) if l.startswith(section))
        end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
        rows = [i for i in range(start, end) if re.match(r"^\|\s*\d+\s*\|", lines[i])]
        if rows:
            insert_at = rows[-1] + 1
            next_num = int(lines[rows[-1]].split("|")[1].strip()) + 1
        else:
            sep = next((i for i in range(start, end) if re.match(r"^\|[-\s|]+\|$", lines[i].replace(" ", ""))), None)
            if sep is None:
                return False
            insert_at = sep + 1
            next_num = 1
        today = datetime.now(LOCAL_TZ).strftime("%Y-%m-%d")
        lines.insert(insert_at, f"| {next_num} | [[{title}]] | Drafted | {fmt} | {today} | From voice note via Ghost |")
        open(index_path, "w", encoding="utf-8").write("\n".join(lines) + "\n")
        return True
    except Exception as e:
        logger.error(f"Blog index update error: {e}")
        return False

async def process_blog_note(user_id: int, transcript: str) -> str:
    """Blog voice note → edited draft in the vault → index row → confirmation with flags."""
    draft = await draft_blog_post(transcript)
    if not draft:
        return "Couldn't shape that into a draft — the editing call failed. Your words aren't lost; send it again in a minute."
    rel_path = write_blog_draft(draft, transcript)
    if not rel_path:
        return "Drafted it but couldn't write to the vault — check the mount. Try again."
    index_ok = update_blog_index(draft["title"], draft["format"], draft["brand"])
    confirmation_lines = [
        f"Drafted: \"{draft['title']}\" → {rel_path}",
        f"Brand: {draft['brand']} | Format: {draft['format']}",
    ]
    if draft["alts"]:
        confirmation_lines.append("Alt titles: " + " / ".join(draft["alts"]))
    confirmation_lines.append("Index updated to Drafted." if index_ok
                              else "Couldn't update blog-index.md — add the row yourself.")
    if draft["flags"]:
        confirmation_lines.append("I had to infer or fill these — check them before publishing:")
        confirmation_lines.extend(f"• {f}" for f in draft["flags"])
    else:
        confirmation_lines.append("Nothing inferred — it's all from your words.")
    confirmation_lines.append("It's a draft for your review. Nothing publishes itself.")
    confirmation = "\n".join(confirmation_lines)
    await save_to_postgres(user_id, "user", f"[voice note, blog] {transcript}")
    await save_to_postgres(user_id, "assistant", confirmation)
    return confirmation

# Content-intent signals (session 16, task 2). When one of these appears up front,
# the message IS content — it routes to the blog pipeline no matter what financial or
# food keywords appear later in the ramble ("spent way too much money on candles" as
# part of a story must not become an Ollama expense). "This is for cephalopod" spoken
# aloud routes to the SAME blog pipeline — it does NOT touch ghost_handoffs; the
# dashboard-only handoff restriction from session 13 is deliberate and stands.
BLOG_VOICE_TRIGGERS = ("blog note", "blog post", "blog idea", "new blog",
                       "for cephalopod", "cephalopod post", "this is a blog", "content note")
CONTENT_INTENT_WINDOW = 120  # she states intent up front; don't match mid-ramble mentions
FOOD_VOICE_PATTERNS = (
    "just had", "just ate", "i had", "i ate", "i'm having", "im having", "having a", "eating",
    "for breakfast", "for lunch", "for dinner", "snacked on", "snacking on"
)

def has_content_intent(text: str, strict: bool = False) -> bool:
    """strict=False (voice): any content trigger in the opening window — voice rambles
    state intent up front and this preserves the pre-existing voice behaviour.
    strict=True (typed text): the message must OPEN with the intent (after common
    spoken/typed filler), so "I read a blog post about budgeting yesterday" doesn't
    hijack a chat message into draft-creation."""
    import re as _re
    t = text.lower().strip()
    if not any(trig in t[:CONTENT_INTENT_WINDOW] for trig in BLOG_VOICE_TRIGGERS):
        return False
    if not strict:
        return True
    cleaned = _re.sub(r"^(?:(?:ok(?:ay)?|so|um+|uh+|right|hey|hi|hello|alright|good (?:morning|afternoon|evening))[\s,.!-]+)+", "", t)
    starters = ("blog", "new blog", "content note", "this is", "for cephalopod", "cephalopod")
    return cleaned.startswith(starters)

def voice_intent(transcript: str) -> str:
    if has_content_intent(transcript):
        return "blog"
    t = transcript.lower()
    if any(p in t for p in FOOD_VOICE_PATTERNS):
        return "food"
    return "generic"

async def process_voice(user_id: int, transcript: str) -> str:
    intent = voice_intent(transcript)
    logger.info(f"Voice note intent: {intent} — {transcript[:80]}")
    if intent == "blog":
        return await process_blog_note(user_id, transcript)
    if intent == "food":
        return await process_food_note(user_id, transcript)
    # generic voice → the normal pipeline (finance still routes to Ollama there)
    return await process_message(user_id, transcript)

async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        voice = update.message.voice
        tg_file = await context.bot.get_file(voice.file_id)
        path = f"/tmp/voice_{voice.file_unique_id}.oga"
        await tg_file.download_to_drive(path)
        try:
            transcript, confident, detail = await asyncio.to_thread(transcribe_audio, path)
        finally:
            try:
                os.remove(path)
            except OSError:
                pass
        if not transcript:
            await update.message.reply_text("Couldn't make out a word of that. Try again or type it.")
            return
        if not confident:
            # Never act on a transcript Whisper wasn't sure of — that's how invented
            # detail gets in. Ask, don't guess.
            logger.info(f"Voice note LOW CONFIDENCE ({detail}) — asking for a repeat: {transcript[:60]!r}")
            await update.message.reply_text(
                f"I didn't catch that clearly. What I think I heard: \"{transcript[:150]}\"\n"
                "Say it again, or type it, and I'll act on it properly.")
            return
        logger.info(f"Voice note confidence OK ({detail})")
        await log_message("telegram_in", "chat", f"[voice] {transcript}")
        response = await process_voice(update.message.from_user.id, transcript)
        await update.message.reply_text(response)
        await log_message("telegram_out", "chat", response)
    except Exception as e:
        logger.error(f"Voice handling error: {e}")
        await update.message.reply_text("Voice note didn't process. Type it instead.")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        await log_message("telegram_in", "chat", update.message.text)
        response = await process_message(update.message.from_user.id, update.message.text)
        await update.message.reply_text(response)
        await log_message("telegram_out", "chat", response)
    except Exception as e:
        logger.error(f"Error: {e}")
        await update.message.reply_text(".")

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Telegram error: {context.error}")

async def _whisper_idle_loop():
    """Background loop: unload Whisper once it's been idle, freeing RAM for Ollama."""
    while True:
        await asyncio.sleep(60)
        try:
            await asyncio.to_thread(_maybe_unload_whisper)
        except Exception as e:
            logger.error(f"Whisper idle loop error: {e}")

async def _post_init(app):
    app.create_task(_whisper_idle_loop())

def main():
    app = Application.builder().token(BOT_TOKEN).post_init(_post_init).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    app.add_error_handler(error_handler)
    app.run_polling()

if __name__ == '__main__':
    main()
