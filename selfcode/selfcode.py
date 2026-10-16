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
import time
import json
import hashlib
from datetime import datetime, timezone, timedelta

import asyncpg
import httpx

GHOST_ROOT = os.environ.get("GHOST_ROOT") or os.path.expanduser("~/ghost")
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


def _git_build_info():
    """{commit, commit_subject, dirty} for the GHOST_ROOT checkout. Any git failure gives
    nulls — build info is a display nicety and must never block a deploy."""
    info = {"commit": None, "commit_subject": None, "dirty": None}
    try:
        rc, out, _ = _run(["git", "rev-parse", "--short", "HEAD"], cwd=GHOST_ROOT)
        if rc == 0 and out.strip():
            info["commit"] = out.strip()
        rc, out, _ = _run(["git", "log", "-1", "--format=%s"], cwd=GHOST_ROOT)
        if rc == 0:
            info["commit_subject"] = (out.strip().splitlines() or [None])[0]
        rc, out, _ = _run(["git", "status", "--porcelain"], cwd=GHOST_ROOT)
        if rc == 0:
            info["dirty"] = bool(out.strip())
    except Exception:
        pass
    return info


def _write_build_info():
    """Write build_info.json to a temp file (never committed) for the dashboard configmap.
    Returns the path, or None if even the temp write fails (deploy proceeds without it)."""
    try:
        import tempfile
        fd, path = tempfile.mkstemp(prefix="ghost-build-info-", suffix=".json")
        with os.fdopen(fd, "w") as f:
            json.dump(_git_build_info(), f)
        return path
    except Exception:
        return None


def _rebuild_dashboard():
    """dashboard: refresh configmap from dashboard.py + rollout. kubectl only, no sudo."""
    dpy = os.path.join(ALLOWED_DIRS["dashboard"], "dashboard.py")
    log = []
    bi_path = _write_build_info()
    argv = ["kubectl", "create", "configmap", "ghost-dashboard-code", f"--from-file={dpy}"]
    if bi_path:
        argv.append(f"--from-file=build_info.json={bi_path}")
    rc, cm_yaml, err = _run(argv + ["-n", "ghost", "--dry-run=client", "-o", "yaml"])
    if bi_path:
        try:
            os.unlink(bi_path)
        except OSError:
            pass
    if rc != 0:
        return False, "configmap render failed:\n" + err
    rc, out, err = _run(["kubectl", "apply", "-f", "-"], stdin_bytes=cm_yaml.encode())
    log.append(f"$ kubectl apply configmap\n{err.strip() or out.strip()}")
    if rc != 0:
        return False, "\n".join(log)
    rc, out, err = _run(["kubectl", "rollout", "restart", "deployment/ghost-dashboard", "-n", "ghost"])
    log.append(f"$ kubectl rollout restart deployment/ghost-dashboard\n{err.strip() or out.strip()}")
    return rc == 0, "\n".join(log)


# --------------------------------------------------------------------------
# Session 44 — real post-deploy verification. The root ghost-rebuild wrapper (and
# the old configmap path) declared success the moment `kubectl rollout restart` was
# ISSUED, never confirming the new pod is actually healthy and running the new code —
# the gap that let Session 10's rebuild go unconfirmed. These checks (rollout status,
# pod health, md5 disk==pod for images; rollout + live version-stamp + 200 for the
# dashboard) are the same three every session has run by hand, now built into the
# tooling so they can't be skipped. Failure is loud and non-zero; auto-rollback is
# deliberately out of scope.
# --------------------------------------------------------------------------
_IMAGE_FILES = {"bot": ["bot.py", "habitica.py", "fatsecret.py", "decisions.py"],
                "scheduler": ["scheduler.py", "habitica.py", "habitica_sync.py", "fatsecret.py", "decisions.py"]}

def _newest_pod(app_label: str) -> str:
    rc, out, err = _run(["kubectl", "get", "pods", "-n", "ghost", "-l", f"app={app_label}",
                         "--sort-by=.metadata.creationTimestamp",
                         "-o", "jsonpath={.items[-1:].metadata.name}"])
    return out.strip() if rc == 0 else ""

def _verify_image_deploy(service: str, image: str, since_dt):
    """(ok, log, problems) — rollout completed, newest pod Running/Ready/0-restarts/fresh,
    and md5(disk)==md5(/app/…) for every copied source file."""
    problems, log = [], []
    rc, out, err = _run(["kubectl", "rollout", "status", f"deployment/{image}",
                         "-n", "ghost", "--timeout=180s"])
    log.append(f"rollout status: {(out + err).strip() or 'ok'}")
    if rc != 0:
        problems.append(f"rollout status did not complete: {(err or out).strip()[:120]}")
    pod = _newest_pod(image)
    if not pod:
        problems.append(f"no pod found for deployment/{image}")
    else:
        rc, js, err = _run(["kubectl", "get", "pod", pod, "-n", "ghost", "-o", "json"])
        try:
            d = json.loads(js)
        except Exception:
            d = {}
        st = d.get("status", {})
        phase = st.get("phase")
        cs = (st.get("containerStatuses") or [{}])[0]
        ready = cs.get("ready")
        restarts = cs.get("restartCount", 0)
        created = d.get("metadata", {}).get("creationTimestamp", "")
        fresh = False
        try:
            cdt = datetime.strptime(created, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            fresh = cdt >= since_dt
        except Exception:
            pass
        log.append(f"pod {pod}: phase={phase} ready={ready} restarts={restarts} created={created} fresh={fresh}")
        if phase != "Running":
            problems.append(f"pod {pod} phase={phase} (want Running)")
        if not ready:
            problems.append(f"pod {pod} not Ready (crash-looping or still starting?)")
        if restarts and int(restarts) > 0:
            problems.append(f"pod {pod} restartCount={restarts} (crash-looping on start?)")
        if not fresh:
            problems.append(f"pod {pod} is NOT fresh (created {created}) — the old pod never cycled")
        for fn in _IMAGE_FILES.get(service, []):
            disk = os.path.join(ALLOWED_DIRS[service], fn)
            try:
                dm = hashlib.md5(open(disk, "rb").read()).hexdigest()
            except Exception as e:
                problems.append(f"md5 disk read failed {fn}: {e}")
                continue
            rc, pm, err = _run(["kubectl", "exec", pod, "-n", "ghost", "--", "md5sum", f"/app/{fn}"])
            pmh = pm.split()[0] if pm.split() else ""
            match = dm == pmh
            log.append(f"md5 {fn}: disk={dm[:8]} pod={pmh[:8]} {'OK' if match else 'MISMATCH'}")
            if not match:
                problems.append(f"md5 mismatch {fn}: disk {dm[:8]} != pod {pmh[:8]}")
    return (len(problems) == 0), "\n".join(log), problems

def _verify_dashboard_deploy(since_dt):
    """(ok, log, problems) — rollout completed, and the LIVE serving process reports the
    just-deployed source md5 via /api/version (closes the Session-31 caching/pip-timing
    gap: not just that a page loads, but that the NEW page loads), plus a 200 on /."""
    problems, log = [], []
    rc, out, err = _run(["kubectl", "rollout", "status", "deployment/ghost-dashboard",
                         "-n", "ghost", "--timeout=180s"])
    log.append(f"rollout status: {(out + err).strip() or 'ok'}")
    if rc != 0:
        problems.append(f"rollout status did not complete: {(err or out).strip()[:120]}")
    disk = os.path.join(ALLOWED_DIRS["dashboard"], "dashboard.py")
    try:
        disk_md5 = hashlib.md5(open(disk, "rb").read()).hexdigest()
    except Exception as e:
        return False, f"disk md5 read failed: {e}", ["disk md5 read failed"]
    rc, cip, err = _run(["kubectl", "get", "svc", "ghost-dashboard", "-n", "ghost",
                         "-o", "jsonpath={.spec.clusterIP}"])
    cip = cip.strip()
    if not cip:
        problems.append("could not resolve dashboard clusterIP")
        return False, "\n".join(log), problems
    base = f"http://{cip}:8080"
    matched, last = False, ""
    for _ in range(45):  # ~45 * 4s = 180s; dashboard pip-installs at startup before serving
        try:
            r = httpx.get(f"{base}/api/version", timeout=5)
            last = r.text.strip()[:120]
            if r.status_code == 200 and disk_md5 in r.text:
                matched = True
                break
        except Exception as e:
            last = f"{type(e).__name__}: {str(e)[:50]}"
        time.sleep(4)
    log.append(f"/api/version: {'MATCH ' + disk_md5[:8] if matched else 'NO MATCH'} (last: {last})")
    if not matched:
        problems.append("dashboard /api/version never reported the deployed source md5 "
                        "(stale/old serving process, or startup failed)")
    try:
        r = httpx.get(f"{base}/", timeout=8)
        log.append(f"GET / -> {r.status_code}")
        if r.status_code != 200:
            problems.append(f"GET / returned {r.status_code} (want 200)")
    except Exception as e:
        problems.append(f"GET / failed: {type(e).__name__}")
    return (len(problems) == 0), "\n".join(log), problems

def rebuild_service(service: str):
    """Dispatch to the one fixed rebuild for a validated service, THEN verify the deploy
    genuinely landed (Session 44). Returns (ok, log) where ok is True ONLY if the rebuild
    ran AND verification passed — a rollout that never became healthy, a stale/crash-looping
    pod, an md5 mismatch, or a stale dashboard serving process all make ok False (loud)."""
    since = datetime.now(timezone.utc) - timedelta(seconds=5)  # freshness margin vs clock skew
    if service == "bot":
        ok, log = _rebuild_image_service("bot", "ghost-bot")
        if not ok:
            return False, log
        vok, vlog, probs = _verify_image_deploy("bot", "ghost-bot", since)
    elif service == "scheduler":
        ok, log = _rebuild_image_service("scheduler", "ghost-scheduler")
        if not ok:
            return False, log
        vok, vlog, probs = _verify_image_deploy("scheduler", "ghost-scheduler", since)
    elif service == "dashboard":
        ok, log = _rebuild_dashboard()
        if not ok:
            return False, log
        vok, vlog, probs = _verify_dashboard_deploy(since)
    else:
        return False, f"no rebuild defined for '{service}'"
    full = log + "\n--- post-deploy verification ---\n" + vlog
    if not vok:
        full += "\nFAILURE: " + "; ".join(probs)
    return vok, full


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
    elif cmd == "rebuild" and len(args) == 2:
        # Session 44: rebuild + real post-deploy verification. Used by `ghost-deploy`.
        svc = args[1]
        if svc not in ("bot", "scheduler", "dashboard"):
            print(f"FAILURE: unknown service '{svc}' — bot | scheduler | dashboard", file=sys.stderr)
            sys.exit(2)
        ok, log = rebuild_service(svc)
        print(log)
        if ok:
            print(f"VERIFIED: {svc} rebuilt, rolled out, and confirmed running the new code.")
            sys.exit(0)
        else:
            print(f"FAILURE: {svc} deploy did NOT verify — do not trust it as deployed (see above).",
                  file=sys.stderr)
            sys.exit(1)
    else:
        print(__doc__)
        sys.exit(2)
