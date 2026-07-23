"""Ghost restricted self-coding mechanism.

Everything "approval" is allowed to do is fixed HERE, in advance — never decided
at approval time. A proposal can only ever:
  1. touch ONE regular .py file inside bot/, scheduler/, or dashboard/,
  2. write content that was generated and shown at PROPOSAL time (stored verbatim),
  3. trigger exactly ONE hardcoded rebuild sequence for that file's service.

There is no path here to run an arbitrary command, touch another file, read a
secret, or reach the host shell/SSH. `service` is validated against a fixed set
before any subprocess runs, and no variable input is ever interpolated into a
command.

Runs HOST-SIDE only. The bot pod deliberately has no filesystem/kubectl/sudo
access — it records approval into Postgres; this module (run from a context that
has the code tree and rebuild rights) performs the privileged action. Bot and
scheduler rebuilds need sudo (the operator's session); dashboard rebuild is kubectl-only.

CLI:
    python3 selfcode.py propose <service> <file_path> "<problem description>"
    python3 selfcode.py apply <proposal_id>
    python3 selfcode.py undo <service>
    python3 selfcode.py expire            # sweep >24h proposed rows to 'expired'
"""
import os
import sys
import difflib
import subprocess
import shutil
from datetime import datetime, timezone

import asyncpg
import httpx

GHOST_ROOT = os.environ.get("GHOST_ROOT", "/home/admin_hivequeen/ghost")
ALLOWED_DIRS = {
    "bot": os.path.join(GHOST_ROOT, "bot"),
    "scheduler": os.path.join(GHOST_ROOT, "scheduler"),
    "dashboard": os.path.join(GHOST_ROOT, "dashboard"),
}
# Names/patterns that are NEVER proposable, even inside an allowed dir.
# Only .py application code is self-modifiable; secret material in this project is
# structurally never a .py file (it lives in k8s secrets, CREDENTIALS.md and .yaml),
# so blocking those file types is the precise guard — a substring match on the name
# would wrongly reject legit code like fatsecret.py.
DENIED_BASENAMES = {"credentials.md", "selfcode.py"}
DENIED_SUFFIXES = (".yaml", ".yml", ".bak", ".env")

POSTGRES_HOST = os.environ.get("POSTGRES_HOST", "ghost-postgres-postgresql.ghost.svc.cluster.local")
POSTGRES_PASSWORD = os.environ.get("POSTGRES_PASSWORD", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
BOT_TOKEN = os.environ.get("BOT_TOKEN")
TELEGRAM_USER_ID = os.environ.get("TELEGRAM_USER_ID", "")


# --------------------------------------------------------------------------
# Path allowlist — enforced at PROPOSAL time and re-checked at APPLY time.
# --------------------------------------------------------------------------
def validate_target(service: str, file_path: str):
    """Return (abs_path, None) if this file may be proposed, else (None, reason)."""
    if service not in ALLOWED_DIRS:
        return None, f"'{service}' is not a self-codable service (only bot, scheduler, dashboard)."
    base_dir = os.path.realpath(ALLOWED_DIRS[service])
    abs_path = os.path.realpath(os.path.join(base_dir, os.path.basename(file_path))
                               if os.path.basename(file_path) == file_path
                               else file_path)
    # must resolve to inside the one allowed dir (blocks .. traversal and symlinks out)
    if os.path.commonpath([abs_path, base_dir]) != base_dir:
        return None, f"{file_path} is outside {service}/ — refused."
    name = os.path.basename(abs_path).lower()
    if name in DENIED_BASENAMES:
        return None, f"{name} is off-limits to self-coding."
    if name.endswith(DENIED_SUFFIXES):
        return None, f"{name}: this file type ({os.path.splitext(name)[1]}) cannot be self-modified."
    if not name.endswith(".py"):
        return None, "Only .py source files can be self-modified."
    if not os.path.isfile(abs_path):
        return None, f"{abs_path} does not exist."
    return abs_path, None


# --------------------------------------------------------------------------
# Fixed rebuild actions — the ONLY commands apply/undo may ever run.
# service is validated before any of these run; no variable is interpolated
# into a shell string.
# --------------------------------------------------------------------------
def _run(argv, cwd=None, stdin_bytes=None):
    p = subprocess.run(argv, cwd=cwd, input=stdin_bytes,
                       capture_output=True, timeout=600)
    return p.returncode, (p.stdout or b"").decode(errors="ignore"), (p.stderr or b"").decode(errors="ignore")


REBUILD_WRAPPER = "/usr/local/sbin/ghost-rebuild"

def _rebuild_image_service(service: str, image: str):
    """bot / scheduler: delegate the whole build+import+rollout to the fixed
    root-owned wrapper via a single scoped NOPASSWD sudo call. `service` is one of
    the two literals validated by the caller and by the wrapper itself — no shell,
    no interpolation. Fails cleanly (returning False) when the sudo grant/wrapper
    isn't installed, so apply just records 'applied_no_rebuild' and undo still works."""
    if service not in ("bot", "scheduler"):
        return False, f"refusing rebuild for '{service}'"
    if not os.path.exists(REBUILD_WRAPPER):
        return False, (f"{REBUILD_WRAPPER} not installed — the scoped sudo rebuild grant "
                       f"is not set up yet (see selfcode/README-sudo.md). File + backup are written; "
                       f"rebuild {service} by hand or install the grant.")
    rc, out, err = _run(["sudo", "-n", REBUILD_WRAPPER, service])
    return rc == 0, f"$ sudo -n {REBUILD_WRAPPER} {service}\n{(out + err).strip()}"


def _rebuild_dashboard():
    """dashboard: refresh configmap from dashboard.py + rollout. kubectl only, no sudo."""
    dpy = os.path.join(ALLOWED_DIRS["dashboard"], "dashboard.py")
    log = []
    rc, cm_yaml, err = _run(["kubectl", "create", "configmap", "ghost-dashboard-code",
                             f"--from-file={dpy}", "-n", "ghost",
                             "--dry-run=client", "-o", "yaml"])
    if rc != 0:
        return False, "configmap render failed:\n" + err
    rc, out, err = _run(["kubectl", "apply", "-f", "-"], stdin_bytes=cm_yaml.encode())
    log.append(f"$ kubectl apply configmap\n{err.strip() or out.strip()}")
    if rc != 0:
        return False, "\n".join(log)
    rc, out, err = _run(["kubectl", "rollout", "restart", "deployment/ghost-dashboard", "-n", "ghost"])
    log.append(f"$ kubectl rollout restart deployment/ghost-dashboard\n{err.strip() or out.strip()}")
    return rc == 0, "\n".join(log)


def rebuild_service(service: str):
    """Dispatch to the one fixed rebuild for a validated service."""
    if service == "bot":
        return _rebuild_image_service("bot", "ghost-bot")
    if service == "scheduler":
        return _rebuild_image_service("scheduler", "ghost-scheduler")
    if service == "dashboard":
        return _rebuild_dashboard()
    return False, f"no rebuild defined for '{service}'"


# --------------------------------------------------------------------------
# Telegram (send only; the bot pod handles receiving/approval).
# --------------------------------------------------------------------------
async def send_telegram(message: str) -> bool:
    if not BOT_TOKEN:
        return False
    try:
        async with httpx.AsyncClient() as client:
            r = await client.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                                  json={"chat_id": TELEGRAM_USER_ID, "text": message}, timeout=20)
        return r.status_code == 200
    except Exception:
        return False


async def _pg():
    return await asyncpg.connect(host=POSTGRES_HOST, port=5432, user="postgres",
                                 password=POSTGRES_PASSWORD, database="ghost_db")


# --------------------------------------------------------------------------
# Task 1 — Proposal generation
# --------------------------------------------------------------------------
GEN_SYSTEM = """You are Ghost's code-fix generator. You are given one Python source file and a problem to fix in it.
Return the COMPLETE corrected file, nothing else — no markdown fences, no commentary before or after.
Make the smallest change that fixes the described problem. Do not refactor unrelated code, rename things, or reformat.
Preserve the existing style exactly. If the request is unclear or unsafe, return the file UNCHANGED."""


async def propose_change(service: str, file_path: str, problem_description: str) -> dict:
    """Generate a single-file diff + explanation and store it 'proposed'. One at a time."""
    abs_path, reason = validate_target(service, file_path)
    if reason:
        return {"ok": False, "error": reason}

    conn = await _pg()
    try:
        open_prop = await conn.fetchrow(
            "SELECT id FROM code_proposals WHERE status IN ('proposed','approved') ORDER BY id DESC LIMIT 1")
        if open_prop:
            return {"ok": False, "error": f"Proposal #{open_prop['id']} is still open. Resolve it (apply / no) before proposing another."}

        with open(abs_path, encoding="utf-8") as f:
            original = f.read()

        async with httpx.AsyncClient() as client:
            r = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={"x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01",
                         "content-type": "application/json"},
                json={"model": "claude-sonnet-4-6", "max_tokens": 32000,
                      "system": GEN_SYSTEM,
                      "messages": [{"role": "user",
                                    "content": f"File: {os.path.basename(abs_path)}\nProblem: {problem_description}\n\n--- CURRENT FILE ---\n{original}"}]},
                timeout=600)
        resp = r.json()
        # TRUNCATION GUARDS (added after self-review proposal #8 generated a diff that
        # silently deleted half of bot.py — max_tokens ran out mid-file and the "complete
        # corrected file" contract broke). A truncated regeneration must never become a
        # proposable diff.
        if resp.get("stop_reason") == "max_tokens":
            return {"ok": False, "error": f"{os.path.basename(abs_path)} is too large to regenerate within the "
                                          "output limit — the result would be truncated. Refusing to propose."}
        new_content = resp["content"][0]["text"]
        if new_content.startswith("```"):
            new_content = new_content.split("\n", 1)[1].rsplit("```", 1)[0]
        if not new_content.strip():
            return {"ok": False, "error": "Generator returned empty output — nothing proposed."}
        if len(new_content) < 0.5 * len(original):
            return {"ok": False, "error": "Generated file is less than half the original's size — that's a "
                                          "truncation or mass-deletion, not a fix. Refusing to propose."}
        try:
            import ast as _ast
            _ast.parse(new_content)
        except SyntaxError as e:
            return {"ok": False, "error": f"Generated file does not parse (SyntaxError: {e}) — refusing to propose."}
        if new_content == original:
            return {"ok": False, "error": "No change needed — the generator returned the file unchanged."}

        diff = "".join(difflib.unified_diff(
            original.splitlines(keepends=True), new_content.splitlines(keepends=True),
            fromfile=f"a/{os.path.basename(abs_path)}", tofile=f"b/{os.path.basename(abs_path)}"))

        # short plain-English explanation
        async with httpx.AsyncClient() as client:
            er = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={"x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01",
                         "content-type": "application/json"},
                json={"model": "claude-sonnet-4-6", "max_tokens": 300,
                      "system": "Explain this code change in 1-3 plain sentences: what changed and why. No preamble.",
                      "messages": [{"role": "user", "content": f"Problem: {problem_description}\n\nDiff:\n{diff}"}]},
                timeout=60)
        explanation = er.json()["content"][0]["text"].strip()

        pid = await conn.fetchval("""
            INSERT INTO code_proposals (service, file_path, diff, new_content, explanation, status)
            VALUES ($1, $2, $3, $4, $5, 'proposed') RETURNING id
        """, service, abs_path, diff, new_content, explanation)
    finally:
        await conn.close()

    added = sum(1 for l in diff.splitlines() if l.startswith("+") and not l.startswith("+++"))
    removed = sum(1 for l in diff.splitlines() if l.startswith("-") and not l.startswith("---"))
    shown = diff if len(diff) < 3000 else diff[:3000] + "\n... (diff truncated, full version stored)"
    msg = (f"PROPOSAL #{pid} — {service}/{os.path.basename(abs_path)}\n\n"
           f"{explanation}\n\n"
           f"(+{added} / -{removed} lines)\n\n{shown}\n\n"
           f"Reply \"apply this change\" to apply and rebuild {service}. "
           f"Reply \"no\" to reject. Expires in 24h.")
    await send_telegram(msg)
    return {"ok": True, "id": pid, "diff": diff, "explanation": explanation}


# --------------------------------------------------------------------------
# Task 3 — Apply (only for an approved proposal)
# --------------------------------------------------------------------------
async def apply_change(proposal_id: int) -> dict:
    conn = await _pg()
    try:
        row = await conn.fetchrow("SELECT * FROM code_proposals WHERE id = $1", proposal_id)
        if not row:
            return {"ok": False, "error": f"No proposal #{proposal_id}."}
        if row["status"] != "approved":
            return {"ok": False, "error": f"Proposal #{proposal_id} is '{row['status']}', not 'approved' — refusing to apply."}

        # re-validate the path at apply time — never trust the stored path blindly
        abs_path, reason = validate_target(row["service"], row["file_path"])
        if reason:
            await conn.execute("UPDATE code_proposals SET status='apply_failed', resolved_at=NOW() WHERE id=$1", proposal_id)
            return {"ok": False, "error": f"Apply refused: {reason}"}

        ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        backup_path = f"{abs_path}.{ts}.bak"
        shutil.copy2(abs_path, backup_path)
        with open(abs_path, "w", encoding="utf-8") as f:
            f.write(row["new_content"])

        ok, log = rebuild_service(row["service"])
        if ok:
            await conn.execute("UPDATE code_proposals SET status='applied', backup_path=$2, resolved_at=NOW() WHERE id=$1",
                               proposal_id, backup_path)
            await send_telegram(f"Applied proposal #{proposal_id} to {row['service']}. Backup: {os.path.basename(backup_path)}. Rebuild done. Say \"undo last change {row['service']}\" if it misbehaves.")
            return {"ok": True, "backup": backup_path, "log": log}
        else:
            # file + backup are on disk; rebuild failed (e.g. sudo unavailable from this context)
            await conn.execute("UPDATE code_proposals SET status='applied_no_rebuild', backup_path=$2, resolved_at=NOW() WHERE id=$1",
                               proposal_id, backup_path)
            await send_telegram(f"Proposal #{proposal_id}: file written + backed up, but the {row['service']} rebuild did not complete here (needs your sudo). Run the {row['service']} rebuild, or \"undo last change {row['service']}\".")
            return {"ok": False, "error": "written but rebuild failed", "backup": backup_path, "log": log}
    finally:
        await conn.close()


# --------------------------------------------------------------------------
# Task 4 — Undo (restore most recent backup for a service, rebuild)
# --------------------------------------------------------------------------
async def undo_last_change(service: str) -> dict:
    if service not in ALLOWED_DIRS:
        return {"ok": False, "error": f"'{service}' is not a self-codable service."}
    conn = await _pg()
    try:
        row = await conn.fetchrow(
            "SELECT * FROM code_proposals WHERE service=$1 AND status IN ('applied','applied_no_rebuild') "
            "AND backup_path IS NOT NULL ORDER BY resolved_at DESC LIMIT 1", service)
        if not row:
            return {"ok": False, "error": f"No applied change to undo for {service}."}
        backup_path = row["backup_path"]
        abs_path, reason = validate_target(service, row["file_path"])
        if reason:
            return {"ok": False, "error": f"Undo refused: {reason}"}
        if not os.path.isfile(backup_path):
            return {"ok": False, "error": f"Backup {backup_path} is missing — cannot undo."}
        shutil.copy2(backup_path, abs_path)
        ok, log = rebuild_service(service)
        await conn.execute("UPDATE code_proposals SET status='undone' WHERE id=$1", row["id"])
        if ok:
            await send_telegram(f"Undid proposal #{row['id']} on {service} — restored the previous version and rebuilt.")
            return {"ok": True, "restored_from": backup_path, "log": log}
        await send_telegram(f"Restored {service} from backup, but its rebuild needs your sudo to finish.")
        return {"ok": False, "error": "restored but rebuild failed", "log": log}
    finally:
        await conn.close()


async def expire_stale() -> int:
    conn = await _pg()
    try:
        rows = await conn.fetch(
            "UPDATE code_proposals SET status='expired', resolved_at=NOW() "
            "WHERE status='proposed' AND created_at < NOW() - INTERVAL '24 hours' RETURNING id")
        return len(rows)
    finally:
        await conn.close()


if __name__ == "__main__":
    import asyncio
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        sys.exit(0)
    cmd = args[0]
    if cmd == "propose" and len(args) >= 4:
        print(asyncio.run(propose_change(args[1], args[2], " ".join(args[3:]))))
    elif cmd == "apply" and len(args) == 2:
        print(asyncio.run(apply_change(int(args[1]))))
    elif cmd == "undo" and len(args) == 2:
        print(asyncio.run(undo_last_change(args[1])))
    elif cmd == "expire":
        print("expired:", asyncio.run(expire_stale()))
    else:
        print(__doc__)
        sys.exit(2)
