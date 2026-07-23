"""Self-code apply runner (host-side, runs as the operator — no root except the one scoped
rebuild grant).

Two kinds of work, deliberately different in how they trigger:

1. DASHBOARD proposals, status='approved'  -> auto-applied (session 10 behaviour).
   Dashboard rebuild is kubectl-only, no credentials, can't take the loop down.

2. BOT/SCHEDULER proposals, status='apply_queued'  -> applied only when the operator has
   BOTH approved it in Telegram ("apply this change" -> 'approved') AND pressed the
   dashboard Apply button (-> 'apply_queued'). Never auto-applied on approval alone.
   Their rebuild runs the fixed root-owned wrapper /usr/local/sbin/ghost-rebuild via
   one scoped NOPASSWD sudo entry; without that grant the file+backup are written and
   the proposal lands 'applied_no_rebuild' so undo still works.

apply_change() only ever runs on an 'approved' proposal, so for the queued case the
runner flips 'apply_queued' -> 'approved' immediately before applying. The runner is
the only actor and works sequentially, so that transition can't race.
"""
import asyncio
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import selfcode  # noqa: E402

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger("selfcode-runner")

POLL_SECONDS = 120


async def _apply_and_report(pid: int, service: str, file_path: str, explanation: str):
    await selfcode.send_telegram(
        f"Applying proposal #{pid} ({service}/{os.path.basename(file_path)}).\n\n"
        f"What changed: {explanation}\n\nRebuilding now — I'll confirm in a moment."
    )
    try:
        result = await selfcode.apply_change(pid)
    except Exception as e:
        logger.error(f"Apply #{pid} raised: {e}")
        await selfcode.send_telegram(
            f"Apply of proposal #{pid} FAILED with an error: {e}\n"
            f"Nothing will retry it. Run `python3 ~/ghost/selfcode/selfcode.py apply {pid}` by hand.")
        return
    if result.get("ok"):
        logger.info(f"Applied #{pid} and rebuilt {service}")
    else:
        logger.error(f"Apply #{pid} did not complete: {result.get('error')}")
        await selfcode.send_telegram(
            f"Apply of proposal #{pid} did not complete: {result.get('error')}. "
            f"The file and its backup are on disk. \"undo last change {service}\" reverses it.")


SELF_REVIEW_SYSTEM = """You are Ghost's self-review analyst. Given Ghost's Outstanding lists and its
source files, pick EXACTLY ONE small, concrete, low-risk code improvement that a
single-file diff could deliver. Only bot/scheduler/dashboard .py files are in scope.
Prefer boring reliability fixes over features. Reply with ONLY a JSON object:
{"service": "bot|scheduler|dashboard", "file": "<basename>.py", "problem": "<one-paragraph
description of the concrete problem and the small fix>"} — no other text."""

async def _gather_outstanding() -> str:
    """Outstanding sections + file inventory from the host filesystem (runner-side)."""
    chunks = []
    for path, label in (
        ("/home/admin_hivequeen/CentralVault/Projects/Ghost/Ghost-Index.md", "Ghost-Index"),
        ("/home/admin_hivequeen/ghost/CLAUDE.md", "CLAUDE.md"),
    ):
        try:
            text = open(path, encoding="utf-8", errors="ignore").read()
            idx = text.lower().find("## outstanding")
            if idx >= 0:
                section = text[idx:idx + 1200]
                chunks.append(f"--- {label} Outstanding ---\n{section}")
        except OSError:
            pass
    files = []
    for svc, d in selfcode.ALLOWED_DIRS.items():
        for f in sorted(os.listdir(d)):
            if f.endswith(".py"):
                files.append(f"{svc}/{f}")
    chunks.append("--- proposable files ---\n" + ", ".join(files))
    return "\n\n".join(chunks)

async def process_self_reviews(conn):
    """Task 4 (session 16): the third proposal trigger. Analysis happens here; the
    proposal itself goes through selfcode.propose_change() UNCHANGED — allowlist,
    one-at-a-time limit, Telegram diff, exact-phrase approval all apply as-is."""
    import httpx, json as _json
    rows = await conn.fetch("SELECT id FROM self_review_requests WHERE status='pending' ORDER BY id LIMIT 1")
    for row in rows:
        rid = row["id"]
        logger.info(f"Self-review request #{rid} — analysing Outstanding list")
        outcome = ""
        try:
            context = await _gather_outstanding()
            async with httpx.AsyncClient() as client:
                r = await client.post(
                    "https://api.anthropic.com/v1/messages",
                    headers={"x-api-key": os.environ.get("ANTHROPIC_API_KEY"),
                             "anthropic-version": "2023-06-01", "content-type": "application/json"},
                    json={"model": "claude-sonnet-4-6", "max_tokens": 400,
                          "system": SELF_REVIEW_SYSTEM,
                          "messages": [{"role": "user", "content": context}]},
                    timeout=120)
            raw = r.json()["content"][0]["text"].strip()
            if raw.startswith("```"):
                raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()
            try:
                pick = _json.loads(raw)
            except _json.JSONDecodeError:
                # model sometimes reasons in prose first despite instructions —
                # pull the flat JSON object out of the text
                import re as _re
                m = _re.search(r'\{[^{}]*"service"[^{}]*\}', raw, _re.S)
                if not m:
                    raise ValueError(f"no JSON object in analysis reply: {raw[:120]!r}")
                pick = _json.loads(m.group(0))
            service, fname, problem = pick["service"], pick["file"], pick["problem"]
            logger.info(f"Self-review picked: {service}/{fname}")
            # THE GATE — completely unchanged. Allowlist, open-proposal limit,
            # diff generation, Telegram notification all live inside propose_change.
            result = await selfcode.propose_change(service, fname, f"[self-directed] {problem}")
            outcome = (f"proposal #{result['id']} created" if result.get("ok")
                       else f"refused by gate: {result.get('error')}")
        except Exception as e:
            outcome = f"analysis failed: {e}"
            logger.error(f"Self-review #{rid} failed: {e}")
        await conn.execute(
            "UPDATE self_review_requests SET status='done', outcome=$2, resolved_at=NOW() WHERE id=$1",
            rid, outcome[:500])
        logger.info(f"Self-review #{rid}: {outcome}")

async def poll_once():
    conn = await selfcode._pg()
    try:
        # 0. self-review requests → analysis → the existing proposal gate
        await process_self_reviews(conn)
        # 1. dashboard: auto-apply on approval
        dash = await conn.fetch(
            "SELECT id, service, file_path, explanation FROM code_proposals "
            "WHERE service = 'dashboard' AND status = 'approved' ORDER BY id")
        # 2. bot/scheduler: apply only when button-queued (already approved in Telegram)
        queued = await conn.fetch(
            "SELECT id, service, file_path, explanation FROM code_proposals "
            "WHERE service IN ('bot','scheduler') AND status = 'apply_queued' ORDER BY id")
        # move queued ones to 'approved' so apply_change accepts them; sequential, no race
        for row in queued:
            await conn.execute("UPDATE code_proposals SET status='approved' WHERE id=$1", row["id"])
    finally:
        await conn.close()

    for row in list(dash) + list(queued):
        logger.info(f"Applying {row['service']} proposal #{row['id']}")
        await _apply_and_report(row["id"], row["service"], row["file_path"], row["explanation"])


async def main():
    logger.info(f"Ghost self-code runner started — dashboard auto + bot/scheduler on button, polling every {POLL_SECONDS}s")
    while True:
        try:
            await poll_once()
        except Exception as e:
            logger.error(f"Poll cycle error: {e}")
        await asyncio.sleep(POLL_SECONDS)


if __name__ == "__main__":
    asyncio.run(main())
