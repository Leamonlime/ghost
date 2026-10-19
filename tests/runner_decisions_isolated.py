#!/usr/bin/env python3
"""ghost-runner decision-loop tests, fully isolated: a stub `claude`, a throwaway git repo, a
throwaway vault and a throwaway Postgres schema (zzt_run_<pid>, dropped at the end). Never touches
the real repo, vault, decisions table or Telegram (stdout notifier).

Needs ~/.ghost-runner/runner.py and ~/.ghost-runner/host.env (Postgres creds). Skips if absent.
  python3 tests/runner_decisions_isolated.py
"""
import asyncio
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HOME = Path.home()
RUNNER = HOME / ".ghost-runner" / "runner.py"
REPO = Path(__file__).resolve().parent.parent
ENV_FILE = HOME / ".ghost-runner" / "host.env"
if not RUNNER.exists() or not ENV_FILE.exists():
    print("SKIP: runner.py or host.env not present")
    sys.exit(0)

TMP = Path(tempfile.mkdtemp(prefix="zzt_runner_"))
os.environ["GHOST_RUNNER_HOME"] = str(TMP / "rundir")
SCHEMA = f"zzt_run_{os.getpid()}"
os.environ["GHOST_RUNNER_DB_SCHEMA"] = SCHEMA
(TMP / "rundir").mkdir()

spec = importlib.util.spec_from_file_location("runner_under_test", RUNNER)
R = importlib.util.module_from_spec(spec)
spec.loader.exec_module(R)

FAILS, PASSES = [], 0


def check(name, cond, extra=""):
    global PASSES
    if cond:
        PASSES += 1
    else:
        FAILS.append(name)
        print(f"FAIL: {name} {extra}")


def sh(*a, cwd=None):
    return subprocess.run(a, cwd=cwd, capture_output=True, text=True).stdout.strip()


# ---- throwaway git repo (master) + vault + stub claude ---------------------------------------
repo = TMP / "repo"
repo.mkdir()
sh("git", "init", "-q", "-b", "master", cwd=repo)
sh("git", "config", "user.email", "t@t", cwd=repo)
sh("git", "config", "user.name", "t", cwd=repo)
(repo / "bot").mkdir()
(repo / "bot" / "x.py").write_text("x = 1\n")
(repo / "README.md").write_text("hi\n")
sh("git", "add", "-A", cwd=repo)
sh("git", "commit", "-q", "-m", "init", cwd=repo)

vault = TMP / "vault"
(vault / "Proj").mkdir(parents=True)
notes = vault / "Proj" / "T — Build Prompts.md"
LONG_STATUS = "**Status:** needs-lea — branch old — " + "a very long stale question " * 40
notes.write_text(f"# Prompts\n\n## Alpha task\n**Status:** queued 01/01/2026\n\nDo alpha.\n\n---\n\n"
                 f"## Beta task\n{LONG_STATUS}\n\nBody of beta, must survive.\n")
(vault / "Proj" / "T — Build Log.md").write_text("# Log\n\n## Session 1 — x\n\nold\n")
(vault / "Proj" / "archive.md").write_text("# Archive\n")
stub = TMP / "claude"
stub.write_text('#!/usr/bin/env python3\nimport sys\nprint(open(__file__.replace("claude","answer.json")).read())\n')
stub.chmod(0o755)

(TMP / "rundir" / "config.toml").write_text(f'''
timezone = "Australia/Melbourne"
start_time = "23:00"
cutoff = "02:00"
hard_stop = "06:00"
max_session_minutes = 5
quiet_minutes = 0
vault = "{vault}"
claude_bin = "{stub}"
model = "opus"
notifier = "stdout"
[decisions]
module = "{REPO}/bot/decisions.py"
env_file = "{ENV_FILE}"
[projects.ghost]
name = "Ghost"
unattended = true
note = "Proj/T — Build Prompts.md"
archive_note = "Proj/archive.md"
build_log = "Proj/T — Build Log.md"
repo = "{repo}"
base_branch = "master"
pre_gate = ["true"]
post_gate = ["true"]
no_deploy = ["README.md"]
[[projects.ghost.rules]]
match = "bot/"
component = "bot"
lea = "bot change merged, not deployed. Run: ghost-deploy bot"
''')
cfg = R.load_config()
proj = cfg["projects"]["ghost"]


async def _mk_schema():
    import asyncpg
    env = R._env_file(cfg["decisions"]["env_file"])
    c = await asyncpg.connect(host=env["POSTGRES_HOST"], port=5432, user="postgres", password=env["POSTGRES_PASSWORD"], database="ghost_db")
    await c.execute(f'CREATE SCHEMA "{SCHEMA}"')
    await c.close()
asyncio.run(_mk_schema())
R.STATE_DIR.mkdir(parents=True, exist_ok=True)
R.LOG_DIR.mkdir(parents=True, exist_ok=True)
R.FOLLOWUP_DIR.mkdir(parents=True, exist_ok=True)


def answer_json(**res):
    (TMP / "answer.json").write_text(json.dumps({"is_error": False, "structured_output": res}))


def decisions_rows(where="true"):
    async def go(conn, D):
        return [dict(r) for r in await conn.fetch(f"SELECT * FROM decisions WHERE {where} ORDER BY id")]
    return R.db_run(cfg, go)


def run_alpha(commit_file=None):
    """Run the queued 'Alpha task' entry through run_entry with the stub session."""
    entry = next(e for e in R.parse_entries(notes) if e.title == "Alpha task")
    if commit_file:   # the stub can't edit files itself; pre-seed what 'the session' would have committed
        pass
    v = R.Vault(cfg, 0, dry=False)
    night = R.Night()
    R.run_entry(cfg, proj, entry, v, night, __import__("datetime").datetime.now(cfg["tz"]) + __import__("datetime").timedelta(hours=1))
    return night, entry


try:
    LONG_Q = "Should we retire the sync? " + ("context " * 200).strip()    # > 1600 chars, far past the old 300/600 clips
    block = ("```DECISIONS\n" + json.dumps([{
        "question": LONG_Q,
        "options": [{"key": "retire", "label": "Retire", "consequence": "turns it off"},
                    {"key": "keep", "label": "Keep", "consequence": "stays on"}],
        "recommended_key": "retire", "merge_state": "merged", "deploy_steps": "ghost-deploy scheduler"}]) + "\n```")

    # 1) needs-lea WITH a DECISIONS block: full text stored, status short, merge_state is the truth
    #    (the branch has no commits -> 'n/a', whatever the model claimed)
    answer_json(status="needs-lea", title="t", summary="s", question=block)
    night, entry = run_alpha()
    rows = decisions_rows()
    check("1: one decision stored", len(rows) == 1, str(len(rows)))
    check("1: question stored IN FULL (not clipped)", rows[0]["question"] == LONG_Q and len(rows[0]["question"]) > 1500)
    check("1: options + recommendation kept", [o["key"] for o in json.loads(rows[0]["options"])] == ["retire", "keep"]
          and rows[0]["recommended_key"] == "retire")
    check("1: merge_state is the runner's truth, not the model's claim", rows[0]["merge_state"] == "n/a", rows[0]["merge_state"])
    after = notes.read_text()
    check("1: vault status line is the short form",
          "## Alpha task\n**Status:** needs-lea: 1 decision pending (see Telegram / Ghost app)\n" in after)
    check("1: no other vault text changed",
          after.replace("**Status:** needs-lea: 1 decision pending (see Telegram / Ghost app)", "**Status:** queued 01/01/2026")
          == f"# Prompts\n\n## Alpha task\n**Status:** queued 01/01/2026\n\nDo alpha.\n\n---\n\n## Beta task\n{LONG_STATUS}\n\nBody of beta, must survive.\n")
    check("1: Build Log + archive untouched", (vault / "Proj" / "T — Build Log.md").read_text() == "# Log\n\n## Session 1 — x\n\nold\n"
          and (vault / "Proj" / "archive.md").read_text() == "# Archive\n")
    check("1: night.needs says count, not the question", night.needs and "1 decision" in night.needs[0] and LONG_Q[:40] not in night.needs[0])
    check("1: repo back on master, no stray branch", sh("git", "branch", "--show-current", cwd=repo) == "master")

    # 2) re-running the same stop is idempotent (same slug + question)
    notes.write_text(notes.read_text().replace("**Status:** needs-lea: 1 decision pending (see Telegram / Ghost app)", "**Status:** queued 01/01/2026", 1))
    night, entry = run_alpha()
    check("2: idempotent insert", len(decisions_rows()) == 1)

    # 3) needs-lea WITHOUT a block: the whole raw message becomes one free-text decision
    notes.write_text(notes.read_text().replace("**Status:** needs-lea: 1 decision pending (see Telegram / Ghost app)", "**Status:** queued 01/01/2026", 1))
    RAW = "I could not decide. " + "x" * 3000
    answer_json(status="needs-lea", title="t", summary="s", question=RAW)
    run_alpha()
    rows = decisions_rows()
    raw_row = [r for r in rows if r["question"] == RAW]
    check("3: raw stop message stored in full as one free-text decision", len(raw_row) == 1 and json.loads(raw_row[0]["options"]) == [])
    check("3: status line counts 2 pending", "needs-lea: 2 decisions pending (see Telegram / Ghost app)" in notes.read_text())

    # 4) refresh_status_lines follows answers; only a needs-lea status line is ever rewritten
    async def ans(conn, D):
        return await D.answer(conn, rows[0]["id"], "retire", via="test")
    out, _ = R.db_run(cfg, ans)
    check("4: answered", out == "answered")
    R.sync_decisions(cfg, R.Vault(cfg, 0, dry=False))
    check("4: status line down to 1 pending", "needs-lea: 1 decision pending (see Telegram / Ghost app)" in notes.read_text())
    beta_before = "## Beta task\n" + LONG_STATUS
    check("4: other entry's status line untouched (not a decision slug)", beta_before in notes.read_text())

    # 5) follow-up file from the fixed template, queued once
    fu = sorted(R.FOLLOWUP_DIR.glob("*.md"))
    check("5: exactly one follow-up file", len(fu) == 1, str(fu))
    txt = fu[0].read_text()
    check("5: template carries decision, answer, branch, deploy notes",
          LONG_Q in txt and "Lea's answer: Retire (option: retire)" in txt and "expected consequence: turns it off" in txt
          and "Original deploy notes: ghost-deploy scheduler" in txt and "Branch: none (work from master)" in txt)
    check("5: template has the fixed steps", all(k in txt for k in ("Apply the answer exactly", "Add or update tests", "never merge yourself", "never deploy")))
    check("5: followup_queued_at set", decisions_rows(f"id={rows[0]['id']}")[0]["followup_queued_at"] is not None)
    R.sync_decisions(cfg)
    check("5: second sync doesn't duplicate", len(list(R.FOLLOWUP_DIR.glob("*.md"))) == 1)
    check("5: follow-ups live outside the vault", str(R.FOLLOWUP_DIR).startswith(str(TMP / "rundir")) and not str(R.FOLLOWUP_DIR).startswith(str(vault)))

    # 6) queued_followups: unmerged decision branch -> stack on it; merged -> master
    sh("git", "checkout", "-q", "-b", "side", cwd=repo)
    (repo / "bot" / "y.py").write_text("y = 1\n")
    sh("git", "add", "-A", cwd=repo)
    sh("git", "commit", "-q", "-m", "side work", cwd=repo)
    sh("git", "checkout", "-q", "master", cwd=repo)
    R.db_run(cfg, lambda conn, D: conn.execute("UPDATE decisions SET branch='side', merge_state='on_branch' WHERE id=$1", rows[0]["id"]))
    q = R.queued_followups(cfg, proj)
    check("6: unmerged branch -> base_ref is the branch", len(q) == 1 and q[0].base_ref == "side", str([e.base_ref for e in q]))
    sh("git", "merge", "-q", "--no-ff", "side", "-m", "merge side", cwd=repo)
    q = R.queued_followups(cfg, proj)
    check("6: merged branch -> base_ref is master", len(q) == 1 and q[0].base_ref is None)

    # 7) full follow-up run: stub 'done' session that commits a bot/ change -> merged, NOT deployed,
    #    a Deploy-button decision appears (merged state), follow-up marked applied, file archived
    async def commit_bot(conn, D):
        return None
    (repo / "bot" / "x.py").write_text("x = 2\n")   # leave a change on master? no — the session branch commits it
    sh("git", "checkout", "--", "bot/x.py", cwd=repo)
    stub.write_text('#!/usr/bin/env python3\nimport subprocess, json, os\nr = os.getcwd()\n'
                    'open("bot/x.py","w").write("x = 3\\n")\nsubprocess.run(["git","add","-A"])\n'
                    'subprocess.run(["git","-c","user.name=s","-c","user.email=s@s","commit","-q","-m","session work"])\n'
                    'print(json.dumps({"is_error": False, "structured_output": {"status":"done","title":"Retire sync","summary":"did it"}}))\n')
    night = R.Night()
    v = R.Vault(cfg, 0, dry=False)
    q = R.queued_followups(cfg, proj)
    R.run_entry(cfg, proj, q[0], v, night, __import__("datetime").datetime.now(cfg["tz"]) + __import__("datetime").timedelta(hours=1))
    check("7: bot change merged to master", (repo / "bot" / "x.py").read_text() == "x = 3\n" and "overnight/" in sh("git", "log", "--oneline", "-3", cwd=repo))
    dep = decisions_rows("kind='deploy'")
    check("7: Deploy decision created for bot, merge_state merged",
          len(dep) == 1 and dep[0]["merge_state"] == "merged" and dep[0]["deploy_steps"] == "ghost-deploy bot"
          and [o["key"] for o in json.loads(dep[0]["options"])] == ["deploy", "later"], str(dep))
    check("7: follow-up decision applied + file moved to done/",
          decisions_rows(f"id={rows[0]['id']}")[0]["status"] == "applied"
          and len(list((R.FOLLOWUP_DIR / "done").glob("*.md"))) == 1)
    check("7: nothing was deployed unattended", not night.failed and any("1 decision" in n for n in night.needs), str(night.needs))
    log_txt = (vault / "Proj" / "T — Build Log.md").read_text()
    check("7: merged-but-undeployed writes no Build Log entry (same as any needs-lea merge)", "## Session 2" not in log_txt)
    check("7: archive/prompt note not touched by a follow-up", (vault / "Proj" / "archive.md").read_text() == "# Archive\n")

    # 7b) a clean follow-up (no deploy needed) -> merged, Build Log entry, applied, file in done/
    async def mk(conn, D):
        did, _ = await D.insert_decision(conn, "clean", "Tidy the readme?", [{"key": "yes", "label": "Yes", "consequence": "tidied"}], "yes", branch=None, merge_state="n/a")
        await D.answer(conn, did, "yes", via="test")
        return did
    cid = R.db_run(cfg, mk)
    R.sync_decisions(cfg)
    stub.write_text('#!/usr/bin/env python3\nimport subprocess, json\nopen("README.md","w").write("hello\\n")\nsubprocess.run(["git","add","-A"])\n'
                    'subprocess.run(["git","-c","user.name=s","-c","user.email=s@s","commit","-q","-m","readme"])\n'
                    'print(json.dumps({"is_error": False, "structured_output": {"status":"done","title":"Tidy readme","summary":"tidied"}}))\n')
    night = R.Night()
    q = [e for e in R.queued_followups(cfg, proj) if e.followup_id == cid]
    R.run_entry(cfg, proj, q[0], R.Vault(cfg, 0, dry=False), night, __import__("datetime").datetime.now(cfg["tz"]) + __import__("datetime").timedelta(hours=1))
    check("7b: clean follow-up shipped + applied", night.shipped and decisions_rows(f"id={cid}")[0]["status"] == "applied", str(night.shipped))
    check("7b: Build Log entry added", "Tidy readme" in (vault / "Proj" / "T — Build Log.md").read_text())

    # 8) deploy safety: claim is single-shot, only whitelisted components, no tap = no deploy
    nothing = R.execute_deploys(cfg, "stdout")
    check("8: unanswered Deploy decision does nothing", nothing == [])
    async def tap_bad(conn, D):
        did, _ = await D.insert_decision(conn, "evil", "Run something else?", [{"key": "deploy", "label": "Go", "consequence": ""}],
                                         None, deploy_steps="ghost-deploy bot; rm -rf ~", kind="deploy")
        return await D.answer(conn, did, "deploy", via="test")
    R.db_run(cfg, tap_bad)
    out = R.execute_deploys(cfg, "stdout")
    bad = decisions_rows("prompt_slug='evil'")[0]
    check("8: non-whitelisted deploy_steps refused, never executed", out and "not a deployable component" in (bad["result"] or ""), str(bad["result"]))
    check("8: refused tap is not retried", R.execute_deploys(cfg, "stdout") == [])
    # 'later' / manual answers are acknowledged with no action
    async def later(conn, D):
        await D.answer(conn, dep[0]["id"], "later", via="test")
    R.db_run(cfg, later)
    R.sync_decisions(cfg)
    check("8: 'Not now' acknowledged, nothing deployed", decisions_rows(f"id={dep[0]['id']}")[0]["status"] == "applied")

    # 9) outbox: DB unreachable -> full text parked locally, status line says so, nothing lost
    saved = R._env_file
    R._env_file = lambda p: {"POSTGRES_HOST": "127.0.0.1", "POSTGRES_PASSWORD": "x"}
    ids, n = R.record_decisions(cfg, "offline", None, "n/a", [R.dec_mod(cfg).raw_decision("lost question " * 100)])
    R._env_file = saved
    parked = list(R.OUTBOX_DIR.glob("*.json"))
    check("9: DB down -> parked in outbox in full", ids is None and len(parked) == 1 and "lost question" * 1 in parked[0].read_text())
    check("9: status line says so", "database unreachable" in R.decision_status_line(None))
    check("9: outbox flushes when DB is back", R.flush_outbox(cfg) == 1 and not list(R.OUTBOX_DIR.glob("*.json")))

    # 10) cards: render_card + buttons through the stdout notifier
    import io, contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        sent = R.send_pending_cards(cfg, "stdout")
    o = buf.getvalue()
    check("10: card per pending decision, recommendation + Other… button", sent >= 1 and "Recommended: Retire" in o or "Other…" in o)
    check("10: callback data is deterministic dec:<id>:<key>", "dec:" in o and ":other" in o)
finally:
    async def drop():
        import asyncpg
        env = R._env_file(cfg["decisions"]["env_file"])
        c = await asyncpg.connect(host=env["POSTGRES_HOST"], port=5432, user="postgres", password=env["POSTGRES_PASSWORD"], database="ghost_db")
        await c.execute(f'DROP SCHEMA IF EXISTS "{SCHEMA}" CASCADE')
        await c.close()
    asyncio.run(drop())
    shutil.rmtree(TMP, ignore_errors=True)

print(f"{PASSES} passed, {len(FAILS)} failed")
sys.exit(1 if FAILS else 0)
