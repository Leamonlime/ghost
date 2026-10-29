#!/usr/bin/env python3
"""Bills categorisation tests against the REAL dashboard handlers, in a throwaway schema.

Every row here is synthetic (zzt_bills_<pid>, dropped at the end) — Lea's real transactions are
never read. Needs Postgres creds in env:

  POSTGRES_HOST=$(kubectl get svc -n ghost ghost-postgres-postgresql -o jsonpath='{.spec.clusterIP}') \
  POSTGRES_PASSWORD=$(kubectl get secret -n ghost ghost-postgres-postgresql -o jsonpath='{.data.postgres-password}' | base64 -d) \
  python3 tests/bills_categories_isolated.py
"""
import asyncio
import io
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
os.environ.setdefault("BOT_CODE_PATH", os.path.join(HERE, "..", "bot"))
sys.path.insert(0, os.path.join(HERE, "..", "dashboard"))

import asyncpg  # noqa: E402
import httpx  # noqa: E402

import dashboard as dash  # noqa: E402

SCHEMA = f"zzt_bills_{os.getpid()}"
FAILS, PASSES = [], 0


def check(name, cond, extra=""):
    global PASSES
    if cond:
        PASSES += 1
    else:
        FAILS.append(f"{name} {extra}")
        print(f"FAIL: {name} {extra}")


async def _conn():
    return await asyncpg.connect(
        host=os.environ.get("POSTGRES_HOST", "localhost"), user="postgres",
        password=os.environ["POSTGRES_PASSWORD"], database="ghost_db",
        server_settings={"search_path": SCHEMA})


async def setup():
    c = await _conn()
    await c.execute(f"CREATE SCHEMA {SCHEMA}")
    await c.close()

    async def pg_conn():
        return await _conn()
    dash.pg_conn = pg_conn
    dash._SPENDING_SCHEMA_READY = False
    dash._BILLS_SCHEMA_READY = False


async def teardown():
    c = await asyncpg.connect(
        host=os.environ.get("POSTGRES_HOST", "localhost"), user="postgres",
        password=os.environ["POSTGRES_PASSWORD"], database="ghost_db")
    await c.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
    await c.close()


def client():
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=dash.app), base_url="http://t")


async def seed_txn(c, d, desc, amt, cat="uncategorised", manual=False):
    return await c.fetchval(
        "INSERT INTO transactions (txn_date, description, amount, category, manual) "
        "VALUES ($1,$2,$3,$4,$5) RETURNING id", d, desc, amt, cat, manual)


async def test_merge():
    """S90 merge still moves transactions + merchant rules + subscriptions and deletes the source."""
    from datetime import date
    async with client() as h:
        await h.get("/api/bills/categories")            # creates + seeds the schema
        c = await _conn()
        await seed_txn(c, date(2026, 9, 1), "ZZT CAFE ONE", -5, "cafe", True)
        await c.execute("INSERT INTO merchant_category_rules (merchant_key, category) VALUES ('zzt cafe', 'cafe')")
        await dash._ensure_bills_schema(c)
        await c.execute("INSERT INTO subscriptions (name, amount, category) VALUES ('zzt sub', 1, 'cafe')")
        await c.close()
        r = (await h.post("/api/bills/category/merge", json={"source": "cafe", "target": "dining_takeaway"})).json()
        check("merge ok", r.get("ok"), str(r))
        check("merge moved txn", r.get("transactions_moved") == 1, str(r))
        check("merge moved rule", r.get("rules_moved") == 1, str(r))
        c = await _conn()
        check("source deleted", not await c.fetchval("SELECT 1 FROM spending_categories WHERE key='cafe'"))
        check("txn now target", await c.fetchval("SELECT category FROM transactions WHERE description='ZZT CAFE ONE'") == "dining_takeaway")
        check("rule now target", await c.fetchval("SELECT category FROM merchant_category_rules WHERE merchant_key='zzt cafe'") == "dining_takeaway")
        check("subscription repointed", await c.fetchval("SELECT category FROM subscriptions WHERE name='zzt sub'") == "dining_takeaway")
        await c.close()


async def test_import_and_transfers():
    """Keyword matcher gone: import stores non-manual rows uncategorised, re-import keeps a manual
    pick, and transfer skipping in bill detection follows the RESOLVED category (merchant rule)."""
    from datetime import date
    dash._ollama_narrate = _no_narrate
    lines = ["Date,Description,Amount"]
    for mo in (5, 6, 7, 8):
        lines.append(f"01/{mo:02d}/2026,ZZTSAVER 1,-200.00")      # own-account transfer
        lines.append(f"03/{mo:02d}/2026,ZZTPOWER 9,-80.00")       # a real monthly bill
    lines.append("05/08/2026,ZZTGROCER 4,-30.00")
    csv = ("\n".join(lines) + "\n").encode()
    async with client() as h:
        r = (await h.post("/api/bills/import-csv", files={"file": ("zzt.csv", io.BytesIO(csv), "text/csv")})).json()
        check("import ok", r.get("ok"), str(r)[:300])
        c = await _conn()
        cats = {x[0] for x in await c.fetch("SELECT DISTINCT category FROM transactions WHERE description LIKE 'ZZT%' AND NOT manual")}
        check("non-manual rows stored uncategorised", cats == {"uncategorised"}, str(cats))
        gid = await c.fetchval("SELECT id FROM transactions WHERE description='ZZTGROCER 4'")
        await c.close()
        r = (await h.post("/api/bills/categorize", json={"id": gid, "category": "groceries"})).json()
        check("manual pick ok", r.get("ok"), str(r))
        r = (await h.post("/api/bills/import-csv", files={"file": ("zzt.csv", io.BytesIO(csv), "text/csv")})).json()
        check("re-import deduped", r.get("inserted") == 0, str(r.get("inserted")))
        c = await _conn()
        check("manual pick survives re-import",
              await c.fetchval("SELECT category FROM transactions WHERE id=$1", gid) == "groceries")
        await c.execute("INSERT INTO merchant_category_rules (merchant_key, category) VALUES ('zztsaver','savings_transfers')")
        await c.close()
        cands = (await h.get("/api/bills/candidates")).json().get("candidates", [])
        pats = {x["source_pattern"] for x in cands}
        check("real bill still detected", "zztpower" in pats, str(pats))
        check("rule-categorised transfer skipped", "zztsaver" not in pats, str(pats))
        # price-check skip: a fixed bill linked to the transfer pattern must not be flagged
        c = await _conn()
        await c.execute("INSERT INTO bills (name, amount, frequency, bill_type, source_pattern) "
                        "VALUES ('zzt saver bill', 1, 'monthly', 'fixed', 'zztsaver')")
        res = await dash._refresh_linked_bills(c)
        flag = await c.fetchval("SELECT price_flag FROM bills WHERE name='zzt saver bill'")
        await c.close()
        check("refresh skips rule-categorised transfer", flag is None and res.get("fixed_flagged") == 0, f"{flag} {res}")


async def _no_narrate(summary):
    return ""


async def test_resort():
    """Re-sort view: filter=cat:<key> lists one category; 'always' with resort_from moves the
    merchant's other manual picks out of that category too, and leaves other merchants alone."""
    from datetime import date
    async with client() as h:
        await h.post("/api/bills/category/add", json={"name": "Cats"})
        c = await _conn()
        a1 = await seed_txn(c, date(2026, 9, 2), "ZZTPETS 1", -10, "shopping_retail", True)
        a2 = await seed_txn(c, date(2026, 9, 9), "ZZTPETS 2", -12, "shopping_retail", True)
        b1 = await seed_txn(c, date(2026, 9, 3), "ZZTHARDWARE 1", -40, "shopping_retail", True)
        await c.close()
        r = (await h.get("/api/bills/transactions?filter=cat:shopping_retail")).json()
        check("re-sort filter lists the category", {t["id"] for t in r["transactions"]} == {a1, a2, b1}, str(r.get("count")))
        r = (await h.post("/api/bills/categorize", json={"id": a1, "category": "cats", "repeat_merchant": True,
                                                         "resort_from": "shopping_retail"})).json()
        check("re-sort categorize ok", r.get("ok") and r.get("merchant") == "zztpets", str(r))
        c = await _conn()
        check("sibling manual pick moved", await c.fetchval("SELECT category FROM transactions WHERE id=$1", a2) == "cats")
        check("other merchant untouched", await c.fetchval("SELECT category FROM transactions WHERE id=$1", b1) == "shopping_retail")
        await c.close()


async def test_caps():
    from datetime import date, datetime
    async with client() as h:
        cats = {d["key"]: d for d in (await h.get("/api/bills/categories")).json()["details"]}
        check("cap off by default: rent/utilities/debt/money movement",
              not any(cats[k]["cap_enabled"] for k in ("rent_mortgage", "utilities", "debt_loan_repayments", "savings_transfers")))
        check("cap on by default: groceries", cats["groceries"]["cap_enabled"] and cats["groceries"]["monthly_cap"] is None)
        check("fixed defaults", cats["rent_mortgage"]["fixed"] and cats["subscriptions"]["fixed"] and not cats["groceries"]["fixed"])
        r = (await h.post("/api/bills/category/cap", json={"name": "groc", "monthly_cap": 100})).json()
        check("cap set by loose name", r.get("ok") and r.get("category") == "groceries" and r.get("monthly_cap") == 100, str(r))
        r = await h.post("/api/bills/category/cap", json={"name": "s", "monthly_cap": 5})
        check("ambiguous name -> 409", r.status_code == 409 and r.json().get("candidates"), r.text[:200])
        r = await h.post("/api/bills/category/cap", json={"name": "zzt nothing", "monthly_cap": 5})
        check("unknown name -> 404", r.status_code == 404)
        r = (await h.post("/api/bills/category/cap", json={"category": "rent_mortgage", "monthly_cap": 50})).json()
        check("amount on an off-by-default category switches it on", r.get("cap_enabled") is True, str(r))
        await h.post("/api/bills/category/cap", json={"category": "rent_mortgage", "monthly_cap": None, "enabled": False})
        r = (await h.post("/api/bills/cap-settings", json={"warn_pct": 75})).json()
        check("warn pct saved", r.get("ok") and r.get("warn_pct") == 75)
        check("warn pct validated", (await h.post("/api/bills/cap-settings", json={"warn_pct": 150})).status_code == 400)
        # month-to-date spend vs cap, via a merchant rule (resolved category, not the raw column)
        today = datetime.now(dash.LOCAL_TZ).date()
        c = await _conn()
        await c.execute("INSERT INTO merchant_category_rules (merchant_key, category) VALUES ('zztfood','groceries')")
        await seed_txn(c, today.replace(day=1), "ZZTFOOD 1", -40)
        await seed_txn(c, today.replace(day=1), "ZZTFOOD 2", -40)
        await c.close()
        st = (await h.get("/api/bills/caps-status")).json()
        g = next(i for i in st["items"] if i["key"] == "groceries")
        check("caps-status warn at 80% (threshold 75)", g["status"] == "warn" and g["pct"] == 80.0, str(g))
        check("unset caps listed as unset", any(i["status"] == "unset" for i in st["items"]))
        check("off categories not listed", not any(i["key"] == "rent_mortgage" for i in st["items"]))
        sp = (await h.get("/api/bills/spending")).json()
        check("spending summary carries caps", sp.get("caps", {}).get("items"), str(sp.get("caps"))[:200])
        # merge carries the cap to a target without one
        await h.post("/api/bills/category/add", json={"name": "Zzt Food Two"})
        await h.post("/api/bills/category/merge", json={"source": "groceries", "target": "zzt_food_two"})
        cats = {d["key"]: d for d in (await h.get("/api/bills/categories")).json()["details"]}
        check("merge carries cap", cats["zzt_food_two"]["monthly_cap"] == 100, str(cats.get("zzt_food_two")))
    check("pure status: over", dash.compute_cap_status({"x": 120}, [{"key": "x", "label": "X", "cap_enabled": True, "monthly_cap": 100}], 80)[0]["status"] == "over")
    check("pure status: ok", dash.compute_cap_status({"x": 10}, [{"key": "x", "label": "X", "cap_enabled": True, "monthly_cap": 100}], 80)[0]["status"] == "ok")


def test_cap_messages():
    import importlib.util
    spec = importlib.util.spec_from_file_location("sched", os.path.join(HERE, "..", "scheduler", "scheduler.py"))
    sched = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sched)
    one = sched.budget_cap_message([{"label": "Groceries", "level": "warn"}], 80, "2026-10-20")
    check("cap message: warn wording", "Groceries has passed 80%" in one, one)
    two = sched.budget_cap_message([{"label": "Groceries", "level": "warn"}, {"label": "Lotto", "level": "over"}], 80)
    check("cap message: combined", "Lotto is at or over" in two and two.count("\n") == 2, two)
    check("cap message: no dollar figures", "$" not in one + two)
    check("cap message: nothing to say", sched.budget_cap_message([], 80) == "")


async def test_export():
    from datetime import date, datetime
    rnd = dash.review_round
    check("round $10", rnd(994) == 990 and rnd(995) == 1000 and rnd(1000) == 1000)
    check("round $50 over $1,000", rnd(1020) == 1000 and rnd(1030) == 1050 and rnd(2474) == 2450)
    # pure builder: planted identifiers can't get in because the builder never sees descriptions
    months = ["2026-04", "2026-05", "2026-06"]
    cells = {("2026-04", "groceries"): (412.0, 9), ("2026-05", "groceries"): (388.0, 7),
             ("2026-06", "groceries"): (401.0, 8), ("2026-05", "travel"): (1234.0, 1),
             ("2026-06", "travel"): (80.0, 1), ("2026-06", "cats"): (55.0, 1)}
    cats = {"groceries": {"label": "Groceries", "fixed": False}, "travel": {"label": "Travel", "fixed": False},
            "cats": {"label": "Cats", "fixed": False}, "rent_mortgage": {"label": "Rent", "fixed": True}}
    md = dash.build_review_export(months, cells, cats, {"2026-04": 3000, "2026-05": 3000, "2026-06": 3000},
                                  [{"month": date(2026, 11, 1), "label": "Travel", "amount": 1860, "tag": "wedding"}],
                                  "October 2026")
    cat_table = md.split("## Income")[0]
    check("single-txn categories folded into Other", "| Travel |" not in cat_table and "| Cats |" not in cat_table, md)
    check("Other shows <2 for a lone transaction, sum when 2+", "| Other | Variable | — | <2 | $140 | $50 |" in md, md)
    check("6-mo avg column", "| Groceries | Variable | $410 | $390 | $400 | $400 |" in md, md)
    check("savings rate", "| Savings rate | " in md and "| Income | $3,000 |" in md, md)
    check("upcoming tagged", "| Nov 26 | Travel | $1,850 | Wedding |" in md, md)
    check("export passes scan", dash.review_scan(md) == [], str(dash.review_scan(md)))
    check("export under ~60 lines", md.count("\n") <= 60, str(md.count("\n")))
    hits = dash.review_scan("Account 062-000 ref 123456789 mail a.b@c.com call 0412 345 678")
    whats = {h["what"] for h in hits}
    check("scan catches BSB, 6+ digits, email, phone",
          {"BSB-like (NNN-NNN)", "6+ digit run", "email address", "phone number"} <= whats, str(hits))
    # endpoint end to end, with a hostile description, note and category name
    today = datetime.now(dash.LOCAL_TZ).date()
    m1 = dash._add_months(today.replace(day=1), -1)
    async with client() as h:
        c = await _conn()
        for i in range(3):
            await seed_txn(c, m1, f"ZZTSECRET JOHN SMITH 0412345678 BSB 062-000 #{i}", -25, "health_medical", True)
        await seed_txn(c, m1, "ZZTEMPLOYER PTY PAYROLL 123456789", 2500, "income", True)
        await c.close()
        await h.post("/api/bills/planned-costs", json={"month": dash._add_months(today.replace(day=1), 2).strftime("%Y-%m"),
                                                       "category": "travel", "amount": 640, "tag": "move",
                                                       "note": "deposit for John at 42 Secret St"})
        r = (await h.get("/api/bills/export-review")).json()
        md = r.get("markdown") or ""
        check("export endpoint ok", r.get("ok"), str(r)[:300])
        check("no description/merchant/employer/name leaks",
              not any(x in md.upper() for x in ("ZZTSECRET", "JOHN", "SMITH", "ZZTEMPLOYER", "PAYROLL", "SECRET ST", "0412", "062-000")), md)
        check("planned cost exported without its note", "| Move |" in md and "deposit" not in md, md)
        check("income counted", "| Income |" in md, md)
        # a category label carrying an ID trips the scan and blocks the export
        await h.post("/api/bills/category/rename", json={"category": "health_medical", "new_label": "Dr 0412 345 678"})
        r = (await h.get("/api/bills/export-review")).json()
        check("scan blocks and reports", r.get("blocked") and not r.get("markdown") and r.get("matches"), str(r)[:300])


TESTS = [test_merge, test_import_and_transfers, test_resort, test_caps, test_export]


async def main():
    if not os.environ.get("POSTGRES_PASSWORD"):
        print("SKIP: POSTGRES_PASSWORD not set")
        return 0
    test_cap_messages()
    await setup()
    try:
        for t in TESTS:
            await t()
    finally:
        await teardown()
    print(f"{PASSES} passed, {len(FAILS)} failed")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
