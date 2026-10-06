# Ghost — Google Calendar setup (one-time, Session 82)

This is Ghost's **own** hivequeen Google Calendar integration — a separate OAuth app with its
own credentials. It is **not** the claude.ai / Cowork connector; Claude (this chat surface or
any other) does not get calendar access, only Ghost does.

You do the Google consent screen **once**. After that Ghost refreshes access tokens
automatically forever — no further manual steps.

---

## 1. Create the OAuth app (Google Cloud Console — ~5 min, one time)

1. Go to <https://console.cloud.google.com/> and create a project (e.g. `ghost-calendar`), or
   reuse one you own.
2. **Enable the API**: APIs & Services → Library → search **Google Calendar API** → **Enable**.
3. **OAuth consent screen**: APIs & Services → OAuth consent screen.
   - User type: **External** (unless you have Workspace — then Internal is fine).
   - App name `Ghost`, your email as support + developer contact. Save.
   - **Test users**: add your own Google account (the one whose calendar Ghost manages). In
     "Testing" mode the refresh token stays valid for your test-user account — you don't need
     to publish the app.
   - Scopes: you can leave the scope list empty here; the mint script requests
     `https://www.googleapis.com/auth/calendar` directly.
4. **Credentials** → Create credentials → **OAuth client ID** → Application type
   **Desktop app** → name it `ghost-desktop` → Create.
   - Copy the **Client ID** and **Client secret**.

## 2. Mint the refresh token (run the script once, on a machine with a browser)

```bash
cd ~/ghost/tools
export GOOGLE_CLIENT_ID='<the client id>'
export GOOGLE_CLIENT_SECRET='<the client secret>'
python3 gcal_mint_token.py
```

It prints a Google URL (and tries to open it), you approve access with your Google account,
the browser redirects back to the script, and it prints the `refresh_token` plus the exact
`kubectl patch` command to store all three values.

> If you run it on hivequeen over SSH with no browser, copy the printed URL to a browser on
> another machine on the same network — the redirect goes to `http://localhost:<port>`, so run
> the script on the machine whose browser you'll use. (The script binds that localhost port.)

## 3. Store the credentials + turn it on

The script prints this (with real values filled in) — it's a **non-destructive merge** into
`ghost-app-secrets`, leaving every other key intact:

```bash
kubectl patch secret ghost-app-secrets -n ghost --type merge \
  -p '{"stringData":{"google-client-id":"...","google-client-secret":"...","google-refresh-token":"..."}}'
```

Optional: to point Ghost at a **secondary test calendar** first (recommended for the very
first live check), also set `google-calendar-id` to that calendar's ID (Calendar settings →
Integrate calendar → Calendar ID). Leave it unset and Ghost uses your **primary** calendar.

Then restart the three consumers so they pick up the new secret keys (no image rebuild needed —
the env is already wired):

```bash
kubectl rollout restart deployment ghost-dashboard -n ghost
kubectl rollout restart deployment ghost-bot -n ghost
kubectl rollout restart deployment ghost-scheduler -n ghost
```

## 4. Verify it's live

- In dashboard chat or Telegram: **"what's on my calendar today"** → should read your calendar
  (or say it's clear), not "isn't connected yet".
- **"schedule a test event tomorrow at 3pm"** → Ghost reads it back and waits for your **yes**
  before writing; reply **yes**, check it appears, then **"delete my test event"** → yes.

That's it. Access-token refresh is automatic from here on.

---

### Scopes / security notes
- Scope is full read/write calendar (`.../auth/calendar`) — needed for create/move/delete.
- The refresh token is long-lived; it lives only in `ghost-app-secrets` (k8s), never in the repo.
- Revoke anytime at <https://myaccount.google.com/permissions> (Ghost stops working; re-mint to
  restore).
- Every calendar **write** from chat is confirm-first: Ghost echoes exactly what will change and
  only acts on your explicit "yes" that turn.
