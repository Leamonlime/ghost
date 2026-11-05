"""UI tests for value habits, weekly targets, specific weekdays and monthly habits (items d, e, f, i)."""
import re
import zoneinfo
from datetime import datetime

import pytest
from playwright.sync_api import expect

from conftest import BASE, unique
from test_habits_todos import after_post, habit_row, load, reload, streak_of

TZ = zoneinfo.ZoneInfo(__import__("os").environ.get("GHOST_E2E_TZ", "Australia/Melbourne"))


def today_wd():
    return datetime.now(TZ).weekday()


def _open_form(page):
    page.click("button:has-text('+ New habit')")


def _post_habit(api, **body):
    r = api.req.post(f"{BASE}/api/habits/create", data=body)
    assert r.ok and r.json()["id"], r.text()
    return r.json()["id"]


# ---------------------------------------------------------------- (d) value habit + (e) weekly range
def test_value_habit_can_be_created_in_the_form_with_weekly_target_range_and_unit(page, api):
    name = unique("focus")
    load(page); _open_form(page)
    page.fill("#hName", name)
    page.select_option("#hField", "value")
    page.select_option("#hFreq", "count")
    page.fill("#hTargetMin", "5"); page.fill("#hTargetMax", "10"); page.fill("#hUnit", "hrs")
    after_post(page, "/api/habits/create", lambda: page.click("#habitForm .go"))
    reload(page)
    h = api.find("habit", name)
    assert (h["field_type"], h["frequency_kind"], h["target_value"], h["target_max"], h["unit"]) == ("value", "count", 5.0, 10.0, "hrs"), h
    expect(habit_row(page, name).locator(".row-meta")).to_contain_text("0 of 5–10 hrs this week")


def test_value_habit_logging_shows_progress_met_and_bonus_over_max(page, api):
    name = unique("focus-log")
    hid = _post_habit(api, name=name, field_type="value", frequency_kind="count", target_value=5, target_max=10, unit="hrs")
    load(page)
    meta = lambda: habit_row(page, name).locator(".row-meta")

    def log(v):
        page.once("dialog", lambda d: d.accept(str(v)))
        after_post(page, "/api/habits/tap", lambda: habit_row(page, name).locator(".tap-btn.plus").click())

    log(2.5)
    expect(meta()).to_contain_text("2.5 of 5–10 hrs this week")
    assert streak_of(habit_row(page, name)) == 0
    log(3)                                            # 5.5 -> target met
    expect(meta()).to_contain_text("5.5 of 5–10 hrs this week ✓")
    assert streak_of(habit_row(page, name)) == 1
    log(5)                                            # 10.5 -> over max = bonus
    expect(meta()).to_contain_text("bonus")
    reload(page)
    p = next(h for h in api.habits() if h["id"] == hid)["period"]
    assert (p["done"], p["met"], p["bonus"]) == (10.5, True, True), p


def test_value_habit_rejects_zero_negative_and_cancelled_values(page, api):
    name = unique("focus-bad")
    hid = _post_habit(api, name=name, field_type="value", frequency_kind="count", target_value=5, unit="hrs")
    for bad in (0, -2, "abc"):
        r = api.req.post(f"{BASE}/api/habits/tap", data={"id": hid, "direction": "up", "value": bad})
        assert r.status == 500 or not r.json().get("ok"), (bad, r.text())
    load(page)
    page.once("dialog", lambda d: d.dismiss())
    habit_row(page, name).locator(".tap-btn.plus").click()
    assert next(h for h in api.habits() if h["id"] == hid)["period"]["done"] == 0


def test_weekly_tickbox_target_range_progress_and_label(page, api):
    name = unique("connection")
    load(page); _open_form(page)
    page.fill("#hName", name); page.select_option("#hFreq", "count")
    page.fill("#hCountMin", "3"); page.fill("#hCountMax", "4")
    after_post(page, "/api/habits/create", lambda: page.click("#habitForm .go"))
    row = habit_row(page, name)
    expect(row.locator(".row-meta")).to_contain_text("3-4x / week")
    expect(row.locator(".row-meta")).to_contain_text("0 of 3–4 this week")
    after_post(page, "/api/habits/tap", lambda: row.locator(".tap-btn.check").click())
    expect(row.locator(".row-meta")).to_contain_text("1 of 3–4 this week")
    after_post(page, "/api/habits/tap", lambda: row.locator(".tap-btn.check").click())       # un-tick
    reload(page)
    expect(habit_row(page, name).locator(".row-meta")).to_contain_text("0 of 3–4 this week")


# ---------------------------------------------------------------- (f) weekday-only habits
def test_specific_days_habit_is_saved_with_the_chosen_days(page, api):
    name = unique("days")
    load(page); _open_form(page)
    page.fill("#hName", name); page.select_option("#hFreq", "days")
    for d in (1, 2, 3):
        page.check(f"#hDays input[value='{d}']")
    after_post(page, "/api/habits/create", lambda: page.click("#habitForm .go"))
    h = api.find("habit", name)
    assert h["frequency_days"] == [1, 2, 3] and h["frequency_label"] == "Tue Wed Thu", h


def test_habit_only_appears_on_its_days_and_hides_otherwise(page, api):
    on, off = unique("due-today"), unique("not-today")
    _post_habit(api, name=on, frequency_kind="weekdays", frequency_days=[today_wd()])
    _post_habit(api, name=off, frequency_kind="weekdays", frequency_days=[(today_wd() + 1) % 7])
    load(page)
    expect(habit_row(page, on)).to_be_visible()
    expect(habit_row(page, off)).to_be_hidden()
    page.click("#offToggle")
    expect(habit_row(page, off)).to_be_visible()
    page.click("#offToggle")
    expect(habit_row(page, off)).to_be_hidden()


def test_off_day_habit_is_not_in_phone_app_today_but_due_one_is(page, api, requires_app_token):
    on, off = unique("app-due"), unique("app-off")
    a = _post_habit(api, name=on, frequency_kind="weekdays", frequency_days=[today_wd()])
    b = _post_habit(api, name=off, frequency_kind="weekdays", frequency_days=[(today_wd() + 3) % 7])
    ids = {x["id"] for x in api.app("GET", "/today").json()["habits"]}
    assert a in ids and b not in ids


def test_editing_a_habit_to_other_days_changes_visibility(page, api):
    name = unique("move-days")
    _post_habit(api, name=name, frequency_kind="weekdays", frequency_days=[today_wd()])
    load(page)
    expect(habit_row(page, name)).to_be_visible()
    habit_row(page, name).locator(".row-edit").click()
    for d in range(7):
        page.set_checked(f"#hDays input[value='{d}']", d == (today_wd() + 2) % 7)
    after_post(page, "/api/habits/edit", lambda: page.click("#habitForm .go"))
    expect(habit_row(page, name)).to_be_hidden()


# ---------------------------------------------------------------- (i) monthly
def test_monthly_habit_created_ticked_once_a_month_and_unticked(page, api):
    name = unique("finances")
    load(page); _open_form(page)
    page.fill("#hName", name); page.select_option("#hFreq", "monthly")
    after_post(page, "/api/habits/create", lambda: page.click("#habitForm .go"))
    row = habit_row(page, name)
    expect(row.locator(".row-meta")).to_contain_text("monthly")
    expect(row.locator(".row-meta")).to_contain_text("0 of 1 this month")
    after_post(page, "/api/habits/tap", lambda: row.locator(".tap-btn.check").click())
    reload(page)
    expect(habit_row(page, name).locator(".row-meta")).to_contain_text("1 of 1 this month ✓")
    assert streak_of(habit_row(page, name)) == 1
    after_post(page, "/api/habits/tap", lambda: habit_row(page, name).locator(".tap-btn.check").click())
    reload(page)
    expect(habit_row(page, name).locator(".row-meta")).to_contain_text("0 of 1 this month")
    assert streak_of(habit_row(page, name)) == 0


def test_monthly_habit_is_available_every_day_not_only_one_date(page, api):
    name = unique("finances-any")
    hid = _post_habit(api, name=name, frequency_kind="monthly")
    assert next(h for h in api.habits() if h["id"] == hid)["due_today"] is True
