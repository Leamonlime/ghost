#!/usr/bin/env python3
"""Live dashboard smoke test (Session 57) — the pre-deploy safety net.

Loads each ghost-*.html page in a real browser against a running dashboard, waits for it to
settle, and FAILS LOUDLY if any contract `data-role` hook is missing, if the page threw an
uncaught JS error, or if any contract API endpoint doesn't return 200. This is exactly the
class of regression that bit before: a redesign quietly renamed a class/structure the JS
reached into, and the data binding broke with no error at deploy time.

Run before any dashboard deploy, against the live dashboard:
    GHOST_SMOKE_BASE_URL=http://<dashboard-host>:8080 python3 tests/smoke/smoke_live.py

Needs: pip install playwright && playwright install chromium. Exit 0 iff all checks pass.

NOTE (honest scope): this needs a real browser AND the live backend (for the API-200 and
JS-render checks), so it is a PRE-DEPLOY gate, not a GitHub-Actions job. The CI job runs the
static subset (smoke_static.py), which needs neither.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from contract import PAGES, PAGE_HOOKS, API_ENDPOINTS  # noqa: E402

BASE = os.environ.get("GHOST_SMOKE_BASE_URL", "http://localhost:8080").rstrip("/")


def main():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("FAIL: playwright not installed. `pip install playwright && playwright install chromium`")
        return 1

    failures = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context()

        # 1. Each page: contract hooks present + no uncaught JS error.
        for page_name in PAGES:
            page = ctx.new_page()
            errors = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            url = f"{BASE}/{page_name}"
            try:
                page.goto(url, wait_until="networkidle", timeout=20000)
            except Exception as e:
                failures.append(f"{page_name}: page load failed: {e}")
                page.close()
                continue
            for role in PAGE_HOOKS[page_name]:
                n = page.locator(f'[data-role="{role}"]').count()
                if n < 1:
                    failures.append(f"{page_name}: MISSING data-role=\"{role}\"")
            if errors:
                failures.append(f"{page_name}: uncaught JS error(s): {errors[:2]}")
            page.close()

        # 2. API endpoints return 200.
        req = ctx.request
        for ep in API_ENDPOINTS:
            try:
                resp = req.get(f"{BASE}{ep}", timeout=20000)
                if resp.status != 200:
                    failures.append(f"API {ep}: HTTP {resp.status}")
            except Exception as e:
                failures.append(f"API {ep}: request failed: {e}")

        browser.close()

    if failures:
        print(f"SMOKE FAIL ({len(failures)}):")
        for f in failures:
            print("  -", f)
        return 1
    print(f"SMOKE OK — {len(PAGES)} pages, {sum(len(v) for v in PAGE_HOOKS.values())} hooks, "
          f"{len(API_ENDPOINTS)} endpoints, all green (base {BASE})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
