#!/usr/bin/env python3
"""End-to-end CSV import against the REAL dashboard handlers, in a throwaway schema (zzt_import_<pid>,
dropped at the end). Every row is SYNTHETIC. Lea's real transactions are never read.

While it runs it also enforces the data boundaries:
  - no outbound connection except to Postgres (socket.connect is wrapped),
  - no temp file is created for the upload (tempfile / starlette spooling is wrapped),
  - no merchant name or amount appears in any log record (root logger is captured).

  POSTGRES_HOST=$(kubectl get svc -n ghost ghost-postgres-postgresql -o jsonpath='{.spec.clusterIP}') \
  POSTGRES_PASSWORD=$(kubectl get secret -n ghost ghost-postgres-postgresql -o jsonpath='{.data.postgres-password}' | base64 -d) \
  python3 tests/finance_import_isolated.py
"""
import asyncio
import io
import logging
import os
import socket
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
os.environ.setdefault("BOT_CODE_PATH", os.path.join(HERE, "..", "bot"))
sys.path.insert(0, os.path.join(HERE, "..", "dashboard"))

import asyncpg  # noqa: E402
import httpx  # noqa: E402

import dashboard as dash  # noqa: E402
import finance_import as fi  # noqa: E402

SCHEMA = f"zzt_import_{os.getpid()}"
PG_HOST = os.environ.get("POSTGRES_HOST", "localhost")
FAILS, PASSES = [], 0

# ---- boundary instrumentation -------------------------------------------------------------
LOG_LINES = []


class _Capture(logging.Handler):
    def emit(self, record):
        try:
            LOG_LINES.append(record.getMessage())
        except Exception:
            LOG_LINES.append(str(record.msg))


BAD_CONNECTS, TEMPFILES = [], []
_real_connect = socket.socket.connect
_real_connect_ex = socket.socket.connect_ex
_PG_IPS = set()


def _guard_addr(addr):
    host = addr[0] if isinstance(addr, tuple) else addr
    port = addr[1] if isinstance(addr, tuple) and len(addr) > 1 else None
    if port != 5432 or host not in _PG_IPS:
        BAD_CONNECTS.append(addr)
        raise ConnectionRefusedError("outbound connection blocked by finance_import_isolated")


def _connect(self, addr):
    _guard_addr(addr)
    return _real_connect(self, addr)


def _connect_ex(self, addr):
    _guard_addr(addr)
    return _real_connect_ex(self, addr)


def _no_temp(*a, **k):
    TEMPFILES.append(1)
    raise RuntimeError("temp file creation blocked during import")


def install_guards():
    _PG_IPS.update({PG_HOST, "127.0.0.1", "::1"})
    try:
        _PG_IPS.update(i[4][0] for i in socket.getaddrinfo(PG_HOST, 5432))
    except OSError:
        pass
    socket.socket.connect = _connect
    socket.socket.connect_ex = _connect_ex
    for name in ("SpooledTemporaryFile", "NamedTemporaryFile", "TemporaryFile", "mkstemp"):
        setattr(tempfile, name, _no_temp)
    try:
        import starlette.formparsers as sf
        sf.SpooledTemporaryFile = _no_temp
    except Exception:
        pass
    h = _Capture(level=logging.DEBUG)
    logging.getLogger().addHandler(h)
    logging.getLogger().setLevel(logging.DEBUG)
    dash.logger.setLevel(logging.DEBUG)


def check(name, cond, extra=""):
    global PASSES
    if cond:
        PASSES += 1
    else:
        FAILS.append(f"{name} {extra}")
        print(f"FAIL: {name} {extra}")


async def _conn():
    return await asyncpg.connect(host=PG_HOST, user="postgres", password=os.environ["POSTGRES_PASSWORD"],
                                 database="ghost_db", server_settings={"search_path": SCHEMA})


def client():
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=dash.app), base_url="http://t")


def mp(csv_text, name="zzt-statement.csv"):
    return {"files": {"file": (name, io.BytesIO(csv_text.encode()), "text/csv")}}


# Synthetic ANZ-shaped statement. Two identical same-day purchases are deliberate.
ANZ = (
    "Date,Value,Details\n"
    "01/10/2026,-12.50,EFTPOS ZZTGROCER 1234 RICHMOND AU\n"
    "01/10/2026,-12.50,EFTPOS ZZTGROCER 1234 RICHMOND AU\n"
    '02/10/2026,"-1,234.00",PAYMENT TO ZZT LANDLORD 123456789\n'
    "03/10/2026,2500.00,ZZT EMPLOYER PAY 987654\n"
    "04/10/2026,-300.00,ANZ M-BANKING FUNDS TFER TRANSFER 112233 TO 012345678\n"
    "05/10/2026,-8.40,VISA DEBIT PURCHASE CARD 4821 ZZT CAFE CO CARLTON AU\n"
)
SECRET_STRINGS = ["ZZTGROCER", "LANDLORD", "EMPLOYER", "ZZT CAFE", "ZZTLEGACY", "ZZTLEAK",
                  "12.50", "1234.00", "1,234", "2500", "300.00", "8.40", "012345678", "112233"]


async def snapshot(c):
    return [tuple(r) for r in await c.fetch(
        "SELECT txn_date, description, amount, category, rule_id, dedupe_hash, manual FROM transactions ORDER BY id")]


async def run():
    async with client() as h:
        await h.get("/api/bills/categories")                    # creates + seeds the schema
        c = await _conn()
        # A pre-Build-1 row (no hash) is backfilled and later dedupes against the same CSV line.
        await c.execute("INSERT INTO transactions (txn_date, description, amount) VALUES "
                        "('2026-10-05','VISA DEBIT PURCHASE CARD 4821 ZZT CAFE CO CARLTON AU',-8.40)")
        await c.close()
        dash._SPENDING_SCHEMA_READY = False
        await h.get("/api/bills/categories")
        c = await _conn()
        check("legacy row backfilled", await c.fetchval(
            "SELECT dedupe_hash IS NOT NULL AND norm_key='ZZT CAFE CO' FROM transactions"))
        check("old unique constraint dropped", not await c.fetchval(
            "SELECT 1 FROM pg_constraint WHERE conname='transactions_txn_date_description_amount_key' "
            "AND connamespace=(SELECT oid FROM pg_namespace WHERE nspname=$1)", SCHEMA))
        await c.close()

        # ---- first import
        r = (await h.post("/api/import/csv", **mp(ANZ))).json()
        check("import ok", r.get("ok"), str(r))
        check("counts only in response", set(r) <= {"ok", "imported", "duplicates", "uncategorised", "skipped",
                                                   "rows_in_file", "format"}, str(sorted(r)))
        check("imported 5 (cafe already there)", r.get("imported") == 5, str(r))
        check("1 duplicate (backfilled legacy row)", r.get("duplicates") == 1, str(r))
        check("uncategorised 4 (transfer matched)", r.get("uncategorised") == 4, str(r))
        c = await _conn()
        check("identical same-day purchases both stored", await c.fetchval(
            "SELECT count(*) FROM transactions WHERE norm_key='ZZTGROCER'") == 2)
        tr = await c.fetchrow("SELECT category, rule_id, description, norm_key FROM transactions WHERE norm_key=$1",
                              fi.TRANSFER_KEY)
        check("transfer -> existing transfer category", tr and tr["category"] == "savings_transfers", str(tr and tr["category"]))
        check("transfer has rule id", tr and tr["rule_id"] is not None)
        check("account number not stored", tr and "012345678" not in tr["description"] and "012345678" not in tr["norm_key"])
        before = await snapshot(c)
        await c.close()

        # ---- idempotency
        r = (await h.post("/api/import/csv", **mp(ANZ))).json()
        check("re-import: 0 imported", r.get("imported") == 0, str(r))
        check("re-import: all duplicates", r.get("duplicates") == 6, str(r))
        c = await _conn()
        check("re-import changes nothing", await snapshot(c) == before)
        await c.close()

        # ---- transfers excluded from spending totals
        sp = (await h.get("/api/bills/spending")).json()
        spent = sp["by_month"]["2026-10"]["spent"]
        check("transfer excluded from spent", abs(spent - (12.5 * 2 + 1234 + 8.4)) < 0.01, str(spent))

        # ---- uncategorised queue + assign without apply_past
        q = (await h.get("/api/import/uncategorised")).json()
        groups = {g["key"]: g for g in q.get("groups", [])}
        check("queue grouped by key", groups.get("ZZTGROCER", {}).get("count") == 2, str(list(groups)))
        check("transfer not queued", fi.TRANSFER_KEY not in groups)
        first = groups["ZZTGROCER"]["transactions"][0]["id"]
        r = (await h.post("/api/import/assign", json={"key": "ZZTGROCER", "category": "groceries",
                                                      "apply_past": False, "txn_ids": [first]})).json()
        check("assign ok", r.get("ok") and r.get("applied") == 1, str(r))
        q = (await h.get("/api/import/uncategorised")).json()
        check("other grocer row still queued", {g["key"]: g["count"] for g in q["groups"]}.get("ZZTGROCER") == 1)

        # future import uses the new rule and stores its id
        r = (await h.post("/api/import/csv", **mp("Date,Value,Details\n09/10/2026,-20.00,EFTPOS ZZTGROCER 77 KEW AU\n"))).json()
        check("rule applied on import", r.get("imported") == 1 and r.get("uncategorised") == 0, str(r))
        c = await _conn()
        row = await c.fetchrow("SELECT category, rule_id FROM transactions WHERE txn_date='2026-10-09'")
        rid = await c.fetchval("SELECT id FROM category_rules WHERE pattern='ZZTGROCER'")
        check("matched rule id stored", row["category"] == "groceries" and row["rule_id"] == rid, str(dict(row)))
        await c.close()

        # ---- manual pick never overwritten; apply_past sweeps the rest
        c = await _conn()
        landlord = await c.fetchval("SELECT id FROM transactions WHERE norm_key='ZZT LANDLORD'")
        await c.close()
        r = (await h.post("/api/bills/categorize", json={"id": landlord, "category": "rent_mortgage"})).json()
        check("manual pick ok", r.get("ok"), str(r))
        r = (await h.post("/api/import/assign", json={"pattern": "zzt", "match_type": "contains",
                                                      "category": "shopping_retail", "apply_past": True})).json()
        check("contains rule applied to past", r.get("ok") and r.get("applied", 0) >= 2, str(r))
        c = await _conn()
        check("manual pick survives apply_past", await c.fetchval(
            "SELECT category FROM transactions WHERE id=$1", landlord) == "rent_mortgage")
        check("grocer rows claimed by exact rule untouched", await c.fetchval(
            "SELECT count(*) FROM transactions WHERE norm_key='ZZTGROCER' AND category='groceries'") == 2)
        await c.close()
        rules = (await h.get("/api/import/rules")).json()["rules"]
        check("rules listed exact first", [x["match_type"] for x in rules][:1] == ["exact"], str(rules))
        bad = (await h.post("/api/import/assign", json={"pattern": "([", "match_type": "regex", "category": "shopping_retail"})).json()
        check("invalid regex refused", not bad.get("ok"))

        # ---- legacy S85 merchant rules still count as categorised
        c = await _conn()
        await c.execute("INSERT INTO merchant_category_rules (merchant_key, category) VALUES ('zztlegacy','groceries')")
        await c.close()
        r = (await h.post("/api/import/csv", **mp("Date,Value,Details\n10/10/2026,-3.00,ZZTLEGACY 5\n"))).json()
        check("legacy merchant rule not queued", r.get("uncategorised") == 0, str(r))

        # ---- upload handling
        r = await h.post("/api/import/csv", content=b"Date,Value,Details\n11/10/2026,-1.00,ZZT RAW\n",
                         headers={"Content-Type": "text/csv"})
        check("raw text/csv body accepted", r.status_code == 200 and r.json().get("imported") == 1, r.text[:200])
        r = await h.post("/api/import/csv", files={"file": ("x.xlsx", io.BytesIO(b"PK\x03\x04"), "application/octet-stream")})
        check("non-csv refused", r.status_code == 422, str(r.status_code))
        r = await h.post("/api/import/csv", content=b"a" * (fi.MAX_UPLOAD_BYTES + 200 * 1024),
                         headers={"Content-Type": "text/csv"})
        check("oversize refused 413", r.status_code == 413, str(r.status_code))
        r = await h.post("/api/import/csv", content=b"foo,bar\nZZTLEAK,1\n", headers={"Content-Type": "text/csv"})
        check("unmappable refused 422", r.status_code == 422 and "ZZTLEAK" not in r.text, r.text[:200])

        # ---- Bills page upload goes through the same pipeline, keeps its response keys
        r = (await h.post("/api/bills/import-csv", **mp(ANZ))).json()
        check("bills import ok", r.get("ok"), str(r)[:200])
        check("bills keys kept", r.get("inserted") == 0 and r.get("updated_deduped") == 6 and "bill_refresh" in r, str({k: r.get(k) for k in ("inserted", "updated_deduped")}))
        check("bills narration empty", r.get("narration") == "")

        # ---- an internal failure logs the exception type only
        real = fi.import_csv

        async def boom(*a, **k):
            raise ValueError("ZZTLEAK -12.50")
        fi.import_csv = boom
        r = await h.post("/api/import/csv", **mp(ANZ))
        fi.import_csv = real
        check("failure -> 500 without detail", r.status_code == 500 and "ZZTLEAK" not in r.text)


async def main():
    if "POSTGRES_PASSWORD" not in os.environ:
        print("finance_import_isolated: SKIPPED (POSTGRES_PASSWORD not set)")
        return 0
    c = await asyncpg.connect(host=PG_HOST, user="postgres", password=os.environ["POSTGRES_PASSWORD"], database="ghost_db")
    await c.execute(f"CREATE SCHEMA {SCHEMA}")
    await c.close()

    async def pg_conn():
        return await _conn()
    dash.pg_conn = pg_conn
    dash._SPENDING_SCHEMA_READY = False
    dash._BILLS_SCHEMA_READY = False
    install_guards()
    try:
        await run()
    finally:
        socket.socket.connect = _real_connect
        socket.socket.connect_ex = _real_connect_ex
        c = await asyncpg.connect(host=PG_HOST, user="postgres", password=os.environ["POSTGRES_PASSWORD"], database="ghost_db")
        await c.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        await c.close()
    check("no outbound connection attempted", not BAD_CONNECTS, str(BAD_CONNECTS))
    check("no temp file created for uploads", not TEMPFILES, str(len(TEMPFILES)))
    leaks = [ln for ln in LOG_LINES if any(s in ln for s in SECRET_STRINGS)]
    check("no transaction data in logs", not leaks, f"{len(leaks)} leaking log lines")
    print(f"finance_import_isolated: {PASSES} passed, {len(FAILS)} failed")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
