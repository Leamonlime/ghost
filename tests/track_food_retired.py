#!/usr/bin/env python3
"""FatSecret retirement (decision #9, 11/10/2026) and the retired Habitica 'Track food' tick (S107).
No network, no DB: imports scheduler.py and the real fatsecret.py from both bot/ and scheduler/. Exit 0 iff all pass.
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

    # 3. (S107) Habitica is gone, so the 'Track food' Habitica checklist tick went with it.
    check("check_food_tracked removed", not hasattr(scheduler, "check_food_tracked"))
    check("no Habitica health check", "habitica" not in [s for s, _ in scheduler.HEALTH_CHECKS]
          and not hasattr(scheduler, "check_habitica_health"))
    check("scheduler/habitica.py gone", not os.path.exists(os.path.join(ROOT, "scheduler", "habitica.py")))

    print(f"track food / fatsecret retired: {'all pass' if not fails else f'{len(fails)} FAIL'}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
