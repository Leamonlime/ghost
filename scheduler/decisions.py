"""Decision store — questions the overnight runner (or anything else) needs Lea to answer.

Notifier-agnostic on purpose: this module knows rows, options, answers and plain-text
rendering. Telegram buttons, the Ghost app API and the dashboard card are thin layers on top
(callback data and button rows are plain data built here, so a different front end only has to
call answer()). Telegram is being retired; nothing here may depend on it.

Copies live in bot/ (canonical), scheduler/ (evening reminders, copy kept identical by
smoke_static) and are imported by the dashboard via /botcode and by the host-side ghost-runner.

Idempotency rules:
  * insert_decision is idempotent on (prompt_slug, sha256 of the normalised question).
  * answer() only ever flips a 'pending' row. A second answer (double tap, retry) returns the
    stored one untouched, so the first answer always wins.
Nothing in here auto-answers, auto-applies or auto-expires a decision; 'expired' is set by hand.
"""
import hashlib
import json
import re
from datetime import datetime, timezone

STATUSES = ("pending", "answered", "applied", "expired")
MERGE_STATES = ("merged", "on_branch", "n/a")
KINDS = ("decision", "deploy", "manual")   # manual = a step only Lea can do; answering just acknowledges it
OTHER_KEY = "other"
STALE_DAYS = 7
KEY_RE = re.compile(r"^[a-z0-9_]{1,16}$")
CALLBACK_PREFIX = "dec"
TELEGRAM_CARD_LIMIT = 3500          # leave room under Telegram's 4096 for the footer lines

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS decisions (
    id                  BIGSERIAL PRIMARY KEY,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    prompt_slug         TEXT NOT NULL,
    branch              TEXT,
    merge_state         TEXT NOT NULL DEFAULT 'n/a',     -- merged | on_branch | n/a
    deploy_steps        TEXT,
    question            TEXT NOT NULL,                    -- full text, never truncated
    options             JSONB NOT NULL DEFAULT '[]',      -- [{key,label,consequence}]
    recommended_key     TEXT,
    status              TEXT NOT NULL DEFAULT 'pending',  -- pending | answered | applied | expired
    answer_key          TEXT,
    answer_text         TEXT,
    answered_at         TIMESTAMPTZ,
    answered_via        TEXT,                             -- telegram | app | dashboard | api | ...
    followup_queued_at  TIMESTAMPTZ,
    reminder_count      INT NOT NULL DEFAULT 0,
    -- beyond the original column list:
    question_hash       TEXT NOT NULL,                    -- idempotency key with prompt_slug
    kind                TEXT NOT NULL DEFAULT 'decision', -- decision | deploy (Deploy button) | manual (ack only)
    result              TEXT,                             -- outcome of the follow-up / deploy
    UNIQUE (prompt_slug, question_hash)
);
CREATE INDEX IF NOT EXISTS decisions_status_idx ON decisions(status);
"""

_READY = False


async def ensure_schema(conn):
    global _READY
    if _READY:
        return
    await conn.execute(SCHEMA_SQL)
    _READY = True


def reset_schema_cache():
    """Tests that switch search_path need the CREATE TABLE to run again."""
    global _READY
    _READY = False


# ---------------------------------------------------------------------------------------------
# Parsing what a session emits
# ---------------------------------------------------------------------------------------------
_BLOCK_RE = re.compile(r"```[ \t]*DECISIONS[ \t]*\r?\n(.*?)```", re.S | re.I)


def question_hash(question: str) -> str:
    norm = " ".join((question or "").split()).lower()
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()[:32]


def _slug_key(raw, taken, fallback):
    k = re.sub(r"[^a-z0-9]+", "_", str(raw or "").lower()).strip("_")[:16].strip("_")
    if not k or k == OTHER_KEY or not KEY_RE.match(k):
        k = fallback
    base, n = k, 2
    while k in taken:
        k = f"{base[:13]}_{n}"
        n += 1
    return k


def normalise_decision(d: dict):
    """One decision dict from a session -> clean dict, or None if it has no question."""
    if not isinstance(d, dict):
        return None
    q = str(d.get("question") or "").strip()
    if not q:
        return None
    opts, taken = [], set()
    for i, o in enumerate(d.get("options") or []):
        if isinstance(o, str):
            o = {"label": o}
        if not isinstance(o, dict):
            continue
        label = str(o.get("label") or o.get("text") or "").strip()
        if not label:
            continue
        key = _slug_key(o.get("key"), taken, "abcdefgh"[i % 8])
        taken.add(key)
        opts.append({"key": key, "label": label[:120],
                     "consequence": str(o.get("consequence") or "").strip()[:300]})
    rec = str(d.get("recommended_key") or d.get("recommended") or "").strip().lower()
    if rec not in {o["key"] for o in opts}:
        # accept the recommendation given as the option's label or its raw key before slugging
        match = [o["key"] for o in opts if o["label"].lower() == rec]
        rec = match[0] if match else None
    ms = str(d.get("merge_state") or "n/a").strip().lower().replace("-", "_").replace(" ", "_")
    return {"question": q, "options": opts, "recommended_key": rec or None,
            "merge_state": ms if ms in MERGE_STATES else "n/a",
            "deploy_steps": str(d.get("deploy_steps") or "").strip() or None}


def parse_decisions_block(*texts):
    """Find a fenced ```DECISIONS JSON block in any of the given strings.
    Returns a list of normalised decisions, or [] when there is no usable block."""
    for text in texts:
        if not text:
            continue
        for m in _BLOCK_RE.finditer(str(text)):
            try:
                data = json.loads(m.group(1))
            except ValueError:
                continue
            if isinstance(data, dict):
                data = data.get("decisions") if isinstance(data.get("decisions"), list) else [data]
            if not isinstance(data, list):
                continue
            out = [x for x in (normalise_decision(d) for d in data) if x]
            if out:
                return out
    return []


def raw_decision(text, merge_state="n/a", deploy_steps=None):
    """Fallback: the whole stop message as ONE free-text decision (no options -> 'Other…' only)."""
    return {"question": str(text).strip() or "(the session stopped without saying why)",
            "options": [], "recommended_key": None,
            "merge_state": merge_state if merge_state in MERGE_STATES else "n/a",
            "deploy_steps": deploy_steps}


# ---------------------------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------------------------
def _row(r):
    if r is None:
        return None
    d = dict(r)
    if isinstance(d.get("options"), str):
        try:
            d["options"] = json.loads(d["options"])
        except ValueError:
            d["options"] = []
    d["options"] = d.get("options") or []
    return d


async def insert_decision(conn, prompt_slug, question, options=None, recommended_key=None,
                          branch=None, merge_state="n/a", deploy_steps=None, kind="decision"):
    """-> (id, created). Idempotent on (prompt_slug, question hash)."""
    await ensure_schema(conn)
    h = question_hash(question)
    new_id = await conn.fetchval(
        """INSERT INTO decisions (prompt_slug, branch, merge_state, deploy_steps, question, options,
                                  recommended_key, question_hash, kind)
           VALUES ($1,$2,$3,$4,$5,$6::jsonb,$7,$8,$9)
           ON CONFLICT (prompt_slug, question_hash) DO NOTHING RETURNING id""",
        prompt_slug, branch, merge_state if merge_state in MERGE_STATES else "n/a", deploy_steps,
        question, json.dumps(options or []), recommended_key, h, kind)
    if new_id:
        return new_id, True
    existing = await conn.fetchval("SELECT id FROM decisions WHERE prompt_slug=$1 AND question_hash=$2",
                                   prompt_slug, h)
    return existing, False


async def get_decision(conn, did):
    await ensure_schema(conn)
    return _row(await conn.fetchrow("SELECT * FROM decisions WHERE id=$1", int(did)))


async def list_decisions(conn, status=None, limit=200):
    await ensure_schema(conn)
    if status:
        rows = await conn.fetch("SELECT * FROM decisions WHERE status=$1 ORDER BY id LIMIT $2", status, limit)
    else:
        rows = await conn.fetch("SELECT * FROM decisions ORDER BY id DESC LIMIT $1", limit)
    return [_row(r) for r in rows]


def option_for(row, key):
    return next((o for o in row.get("options") or [] if o["key"] == key), None)


async def answer(conn, did, key, text=None, via="unknown"):
    """Record an answer. -> (outcome, row) where outcome is one of
    'answered' (this call set it), 'already' (row was not pending; stored answer untouched),
    'invalid' (unknown option / empty 'other' text), 'missing' (no such id)."""
    await ensure_schema(conn)
    key = (key or "").strip().lower()
    async with conn.transaction():
        row = _row(await conn.fetchrow("SELECT * FROM decisions WHERE id=$1 FOR UPDATE", int(did)))
        if row is None:
            return "missing", None
        if row["status"] != "pending":
            return "already", row
        if key == OTHER_KEY:
            if not (text or "").strip():
                return "invalid", row
            label = text.strip()
        else:
            opt = option_for(row, key)
            if opt is None:
                return "invalid", row
            label = (text or "").strip() or opt["label"]
        upd = await conn.fetchrow(
            """UPDATE decisions SET status='answered', answer_key=$2, answer_text=$3,
                      answered_at=now(), answered_via=$4 WHERE id=$1 RETURNING *""",
            int(did), key, label, via)
        return "answered", _row(upd)


async def mark(conn, did, status, result=None):
    await ensure_schema(conn)
    assert status in STATUSES
    await conn.execute("UPDATE decisions SET status=$2, result=COALESCE($3, result) WHERE id=$1",
                       int(did), status, result)


async def pending_for_slug(conn, slug):
    await ensure_schema(conn)
    return await conn.fetchval("SELECT count(*) FROM decisions WHERE prompt_slug=$1 AND status='pending'", slug)


# ---------------------------------------------------------------------------------------------
# Presentation (plain data / plain text — no Telegram objects)
# ---------------------------------------------------------------------------------------------
def age_days(row, now=None):
    now = now or datetime.now(timezone.utc)
    ca = row["created_at"]
    if ca.tzinfo is None:
        ca = ca.replace(tzinfo=timezone.utc)
    return max(0, (now - ca).days)


def is_stale(row, now=None):
    return row["status"] == "pending" and age_days(row, now) >= STALE_DAYS


def to_public(row, now=None):
    iso = lambda v: v.isoformat() if v else None
    return {"id": row["id"], "kind": row.get("kind", "decision"), "created_at": iso(row["created_at"]),
            "prompt_slug": row["prompt_slug"], "branch": row["branch"], "merge_state": row["merge_state"],
            "deploy_steps": row["deploy_steps"], "question": row["question"], "options": row["options"],
            "recommended_key": row["recommended_key"], "status": row["status"],
            "answer_key": row["answer_key"], "answer_text": row["answer_text"],
            "answered_at": iso(row["answered_at"]), "answered_via": row["answered_via"],
            "followup_queued_at": iso(row["followup_queued_at"]), "reminder_count": row["reminder_count"],
            "result": row.get("result"), "age_days": age_days(row, now), "stale": is_stale(row, now)}


def _clip(s, n):
    s = str(s)
    return s if len(s) <= n else s[:n - 1].rstrip() + "…"


def render_card(row, now=None):
    """The message body for a pending decision."""
    head = f"🙋 Decision #{row['id']} — {row['prompt_slug']}"
    if is_stale(row, now):
        head += f"  ⚠️ STALE ({age_days(row, now)} days waiting)"
    bits = []
    if row.get("branch"):
        bits.append(f"branch {row['branch']}")
    bits.append({"merged": "merged into master", "on_branch": "NOT merged (still on its branch)"}
                .get(row["merge_state"], "no merge involved"))
    lines = [head, "(" + "; ".join(bits) + ")", "", _clip(row["question"], TELEGRAM_CARD_LIMIT)]
    if row["question"] and len(row["question"]) > TELEGRAM_CARD_LIMIT:
        lines.append("(full text in the Ghost app / GET /api/decisions)")
    if row["options"]:
        lines.append("")
        for o in row["options"]:
            star = " ★" if o["key"] == row.get("recommended_key") else ""
            lines.append(f"• {o['label']}{star}" + (f" — {o['consequence']}" if o.get("consequence") else ""))
        rec = option_for(row, row.get("recommended_key"))
        if rec:
            lines.append(f"Recommended: {rec['label']}")
    if row.get("deploy_steps"):
        lines.append(f"\nDeploy steps: {_clip(row['deploy_steps'], 400)}")
    return "\n".join(lines)


def callback_data(did, key):
    return f"{CALLBACK_PREFIX}:{int(did)}:{key}"


def parse_callback(data):
    """'dec:12:retire' -> (12, 'retire'), or None if it is not ours / malformed."""
    m = re.fullmatch(rf"{CALLBACK_PREFIX}:(\d{{1,9}}):([a-z0-9_]{{1,16}})", data or "")
    return (int(m.group(1)), m.group(2)) if m else None


def button_rows(row):
    """Inline keyboard as plain data: one button per option, then 'Other…'."""
    rows = []
    for o in row["options"]:
        label = ("★ " if o["key"] == row.get("recommended_key") else "") + o["label"]
        rows.append([{"text": _clip(label, 60), "callback_data": callback_data(row["id"], o["key"])}])
    rows.append([{"text": "Other…", "callback_data": callback_data(row["id"], OTHER_KEY)}])
    return rows


def next_step_text(row):
    """What happens after an answer — only claims things the pipeline really does."""
    if row.get("kind") == "manual":
        return "Next: nothing. Marked as handled."
    if row.get("kind") == "deploy":
        if row.get("answer_key") == "deploy":
            return "Next: ghost-deploy runs on the host within a minute; I'll report VERIFIED or FAILURE here."
        return "Next: nothing. It stays merged and undeployed."
    return ("Next: a follow-up prompt is built from this answer and queued for tonight's 23:00 run "
            "(or run it sooner with `runner.py --apply-decisions` on the host). Nothing is applied until then.")


def answered_text(row):
    ans = row.get("answer_text") or row.get("answer_key")
    via = f" via {row['answered_via']}" if row.get("answered_via") else ""
    return (f"✅ Decision #{row['id']} answered{via}: {ans}\n\n"
            f"{_clip(row['question'], 600)}\n\n{next_step_text(row)}")


def waiting_other_text(row):
    return (f"✍️ Decision #{row['id']}: type your answer as your next message "
            f"(send \"cancel\" to back out).\n\n{_clip(row['question'], 1200)}")


# ---------------------------------------------------------------------------------------------
# Reminders (evening summary)
# ---------------------------------------------------------------------------------------------
async def reminder_block(conn, now=None, bump=True):
    """Text block listing every pending decision (stale ones flagged), and bump reminder_count
    for each listed row. None when nothing is pending. Never answers or expires anything."""
    await ensure_schema(conn)
    rows = await list_decisions(conn, "pending")
    if not rows:
        return None
    lines = [f"Still waiting on you ({len(rows)} decision{'s' if len(rows) != 1 else ''}) — "
             f"answer with the buttons in the morning report, /decisions, or the Ghost app:"]
    for r in rows:
        first = _clip(" ".join(r["question"].split()), 110)
        flag = f" ⚠️ STALE {age_days(r, now)}d" if is_stale(r, now) else f" ({age_days(r, now)}d)"
        lines.append(f"• #{r['id']}{flag}: {first}")
    if bump:
        await conn.execute("UPDATE decisions SET reminder_count = reminder_count + 1 WHERE status='pending'")
    return "\n".join(lines)
