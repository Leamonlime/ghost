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
            if task_name.lower() in task["text"].lower():
                score_response = await client.post(
                    f"{BASE_URL}/tasks/{task['id']}/score/up",
                    headers=HEADERS,
                    timeout=30.0
                )
                return score_response.json().get("success", False)
        
        return False
