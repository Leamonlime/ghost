"""Shared fixtures for the Habits/Todos CRUD suite.

SAFETY CONTRACT (read before editing):
  * Every record the suite creates is named with PREFIX ("ZZ-TEST ") + a unique suffix.
  * The API wrapper (`Api`) only ever acts on `Item` objects it created itself, and refuses to
    create one whose name lacks the prefix. There is no code path that ticks/edits/deletes an
    id found any other way.
  * The sweep archives ONLY rows whose name starts with PREFIX, before and after every test.
  * "Delete" in Ghost is a soft archive (habits.active=FALSE / todos.archived=TRUE), so swept rows
    stay in Postgres as archived ZZ-TEST rows. They never show on any surface. See README.
"""
import os
import uuid
from dataclasses import dataclass

import pytest

PREFIX = "ZZ-TEST "
BASE = (os.environ.get("GHOST_E2E_BASE_URL") or os.environ.get("GHOST_SMOKE_BASE_URL")
        or "http://localhost:8080").rstrip("/")
APP_TOKEN = os.environ.get("GHOST_APP_API_TOKEN", "")
PAGE_PATH = "/ghost-habits.html"


def unique(label: str) -> str:
    return f"{PREFIX}{label} {uuid.uuid4().hex[:6]}"


@dataclass(frozen=True)
class Item:
    kind: str   # "habit" | "todo"
    id: int
    name: str


class FeatureAbsent(AssertionError):
    """The control/endpoint the spec asks for does not exist in the app. Tests that raise this are
    marked xfail(strict=True): they show as XFAIL today and flip to a hard failure (XPASS) the day
    the feature ships, prompting someone to turn the test into a real one."""


class Api:
    def __init__(self, req):
        self.req = req

    # ---- reads (no side effects) ----
    def habits(self):
        return self.req.get(f"{BASE}/api/habits").json()["habits"]

    def todos(self):
        return self.req.get(f"{BASE}/api/todos?include_done=1").json()["todos"]

    def habit(self, item: Item):
        return next((h for h in self.habits() if h["id"] == item.id), None)

    def todo(self, item: Item):
        return next((t for t in self.todos() if t["id"] == item.id), None)

    def find(self, kind: str, name: str):
        rows = self.habits() if kind == "habit" else self.todos()
        key = "name" if kind == "habit" else "text"
        return next((r for r in rows if r[key] == name), None)

    # ---- writes: only ever on ZZ-TEST items ----
    def create_habit(self, name, field_type="tickbox", frequency_kind="weekdays", checklist=None):
        assert name.startswith(PREFIX), f"refusing to create non-test habit {name!r}"
        body = {"name": name, "field_type": field_type, "frequency_kind": frequency_kind}
        if checklist:
            body["checklist"] = checklist
        r = self.req.post(f"{BASE}/api/habits/create", data=body)
        assert r.ok, r.text()
        hid = r.json()["id"]
        assert hid, r.text()
        return Item("habit", hid, name)

    def create_todo(self, name, priority="med", due_date=None, kind="tickbox", checklist=None):
        assert name.startswith(PREFIX), f"refusing to create non-test todo {name!r}"
        body = {"text": name, "priority": priority, "kind": kind}
        if due_date:
            body["due_date"] = due_date
        if checklist:
            body["checklist"] = checklist
        r = self.req.post(f"{BASE}/api/todos/create", data=body)
        assert r.ok, r.text()
        tid = r.json()["id"]
        assert tid, r.text()
        return Item("todo", tid, name)

    def tap(self, item: Item, direction="up"):
        assert item.name.startswith(PREFIX)
        return self.req.post(f"{BASE}/api/habits/tap", data={"id": item.id, "direction": direction}).json()

    def complete_todo(self, item: Item, done=True):
        assert item.name.startswith(PREFIX)
        return self.req.post(f"{BASE}/api/todos/complete", data={"id": item.id, "done": done}).json()

    def archive(self, item: Item):
        assert item.name.startswith(PREFIX)
        path = "habits" if item.kind == "habit" else "todos"
        return self.req.post(f"{BASE}/api/{path}/delete", data={"id": item.id})

    # ---- phone-app API (bearer token) ----
    def app(self, method, path, **kw):
        headers = {"Authorization": f"Bearer {APP_TOKEN}"}
        return self.req.fetch(f"{BASE}/api/app/v1{path}", method=method, headers=headers, **kw)

    # ---- sweep ----
    def sweep(self):
        """Archive every leftover ZZ-TEST record. Touches nothing else."""
        n = 0
        for h in self.habits():
            if h["name"].startswith(PREFIX):
                self.archive(Item("habit", h["id"], h["name"])); n += 1
        for t in self.todos():
            if t["text"].startswith(PREFIX):
                self.archive(Item("todo", t["id"], t["text"])); n += 1
        return n


@pytest.fixture(scope="session")
def pw():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        yield p


@pytest.fixture(scope="session")
def browser(pw):
    b = pw.chromium.launch()
    yield b
    b.close()


@pytest.fixture(scope="session")
def api_ctx(pw):
    ctx = pw.request.new_context()
    yield ctx
    ctx.dispose()


@pytest.fixture
def api(api_ctx):
    a = Api(api_ctx)
    a.sweep()               # start clean (leftovers from a crashed run)
    yield a
    a.sweep()               # always leave clean


@pytest.fixture
def requires_app_token():
    if not APP_TOKEN:
        pytest.skip("GHOST_APP_API_TOKEN not set - phone-app API checks skipped")


class Watch:
    def __init__(self):
        self.problems = []
        self.allow_http = set()   # status codes a test deliberately provokes


@pytest.fixture
def page(browser, api):
    """A page that records console errors, uncaught JS errors, failed requests and HTTP>=400.
    Any of them fails the test at teardown (the spec: 'no console errors or failed network
    requests during any of the above'). Tests that provoke an error on purpose add to
    `page.watch.allow_http`."""
    ctx = browser.new_context(viewport={"width": 1100, "height": 900})
    pg = ctx.new_page()
    w = Watch()
    pg.watch = w
    pg.on("console", lambda m: w.problems.append(f"console.error: {m.text}") if m.type == "error" else None)
    pg.on("pageerror", lambda e: w.problems.append(f"uncaught JS error: {e}"))

    def _failed(r):
        err = (r.failure or "")
        if "ERR_ABORTED" in err:      # a reload cancelling an in-flight fetch is not a failure
            return
        w.problems.append(f"request failed: {r.method} {r.url} ({err})")
    pg.on("requestfailed", _failed)

    def _resp(r):
        if r.status >= 400 and r.status not in w.allow_http and r.url.startswith(BASE):
            w.problems.append(f"HTTP {r.status}: {r.request.method} {r.url}")
    pg.on("response", _resp)
    yield pg
    ctx.close()
    assert not w.problems, "browser problems during test:\n  " + "\n  ".join(w.problems)
