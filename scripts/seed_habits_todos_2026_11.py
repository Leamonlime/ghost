#!/usr/bin/env python3
"""Seed the November-2026 habits + todos plan (idempotent: skips any live item with the same name).

Run inside the dashboard pod (so it uses the live native module + DB), or locally against an
isolated DB with GHOST_DB_NAME set. Needs GHOST_USER_ID (the owner id the dashboard lists for).

    kubectl exec -i -n ghost deploy/ghost-dashboard -- python3 - < scripts/seed_habits_todos_2026_11.py
    python3 scripts/seed_habits_todos_2026_11.py --dry-run
"""
import asyncio
import os
import sys

sys.path.insert(0, os.environ.get("BOT_CODE_PATH", "/botcode"))
import native  # noqa: E402

ALL = [0, 1, 2, 3, 4, 5, 6]
DRY = "--dry-run" in sys.argv

# (name, kwargs for native.create_habit)
HABITS = [
    # ---- daily core
    ("Cats fed", {}),
    ("Water", {}),
    ("Eat", {"field_type": "checklist", "checklist": ["Breakfast", "Lunch", "Dinner", "Snacks"]}),
    ("Exercise", {}),
    ("Clean litterbox", {}),
    ("Smoking", {"field_type": "negative"}),
    # ---- routines (daily checklists)
    ("Morning routine", {"field_type": "checklist", "checklist": [
        "Slow morning: check the day's todos in Ghost",
        "Slow morning: stretch?", "Slow morning: journal?", "Slow morning: read?", "Slow morning: meditate?",
        "Up: track weight", "Up: supplements + protein shake", "Up: brush teeth", "Up: face/skincare",
        "Up: make bed", "Up: yoga"]}),
    ("Office-day add-on", {"field_type": "checklist", "frequency_days": [1, 2, 3],   # Tue/Wed/Thu
                           "checklist": ["Dress", "Pack bag", "Leave by 7:30"]}),
    ("Evening routine", {"field_type": "checklist", "checklist": ["Tidy", "Litterbox check", "Shower", "Skincare"]}),
    ("Bedtime", {"field_type": "checklist", "checklist": ["Stretch", "Track food", "Vitamins"]}),
    # ---- weekly targets
    ("Weights", {"frequency_kind": "count", "frequency_count": 3}),
    ("Handstand", {"frequency_kind": "count", "frequency_count": 3}),
    ("Splits", {"frequency_kind": "count", "frequency_count": 4}),
    ("Connection", {"frequency_kind": "count", "frequency_count": 3, "frequency_count_max": 4,
                    "log_date_label": "Log date"}),
    ("Post to social media", {"frequency_kind": "count", "frequency_count": 3}),
    ("Substack: post or schedule a blog", {"frequency_kind": "count", "frequency_count": 2}),
    ("Focus project hours", {"field_type": "value", "frequency_kind": "count",
                             "target_value": 5, "target_max": 10, "unit": "hrs"}),
    # ---- monthly
    ("Finances review", {"frequency_kind": "monthly"}),
]

# (text, priority, due_date, checklist or None)
TODOS = [
    ("Get a new job: follow up on applications (if no reply by the weekend, start applying for other roles)", "high", None, None),
    ("Prep office for Mum's arrival", "high", "2026-11-26",
     ["Vanity to bathroom", "Storage across bedroom/office/TV cabinet",
      "Desk + chair + monitors to living room", "Litter boxes to hallway"]),
    ("Declutter", "med", None, ["Office", "Bedroom", "Linen (after Mum arrives)"]),
    ("Depop: list what's left once the bedroom is done", "med", None, None),
    ("Get BC check done (aim for this weekend)", "med", "2026-11-07", None),
    ("Get eye test done", "med", None, None),
    ("Call Budget to add frequent flyer number", "med", "2026-11-06", None),
    ("Product photos + Canva edit", "med", "2026-11-06", ["Part 1 (~15 min)", "Part 2 (1-2 hrs)"]),
    ("Hard drive + Proton move (~2 hrs); side project: photo magazines of memories", "med", "2026-12-31", None),
    ("Schedule next Substack post", "med", None, None),
    ("Bixby routines + home screen", "med", "2026-11-05", None),
    ("Clean up Instagram saved", "med", "2026-11-05", None),
    ("Garden cleanup", "low", None, None),
    ("Stashed recycling bottles, with a tip run", "low", None, None),
    ("Fanfiction", "low", None, ["Finish second draft", "Quick third draft", "Publish"]),
]


async def main():
    uid = int(os.environ.get("GHOST_USER_ID") or os.environ.get("TELEGRAM_USER_ID") or 0) or native.DEFAULT_USER_ID
    have_h = {h["name"] for h in await native.list_habits(uid)}
    have_t = {t["text"] for t in await native.list_todos(uid)}
    made_h = made_t = 0
    for name, kw in HABITS:
        if name in have_h:
            print("skip habit (exists):", name); continue
        print(("would create" if DRY else "create"), "habit:", name)
        if not DRY:
            assert await native.create_habit(name, user_id=uid, **kw), name
        made_h += 1
    for text, prio, due, items in TODOS:
        if text in have_t:
            print("skip todo (exists):", text); continue
        print(("would create" if DRY else "create"), "todo:", text)
        if not DRY:
            assert await native.create_todo(text, kind="checklist" if items else "tickbox", priority=prio,
                                            due_date=due, checklist=items, user_id=uid), text
        made_t += 1
    print(f"habits {made_h}/{len(HABITS)}, todos {made_t}/{len(TODOS)} {'(dry run)' if DRY else 'created'}")


asyncio.run(main())
