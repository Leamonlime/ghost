import httpx
import os

HABITICA_USER_ID = os.environ.get("HABITICA_USER_ID")
HABITICA_API_TOKEN = os.environ.get("HABITICA_API_TOKEN")
HABITICA_CLIENT = f"{HABITICA_USER_ID}-ghost-hivequeen"

HEADERS = {
    "x-api-user": HABITICA_USER_ID,
    "x-api-key": HABITICA_API_TOKEN,
    "x-client": HABITICA_CLIENT,
    "Content-Type": "application/json"
}

BASE_URL = "https://habitica.com/api/v3"

async def health_check() -> tuple[bool, str]:
    """Session 43: live Habitica reachability probe (GET /user), mirroring the dashboard's
    _habitica_health. Lets the bot chat GROUND an 'is Habitica working?' answer in a real
    just-now response instead of guessing — the 20/08 incident was Ghost asserting
    'Habitica's out' while it was returning 200 the whole time. Returns (ok, detail)."""
    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(f"{BASE_URL}/user?userFields=_id", headers=HEADERS, timeout=8.0)
        return (r.status_code == 200, "200 OK" if r.status_code == 200 else f"http {r.status_code}")
    except Exception as e:
        return (False, f"{type(e).__name__}: {str(e)[:60]}")

async def get_dailies() -> dict:
    """Get today's dailies and their completion status."""
    async with httpx.AsyncClient() as client:
        response = await client.get(
            f"{BASE_URL}/tasks/user?type=dailys",
            headers=HEADERS,
            timeout=30.0
        )
        data = response.json()
        
        if not data.get("success"):
            return {"done": [], "pending": []}
        
        done = []
        pending = []
        
        for task in data["data"]:
            if task.get("isDue", False):
                if task.get("completed", False):
                    done.append(task["text"])
                else:
                    pending.append(task["text"])
        
        return {"done": done, "pending": pending}

async def get_habits() -> list:
    """Get habits."""
    async with httpx.AsyncClient() as client:
        response = await client.get(
            f"{BASE_URL}/tasks/user?type=habits",
            headers=HEADERS,
            timeout=30.0
        )
        data = response.json()
        
        if not data.get("success"):
            return []
        
        return [task["text"] for task in data["data"]]

async def complete_task(task_name: str) -> bool:
    """Find and complete a task by name."""
    async with httpx.AsyncClient() as client:
        response = await client.get(
            f"{BASE_URL}/tasks/user?type=dailys",
            headers=HEADERS,
            timeout=30.0
        )
        data = response.json()
        
        if not data.get("success"):
            return False
        
        for task in data["data"]:
            if any(word in task["text"].lower() for word in task_name.lower().split()):
                score_response = await client.post(
                    f"{BASE_URL}/tasks/{task['id']}/score/up",
                    headers=HEADERS,
                    timeout=30.0
                )
                return score_response.json().get("success", False)
        
        return False

_tag_cache = {}

async def get_or_create_tag(name: str) -> str | None:
    """Return the tag id for `name` (case-insensitive), creating it if missing."""
    key = name.strip().lower()
    if key in _tag_cache:
        return _tag_cache[key]
    async with httpx.AsyncClient() as client:
        response = await client.get(f"{BASE_URL}/tags", headers=HEADERS, timeout=30.0)
        data = response.json()
        if data.get("success"):
            for tag in data["data"]:
                _tag_cache[tag["name"].strip().lower()] = tag["id"]
            if key in _tag_cache:
                return _tag_cache[key]
        created = await client.post(f"{BASE_URL}/tags", headers=HEADERS,
                                    json={"name": name.strip()}, timeout=30.0)
        cdata = created.json()
        if cdata.get("success"):
            _tag_cache[key] = cdata["data"]["id"]
            return _tag_cache[key]
    return None

async def get_checklists() -> dict:
    """Checklist items nested inside dailies AND to-dos.

    Returns {'dailies': {task_text: [(item_text, done), ...]},
             'todos':   {task_text: [(item_text, done), ...]}}
    including only tasks that actually have checklist items.

    Deliberately NOT a per-task GET /tasks/{id} (the approach drafted 21/07): the
    list endpoints already include each task's full `checklist` array — verified
    live — and a second call per task (~50 for the current board) would blow
    Habitica's 30-requests/minute limit. Two calls total, both types covered.
    """
    out = {"dailies": {}, "todos": {}}
    async with httpx.AsyncClient() as client:
        for task_type, bucket in (("dailys", "dailies"), ("todos", "todos")):
            response = await client.get(
                f"{BASE_URL}/tasks/user?type={task_type}",
                headers=HEADERS,
                timeout=30.0
            )
            data = response.json()
            if not data.get("success"):
                continue
            for task in data["data"]:
                if task_type == "todos" and task.get("completed"):
                    continue
                items = task.get("checklist") or []
                if items:
                    out[bucket][task["text"].strip()] = [
                        (i.get("text", ""), bool(i.get("completed"))) for i in items
                    ]
    return out

async def create_todo(text: str, notes: str = "", tags: list | None = None, date: str | None = None) -> bool:
    """Create a new to-do. tags = list of tag ids; date = 'YYYY-MM-DD' due date."""
    payload = {"text": text, "type": "todo"}
    if notes:
        payload["notes"] = notes
    if tags:
        payload["tags"] = tags
    if date:
        payload["date"] = date
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{BASE_URL}/tasks/user",
            headers=HEADERS,
            json=payload,
            timeout=30.0
        )
        return response.json().get("success", False)

async def create_daily(text: str, frequency: str = "daily") -> bool:
    """Create a new daily task. frequency: daily or weekly."""
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{BASE_URL}/tasks/user",
            headers=HEADERS,
            json={"text": text, "type": "daily", "frequency": frequency},
            timeout=30.0
        )
        return response.json().get("success", False)

async def create_habit(text: str) -> bool:
    """Create a new habit (up-scoring only)."""
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{BASE_URL}/tasks/user",
            headers=HEADERS,
            json={"text": text, "type": "habit", "up": True, "down": False},
            timeout=30.0
        )
        return response.json().get("success", False)

async def log_habit(habit_name: str, direction: str = "up") -> bool:
    """Score a habit up or down by name match."""
    if direction not in ("up", "down"):
        return False
    async with httpx.AsyncClient() as client:
        response = await client.get(
            f"{BASE_URL}/tasks/user?type=habits",
            headers=HEADERS,
            timeout=30.0
        )
        data = response.json()
        if not data.get("success"):
            return False
        for task in data["data"]:
            if habit_name.lower().strip() in task["text"].lower():
                score = await client.post(
                    f"{BASE_URL}/tasks/{task['id']}/score/{direction}",
                    headers=HEADERS,
                    timeout=30.0
                )
                return score.json().get("success", False)
        return False

async def get_todos() -> list:
    """Get incomplete to-dos."""
    async with httpx.AsyncClient() as client:
        response = await client.get(
            f"{BASE_URL}/tasks/user?type=todos",
            headers=HEADERS,
            timeout=30.0
        )
        data = response.json()
        if not data.get("success"):
            return []
        return [task["text"] for task in data["data"] if not task.get("completed", False)]

async def get_user_stats() -> dict:
    """Vitals for the command-center dashboard (Session 35). Full /user is required —
    ?userFields=stats drops the computed maxHealth/maxMP/toNextLevel. All fields real."""
    async with httpx.AsyncClient() as client:
        r = await client.get(f"{BASE_URL}/user", headers=HEADERS, timeout=30.0)
        d = r.json()
        if not d.get("success"):
            return {}
        s = d["data"]["stats"]
        return {"hp": s.get("hp"), "hpMax": s.get("maxHealth"), "mp": s.get("mp"),
                "mpMax": s.get("maxMP"), "xp": s.get("exp"), "xpMax": s.get("toNextLevel"),
                "level": s.get("lvl"), "class": s.get("class"), "gold": s.get("gp")}

async def get_habits_detailed() -> list:
    """Habits as {name,type,positiveNegative,streak,doneToday,atRisk} (Session 35).
    NB: streak/doneToday/atRisk are DERIVED — habits have no native streak/done state
    (those are daily concepts). doneToday=tapped-up today, streak=trailing history days
    with a positive score, atRisk=negative accumulated value. Best-effort, not exact."""
    async with httpx.AsyncClient() as client:
        r = await client.get(f"{BASE_URL}/tasks/user?type=habits", headers=HEADERS, timeout=30.0)
        d = r.json()
        if not d.get("success"):
            return []
        out = []
        for t in d["data"]:
            up, down = bool(t.get("up")), bool(t.get("down"))
            pn = "both" if up and down else ("positive" if up else ("negative" if down else "none"))
            streak = 0
            for h in reversed(t.get("history") or []):
                if (h.get("scoredUp") or 0) > 0 or (h.get("value") or 0) > 0:
                    streak += 1
                else:
                    break
            out.append({"name": t.get("text", "").strip(), "type": "habit",
                        "positiveNegative": pn,
                        "streak": streak,
                        "doneToday": (t.get("counterUp") or 0) > 0,
                        "atRisk": (t.get("value") or 0) < 0})
        return out

async def get_todos_detailed() -> list:
    """Active to-dos as {text,priority,done} (Session 35). /tasks?type=todos returns
    only incomplete ones, so done is False here — the actionable list."""
    async with httpx.AsyncClient() as client:
        r = await client.get(f"{BASE_URL}/tasks/user?type=todos", headers=HEADERS, timeout=30.0)
        d = r.json()
        if not d.get("success"):
            return []
        return [{"text": t.get("text", "").strip(), "priority": t.get("priority", 1),
                 "done": bool(t.get("completed"))} for t in d["data"]]

async def complete_todo(todo_text: str) -> bool:
    """Complete a to-do by exact text match (name comes from get_todos, so exact is
    safe and avoids the partial-word false matches complete_task risks). Session 33."""
    async with httpx.AsyncClient() as client:
        response = await client.get(
            f"{BASE_URL}/tasks/user?type=todos", headers=HEADERS, timeout=30.0
        )
        data = response.json()
        if not data.get("success"):
            return False
        for task in data["data"]:
            if task["text"].strip().lower() == todo_text.strip().lower():
                score = await client.post(
                    f"{BASE_URL}/tasks/{task['id']}/score/up", headers=HEADERS, timeout=30.0
                )
                return score.json().get("success", False)
        return False
