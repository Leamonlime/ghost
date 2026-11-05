"""CRUD sanity suite for the Ghost dashboard Habits & Todos page (/ghost-habits.html).

One test per behaviour. Names read  test_<habits|todos>_<behaviour>[<type>]  so a failure says
exactly which action broke. Everything created is named "ZZ-TEST ..." and swept by the `api`
fixture (see conftest.py). Run: see tests/e2e/README.md.

Tests marked xfail(raises=FeatureAbsent, strict=True) probe for a control/endpoint the spec asks
for but the app does not have today; they are the "missing feature" list, not flakes.
"""
import re

import pytest
from playwright.sync_api import expect

from conftest import BASE, PAGE_PATH, PREFIX, FeatureAbsent, unique

ABSENT = pytest.mark.xfail(raises=FeatureAbsent, strict=True)
HABIT_TYPES = ["tickbox", "positive", "negative", "both", "checklist"]
PAST, FUTURE = "2000-01-01", "2099-12-31"


# ------------------------------------------------------------------ UI helpers
def load(page):
    page.goto(BASE + PAGE_PATH)
    page.wait_for_function(
        "() => !document.querySelector('#habitsList').textContent.includes('Loading')"
        " && !document.querySelector('#todosList').textContent.includes('Loading')")


def reload(page):
    page.reload()
    page.wait_for_function(
        "() => !document.querySelector('#habitsList').textContent.includes('Loading')"
        " && !document.querySelector('#todosList').textContent.includes('Loading')")


def exact(name):
    return re.compile("^" + re.escape(name) + "$")


def habit_row(page, name):
    return page.locator(".habit-row").filter(has=page.locator(".row-name", has_text=exact(name)))


def todo_row(page, name):
    return page.locator(".todo-row").filter(has=page.locator(".row-name", has_text=exact(name)))


def mini(row):
    """The checklist-mini block that follows a checklist row."""
    return row.locator("xpath=following-sibling::*[1][contains(@class,'checklist-mini')]")


def item(mini_loc, text):
    return mini_loc.locator(".checklist-item").filter(has_text=exact(text))


def after_post(page, suffix, action):
    """Run `action`, wait for the POST to <suffix> and the list refetch that follows it."""
    is_list = lambda r: (r.request.method == "GET"
                         and r.url.split("?")[0].endswith(("/api/habits", "/api/todos")))
    with page.expect_response(is_list):                      # the re-render fetch the page fires after a write
        with page.expect_response(lambda r: r.url.endswith(suffix) and r.request.method == "POST"):
            action()
    page.wait_for_timeout(120)                               # let the innerHTML swap land


def create_habit_ui(page, name, field="tickbox", freq="weekdays", items=""):
    load(page)
    page.click("button:has-text('+ New habit')")
    page.fill("#hName", name)
    page.select_option("#hField", field)
    page.select_option("#hFreq", freq)
    if items:
        page.fill("#hItems", items)
    after_post(page, "/api/habits/create", lambda: page.click("#habitForm .go"))


def create_todo_ui(page, name, priority="med", due=None):
    load(page)
    page.click("button:has-text('+ New todo')")
    page.fill("#tText", name)
    page.select_option("#tPriority", priority)
    if due:
        page.fill("#tDue", due)
    after_post(page, "/api/todos/create", lambda: page.click("#todoForm .go"))


def streak_of(row):
    return int(re.search(r"(\d+)", row.locator(".streak-pill").inner_text()).group(1))


def struck(loc):
    return "line-through" in loc.evaluate("e => getComputedStyle(e).textDecorationLine")


def need_control(row, selector, what):
    if row.locator(selector).count() == 0:
        raise FeatureAbsent(f"no {what} control on the row (looked for {selector!r})")


# ====================================================================== HABITS: create
@pytest.mark.parametrize("ftype", HABIT_TYPES)
def test_habits_create_persists_after_reload(page, api, ftype):
    name = unique(f"create-{ftype}")
    create_habit_ui(page, name, field=ftype)
    expect(habit_row(page, name)).to_have_count(1)
    reload(page)
    row = habit_row(page, name)
    expect(row).to_have_count(1)
    expect(row.locator(".row-meta")).to_contain_text("positive + negative" if ftype == "both" else ftype)
    h = api.find("habit", name)
    assert h and h["field_type"] == ftype and h["streak"] == 0 and not h["done_today"]


def test_habits_create_weekly_count_frequency_persists(page, api):
    name = unique("freq-count")
    create_habit_ui(page, name, freq="count")
    reload(page)
    expect(habit_row(page, name).locator(".row-meta")).to_contain_text("3x / week")
    assert api.find("habit", name)["frequency_kind"] == "count"


def test_habits_create_checklist_with_initial_items_persists(page, api):
    name = unique("create-items")
    create_habit_ui(page, name, field="checklist", items="alpha, beta, gamma")
    reload(page)
    m = mini(habit_row(page, name))
    expect(m.locator(".checklist-item")).to_have_text(["alpha", "beta", "gamma"])


# ====================================================================== HABITS: tick / count
def test_habits_tickbox_tick_sets_state_and_streak_persist(page, api):
    name = unique("tick")
    h = api.create_habit(name)
    load(page)
    row = habit_row(page, name)
    after_post(page, "/api/habits/tap", lambda: row.locator(".tap-btn.check").click())
    for _ in range(2):   # immediately, then after a reload
        expect(row.locator(".tap-btn.check")).to_have_class(re.compile(r"\bon\b"))
        assert streak_of(row) == 1
        assert api.habit(h)["done_today"] is True
        reload(page); row = habit_row(page, name)


def test_habits_tickbox_untick_reverts_state_and_streak(page, api):
    name = unique("untick")
    h = api.create_habit(name)
    api.tap(h, "check")
    load(page)
    row = habit_row(page, name)
    expect(row.locator(".tap-btn.check")).to_have_class(re.compile(r"\bon\b"))
    after_post(page, "/api/habits/tap", lambda: row.locator(".tap-btn.check").click())   # click the lit tick again
    reload(page); row = habit_row(page, name)
    expect(row.locator(".tap-btn.check")).not_to_have_class(re.compile(r"\bon\b"))
    assert streak_of(row) == 0
    assert api.habit(h)["done_today"] is False


def test_habits_counter_positive_increments_persist(page, api):
    name = unique("counter")
    h = api.create_habit(name, "positive")
    load(page)
    row = habit_row(page, name)
    for _ in range(3):
        after_post(page, "/api/habits/tap", lambda: row.locator(".tap-btn.plus").click())
    reload(page); row = habit_row(page, name)
    state = api.habit(h)
    assert state["today_up"] == 3, state
    assert state["done_today"] is True and streak_of(row) == 1


def test_habits_counter_positive_shows_todays_count_in_ui(page, api):
    """After 3 taps the row should show the count somewhere (the API has today_up=3)."""
    name = unique("counter-visible")
    h = api.create_habit(name, "positive")
    for _ in range(3):
        api.tap(h, "up")
    load(page)
    row = habit_row(page, name)
    text = row.locator(".row-main").inner_text() + row.locator(".tap-btns").inner_text()
    assert re.search(r"\b3 today\b", text), "no visible today-count on a counter habit (API has today_up=3); row text=" + repr(text)


def test_habits_negative_decrement_persist(page, api):
    name = unique("negative")
    h = api.create_habit(name, "negative")
    load(page)
    row = habit_row(page, name)
    after_post(page, "/api/habits/tap", lambda: row.locator(".tap-btn.minus").click())
    reload(page)
    state = api.habit(h)
    assert state["today_down"] == 1 and state["today_up"] == 0, state
    assert state["done_today"] is False and state["streak"] == 0


def test_habits_both_up_then_down_updates_net_state(page, api):
    name = unique("both")
    h = api.create_habit(name, "both")
    load(page)
    row = habit_row(page, name)
    after_post(page, "/api/habits/tap", lambda: row.locator(".tap-btn.plus").click())
    assert api.habit(h)["done_today"] is True and api.habit(h)["streak"] == 1
    after_post(page, "/api/habits/tap", lambda: row.locator(".tap-btn.minus").click())
    reload(page); row = habit_row(page, name)
    state = api.habit(h)
    assert (state["today_up"], state["today_down"]) == (1, 1), state
    assert state["done_today"] is False and streak_of(row) == 0, "net +1-1 = 0 should not count as done"


def test_habits_value_entry_logged_via_app_api_persists(page, api, requires_app_token):
    name = unique("value")
    h = api.create_habit(name, "positive")
    r = api.app("POST", f"/habits/{h.id}/log", data={"direction": "up", "value": 250})
    assert r.ok, r.text()
    state = api.habit(h)
    assert state["today_value"] == 250 and state["done_today"] is True, state
    load(page)
    assert streak_of(habit_row(page, name)) == 1
    today = api.app("GET", "/today").json()
    mine = next((x for x in today["habits"] if x["id"] == h.id), None)
    assert mine and mine["logged_value"] == 250, mine


@ABSENT
def test_habits_counter_untick_or_undo_last_increment(page, api):
    name = unique("undo")
    api.create_habit(name, "positive")
    load(page)
    need_control(habit_row(page, name), ".tap-btn.minus, .tap-btn.undo, [title*='undo' i]", "undo/decrement on a positive counter")


# ====================================================================== HABITS: edit
def _habit_edit(page, name, new_name=None, ftype=None, freq=None):
    row = habit_row(page, name)
    row.locator(".row-edit").click()
    if new_name:
        page.fill("#hName", new_name)
    if ftype:
        page.select_option("#hField", ftype)
    if freq:
        page.select_option("#hFreq", freq)
    after_post(page, "/api/habits/edit", lambda: page.click("#habitForm .go"))


def test_habits_edit_rename_saves_and_persists(page, api):
    name, new = unique("edit-rename"), unique("renamed")
    h = api.create_habit(name)
    api.tap(h, "check")
    load(page)
    _habit_edit(page, name, new_name=new)
    reload(page)
    expect(habit_row(page, new)).to_have_count(1)
    expect(habit_row(page, name)).to_have_count(0)
    s = api.habit(h)
    assert s["name"] == new and s["streak"] == 1 and s["done_today"], "rename must keep the streak/history"


def test_habits_edit_change_type_saves_and_persists(page, api):
    name = unique("edit-type")
    h = api.create_habit(name, "tickbox")
    load(page)
    _habit_edit(page, name, ftype="both")
    reload(page)
    assert api.habit(h)["field_type"] == "both"
    expect(habit_row(page, name).locator(".tap-btn.plus")).to_have_count(1)
    expect(habit_row(page, name).locator(".tap-btn.minus")).to_have_count(1)


def test_habits_edit_change_frequency_saves_and_persists(page, api):
    name = unique("edit-freq")
    h = api.create_habit(name)
    load(page)
    _habit_edit(page, name, freq="count")
    reload(page)
    s = api.habit(h)
    assert s["frequency_kind"] == "count" and s["frequency_label"] == "3x / week", s
    expect(habit_row(page, name).locator(".row-meta")).to_contain_text("3x / week")


def test_habits_edit_cancel_by_closing_form_does_not_turn_add_into_save(page, api):
    name = unique("edit-cancel")
    h = api.create_habit(name)
    load(page)
    habit_row(page, name).locator(".row-edit").click()
    page.click("button:has-text('+ New habit')")        # close the form (cancel)
    page.click("button:has-text('+ New habit')")        # reopen as a fresh Add
    other = unique("fresh")
    page.fill("#hName", other)
    after_post(page, "/api/habits/create", lambda: page.click("#habitForm .go"))
    expect(habit_row(page, name)).to_have_count(1)
    expect(habit_row(page, other)).to_have_count(1)


# ====================================================================== HABITS: delete
def test_habits_delete_cancel_keeps_it(page, api):
    name = unique("del-cancel")
    api.create_habit(name)
    load(page)
    page.once("dialog", lambda d: d.dismiss())
    habit_row(page, name).locator(".row-del").click()
    reload(page)
    expect(habit_row(page, name)).to_have_count(1)


def test_habits_delete_confirm_removes_after_reload(page, api):
    name = unique("del")
    h = api.create_habit(name)
    load(page)
    msgs = []
    page.once("dialog", lambda d: (msgs.append(d.message), d.accept()))
    after_post(page, "/api/habits/delete", lambda: habit_row(page, name).locator(".row-del").click())
    assert msgs and name in msgs[0], "confirm dialog should name the habit"
    reload(page)
    expect(habit_row(page, name)).to_have_count(0)
    assert api.habit(h) is None


# ====================================================================== HABITS: checklist
def _checklist_habit(page, api, n=3):
    name = unique("chk")
    h = api.create_habit(name, "checklist", checklist=[f"item{i}" for i in range(1, n + 1)])
    load(page)
    return name, h


def test_habits_checklist_add_item_persists(page, api):
    name, h = _checklist_habit(page, api)
    page.once("dialog", lambda d: d.accept("newone"))
    after_post(page, "/api/habits/checklist/add", lambda: mini(habit_row(page, name)).locator(".chk-add").click())
    reload(page)
    expect(item(mini(habit_row(page, name)), "newone")).to_have_count(1)
    assert [c["text"] for c in api.habit(h)["checklist"]][-1] == "newone"


def test_habits_checklist_add_empty_item_rejected(page, api):
    name, h = _checklist_habit(page, api)
    page.once("dialog", lambda d: d.accept("   "))
    mini(habit_row(page, name)).locator(".chk-add").click()
    page.wait_for_load_state("networkidle")
    assert len(api.habit(h)["checklist"]) == 3


def test_habits_checklist_tick_item_persists(page, api):
    name, h = _checklist_habit(page, api)
    after_post(page, "/api/habits/checklist", lambda: item(mini(habit_row(page, name)), "item1").click())
    reload(page)
    expect(item(mini(habit_row(page, name)), "item1")).to_have_class(re.compile(r"\bdone\b"))
    assert struck(item(mini(habit_row(page, name)), "item1"))
    assert [c["done"] for c in api.habit(h)["checklist"]] == [True, False, False]


def test_habits_checklist_untick_item_persists(page, api):
    name, h = _checklist_habit(page, api)
    for _ in range(2):   # tick, then untick
        after_post(page, "/api/habits/checklist", lambda: item(mini(habit_row(page, name)), "item1").click())
    reload(page)
    expect(item(mini(habit_row(page, name)), "item1")).not_to_have_class(re.compile(r"\bdone\b"))
    assert [c["done"] for c in api.habit(h)["checklist"]] == [False, False, False]


def test_habits_checklist_parent_shows_progress_1_of_3(page, api):
    name, h = _checklist_habit(page, api)
    after_post(page, "/api/habits/checklist", lambda: item(mini(habit_row(page, name)), "item1").click())
    reload(page)
    text = habit_row(page, name).inner_text() + mini(habit_row(page, name)).inner_text()
    assert re.search(r"\b1\s*(of|/)\s*3\b", text), f"no '1 of 3' progress on the checklist habit; text={text!r}"


def test_habits_checklist_ticking_all_items_completes_parent(page, api):
    name, h = _checklist_habit(page, api)
    for t in ("item1", "item2"):
        after_post(page, "/api/habits/checklist", lambda t=t: item(mini(habit_row(page, name)), t).click())
    assert api.habit(h)["done_today"] is False, "2 of 3 must not complete the parent"
    after_post(page, "/api/habits/checklist", lambda: item(mini(habit_row(page, name)), "item3").click())
    reload(page)
    assert api.habit(h)["done_today"] is True
    row = habit_row(page, name)
    expect(row.locator(".streak-pill")).to_have_class(re.compile(r"\bdone\b"))
    assert streak_of(row) == 1


def test_habits_checklist_unticking_an_item_reopens_parent(page, api):
    name, h = _checklist_habit(page, api, n=2)
    for t in ("item1", "item2"):
        after_post(page, "/api/habits/checklist", lambda t=t: item(mini(habit_row(page, name)), t).click())
    assert api.habit(h)["done_today"] is True
    after_post(page, "/api/habits/checklist", lambda: item(mini(habit_row(page, name)), "item2").click())
    reload(page)
    assert api.habit(h)["done_today"] is False and streak_of(habit_row(page, name)) == 0


def test_habits_checklist_rename_item_persists(page, api):
    name, h = _checklist_habit(page, api)
    page.once("dialog", lambda d: d.accept("renamed-item"))
    line = mini(habit_row(page, name)).locator(".chk-line").filter(has_text="item2")
    after_post(page, "/api/habits/checklist/rename", lambda: line.locator(".item-edit").click())
    reload(page)
    assert [c["text"] for c in api.habit(h)["checklist"]] == ["item1", "renamed-item", "item3"]
    expect(item(mini(habit_row(page, name)), "renamed-item")).to_have_count(1)


def test_habits_checklist_delete_item_persists_and_parent_recomputes(page, api):
    name, h = _checklist_habit(page, api, n=2)
    api.req.post(f"{BASE}/api/habits/checklist", data={"id": api.habit(h)["checklist"][0]["id"], "done": True})
    page.once("dialog", lambda d: d.accept())
    line = mini(habit_row(page, name)).locator(".chk-line").filter(has_text="item2")
    after_post(page, "/api/habits/checklist/delete", lambda: line.locator(".item-del").click())
    reload(page)
    assert [c["text"] for c in api.habit(h)["checklist"]] == ["item1"]
    assert api.habit(h)["done_today"] is True, "deleting the only unticked item leaves all remaining ticked = complete"
    expect(item(mini(habit_row(page, name)), "item2")).to_have_count(0)


def test_habits_checklist_delete_item_cancel_keeps_it(page, api):
    name, h = _checklist_habit(page, api, n=2)
    page.once("dialog", lambda d: d.dismiss())
    mini(habit_row(page, name)).locator(".chk-line").filter(has_text="item2").locator(".item-del").click()
    reload(page)
    assert len(api.habit(h)["checklist"]) == 2


@ABSENT
def test_habits_checklist_reorder_items(page, api):
    name, h = _checklist_habit(page, api)
    need_control(mini(habit_row(page, name)), "[draggable='true'], .item-up, .item-down, .drag-handle", "reorder")


def test_habits_checklist_delete_parent_removes_it_and_its_items(page, api):
    name, h = _checklist_habit(page, api)
    page.once("dialog", lambda d: d.accept())
    after_post(page, "/api/habits/delete", lambda: habit_row(page, name).locator(".row-del").click())
    reload(page)
    expect(habit_row(page, name)).to_have_count(0)
    assert api.habit(h) is None
    # items hang off the parent; none of ours may linger in the visible DOM
    texts = page.locator(".checklist-mini").all_inner_texts()
    assert not any("item1" in t and "item2" in t and "item3" in t for t in texts)


# ====================================================================== TODOS: create
@pytest.mark.parametrize("prio", ["low", "med", "high"])
def test_todos_create_with_priority_persists(page, api, prio):
    name = unique(f"prio-{prio}")
    create_todo_ui(page, name, priority=prio)
    reload(page)
    row = todo_row(page, name)
    expect(row).to_have_count(1)
    expect(row.locator(f".todo-p.{prio}")).to_have_count(1)
    assert api.find("todo", name)["priority"] == prio


def test_todos_create_with_due_date_persists(page, api):
    name = unique("due")
    create_todo_ui(page, name, due=FUTURE)
    reload(page)
    row = todo_row(page, name)
    expect(row.locator(".todo-due")).to_have_text("Due " + FUTURE[5:])
    assert api.find("todo", name)["due_date"] == FUTURE


def test_todos_create_without_due_date_shows_none(page, api):
    name = unique("nodue")
    create_todo_ui(page, name)
    reload(page)
    expect(todo_row(page, name).locator(".todo-due")).to_have_count(0)
    assert api.find("todo", name)["due_date"] is None


# ====================================================================== TODOS: edit
def test_todos_edit_name_priority_due_via_app_api_persists(page, api, requires_app_token):
    name = unique("edit-api")
    t = api.create_todo(name, priority="low")
    new = unique("edited")
    r = api.app("PATCH", f"/todos/{t.id}", data={"text": new, "priority": "high", "due_date": FUTURE})
    assert r.ok, r.text()
    load(page)
    row = todo_row(page, new)
    expect(row).to_have_count(1)
    expect(row.locator(".todo-p.high")).to_have_count(1)
    expect(row.locator(".todo-due")).to_have_text("Due " + FUTURE[5:])
    expect(todo_row(page, name)).to_have_count(0)


def test_todos_edit_clear_due_date_via_app_api(page, api, requires_app_token):
    t = api.create_todo(unique("edit-clear"), due_date=FUTURE)
    assert api.app("PATCH", f"/todos/{t.id}", data={"due_date": None}).ok
    assert api.todo(t)["due_date"] is None


def _todo_edit(page, name, new_name=None, prio=None, due=None):
    todo_row(page, name).locator(".row-edit").click()
    if new_name:
        page.fill("#tText", new_name)
    if prio:
        page.select_option("#tPriority", prio)
    if due is not None:
        page.fill("#tDue", due)
    after_post(page, "/api/todos/edit", lambda: page.click("#todoForm .go"))


def test_todos_edit_name_in_dashboard_ui_persists(page, api):
    name, new = unique("tedit-name"), unique("tedit-renamed")
    t = api.create_todo(name, priority="high", due_date=FUTURE)
    load(page)
    _todo_edit(page, name, new_name=new)
    reload(page)
    expect(todo_row(page, new)).to_have_count(1)
    expect(todo_row(page, name)).to_have_count(0)
    s = api.todo(t)
    assert (s["text"], s["priority"], s["due_date"]) == (new, "high", FUTURE), "editing the name must not disturb other fields"


def test_todos_edit_priority_in_dashboard_ui_persists(page, api):
    name = unique("tedit-prio")
    t = api.create_todo(name, priority="low")
    load(page)
    _todo_edit(page, name, prio="high")
    reload(page)
    expect(todo_row(page, name).locator(".todo-p.high")).to_have_count(1)
    assert api.todo(t)["priority"] == "high"


def test_todos_edit_due_date_set_change_and_clear_in_dashboard_ui(page, api):
    name = unique("tedit-due")
    t = api.create_todo(name)
    load(page)
    _todo_edit(page, name, due=FUTURE)
    assert api.todo(t)["due_date"] == FUTURE
    _todo_edit(page, name, due="2098-06-01")
    assert api.todo(t)["due_date"] == "2098-06-01"
    _todo_edit(page, name, due="")
    reload(page)
    assert api.todo(t)["due_date"] is None
    expect(todo_row(page, name).locator(".todo-due")).to_have_count(0)


def test_todos_edit_empty_name_is_rejected(page, api):
    name = unique("tedit-empty")
    t = api.create_todo(name)
    r = api.req.post(f"{BASE}/api/todos/edit", data={"id": t.id, "text": "   "})
    assert r.status == 400 and api.todo(t)["text"] == name


# ====================================================================== TODOS: complete / delete
def test_todos_complete_strikes_through_and_persists(page, api):
    name = unique("complete")
    t = api.create_todo(name)
    load(page)
    after_post(page, "/api/todos/complete", lambda: todo_row(page, name).locator(".todo-check").click())
    for _ in range(2):
        row = todo_row(page, name)
        expect(row.locator(".todo-check")).to_have_class(re.compile(r"\bdone\b"))
        assert struck(row.locator(".row-name"))
        assert api.todo(t)["done"] is True
        reload(page)


def test_todos_uncomplete_clears_strikethrough_and_persists(page, api):
    name = unique("uncomplete")
    t = api.create_todo(name)
    api.complete_todo(t)
    load(page)
    row = todo_row(page, name)
    assert struck(row.locator(".row-name"))
    after_post(page, "/api/todos/complete", lambda: row.locator(".todo-check").click())
    reload(page)
    row = todo_row(page, name)
    expect(row.locator(".todo-check")).not_to_have_class(re.compile(r"\bdone\b"))
    assert not struck(row.locator(".row-name"))
    assert api.todo(t)["done"] is False


def test_todos_delete_cancel_keeps_it(page, api):
    name = unique("tdel-cancel")
    api.create_todo(name)
    load(page)
    page.once("dialog", lambda d: d.dismiss())
    todo_row(page, name).locator(".row-del").click()
    reload(page)
    expect(todo_row(page, name)).to_have_count(1)


def test_todos_delete_confirm_removes_after_reload(page, api):
    name = unique("tdel")
    t = api.create_todo(name)
    load(page)
    msgs = []
    page.once("dialog", lambda d: (msgs.append(d.message), d.accept()))
    after_post(page, "/api/todos/delete", lambda: todo_row(page, name).locator(".row-del").click())
    assert msgs and name in msgs[0]
    reload(page)
    expect(todo_row(page, name)).to_have_count(0)
    assert api.todo(t) is None


# ====================================================================== TODOS: checklist
def _checklist_todo(page, api, n=3):
    name = unique("tchk")
    t = api.create_todo(name, kind="checklist", checklist=[f"step{i}" for i in range(1, n + 1)])
    load(page)
    return name, t


def test_todos_checklist_add_item_persists_and_updates_count(page, api):
    name, t = _checklist_todo(page, api)
    page.once("dialog", lambda d: d.accept("step4"))
    after_post(page, "/api/todos/checklist/add", lambda: mini(todo_row(page, name)).locator(".chk-add").click())
    reload(page)
    expect(item(mini(todo_row(page, name)), "step4")).to_have_count(1)
    expect(todo_row(page, name).locator(".row-meta")).to_have_text("checklist · 0 of 4")


def test_todos_checklist_tick_item_updates_progress_count(page, api):
    name, t = _checklist_todo(page, api)
    after_post(page, "/api/todos/checklist", lambda: item(mini(todo_row(page, name)), "step1").click())
    reload(page)
    expect(todo_row(page, name).locator(".row-meta")).to_have_text("checklist · 1 of 3")
    expect(item(mini(todo_row(page, name)), "step1")).to_have_class(re.compile(r"\bdone\b"))


def test_todos_checklist_untick_item_updates_progress_count(page, api):
    name, t = _checklist_todo(page, api)
    for _ in range(2):
        after_post(page, "/api/todos/checklist", lambda: item(mini(todo_row(page, name)), "step1").click())
    reload(page)
    expect(todo_row(page, name).locator(".row-meta")).to_have_text("checklist · 0 of 3")


def test_todos_checklist_partial_ticks_do_not_complete_parent(page, api):
    name, t = _checklist_todo(page, api)
    for s in ("step1", "step2"):
        after_post(page, "/api/todos/checklist", lambda s=s: item(mini(todo_row(page, name)), s).click())
    assert api.todo(t)["done"] is False


def test_todos_checklist_ticking_all_items_completes_parent(page, api):
    name, t = _checklist_todo(page, api)
    for s in ("step1", "step2", "step3"):
        after_post(page, "/api/todos/checklist", lambda s=s: item(mini(todo_row(page, name)), s).click())
    reload(page)
    row = todo_row(page, name)
    expect(row.locator(".todo-check")).to_have_class(re.compile(r"\bdone\b"))
    assert struck(row.locator(".row-name")) and api.todo(t)["done"] is True


def test_todos_checklist_unticking_item_reopens_completed_parent(page, api):
    name, t = _checklist_todo(page, api, n=2)
    for s in ("step1", "step2"):
        after_post(page, "/api/todos/checklist", lambda s=s: item(mini(todo_row(page, name)), s).click())
    after_post(page, "/api/todos/checklist", lambda: item(mini(todo_row(page, name)), "step2").click())
    reload(page)
    assert api.todo(t)["done"] is False
    expect(todo_row(page, name).locator(".todo-check")).not_to_have_class(re.compile(r"\bdone\b"))


def test_todos_checklist_rename_item_persists(page, api):
    name, t = _checklist_todo(page, api)
    page.once("dialog", lambda d: d.accept("renamed-step"))
    line = mini(todo_row(page, name)).locator(".chk-line").filter(has_text="step2")
    after_post(page, "/api/todos/checklist/rename", lambda: line.locator(".item-edit").click())
    reload(page)
    assert [c["text"] for c in api.todo(t)["checklist"]] == ["step1", "renamed-step", "step3"]


def test_todos_checklist_delete_item_updates_count_and_completes_parent(page, api):
    name, t = _checklist_todo(page, api, n=2)
    api.req.post(f"{BASE}/api/todos/checklist", data={"id": api.todo(t)["checklist"][0]["id"], "done": True})
    page.once("dialog", lambda d: d.accept())
    load(page)
    line = mini(todo_row(page, name)).locator(".chk-line").filter(has_text="step2")
    after_post(page, "/api/todos/checklist/delete", lambda: line.locator(".item-del").click())
    reload(page)
    assert [c["text"] for c in api.todo(t)["checklist"]] == ["step1"]
    assert api.todo(t)["done"] is True
    expect(todo_row(page, name).locator(".row-meta")).to_have_text("checklist · 1 of 1")


def test_todos_checklist_delete_parent_removes_it_and_its_items(page, api):
    name, t = _checklist_todo(page, api)
    page.once("dialog", lambda d: d.accept())
    after_post(page, "/api/todos/delete", lambda: todo_row(page, name).locator(".row-del").click())
    reload(page)
    expect(todo_row(page, name)).to_have_count(0)
    assert api.todo(t) is None
    assert not any("step1" in t_ for t_ in page.locator(".checklist-mini").all_inner_texts())


# ====================================================================== TODOS: overdue
def test_todos_overdue_styling_for_past_due_date(page, api):
    name = unique("overdue")
    api.create_todo(name, due_date=PAST)
    load(page)
    row = todo_row(page, name)
    expect(row.locator(".todo-due")).to_have_class(re.compile(r"\boverdue\b"))
    assert int(row.locator(".todo-due").evaluate("e => getComputedStyle(e).fontWeight")) >= 600
    assert re.search(r"\d+ overdue", page.locator("#todosSub").inner_text())


def test_todos_overdue_styling_clears_when_due_date_changed(page, api, requires_app_token):
    name = unique("overdue-edit")
    t = api.create_todo(name, due_date=PAST)
    assert api.app("PATCH", f"/todos/{t.id}", data={"due_date": FUTURE}).ok
    load(page)
    expect(todo_row(page, name).locator(".todo-due")).not_to_have_class(re.compile(r"\boverdue\b"))


def test_todos_overdue_styling_clears_when_completed(page, api):
    name = unique("overdue-done")
    t = api.create_todo(name, due_date=PAST)
    load(page)
    after_post(page, "/api/todos/complete", lambda: todo_row(page, name).locator(".todo-check").click())
    reload(page)
    expect(todo_row(page, name).locator(".todo-due")).not_to_have_class(re.compile(r"\boverdue\b"))
    assert api.todo(t)["overdue"] is False


def test_todos_overdue_returns_if_completed_todo_is_reopened(page, api):
    name = unique("overdue-reopen")
    t = api.create_todo(name, due_date=PAST)
    api.complete_todo(t); api.complete_todo(t, False)
    load(page)
    expect(todo_row(page, name).locator(".todo-due")).to_have_class(re.compile(r"\boverdue\b"))


# ====================================================================== TODOS: other views
def test_todos_appear_in_phone_app_today_and_leave_when_completed(page, api, requires_app_token):
    name = unique("appview")
    t = api.create_todo(name, priority="high", due_date=FUTURE)
    today = api.app("GET", "/today").json()
    mine = next((x for x in today["todos"] if x["id"] == t.id), None)
    assert mine, "UI/API-created todo missing from /api/app/v1/today"
    assert (mine["text"], mine["priority"], mine["due_date"], mine["overdue"]) == (name, "high", FUTURE, False)
    assert api.app("POST", f"/todos/{t.id}/complete", data={"done": True}).ok
    assert not any(x["id"] == t.id for x in api.app("GET", "/today").json()["todos"])
    load(page)
    assert struck(todo_row(page, name).locator(".row-name")), "app-completed todo not struck through in dashboard"


def test_todos_created_in_phone_app_appear_in_dashboard(page, api, requires_app_token):
    name = unique("appcreate")
    r = api.app("POST", "/todos", data={"text": name, "priority": "low", "due_date": FUTURE})
    assert r.ok, r.text()
    load(page)
    row = todo_row(page, name)
    expect(row).to_have_count(1)
    expect(row.locator(".todo-p.low")).to_have_count(1)


def test_habits_appear_in_phone_app_today(page, api, requires_app_token):
    name = unique("appview-habit")
    h = api.create_habit(name, "tickbox")
    mine = next((x for x in api.app("GET", "/today").json()["habits"] if x["id"] == h.id), None)
    assert mine and mine["name"] == name and mine["type"] == "tickbox" and mine["done_today"] is False


# ====================================================================== validation + layout
@pytest.mark.parametrize("blank", ["", "   "])
def test_habits_empty_name_is_not_created(page, api, blank):
    before = len(api.habits())
    load(page)
    page.click("button:has-text('+ New habit')")
    page.fill("#hName", blank)
    page.click("#habitForm .go")
    page.wait_for_load_state("networkidle")
    assert len(api.habits()) == before
    r = api.req.post(f"{BASE}/api/habits/create", data={"name": blank})
    assert r.status < 500 and not r.json().get("ok"), f"API accepted blank habit name: {r.status} {r.text()}"


@pytest.mark.parametrize("blank", ["", "   "])
def test_todos_empty_name_is_not_created(page, api, blank):
    before = len(api.todos())
    load(page)
    page.click("button:has-text('+ New todo')")
    page.fill("#tText", blank)
    page.click("#todoForm .go")
    page.wait_for_load_state("networkidle")
    assert len(api.todos()) == before
    r = api.req.post(f"{BASE}/api/todos/create", data={"text": blank})
    assert r.status < 500 and not r.json().get("ok"), f"API accepted blank todo text: {r.status} {r.text()}"


def _no_overflow(page, row, name):
    vw = page.viewport_size["width"]
    box = row.locator(".row-name").bounding_box()
    assert box and box["x"] + box["width"] <= vw + 1, f"{name[:30]!r}: name overflows viewport ({box} vs {vw})"
    assert page.evaluate("document.documentElement.scrollWidth") <= vw + 1, "page scrolls horizontally"


@pytest.mark.parametrize("shape", ["words", "unbroken"])
def test_habits_very_long_name_does_not_break_layout(page, api, shape):
    page.set_viewport_size({"width": 390, "height": 800})
    name = PREFIX + (("long name " * 40) if shape == "words" else "W" * 200) + uuid_tail()
    api.create_habit(name)
    load(page)
    row = habit_row(page, name)
    expect(row).to_have_count(1)
    expect(row.locator(".row-del")).to_be_visible()
    _no_overflow(page, row, name)


@pytest.mark.parametrize("shape", ["words", "unbroken"])
def test_todos_very_long_name_does_not_break_layout(page, api, shape):
    page.set_viewport_size({"width": 390, "height": 800})
    name = PREFIX + (("long name " * 40) if shape == "words" else "W" * 200) + uuid_tail()
    api.create_todo(name, priority="high", due_date=FUTURE)
    load(page)
    row = todo_row(page, name)
    expect(row).to_have_count(1)
    expect(row.locator(".row-del")).to_be_visible()
    _no_overflow(page, row, name)


def uuid_tail():
    import uuid
    return uuid.uuid4().hex[:6]


def test_habits_and_todos_names_with_markup_and_quotes_are_escaped_and_deletable(page, api):
    h_name, t_name = unique("<b>bold</b> it's \"q\""), unique("<i>it</i> it's \"q\"")
    h = api.create_habit(h_name); t = api.create_todo(t_name)
    load(page)
    expect(habit_row(page, h_name)).to_have_count(1)
    expect(todo_row(page, t_name)).to_have_count(1)
    assert page.locator(".row-name b, .row-name i").count() == 0, "name rendered as HTML (XSS)"
    page.once("dialog", lambda d: d.accept())
    after_post(page, "/api/habits/delete", lambda: habit_row(page, h_name).locator(".row-del").click())
    page.once("dialog", lambda d: d.accept())
    after_post(page, "/api/todos/delete", lambda: todo_row(page, t_name).locator(".row-del").click())
    assert api.habit(h) is None and api.todo(t) is None


def test_page_loads_cleanly_with_no_console_or_network_errors(page, api):
    api.create_habit(unique("clean-h")); api.create_todo(unique("clean-t"))
    load(page)
    reload(page)   # the `page` fixture fails the test if anything was logged


def test_habits_tickbox_untick_then_retick_credits_streak_once(page, api):
    name = unique("retick")
    h = api.create_habit(name)
    for d in ("check", "uncheck", "check"):
        api.tap(h, d)
    s = api.habit(h)
    assert s["done_today"] is True and s["streak"] == 1, s
    api.tap(h, "uncheck"); api.tap(h, "uncheck")        # double un-tick is harmless
    s = api.habit(h)
    assert s["done_today"] is False and s["streak"] == 0, s
