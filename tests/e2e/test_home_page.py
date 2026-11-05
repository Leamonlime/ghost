"""The home page (/ , the Command Center) must show the REBUILT native habits and todos, not
Habitica's. It reads /api/command-center."""
import re
from datetime import datetime
import os
import zoneinfo

from playwright.sync_api import expect

from conftest import BASE, unique
from test_habit_extras import mk

TZ = zoneinfo.ZoneInfo(os.environ.get("GHOST_E2E_TZ", "Australia/Melbourne"))


def wd():
    return datetime.now(TZ).weekday()


def home(page):
    page.goto(BASE + "/")
    page.wait_for_function("() => document.querySelectorAll('#habitsList [data-role=habit-row]').length > 0"
                           " || document.getElementById('statusTxt').textContent.includes('OFFLINE')", timeout=15000)


def hrow(page, name):
    return page.locator("#habitsList [data-role=habit-row]").filter(has_text=re.compile(re.escape(name)))


def trow(page, name):
    return page.locator("#todosList [data-role=todo-row]").filter(has_text=re.compile(re.escape(name)))


def test_home_page_shows_a_new_plan_habit_and_a_new_plan_todo(page, api):
    h, t = unique("home-habit"), unique("home-todo")
    mk(api, name=h)
    api.create_todo(t, priority="high", due_date="2099-03-04")
    home(page)
    expect(hrow(page, h)).to_have_count(1)
    expect(trow(page, t)).to_have_count(1)
    expect(trow(page, t).locator(".todo-p")).to_have_text("HIGH")
    expect(trow(page, t)).to_contain_text("due 03-04")


def test_home_page_cards_no_longer_say_habitica(page, api):
    mk(api, name=unique("home-title"))
    home(page)
    titles = " ".join(page.locator(".card-title").all_inner_texts())
    assert "Habits" in titles and "ToDos" in titles
    assert not re.search(r"Habits — Habitica|ToDos — Habitica", titles)


def test_home_habits_come_from_the_command_center_native_payload_not_habitica(page, api):
    h = unique("home-payload")
    mk(api, name=h)
    d = api.req.get(f"{BASE}/api/command-center").json()
    names = [x["name"] for x in d["habits"]]
    assert h in names
    mine = next(x for x in d["habits"] if x["name"] == h)
    assert {"progress", "streakUnit", "kind", "frequency"} <= set(mine), mine
    native_names = {x["name"] for x in api.habits() if x["due_today"]}
    assert set(names) == native_names, "home habits must be exactly today's native habits"
    assert [x["text"] for x in d["todos"]] == [x["text"] for x in api.todos() if not x["done"]]


def test_home_shows_weekday_limited_habit_only_on_its_days(page, api):
    on, off = unique("home-due"), unique("home-off")
    mk(api, name=on, frequency_kind="weekdays", frequency_days=[wd()])
    mk(api, name=off, frequency_kind="weekdays", frequency_days=[(wd() + 1) % 7])
    home(page)
    expect(hrow(page, on)).to_have_count(1)
    expect(hrow(page, off)).to_have_count(0)


def test_home_shows_weekly_target_progress(page, api):
    name = unique("home-weekly")
    mk(api, name=name, frequency_kind="count", frequency_count=3)
    home(page)
    expect(hrow(page, name)).to_contain_text("0 of 3 this week")


def test_home_shows_checklist_routine_progress_and_negative_state(page, api):
    r, n = unique("home-routine"), unique("home-smoke")
    hid = mk(api, name=r, field_type="checklist", checklist=["a", "b", "opt?"])
    mk(api, name=n, field_type="negative")
    first = next(c for c in api.req.get(f"{BASE}/api/habits").json()["habits"] if c["id"] == hid)["checklist"][0]
    api.req.post(f"{BASE}/api/habits/checklist", data={"id": first["id"], "done": True})
    home(page)
    expect(hrow(page, r)).to_contain_text("1 of 2")
    expect(hrow(page, n)).to_contain_text("clean so far today")


def test_home_todos_are_in_priority_order_and_checklist_progress_shows(page, api):
    lo, hi, ck = unique("home-low"), unique("home-high"), unique("home-ck")
    api.create_todo(lo, priority="low")
    api.create_todo(hi, priority="high", due_date="2099-12-31")
    api.create_todo(ck, priority="med", kind="checklist", checklist=["x", "y"])
    home(page)
    texts = page.locator("#todosList [data-role=todo-row]").all_inner_texts()
    idx = lambda s: next(i for i, t in enumerate(texts) if s in t)
    assert idx(hi) < idx(ck) < idx(lo), texts
    expect(trow(page, ck)).to_contain_text("0 of 2")


def test_home_overdue_todo_is_styled(page, api):
    name = unique("home-overdue")
    api.create_todo(name, due_date="2000-01-01")
    home(page)
    due = trow(page, name).locator(".tdue")
    expect(due).to_have_count(1)
    assert int(due.evaluate("e => getComputedStyle(e).fontWeight")) >= 600


def test_home_updates_after_a_habit_is_ticked_without_waiting_for_the_cache(page, api):
    name = unique("home-tick")
    hid = mk(api, name=name)
    api.req.get(f"{BASE}/api/command-center")                      # warm the 25s cache
    api.req.post(f"{BASE}/api/habits/tap", data={"id": hid, "direction": "check"})
    d = api.req.get(f"{BASE}/api/command-center").json()
    mine = next(x for x in d["habits"] if x["name"] == name)
    assert mine["doneToday"] is True and mine["streak"] == 1, mine


def test_demo_mode_masks_native_habit_and_todo_names(page, api):
    h, t = unique("home-secret"), unique("home-secret-todo")
    mk(api, name=h); api.create_todo(t)
    d = api.req.get(f"{BASE}/api/command-center?demo=1").json()
    blob = str(d["habits"]) + str(d["todos"])
    assert h not in blob and t not in blob
