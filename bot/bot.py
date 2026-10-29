import asyncio
import contextvars
import logging
import os
import json
import re
import time
import asyncpg
from redis.asyncio import Redis
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, MessageHandler, filters, ContextTypes
import httpx
import zoneinfo
from datetime import datetime, timedelta

import native  # Ghost 2.0 native Habits & Todos (Phase 1) — shared by bot + dashboard
import decisions  # decision loop: questions the overnight runner needs answered (Telegram buttons + app API)

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
# Decision-button authorisation: only the owner's Telegram id may answer. Deliberately NOT
# TELEGRAM_USER_ID, which would also change native.DEFAULT_USER_ID in this pod. Unset = fail closed.
OWNER_ID = int(os.environ.get("GHOST_OWNER_ID") or "0")
BRAND_PRIMARY = os.environ.get("GHOST_BRAND_PRIMARY", "Personal Blog")
BRAND_SECONDARY = os.environ.get("GHOST_BRAND_SECONDARY", "Second Brand")
BRAND_DOMAIN = os.environ.get("GHOST_BRAND_DOMAIN", "example.com")
BRAND_VAULT_DIR = os.environ.get("GHOST_BRAND_VAULT_DIR", "Blog")
TZ_NAME = os.environ.get("GHOST_TZ", "UTC")
LOCAL_TZ = zoneinfo.ZoneInfo(TZ_NAME)
VAULT_PATH = os.environ.get("VAULT_PATH", "/vault")
# Ollama generate endpoint (money path). Was hardcoded here AND in scheduler.py (S27 diagnosed the
# 10.42.0.1 node-gateway IP as load-bearing); moved to env S56 so a CNI/node change is one config
# edit, not a silent break in two files. Same var name as scheduler.py = single source of truth.
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://10.42.0.1:11434/api/generate")

# Shared Postgres connection helper (Session-55 dedupe item, folded in with the 2.0 build):
# new query paths use this one helper instead of re-inlining asyncpg.connect(...). native.py
# owns the canonical implementation; bot + dashboard delegate to it. (The many pre-existing
# inline asyncpg.connect call sites are deliberately left alone — they belong to code 2.0 is
# replacing, and churning them would be the wasted effort the 2.0 scoping note warns against.)
pg_conn = native.pg_conn

SYSTEM_PROMPT = f"""You are Ghost. An AI accountability system running on a private server called hivequeen. You watch habits, money, and behaviour for one person: {USER_NAME}.

WHAT YOU CAN DO:
- Native Habits & Todos (Ghost 2.0, Phase 1 — the go-forward system, being validated ALONGSIDE Habitica, not yet replacing it): create a native habit or to-do from ordinary conversation ("add a habit for stretching", "track reading as a habit", "add a to-do to call the dentist", "add X to my to-dos" — you can create several at once, e.g. "add my exercise habit and a to-do for groceries"); tick a native habit or complete a native to-do by saying you did it ("did my X", "finished X", "X +/-"). These writes are deterministic — they happen directly, no guessing. The live NATIVE HABITS & TODOS block below is the source of truth for what native habits/todos exist and their streak/done state — answer "what habits do I have" / "did that to-do get created" DIRECTLY from that block, never "I can't confirm". If a create request's name is unclear you ASK for it; if a completion could mean more than one item you ASK which; you never guess, and if a write fails you say so — never claim a save that did not happen.
- Habitica: read dailies, habits and to-dos; mark tasks complete; create to-dos ("add task ..."), dailies ("create daily ...") and habits ("new habit ..."); score habits ("scored ...", "habit up/down ...")
- Money: tracked from bank-CSV uploads on the Bills dashboard ONLY, not from chat. You do NOT log expenses, income, debts or budgets from messages — that pipeline was removed. If she tells you about a transaction in chat, don't claim to have logged it; point her to the Bills dashboard CSV import instead.
- Obsidian: on request you can SEARCH her whole vault ("check my notes about ..."), READ a specific note to summarise/quote ("read my X note", "the recipe for Y"), and WRITE — append to a note ("add to note X: ...") or create one ("create note X: ..."), plus a quick daily capture ("note that ..."). Every write is confirm-first: you read back the exact content and target note and only write after she says yes that same turn; you NEVER write on an assumption, and if the target note is unclear you ask which rather than guessing or duplicating. On an ambiguous search/read match, ask which note she means.
- Google Calendar: on request you READ her calendar ("what's on today/tomorrow", "when's my next dentist", "am I free Friday") and WRITE it — create, move or delete events ("schedule X on Friday at 3pm", "move my dentist appointment to Tuesday", "cancel my 2pm meeting"). Every write is confirm-first, exactly like Obsidian: you read back precisely what will change (title, date, time) and only touch the calendar after she says yes that same turn; you NEVER create/move/delete on an assumption, and if which event she means is unclear you ask rather than guessing. Use the title/detail she actually gives — don't add vagueness or specificity she didn't ask for. If the calendar isn't connected yet, say so plainly; never claim an event was added/moved/deleted unless your context confirms it happened.
- Long-term memory: when {USER_NAME} says "remember ..." (or "don't forget ..."), you save it durably — it is shown back to you as LONG-TERM MEMORY in every later chat, so rely on it and never claim you can't recall something listed there. She can ask "what do you remember" or say "forget ...". If she shares something clearly worth keeping and doesn't say "remember", you can offer to.
- Track water, meals, movement and exercise day to day, and notice multi-day patterns
- Overnight build decisions: when the overnight build runner stops on a question, it arrives as a Telegram card with answer buttons (and /decisions re-sends pending ones). Answers are recorded by the buttons themselves, deterministically — you do NOT record or apply them from chat, and you never claim a decision was answered or applied unless your context says so.
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
- NAME: do NOT open with her name, and do not use it by default. You talk to one person and she knows who she is — a name-opener every time reads as a mechanical tic, not warmth. Use her name ONLY when it does real work: re-getting her attention after a long silence, or a moment that genuinely calls for it (something serious, a real celebration). Rare, not routine.
- Never say: "I understand", "That's great", "Of course", "certainly", "absolutely", "definitely"
- Exclamation marks: almost never — only when the moment genuinely earns one
- Got something wrong? One line of apology, then move on. No grovelling.
- Short by default — one or two lines. Longer only when the situation demands it.
- NEVER use "qualify as furniture" or "long enough to fossilize", or any variation of either

UNCERTAINTY — NON-NEGOTIABLE (both directions):
- GROUNDING RULE — governs everything below, no exceptions: the ONLY things you actually know are (1) what {USER_NAME} said in this conversation and (2) what is explicitly present in your context blocks THIS turn — SYSTEM TRUTH (live integration health, the Cephalopod-reach boundary), TODAY SO FAR, the HABITICA BOARD, PATTERNS, LONG-TERM MEMORY, and any vault excerpts. Any claim about system state, an integration, your own code, a past action, a handoff, or the world that is NOT backed by one of those is something you DO NOT KNOW. Say "I'd have to check" (or ask), and offer to look — do not answer from what merely sounds like it should be true, do not infer it, do not fill the gap. "Sounds plausible" is not "known", and something not appearing in a block is not evidence either way. This holds even when a confident-sounding answer is right there to give — and it holds for a premise buried INSIDE her message (a stated cause, a "that's why...", a past-tense "it did X earlier"): if you have no evidence for that premise, don't adopt it and don't start acting on it, name that you have no record of it.
- NEVER INVENT FORWARD. Never state a specific event, place, trip, plan, purchase, person, date or fact that was not explicitly in {USER_NAME}'s message or in verified data given to you (Habitica, Obsidian, FatSecret, the context blocks below). You have no knowledge of her life beyond those. A plausible-sounding detail you made up is a lie, not a guess.
- If her message is unclear, garbled, incomplete or ambiguous, ASK A CLARIFYING QUESTION. Never fill the gap with something that sounds right.
- NEVER INVENT BACKWARD. A missing log is not a fact about the world. Never claim {USER_NAME} didn't eat, drink, move or do anything based on the absence of a log — say "nothing logged", and ask if it matters.
- Her routine (office Mon–Wed, WFH Thu–Fri, meal times, bedtime) is a typical pattern, not live knowledge. Say "usually" — never assert where she is or what she's doing right now.
- If you're inferring, sound like it. "Looks like", "if the log's right", "assuming you're home by now" — not flat statements.
- If you get something wrong and she calls it out, correct it in one line without inventing a new explanation for why it happened.
- NEVER CLAIM AN ACTION YOU DID NOT TAKE. Every real write (note, Habitica, FatSecret, handoff) happens in code before you reply, and the context tells you when it did. If no confirmation is in your context, the action did not happen — say you can't do it or ask, never reply as if it's done. "Sent", "logged", "deleted", "told Cephalopod" are claims about reality, not conversational filler.
- NEVER PROMISE A LATER FOLLOW-UP. You act only within the single turn you're replying in — there is no background job that checks something and messages her afterwards. So never say "I'll have a look and get back to you", "let me check and circle back", "I'll keep an eye on it", or anything that defers a result to a future message: that follow-up will never come, and the thread just goes silent. Either do the lookup NOW (you can read her calendar, search/read the vault, and read the live Habitica/board context this turn) and answer in this reply, or say plainly you can't and ask her to prompt you again. Answer in the present, never in the future.
- NEVER CLAIM — OR AGREE — THAT A SYSTEM FAILED, PRESENT OR PAST, without an actual error in your context: a real exception, a non-200 response, or a live status result IN THIS TURN. This covers present tense ("Habitica's out", "the writes aren't going through"), PAST tense ("Redis dumped my data last night", "the DB crashed earlier", "it went down this morning"), and DATA-LOSS claims — all are factual claims needing evidence. If SHE volunteers one and the SYSTEM TRUTH block shows that system healthy, the block wins: it shows OK/connected NOW and you have NO record of any failure — say exactly that and offer a live check, do NOT agree, do NOT accept the premise, and do NOT start acting on it (planning a rebuild, "that explains it", etc.). "OK now" is not agreement that it broke earlier, but absence of evidence means you don't confirm a failure either — you have no record of one. (On 20/08 you told her "Habitica's out" while it returned 200 the whole time — do not do that, in any tense.)
- CEPHALOPOD is a separate system. The ONLY path to it is the dashboard chat command "send to cephalopod: <text>" (or "send <note name> to cephalopod"). From Telegram you cannot send anything to Cephalopod, instruct it, or delete from it — if she asks via Telegram, say it has to be done from the dashboard chat, in those words.

WHEN CONFIRMING A HABIT: one line, no praise — "noted." / "good." / "about time." / "logged."
WHEN HANDLING EXCUSES: weak excuse — call it out in one line. Legitimate — acknowledge briefly, move on. Never lecture.
WHEN ASKED WHAT YOU CAN DO: tell her plainly what's listed above. Don't pretend you can't do things you can.

CURRENT TIMEZONE: {TZ_NAME}
CURRENT INTEGRATIONS: Telegram (text + voice notes), Habitica (read/write), Postgres, Redis, Obsidian vault (read/write), desk dashboard with chat, Tasker scrolling interrupts, FatSecret food diary (READ AND WRITE — voice food notes log to it automatically, and a typed message containing "fatsecret" plus the food logs it too, e.g. "log to fatsecret: crackers and pate". If TODAY SO FAR shows no food logged, that means the diary is empty so far today — NOT that FatSecret is disconnected or read-only; never claim either)."""

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
        await r.set(f"conversation:{user_id}", json.dumps(history), ex=259200)
        await r.aclose()
    except Exception as e:
        logger.error(f"Redis save error: {e}")

RECENT_PHRASES_KEY = "recent_ghost_phrases"

# Locked cascade stages from the vault Area specs (Money Moves.md, Creative.md).
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

async def get_board_context() -> str:
    """Real Habitica board state (dailies done/pending, open to-dos, habits), so
    'what have I done today' / 'what's left' is answered from the ACTUAL board, not
    LLM recall (Session 33). Cached in Redis 'board_context' (5 min) to avoid the
    30-req/min limit on every chat turn; busted by any Habitica write (see
    _bust_board_cache). Returns '' if Habitica is unreachable — never a guess."""
    try:
        r = Redis(host=REDIS_HOST, port=6379, password=REDIS_PASSWORD, decode_responses=True)
        cached = await r.get("board_context")
        if cached is not None:
            await r.aclose()
            return cached
        await r.aclose()
    except Exception:
        pass
    try:
        from habitica import get_dailies, get_habits_detailed, get_todos
        dailies = await get_dailies()
        todos = await get_todos()
        try:
            habits = await get_habits_detailed()
        except Exception:
            habits = []
        done = dailies.get("done", [])
        pending = dailies.get("pending", [])
        parts = ["HABITICA BOARD (live, as of now — this is the source of truth for "
                 "what's done, not your memory):"]
        parts.append(f"Dailies done today: {', '.join(done) if done else '(none yet)'}.")
        parts.append(f"Dailies still pending today: {', '.join(pending) if pending else '(none — all done)'}.")
        if todos:
            parts.append(f"Open to-dos: {', '.join(todos[:12])}.")
        if habits:
            # Session 52: fold each habit's derived streak/tapped/at-risk state into the
            # board line so "what's my streak on X" is answered from real data, not a guess.
            hbits = []
            for h in habits:
                extra = []
                if h.get("streak", 0) >= 2:
                    extra.append(f"{h['streak']}-day streak")
                if h.get("doneToday"):
                    extra.append("tapped today")
                if h.get("atRisk"):
                    extra.append("in the red")
                hbits.append(h["name"] + (f" ({', '.join(extra)})" if extra else ""))
            parts.append("Habits on the board: " + ", ".join(hbits)
                         + ". (Habit streak / tapped-today / at-risk are DERIVED best-effort values, "
                           "not exact Habitica fields — habits have no native streak.)")
        text = "\n".join(parts)
        try:
            r = Redis(host=REDIS_HOST, port=6379, password=REDIS_PASSWORD, decode_responses=True)
            await r.set("board_context", text, ex=300)
            await r.aclose()
        except Exception:
            pass
        return text
    except Exception as e:
        logger.error(f"get_board_context error: {e}")
        return ""

async def _bust_board_cache():
    """Drop the cached board so the next turn re-reads the real state after a write."""
    try:
        r = Redis(host=REDIS_HOST, port=6379, password=REDIS_PASSWORD, decode_responses=True)
        await r.delete("board_context")
        await r.aclose()
    except Exception:
        pass


async def get_native_context(user_id: int = None) -> str:
    """Ghost 2.0 (Phase 1) native Habits & Todos state, injected into every Claude turn the same
    way get_board_context (Habitica) is — so 'what habits do I have' / 'did that to-do get created'
    is answered from the REAL native tables, not LLM recall or 'I can't confirm' (S66: added after
    Lea's native adds went nowhere and Ghost had no native read to fall back on). A real query each
    time the cache expires; Redis-cached 'native_context' 5 min, busted by any native write via
    _bust_native_cache. Returns '' on a DB error — never a guess."""
    try:
        r = Redis(host=REDIS_HOST, port=6379, password=REDIS_PASSWORD, decode_responses=True)
        cached = await r.get("native_context")
        await r.aclose()
        if cached is not None:
            return cached
    except Exception:
        pass
    try:
        uid = user_id if user_id is not None else native.DEFAULT_USER_ID
        habits = await native.list_habits(uid)
        todos = await native.list_todos(uid, include_done=True)
        parts = ["NATIVE HABITS & TODOS (Ghost 2.0, live — the source of truth for the native "
                 "system that is replacing Habitica; NOT your memory):"]
        if habits:
            hb = []
            for h in habits[:20]:
                bits = []
                if h.get("streak", 0) >= 1:
                    bits.append(f"{h['streak']}d streak")
                bits.append("done today" if h.get("done_today") else "not done today")
                hb.append(f"{h['name']} ({', '.join(bits)})")
            parts.append("Habits: " + "; ".join(hb) + ".")
        else:
            parts.append("Habits: (none created yet).")
        if todos:
            open_t, done_t = [], []
            for t in todos:
                if t.get("done"):
                    done_t.append(t["text"])
                else:
                    due = f" due {t['due_date']}" if t.get("due_date") else ""
                    open_t.append(f"{t['text']}{due}{' [OVERDUE]' if t.get('overdue') else ''}")
            if open_t:
                parts.append("Open to-dos: " + "; ".join(open_t[:20]) + ".")
            if done_t:
                parts.append(f"Done to-dos: {', '.join(done_t[:10])}.")
        else:
            parts.append("To-dos: (none created yet).")
        text = "\n".join(parts)
        try:
            r = Redis(host=REDIS_HOST, port=6379, password=REDIS_PASSWORD, decode_responses=True)
            await r.set("native_context", text, ex=300)
            await r.aclose()
        except Exception:
            pass
        return text
    except Exception as e:
        logger.error(f"get_native_context error: {e}")
        return ""


async def _bust_native_cache():
    """Drop the cached native state so the next turn re-reads real rows after a native write."""
    try:
        r = Redis(host=REDIS_HOST, port=6379, password=REDIS_PASSWORD, decode_responses=True)
        await r.delete("native_context")
        await r.aclose()
    except Exception:
        pass

# ---------------------------------------------------------------------------
# Live system-status line (Session 52, Task 1). The single highest-value item from
# Session 50's grounding-block spec: a cached, always-on line telling the model which
# integrations are actually up right now, so a VOLUNTEERED "Habitica's out" (a statement,
# not a status question — the S42/S43 class handle_integration_status could not catch)
# is answered from a real check instead of agreed with. Six probes run in parallel,
# lazily, and the result is cached in Redis `system_status` (~45s TTL, the dashboard's
# 25s command-center pattern) so at most one chat turn per ~45s pays the probe cost;
# an idle bot pays nothing. Cephalopod is deliberately reported as unreachable-from-here
# rather than probed (the dashboard-only boundary; avoids a network dep bot.py lacks).
# ---------------------------------------------------------------------------
SYSTEM_STATUS_TTL = 45

async def _probe_habitica_status() -> str:
    try:
        from habitica import health_check
        ok, detail = await health_check()
        return "OK" if ok else f"DOWN ({detail})"
    except Exception as e:
        return f"DOWN (unreachable: {type(e).__name__})"

async def _probe_fatsecret_status() -> str:
    try:
        import fatsecret
        if not fatsecret.is_configured():
            return "not configured"
        summ = await get_fatsecret_summary()   # 15-min cached; None here = a real fetch failure
        return "connected" if summ is not None else "configured but the fetch is failing"
    except Exception:
        return "configured but the fetch is failing"

async def _probe_anthropic_status() -> str:
    try:
        async with httpx.AsyncClient() as client:
            r = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={"x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01",
                         "content-type": "application/json"},
                json={"model": "claude-haiku-4-5-20251001", "max_tokens": 1,
                      "messages": [{"role": "user", "content": "hi"}]},
                timeout=12)
        return "OK" if r.status_code == 200 else f"DOWN (HTTP {r.status_code})"
    except Exception:
        return "DOWN (unreachable)"

async def _probe_postgres_status() -> str:
    try:
        conn = await asyncpg.connect(host=POSTGRES_HOST, port=5432, user="postgres",
                                     password=POSTGRES_PASSWORD, database="ghost_db", timeout=8)
        await conn.fetchval("SELECT 1")
        await conn.close()
        return "OK"
    except Exception:
        return "DOWN (unreachable)"

async def _probe_redis_status() -> str:
    try:
        rr = Redis(host=REDIS_HOST, port=6379, password=REDIS_PASSWORD, decode_responses=True)
        await rr.ping()
        await rr.aclose()
        return "OK"
    except Exception:
        return "DOWN (unreachable)"

async def get_system_truth_context() -> str:
    """The consolidated always-on SYSTEM TRUTH block (Session 58), cached ~45s: live integration
    health + the Cephalopod-reach boundary, as ONE grounded block rather than
    separate bolt-ons. This is THE source of truth for system state; the hardened SYSTEM_PROMPT
    rule ties every system/world claim to it ("not in the block => I'd have to check").

    MONEY BOUNDARY: deliberately carries NO expense/debt/budget data. Money stays on the
    Ollama/deterministic side (30/08/2026 decision) — never in this Claude-side block."""
    try:
        r = Redis(host=REDIS_HOST, port=6379, password=REDIS_PASSWORD, decode_responses=True)
        cached = await r.get("system_truth")
        await r.aclose()
        if cached is not None:
            return cached
    except Exception:
        pass
    try:
        hab, fs, anth, pg, rds = await asyncio.gather(
            _probe_habitica_status(), _probe_fatsecret_status(), _probe_anthropic_status(),
            _probe_postgres_status(), _probe_redis_status())
    except Exception as e:
        logger.error(f"get_system_truth_context build error: {e}")
        return ""
    integ = "\n".join([
        f"- Habitica: {hab}",
        f"- FatSecret: {fs}",
        f"- Anthropic (your own model API): {anth}",
        f"- Postgres (her data store): {pg}",
        f"- Redis: {rds}",
    ])
    text = (
        "SYSTEM TRUTH (live, cached ~45s — the source of truth for system state right now. Anything "
        "NOT in this block you do NOT know: say you'd have to check, never infer it):\n"
        "Integrations:\n" + integ + "\n"
        "- Cephalopod: a separate system. From here (Telegram) there is NO path to it and no live "
        "status — it's reachable only from the dashboard chat. Never say you sent/told it anything from here.\n"
        "If she says something here is down/broken/applied/sent and this block says otherwise, trust THIS "
        "block: tell her what it actually shows and offer to re-check. Only call something down if it shows "
        "DOWN/UNREACHABLE here or you hit a real error THIS turn."
    )
    try:
        r = Redis(host=REDIS_HOST, port=6379, password=REDIS_PASSWORD, decode_responses=True)
        await r.set("system_truth", text, ex=SYSTEM_STATUS_TTL)
        await r.aclose()
    except Exception:
        pass
    return text

async def get_today_context() -> str:
    now = datetime.now(LOCAL_TZ)
    # Full local date, not just weekday+time: without the calendar date the model
    # knew "wednesday 11am" but had to GUESS the date/month/year (Session 33 — the
    # "doesn't know what day it is" symptom was missing data, not a tz bug).
    date_str = now.strftime('%A %d %B %Y').replace(' 0', ' ')
    time_str = now.strftime('%I:%M%p').lstrip('0').lower()
    lines = [f"Today is {date_str}. Current time: {time_str}."]
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
        await conn.close()
        lines.append(f"Messages from {USER_NAME} today: {msg_row['n']}.")
        # MONEY BOUNDARY (S58): the "Spending last 7 days: $X" expense aggregate that used to sit
        # here was removed — expense/debt/budget data stays Ollama/deterministic-side (30/08/2026
        # decision), never in the Claude grounding stack. Money questions route to the deterministic
        # money-lookup / Ollama path, not this block.
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
        # Local-date bound, not SQL CURRENT_DATE: the DB session runs in GMT, so
        # CURRENT_DATE is the UTC day and shifts the 14-day window by one at the
        # Melbourne 00:00-10:00 boundary (same root cause as Session 30's fix).
        since = datetime.now(LOCAL_TZ).date() - timedelta(days=14)
        rows = await conn.fetch(
            "SELECT date, event_type FROM daily_events WHERE date >= $1 ORDER BY date", since
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

def _preserve_vault_owner(path: str, orig_st) -> None:
    """Keep a vault note owned by the human, not root. The bot pod runs as root, so a
    NEWLY CREATED note is root-owned and locks the user out of her own file in Obsidian
    (the same class of bug fixed on the scheduler's sync writer in session 26). Truncate-
    in-place (open 'w') and append (open 'a') keep an existing file's owner untouched, so
    this only has to act on newly created files — but it also restores a captured owner
    for belt-and-braces if a caller ever switches to a replace-style write.
    orig_st = os.stat() taken before the write if the file existed, else None (new file →
    inherit the vault root's owner). Failure is logged, never fatal."""
    try:
        if orig_st is not None:
            os.chown(path, orig_st.st_uid, orig_st.st_gid)
            os.chmod(path, orig_st.st_mode)
        else:
            vst = os.stat(VAULT_PATH)
            os.chown(path, vst.st_uid, vst.st_gid)
    except OSError as e:
        logger.error(f"could not restore vault-note owner on {path}: {e}")

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
        orig_st = os.stat(path) if os.path.exists(path) else None
        if orig_st is not None:
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
        _preserve_vault_owner(path, orig_st)
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

async def handle_cephalopod_query(user_message: str, source: str):
    """Session 52 (Task 4): a QUESTION about whether something was handed to Cephalopod
    ("did you send/tell Cephalopod …?", "is that in Cephalopod yet?"). From TELEGRAM the
    answer is a hard-coded boundary statement — the handoff path is dashboard-only, so from
    here Ghost has sent nothing and cannot (the S16 class: Claude once fabricated 'Sent to
    Cephalopod' over Telegram). From the DASHBOARD it answers from a real ghost_handoffs read.
    Runs BEFORE handle_cephalopod_handoff so a question never trips the send path — which also
    closes a latent bug where 'did you send the note to cephalopod?' on the dashboard matched
    the send regex and handed off a note off the back of a mere question.
    Returns a reply, or None to let the message route normally (incl. real imperative sends)."""
    low = user_message.lower()
    if "cephalopod" not in low:
        return None
    # Never intercept an actual imperative send — that belongs to handle_cephalopod_handoff.
    if re.search(r'(?:send|hand ?off|pass|give)\b[^?]*cephalopod\s*:', low) or \
       low.strip().startswith(("send ", "hand off", "handoff", "pass ", "give ")):
        return None
    is_q = low.rstrip().endswith("?") or low.lstrip().startswith(
        ("did ", "have you", "has ", "was ", "were ", "is ", "are you", "you ", "already"))
    retro = any(c in low for c in ("did you send", "did you tell", "have you sent", "have you told",
                                   "already sent", "already told", "did cephalopod", "get sent to cephalopod",
                                   "sent to cephalopod", "told cephalopod", "pass to cephalopod",
                                   "handed to cephalopod", "did you pass", "did you hand", "did you give",
                                   "in cephalopod", "reach cephalopod", "make it to cephalopod"))
    if not (is_q or retro):
        return None
    if source != "dashboard":
        return ("I can't send anything to Cephalopod from here — that only works from the dashboard "
                "chat ('send to cephalopod: ...'). So from this Telegram conversation I haven't sent it "
                "anything, and I can't. If you want it handed over, do it from the dashboard.")
    # Dashboard: answer from the real handoff table (Cephalopod's own database).
    try:
        conn = await asyncpg.connect(host=POSTGRES_HOST, port=5432, user="postgres",
                                     password=POSTGRES_PASSWORD, database=CEPHALOPOD_DB)
        try:
            rows = await conn.fetch(
                "SELECT id, kind, created_at, left(payload, 50) AS snippet "
                "FROM ghost_handoffs ORDER BY id DESC LIMIT 3")
            total = await conn.fetchval("SELECT COUNT(*) FROM ghost_handoffs")
        finally:
            await conn.close()
    except Exception as e:
        logger.error(f"cephalopod query read error: {e}")
        return "I can't reach Cephalopod's handoff table right now, so I can't tell you for certain what's been sent. Try again in a moment."
    if not rows:
        return "Nothing has been handed to Cephalopod — the handoff table is empty."
    bits = []
    for r in rows:
        when = r["created_at"].astimezone(LOCAL_TZ).strftime("%d %b %H:%M") if r["created_at"] else "?"
        bits.append(f"#{r['id']} {r['kind']} ({when}): {r['snippet'].strip()}…")
    return (f"{total} handoff(s) to Cephalopod on record. Most recent:\n" + "\n".join(bits))

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

# ---------------------------------------------------------------------------
# S80 phase-2 vault tools: on-demand full-vault SEARCH, READ, and confirmed WRITE, callable
# mid-conversation by the chat turn loop (deterministic handlers, same pattern as native).
# CHAT-TRIGGERED ONLY — no anchor/scheduler/background path gets write access. Every write is
# echoed back and only committed after an explicit "yes" the same turn, then logged to
# ghost_db.vault_writes (durable + independent of Obsidian). Vault content NEVER touches the
# financial schema (transactions/spending/rook) — the standing boundary is untouched here.
# No folder restrictions on search/read/write (Lea owns vault tidiness), only a traversal guard
# so a write can't escape the vault entirely.
# ---------------------------------------------------------------------------
_VAULT_STOPWORDS = {
    "what", "did", "i", "write", "wrote", "about", "my", "notes", "note", "vault", "read", "open",
    "obsidian", "check", "the", "a", "an", "in", "on", "me", "you", "tell", "show", "summarise",
    "summarize", "find", "have", "has", "do", "does", "say", "says", "said", "for", "and", "is",
    "are", "was", "were", "of", "to", "there", "anything", "whats", "quote", "from", "recipe"
}

def _vault_scan(terms):
    """Full-vault markdown scan -> ranked [(score, relpath, fname, cleaned, lower)]. Dotfolders
    skipped; filename hits weighted 5x. The shared engine behind search + read + ambiguity."""
    import re as _re
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
            nl = fname.lower()
            score = sum(lower.count(t) for t in terms) + sum(5 for t in terms if t in nl)
            if score > 0:
                results.append((score, os.path.relpath(os.path.join(root, fname), VAULT_PATH),
                                fname, cleaned, lower))
    results.sort(key=lambda x: -x[0])
    return results

def vault_search_matches(query: str, limit: int = 6):
    """Ranked note matches for an on-demand search: [{path, fname, score, excerpt}]. Multiple
    entries let the caller ASK which note is meant instead of guessing (Task 1)."""
    import re as _re
    terms = [w for w in _re.findall(r'[a-z0-9]+', query.lower()) if w not in _VAULT_STOPWORDS and len(w) > 2]
    if not terms:
        return []
    out = []
    for score, rel, fname, cleaned, lower in _vault_scan(terms)[:limit]:
        pos = min((lower.find(t) for t in terms if t in lower), default=0)
        start = max(0, pos - 100)
        out.append({"path": rel, "fname": fname, "score": score,
                    "excerpt": " ".join(cleaned[start:start + 400].split())})
    return out

def find_notes_by_name(name_query: str):
    """All .md notes whose FILENAME contains name_query (case-insensitive). [] if none.
    Used to resolve a write/read target; >1 hit = ambiguous -> the caller asks."""
    q = (name_query or "").lower().strip()
    if q.endswith(".md"):
        q = q[:-3]
    q = q.strip()
    if not q:
        return []
    hits = []
    for root, dirs, files in os.walk(VAULT_PATH):
        dirs[:] = [d for d in dirs if not d.startswith('.')]
        for fname in files:
            if fname.endswith(".md") and q in fname.lower():
                hits.append(os.path.relpath(os.path.join(root, fname), VAULT_PATH))
    hits.sort(key=len)   # shortest filename first = the most exact-ish match
    return hits

def append_to_note_path(relpath: str, content: str) -> str:
    """Append content VERBATIM to an existing note by exact relpath (never clobbers — open 'a',
    leading newline for separation), owner preserved. Returns relpath, or '' if missing/failed."""
    path = os.path.join(VAULT_PATH, relpath)
    if not os.path.isfile(path):
        return ""
    try:
        orig = os.stat(path)
        with open(path, "a", encoding="utf-8") as f:
            f.write("\n" + content.rstrip("\n") + "\n")
        _preserve_vault_owner(path, orig)
        return relpath
    except OSError as e:
        logger.error(f"append_to_note_path error: {e}")
        return ""

def create_vault_note(relpath: str, content: str) -> str:
    """Create a NEW note at relpath (adds .md if missing, makes parent dirs). Refuses to overwrite
    an existing file (caller handles the exists->append/ask case) and guards against escaping the
    vault. Owner set to the vault owner (not root). Returns relpath written, or '' on refuse/fail."""
    rel = (relpath or "").strip().lstrip("/")
    if not rel:
        return ""
    if not rel.endswith(".md"):
        rel += ".md"
    base = os.path.abspath(VAULT_PATH)
    full = os.path.normpath(os.path.join(base, rel))
    if full != base and not full.startswith(base + os.sep):
        logger.error(f"create_vault_note traversal refused: {relpath}")
        return ""
    if os.path.exists(full):
        return ""
    try:
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as f:
            f.write(content.rstrip("\n") + "\n")
        _preserve_vault_owner(full, None)
        return os.path.relpath(full, base)
    except OSError as e:
        logger.error(f"create_vault_note error: {e}")
        return ""

async def log_vault_write(note_path: str, mode: str, content: str, source: str = "telegram"):
    """Durable, Obsidian-independent record of every committed vault write (Task 4). ghost_db,
    NOT the financial schema. Never fatal to the write it records."""
    try:
        conn = await pg_conn()
        try:
            await conn.execute(
                """CREATE TABLE IF NOT EXISTS vault_writes (
                    id BIGSERIAL PRIMARY KEY, ts TIMESTAMPTZ NOT NULL DEFAULT now(),
                    note_path TEXT NOT NULL, mode TEXT NOT NULL, content TEXT, source TEXT)""")
            await conn.execute(
                "INSERT INTO vault_writes (note_path, mode, content, source) VALUES ($1,$2,$3,$4)",
                note_path, mode, content, source)
        finally:
            await conn.close()
    except Exception as e:
        logger.error(f"log_vault_write error: {e}")

import re as _re_vault
# Write-intent grammar. Deliberately anchored on a target + ':' so target/content are unambiguous;
# the message is echoed back for confirmation regardless, so a mis-parse can't silently write.
_WRITE_CREATE_RE = _re_vault.compile(
    r'^\s*(?:create|make|start)\s+(?:a\s+)?(?:new\s+)?note\s+(?:called\s+|titled\s+|named\s+)?'
    r'(?P<n>.+?)\s*(?:with\s+content\s+|with\s+|:|—|-)\s*(?P<c>.+)$', _re_vault.I | _re_vault.S)
_WRITE_APPEND_A = _re_vault.compile(   # "add/append [to] [my|the] note NAME: CONTENT"
    r'^\s*(?:append|add)(?:\s+to)?\s+(?:my\s+|the\s+)?note\s+(?P<n>[^:]+?)\s*:\s*(?P<c>.+)$',
    _re_vault.I | _re_vault.S)
_WRITE_APPEND_B = _re_vault.compile(   # "add to [my] NAME note: CONTENT"
    r'^\s*(?:append|add)(?:\s+to)?\s+(?:my\s+|the\s+)?(?P<n>[^:]+?)\s+note\s*:\s*(?P<c>.+)$',
    _re_vault.I | _re_vault.S)
_DAILY_TRIGS = ("note that ", "log this ", "write to my notes ", "add to obsidian ")
_VAULT_YES = {"yes", "yep", "yeah", "y", "confirm", "confirmed", "do it", "go ahead", "send it",
              "commit", "ok", "okay", "sure", "yes please", "please do", "write it"}
_VAULT_NO = {"no", "nope", "cancel", "stop", "don't", "dont", "nevermind", "never mind",
             "scrap it", "forget it", "leave it"}

def _vw_affirmative(m: str) -> bool:
    ml = m.strip().lower().strip(" .!,")
    return ml in _VAULT_YES

def _vw_negative(m: str) -> bool:
    ml = m.strip().lower().strip(" .!,")
    return ml in _VAULT_NO

def _parse_vault_write(msg: str):
    """(mode, target_name, content) with mode in {create, append, daily}; ('unparseable','','')
    when a write verb is clearly present but target/content can't be split; else None."""
    m = _WRITE_CREATE_RE.match(msg)
    if m:
        return ("create", m.group("n").strip().strip('"\''), m.group("c").strip())
    for rx in (_WRITE_APPEND_A, _WRITE_APPEND_B):
        m = rx.match(msg)
        if m:
            return ("append", m.group("n").strip().strip('"\''), m.group("c").strip())
    low = msg.lower()
    for trig in _DAILY_TRIGS:
        if low.startswith(trig):
            return ("daily", "", msg[len(trig):].strip(" :,-.!?"))
    if _re_vault.match(r'^\s*(?:append|add\s+to|create|make)\b', low) and ("note" in low or "obsidian" in low):
        return ("unparseable", "", "")
    return None

async def _commit_vault_write(data: dict, source: str) -> str:
    mode, path, content = data["mode"], data.get("path", ""), data["content"]
    if mode == "daily":
        written = append_to_daily_note(content)
    elif mode == "append":
        written = append_to_note_path(path, content)
    elif mode == "create":
        written = create_vault_note(path, content)
    else:
        written = ""
    if not written:
        return "That didn't write — the note wasn't found or the write failed, so nothing changed."
    await log_vault_write(written, mode, content, source)
    logger.info(f"vault write committed: {mode} -> {written}")
    return f"{'Created' if mode == 'create' else 'Appended to'} {written}. Logged it."

async def handle_vault_write(user_id: int, user_message: str, source: str):
    """S80: on-demand vault write with a same-turn confirmation handshake. Turn 1 parses the
    request, resolves/asks for the target, echoes the EXACT content + target and stores a pending
    write; turn 2's explicit yes commits it (turn 2's no scraps it). Never writes on an assumption;
    an ambiguous target asks rather than guessing or creating a duplicate. Returns a reply, or None
    to fall through. Only the chat turn reaches this — no scheduler/anchor path does."""
    pend_key = f"pending_vault_write:{user_id}"
    r = Redis(host=REDIS_HOST, port=6379, password=REDIS_PASSWORD, decode_responses=True)
    try:
        pending = await r.get(pend_key)
        if pending:
            if _vw_affirmative(user_message):
                await r.delete(pend_key)
                return await _commit_vault_write(json.loads(pending), source)
            if _vw_negative(user_message):
                await r.delete(pend_key)
                return "Scrapped — nothing written."
            # neither yes nor no: don't leave a landmine that a later stray 'yes' could fire.
            await r.delete(pend_key)
        parsed = _parse_vault_write(user_message)
        if not parsed:
            return None
        mode, name, content = parsed
        if mode == "unparseable":
            return ("Say it as `add to note <name>: <what to add>` or `create note <name>: <content>` "
                    "and I'll read it back before writing anything.")
        if not content and mode != "unparseable":
            return "There's nothing to write — give me the content and I'll read it back before saving."
        if mode == "daily":
            target_rel, target_desc = "", "today's daily note"
        elif mode == "create":
            existing = find_notes_by_name(name)
            if existing:
                return (f"A note matching '{name}' already exists ({existing[0]}). Want that one? Say "
                        f"`add to note {name}: ...` to append instead of making a duplicate.")
            target_rel = name if name.lower().endswith(".md") else name + ".md"
            target_desc = f"a new note `{target_rel}`"
        else:  # append
            matches = find_notes_by_name(name)
            if not matches:
                return (f"No note named like '{name}'. Make it with `create note {name}: <content>`, "
                        "or tell me the exact note name.")
            if len(matches) > 1:
                return ("More than one note matches that — which do you mean: "
                        + ", ".join(f"`{p}`" for p in matches[:6])
                        + "? Tell me and I'll read the change back before writing.")
            target_rel, target_desc = matches[0], f"`{matches[0]}`"
        await r.set(pend_key, json.dumps({"mode": mode, "path": target_rel,
                                          "content": content, "source": source}), ex=900)
        verb = "create" if mode == "create" else "append to"
        return (f"Ready to {verb} {target_desc}:\n\n{content}\n\nConfirm? (yes to write, no to scrap)")
    finally:
        await r.aclose()

_VAULT_READ_RE = _re_vault.compile(
    r'\b(?:read|open|show(?:\s+me)?|summari[sz]e|quote(?:\s+from)?|pull up|whats?\s+in)\b.*?'
    r'\b(?:note|notes)\b', _re_vault.I)

async def handle_vault_read(user_id: int, user_message: str):
    """S80 Task 2: read a specific note on request (fuzzy name ok). Resolves via filename match,
    else falls back to Task-1 content search; on multiple matches, asks which. Returns a reply
    (Claude summarising/quoting the resolved note), or None to fall through. Read-only."""
    low = user_message.lower()
    if not _VAULT_READ_RE.search(user_message):
        return None
    # extract the name: strip read verbs + 'note(s)'/'my'/'the' filler, keep the topic words
    name = _re_vault.sub(
        r'\b(?:can you|could you|please|read|open|show|me|summari[sz]e|quote|from|pull up|'
        r'whats?|in|my|the|note|notes|for|out|to)\b', " ", low)
    name = _re_vault.sub(r'[^a-z0-9 ]+', " ", name).strip()
    if not name:
        return None
    matches = find_notes_by_name(name)
    if len(matches) == 1:
        result = read_vault_note(matches[0].rsplit("/", 1)[-1][:-3])
        if result:
            rel, content = result
            snippet = content[:4000]
            return await call_claude(
                user_id,
                f"{user_message} [Vault note '{rel}' content follows — summarise or quote from ONLY "
                f"this, do not invent: <<<{snippet}>>>]", route_text=user_message)
    elif len(matches) > 1:
        return ("A few notes match that — which one: " + ", ".join(f"`{p}`" for p in matches[:6])
                + "?")
    # no filename match -> content search to locate it
    hits = vault_search_matches(name, limit=6)
    if not hits:
        return await call_claude(
            user_id, f"{user_message} [No vault note found matching that — tell her plainly you "
            "couldn't find it and ask for the exact name]", route_text=user_message)
    if len(hits) == 1 or hits[0]["score"] >= 2 * (hits[1]["score"] if len(hits) > 1 else 0):
        top = hits[0]
        result = read_vault_note(top["fname"][:-3])
        if result:
            rel, content = result
            return await call_claude(
                user_id, f"{user_message} [Best-match vault note '{rel}' content follows — summarise "
                f"or quote ONLY this, do not invent: <<<{content[:4000]}>>>]", route_text=user_message)
    names = ", ".join(f"`{h['path']}`" for h in hits[:6])
    return f"Could be a few notes — which do you mean: {names}?"

# ---------------------------------------------------------------------------
# Google Calendar (Session 82). Ghost's OWN calendar (gcal.py): on-demand reads and
# confirm-first create/move/delete, reusing the Session 80 vault-write handshake shape
# (pending state keyed by user, commit ONLY on her explicit yes THIS turn, a non-yes/non-no
# clears the pending so no stray later 'yes' fires it). Only the chat turn reaches these
# (Telegram and dashboard both route through process_message); the scheduler wires calendar
# into the anchors separately, read-only. Calendar carries NO financial/mood routing limit.
# The write grammar is deliberately narrow + always echoed back before committing, so a
# mis-parse can never silently touch the calendar.
# ---------------------------------------------------------------------------
from datetime import date as _cdate, time as _ctime
import gcal

_CAL_WEEKDAYS = {"monday": 0, "mon": 0, "tuesday": 1, "tue": 1, "tues": 1, "wednesday": 2,
                 "wed": 2, "weds": 2, "thursday": 3, "thu": 3, "thur": 3, "thurs": 3,
                 "friday": 4, "fri": 4, "saturday": 5, "sat": 5, "sunday": 6, "sun": 6}
_CAL_MONTHS = {"january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3,
               "april": 4, "apr": 4, "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7,
               "august": 8, "aug": 8, "september": 9, "sep": 9, "sept": 9, "october": 10,
               "oct": 10, "november": 11, "nov": 11, "december": 12, "dec": 12}
# A calendar-write intent must carry one of these signals — keeps "delete the gym habit" /
# "cancel my subscription" out of the calendar path (they route to native/chat instead).
_CAL_SIGNAL_RE = re.compile(
    r'\b(calendar|event|appointment|appt|meeting|booking|reservation|schedule|scheduled|reschedul\w*|book)\b',
    re.I)
_CAL_DEFAULT_MINUTES = 60          # timed event with no explicit end

def _cal_now():
    return datetime.now(LOCAL_TZ)

def _cal_parse_time(text):
    """First clock time in text -> datetime.time, or None. Accepts 3pm / 3:30pm / 15:00 / noon /
    midnight / midday. A bare number without am|pm is NOT accepted (too ambiguous)."""
    t = text.lower()
    if re.search(r'\bnoon\b|\bmidday\b', t):
        return _ctime(12, 0)
    if re.search(r'\bmidnight\b', t):
        return _ctime(0, 0)
    m = re.search(r'\b(\d{1,2})(?::(\d{2}))?\s*(a\.?m\.?|p\.?m\.?)', t)
    if m:
        h = int(m.group(1)) % 12
        if m.group(3).startswith("p"):
            h += 12
        mi = int(m.group(2) or 0)
        if 0 <= h <= 23 and 0 <= mi <= 59:
            return _ctime(h, mi)
    m = re.search(r'\b([01]?\d|2[0-3]):([0-5]\d)\b', t)     # 24h HH:MM
    if m:
        return _ctime(int(m.group(1)), int(m.group(2)))
    return None

def _cal_parse_date(text):
    """First calendar date in text -> datetime.date, or None. today/tonight/this-x, tomorrow,
    weekday names (soonest future occurrence), 'the Nth', 'Month D', 'D Month', DD/MM(/YY),
    'in N days'. The resolved date is always echoed back for confirmation."""
    t = text.lower()
    today = _cal_now().date()
    if re.search(r'\btoday\b|\btonight\b|\bthis (morning|afternoon|evening|arvo)\b', t):
        return today
    if re.search(r'\btomorrow\b|\btmr\b|\btmrw\b', t):
        return today + timedelta(days=1)
    m = re.search(r'\bin (\d{1,3}) days?\b', t)
    if m:
        return today + timedelta(days=int(m.group(1)))
    if re.search(r'\bin a week\b|\bnext week\b', t) and not re.search(r'\b(' + "|".join(_CAL_WEEKDAYS) + r')\b', t):
        return today + timedelta(days=7)
    m = re.search(r'\b(next\s+)?(' + "|".join(_CAL_WEEKDAYS) + r')\b', t)
    if m:
        target = _CAL_WEEKDAYS[m.group(2)]
        delta = (target - today.weekday()) % 7
        if delta == 0:
            delta = 7                          # 'friday' when today is friday -> the next one
        if m.group(1):                         # 'next friday' -> the following week's
            delta += 7 if delta <= 7 else 0
        return today + timedelta(days=delta)
    # 'Month D' or 'D Month' (+ optional ordinal / year)
    mon = "|".join(_CAL_MONTHS)
    month = day = None
    yr = None
    m = re.search(r'\b(' + mon + r')\s+(\d{1,2})(?:st|nd|rd|th)?(?:\s*,?\s*(\d{4}))?\b', t)
    if m:
        month, day, yr = _CAL_MONTHS[m.group(1)], int(m.group(2)), m.group(3)
    else:
        m = re.search(r'\b(\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?(' + mon + r')(?:\s*,?\s*(\d{4}))?\b', t)
        if m:
            day, month, yr = int(m.group(1)), _CAL_MONTHS[m.group(2)], m.group(3)
    if month and day:
        try:
            year = int(yr) if yr else today.year
            d = _cdate(year, month, day)
            if not yr and d < today:
                d = _cdate(year + 1, month, day)
            return d
        except ValueError:
            pass
    # 'the Nth' (this month, else next month)
    m = re.search(r'\bthe (\d{1,2})(?:st|nd|rd|th)\b', t)
    if m:
        day = int(m.group(1))
        try:
            d = today.replace(day=day)
        except ValueError:
            return None
        if d < today:
            d = (today.replace(day=1) + timedelta(days=32)).replace(day=day)
        return d
    # DD/MM or DD/MM/YY(YY)
    m = re.search(r'\b(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\b', t)
    if m:
        day, month, yr = int(m.group(1)), int(m.group(2)), m.group(3)
        try:
            year = today.year
            if yr:
                year = int(yr) + (2000 if len(yr) == 2 else 0)
            d = _cdate(year, month, day)
            if not yr and d < today:
                d = _cdate(year + 1, month, day)
            return d
        except ValueError:
            return None
    return None

def _cal_fmt_dt(dt, all_day):
    if all_day:
        d = dt if isinstance(dt, _cdate) and not isinstance(dt, datetime) else dt.date()
        return d.strftime("%a %-d %b") + " (all day)"
    return dt.strftime("%a %-d %b, %-I:%M%p").replace("AM", "am").replace("PM", "pm")

# ---- intent parsing --------------------------------------------------------
_CAL_CREATE_RE = re.compile(
    r'^\s*(?:can you\s+|please\s+|could you\s+)?'
    r'(?:add|create|schedule|put|set\s?up|book|new)\s+(?P<rest>.+)$', re.I)
_CAL_MOVE_RE = re.compile(
    r'^\s*(?:can you\s+|please\s+)?(?:move|reschedule|shift|push|change)\s+'
    r'(?:my|the)?\s*(?P<query>.+?)\s+(?:to|->|→|until)\s+(?P<when>.+)$', re.I)
_CAL_DELETE_RE = re.compile(
    r'^\s*(?:can you\s+|please\s+)?(?:delete|cancel|remove|drop|clear)\s+'
    r'(?:my|the)?\s*(?P<query>.+?)(?:\s+from\s+(?:my\s+)?calendar)?\s*$', re.I)
# Where the "when" part of a create begins — split title from time here.
_CAL_WHEN_MARKER = re.compile(
    r'\b(?:on|at|from|tomorrow|tmr|tmrw|tonight|today|this (?:morning|afternoon|evening|arvo)|'
    r'next|in \d+ days?|in a week|next week|' + "|".join(_CAL_WEEKDAYS) + r'|'
    r'\d{1,2}(?::\d{2})?\s*(?:a\.?m\.?|p\.?m\.?)|\d{1,2}:\d{2}|noon|midday|midnight|'
    r'(?:' + "|".join(_CAL_MONTHS) + r')\s+\d{1,2}|\d{1,2}(?:st|nd|rd|th)|\d{1,2}/\d{1,2})\b', re.I)

def _cal_strip_title(raw):
    """Trim leading 'a/an/my/the [calendar] event/appointment/meeting called/for/:' filler
    off a create title, and surrounding quotes/punct."""
    s = raw.strip()
    s = re.sub(r'^(?:a|an|my|the)\s+', "", s, flags=re.I)
    s = re.sub(r'^(?:calendar\s+)?(?:event|appointment|appt|meeting|booking|reservation)\s+'
               r'(?:called|titled|named|for|about|:)?\s*', "", s, flags=re.I)
    s = re.sub(r'^(?:called|titled|named|for|about|to|:)\s+', "", s, flags=re.I)
    return s.strip().strip('"\'').strip(" :,-")

def _cal_parse_create(rest):
    """rest = text after the create verb. Split into (title, when_text). The when starts at the
    first temporal marker; everything before is the title. Returns (title, when_text) or
    (title, '') when no time phrase is present (caller asks for a time)."""
    m = _CAL_WHEN_MARKER.search(rest)
    if m and m.start() > 0:
        title = _cal_strip_title(rest[:m.start()])
        when = rest[m.start():].strip()
    else:
        title, when = _cal_strip_title(rest), ""
    return title, when

def _cal_when_to_dt(when_text, base_date=None, base_time=None):
    """Resolve a when-phrase into (start_datetime_or_date, all_day). base_date/base_time supply
    the missing half on a move (keep the event's existing date or time-of-day). Returns
    (None, False) if nothing usable parsed."""
    d = _cal_parse_date(when_text)
    t = _cal_parse_time(when_text)
    if d is None and t is None:
        return (None, False)
    if d is None:
        d = base_date or _cal_now().date()
    if t is None:
        if base_time is not None:
            t = base_time
        else:
            return (d, True)      # date only -> all-day event
    return (datetime.combine(d, t, tzinfo=LOCAL_TZ), False)

# S84: strip calendar filler from a move/delete target query. "delete my dentist APPOINTMENT" /
# "cancel the 3pm MEETING" carry a trailing type-word that is NOT part of the event's title, which
# broke the substring match (same class as the S81 _clean_edit_name bug). Also drops a leading
# my/the/a and a trailing "from (my) calendar".
_CAL_TYPEWORD_RE = re.compile(
    r'\s+(?:events?|appointments?|appts?|meetings?|bookings?|reservations?|calendar)\s*$', re.I)

def _cal_clean_query(q):
    q = (q or "").strip().strip('"\'').strip()
    q = re.sub(r'^(?:my|the|a|an)\s+', "", q, flags=re.I)
    q = re.sub(r'\s+from\s+(?:my\s+)?calendar\s*$', "", q, flags=re.I)
    prev = None
    while prev != q:                 # peel repeated trailing type-words ("test event" -> "test")
        prev = q
        q = _CAL_TYPEWORD_RE.sub("", q).strip()
    return q.strip(" .,!?:-")

async def _cal_resolve_target(query):
    """Find upcoming events (now -> +90d) matching a free-text query for move/delete. Returns
    (list_of_matches, note). Cleans the query of calendar filler, then matches forgivingly:
    substring either way, or all query tokens present in the summary. A named query with no match
    returns [] (caller says 'no match'); a bare/empty query returns all (caller asks which)."""
    now = _cal_now()
    cleaned = _cal_clean_query(query)
    events = await gcal.get_events_in_window(now, now + timedelta(days=90), query=(cleaned or None))
    if events is None:
        return (None, "unreachable")
    q = cleaned.lower()
    if not q:
        return (events, "")          # e.g. "delete my appointment" — nothing to match on, ask which
    def _match(e):
        s = (e["summary"] or "").lower()
        if q in s or s in q:
            return True
        toks = [t for t in re.split(r"\s+", q) if t]
        return bool(toks) and all(t in s for t in toks)
    return ([e for e in events if _match(e)], "")

async def handle_calendar_write(user_id: int, user_message: str, source: str):
    """Confirm-first calendar create/move/delete. Turn 1 parses + echoes exactly what will
    change and stores a pending action; turn 2's explicit yes commits (no scraps it). Ambiguous
    targets ask which rather than guessing. Chat-triggered only. Returns a reply, or None."""
    pend_key = f"pending_calendar_write:{user_id}"
    r = Redis(host=REDIS_HOST, port=6379, password=REDIS_PASSWORD, decode_responses=True)
    try:
        pending = await r.get(pend_key)
        if pending:
            if _vw_affirmative(user_message):
                await r.delete(pend_key)
                return await _cal_commit(json.loads(pending))
            if _vw_negative(user_message):
                await r.delete(pend_key)
                return "Left the calendar as it was."
            await r.delete(pend_key)      # neither yes nor no -> no landmine
        # detect intent (require a calendar signal word to enter the write path at all)
        if not _CAL_SIGNAL_RE.search(user_message):
            return None
        if not gcal.is_configured():
            # only speak up once we're sure this was a calendar-write request
            if (_CAL_MOVE_RE.match(user_message) or _CAL_DELETE_RE.match(user_message)
                    or _CAL_CREATE_RE.match(user_message)):
                return ("Calendar isn't connected yet — the one-time Google sign-in still needs "
                        "running before I can add or change anything.")
            return None

        mmove = _CAL_MOVE_RE.match(user_message)
        mdel = _CAL_DELETE_RE.match(user_message)
        mnew = _CAL_CREATE_RE.match(user_message)

        if mmove:
            query = mmove.group("query").strip().strip('"\'')
            matches, note = await _cal_resolve_target(query)
            if note == "unreachable":
                return "Couldn't reach your calendar just now to find that — try again in a moment."
            if not matches:
                return f"No upcoming event matching '{query}'. Nothing moved."
            if len(matches) > 1:
                opts = "; ".join(f"{e['summary']} ({_cal_fmt_dt(e['start'], e['all_day'])})" for e in matches[:6])
                return f"More than one matches '{query}' — which: {opts}?"
            ev = matches[0]
            base_date = ev["start"].date() if isinstance(ev["start"], datetime) else ev["start"]
            base_time = ev["start"].time() if isinstance(ev["start"], datetime) else None
            new_start, all_day = _cal_when_to_dt(mmove.group("when"), base_date, base_time)
            if new_start is None:
                return "I couldn't work out the new time from that — say it like 'to Friday at 3pm'."
            # preserve duration
            if isinstance(ev["start"], datetime) and isinstance(ev["end"], datetime) and not all_day:
                dur = ev["end"] - ev["start"]
                new_end = new_start + (dur if dur.total_seconds() > 0 else timedelta(minutes=_CAL_DEFAULT_MINUTES))
            elif all_day:
                new_end = (new_start + timedelta(days=1))
            else:
                new_end = new_start + timedelta(minutes=_CAL_DEFAULT_MINUTES)
            payload = {"action": "move", "event_id": ev["id"], "summary": ev["summary"],
                       "start": new_start.isoformat(), "end": new_end.isoformat(), "all_day": all_day}
            await r.set(pend_key, json.dumps(payload), ex=900)
            return (f"Ready to move '{ev['summary']}' to {_cal_fmt_dt(new_start, all_day)}. "
                    "Confirm? (yes to move, no to leave it)")

        if mdel:
            query = mdel.group("query").strip().strip('"\'')
            matches, note = await _cal_resolve_target(query)
            if note == "unreachable":
                return "Couldn't reach your calendar just now to find that — try again in a moment."
            if not matches:
                return f"No upcoming event matching '{query}'. Nothing deleted."
            if len(matches) > 1:
                opts = "; ".join(f"{e['summary']} ({_cal_fmt_dt(e['start'], e['all_day'])})" for e in matches[:6])
                return f"More than one matches '{query}' — which do you want gone: {opts}?"
            ev = matches[0]
            payload = {"action": "delete", "event_id": ev["id"], "summary": ev["summary"],
                       "start": ev["start"].isoformat() if ev["start"] else "", "all_day": ev["all_day"]}
            await r.set(pend_key, json.dumps(payload), ex=900)
            return (f"Ready to delete '{ev['summary']}' ({_cal_fmt_dt(ev['start'], ev['all_day'])}). "
                    "Confirm? (yes to delete, no to keep it)")

        if mnew:
            title, when_text = _cal_parse_create(mnew.group("rest"))
            if not title:
                return "What should I call the event?"
            if not when_text:
                return f"When is '{title}'? Give me a day and time and I'll read it back before adding it."
            start, all_day = _cal_when_to_dt(when_text)
            if start is None:
                return f"I couldn't parse a date/time for '{title}' from that — try 'tomorrow at 3pm'."
            if all_day:
                end = start + timedelta(days=1)
            else:
                # 'until/to <time>' end?
                mend = re.search(r'\b(?:until|to|till|-)\s+(.+)$', when_text)
                end_t = _cal_parse_time(mend.group(1)) if mend else None
                end = datetime.combine(start.date(), end_t, tzinfo=LOCAL_TZ) if end_t else start + timedelta(minutes=_CAL_DEFAULT_MINUTES)
                if end <= start:
                    end = start + timedelta(minutes=_CAL_DEFAULT_MINUTES)
            payload = {"action": "create", "summary": title,
                       "start": (start.isoformat() if not all_day else start.isoformat()),
                       "end": end.isoformat(), "all_day": all_day}
            await r.set(pend_key, json.dumps(payload), ex=900)
            return (f"Ready to add '{title}' — {_cal_fmt_dt(start, all_day)}"
                    + ("" if all_day else f" to {end.strftime('%-I:%M%p').replace('AM','am').replace('PM','pm')}")
                    + ". Confirm? (yes to add, no to scrap)")
        return None
    finally:
        await r.aclose()

async def _cal_commit(payload: dict) -> str:
    """Execute a confirmed calendar action. Never raises; a failed API call reports plainly."""
    action = payload.get("action")
    try:
        if action == "create":
            all_day = payload["all_day"]
            start = (_cdate.fromisoformat(payload["start"][:10]) if all_day
                     else datetime.fromisoformat(payload["start"]))
            end = (_cdate.fromisoformat(payload["end"][:10]) if all_day
                   else datetime.fromisoformat(payload["end"]))
            ev = await gcal.create_event(payload["summary"], start, end, all_day=all_day)
            if not ev:
                return "That didn't take — the calendar write failed, so nothing was added."
            return f"Added '{ev['summary']}' — {_cal_fmt_dt(ev['start'], ev['all_day'])}."
        if action == "move":
            all_day = payload["all_day"]
            start = (_cdate.fromisoformat(payload["start"][:10]) if all_day
                     else datetime.fromisoformat(payload["start"]))
            end = (_cdate.fromisoformat(payload["end"][:10]) if all_day
                   else datetime.fromisoformat(payload["end"]))
            ev = await gcal.move_event(payload["event_id"], start, end, all_day=all_day)
            if not ev:
                return "That didn't take — the move failed, so the event's where it was."
            return f"Moved '{ev['summary']}' to {_cal_fmt_dt(ev['start'], ev['all_day'])}."
        if action == "delete":
            ok = await gcal.delete_event(payload["event_id"])
            if not ok:
                return "That didn't take — the delete failed, so the event's still there."
            return f"Deleted '{payload.get('summary', 'that event')}'."
    except Exception as e:
        logger.error(f"_cal_commit error: {e}")
        return "Something went wrong committing that calendar change — nothing was saved."
    return "Nothing to commit."

# ---- on-demand read --------------------------------------------------------
# S84: broadened to catch natural schedule phrasings that previously fell through to a generic
# reply (no calendar fetch, no logs — the exact silent-miss shape the S84 debug was about):
# "do I have anything today", "what have I got on today", "what does my day look like",
# "anything on tomorrow", "what am I doing today", "what's happening today".
_CAL_READ_RE = re.compile(
    r'(\bcalendar\b|\bschedule\b|\bagenda\b|\bam i free\b|\bwhat(?:\'s| is| do i have)\b[^?]*\b'
    r'(?:on|planned|coming up)\b|\bany (?:events|appointments|meetings|plans)\b|'
    r'\bwhen(?:\'s| is)\s+my\s+next\b|\bnext (?:appointment|meeting|event)\b|'
    r'\bwhat(?:\'s| is)\s+(?:on|coming up)\b|'
    r'\bdo i have (?:anything|much|any (?:plans|meetings|events|appointments))\b|'
    r'\banything (?:on|planned|scheduled)\b|'
    r'\bwhat have i got on\b|'
    r'\bwhat(?:\'s| is| does)\s+my day\b|'
    r'\bwhat am i (?:doing|up to)\s+(?:today|tomorrow|tonight|this|on|later)\b|'
    r'\bwhat(?:\'s| is)\s+happening\s+(?:today|tomorrow|tonight|this|on|later)\b)', re.I)

def _cal_read_window(text):
    """(start_dt, end_dt, label) for a read. Defaults to today; understands tomorrow, a named
    weekday, 'this week'/'next N days', and 'next X' (a wide lookahead)."""
    t = text.lower()
    now = _cal_now()
    today = now.date()
    if re.search(r'\bnext\b.*\b(appointment|meeting|event)\b|\bwhen(?:\'s| is)\s+my\s+next\b', t):
        return (now, now + timedelta(days=90), "coming up")
    if re.search(r'\btomorrow\b|\btmr\b|\btmrw\b', t):
        d = today + timedelta(days=1)
        return (datetime.combine(d, _ctime.min, tzinfo=LOCAL_TZ),
                datetime.combine(d, _ctime.min, tzinfo=LOCAL_TZ) + timedelta(days=1), "tomorrow")
    if re.search(r'\bthis week\b|\bnext (?:few )?days\b|\bweek\b', t):
        return (now, datetime.combine(today, _ctime.min, tzinfo=LOCAL_TZ) + timedelta(days=7), "this week")
    m = re.search(r'\b(next\s+)?(' + "|".join(_CAL_WEEKDAYS) + r')\b', t)
    if m:
        d = _cal_parse_date(m.group(0))
        if d:
            return (datetime.combine(d, _ctime.min, tzinfo=LOCAL_TZ),
                    datetime.combine(d, _ctime.min, tzinfo=LOCAL_TZ) + timedelta(days=1),
                    d.strftime("%A %-d %b"))
    # explicit date?
    d = _cal_parse_date(t)
    if d and not re.search(r'\btoday\b', t):
        return (datetime.combine(d, _ctime.min, tzinfo=LOCAL_TZ),
                datetime.combine(d, _ctime.min, tzinfo=LOCAL_TZ) + timedelta(days=1),
                d.strftime("%A %-d %b"))
    start = datetime.combine(today, _ctime.min, tzinfo=LOCAL_TZ)
    return (start, start + timedelta(days=1), "today")

async def handle_calendar_read(user_id: int, user_message: str):
    """On-demand calendar read (Telegram + dashboard). Resolves the window (today/tomorrow/
    weekday/this week / 'next X'), fetches events, and lets Claude answer from ONLY that list.
    Returns a reply, or None to fall through. Read-only."""
    if not _CAL_READ_RE.search(user_message):
        return None
    if not gcal.is_configured():
        return "Calendar isn't connected yet — the one-time Google sign-in still needs running."
    start, end, label = _cal_read_window(user_message)
    # 'next X' — pull a query term out for the lookahead
    query = None
    mnext = re.search(r'\bnext\s+([a-z][a-z ]*?)(?:\?|$)', user_message.lower())
    if mnext and mnext.group(1).strip() not in ("appointment", "meeting", "event", "week"):
        query = mnext.group(1).strip()
    events = await gcal.get_events_in_window(start, end, query=query)
    if events is None:
        return "Couldn't reach your calendar just now — try again in a moment."
    if not events:
        gap = f"[No events on the calendar for {label}" + (f" matching '{query}'" if query else "") + "]"
        return await call_claude(
            user_id, f"{user_message} {gap} — tell her plainly there's nothing on, don't invent anything.",
            route_text=user_message)
    lines = []
    for e in events[:20]:
        when = gcal.fmt_event(e)
        lines.append(f"- {when}")
    listing = "\n".join(lines)
    ctx = (f"[Calendar events for {label}" + (f" matching '{query}'" if query else "") +
           f" — answer from ONLY these, do not invent or add any:\n{listing}]")
    return await call_claude(user_id, f"{user_message} {ctx}", route_text=user_message)

# ---------------------------------------------------------------------------
# Capture routing (Session 40). A message starting with "capture" files the rest
# under a "## Captured via Ghost" section in the ONE project/goal note whose
# keyword matches (Admin/Ghost — Capture Routing.md is the Lea-editable table,
# re-read live each capture — same manifest pattern as Ghost — App Categories.md),
# or in Admin/Inbox.md when zero / more than one distinct note matches. APPEND-ONLY:
# existing note content is never edited or removed.
# ---------------------------------------------------------------------------
CAPTURE_ROUTING_REL = "Admin/Ghost — Capture Routing.md"
CAPTURE_INBOX_REL = "Admin/Inbox.md"
CAPTURE_SECTION = "## Captured via Ghost"
# "capture" as a leading word only (word-boundary lookahead excludes "captured"/
# "captures"); optional this/that; then any separators before the payload.
_CAPTURE_TRIGGER_RE = re.compile(
    r"^\s*capture(?![a-z])(?:\s+(?:this|that|the\s+following))?[\s:._\-–—]*", re.IGNORECASE)

def load_capture_routes() -> list:
    """Parse the '## Routes' section of the routing note into [(keywords, dest_rel), ...]
    with keywords lowercased. Live read every call; an unreadable/absent note yields []
    (everything then falls to Inbox) — never an error state."""
    routes = []
    try:
        text = open(os.path.join(VAULT_PATH, CAPTURE_ROUTING_REL), encoding="utf-8", errors="ignore").read()
    except OSError as e:
        logger.error(f"capture routing note unreadable ({e}) — captures route to Inbox")
        return routes
    in_routes = False
    for line in text.splitlines():
        if line.startswith("## "):
            in_routes = line.strip().lower().startswith("## routes")
            continue
        if not in_routes:
            continue
        m = re.match(r"\s*-\s*(.*?)=>\s*(.+?)\s*$", line)
        if not m:
            continue
        kws = [k.strip().lower() for k in re.findall(r"`([^`]+)`", m.group(1)) if k.strip()]
        dest = m.group(2).strip()
        if kws and dest:
            routes.append((kws, dest))
    return routes

_area_index_cache = {"ts": 0.0, "map": None}
AREA_INDEX_TTL = 300

def _area_note_map() -> dict:
    """Map of `area:` frontmatter value (lowercased) -> vault-relative note path, built by
    scanning the vault (cached AREA_INDEX_TTL). Lets capture routing resolve a destination by
    what the note IS, not where it sits, so a folder move (Goals/->Areas/) can't silently
    break routing. A `kind: area` note ALWAYS wins over a non-area note sharing the same area
    value (e.g. `area: Creative` on both Areas/Creative.md and a novel index — the Area note is
    the capture target); within a tier, first-in-walk wins."""
    now = time.time()
    if _area_index_cache["map"] is not None and now - _area_index_cache["ts"] < AREA_INDEX_TTL:
        return _area_index_cache["map"]
    area_notes = {}   # kind:area notes (authoritative)
    other = {}        # notes with an area: value but not kind:area (fallback, e.g. goals)
    for root, dirs, files in os.walk(VAULT_PATH):
        dirs[:] = [d for d in dirs if not d.startswith('.')]
        for fname in files:
            if not fname.endswith(".md"):
                continue
            path = os.path.join(root, fname)
            try:
                with open(path, encoding="utf-8", errors="ignore") as f:
                    if f.readline().strip() != "---":
                        continue
                    area = kind = None
                    for line in f:
                        if line.strip() == "---":
                            break
                        ma = re.match(r"\s*area\s*:\s*(.+?)\s*$", line)
                        if ma:
                            area = ma.group(1).strip().strip('"\'').lower()
                        mk = re.match(r"\s*kind\s*:\s*(.+?)\s*$", line)
                        if mk:
                            kind = mk.group(1).strip().strip('"\'').lower()
                    if area:
                        (area_notes if kind == "area" else other).setdefault(
                            area, os.path.relpath(path, VAULT_PATH))
            except OSError:
                continue
    amap = dict(other)
    amap.update(area_notes)   # kind:area wins over a same-named non-area note
    _area_index_cache["ts"] = now
    _area_index_cache["map"] = amap
    return amap

def _resolve_capture_dest(dest: str):
    """A route destination is either an `area:<Name>` reference (resolved via frontmatter to
    wherever that Area note lives now) or a literal vault-relative path (as before). Returns
    a real vault-relative path, or None if an area: dest can't be resolved."""
    if dest.lower().startswith("area:"):
        return _area_note_map().get(dest[5:].strip().lower())
    return dest

def _append_under_section(note_rel: str, line: str, section: str = CAPTURE_SECTION) -> bool:
    """Append `line` (a full markdown list item) under `section` in a vault note,
    creating the section at end-of-note if absent. APPEND-ONLY — never edits or removes
    existing lines. Preserves the file's original owner/mode: the bot pod runs as root,
    and writing must not leave the note root-owned and lock Lea out of her own vault."""
    path = os.path.join(VAULT_PATH, note_rel)
    try:
        try:
            st = os.stat(path)
        except FileNotFoundError:
            st = None
        content = ""
        if st is not None:
            with open(path, encoding="utf-8", errors="ignore") as f:
                content = f.read()
        lines = content.split("\n")
        hidx = next((i for i, l in enumerate(lines)
                     if l.strip().lower() == section.strip().lower()), None)
        if hidx is None:
            body = content.rstrip("\n")
            new = (body + "\n\n" if body else "") + section + "\n\n" + line + "\n"
        else:
            end = len(lines)
            for j in range(hidx + 1, len(lines)):
                s = lines[j].lstrip()
                if s.startswith("# ") or s.startswith("## "):
                    end = j
                    break
            k = end - 1
            while k > hidx and lines[k].strip() == "":
                k -= 1
            lines.insert(k + 1, line)
            new = "\n".join(lines)
        tmp = path + ".captmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(new)
        os.replace(tmp, path)
        if st is not None:
            try:
                os.chown(path, st.st_uid, st.st_gid)
                os.chmod(path, st.st_mode)
            except OSError as e:
                logger.error(f"capture chown/chmod back failed on {note_rel}: {e}")
        return True
    except OSError as e:
        logger.error(f"_append_under_section error on {note_rel}: {e}")
        return False

async def handle_capture(user_id: int, user_message: str) -> str | None:
    """Last-in-precedence trigger: a message starting with 'capture' files the rest
    under the routed project/goal note (exactly one DISTINCT keyword-matched note) or
    Admin/Inbox.md (zero, or more than one — never guesses). Returns a one-line
    confirmation naming where it landed, or None if this isn't a capture message."""
    m = _CAPTURE_TRIGGER_RE.match(user_message or "")
    if not m:
        return None
    payload = user_message[m.end():].strip()
    if not payload:
        return None  # bare "capture" with nothing to file — let normal chat handle it
    low = payload.lower()
    matched = []  # distinct destination notes (raw route dests), in note order
    for kws, dest in load_capture_routes():
        if dest not in matched and any(kw in low for kw in kws):
            matched.append(dest)
    now = datetime.now(LOCAL_TZ)
    entry = f"- {now.strftime('%Y-%m-%d %H:%M')} — {payload}"
    # Resolve each raw dest (area: refs -> real path via frontmatter; literal paths unchanged).
    # Only destinations that resolve to a real note count; an area: that can't resolve is dropped.
    resolved = [(_resolve_capture_dest(d), d) for d in matched]
    good = [r for (r, raw) in resolved if r]
    if len(good) == 1:
        where = good[0] if _append_under_section(good[0], entry) else None
        single = True
    else:
        where = CAPTURE_INBOX_REL if _append_under_section(CAPTURE_INBOX_REL, entry) else None
        single = False
    if not where:
        reply = "Tried to capture that but the vault write failed — nothing saved, check the logs."
    elif single:
        reply = f"Captured to {os.path.basename(where)[:-3]} — under Captured via Ghost."
    elif not matched:
        reply = "No single project matched, so it's in Inbox for you to triage."
    elif not good:
        reply = "That matched a route but I couldn't resolve its destination note, so it's in Inbox."
    else:
        names = ", ".join(os.path.basename(r)[:-3] for r in good)
        reply = f"That matched more than one ({names}), so I put it in Inbox rather than guess."
    logger.info(f"Capture: matched={matched} resolved={good} -> {where}")
    return reply

# Session 41 (fabrication audit): a retrospective QUESTION ("did the $50 get saved?",
# "you marked yoga done, right?", "did you add milk to my list?") must never TRIGGER
# the action it asks about. Without this, the money word / completion word / "add X to
# list" phrasing inside the question fires the real write — the S11 France-trip class of
# bug (a question mis-parsed as an action), re-surfaced by the audit when "you already
# saved the $50 ...?" logged a phantom $50 expense. Guards the write-triggering paths;
# the answer then comes from real records / board state (grounded), never a phantom write.
_RETRO_CUES = ("did you", "didn't you", "did i", "have you", "haven't you", "have i",
               "already", "right?", " yet?", "is it done", "was it", "how much",
               "did that", "is that", "is it saved", "get saved", "got saved",
               "did it", "has it", "do i still", "already saved", "already logged",
               "already added", "already marked", "already done")

def _is_retrospective_query(msg: str) -> bool:
    """True if the message is a question ASKING whether an action already happened, as
    opposed to a request/statement to DO it. Only such retrospective questions are
    guarded — forward requests ('add X to my list', 'log $20 for lunch') still write."""
    low = (msg or "").strip().lower()
    if not low.endswith("?"):
        return False
    return any(cue in low for cue in _RETRO_CUES)

async def get_vault_context() -> str:
    """Always-on background context from the vault: recently modified notes in
    Projects/, Areas/ — frontmatter + first 300 chars each, newest first,
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
    for sub in ("Projects", "Areas"):
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
                OLLAMA_URL,
                json={
                    # Session 27 (30/07/2026): CPU-only + the 1b model. Diagnosed that
                    # offloading 9/29 layers to the GeForce 940MX over the NVK/Vulkan
                    # path (no CUDA driver) was the bottleneck — ~14-16x SLOWER than
                    # CPU-only on this host, with the CPU sitting ~80% idle waiting on
                    # the GPU. num_gpu:0 forces CPU-only; 1b (already installed) is the
                    # fastest here (~11s, ~12 tok/s vs the old ~56-84s / 0.49 tok/s).
                    "model": "llama3.2:1b",
                    "prompt": ollama_prompt,
                    "stream": False,
                    "keep_alive": "1h",
                    "options": {
                        "temperature": 0.7,
                        "num_gpu": 0,
                        # 40 is plenty for "one or two lines".
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

    if len(history) > 30:
        history = history[-30:]

    yesterday = await get_yesterday_summary()
    today_context = await get_today_context()
    board_context = await get_board_context()
    native_context = await get_native_context(user_id)   # S66: live native Habits & Todos state
    pattern_context = await get_pattern_context()
    system_truth = await get_system_truth_context()   # S58: integrations + cephalopod, one block
    recent_phrases = await get_recent_phrases()
    vault_ctx = await get_vault_context()
    system_with_context = SYSTEM_PROMPT + "\n\n" + today_context
    if system_truth:
        system_with_context += "\n\n" + system_truth
    if native_context:
        system_with_context += "\n\n" + native_context
    if board_context:
        system_with_context += "\n\n" + board_context
    if pattern_context:
        system_with_context += "\n\n" + pattern_context
    memory_context = await get_memory_context(user_id)
    if memory_context:
        system_with_context += "\n\n" + memory_context
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
    message_lower = router_text.lower()
    # S70: the conversational money-logging pipeline (Session 52) is REMOVED. Financial tracking is
    # CSV-only now (upload on the Bills dashboard → pandas → local Ollama narration, which never
    # touches this Claude path). A money mention typed in chat is no longer special-cased; it just
    # reaches Claude like any other message. Rationale: the old "reroute to Ollama" only protected
    # the processing step — Ghost's chat brain IS Claude, so a financial mention was already read by
    # Claude the moment it was typed, before any reroute. The CSV pipeline has no such boundary leak.

    vault_triggers = ["obsidian", "my notes", "my vault", "what did i write", "check my notes",
                      "character", "who is", "tell me about", "novel", "check my vault",
                      "search my", "find my note", "find the note", "look up my"]
    if any(t in message_lower for t in vault_triggers):
        logger.info("Vault query detected")
        matches = vault_search_matches(router_text)
        if not matches:
            vault_context = "No matching notes found in the vault."
        else:
            vault_context = "\n\n".join(f"--- {m['path']} ---\n{m['excerpt']}" for m in matches[:3])
            # S80 Task 1: on multiple plausibly-close matches, tell Claude to ASK which one she
            # means rather than answering from a guessed note.
            if len(matches) > 1 and matches[1]["score"] >= 0.6 * matches[0]["score"]:
                names = ", ".join(m["path"] for m in matches[:4])
                vault_context = (f"[MULTIPLE notes plausibly match this — ASK which one she means "
                                 f"before answering, listing them: {names}]\n\n" + vault_context)
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

    return await _finish(await call_claude_api(user_id, message), "claude", False)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Ghost online. I'm watching.")

SCROLL_SUPPRESS_KEY = "scroll_suppressed_until"
SCROLL_SUPPRESS_DEFAULT_MIN = 45
SCROLL_SUPPRESS_MAX_MIN = 180

# Budget caps over chat ("set groceries cap to 400", "remove the travel cap", "my caps", "cap warning
# at 75"). Strict patterns that all need the word "cap", so ordinary chat never matches. Handled here
# with no model: the dashboard owns the caps (POST /api/bills/category/cap), this only parses and
# relays. process_message does NOT write these to the messages table, which feeds Claude context.
DASHBOARD_INTERNAL_URL = os.environ.get("DASHBOARD_INTERNAL_URL", "http://ghost-dashboard.ghost.svc.cluster.local:8080")
_AMT = r"\$?\s*(\d[\d,]*(?:\.\d{1,2})?)"
_CAP_SET_RES = (
    re.compile(r"^(?:please\s+)?(?:set\s+)?(?:my\s+|the\s+)?(.+?)\s+(?:budget\s+|monthly\s+)?cap\s*(?:to|at|=|of|:)?\s*" + _AMT + r"(?:\s*(?:a|per|/)\s*month)?$", re.I),
    re.compile(r"^(?:please\s+)?cap\s+(?:my\s+|the\s+)?(.+?)\s+(?:at|to)\s+" + _AMT + r"(?:\s*(?:a|per|/)\s*month)?$", re.I),
    re.compile(r"^(?:please\s+)?set\s+(?:my\s+|the\s+)?(?:budget\s+|monthly\s+)?cap\s+(?:for|on)\s+(?:my\s+|the\s+)?(.+?)\s+(?:to|at)\s+" + _AMT + r"(?:\s*(?:a|per|/)\s*month)?$", re.I),
)
_CAP_CLEAR_RE = re.compile(r"^(?:please\s+)?(?:remove|clear|delete|drop|turn\s+off)\s+(?:my\s+|the\s+)?(.+?)\s+(?:budget\s+)?cap$", re.I)
_CAP_LIST_RE = re.compile(r"^(?:show\s+)?(?:my\s+)?(?:budget\s+)?caps\??$|^how\s+are\s+my\s+(?:budget\s+)?caps(?:\s+going)?\??$", re.I)
_CAP_WARN_RE = re.compile(r"^(?:set\s+)?(?:the\s+)?cap\s+warning\s*(?:to|at|=)?\s*(\d{1,2})\s*%?$", re.I)


def parse_cap_command(text: str):
    """-> ('set', name, amount) | ('clear', name) | ('list',) | ('warn', pct) | None."""
    t = re.sub(r"[.!]+$", "", (text or "").strip())
    if len(t) > 80 or "cap" not in t.lower():
        return None
    m = _CAP_WARN_RE.match(t)
    if m:
        return ("warn", int(m.group(1)))
    if _CAP_LIST_RE.match(t):
        return ("list",)
    m = _CAP_CLEAR_RE.match(t)
    if m:
        return ("clear", m.group(1).strip())
    for rx in _CAP_SET_RES:
        m = rx.match(t)
        if m:
            return ("set", m.group(1).strip(), float(m.group(2).replace(",", "")))
    return None


async def handle_budget_cap_command(user_message: str) -> str | None:
    cmd = parse_cap_command(user_message)
    if cmd is None:
        return None
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            if cmd[0] == "list":
                d = (await client.get(f"{DASHBOARD_INTERNAL_URL}/api/bills/caps-status")).json()
                items = d.get("items") or []
                if not items:
                    return "No categories have capping switched on."
                lines = []
                for it in items:
                    if it["status"] == "unset":
                        lines.append(f"- {it['label']}: no cap set yet")
                    else:
                        lines.append(f"- {it['label']}: {round(it['pct'])}% of ${it['cap']:,.0f}")
                through = f" (spending up to {d['data_through']})" if d.get("data_through") else ""
                return "Caps this month" + through + ":\n" + "\n".join(lines)
            if cmd[0] == "warn":
                d = (await client.post(f"{DASHBOARD_INTERNAL_URL}/api/bills/cap-settings",
                                       json={"warn_pct": cmd[1]})).json()
                return (f"Cap heads-up now fires at {cmd[1]}%, and again at 100%." if d.get("ok")
                        else f"Couldn't change that: {d.get('error', 'unknown error')}.")
            amount = cmd[2] if cmd[0] == "set" else None
            resp = await client.post(f"{DASHBOARD_INTERNAL_URL}/api/bills/category/cap",
                                     json={"name": cmd[1], "monthly_cap": amount})
            d = resp.json()
    except Exception as e:
        logger.error(f"budget cap command failed: {e}")
        return "Couldn't reach the Bills service to change that cap, so nothing changed. Try the Bills page."
    if resp.status_code == 409:
        return f"Which one did you mean: {', '.join(d.get('candidates') or [])}?"
    if resp.status_code == 404:
        return f"There's no category called \"{cmd[1]}\". Check the names on the Bills page."
    if not d.get("ok"):
        return f"Couldn't change that cap: {d.get('error', 'unknown error')}."
    if cmd[0] == "clear":
        return f"Cap removed for {d['label']}."
    return f"Cap set: {d['label']}, ${d['monthly_cap']:,.2f} a month. You'll get a heads-up as it gets close."


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

# ---------------------------------------------------------------------------
# Commitment tracking (session 19). DELIBERATELY NARROW.
#
# Scope guards, in order of how easy they'd be to erode:
#  - It only ever reads messages she ALREADY sends to Ghost. No new listening
#    surface, no passive monitoring, no open mic. Same input as everything else.
#  - It is NOT a second to-do list. Explicit task language ("add X to my list",
#    "remind me to X", "need to X") still belongs to Habitica and is skipped
#    here; this only claims CASUAL self-declared intent ("I'll ...").
#  - Nothing is logged without her say-so. Detection asks; only "yes" writes.
#  - One pending confirmation at a time, so it can't interrogate her in a loop.
#  - Everything expires. Nothing accumulates forever.
COMMITMENT_PENDING_EXPIRY = timedelta(hours=1)   # unanswered "is this a commitment?" -> drop it
COMMITMENT_ACTIVE_EXPIRY = timedelta(days=14)    # tracked commitment goes stale -> drop it
COMMITMENT_MAX_LEN = 140                         # long messages are conversation, not a commitment

# Casual intent only. "need to"/"remind me to"/"add to list" are Habitica's, not ours.
COMMITMENT_PATTERN = r"^\s*(?:(?:so|ok(?:ay)?|and|then|right|well|anyway)[,\s]+)*i(?:'ll|\s+will|'m\s+going\s+to|\s+am\s+going\s+to|'m\s+gonna|\s+am\s+gonna)\s+(.+)"

async def _commitments_conn():
    return await asyncpg.connect(host=POSTGRES_HOST, port=5432, user="postgres",
                                 password=POSTGRES_PASSWORD, database="ghost_db")

def detect_commitment(user_message: str) -> str | None:
    """Extract a self-declared future intention, or None. Conservative by design:
    short messages only, casual first-person intent only, and never phrasing that
    the Habitica task-capture path already owns."""
    import re as _re
    if len(user_message) > COMMITMENT_MAX_LEN:
        return None
    low = user_message.lower()
    # Hands off anything Habitica's capture already claims.
    if (_re.search(r'\badd (.+?) to (?:my|the) list\b', low)
            or _re.match(r'\s*remind me to ', low)
            or _re.match(r"\s*(?:i )?need to ", low)):
        return None
    m = _re.match(COMMITMENT_PATTERN, user_message, _re.IGNORECASE | _re.DOTALL)
    if not m:
        return None
    text = m.group(1).strip(" .,!?")
    # Filter out present-tense chatter that only looks like intent.
    if len(text) < 4 or text.lower().startswith(("be ", "have ", "feel ", "think ", "guess ")):
        return None
    return text

async def handle_commitment(user_id: int, user_message: str, source: str) -> str | None:
    """Two jobs: resolve an open confirmation, or ask for one. Returns a reply
    when it handled the turn, else None so the normal pipeline continues."""
    import re as _re
    low = user_message.strip().lower()
    try:
        conn = await _commitments_conn()
        try:
            pending = await conn.fetchrow(
                "SELECT id, text FROM commitments WHERE status = 'pending_confirmation' "
                "AND expires_at > NOW() ORDER BY id DESC LIMIT 1")
            # 1. Resolve an open confirmation on a short yes/no.
            if pending and len(low) <= 24:
                if _re.match(r"^(yes|yep|yeah|yup|ok(ay)?|sure|do it|please|go on|correct)\b[.!]?$", low):
                    await conn.execute(
                        "UPDATE commitments SET status = 'active', confirmed_at = NOW(), expires_at = $2 "
                        "WHERE id = $1", pending["id"], datetime.now(LOCAL_TZ) + COMMITMENT_ACTIVE_EXPIRY)
                    logger.info(f"Commitment #{pending['id']} confirmed -> active")
                    # Wording states exactly what exists: it appears in the evening
                    # summary and expires. There is no completion-marking path, so
                    # this must not imply one.
                    return (f"Tracked: {pending['text']}. I'll surface it in your evening "
                            f"summary, and it drops off on its own after "
                            f"{COMMITMENT_ACTIVE_EXPIRY.days} days.")
                if _re.match(r"^(no|nope|nah|don't|dont|skip it|leave it)\b[.!]?$", low):
                    await conn.execute("UPDATE commitments SET status = 'declined' WHERE id = $1",
                                       pending["id"])
                    logger.info(f"Commitment #{pending['id']} declined")
                    return "Fine, not tracking it."
            if pending:
                return None  # one at a time — don't stack another question on her
            # 2. New detection -> ASK, never log silently.
            text = detect_commitment(user_message)
            if not text:
                return None
            row = await conn.fetchrow(
                "INSERT INTO commitments (user_id, text, status, source, expires_at) "
                "VALUES ($1, $2, 'pending_confirmation', $3, $4) RETURNING id",
                user_id, text, source, datetime.now(LOCAL_TZ) + COMMITMENT_PENDING_EXPIRY)
            logger.info(f"Commitment #{row['id']} detected, awaiting confirmation: {text[:60]!r}")
            return f"Sounds like you're committing to {text} — want me to track that and check in?"
        finally:
            await conn.close()
    except Exception as e:
        logger.error(f"Commitment handling error: {e}")
        return None

async def get_active_commitments() -> list:
    """Active, unexpired commitments — used by the evening summary so 'I'll check
    in' is a real promise and not a fabricated one."""
    try:
        conn = await _commitments_conn()
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

# Session 43: when Lea asks whether an integration is up/down, run a REAL live check and
# ground the answer in it — the model must not guess "it's out" (the 20/08 incident). Returns
# a grounded reply, or None if this isn't a status question (normal routing continues).
_STATUS_TOKENS = ("working", "down", "offline", "online", "reachable", "unreachable",
                  "broken", "disconnect", "connected", "connection", "responding",
                  "outage", "reach", " out", "is it up", "still up", "still working", "alive")

# Declarative down-assertions (Session 52) — a statement claiming an integration is down,
# not a question. Paired with a named integration so "I'm down for that" style phrasings
# don't trip it; and even a false positive only yields a grounded status check, never a write.
_DOWN_CLAIM_PHRASES = ("is down", "'s down", "is offline", "is broken", "is out",
                       "went down", "went out", "not working", "isn't working", "isnt working",
                       "not updating", "isn't updating", "isnt updating", "not responding",
                       "not syncing", "isn't syncing", "stopped working", "keeps failing",
                       "is failing", "won't connect", "wont connect", "can't connect",
                       "cant connect", "is unreachable", "not connecting", "is broken",
                       "has gone down", "seems down", "looks down", "must be down")
# Standalone strong down-words: paired with a named integration these fire even when an adverb
# splits a phrase ("habitica is completely down", "totally broken"). Bare "down" is safe here
# because a named integration is also required, and the worst case is a grounded status check.
_DOWN_WORDS = ("down", "offline", "broken", "unreachable", "borked", "dead")

async def handle_integration_status(user_id: int, user_message: str) -> str | None:
    low = user_message.lower().strip()
    named = [n for n in ("habitica", "fatsecret", "cephalopod") if n in low]
    is_q = low.endswith("?") or low.startswith(("is ", "are ", "can ", "have ", "do ", "does ",
                                                "how's", "hows", "did ", "you ", "your "))
    status_word = any(t in low for t in _STATUS_TOKENS)
    # the 20/08 phrasing: "I'm not seeing habitica updated" — a named integration + a
    # "not updating/updated" complaint is a status concern, so live-check rather than guess.
    neg_update = "updat" in low and any(x in low for x in ("not ", "n't", "nothing", "never", "isn"))
    # Session 52 (Task 2): also catch a VOLUNTEERED down-CLAIM ("habitica is down", "fatsecret
    # isn't working", "your connection to habitica is broken") — a statement, not a question,
    # which the question-only gate above missed (the S42/S43 class). A named integration plus a
    # down-assertion runs the same fresh live check. The Task-1 status line is the passive
    # backstop for everything else; this is the active fresh-check path when she asserts it.
    down_claim = bool(named) and (any(p in low for p in _DOWN_CLAIM_PHRASES)
                                  or any(w in low for w in _DOWN_WORDS))
    if not ((status_word and is_q) or (neg_update and named) or down_claim):
        return None
    targets = named
    if not targets and status_word and any(g in low for g in ("integration", "everything", "all your",
                                              "your systems", "all systems", "fully connected",
                                              "your connection", "connected to everything")):
        targets = ["habitica", "fatsecret"]   # the external WRITE integrations
    if not targets:
        return None
    results = []
    if "habitica" in targets:
        from habitica import health_check
        ok, detail = await health_check()
        results.append(f"Habitica = {'OK' if ok else 'UNREACHABLE'} ({detail})")
    if "fatsecret" in targets:
        import fatsecret
        results.append("FatSecret = " + ("configured/connected" if fatsecret.is_configured()
                                         else "NOT configured"))
    if "cephalopod" in targets:
        results.append("Cephalopod = a separate system; from Telegram you cannot reach or query it "
                       "(dashboard chat only), so you have no live status for it here")
    ctx = ("[Integration status — LIVE check just now: " + "; ".join(results) +
           ". Report exactly these results; do NOT call anything down/working beyond what's here.]")
    logger.info(f"Integration status query — live check: {results}")
    return await call_claude(user_id, f"{user_message} {ctx}", route_text=user_message)

# ===========================================================================
# LONG-TERM MEMORY (Session 48, 26/08/2026)
# ---------------------------------------------------------------------------
# Ghost had no durable per-user memory: a plain fact Lea stated lived only in the
# Redis conversation cache (20 msgs / 24h) and, although written to `messages`, was
# never read back — so it fell out of context within a day. (The apparent "recall"
# seen while testing was an artefact of the GLOBAL recent_ghost_phrases anti-repeat
# list echoing Ghost's own reply, not memory.) This adds a `memories` table + explicit
# capture ("remember X") + always-injected recall, so remembered facts persist
# indefinitely and are shown back to the model every turn.
#
# DATA BOUNDARY: memories are injected into the CLAUDE context, so financial content is
# deliberately NOT captured here — a "remember I spent $X" message carries a finance
# trigger and returns None from the handler, so it falls through to the Ollama-only
# money path instead. Obvious secrets (passwords/PINs/card numbers) are refused too.
# ===========================================================================

_MEMORY_SCHEMA_READY = False

async def _memory_conn():
    return await asyncpg.connect(host=POSTGRES_HOST, port=5432, user="postgres",
                                 password=POSTGRES_PASSWORD, database="ghost_db")

async def _ensure_memory_schema(conn):
    global _MEMORY_SCHEMA_READY
    if _MEMORY_SCHEMA_READY:
        return
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS memories (
            id           SERIAL PRIMARY KEY,
            user_id      BIGINT NOT NULL,
            content      TEXT NOT NULL,
            source       TEXT DEFAULT 'chat',
            created_at   TIMESTAMPTZ DEFAULT now(),
            last_used_at TIMESTAMPTZ,
            active       BOOLEAN NOT NULL DEFAULT TRUE
        )
    """)
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_memories_user_active ON memories (user_id, active)")
    _MEMORY_SCHEMA_READY = True

# Money words — a "remember ..." carrying any of these is NOT kept as a Claude-visible memory.
# (S70: the conversational money pipeline was removed, but this guard stays — it keeps financial
# mentions out of the durable Claude-context memory store regardless of how they arrive.)
_MEMORY_FINANCE_TRIGGERS = (
    "spent", "spend", "bought", "purchase", "cost", "price", "paid", "pay",
    "budget", "money", "dollar", "dollars", "cash", "bank", "account", "invoice",
    "receipt", "expense", "debt", "loan", "savings", "financial", "finance",
    "credit", "transfer", "withdraw", "deposit", "overdraft", "owe", "owing",
)
_SENSITIVE_RE = re.compile(r"\b(password|passcode|pin|card\s*number|cvv|social security|ssn)\b", re.IGNORECASE)
_LONG_DIGITS_RE = re.compile(r"\d[\d\s-]{5,}\d")  # 7+ digit runs = card/account/PIN-ish

def _memory_looks_sensitive(text: str) -> bool:
    return bool(_SENSITIVE_RE.search(text) or _LONG_DIGITS_RE.search(text))

async def add_memory(user_id: int, content: str, source: str = "chat"):
    content = (content or "").strip().rstrip(" .!,")
    if not content:
        return None
    try:
        conn = await _memory_conn()
        try:
            await _ensure_memory_schema(conn)
            existing = await conn.fetch(
                "SELECT content FROM memories WHERE user_id=$1 AND active=TRUE", user_id)
            for e in existing:
                if e["content"].strip().lower() == content.lower():
                    return "dup"
            row = await conn.fetchrow(
                "INSERT INTO memories (user_id, content, source) VALUES ($1,$2,$3) RETURNING id",
                user_id, content, source)
            return row["id"]
        finally:
            await conn.close()
    except Exception as e:
        logger.error(f"add_memory error: {e}")
        return None

async def get_memories(user_id: int) -> list:
    try:
        conn = await _memory_conn()
        try:
            await _ensure_memory_schema(conn)
            rows = await conn.fetch(
                "SELECT id, content FROM memories WHERE user_id=$1 AND active=TRUE ORDER BY created_at", user_id)
            return [dict(r) for r in rows]
        finally:
            await conn.close()
    except Exception as e:
        logger.error(f"get_memories error: {e}")
        return []

async def forget_memories(user_id: int, topic: str) -> list:
    topic = (topic or "").strip()
    if not topic:
        return []
    try:
        conn = await _memory_conn()
        try:
            await _ensure_memory_schema(conn)
            rows = await conn.fetch(
                "SELECT id, content FROM memories WHERE user_id=$1 AND active=TRUE AND content ILIKE $2",
                user_id, f"%{topic}%")
            for r in rows:
                await conn.execute("UPDATE memories SET active=FALSE WHERE id=$1", r["id"])
            return [r["content"] for r in rows]
        finally:
            await conn.close()
    except Exception as e:
        logger.error(f"forget_memories error: {e}")
        return []

async def get_memory_context(user_id: int) -> str:
    """Injected into every Claude turn so remembered facts are always in view."""
    mems = await get_memories(user_id)
    if not mems:
        return ""
    lines = "\n".join(f"- {m['content']}" for m in mems)
    return (
        f"LONG-TERM MEMORY — durable facts {USER_NAME} has told you to remember (persisted, not part "
        f"of this chat's short history). Treat these as true and current, use them naturally when "
        f"relevant, and never claim you don't remember something that appears here:\n" + lines
    )

_MEM_STORE_RE = re.compile(
    r"^\s*(?:please\s+)?(?:remember\s+that|remember|don'?t\s+forget\s+that|don'?t\s+forget|"
    r"keep\s+in\s+mind\s+that|keep\s+in\s+mind|for\s+the\s+record|for\s+future\s+reference|"
    r"note\s+to\s+self)[\s:,\-–—]+(.+)$", re.IGNORECASE | re.DOTALL)
_MEM_FORGET_RE = re.compile(
    r"^\s*(?:please\s+)?forget(?:\s+(?:that|about|what\s+i\s+said\s+about))?\s+(.+)$", re.IGNORECASE)
_MEM_LIST_RE = re.compile(
    r"(what\s+do\s+you\s+(?:remember|know)(?:\s+about\s+me)?|what\s+have\s+i\s+told\s+you\s+to\s+remember|"
    r"list\s+(?:your|my)\s+memor|show\s+(?:me\s+)?(?:your|my)\s+memor)", re.IGNORECASE)

async def handle_memory(user_id: int, user_message: str, source: str = "telegram") -> str | None:
    """Explicit long-term memory: store / forget / list. Returns a reply, or None to let
    the message route normally. Financial + sensitive content returns None (never stored),
    so the money-stays-on-Ollama boundary is preserved."""
    text = user_message.strip()
    low = text.lower()

    fm = _MEM_FORGET_RE.match(text)
    if fm and not low.endswith("?"):
        topic = fm.group(1).strip(" .!\"'")
        removed = await forget_memories(user_id, topic)
        if removed:
            head = "; ".join(removed[:5])
            more = f" (+{len(removed) - 5} more)" if len(removed) > 5 else ""
            return f"Forgotten: {head}{more}."
        return f"Nothing in my memory matched \"{topic}\"."

    if _MEM_LIST_RE.search(low):
        mems = await get_memories(user_id)
        if not mems:
            return "Nothing in long-term memory yet. Tell me to \"remember\" something and it sticks."
        return "Here's what I've got stored:\n" + "\n".join(f"• {m['content']}" for m in mems)

    sm = _MEM_STORE_RE.match(text)
    if sm:
        if low.endswith("?"):
            return None  # "remember when ...?" is a question, not an instruction
        content = sm.group(1).strip()
        if content.lower().startswith(("to ", "to,")):
            return None  # "remember to X" is a task/reminder, not a durable fact
        if any(t in low for t in _MEMORY_FINANCE_TRIGGERS):
            return None  # money stays on the Ollama-only path; never a Claude-visible memory
        if _memory_looks_sensitive(content):
            return ("I won't keep passwords, PINs or card numbers in memory — that shouldn't sit in a "
                    "chat log. Give me the non-sensitive version if there is one.")
        res = await add_memory(user_id, content, source)
        if res == "dup":
            return "Already had that one."
        if res:
            return "Got it, I'll remember that."
        return "Tried to save that to memory but the write failed — nothing stored, check the logs."

    return None


async def handle_native_create(user_id: int, user_message: str) -> str | None:
    """Ghost 2.0 (Phase 1) — native habit/todo CREATE, deterministic (no LLM).
    S66: widened from the old "create habit: X" syntax to ordinary conversation ("add a habit for
    stretching", "add my exercise habit and a todo to call the dentist", "track reading as a habit",
    "add X to my todos") via native.detect_native_create — still NOT poaching Habitica's own openers
    (those are guarded inside the detector), still failing CLOSED: a message that clearly signals
    creation intent but has no parseable name gets an honest "didn't catch the name" reply with the
    syntax that works, NEVER a silent fall-through to chat and never a fabricated confirmation.
    Runs early (before the ambient water/meal/movement block) so a habit named after an ambient
    signal ("drink water") can't score a real signal. Returns None only when there's no create intent
    at all. Shared by Telegram AND dashboard chat (both go through process_message)."""
    try:
        det = native.detect_native_create(user_message)
    except Exception as e:
        logger.error(f"native detect_native_create error: {e}")
        return None
    if det is None:
        return None   # no creation intent — fall through to the rest of the pipeline

    made, failed = [], []
    for c in det.get("creations", []):
        try:
            if c["kind"] == "habit":
                hid = await native.create_habit(c["name"], field_type=c.get("field_type", "tickbox"), user_id=user_id)
                (made if hid else failed).append(f"habit “{c['name']}”" + (f" ({native._field_label(c['field_type'])})" if hid else ""))
            else:
                tid = await native.create_todo(c["text"], priority=c.get("priority", "med"), user_id=user_id)
                (made if tid else failed).append(f"to-do “{c['text']}”")
        except Exception as e:
            logger.error(f"native create write error ({c}): {e}")
            failed.append(f"{c['kind']} “{c.get('name') or c.get('text')}”")

    if made:
        await _bust_native_cache()   # so a same-conversation "what habits do I have" sees the new item
        msg = "Added " + _join_list(made) + " to Ghost — it's on the Habits page now."
        if failed:
            msg += " But I couldn't save " + _join_list(failed) + " — try again?"
        return msg
    if det.get("needs_name") or failed:
        # creation intent detected but nothing usable parsed — ask, don't guess, don't stay silent
        return ("I caught that you want to add a habit or a to-do, but I didn't get the name. "
                "Try “add a habit for <thing>” or “add a to-do to <thing>” and I'll create it.")
    return None


def _join_list(items):
    items = list(items)
    if len(items) == 1:
        return items[0]
    if len(items) == 2:
        return items[0] + " and " + items[1]
    return ", ".join(items[:-1]) + " and " + items[-1]


async def process_message(user_id: int, user_message: str, source: str = "telegram") -> str:
    """Full Ghost pipeline — confirmation flags, daily events, write commands, routing.
    Shared by the Telegram handler and the dashboard chat."""
    _msg_source.set(source)
    # Session 41: stamp that Lea just interacted (any inbound, telegram or dashboard), so
    # the scheduler can hold proactive nudges while she's mid-conversation — the 10:58
    # "you've been glued to the chair" nudge fired 10 min into a live chat because nothing
    # told the scheduler she was present. Set first, before any early-return handler.
    try:
        _ract = Redis(host=REDIS_HOST, port=6379, password=REDIS_PASSWORD, decode_responses=True)
        await _ract.set("last_user_activity", datetime.now(LOCAL_TZ).timestamp(), ex=6 * 3600)
        await _ract.aclose()
    except Exception as e:
        logger.error(f"last_user_activity set error: {e}")
    deletion_reply = await handle_voice_note_deletion(user_id, user_message)
    if deletion_reply is not None:
        await save_to_postgres(user_id, "user", user_message)
        await save_to_postgres(user_id, "assistant", deletion_reply)
        return deletion_reply
    location_reply = await handle_location_override(user_message)
    if location_reply is not None:
        await save_to_postgres(user_id, "user", user_message)
        await save_to_postgres(user_id, "assistant", location_reply)
        return location_reply
    # Session 52 (Task 4): a QUESTION about Cephalopod handoffs is answered deterministically
    # (Telegram → the dashboard-only boundary; dashboard → a real ghost_handoffs read). Runs
    # BEFORE the send handler so a question can never trip the send path.
    cephalopod_query_reply = await handle_cephalopod_query(user_message, source)
    if cephalopod_query_reply is not None:
        await save_to_postgres(user_id, "user", user_message)
        await save_to_postgres(user_id, "assistant", cephalopod_query_reply)
        return cephalopod_query_reply

    cephalopod_reply = await handle_cephalopod_handoff(user_message, source)
    if cephalopod_reply is not None:
        await save_to_postgres(user_id, "user", user_message)
        await save_to_postgres(user_id, "assistant", cephalopod_reply)
        return cephalopod_reply

    # Budget caps: answered without a model and deliberately NOT saved to the messages table, which
    # feeds Claude context (get_yesterday_summary) — cap amounts stay out of anything Claude reads.
    cap_reply = await handle_budget_cap_command(user_message)
    if cap_reply is not None:
        return cap_reply

    scroll_reply = await handle_scroll_suppression(user_message)
    if scroll_reply is not None:
        await save_to_postgres(user_id, "user", user_message)
        await save_to_postgres(user_id, "assistant", scroll_reply)
        return scroll_reply

    # S80 phase 2: on-demand vault WRITE (append/create) with a same-turn confirmation handshake.
    # Runs early so a pending "yes" commits before other routing can eat it, and so a write request
    # never falls through to a generative reply. Chat-triggered only; nothing else reaches this.
    vault_write_reply = await handle_vault_write(user_id, user_message, source)
    if vault_write_reply is not None:
        await save_to_postgres(user_id, "user", user_message)
        await save_to_postgres(user_id, "assistant", vault_write_reply)
        return vault_write_reply

    # S80 phase 2: on-demand READ of a specific note (fuzzy name). After write so "add to note X"
    # never trips the read path; before native/finance so a note read isn't misrouted.
    vault_read_reply = await handle_vault_read(user_id, user_message)
    if vault_read_reply is not None:
        await save_to_postgres(user_id, "user", user_message)
        await save_to_postgres(user_id, "assistant", vault_read_reply)
        return vault_read_reply

    # S82: Google Calendar write (create/move/delete) with the same confirm-first handshake as
    # vault writes. Runs before the read so a pending "yes" commits, and before native/finance
    # so "schedule X" / "move my appointment" never falls through to a generative reply. A
    # calendar signal word is required to enter, so native/vault deletes aren't poached.
    calendar_write_reply = await handle_calendar_write(user_id, user_message, source)
    if calendar_write_reply is not None:
        await save_to_postgres(user_id, "user", user_message)
        await save_to_postgres(user_id, "assistant", calendar_write_reply)
        return calendar_write_reply

    # S82: on-demand calendar READ ("what's on tomorrow", "when's my next dentist"). After write
    # so a write request isn't eaten by the read path. Read-only.
    calendar_read_reply = await handle_calendar_read(user_id, user_message)
    if calendar_read_reply is not None:
        await save_to_postgres(user_id, "user", user_message)
        await save_to_postgres(user_id, "assistant", calendar_read_reply)
        return calendar_read_reply

    # Ghost 2.0 (Phase 1) — native habit/todo CREATE. Deterministic, runs early so a habit
    # named after an ambient signal ("drink water") doesn't trip the water/meal/movement
    # recorders below. Distinct phrasing from Habitica's create triggers → parallel-safe.
    native_create_reply = await handle_native_create(user_id, user_message)
    if native_create_reply is not None:
        await save_to_postgres(user_id, "user", user_message)
        await save_to_postgres(user_id, "assistant", native_create_reply)
        return native_create_reply

    # Commitment tracking (session 19): resolve an open confirmation, or ask about a
    # newly-spotted intention. detect_commitment() defers to Habitica's task-capture phrasing.
    commitment_reply = await handle_commitment(user_id, user_message, source)
    if commitment_reply is not None:
        await save_to_postgres(user_id, "user", user_message)
        await save_to_postgres(user_id, "assistant", commitment_reply)
        return commitment_reply

    # Session 43: "is Habitica working?" etc. → real live check, grounded answer (never a guess).
    status_reply = await handle_integration_status(user_id, user_message)
    if status_reply is not None:
        await save_to_postgres(user_id, "user", user_message)
        await save_to_postgres(user_id, "assistant", status_reply)
        return status_reply

    # Long-term memory (Session 48): explicit "remember X" / "forget X" / "what do you
    # remember". Runs before content/finance routing; financial + sensitive content is NOT
    # stored (handle_memory returns None for those, so money still routes to Ollama).
    memory_reply = await handle_memory(user_id, user_message, source)
    if memory_reply is not None:
        await save_to_postgres(user_id, "user", user_message)
        await save_to_postgres(user_id, "assistant", memory_reply)
        return memory_reply

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
    # Session 33 — typed food write-through. Broadened past the "fatsecret" keyword so a
    # natural eat-statement logs too, routed through the SAME self-confirming pipeline
    # voice notes use (it extracts items, logs, and reports exactly what landed — so a
    # mis-extract is visible, not silent, and a non-food message extracts nothing and
    # writes nothing). Questions are excluded (they want the board/food READ context).
    _eat = any(w in _low for w in ("ate ", "i ate", "eaten", "eating", "had ", "having ", "grabbed", "snacked", "just had"))
    _mealctx = any(w in _low for w in ("for breakfast", "for lunch", "for dinner", "for a snack", "for dessert"))
    _logfood = (any(w in _low for w in ("log", "logged", "record", "track", "add"))
                and any(w in _low for w in ("breakfast", "lunch", "dinner", "snack", "meal", "food", "calorie")))
    _fatsecret_kw = "fatsecret" in _low and any(w in _low for w in ("log", "add", "record", "put"))
    if (not _low.rstrip().endswith("?")) and (_fatsecret_kw or (_eat and _mealctx) or _logfood):
        import fatsecret
        if fatsecret.is_configured():
            logger.info("Typed food statement — routing to FatSecret pipeline")
            return await process_food_note(user_id, user_message)

    water_triggers = ["drank", "drunk", "water", "hydrated", "bottle", "1.5l", "500ml"]
    movement_triggers = ["walked", "walk", "moved", "exercise", "yoga", "gym", "weights", "run", "stretch"]
    drink_triggers = ["coffee", "tea", "juice", "smoothie", "drink", "drank", "water", "hydrated"]
    exercise_triggers = ["yoga", "gym", "weights", "workout", "worked out", "exercised", "went for a run"]
    message_lower_check = user_message.lower()
    now_mel = datetime.now(LOCAL_TZ)
    now_ts = now_mel.timestamp()

    # Session 40: a "capture ..." message is an explicit filing instruction, NOT a
    # behaviour log. Skip the passive water/movement/meal signal-recording below so
    # "capture: the yoga note needs work" doesn't mark exercise done / score water /
    # log a meal. Capture itself is still handled last, just before call_claude.
    _is_capture = bool(_CAPTURE_TRIGGER_RE.match(user_message))
    # Session 41: a retrospective question must not trigger the write-command paths below
    # (vault-write / cascade / task-capture / habitica-create / completion) NOR the passive
    # signal-recording — "have I had enough water?" must not score the Water habit, and
    # "did I finish lunch?" must not log a meal. Combined with the S40 capture skip.
    _retro = _is_retrospective_query(user_message)
    _skip_passive = _is_capture or _retro

    seconds_to_midnight = int(((now_mel + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0) - now_mel).total_seconds())
    water_now = (not _skip_passive) and any(t in message_lower_check for t in water_triggers)
    water_count = 0
    try:
        r = Redis(host=REDIS_HOST, port=6379, password=REDIS_PASSWORD, decode_responses=True)
        if water_now:
            await r.set("last_water_confirmed", now_ts, ex=86400)
            water_count = await r.incr("water_confirm_count")
            await r.expire("water_confirm_count", seconds_to_midnight)
        if (not _skip_passive) and any(t in message_lower_check for t in movement_triggers):
            await r.set("last_movement_confirmed", now_ts, ex=86400)
        if (not _skip_passive) and any(t in message_lower_check for t in drink_triggers):
            await r.set("last_drink_mentioned", now_ts, ex=7200)
        if (not _skip_passive) and any(t in message_lower_check for t in exercise_triggers):
            await r.set("exercise_confirmed", "1", ex=seconds_to_midnight)
        await r.aclose()
    except Exception as e:
        logger.error(f"Redis confirm flags error: {e}")

    # Session 32: a water confirmation now scores the real "❤️‍🔥 Water" HABIT on
    # Habitica. Before this NOTHING wrote water to Habitica — the habit existed on the
    # board but nudges/logging only touched Redis + daily_events. Scored per confirmation
    # (a habit is meant to be tapped each time). Errors log loudly, never swallowed
    # (the app_usage silent-failure lesson); a False result means the habit wasn't found.
    water_sync_failed = False   # Session 43: surface a genuine Habitica-sync failure to Lea once
    if water_now:
        try:
            from habitica import log_habit
            scored = await log_habit("Water", "up")
            if scored:
                logger.info("Water habit scored up on Habitica")
                await _bust_board_cache()
            else:
                logger.error("Water habit score FAILED — 'Water' habit not found or API error")
                water_sync_failed = True
        except Exception as e:
            logger.error(f"Water habit score error: {e}")
            water_sync_failed = True

    # daily_events for multi-day pattern tracking
    if (not _skip_passive) and any(t in message_lower_check for t in exercise_triggers):
        await log_daily_event("exercise_done")
    if water_count >= 3:  # 3x 500ml bottles = 1.5L daily target
        await log_daily_event("water_target_hit")
    skip_words = ["skipped", "skipping", "didn't", "did not", "no time for"]
    eat_words = ["ate", "had", "having", "eating", "eaten", "finished", "grabbed", "made"]
    for meal in ("breakfast", "lunch", "dinner"):
        if (not _skip_passive) and meal in message_lower_check:
            if any(w in message_lower_check for w in skip_words):
                await log_daily_event(f"{meal}_skipped")
            elif any(w in message_lower_check for w in eat_words):
                await log_daily_event(f"{meal}_done")

    # S80: the old immediate (no-confirm) "add to note X" / "note that" vault-write block was
    # removed — chat-triggered writes now go through handle_vault_write's confirm-first handshake
    # earlier in this pipeline, so no write fires on an assumption and every write is logged.
    vault_write_ctx = None

    # Stage-cascade unlocks (vault Area specs): "finance stage 1 complete" → create stage 2 items.
    # Explicit trigger by design — stage completion is a real-world milestone (loan paid off),
    # not something Ghost can infer from Habitica state.
    cascade_ctx = None
    try:
        import re as _re
        cm = None if _retro else _re.search(r'\b(finance|creative)\s+stage\s+(\d)\s+(complete|done|finished)\b', message_lower_check)
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
        if captured and not _retro:
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
        # S64: these Habitica write commands must be the OPENING intent of the message (after minor
        # polite filler), not a phrase buried mid-sentence. The old checks matched the substring
        # ANYWHERE — so "I added a habit on the new habit dashboard, can you see it?" hit "new habit"
        # and created a stray "dashboard, can you see it" habit. Anchoring to the message start
        # (like native's own creation regex) fixes the ambient false-positive. Shared by Telegram
        # AND dashboard chat, since both route through this same process_message block.
        def _opens_with(*phrases):
            pat = (r'^\s*(?:(?:please|can you|could you|would you|pls|ok|okay|hey|hey ghost|ghost)[\s,]+)*'
                   r'(?:' + '|'.join(re.escape(p) for p in phrases) + r')\b')
            return re.match(pat, m) is not None
        if _retro:
            pass  # retrospective question ("did you add X to habitica?") must not create anything
        elif _opens_with("add to habitica", "add task"):
            from habitica import create_todo
            text = _after("add to habitica") if "add to habitica" in m else _after("add task")
            if text:
                ok = await create_todo(text)
                habitica_ctx = f"[Habitica to-do '{text}' created]" if ok else "[Habitica to-do creation failed]"
        elif _opens_with("create daily"):
            from habitica import create_daily
            text = _after("create daily")
            if text:
                ok = await create_daily(text)
                habitica_ctx = f"[Habitica daily '{text}' created]" if ok else "[Habitica daily creation failed]"
        elif _opens_with("new habit"):
            from habitica import create_habit
            text = _after("new habit")
            if text:
                ok = await create_habit(text)
                habitica_ctx = f"[Habitica habit '{text}' created]" if ok else "[Habitica habit creation failed]"
        elif _opens_with("habit up", "habit down") or m.startswith("scored"):
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

    # Ghost 2.0 (Phase 1) — native habit TICK / todo COMPLETE, deterministic, runs alongside
    # Habitica. Placed AFTER the ambient water/movement/meal block (so Habitica's ambient paths
    # still fire) and BEFORE the Habitica completion-by-name block below (so native items win for
    # their own names). native.route_message only claims a message when an explicit action verb or
    # +/- token matches exactly ONE native item; a bare ambient mention or no native match returns
    # None, so this is invisible until Lea actually has native habits/todos. Parallel-run safe:
    # nothing here touches habitica.py.
    if not _retro:
        try:
            nres = await native.route_message(user_message, user_id)
        except Exception as e:
            logger.error(f"native action error: {e}")
            nres = None
        if nres and nres.get("action") == "ambiguous":
            names = ", ".join(f"'{n}'" for n in nres["candidates"][:5])
            return await call_claude(user_id, f"{user_message} [AMBIGUOUS native match: {names} — ask her which one to mark, do not guess or write anything]", route_text=user_message)
        if nres and nres.get("action") == "tap_habit":
            if nres.get("ok"):
                await _bust_native_cache()
                return await call_claude(user_id, f"{user_message} [Native habit '{nres['habit']}' tapped {nres['direction']}, streak now {nres['streak']} — confirm briefly what was logged]", route_text=user_message)
            return await call_claude(user_id, f"{user_message} [Native habit tap FAILED — tell her it did not save, do not claim success]", route_text=user_message)
        if nres and nres.get("action") == "complete_todo":
            if nres.get("ok"):
                await _bust_native_cache()
                return await call_claude(user_id, f"{user_message} [Native to-do '{nres['text']}' completed — confirm briefly]", route_text=user_message)
            return await call_claude(user_id, f"{user_message} [Native to-do completion FAILED — tell her it did not save]", route_text=user_message)
        if nres and nres.get("action") == "list_edit":
            # S79 Task 4: a swap/skip/remove actually mutated the native list (archive + optional add).
            if nres.get("ok"):
                await _bust_native_cache()
                parts = []
                if nres.get("removed"):
                    parts.append(f"removed the {nres['removed'][0]} '{nres['removed'][1]}'")
                if nres.get("added"):
                    parts.append(f"added '{nres['added']}' to the to-dos")
                return await call_claude(user_id, f"{user_message} [Native list updated: {' and '.join(parts)} — confirm briefly EXACTLY this and nothing else]", route_text=user_message)
            return await call_claude(user_id, f"{user_message} [Native list edit: nothing on the native list matched '{nres.get('target','')}'; ask her which item she means, do NOT claim anything was changed]", route_text=user_message)

    completion_triggers = ["mark", "done", "finished", "complete", "ticked", "tick off", "mark off",
                           "did ", "did my", "went to", "smashed", "knocked out", "wrapped up"]
    message_lower = user_message.lower()

    if cascade_ctx:
        return await call_claude(user_id, f"{user_message} {cascade_ctx}", route_text=user_message)
    if vault_write_ctx:
        return await call_claude(user_id, f"{user_message} {vault_write_ctx}", route_text=user_message)
    if task_capture_ctx:
        return await call_claude(user_id, f"{user_message} {task_capture_ctx} [Confirm in one line: what was added and how it's tagged.]", route_text=user_message)
    if habitica_ctx:
        await _bust_board_cache()
        return await call_claude(user_id, f"{user_message} {habitica_ctx} Please list this data clearly for {USER_NAME}.", route_text=user_message)
    if (not _retro) and any(trigger in message_lower for trigger in completion_triggers):
        try:
            # Session 33 — conversational write-through across the WHOLE board, not just
            # dailies: match "done/finished X" against dailies, habits AND to-dos. Exact
            # substring of the item name must appear in the message (guards against false
            # writes); if the words hit >1 distinct item, ask which rather than guessing.
            from habitica import complete_task, get_dailies, get_habits, get_todos, log_habit, complete_todo
            dailies = await get_dailies()
            pending_dailies = dailies["pending"]
            try:
                habit_names = await get_habits()
            except Exception:
                habit_names = []
            try:
                todo_names = await get_todos()
            except Exception:
                todo_names = []
            # candidate = (kind, name); only match reasonably specific names (>=4 chars)
            candidates = ([("daily", t) for t in pending_dailies]
                          + [("habit", t) for t in habit_names]
                          + [("todo", t) for t in todo_names])
            matches = []
            for kind, name in candidates:
                core = name.strip().lower()
                # strip leading emoji/symbols so "❤️‍🔥 water" matches on "water"
                core_words = core.split()
                if core and core in message_lower and len(core) >= 4:
                    matches.append((kind, name))
                elif core_words and len(core_words[-1]) >= 4 and core_words[-1] in message_lower:
                    matches.append((kind, name))
            # de-dup by name
            seen = set(); matches = [(k, n) for k, n in matches if not (n in seen or seen.add(n))]
            if len(matches) > 1:
                names = ", ".join(f"'{n.strip()}'" for _, n in matches[:5])
                return await call_claude(user_id, f"{user_message} [AMBIGUOUS: that could mean {names} — ask her which one to mark, do not guess or write anything yet]", route_text=user_message)
            if len(matches) == 1:
                kind, name = matches[0]
                if kind == "habit":
                    ok = await log_habit(name.strip(), "up")
                elif kind == "todo":
                    ok = await complete_todo(name)
                else:
                    ok = await complete_task(name)
                if ok:
                    await _bust_board_cache()
                    return await call_claude(user_id, f"{user_message} [Habitica {kind} '{name.strip()}' recorded (scored/completed) — confirm briefly what was logged]", route_text=user_message)
                return await call_claude(user_id, f"{user_message} [Habitica {kind} '{name.strip()}' found but the write FAILED — tell her it didn't save, do not claim success]", route_text=user_message)
            return await call_claude(user_id, user_message)
        except Exception as e:
            logger.error(f"Habitica error: {e}")
            return await call_claude(user_id, user_message)

    # Capture (Session 40) — LAST in precedence: only a message no earlier check
    # claimed reaches here. A "capture ..." message files the rest to the routed
    # project/goal note (or Inbox); everything else falls straight through to the
    # normal chat/finance routing inside call_claude, unchanged.
    capture_reply = await handle_capture(user_id, user_message)
    if capture_reply is not None:
        await save_to_postgres(user_id, "user", user_message)
        await save_to_postgres(user_id, "assistant", capture_reply)
        return capture_reply

    # Session 43: if the Habitica half of a water confirmation genuinely failed, tell Lea
    # ONCE — her water is noted locally (Redis is decoupled and already set above), but the
    # Habitica habit tick didn't sync. Gated by a per-day Redis flag so a sustained outage
    # doesn't re-nag on every confirmation, and it's framed as a status note, not a nudge.
    if water_sync_failed:
        try:
            rr = Redis(host=REDIS_HOST, port=6379, password=REDIS_PASSWORD, decode_responses=True)
            first = not await rr.get("water_sync_fail_notified")
            if first:
                await rr.set("water_sync_fail_notified", "1", ex=seconds_to_midnight)
            await rr.aclose()
            if first:
                note = (" [STATUS NOTE — deliver ONCE, plainly, as a heads-up not a nudge and not a "
                        "criticism of her: her water is noted locally, but the Habitica Water-habit tick "
                        "did NOT sync just now (Habitica-side write issue). One line; do not repeat it in "
                        "later turns.]")
                return await call_claude(user_id, user_message + note, route_text=user_message)
        except Exception as e:
            logger.error(f"water sync-fail surface error: {e}")
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
        _preserve_vault_owner(path, None)  # always a new file → inherit vault owner, not root
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


# ---------------------------------------------------------------------------------------------
# Decision loop (Telegram front end for bot/decisions.py). Callback handling is deterministic:
# matched on decision id + option key, never interpreted by an LLM. Double taps are idempotent
# because decisions.answer() only ever flips a 'pending' row.
# ---------------------------------------------------------------------------------------------
DECISION_OTHER_TTL = 3600

def _decision_markup(row):
    return InlineKeyboardMarkup([[InlineKeyboardButton(b["text"], callback_data=b["callback_data"]) for b in line]
                                 for line in decisions.button_rows(row)])

async def _edit_decision_msg(q, text, markup=None):
    try:
        await q.edit_message_text(text[:4000], reply_markup=markup)
    except Exception as e:  # "message is not modified" on a double tap is expected
        logger.info(f"decision edit skipped: {e}")

async def handle_decision_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q is None:
        return
    async def toast(text=None, alert=False):
        try:
            await q.answer(text, show_alert=alert)
        except Exception as e:
            logger.info(f"decision callback answer skipped: {e}")
    parsed = decisions.parse_callback(q.data)
    if not parsed:
        return await toast()
    if not OWNER_ID or q.from_user.id != OWNER_ID:
        logger.warning(f"decision callback from non-owner {q.from_user.id} refused")
        return await toast("Not authorised.", True)
    did, key = parsed
    conn = await pg_conn()
    try:
        row = await decisions.get_decision(conn, did)
        if row is None:
            return await toast("That decision no longer exists.", True)
        if key == decisions.OTHER_KEY and row["status"] == "pending":
            r = Redis(host=REDIS_HOST, port=6379, password=REDIS_PASSWORD, decode_responses=True)
            await r.set(f"decision_other:{q.from_user.id}",
                        json.dumps({"id": did, "chat_id": q.message.chat_id, "message_id": q.message.message_id}),
                        ex=DECISION_OTHER_TTL)
            await r.aclose()
            await _edit_decision_msg(q, decisions.waiting_other_text(row), _decision_markup(row))
            return await toast("Type your answer as your next message.")
        outcome, row = await decisions.answer(conn, did, key, via="telegram")
    finally:
        await conn.close()
    if outcome == "invalid":
        return await toast("That option isn't valid for this decision.", True)
    if outcome == "answered":
        logger.info(f"decision #{did} answered via telegram: {row['answer_key']}")
        try:
            r = Redis(host=REDIS_HOST, port=6379, password=REDIS_PASSWORD, decode_responses=True)
            await r.delete(f"decision_other:{q.from_user.id}")
            await r.aclose()
        except Exception:
            pass
        await toast("Recorded.")
    else:  # already answered: same tap twice, or a different tap after the first
        await toast("Already recorded." if row["answer_key"] == key
                    else f"Already answered: {row['answer_text'] or row['answer_key']}", row["answer_key"] != key)
    await _edit_decision_msg(q, decisions.answered_text(row))

async def maybe_handle_decision_other(update: Update) -> str | None:
    """If the previous tap was 'Other…', this text message IS that decision's answer."""
    uid = update.message.from_user.id
    if not OWNER_ID or uid != OWNER_ID:
        return None
    r = Redis(host=REDIS_HOST, port=6379, password=REDIS_PASSWORD, decode_responses=True)
    try:
        raw = await r.get(f"decision_other:{uid}")
        if not raw:
            return None
        bound = json.loads(raw)
        text = (update.message.text or "").strip()
        if text.lower() in ("cancel", "/cancel"):
            await r.delete(f"decision_other:{uid}")
            return f"Okay, decision #{bound['id']} is still pending. /decisions shows it again."
        conn = await pg_conn()
        try:
            outcome, row = await decisions.answer(conn, bound["id"], decisions.OTHER_KEY, text, via="telegram")
        finally:
            await conn.close()
        await r.delete(f"decision_other:{uid}")
    finally:
        await r.aclose()
    if outcome == "missing":
        return "That decision no longer exists."
    if outcome == "invalid":
        return "I couldn't use that as an answer — tap Other… again and type it."
    try:  # update the original card so it doesn't keep offering buttons
        await update.get_bot().edit_message_text(decisions.answered_text(row)[:4000],
                                                 chat_id=bound["chat_id"], message_id=bound["message_id"])
    except Exception as e:
        logger.info(f"decision card edit skipped: {e}")
    if outcome == "already":
        return f"Decision #{row['id']} was already answered ({row['answer_text'] or row['answer_key']}); nothing changed."
    return decisions.answered_text(row)

async def cmd_decisions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Re-send every pending decision as a card with buttons."""
    if not OWNER_ID or update.message.from_user.id != OWNER_ID:
        return
    conn = await pg_conn()
    try:
        rows = await decisions.list_decisions(conn, "pending")
    finally:
        await conn.close()
    if not rows:
        await update.message.reply_text("No decisions waiting on you.")
        return
    for row in rows:
        await update.message.reply_text(decisions.render_card(row)[:4000], reply_markup=_decision_markup(row))

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        await log_message("telegram_in", "chat", update.message.text)
        bound_reply = await maybe_handle_decision_other(update)
        if bound_reply is not None:
            await update.message.reply_text(bound_reply)
            await log_message("telegram_out", "chat", bound_reply)
            return
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
    app.add_handler(CommandHandler("decisions", cmd_decisions))
    app.add_handler(CallbackQueryHandler(handle_decision_callback, pattern=r"^dec:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    app.add_error_handler(error_handler)
    app.run_polling()

if __name__ == '__main__':
    main()
