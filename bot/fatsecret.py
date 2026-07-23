"""FatSecret Platform API integration (official API, free tier).

Reads and writes the user's own food diary via 3-legged OAuth 1.0a — the only auth
model FatSecret offers for user-level data (their OAuth2 is app-level only).
Signing is plain stdlib HMAC-SHA1, no extra dependency.

Env (all four required, from ghost-app-secrets):
    FATSECRET_CONSUMER_KEY / FATSECRET_CONSUMER_SECRET   — the API app
    FATSECRET_ACCESS_TOKEN / FATSECRET_ACCESS_SECRET     — the user's delegated tokens
                                                           (one-time authorize flow, do not expire)

Every function degrades gracefully: None/empty when unconfigured or on error,
mirroring the old cronometer.py contract.
"""
import asyncio
import base64
import hashlib
import hmac
import logging
import os
import secrets as _secrets
import time
import urllib.parse
from datetime import datetime, date
import zoneinfo

import httpx

logger = logging.getLogger(__name__)

LOCAL_TZ = zoneinfo.ZoneInfo(os.environ.get("GHOST_TZ", "UTC"))
API_URL = "https://platform.fatsecret.com/rest/server.api"

CONSUMER_KEY = os.environ.get("FATSECRET_CONSUMER_KEY")
CONSUMER_SECRET = os.environ.get("FATSECRET_CONSUMER_SECRET")
ACCESS_TOKEN = os.environ.get("FATSECRET_ACCESS_TOKEN")
ACCESS_SECRET = os.environ.get("FATSECRET_ACCESS_SECRET")

MEAL_GROUPS = {"breakfast": "breakfast", "lunch": "lunch", "dinner": "dinner", "other": "snacks"}

_HTTP_TIMEOUT = httpx.Timeout(15.0)

def is_configured() -> bool:
    return bool(CONSUMER_KEY and CONSUMER_SECRET and ACCESS_TOKEN and ACCESS_SECRET)

def _pct(s: str) -> str:
    return urllib.parse.quote(str(s), safe="~")

def sign_request(method: str, url: str, params: dict,
                 consumer_key: str, consumer_secret: str,
                 token: str = "", token_secret: str = "") -> dict:
    """Return params + OAuth1 HMAC-SHA1 signature fields for a request."""
    oauth = {
        "oauth_consumer_key": consumer_key,
        "oauth_nonce": _secrets.token_hex(16),
        "oauth_signature_method": "HMAC-SHA1",
        "oauth_timestamp": str(int(time.time())),
        "oauth_version": "1.0",
    }
    if token:
        oauth["oauth_token"] = token
    all_params = {**params, **oauth}
    param_str = "&".join(f"{_pct(k)}={_pct(v)}" for k, v in sorted(all_params.items()))
    base_string = f"{method.upper()}&{_pct(url)}&{_pct(param_str)}"
    key = f"{_pct(consumer_secret)}&{_pct(token_secret)}"
    digest = hmac.new(key.encode(), base_string.encode(), hashlib.sha1).digest()
    all_params["oauth_signature"] = base64.b64encode(digest).decode()
    return all_params

async def _call(method_name: str, extra: dict) -> dict | None:
    """Signed call to the platform API. Returns parsed JSON or None on failure."""
    params = {"method": method_name, "format": "json", **extra}
    signed = sign_request("GET", API_URL, params,
                          CONSUMER_KEY, CONSUMER_SECRET, ACCESS_TOKEN, ACCESS_SECRET)
    async with httpx.AsyncClient() as client:
        r = await client.get(API_URL, params=signed, timeout=_HTTP_TIMEOUT)
    data = r.json()
    if "error" in data:
        logger.error(f"FatSecret {method_name} error: {data['error']}")
        return None
    return data

def _days_since_epoch(day: date) -> int:
    return (day - date(1970, 1, 1)).days

def _as_list(value) -> list:
    """FatSecret returns a dict for a single item, a list for many."""
    if value is None:
        return []
    return value if isinstance(value, list) else [value]

def _f(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0

async def get_today_summary() -> dict | None:
    """Today's diary: {'calories','protein','carbs','fat','entries','foods','groups'} or None."""
    if not is_configured():
        return None
    try:
        today = datetime.now(LOCAL_TZ).date()
        data = await _call("food_entries.get.v2", {"date": _days_since_epoch(today)})
        if data is None:
            return None
        entries = _as_list((data.get("food_entries") or {}).get("food_entry"))
        summary = {"calories": 0.0, "protein": 0.0, "carbs": 0.0, "fat": 0.0,
                   "entries": len(entries), "foods": [], "groups": []}
        groups = set()
        for e in entries:
            summary["calories"] += _f(e.get("calories"))
            summary["protein"] += _f(e.get("protein"))
            summary["carbs"] += _f(e.get("carbohydrate"))
            summary["fat"] += _f(e.get("fat"))
            name = e.get("food_entry_name") or e.get("food_entry_description")
            if name:
                summary["foods"].append(str(name))
            meal = str(e.get("meal", "")).lower()
            if meal in MEAL_GROUPS:
                groups.add(MEAL_GROUPS[meal])
        summary["groups"] = sorted(groups)
        return summary
    except Exception as e:
        logger.error(f"FatSecret fetch error: {e}")
        return None

async def has_logged_meal(meal: str) -> bool | None:
    """True/False when diary data is available, None when unconfigured/failed."""
    summary = await get_today_summary()
    if summary is None:
        return None
    return meal.lower().strip() in summary["groups"]

async def search_food(query: str) -> dict | None:
    """Top food match: {'food_id','name','description'} or None."""
    if not is_configured():
        return None
    try:
        data = await _call("foods.search", {"search_expression": query, "max_results": 5})
        if data is None:
            return None
        foods = _as_list((data.get("foods") or {}).get("food"))
        if not foods:
            return None
        top = foods[0]
        return {"food_id": top["food_id"], "name": top.get("food_name", ""),
                "description": top.get("food_description", "")}
    except Exception as e:
        logger.error(f"FatSecret search error: {e}")
        return None

async def log_food(query: str, meal: str = "other") -> dict | None:
    """Search for `query`, log 1 serving of the top match to today's diary.
    Returns {'name','serving','meal','calories'} on success, None on failure."""
    if not is_configured():
        return None
    if meal not in ("breakfast", "lunch", "dinner", "other"):
        meal = "other"
    try:
        match = await search_food(query)
        if not match:
            return None
        food = await _call("food.get.v2", {"food_id": match["food_id"]})
        if food is None:
            return None
        servings = _as_list(((food.get("food") or {}).get("servings") or {}).get("serving"))
        if not servings:
            return None
        serving = servings[0]
        today = datetime.now(LOCAL_TZ).date()
        result = await _call("food_entry.create", {
            "food_id": match["food_id"],
            "food_entry_name": match["name"],
            "serving_id": serving["serving_id"],
            "number_of_units": "1",
            "meal": meal,
            "date": _days_since_epoch(today),
        })
        if result is None:
            return None
        return {"name": match["name"],
                "serving": serving.get("serving_description", "1 serving"),
                "meal": meal,
                "calories": _f(serving.get("calories"))}
    except Exception as e:
        logger.error(f"FatSecret log error: {e}")
        return None