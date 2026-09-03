# Dashboard smoke tests (Session 57)

Guards against the failure that bit before: a UI redesign silently renaming a class/structure
the JS reached into, breaking a data binding with no error at deploy time. The fix was
`data-role="..."` hooks (contractually stable — restyle `.card` freely, never touch `data-role`).
These tests assert those hooks exist.

`contract.py` is the single source of truth: per-page required `data-role` hooks + the API
endpoints that must return 200. Both runners import it, so they can't drift.

## `smoke_live.py` — the real pre-deploy gate (runnable now)
Loads each page in a real browser against a running dashboard, checks every contract hook is in
the DOM, checks no uncaught JS error, and checks each API endpoint returns 200.

```
pip install playwright && playwright install chromium
GHOST_SMOKE_BASE_URL=http://<dashboard-host>:8080 python3 tests/smoke/smoke_live.py
```
Run this before every dashboard deploy. Needs a browser AND the live backend (API-200 +
JS-render), so it is a pre-deploy step, not a CI job.

## `smoke_static.py` — the CI subset (no browser, no backend)
Scans the ghost-*.html files for the required hooks. This is what GitHub Actions runs.

```
python3 tests/smoke/smoke_static.py          # scans ~/CentralVault/... or ./pages or $GHOST_PAGES_DIR
```

## Pages in the repo (S59)

The five `ghost-*.html` pages (+ `ghost-common.css`) are now committed at `dashboard/pages/` — a
version-controlled copy of the vault-served originals — so `smoke_static.py` and the CI workflow
have something to scan. **They are a mirror:** the dashboard still serves the live pages from the
Obsidian vault at request time, so a vault edit must also be reflected here or CI won't catch a
break in it. (Making the dashboard serve from the repo, eliminating the mirror, is a separate 2.0
architecture decision — not done here.)

## CI status — honest
`.github/workflows/dashboard-smoke.yml` runs `smoke_static.py` on push/PR. **It is PARTIAL, by
the repo's structure, not by omission:**
- The `ghost-*.html` pages live in the Obsidian **vault**, not in this repo, and the dashboard
  serves them from there. So in CI there is nothing to scan until a copy is committed under
  `tests/smoke/pages/` (or `GHOST_PAGES_DIR` points at one). Until then the job **SKIPs green**
  (honest: green means "no broken hook found", never a false pass — a present page with a missing
  hook fails red).
- The API-200 and JS-render checks **cannot** run in GitHub Actions — they need the live backend
  (Postgres/Redis/Habitica + the dashboard app), which CI has no access to. Those stay in
  `smoke_live.py`.
- This repo is a frozen public snapshot (Session 25) that does not currently receive regular
  pushes, so "runs on every push" is only literally true once pushing resumes.

Net: the **live smoke test is the real, working safety net today**; the GitHub Actions workflow
is wired and correct but only enforces once the pages are committed here and pushing resumes.
