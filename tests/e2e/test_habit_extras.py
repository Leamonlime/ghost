"""Log-date second tick (g) and optional checklist items (h)."""
import re
from datetime import datetime
import zoneinfo, os

import pytest
from playwright.sync_api import expect

from conftest import BASE, unique
from test_habits_todos import after_post, habit_row, item, load, mini, reload, streak_of

TZ = zoneinfo.ZoneInfo(os.environ.get("GHOST_E2E_TZ", "Australia/Melbourne"))


def today():
    return datetime.now(TZ).date().isoformat()


def mk(api, **body):
    r = api.req.post(f"{BASE}/api/habits/create", data=body)
    assert r.ok and r.json()["id"], r.text()
    return r.json()["id"]


def state(api, hid):
    return next((h for h in api.habits() if h["id"] == hid), None)


# ------------------------------------------------------------------ (g) Log date
def test_log_date_second_tick_can_be_enabled_in_the_create_form(page, api):
    name = unique("connection")
    load(page); page.click("button:has-text('+ New habit')")
    page.fill("#hName", name)
    page.check("#hLogOn")
    after_post(page, "/api/habits/create", lambda: page.click("#habitForm .go"))
    reload(page)
    assert api.find("habit", name)["log_date_label"] == "Log date"
    expect(habit_row(page, name).locator(".logdate-btn")).to_have_text("Log date")


def test_habit_without_second_tick_has_no_log_date_button(page, api):
    name = unique("plain")
    hid = mk(api, name=name)
    load(page)
    expect(habit_row(page, name).locator(".logdate-btn")).to_have_count(0)
    r = api.req.post(f"{BASE}/api/habits/log-date", data={"id": hid})
    assert r.status == 400


def test_normal_tick_does_not_log_the_date(page, api):
    name = unique("tick-nolog")
    hid = mk(api, name=name, log_date_label="Log date")
    load(page)
    row = habit_row(page, name)
    after_post(page, "/api/habits/tap", lambda: row.locator(".tap-btn.check").click())
    reload(page)
    s = state(api, hid)
    assert s["done_today"] is True and s["logged_dates"] == [] and s["logged_today"] is False, s
    expect(habit_row(page, name).locator(".logdate-btn")).not_to_have_class(re.compile(r"\bon\b"))


def test_log_date_tick_records_todays_date_and_persists_without_ticking_the_habit(page, api):
    name = unique("log-only")
    hid = mk(api, name=name, log_date_label="Log date")
    load(page)
    after_post(page, "/api/habits/log-date", lambda: habit_row(page, name).locator(".logdate-btn").click())
    for _ in range(2):
        s = state(api, hid)
        assert s["logged_dates"] == [today()] and s["logged_today"] is True, s
        assert s["done_today"] is False and s["streak"] == 0, "the second tick must not complete the habit"
        expect(habit_row(page, name).locator(".logdate-btn")).to_have_class(re.compile(r"\bon\b"))
        expect(habit_row(page, name)).to_contain_text("last: " + today())
        reload(page)


def test_log_date_tick_can_be_cleared_and_relogged(page, api):
    name = unique("log-toggle")
    hid = mk(api, name=name, log_date_label="Log date")
    load(page)
    for expected in ([today()], [], [today()]):
        after_post(page, "/api/habits/log-date", lambda: habit_row(page, name).locator(".logdate-btn").click())
        assert state(api, hid)["logged_dates"] == expected


def test_log_date_rejects_bad_date_and_accepts_a_backdated_one(page, api):
    hid = mk(api, name=unique("log-date-api"), log_date_label="Log date")
    assert api.req.post(f"{BASE}/api/habits/log-date", data={"id": hid, "date": "not-a-date"}).status == 400
    assert api.req.post(f"{BASE}/api/habits/log-date", data={"id": hid, "date": "2026-10-01"}).ok
    assert state(api, hid)["logged_dates"] == ["2026-10-01"]


def test_log_date_can_be_added_to_an_existing_habit_by_editing(page, api):
    name = unique("log-edit")
    hid = mk(api, name=name)
    load(page)
    habit_row(page, name).locator(".row-edit").click()
    page.check("#hLogOn"); page.fill("#hLogLabel", "Date logged")
    after_post(page, "/api/habits/edit", lambda: page.click("#habitForm .go"))
    assert state(api, hid)["log_date_label"] == "Date logged"
    expect(habit_row(page, name).locator(".logdate-btn")).to_have_text("Date logged")


def test_log_date_tick_is_independent_of_untick(page, api):
    name = unique("log-untick")
    hid = mk(api, name=name, log_date_label="Log date")
    api.req.post(f"{BASE}/api/habits/log-date", data={"id": hid})
    api.tap(type("I", (), {"name": name, "id": hid, "kind": "habit"})(), "check")
    api.tap(type("I", (), {"name": name, "id": hid, "kind": "habit"})(), "uncheck")
    s = state(api, hid)
    assert s["done_today"] is False and s["logged_today"] is True


# ------------------------------------------------------------------ (h) optional checklist items
def _slow(page, api):
    name = unique("slow")
    hid = mk(api, name=name, field_type="checklist",
             checklist=["check todos", "stretch?", "journal?", "read?", "meditate?"])
    load(page)
    return name, hid


def test_optional_items_marked_with_trailing_question_mark_at_create(page, api):
    name, hid = _slow(page, api)
    s = state(api, hid)
    assert [(c["text"], c["optional"]) for c in s["checklist"]] == [
        ("check todos", False), ("stretch", True), ("journal", True), ("read", True), ("meditate", True)]
    expect(item(mini(habit_row(page, name)), "stretch optional")).to_have_count(1)
    expect(habit_row(page, name).locator(".row-meta")).to_contain_text("0 of 1")


def test_ticking_only_the_required_item_completes_the_parent(page, api):
    name, hid = _slow(page, api)
    after_post(page, "/api/habits/checklist", lambda: item(mini(habit_row(page, name)), "check todos").click())
    reload(page)
    s = state(api, hid)
    assert s["done_today"] is True and s["streak"] == 1, s
    expect(habit_row(page, name).locator(".row-meta")).to_contain_text("1 of 1")


def test_ticking_optional_items_alone_does_not_complete_the_parent(page, api):
    name, hid = _slow(page, api)
    for t in ("stretch", "journal"):
        after_post(page, "/api/habits/checklist", lambda t=t: item(mini(habit_row(page, name)), f"{t} optional").click())
    s = state(api, hid)
    assert s["done_today"] is False and s["streak"] == 0
    expect(habit_row(page, name).locator(".row-meta")).to_contain_text("0 of 1 (+2 optional)")


def test_optional_tick_after_completion_does_not_undo_it_and_unticking_it_neither(page, api):
    name, hid = _slow(page, api)
    after_post(page, "/api/habits/checklist", lambda: item(mini(habit_row(page, name)), "check todos").click())
    for _ in range(2):    # tick then un-tick an optional item
        after_post(page, "/api/habits/checklist", lambda: item(mini(habit_row(page, name)), "read optional").click())
        assert state(api, hid)["done_today"] is True
    assert state(api, hid)["streak"] == 1


def test_unticking_the_required_item_reopens_the_parent_even_if_optionals_are_ticked(page, api):
    name, hid = _slow(page, api)
    for t in ("check todos", "meditate optional"):
        after_post(page, "/api/habits/checklist", lambda t=t: item(mini(habit_row(page, name)), t).click())
    after_post(page, "/api/habits/checklist", lambda: item(mini(habit_row(page, name)), "check todos").click())
    assert state(api, hid)["done_today"] is False


def test_item_can_be_switched_between_optional_and_required(page, api):
    name = unique("switch")
    hid = mk(api, name=name, field_type="checklist", checklist=["a", "b"])
    load(page)
    after_post(page, "/api/habits/checklist", lambda: item(mini(habit_row(page, name)), "a").click())
    assert state(api, hid)["done_today"] is False
    line = mini(habit_row(page, name)).locator(".chk-line").filter(has_text="b")
    after_post(page, "/api/habits/checklist/optional", lambda: line.locator(".item-opt").click())   # b -> optional
    s = state(api, hid)
    assert s["checklist"][1]["optional"] is True and s["done_today"] is True and s["streak"] == 1
    line = mini(habit_row(page, name)).locator(".chk-line").filter(has_text="b")
    after_post(page, "/api/habits/checklist/optional", lambda: line.locator(".item-opt").click())   # back to required
    s = state(api, hid)
    assert s["done_today"] is False and s["streak"] == 0


def test_adding_an_item_with_trailing_question_mark_makes_it_optional(page, api):
    name = unique("add-opt")
    hid = mk(api, name=name, field_type="checklist", checklist=["a"])
    load(page)
    page.once("dialog", lambda d: d.accept("bonus?"))
    after_post(page, "/api/habits/checklist/add", lambda: mini(habit_row(page, name)).locator(".chk-add").click())
    assert [(c["text"], c["optional"]) for c in state(api, hid)["checklist"]] == [("a", False), ("bonus", True)]


def test_adding_a_required_item_to_a_completed_checklist_reopens_it(page, api):
    name = unique("add-req")
    hid = mk(api, name=name, field_type="checklist", checklist=["a"])
    load(page)
    after_post(page, "/api/habits/checklist", lambda: item(mini(habit_row(page, name)), "a").click())
    assert state(api, hid)["done_today"] is True
    page.once("dialog", lambda d: d.accept("late addition"))
    after_post(page, "/api/habits/checklist/add", lambda: mini(habit_row(page, name)).locator(".chk-add").click())
    assert state(api, hid)["done_today"] is False


def test_checklist_with_only_optional_items_never_completes(page, api):
    hid = mk(api, name=unique("all-opt"), field_type="checklist", checklist=["x?", "y?"])
    for c in state(api, hid)["checklist"]:
        api.req.post(f"{BASE}/api/habits/checklist", data={"id": c["id"], "done": True})
    assert state(api, hid)["done_today"] is False
