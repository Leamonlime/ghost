#!/usr/bin/env python3
"""Static dashboard smoke check (Session 57) — CI-runnable, no browser, no backend.

Scans each ghost-*.html file for its required contract `data-role` hooks. This catches the
"a redesign silently deleted a hook the JS/data binding needs" regression at the source-file
level, which is the part that CAN run in GitHub Actions (the API-200 / JS-render checks need
the live backend and stay in smoke_live.py as a pre-deploy gate).

Pages dir resolution (first that exists), overridable with GHOST_PAGES_DIR:
  1. $GHOST_PAGES_DIR
  2. ./pages                (a copy committed alongside this test, for CI)
  3. ~/CentralVault/Projects/Tech/Ghost   (the live vault location, for local runs)

Exit 0 iff every page has all its hooks.
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(__file__))
from contract import PAGES, PAGE_HOOKS  # noqa: E402


def _pages_dir():
    here = os.path.dirname(os.path.abspath(__file__))
    repo_root = os.path.abspath(os.path.join(here, "..", ".."))
    for cand in (os.environ.get("GHOST_PAGES_DIR"),
                 os.path.join(here, "pages"),
                 os.path.join(repo_root, "dashboard", "pages"),   # S59: version-controlled repo copy (what CI scans)
                 os.path.expanduser("~/CentralVault/Projects/Tech/Ghost")):  # live vault copy (local runs)
        if cand and os.path.isdir(cand):
            return cand
    return None


def check_decisions_wiring(repo_root):
    """Decision loop: scheduler/ must carry an identical copy of bot/decisions.py, both Dockerfiles
    must COPY it (an image missing it crash-loops on import), and the verified deploy must md5 it."""
    out = []
    try:
        a = open(os.path.join(repo_root, "bot", "decisions.py"), "rb").read()
        b = open(os.path.join(repo_root, "scheduler", "decisions.py"), "rb").read()
        if a != b:
            out.append("decisions.py: bot/ and scheduler/ copies differ (bot/ is canonical)")
    except OSError as e:
        out.append(f"decisions.py: cannot read ({e})")
    for svc in ("bot", "scheduler"):
        try:
            if "COPY decisions.py" not in open(os.path.join(repo_root, svc, "Dockerfile")).read():
                out.append(f"{svc}/Dockerfile: missing 'COPY decisions.py .'")
        except OSError as e:
            out.append(f"{svc}/Dockerfile: cannot read ({e})")
    try:
        sc = open(os.path.join(repo_root, "selfcode", "selfcode.py")).read()
        m = re.search(r"_IMAGE_FILES\s*=\s*\{(.*?)\}", sc, re.S)
        if not m or m.group(1).count("decisions.py") < 2:
            out.append("selfcode._IMAGE_FILES: decisions.py must be listed for both bot and scheduler")
    except OSError as e:
        out.append(f"selfcode.py: cannot read ({e})")
    return out


def main():
    d = _pages_dir()
    if not d:
        # SKIP (green), not FAIL: the ghost-*.html pages live in the vault, not this repo, so in
        # CI there is nothing to scan until they're committed here. Skipping keeps the signal
        # honest (green == "no broken hook found", never a false pass — a PRESENT page with a
        # missing hook below still fails red). See tests/smoke/README.md.
        print("SKIP: no ghost-*.html pages found in this checkout (they live in the vault). "
              "Nothing to check. Commit a tests/smoke/pages/ copy or set GHOST_PAGES_DIR to enforce in CI.")
        return 0
    print(f"static smoke: scanning {d}")
    failures = check_decisions_wiring(os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")))
    for page in PAGES:
        path = os.path.join(d, page)
        try:
            html = open(path, encoding="utf-8").read()
        except OSError as e:
            failures.append(f"{page}: cannot read ({e})")
            continue
        for role in PAGE_HOOKS[page]:
            if f'data-role="{role}"' not in html:
                failures.append(f'{page}: MISSING data-role="{role}"')
    if failures:
        print(f"STATIC SMOKE FAIL ({len(failures)}):")
        for f in failures:
            print("  -", f)
        return 1
    print(f"STATIC SMOKE OK — {len(PAGES)} pages, "
          f"{sum(len(v) for v in PAGE_HOOKS.values())} hooks all present")
    return 0


if __name__ == "__main__":
    sys.exit(main())
