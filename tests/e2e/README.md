# Habits & Todos CRUD sanity suite (Playwright + pytest)

Drives the real `/ghost-habits.html` page in Chromium against a **running** dashboard and checks that
habits and todos can be created, changed, completed, reverted and deleted, and that the state
persists across a reload. One test per behaviour; the name says which action broke
(`test_todos_checklist_ticking_all_items_completes_parent`, `test_habits_delete_confirm_removes_after_reload`, ...).

> This lives in the ghost repo (`tests/e2e/`). There is no separate `ghost-playwright-tests` repo or
> `test_dashboard.py`; it sits next to `tests/smoke/` and follows its `GHOST_SMOKE_BASE_URL` convention.

## Run it

```bash
pip install playwright pytest && playwright install chromium
export GHOST_E2E_BASE_URL=http://<dashboard-host>:8080    # falls back to GHOST_SMOKE_BASE_URL, then localhost:8080
export GHOST_APP_API_TOKEN=...                            # optional: enables the phone-app API checks
python3 -m pytest tests/e2e -v -rfxX                      # add -k todos / -k habits / -k checklist to narrow
```

* Without `GHOST_APP_API_TOKEN` the phone-app (`/api/app/v1/*`) tests **skip**, nothing else changes.
  Take the value from the dashboard deployment env; never commit or print it.
* No auth is needed for the dashboard itself (it is Tailscale-only). From the host, the ClusterIP works:
  `kubectl get svc ghost-dashboard -n ghost`.
* `libasound.so.2` missing on a bare host? `apt-get download libasound2t64`, `dpkg -x` it somewhere and put
  its `usr/lib/x86_64-linux-gnu` on `LD_LIBRARY_PATH` (no root needed), or `sudo playwright install-deps chromium`.
* CI: `.github/workflows/e2e-collect.yml` only runs `pytest --collect-only` (syntax/import check). The
  real run needs the live Postgres-backed dashboard, so it is a manual, pre-deploy step like `smoke_live.py`.

## Safety - it writes to the LIVE database

* Every record the tests create is named `ZZ-TEST <label> <random>`.
* The API wrapper only acts on records it created itself (`Item` objects) and refuses names without the prefix.
* The `api` fixture **sweeps** (archives) every `ZZ-TEST ` record before and after each test, so a crashed run
  is cleaned by the next one. Nothing without that prefix is ever read for action, ticked, edited or deleted.
* Ghost's "delete" is a **soft archive** (`habits.active=FALSE`, `todos.archived=TRUE`) by design, so swept
  test rows remain in Postgres as archived rows and show on no surface. Purge them by hand if you want:
  `DELETE FROM habits WHERE name LIKE 'ZZ-TEST %'` / `DELETE FROM todos WHERE text LIKE 'ZZ-TEST %'`
  (child tables cascade or are keyed by those ids - check `\d habit_taps` first).
* Taps on test habits write `habit_taps` / `momentum_events` rows that are not removed by archiving.

## Reading results

* **PASSED** - behaviour works.
* **FAILED** - a control exists but misbehaves (a bug), or the page threw a console error / failed request
  (the `page` fixture fails every test that logs one).
* **XFAIL** - the spec asks for something the app has no control/endpoint for yet (edit/rename/reorder, etc.).
  These are `xfail(strict=True)`: the day the feature ships they turn into XPASS = failure, which is your
  prompt to delete the marker and make the test real.
