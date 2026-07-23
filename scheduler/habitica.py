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

async def complete_daily_exact(task_name: str) -> bool:
    """Complete a due, not-yet-completed daily whose text matches exactly
    (case-insensitive). Safe for automation — no fuzzy match, never double-scores."""
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
            if task["text"].strip().lower() == task_name.strip().lower():
                if not task.get("isDue", False) or task.get("completed", False):
                    return False
                score = await client.post(
                    f"{BASE_URL}/tasks/{task['id']}/score/up",
                    headers=HEADERS,
                    timeout=30.0
                )
                return score.json().get("success", False)
        return False

async def get_completed_todos_today(tz) -> list:
    """Texts of todos completed today (tz-local). Habitica returns recent completed todos only."""
    from datetime import datetime
    async with httpx.AsyncClient() as client:
        response = await client.get(
            f"{BASE_URL}/tasks/user?type=completedTodos",
            headers=HEADERS,
            timeout=30.0
        )
        data = response.json()
        if not data.get("success"):
            return []
        today = datetime.now(tz).date()
        out = []
        for task in data["data"]:
            stamp = task.get("dateCompleted")
            if not stamp:
                continue
            try:
                done = datetime.fromisoformat(stamp.replace("Z", "+00:00")).astimezone(tz).date()
            except ValueError:
                continue
            if done == today:
                out.append(task["text"])
        return out

async def get_focus_todos() -> list:
    """Incomplete to-dos carrying the 'focus' badge tag (the urgent/today badge from
    the session-9 tag scheme). Two API calls total (tag list + todo list) — never
    per-task fetches, the 30-req/min limit is real. Returns [] on any failure."""
    async with httpx.AsyncClient() as client:
        tr = await client.get(f"{BASE_URL}/tags", headers=HEADERS, timeout=30.0)
        tdata = tr.json()
        if not tdata.get("success"):
            return []
        focus_id = next((t["id"] for t in tdata["data"]
                         if t.get("name", "").strip().lower() == "focus"), None)
        if not focus_id:
            return []
        rr = await client.get(f"{BASE_URL}/tasks/user?type=todos", headers=HEADERS, timeout=30.0)
        rdata = rr.json()
        if not rdata.get("success"):
            return []
        return [t["text"] for t in rdata["data"]
                if not t.get("completed", False) and focus_id in (t.get("tags") or [])]

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
