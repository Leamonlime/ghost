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
