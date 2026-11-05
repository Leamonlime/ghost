"""/api/bills/planned-costs used to 500 (bare `month` alias -> SQL syntax error). Regression tests.
The write test only ever creates and deletes ITS OWN row (note starts with ZZ-TEST)."""
import pytest

from conftest import BASE, PREFIX


def test_planned_costs_list_returns_200_with_items_and_tags(api_ctx):
    r = api_ctx.get(f"{BASE}/api/bills/planned-costs")
    assert r.status == 200, f"HTTP {r.status}: {r.text()[:200]}"
    body = r.json()
    assert body["ok"] is True and isinstance(body["items"], list) and "tags" in body
    for it in body["items"][:3]:
        assert set(it) >= {"id", "month", "category", "amount", "tag"}
        assert len(it["month"]) == 7 and it["month"][4] == "-", f"month not YYYY-MM: {it['month']!r}"


def test_planned_cost_add_appears_in_list_with_yyyy_mm_month_then_delete(api_ctx):
    cats = api_ctx.get(f"{BASE}/api/bills/categories").json()["categories"]
    if not cats:
        pytest.skip("no spending categories to attach a planned cost to")
    note = PREFIX + "planned-cost regression"
    r = api_ctx.post(f"{BASE}/api/bills/planned-costs",
                     data={"month": "2099-01", "category": cats[0], "amount": 1.23, "tag": "other", "note": note})
    assert r.ok, r.text()
    pid = r.json()["id"]
    try:
        items = api_ctx.get(f"{BASE}/api/bills/planned-costs").json()["items"]
        mine = next((i for i in items if i["id"] == pid), None)
        assert mine and mine["month"] == "2099-01" and mine["amount"] == 1.23, mine
    finally:
        d = api_ctx.post(f"{BASE}/api/bills/planned-costs/delete", data={"id": pid})
        assert d.ok, d.text()
    items = api_ctx.get(f"{BASE}/api/bills/planned-costs").json()["items"]
    assert not any(i["id"] == pid for i in items)
