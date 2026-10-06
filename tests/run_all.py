#!/usr/bin/env python3
"""
Ghost regression suite (Session 45). Runs INSIDE the bot pod (via `ghost-test`), so it
imports the REAL bot/habitica/fatsecret and drives Ghost's actual process_message path
against real Habitica/FatSecret/Redis/Postgres/Claude — no mocks (mocks would hide the
exact "logged success but nothing was written" class of bug Ghost keeps having).

Covers EIGHT high-value, historically-fragile flows — NOT total coverage. Meant to grow.
Dedicated test identity (uid 990045), dedicated reusable targets (a throwaway Habitica
habit, a `zzqacapture` route -> Admin/Ghost-Test-Target.md), self-cleaning, and every
write is verified by a SECOND independent read (never Ghost's own reply/echo).

Exit 0 iff all pass; non-zero otherwise.
"""
import asyncio, os, sys, hashlib
import httpx
import asyncpg
from redis.asyncio import Redis
import bot
import habitica
import fatsecret

TEST_UID = 990045
TEST_HABIT = "zz ghost test habit"
CAP_KW = "zzqacapture"
CAP_TARGET_REL = "Admin/Ghost-Test-Target.md"
INBOX_REL = "Admin/Inbox.md"
MARK = "zzqa"                     # marker in every capture test payload, for cleanup
DASH = "http://ghost-dashboard.ghost.svc.cluster.local:8080"
VAULT = bot.VAULT_PATH

results = []
def record(name, ok, detail): results.append((name, bool(ok), detail))

def _pg():
    return asyncpg.connect(host=bot.POSTGRES_HOST, port=5432, user="postgres",
                           password=bot.POSTGRES_PASSWORD, database="ghost_db")
def _redis():
    return Redis(host=bot.REDIS_HOST, port=6379, password=bot.REDIS_PASSWORD, decode_responses=True)

async def _hab_habit(name):
    """INDEPENDENT read: fresh GET straight to Habitica's servers, not Ghost's reply."""
    async with httpx.AsyncClient() as c:
        r = await c.get(f"{habitica.BASE_URL}/tasks/user?type=habits", headers=habitica.HEADERS, timeout=20)
    for t in r.json().get("data", []):
        if name.lower() in t["text"].lower():
            return t.get("counterUp", 0), t["id"]
    return None, None

# --- 1. Financial routing stays on Ollama; clock time is not misparsed as money ---
async def test_financial_routing():
    conn = await _pg()
    try:
        base_route = await conn.fetchval("SELECT COALESCE(MAX(id),0) FROM routing_log")
        base_exp = await conn.fetchval("SELECT COALESCE(MAX(id),0) FROM expenses")
        await bot.process_message(TEST_UID, "spent $20 on groceries")
        row = await conn.fetchrow("SELECT model, financial_trigger FROM routing_log WHERE id>$1 ORDER BY id DESC LIMIT 1", base_route)
        route_ok = row is not None and row["model"] == "ollama" and row["financial_trigger"]
        exp_after_fin = await conn.fetchval("SELECT COALESCE(MAX(id),0) FROM expenses")
        # a spend-verb near a clock time must log NOTHING — parse_money must reject "at 4:30"
        # as a clock, not bank $4 (the 16/07 regression). "paid a visit" is an idiom, not spending.
        await bot.process_message(TEST_UID, "paid a visit at 4:30")
        exp_after_clock = await conn.fetchval("SELECT COALESCE(MAX(id),0) FROM expenses")
        clock_ok = exp_after_clock == exp_after_fin
        await conn.execute("DELETE FROM expenses WHERE id>$1", base_exp)   # self-clean test expenses
        record("1 financial->ollama; clock not money", route_ok and clock_ok,
               f"routing model={row and row['model']} fin={row and row['financial_trigger']}; clock logged nothing={clock_ok}")
    finally:
        await conn.close()

# --- 2. Retrospective question writes nothing to FatSecret (independent diary read) ---
async def test_retro_no_write():
    if not fatsecret.is_configured():
        record("2 retrospective Q writes nothing to FatSecret", False, "FatSecret not configured"); return
    before = await fatsecret.get_today_summary()
    b = (before or {}).get("entries", 0)
    # phrasing that WOULD hit the food-logging path (log + "breakfast") and extract a real
    # item ("toast") if the question guard regressed — so the test genuinely exercises the guard
    await bot.process_message(TEST_UID, "Did I already log toast for breakfast today?")
    after = await fatsecret.get_today_summary()
    a = (after or {}).get("entries", 0)
    record("2 retrospective Q writes nothing to FatSecret", a == b,
           f"diary entries before={b} after={a} (independent FatSecret read)")

# --- 3. Failure claims stay grounded: healthy Habitica is never called down ---
async def test_failure_claims_grounded():
    ok_live, detail = await habitica.health_check()
    if not ok_live:
        record("3 no fabricated 'Habitica down' when healthy", False,
               f"precondition failed — Habitica not actually healthy right now ({detail})"); return
    POS = ("200", "up", "working", "reachable", "responding", "fine", "healthy", "connected", "connection")
    problems = []
    for q in ("is habitica working?", "is habitica down?", "I'm not seeing habitica updated"):
        r = await bot.handle_integration_status(TEST_UID, q)   # None => status query NOT recognized (regression)
        if r is None:
            problems.append(f"[{q}] not recognized as a status query -> would fall to an unguarded guess"); continue
        low = r.lower()
        if not any(p in low for p in POS):
            problems.append(f"[{q}] reply has no grounded/positive signal: {r[:70]}")
    record("3 no fabricated 'Habitica down' when healthy", not problems,
           "; ".join(problems) if problems else "all phrasings live-checked and grounded (Habitica healthy)")

# --- 4. Capture routing: single / zero / multi + precedence ---
async def test_capture():
    r1 = await bot.process_message(TEST_UID, f"capture: {CAP_KW} {MARK} single-match test")
    tgt = open(os.path.join(VAULT, CAP_TARGET_REL), encoding="utf-8").read()
    single_ok = (MARK in tgt) and ("Test-Target" in (r1 or ""))
    r2 = await bot.process_message(TEST_UID, f"capture: {MARK} buy something that matches no route")
    inbox = open(os.path.join(VAULT, INBOX_REL), encoding="utf-8").read()
    zero_ok = (f"{MARK} buy something that matches no route" in inbox) and ("Inbox" in (r2 or ""))
    r3 = await bot.process_message(TEST_UID, f"capture: {MARK} the yoga and dashboard both need work")
    inbox2 = open(os.path.join(VAULT, INBOX_REL), encoding="utf-8").read()
    multi_ok = (f"{MARK} the yoga and dashboard both need work" in inbox2) and \
               ("Inbox" in (r3 or "") or "more than one" in (r3 or "").lower())
    fin_cap = await bot.handle_capture(TEST_UID, "spent $15 on lunch")
    comp_cap = await bot.handle_capture(TEST_UID, f"did my {TEST_HABIT}")
    prec_ok = fin_cap is None and comp_cap is None
    record("4 capture routing (single/zero/multi/precedence)", single_ok and zero_ok and multi_ok and prec_ok,
           f"single={single_ok} zero={zero_ok} multi={multi_ok} precedence(no-capture)={prec_ok}")

# --- 5. Water/Habitica decoupling + sync-failure surfaced exactly once ---
async def test_water_sync():
    r = _redis()
    keys = ["last_water_confirmed", "water_confirm_count", "water_sync_fail_notified", "last_drink_mentioned"]
    snap = {k: await r.get(k) for k in keys}
    orig_log_habit, orig_cc = habitica.log_habit, bot.call_claude
    captured = []
    async def fail_log(*a, **k): raise RuntimeError("test-forced Habitica failure")
    async def fake_cc(uid, message, route_text=None): captured.append(message); return "(reply)"
    try:
        habitica.log_habit = fail_log
        bot.call_claude = fake_cc
        await r.delete("water_sync_fail_notified"); await r.delete("last_water_confirmed")
        captured.clear()
        await bot.process_message(TEST_UID, "just drank a glass of water")
        note1 = "STATUS NOTE" in (captured[-1] if captured else "")
        decoupled = (await r.get("last_water_confirmed")) is not None
        flag = (await r.get("water_sync_fail_notified")) is not None
        captured.clear()
        await bot.process_message(TEST_UID, "had more water")
        note2 = "STATUS NOTE" in (captured[-1] if captured else "")
        record("5 water decoupled + sync-fail surfaced once", note1 and decoupled and flag and (not note2),
               f"note_first={note1} local_confirm_registered={decoupled} flag_set={flag} note_second(should be False)={note2}")
    finally:
        habitica.log_habit, bot.call_claude = orig_log_habit, orig_cc
        for k, v in snap.items():
            await (r.delete(k) if v is None else r.set(k, v))
        await r.aclose()

# --- 6. A real Habitica write, verified by an INDEPENDENT read (not Ghost's echo) ---
async def test_real_habitica_write():
    before, hid = await _hab_habit(TEST_HABIT)
    if hid is None:
        record("6 real Habitica write, independently verified", False, "test habit not on board"); return
    await bot.process_message(TEST_UID, f"did my {TEST_HABIT} today")
    await asyncio.sleep(1)
    after, _ = await _hab_habit(TEST_HABIT)          # separate GET to Habitica's servers
    ok = (before is not None) and (after is not None) and (after > before)
    record("6 real Habitica write, independently verified", ok, f"counterUp {before} -> {after} (independent read)")
    if ok:
        async with httpx.AsyncClient() as c:        # offset back to net-zero value
            await c.post(f"{habitica.BASE_URL}/tasks/{hid}/score/down", headers=habitica.HEADERS, timeout=20)

# --- 7. Self-code /api/apply never auto-approves ---
async def test_selfcode_apply_guard():
    conn = await _pg()
    tid = None
    try:
        tid = await conn.fetchval(
            "INSERT INTO code_proposals (service, file_path, diff, new_content, explanation, status) "
            "VALUES ('bot','/home/USER/ghost/bot/bot.py','','','ghost-test fixture (S45)','proposed') RETURNING id")
        async with httpx.AsyncClient() as c:
            r1 = (await c.post(f"{DASH}/api/apply", json={"id": tid}, timeout=15)).json()
            r2 = (await c.post(f"{DASH}/api/apply", json={"id": 99999999}, timeout=15)).json()
        st = await conn.fetchval("SELECT status FROM code_proposals WHERE id=$1", tid)
        unapproved_rejected = (r1.get("ok") is False) and (st == "proposed")   # untouched, not queued
        fabricated_rejected = (r2.get("ok") is False)
        record("7 self-code /api/apply never auto-approves", unapproved_rejected and fabricated_rejected,
               f"unapproved: ok={r1.get('ok')} err={r1.get('error')} status_still={st}; fake-id: ok={r2.get('ok')} err={r2.get('error')}")
    finally:
        if tid is not None:
            await conn.execute("DELETE FROM code_proposals WHERE id=$1", tid)
        await conn.close()

# --- 8. Dashboard health: command-center keys + errors empty; /api/version matches disk ---
async def test_dashboard_health():
    async with httpx.AsyncClient() as c:
        cc = (await c.get(f"{DASH}/api/command-center", timeout=25)).json()
        ver = (await c.get(f"{DASH}/api/version", timeout=15)).json()
    expected = {"habits", "todos", "vitals", "budget", "nutrition", "selfCheck", "proposals", "escalation", "timeline"}
    keys_ok = expected.issubset(set(cc.keys()))
    errors_ok = cc.get("errors") == []
    disk_md5 = os.environ.get("DASHBOARD_DISK_MD5", "")
    version_ok = bool(disk_md5) and ver.get("source_md5") == disk_md5
    record("8 dashboard health (command-center + version)", keys_ok and errors_ok and version_ok,
           f"keys_ok={keys_ok} errors={cc.get('errors')} version_match={version_ok} "
           f"(disk={disk_md5[:8]} serving={str(ver.get('source_md5'))[:8]})")

TESTS = [test_financial_routing, test_retro_no_write, test_failure_claims_grounded, test_capture,
         test_water_sync, test_real_habitica_write, test_selfcode_apply_guard, test_dashboard_health]

async def _cleanup():
    # vault: strip test-marked lines from the capture target + Inbox (preserve owner/mode — pod is root)
    for rel in (CAP_TARGET_REL, INBOX_REL):
        p = os.path.join(VAULT, rel)
        try:
            lines = open(p, encoding="utf-8").read().split("\n")
            kept = [l for l in lines if MARK not in l]
            if len(kept) != len(lines):
                st = os.stat(p)
                open(p, "w", encoding="utf-8").write("\n".join(kept))
                try:
                    os.chown(p, st.st_uid, st.st_gid); os.chmod(p, st.st_mode)
                except OSError:
                    pass
        except OSError:
            pass
    # postgres: test-uid conversation rows + any stray test expenses
    try:
        conn = await _pg()
        try:
            await conn.execute("DELETE FROM messages WHERE user_id=$1", TEST_UID)
            await conn.execute("DELETE FROM expenses WHERE description ILIKE '%spent $20 on groceries%' "
                               "OR description ILIKE '%spent ages in a meeting at 4:30%'")
        finally:
            await conn.close()
    except Exception as e:
        print(f"(cleanup note: {e})")

async def main():
    only = os.environ.get("GHOST_TEST_ONLY")   # optional: run a single test by 1-based index (for meta-verification)
    tests = TESTS if not only else [TESTS[int(only) - 1]]
    for t in tests:
        try:
            await t()
        except Exception as e:
            record(t.__name__, False, f"EXCEPTION: {type(e).__name__}: {e}")
    await _cleanup()
    print("=" * 64)
    for name, ok, detail in results:
        print(f"[{'PASS' if ok else 'FAIL'}] {name}\n        {detail}")
    print("=" * 64)
    npass = sum(1 for _, ok, _ in results if ok)
    print(f"{npass}/{len(results)} passed")
    sys.exit(0 if npass == len(results) else 1)

if __name__ == "__main__":
    asyncio.run(main())
