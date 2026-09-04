"""Dashboard smoke-test contract (Session 57) — single source of truth for what a redesign
must NOT silently break. Both the live smoke test (smoke_live.py, Playwright) and the CI
static check (smoke_static.py) import this, so they can't drift apart.

Each page lists the `data-role` hooks that MUST be present (the stable JS/data-binding
contract, immune to class/structure restyling) and the API endpoints that MUST return 200.
"""

# page filename -> required data-role hooks that must exist in the served/rendered page
PAGE_HOOKS = {
    "ghost-dashboard-live.html": [
        "nav-item", "habits", "todos", "budget",
        "momentum", "momentum-ratio", "momentum-sparkline", "momentum-counts",
    ],
    "ghost-bills.html":     ["bills-table"],
    "ghost-quest-log.html": ["quest-list"],
    "ghost-horoscope.html": ["profile-select"],
    "ghost-tarot.html":     ["tarot-board"],
    # Ghost 2.0 Phase 1 — native Habits & Todos
    "ghost-habits.html":    ["nav-item", "habits-list", "todos-list", "level-panel"],
}

# API endpoints that must return HTTP 200 (checked in live mode only — they need the backend)
API_ENDPOINTS = [
    "/api/command-center",
    "/api/momentum?window=60",
    "/api/data",
    "/api/feed",
    "/api/quest-engine/state",
    "/api/bills",
    "/api/bill-meta",
    "/api/bill-settings",
    "/api/liveness",
    "/api/version",
    "/api/habits",
    "/api/todos",
    "/api/level",
]

PAGES = list(PAGE_HOOKS.keys())
