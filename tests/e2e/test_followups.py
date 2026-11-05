"""Lea's review follow-ups: negative-habit feedback (2), editable Log date (3), todo order (4)."""
import re

import pytest
from playwright.sync_api import expect

from conftest import BASE, unique
from test_habits_todos import after_post, habit_row, load, reload, todo_row
from test_habit_extras import mk, state, today


# ------------------------------------------------------------------ (2) negative "-" button
@pytest.mark.parametrize("ftype", ["negative", "both"])
def test_minus_button_gives_visible_feedback_and_persists__(page, api, ftype):
    name = unique(f"minus-{ftype}")
    hid = mk(api, name=name, field_type=ftype)
    load(page)
    row = habit_row(page, name)
    before = row.inner_text()
    after_post(page, "/api/habits/tap", lambda: row.locator(".tap-btn.minus").click())
    row = habit_row(page, name)
    assert row.inner_text() != before, "clicking - changed nothing on screen"
    expect(row.locator(".tap-btn.minus")).to_have_class(re.compile(r"\bon\b"))
    expect(row.locator(".row-meta")).to_contain_text("1 slip today" if ftype == "negative" else "−1 today")
    assert state(api, hid)["today_down"] == 1
    reload(page)                                              # persists
    row = habit_row(page, name)
    expect(row.locator(".tap-btn.minus")).to_have_class(re.compile(r"\bon\b"))
    expect(row.locator(".row-meta")).to_contain_text("1 slip today" if ftype == "negative" else "−1 today")


def test_minus_button_counts_repeated_slips(page, api):
    name = unique("minus-twice")
    hid = mk(api, name=name, field_type="negative")
    load(page)
    for _ in range(2):
        after_post(page, "/api/habits/tap", lambda: habit_row(page, name).locator(".tap-btn.minus").click())
    expect(habit_row(page, name).locator(".row-meta")).to_contain_text("2 slips today")
    assert state(api, hid)["today_down"] == 2


def test_negative_habit_before_any_slip_says_clean_and_minus_is_unlit(page, api):
    name = unique("minus-clean")
    mk(api, name=name, field_type="negative")
    load(page)
    row = habit_row(page, name)
    expect(row.locator(".row-meta")).to_contain_text("clean so far today")
    expect(row.locator(".tap-btn.minus")).not_to_have_class(re.compile(r"\bon\b"))


def test_both_habit_plus_then_minus_shows_both_counts(page, api):
    name = unique("both-counts")
    hid = mk(api, name=name, field_type="both")
    load(page)
    after_post(page, "/api/habits/tap", lambda: habit_row(page, name).locator(".tap-btn.plus").click())
    after_post(page, "/api/habits/tap", lambda: habit_row(page, name).locator(".tap-btn.minus").click())
    expect(habit_row(page, name).locator(".row-meta")).to_contain_text("+1 / −1 today")


# ------------------------------------------------------------------ (3) editable Log date
def _edit_date(page, name, value):
    after_post(page, "/api/habits/log-date/edit", lambda: habit_row(page, name).locator(".logdate-edit").fill(value))


def test_saved_log_date_can_be_changed_to_a_different_date_and_persists(page, api):
    name = unique("logdate-change")
    hid = mk(api, name=name, log_date_label="Log date")
    load(page)
    after_post(page, "/api/habits/log-date", lambda: habit_row(page, name).locator(".logdate-btn").click())
    assert state(api, hid)["logged_dates"] == [today()]
    _edit_date(page, name, "2026-10-01")
    for _ in range(2):                                            # now, and after a reload
        s = state(api, hid)
        assert s["logged_dates"] == ["2026-10-01"] and s["last_logged"] == "2026-10-01", s
        expect(habit_row(page, name).locator(".logdate-edit")).to_have_value("2026-10-01")
        reload(page)


def test_saved_log_date_can_be_cleared_and_persists(page, api):
    name = unique("logdate-clear")
    hid = mk(api, name=name, log_date_label="Log date")
    api.req.post(f"{BASE}/api/habits/log-date", data={"id": hid, "date": "2026-10-05"})
    load(page)
    expect(habit_row(page, name).locator(".logdate-edit")).to_have_value("2026-10-05")
    _edit_date(page, name, "")
    reload(page)
    s = state(api, hid)
    assert s["logged_dates"] == [] and s["last_logged"] is None and s["logged_today"] is False, s
    expect(habit_row(page, name).locator(".logdate-edit")).to_have_value("")


def test_log_date_can_be_picked_when_none_was_saved(page, api):
    name = unique("logdate-pick")
    hid = mk(api, name=name, log_date_label="Log date")
    load(page)
    _edit_date(page, name, "2026-09-20")
    reload(page)
    assert state(api, hid)["logged_dates"] == ["2026-09-20"]
    expect(habit_row(page, name).locator(".logdate-edit")).to_have_value("2026-09-20")


def test_editing_log_date_does_not_tick_the_habit(page, api):
    name = unique("logdate-notick")
    hid = mk(api, name=name, log_date_label="Log date")
    load(page)
    _edit_date(page, name, "2026-10-02")
    s = state(api, hid)
    assert s["done_today"] is False and s["streak"] == 0


def test_log_date_edit_rejects_bad_dates_and_habits_without_the_tick(page, api):
    hid = mk(api, name=unique("logdate-bad"), log_date_label="Log date")
    plain = mk(api, name=unique("logdate-plain"))
    assert api.req.post(f"{BASE}/api/habits/log-date/edit", data={"id": hid, "new_date": "nope"}).status == 400
    assert api.req.post(f"{BASE}/api/habits/log-date/edit", data={"id": plain, "new_date": "2026-10-01"}).status == 400


def test_old_log_date_is_kept_in_history_as_inactive_not_hard_deleted(page, api):
    """Changing a date flips the old row inactive (same soft-delete policy as everything else)."""
    hid = mk(api, name=unique("logdate-soft"), log_date_label="Log date")
    api.req.post(f"{BASE}/api/habits/log-date", data={"id": hid, "date": "2026-10-01"})
    api.req.post(f"{BASE}/api/habits/log-date/edit", data={"id": hid, "old_date": "2026-10-01", "new_date": "2026-10-09"})
    assert state(api, hid)["logged_dates"] == ["2026-10-09"]
    # re-picking the old date brings it straight back (row existed, was only inactive)
    api.req.post(f"{BASE}/api/habits/log-date/edit", data={"id": hid, "new_date": "2026-10-01"})
    assert state(api, hid)["logged_dates"] == ["2026-10-09", "2026-10-01"]


# ------------------------------------------------------------------ (4) todo order
def _rank(p):
    return {"high": 0, "med": 1, "low": 2}[p]


def test_todos_sorted_by_priority_then_due_date_then_creation(page, api):
    names = {
        "low-early": ("low", "2099-01-01"), "med-none": ("med", None), "high-late": ("high", "2099-06-01"),
        "med-early": ("med", "2099-01-05"), "high-early": ("high", "2099-02-01"), "high-none": ("high", None),
        "low-none": ("low", None),
    }
    made = {k: unique(k) for k in names}
    for k, (prio, due) in names.items():
        api.create_todo(made[k], priority=prio, due_date=due)
    todos = [t for t in api.todos() if t["text"] in made.values()]
    order = [next(k for k, v in made.items() if v == t["text"]) for t in todos]
    assert order == ["high-early", "high-late", "high-none", "med-early", "med-none", "low-early", "low-none"], order


def test_whole_todo_list_is_ordered_open_first_then_priority_then_due(page, api):
    api.create_todo(unique("order-a"), priority="low", due_date="2099-01-01")
    api.create_todo(unique("order-b"), priority="high")
    done = api.create_todo(unique("order-c"), priority="high", due_date="2099-01-01")
    api.complete_todo(done)
    keys = [(t["done"], _rank(t["priority"]), t["due_date"] is None, t["due_date"] or "") for t in api.todos()]
    assert keys == sorted(keys), "todo list is not ordered done, priority, due date"


def test_todos_page_renders_in_priority_order(page, api):
    lo, hi = unique("page-low"), unique("page-high")
    api.create_todo(lo, priority="low", due_date="2099-01-01")
    api.create_todo(hi, priority="high", due_date="2099-12-01")
    load(page)
    texts = page.locator("#todosList .todo-row .row-name").all_inner_texts()
    assert texts.index(hi) < texts.index(lo), "High todo with a later due date must come before a Low one"
