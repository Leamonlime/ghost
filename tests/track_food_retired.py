#!/usr/bin/env python3
"""FatSecret retirement + 'Track food' from app food logs (decision #9, 11/10/2026).
No network, no DB: imports scheduler.py with its Redis/Habitica/food_log seams stubbed, and the
real fatsecret.py from both bot/ and scheduler/. Exit 0 iff all pass.
Run: python3 tests/track_food_retired.py
"""
import asyncio
import importlib.util
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.join(ROOT, "scheduler"))
import scheduler  # noqa: E402

fails = []
def check(name, ok, detail=""):
    print(("PASS " if ok else "FAIL ") + name + (f" — {detail}" if detail else ""))
    if not ok:
        fails.append(name)


def _load_fatsecret(d):
    spec = importlib.util.spec_from_file_location(f"fatsecret_{d}", os.path.join(ROOT, d, "fatsecret.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class FakeRedis:
    def __init__(self, store): self.store = store
    async def get(self, k): return self.store.get(k)
    async def set(self, k, v, ex=None): self.store[k] = v
    async def aclose(self): pass


async def run_track(app_count, store, fatsecret_entries=5):
    """Drive the real check_food_tracked with stubbed seams. Returns the list of Habitica ticks."""
    ticks = []
    async def fake_get_redis(): return FakeRedis(store)
    async def fake_count(): return app_count
    async def fake_complete(daily, item):
        ticks.append((daily, item)); return True
    import habitica
    scheduler.get_redis = fake_get_redis
    scheduler.count_app_food_logs_today = fake_count
    scheduler.is_quiet_hours = lambda: False
    habitica.complete_checklist_item = fake_complete
    scheduler._last_food_track_check = 0.0
    # if anything still consulted FatSecret, make it look like a full diary so the test would catch it
    import fatsecret
    async def fake_summary(): return {"entries": fatsecret_entries, "calories": 1, "groups": []}
    fatsecret.get_today_summary = fake_summary
    await scheduler.check_food_tracked()
    return ticks


async def main():
    # 1. FatSecret is retired in BOTH images: every read/write path returns None, configured=False,
    #    even with all four credentials present (so a live secret can't re-enable it).
    for d in ("bot", "scheduler"):
        for k in ("FATSECRET_CONSUMER_KEY", "FATSECRET_CONSUMER_SECRET",
                  "FATSECRET_ACCESS_TOKEN", "FATSECRET_ACCESS_SECRET"):
            os.environ[k] = "x"
        fs = _load_fatsecret(d)
        check(f"{d}/fatsecret RETIRED", fs.RETIRED is True and not fs.is_configured(),
              f"RETIRED={fs.RETIRED} configured={fs.is_configured()} (creds set)")
        r = (await fs.get_today_summary(), await fs.has_logged_meal("lunch"),
             await fs.search_food("toast"), await fs.log_food("toast", "breakfast"))
        check(f"{d}/fatsecret read+write paths off", r == (None, None, None, None), str(r))

    # 2. (01/11/2026) The FatSecret health probe was removed with the retirement; assert it is gone.
    check("health probe: fatsecret removed", not hasattr(scheduler, "check_fatsecret_health")
          and "fatsecret" not in [s for s, _ in scheduler.HEALTH_CHECKS])

    # 3. App food log has an entry -> 'Track food' (Bedtime checklist) ticks, once per day.
    store = {}
    ticks = await run_track(app_count=2, store=store)
    check("app food log entry ticks Track food", ticks == [("Bedtime", "Track food")], str(ticks))
    check("day flag set after tick", store.get("food_daily_completed") == "1", str(store))
    ticks = await run_track(app_count=3, store=store)
    check("second check same day does not re-tick", ticks == [], str(ticks))

    # 4. No app food logs -> no tick, even if FatSecret (stubbed full) would have had entries.
    store = {}
    ticks = await run_track(app_count=0, store=store, fatsecret_entries=9)
    check("no app food log -> no tick (FatSecret ignored)", ticks == [] and not store, f"{ticks} {store}")

    print(f"track food / fatsecret retired: {'all pass' if not fails else f'{len(fails)} FAIL'}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
