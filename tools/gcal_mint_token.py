#!/usr/bin/env python3
"""One-time Google Calendar OAuth consent → long-lived refresh token (Ghost, Session 82).

Run this ONCE, by Lea, on a machine with a browser. It performs the interactive Google
consent for Ghost's OWN Desktop-app OAuth client and prints the refresh_token, which then
goes into the k8s secret `ghost-app-secrets` (key `google-refresh-token`). After that, Ghost
refreshes access tokens automatically forever — no further manual steps.

Stdlib only (no pip installs). Uses the loopback-redirect flow that Google's "Desktop app"
client type supports out of the box.

USAGE:
    export GOOGLE_CLIENT_ID='xxxx.apps.googleusercontent.com'
    export GOOGLE_CLIENT_SECRET='xxxx'
    python3 gcal_mint_token.py

It opens (or prints) a Google consent URL; approve with the Google account whose calendar
Ghost should manage. The browser redirect lands back here, the code is exchanged for tokens,
and the refresh_token is printed. Nothing is written to disk.
"""
import http.server
import json
import os
import secrets
import socket
import sys
import threading
import urllib.parse
import urllib.request
import webbrowser

SCOPE = "https://www.googleapis.com/auth/calendar"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"

CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID")
CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET")

if not (CLIENT_ID and CLIENT_SECRET):
    sys.exit("Set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET in the environment first.")

_result = {}


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


PORT = _free_port()
REDIRECT_URI = f"http://localhost:{PORT}/"
STATE = secrets.token_urlsafe(16)


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        q = urllib.parse.urlparse(self.path).query
        params = urllib.parse.parse_qs(q)
        _result.update({k: v[0] for k, v in params.items()})
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        ok = "code" in _result and _result.get("state") == STATE
        msg = ("Ghost calendar authorised. You can close this tab and return to the terminal."
               if ok else f"Authorisation problem: {_result}")
        self.wfile.write(f"<html><body><h3>{msg}</h3></body></html>".encode())

    def log_message(self, *a):
        pass


def main():
    auth = AUTH_URL + "?" + urllib.parse.urlencode({
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": SCOPE,
        "access_type": "offline",       # ask for a refresh token
        "prompt": "consent",            # force a fresh refresh token even on re-auth
        "state": STATE,
    })
    server = http.server.HTTPServer(("127.0.0.1", PORT), Handler)
    threading.Thread(target=server.handle_request, daemon=True).start()

    print("\nOpen this URL in your browser and approve access:\n")
    print(auth + "\n")
    try:
        webbrowser.open(auth)
    except Exception:
        pass
    print(f"Waiting for the redirect on {REDIRECT_URI} ...")

    # block until the handler thread has captured the redirect
    for _ in range(600):
        if _result:
            break
        threading.Event().wait(0.5)
    if "code" not in _result:
        sys.exit(f"No authorisation code received. Got: {_result}")
    if _result.get("state") != STATE:
        sys.exit("State mismatch — aborting (possible CSRF).")

    data = urllib.parse.urlencode({
        "code": _result["code"],
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "redirect_uri": REDIRECT_URI,
        "grant_type": "authorization_code",
    }).encode()
    req = urllib.request.Request(TOKEN_URL, data=data,
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req) as resp:
        tok = json.load(resp)

    rt = tok.get("refresh_token")
    if not rt:
        sys.exit(f"No refresh_token returned (revoke prior consent and retry with prompt=consent). Got keys: {list(tok)}")

    print("\n" + "=" * 60)
    print("SUCCESS. Add these three keys to ghost-app-secrets (NON-destructive merge —")
    print("stringData merges into the existing secret, leaving every other key intact):")
    print("=" * 60)
    patch = json.dumps({"stringData": {
        "google-client-id": CLIENT_ID,
        "google-client-secret": CLIENT_SECRET,
        "google-refresh-token": rt,
    }})
    print("\n  kubectl patch secret ghost-app-secrets -n ghost --type merge \\")
    print(f"    -p '{patch}'\n")
    print("Then restart the consumers so they pick the keys up:")
    print("  kubectl rollout restart deployment ghost-dashboard -n ghost")
    print("  sudo -n /usr/local/sbin/ghost-rebuild bot        # or: kubectl rollout restart deploy/ghost-bot")
    print("  sudo -n /usr/local/sbin/ghost-rebuild scheduler")
    print("\n(access tokens refresh automatically after this; this is the only manual step.)")


if __name__ == "__main__":
    main()
