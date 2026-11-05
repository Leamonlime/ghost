#!/usr/bin/env python3
"""Pure check of the scheduler's weekday -> location pattern (office Tue/Wed/Thu, WFH Mon/Fri)."""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scheduler"))
import scheduler  # noqa: E402

EXPECT = {0: "wfh", 1: "office", 2: "office", 3: "office", 4: "wfh", 5: "weekend", 6: "weekend"}
bad = [(wd, scheduler.pattern_location(wd)[0], want) for wd, want in EXPECT.items()
       if scheduler.pattern_location(wd) != (want, False)]
for wd, got, want in bad:
    print(f"FAIL weekday {wd}: got {got}, want {want}")
print("scheduler location pattern:", "OK (7/7)" if not bad else f"{len(bad)} FAILED")
sys.exit(1 if bad else 0)
