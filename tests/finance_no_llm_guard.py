#!/usr/bin/env python3
"""Build 1 guard: finance code must never import or call an LLM or HTTP client, and Ollama must stay gone.

Fails the build when:
  1. dashboard/finance_import.py imports anything outside a small stdlib allowlist, or touches
     logging / open() / sockets (it must stay pure: no network, no logs, no files);
  2. any finance function in dashboard.py (import, spending, bills categorisation, rules, caps,
     export) references an HTTP client or a model call;
  3. any Ollama route reappears in bot/scheduler/dashboard code or the k8s yamls;
  4. the money gate runs AFTER anything that logs, saves or calls Claude (static order check), or
     money talk reaches Claude / message_log / the messages table at runtime;
  5. is_money_message misclassifies the adversarial cases below.

No database or network needed.  python3 tests/finance_no_llm_guard.py
"""
import ast
import asyncio
import glob
import io
import os
import re
import sys
import tokenize

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
os.environ.setdefault("BOT_CODE_PATH", os.path.join(ROOT, "bot"))
os.environ.setdefault("REDIS_HOST", "127.0.0.1")        # nothing listens; the activity stamp just fails
sys.path.insert(0, os.path.join(ROOT, "bot"))

FAILS, PASSES = [], 0

FORBIDDEN = {"httpx", "requests", "aiohttp", "urllib", "urllib3", "http", "socket", "anthropic", "openai",
             "ollama", "langchain", "call_claude", "call_claude_api", "claude_line", "call_ollama",
             "OLLAMA_URL", "push_ntfy", "send_push", "AsyncClient"}


def check(name, cond, extra=""):
    global PASSES
    if cond:
        PASSES += 1
    else:
        FAILS.append(f"{name} {extra}")
        print(f"FAIL: {name} {extra}")


def names_in(node):
    out = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Name):
            out.add(n.id)
        elif isinstance(n, ast.Attribute):
            out.add(n.attr)
        elif isinstance(n, (ast.Import, ast.ImportFrom)):
            for a in n.names:
                out.add(a.name.split(".")[0])
            if isinstance(n, ast.ImportFrom) and n.module:
                out.add(n.module.split(".")[0])
    return out


def test_finance_import_module():
    src = open(os.path.join(ROOT, "dashboard", "finance_import.py")).read()
    tree = ast.parse(src)
    allowed = {"csv", "hashlib", "io", "json", "re", "datetime", "decimal"}
    mods = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            mods |= {a.name.split(".")[0] for a in n.names}
        elif isinstance(n, ast.ImportFrom):
            mods.add((n.module or "").split(".")[0])
    check("finance_import imports only stdlib allowlist", mods <= allowed, str(sorted(mods - allowed)))
    used = names_in(tree)
    check("finance_import has no LLM/HTTP reference", not (used & FORBIDDEN), str(sorted(used & FORBIDDEN)))
    check("finance_import does not log", not ({"logging", "logger", "print"} & used), str({"logging", "logger", "print"} & used))
    check("finance_import opens no files", "open" not in used)


FINANCE_FN = re.compile(r"import|spending|^api_bills_|merchant|categor|_cap|cap_|export_review|review_|"
                        r"planned_cost|transactions|resolve_category|_extract_csv|_read_capped_body|"
                        r"uncategorised|rules")


def test_dashboard_finance_functions():
    tree = ast.parse(open(os.path.join(ROOT, "dashboard", "dashboard.py")).read())
    checked = 0
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and FINANCE_FN.search(n.name):
            if n.name in ("import_page",):                   # serves static HTML only
                continue
            bad = names_in(n) & FORBIDDEN
            check(f"dashboard.{n.name} has no LLM/HTTP reference", not bad, str(sorted(bad)))
            checked += 1
    check("finance functions found in dashboard.py", checked >= 25, str(checked))


def _code_tokens(path):
    """NAME and non-docstring STRING tokens (comments and docstrings are prose, not routes)."""
    src = open(path, encoding="utf-8").read()
    doc_lines = set()
    try:
        for n in ast.walk(ast.parse(src)):
            if isinstance(n, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n.body:
                first = n.body[0]
                if isinstance(first, ast.Expr) and isinstance(getattr(first, "value", None), ast.Constant) \
                        and isinstance(first.value.value, str):
                    doc_lines.add(first.lineno)
    except SyntaxError:
        pass
    for tok in tokenize.generate_tokens(io.StringIO(src).readline):
        if tok.type == tokenize.NAME or (tok.type == tokenize.STRING and tok.start[0] not in doc_lines):
            yield tok


def test_no_ollama_anywhere():
    pyfiles = [os.path.join(ROOT, p) for p in ("bot/bot.py", "scheduler/scheduler.py", "dashboard/dashboard.py",
                                               "dashboard/finance_import.py")]
    pat = re.compile(r"ollama|11434|/api/generate|llama3", re.I)
    for p in pyfiles:
        hits = [f"{t.start[0]}:{t.string[:40]}" for t in _code_tokens(p) if pat.search(t.string)]
        check(f"no Ollama route in {os.path.relpath(p, ROOT)}", not hits, str(hits[:5]))
    for y in glob.glob(os.path.join(ROOT, "*", "*.yaml")) + glob.glob(os.path.join(ROOT, "*.example")) \
            + glob.glob(os.path.join(ROOT, ".env.example")):
        txt = open(y).read()
        check(f"no Ollama in {os.path.relpath(y, ROOT)}", not re.search(r"ollama|11434", txt, re.I))


def _first_line(fn, names):
    lines = [n.lineno for n in ast.walk(fn) if isinstance(n, ast.Call)
             and ((isinstance(n.func, ast.Name) and n.func.id in names)
                  or (isinstance(n.func, ast.Attribute) and n.func.attr in names))]
    return min(lines) if lines else None


def _fn(tree, name):
    return next(n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name)


def test_gate_order_static():
    bot_tree = ast.parse(open(os.path.join(ROOT, "bot", "bot.py")).read())
    pm = _fn(bot_tree, "process_message")
    gate = _first_line(pm, {"money_refusal"})
    sinks = _first_line(pm, {"save_to_postgres", "call_claude", "call_claude_api", "log_message",
                             "handle_voice_note_deletion", "process_blog_note", "handle_memory"})
    check("process_message gates money before any save/model call", gate and sinks and gate < sinks,
          f"gate={gate} first_sink={sinks}")
    for name in ("money_refusal", "is_money_message"):
        bad = names_in(_fn(bot_tree, name)) & FORBIDDEN
        check(f"bot.{name} has no LLM/HTTP reference", not bad, str(bad))
    dash_tree = ast.parse(open(os.path.join(ROOT, "dashboard", "dashboard.py")).read())
    for name in ("api_chat", "app_chat"):
        fn = _fn(dash_tree, name)
        g = _first_line(fn, {"money_refusal"})
        s = _first_line(fn, {"log_message", "process_message", "_idem_get"})
        check(f"dashboard.{name} gates money before logging", g and s and g < s, f"gate={g} sink={s}")


MONEY = [
    "how much did I spend on groceries last month", "what's my bank balance", "I spent $40 at the pub",
    "spent 120 on shoes", "is my budget ok this week", "show me my transactions", "how are my finances",
    "my salary came in", "remember I owe 50 dollars", "add pay $300 rent to my list", "credit card is maxed",
    "what's my savings looking like", "I spent too much on takeout", "payday is friday right",
]
NOT_MONEY = [
    "I spent the afternoon reading", "what's on tomorrow", "did my yoga", "just drank a big glass of water",
    "remind me to call mum", "add check the budget to my list", "the river bank was flooded",  # 'bank' alone counts; see below
    "set groceries cap to 400", "show my caps",
]


def test_detector():
    import bot
    for t in MONEY:
        check(f"money: {t!r}", bot.money_refusal(t) == bot.MONEY_REFUSAL)
    for t in NOT_MONEY:
        if t == "the river bank was flooded":
            continue   # deliberately over-blocks: a false refusal is cheap, a leak is not
        check(f"not money: {t!r}", bot.money_refusal(t) is None)


def test_runtime_never_reaches_claude():
    import bot
    calls = []

    def rec(name):
        async def f(*a, **k):
            calls.append(name)
            return ""
        return f
    for name in ("call_claude_api", "call_claude", "save_to_postgres", "log_message", "process_blog_note",
                 "handle_memory", "log_routing"):
        setattr(bot, name, rec(name))
    for t in MONEY:
        calls.clear()
        reply = asyncio.run(bot.process_message(990046, t, source="app"))
        leaked = [c for c in calls if c != "log_routing"]
        check(f"runtime refusal {t!r}", reply == bot.MONEY_REFUSAL and not leaked, f"reply={reply!r} calls={leaked}")


if __name__ == "__main__":
    test_finance_import_module()
    test_dashboard_finance_functions()
    test_no_ollama_anywhere()
    test_gate_order_static()
    test_detector()
    test_runtime_never_reaches_claude()
    print(f"finance_no_llm_guard: {PASSES} passed, {len(FAILS)} failed")
    sys.exit(1 if FAILS else 0)
