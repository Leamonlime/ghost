#!/usr/bin/env python3
"""Decision store tests (bot/decisions.py). Pure-parsing checks always run; the database half runs
in a throwaway schema (zzt_dec_<pid>, dropped at the end) when POSTGRES_PASSWORD is set:

  POSTGRES_HOST=$(kubectl get svc -n ghost ghost-postgres-postgresql -o jsonpath='{.spec.clusterIP}') \
  POSTGRES_PASSWORD=$(kubectl get secret -n ghost ghost-postgres-postgresql -o jsonpath='{.data.postgres-password}' | base64 -d) \
  python3 tests/decisions_isolated.py
Never touches the real decisions table.
"""
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "bot"))
import decisions as D  # noqa: E402

FAILS, PASSES = [], 0


def check(name, cond, extra=""):
    global PASSES
    if cond:
        PASSES += 1
    else:
        FAILS.append(f"{name} {extra}")
        print(f"FAIL: {name} {extra}")


def parsing():
    blk = """some prose
```DECISIONS
[{"question": "Retire the sync?", "options": [
   {"key": "Retire", "label": "Retire it", "consequence": "disables the loop"},
   {"label": "Re-point", "consequence": "resumes writes"},
   {"key": "other", "label": "Reserved key"}],
  "recommended_key": "retire", "merge_state": "on-branch", "deploy_steps": "ghost-deploy scheduler"}]
```
trailing"""
    out = D.parse_decisions_block(None, blk)
    check("block parsed", len(out) == 1)
    d = out[0]
    keys = [o["key"] for o in d["options"]]
    check("keys slugged, unique, 'other' reserved", keys == ["retire", "b", "c"], str(keys))
    check("recommended resolved", d["recommended_key"] == "retire")
    check("merge_state normalised", d["merge_state"] == "on_branch")
    check("deploy_steps kept", d["deploy_steps"] == "ghost-deploy scheduler")
    check("object wrapper accepted",
          len(D.parse_decisions_block('```DECISIONS\n{"decisions":[{"question":"a?"},{"question":"b?"}]}\n```')) == 2)
    check("single object accepted", len(D.parse_decisions_block('```decisions\n{"question":"a?"}\n```')) == 1)
    check("bad JSON -> []", D.parse_decisions_block("```DECISIONS\n{nope\n```") == [])
    check("no block -> []", D.parse_decisions_block("just a question?") == [])
    check("question-less entries dropped", D.parse_decisions_block('```DECISIONS\n[{"options":[]}]\n```') == [])
    check("recommended by label", D.parse_decisions_block(
        '```DECISIONS\n[{"question":"q","options":[{"key":"a","label":"Yes"}],"recommended_key":"yes"}]\n```'
    )[0]["recommended_key"] == "a")
    check("unknown recommended -> None", D.parse_decisions_block(
        '```DECISIONS\n[{"question":"q","options":[{"key":"a","label":"Yes"}],"recommended_key":"zzz"}]\n```'
    )[0]["recommended_key"] is None)
    long_q = "x" * 9000
    check("raw fallback keeps FULL text", D.raw_decision(long_q)["question"] == long_q)
    check("hash ignores case/whitespace", D.question_hash("A  b?") == D.question_hash(" a b? "))
    check("callback roundtrip", D.parse_callback(D.callback_data(12, "retire")) == (12, "retire"))
    check("foreign callback rejected", D.parse_callback("other:1:a") is None and D.parse_callback("dec:x:a") is None)
    check("callback_data fits Telegram's 64 bytes", len(D.callback_data(999999999, "a" * 16)) <= 64)


async def database():
    import asyncpg
    schema = f"zzt_dec_{os.getpid()}"
    conn = await asyncpg.connect(host=os.environ.get("POSTGRES_HOST", "localhost"), port=5432, user="postgres",
                                 password=os.environ["POSTGRES_PASSWORD"], database="ghost_db", timeout=15)
    try:
        await conn.execute(f"CREATE SCHEMA {schema}")
        await conn.execute(f"SET search_path TO {schema}")
        D.reset_schema_cache()
        opts = [{"key": "a", "label": "Do A", "consequence": "A happens"},
                {"key": "b", "label": "Do B", "consequence": "B happens"}]
        i1, c1 = await D.insert_decision(conn, "slug", "Which one?", opts, "a", branch="br", merge_state="on_branch")
        i2, c2 = await D.insert_decision(conn, "slug", "  which   ONE? ", opts, "a")
        check("insert idempotent (same id, created once)", i1 == i2 and c1 and not c2)
        i3, c3 = await D.insert_decision(conn, "other-slug", "Which one?", opts)
        check("same question, different slug = new row", i3 != i1 and c3)
        row = await D.get_decision(conn, i1)
        check("row defaults", row["status"] == "pending" and row["reminder_count"] == 0 and row["kind"] == "decision")
        check("options round-trip as list", row["options"] == opts)

        out, r = await D.answer(conn, i1, "zzz", via="t")
        check("unknown option invalid", out == "invalid")
        out, r = await D.answer(conn, i1, "other", "   ", via="t")
        check("empty 'other' invalid", out == "invalid")
        out, r = await D.answer(conn, 99999999, "a", via="t")
        check("missing id", out == "missing")
        out, r = await D.answer(conn, i1, "b", via="telegram")
        check("first answer wins", out == "answered" and r["answer_key"] == "b" and r["status"] == "answered"
              and r["answer_text"] == "Do B" and r["answered_via"] == "telegram" and r["answered_at"])
        out, r = await D.answer(conn, i1, "b", via="telegram")
        check("double tap idempotent", out == "already" and r["answer_key"] == "b")
        out, r = await D.answer(conn, i1, "a", via="app")
        check("different late answer doesn't overwrite", out == "already" and r["answer_key"] == "b"
              and r["answered_via"] == "telegram")
        # concurrent double tap: exactly one 'answered'
        j, _ = await D.insert_decision(conn, "slug", "Race?", opts)
        pool = [await asyncpg.connect(host=os.environ.get("POSTGRES_HOST", "localhost"), port=5432, user="postgres",
                                      password=os.environ["POSTGRES_PASSWORD"], database="ghost_db") for _ in range(4)]
        for c in pool:
            await c.execute(f"SET search_path TO {schema}")
        res = await asyncio.gather(*[D.answer(c, j, "a", via="t") for c in pool])
        for c in pool:
            await c.close()
        check("concurrent taps -> exactly one 'answered'", [o for o, _ in res].count("answered") == 1, str([o for o, _ in res]))

        k, _ = await D.insert_decision(conn, "slug", "Free text only?", [], None)
        out, r = await D.answer(conn, k, "other", "Do it my way", via="telegram")
        check("other + text answers", out == "answered" and r["answer_key"] == "other" and r["answer_text"] == "Do it my way")
        m, _ = await D.insert_decision(conn, "slug", "Another?", opts)
        out, r = await D.answer(conn, m, "other", "typed", via="app")
        check("other works on a decision with options", out == "answered")

        # reminders: only pending rows, count bumps, stale flagged
        p1, _ = await D.insert_decision(conn, "slug", "Old pending?", opts)
        await conn.execute("UPDATE decisions SET created_at = now() - interval '9 days' WHERE id=$1", p1)
        before = await D.get_decision(conn, p1)
        check("stale after 7 days", D.is_stale(before) and before["reminder_count"] == 0)
        block = await D.reminder_block(conn)
        after = await D.get_decision(conn, p1)
        check("reminder lists stale pending", block and f"#{p1}" in block and "STALE" in block)
        check("reminder_count bumped", after["reminder_count"] == 1)
        check("answered rows not reminded", f"#{i1} " not in block and (await D.get_decision(conn, i1))["reminder_count"] == 0)
        check("status untouched by reminders (never auto-expire)", after["status"] == "pending")

        card = D.render_card(before)
        check("card has question + recommendation", "Old pending?" in card and "Recommended" not in card)
        full = await D.get_decision(conn, i3)
        await conn.execute("UPDATE decisions SET recommended_key='a' WHERE id=$1", i3)
        full = await D.get_decision(conn, i3)
        check("card shows recommendation", "Recommended: Do A" in D.render_card(full))
        rows = D.button_rows(full)
        check("one button per option + Other", len(rows) == 3 and rows[-1][0]["text"] == "Other…")
        pub = D.to_public(full)
        json.dumps(pub)
        check("public shape serialises", pub["stale"] is False and pub["age_days"] == 0)
        await D.mark(conn, p1, "applied", "done")
        check("mark sets status/result", (await D.get_decision(conn, p1))["status"] == "applied")
    finally:
        await conn.execute("SET search_path TO public")
        await conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        await conn.close()


if __name__ == "__main__":
    parsing()
    if os.environ.get("POSTGRES_PASSWORD"):
        asyncio.run(database())
    else:
        print("(POSTGRES_PASSWORD not set — database half skipped)")
    print(f"{PASSES} passed, {len(FAILS)} failed")
    sys.exit(1 if FAILS else 0)
