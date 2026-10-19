"""Google Calendar integration for Ghost (Ghost's OWN hivequeen OAuth app — NOT the
claude.ai/Cowork connector; only Ghost has calendar access, by design).

Full two-way read/write via the Calendar v3 REST API, spoken over plain httpx with an
OAuth2 refresh-token grant — no google client library, mirroring fatsecret.py's
zero-extra-dependency contract. Every function degrades gracefully (None/[] when
unconfigured or on error) so an absent/expired credential never crashes a caller.

Auth model (Session 82, provider decision locked 13/07/2026 — Google over Proton):
    - A Desktop-app OAuth 2.0 client minted once via the interactive consent flow
      (see tools/gcal_mint_token.py). That one-time flow yields a
      long-lived refresh token; from then on token refresh is fully automatic here,
      no further manual steps.
    - Scope: https://www.googleapis.com/auth/calendar (full read/write).

Env (all three required, from ghost-app-secrets, optional:true so pods start without them):
    GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET   — the OAuth app
    GOOGLE_REFRESH_TOKEN                       — the one-time delegated token (does not expire)
    GOOGLE_CALENDAR_ID (optional)              — defaults to 'primary'; point at a
                                                 secondary test calendar for verification.

Calendar data carries NONE of the financial/mood routing restrictions — ordinary
Claude routing is fine (unlike money, which stays on the local-Ollama path).
"""
import asyncio
import logging
import os
import zoneinfo
from datetime import datetime, timedelta, date, time as dtime

import httpx

logger = logging.getLogger(__name__)

LOCAL_TZ = zoneinfo.ZoneInfo(os.environ.get("GHOST_TZ", "UTC"))

CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID")
CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET")
REFRESH_TOKEN = os.environ.get("GOOGLE_REFRESH_TOKEN")
CALENDAR_ID = os.environ.get("GOOGLE_CALENDAR_ID", "primary")

TOKEN_URL = "https://oauth2.googleapis.com/token"
API_BASE = "https://www.googleapis.com/calendar/v3"
_HTTP_TIMEOUT = httpx.Timeout(15.0)

# In-process access-token cache: refreshed on demand, reused until ~1min before expiry.
_token_cache = {"value": None, "exp": 0.0}
_token_lock = asyncio.Lock()


def is_configured() -> bool:
    return bool(CLIENT_ID and CLIENT_SECRET and REFRESH_TOKEN)


async def _access_token() -> str | None:
    """Return a valid access token, refreshing via the refresh-token grant when the
    cached one is missing or within 60s of expiry. Returns None if unconfigured/failed."""
    if not is_configured():
        return None
    now = asyncio.get_event_loop().time()
    if _token_cache["value"] and now < _token_cache["exp"] - 60:
        return _token_cache["value"]
    async with _token_lock:
        now = asyncio.get_event_loop().time()
        if _token_cache["value"] and now < _token_cache["exp"] - 60:
            return _token_cache["value"]
        try:
            async with httpx.AsyncClient() as client:
                r = await client.post(TOKEN_URL, data={
                    "client_id": CLIENT_ID,
                    "client_secret": CLIENT_SECRET,
                    "refresh_token": REFRESH_TOKEN,
                    "grant_type": "refresh_token",
                }, timeout=_HTTP_TIMEOUT)
            if r.status_code != 200:
                logger.error(f"gcal token refresh failed: {r.status_code} {r.text[:200]}")
                return None
            data = r.json()
            _token_cache["value"] = data["access_token"]
            _token_cache["exp"] = asyncio.get_event_loop().time() + int(data.get("expires_in", 3600))
            return _token_cache["value"]
        except Exception as e:
            logger.error(f"gcal token refresh error: {e}")
            return None


async def _api(method: str, path: str, params: dict = None, body: dict = None) -> tuple[int, dict | None]:
    """Authenticated Calendar API call. Returns (status_code, json_or_None). On a 401 it
    refreshes once and retries (covers a token invalidated mid-life). Never raises."""
    token = await _access_token()
    if not token:
        return (0, None)
    url = f"{API_BASE}{path}"
    for attempt in range(2):
        try:
            async with httpx.AsyncClient() as client:
                r = await client.request(
                    method, url, params=params, json=body,
                    headers={"Authorization": f"Bearer {token}"}, timeout=_HTTP_TIMEOUT)
        except Exception as e:
            logger.error(f"gcal {method} {path} error: {e}")
            return (0, None)
        if r.status_code == 401 and attempt == 0:
            _token_cache["value"] = None       # force a refresh and retry once
            token = await _access_token()
            if not token:
                return (401, None)
            continue
        if r.status_code == 204:
            return (204, None)
        try:
            return (r.status_code, r.json())
        except Exception:
            return (r.status_code, None)
    return (0, None)


# --------------------------------------------------------------------------- #
# Normalisation
# --------------------------------------------------------------------------- #
def _parse_edge(edge: dict) -> tuple:
    """A Google event start/end is either {'dateTime','timeZone'} (timed) or {'date'} (all-day).
    Return (aware_datetime_or_date, is_all_day)."""
    if not edge:
        return (None, False)
    if edge.get("dateTime"):
        raw = edge["dateTime"]
        # RFC3339; Python 3.11+ fromisoformat handles the trailing 'Z' and offsets.
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return (None, False)
        return (dt.astimezone(LOCAL_TZ), False)
    if edge.get("date"):
        try:
            return (date.fromisoformat(edge["date"]), True)
        except ValueError:
            return (None, False)
    return (None, False)


def _normalise(ev: dict) -> dict:
    start, all_day = _parse_edge(ev.get("start"))
    end, _ = _parse_edge(ev.get("end"))
    return {
        "id": ev.get("id"),
        "summary": ev.get("summary") or "(no title)",
        "location": ev.get("location") or "",
        "all_day": all_day,
        "start": start,
        "end": end,
        "html_link": ev.get("htmlLink") or "",
        "status": ev.get("status") or "",
    }


def fmt_event(ev: dict) -> str:
    """Short human line for one normalised event, e.g. 'Dentist — 3:00pm' or
    'Trip (all day)'. Times rendered in LOCAL_TZ."""
    s = ev["start"]
    if ev["all_day"] or isinstance(s, date) and not isinstance(s, datetime):
        when = "all day"
    elif isinstance(s, datetime):
        when = s.strftime("%-I:%M%p").lower()
    else:
        when = "time unknown"
    loc = f" @ {ev['location']}" if ev["location"] else ""
    return f"{ev['summary']} — {when}{loc}"


# --------------------------------------------------------------------------- #
# Reads
# --------------------------------------------------------------------------- #
async def get_events(time_min: datetime, time_max: datetime, query: str = None,
                     max_results: int = 50) -> list | None:
    """Normalised, time-ordered events overlapping [time_min, time_max). `query` filters
    server-side (Google's free-text `q`). Returns [] when connected-but-empty, None when
    unconfigured or the call failed (callers distinguish 'nothing on' from 'couldn't check')."""
    if not is_configured():
        return None
    params = {
        "timeMin": time_min.astimezone(zoneinfo.ZoneInfo("UTC")).isoformat().replace("+00:00", "Z"),
        "timeMax": time_max.astimezone(zoneinfo.ZoneInfo("UTC")).isoformat().replace("+00:00", "Z"),
        "singleEvents": "true",      # expand recurring into instances
        "orderBy": "startTime",
        "maxResults": str(max_results),
    }
    if query:
        params["q"] = query
    status, data = await _api("GET", f"/calendars/{CALENDAR_ID}/events", params=params)
    if status != 200 or data is None:
        if status:
            logger.error(f"gcal get_events status {status}")
        return None
    items = [_normalise(e) for e in data.get("items", [])]
    # cancelled instances of recurring events can slip through; drop them.
    return [e for e in items if e["status"] != "cancelled" and e["start"] is not None]


async def get_today_events() -> list | None:
    """Today's events (local midnight → next local midnight)."""
    today = datetime.now(LOCAL_TZ).date()
    start = datetime.combine(today, dtime.min, tzinfo=LOCAL_TZ)
    end = start + timedelta(days=1)
    return await get_events(start, end)


async def get_events_for_date(day: date) -> list | None:
    start = datetime.combine(day, dtime.min, tzinfo=LOCAL_TZ)
    return await get_events(start, start + timedelta(days=1))


async def get_events_in_window(start_dt: datetime, end_dt: datetime, query: str = None) -> list | None:
    """Lookahead over an arbitrary window (lunch/dinner anchor checks, 'next X' searches)."""
    return await get_events(start_dt, end_dt, query=query)


# --------------------------------------------------------------------------- #
# Writes (create / move / delete) — callers gate these behind a confirm-first handshake.
# --------------------------------------------------------------------------- #
def _edge_body(dt, all_day: bool) -> dict:
    if all_day:
        d = dt if isinstance(dt, date) and not isinstance(dt, datetime) else dt.date()
        return {"date": d.isoformat()}
    return {"dateTime": dt.isoformat(), "timeZone": str(LOCAL_TZ)}


async def create_event(summary: str, start, end, all_day: bool = False,
                       location: str = "", description: str = "") -> dict | None:
    """Create an event. `start`/`end` are aware datetimes (timed) or dates (all-day).
    Returns the normalised created event, or None on failure."""
    if not is_configured():
        return None
    body = {
        "summary": summary,
        "start": _edge_body(start, all_day),
        "end": _edge_body(end, all_day),
    }
    if location:
        body["location"] = location
    if description:
        body["description"] = description
    status, data = await _api("POST", f"/calendars/{CALENDAR_ID}/events", body=body)
    if status not in (200, 201) or data is None:
        logger.error(f"gcal create_event status {status}")
        return None
    logger.info(f"gcal event created: {data.get('id')} '{summary}'")
    return _normalise(data)


async def move_event(event_id: str, start, end, all_day: bool = False) -> dict | None:
    """Reschedule an event's start/end (PATCH — other fields untouched)."""
    if not is_configured():
        return None
    body = {"start": _edge_body(start, all_day), "end": _edge_body(end, all_day)}
    status, data = await _api("PATCH", f"/calendars/{CALENDAR_ID}/events/{event_id}", body=body)
    if status != 200 or data is None:
        logger.error(f"gcal move_event status {status}")
        return None
    logger.info(f"gcal event moved: {event_id}")
    return _normalise(data)


async def delete_event(event_id: str) -> bool:
    """Delete an event. True on success (Google returns 204; a 410 = already gone counts too)."""
    if not is_configured():
        return False
    status, _ = await _api("DELETE", f"/calendars/{CALENDAR_ID}/events/{event_id}")
    ok = status in (204, 200, 410)
    if ok:
        logger.info(f"gcal event deleted: {event_id}")
    else:
        logger.error(f"gcal delete_event status {status}")
    return ok
