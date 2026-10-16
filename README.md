# Ghost

[![Last commit](https://img.shields.io/github/last-commit/Leamonlime/ghost/master)](https://github.com/Leamonlime/ghost/commits/master)
[![gitleaks](https://github.com/Leamonlime/ghost/actions/workflows/gitleaks.yml/badge.svg)](https://github.com/Leamonlime/ghost/actions/workflows/gitleaks.yml)

A self-hosted AI accountability assistant. It runs on a home Kubernetes (k3s)
cluster, talks to me over Telegram and a private Android app, tracks my habits,
todos, money and food, and nudges me back on track when I'm scrolling instead of
living.

Built for one person (me), so treat this repo as a worked example of the
architecture rather than a turnkey product. Everything personal is injected
through Kubernetes Secrets; no real values live in the repo.

## What it does

- **Conversation.** Telegram is the primary chat interface; the Claude API does
  the reasoning. The same conversation is shared with the dashboard chat and the
  phone app.
- **Native habits and todos.** Habits (tick, counter, value and checklist types,
  streaks, momentum) and todos live in Ghost's own Postgres tables. They replaced
  the Habitica integration, which is being retired.
- **Money.** Spending is logged conversationally ("spent $12 on lunch") or
  imported from bank CSVs, categorised, and reconciled against recurring bills.
  Financial messages are routed to a local Ollama model only, enforced at the
  routing layer, so that data never leaves the home server.
- **Food.** Nutrition is logged in the phone app against Open Food Facts with a
  USDA FoodData Central fallback, with a personal "my foods" cache.
- **Nudges.** Schedule-aware reminders for water, meals, movement and
  doomscrolling, with quiet hours, a cap-then-stop rule so they never loop, and
  a tone that escalates instead of repeating itself.
- **Calendar.** A private Google Calendar integration with confirm-before-write
  handling for anything it changes.
- **Voice.** Voice notes are transcribed by a local Whisper model; a voice memo
  can be turned into a draft blog post.
- **Dashboard.** A Tailscale-only web UI: habits, bills, a command centre and a
  few themed pages, all sharing one monochrome design system.
- **Phone app API.** A versioned API (`/api/app/v1/`) serving a private
  offline-first Android app: habits, todos, nutrition, chat, widgets and push.
  Push is self-hosted ntfy delivered over UnifiedPush, with no Google push
  service involved.

## How it improves itself

- **Self-code proposals.** Ghost can propose changes to its own bot and scheduler
  code. Nothing applies until approved on the dashboard, and the privileged
  rebuild goes through a fixed, root-owned wrapper that can only rebuild `bot` or
  `scheduler`. (That wrapper and its single-line sudoers grant are host-specific,
  so they are kept out of this repo.)
- **Overnight runner.** A host-side runner works through queued build prompts
  headlessly at night, one branch per prompt. It runs syntax and smoke checks,
  merges only if they pass, and auto-deploys the dashboard alone. Anything it
  cannot decide is stored as a *decision* with options and a recommendation,
  answered by tap (Telegram or the app), which then drives the next run.
  Nothing is ever auto-answered or auto-applied.

## Architecture

```
Telegram / Android app / dashboard
        |
   ghost-bot  ---- Claude API (general reasoning)
   ghost-dashboard (FastAPI)  ---- Ollama (finance only, local)
   ghost-scheduler (nudges, summaries)  ---- Whisper (voice, local)
        |
   Postgres (durable state)   Redis (session memory, pending actions)
```

| Path | What it is |
|---|---|
| `bot/` | Telegram bot: routing, habits/todos, money, voice, calendar |
| `scheduler/` | Proactive nudges, summaries, health checks |
| `dashboard/` | FastAPI dashboard and the phone-app API, plus `pages/` |
| `selfcode/` | Self-code proposals and the rebuild wrapper |
| `tests/smoke/` | Dashboard contract and smoke tests |

Everything runs as pods in the `ghost` namespace. Secrets are read with
`secretKeyRef`; there are no inline values in any manifest.

## Setup

You will need a Linux host running k3s (or any small Kubernetes cluster) with
`kubectl`, Docker, Helm and Tailscale, plus a Telegram bot token (from
BotFather), your own Telegram user ID and an Anthropic API key. Optional: a USDA
FoodData Central key (nutrition fallback) and an ntfy server (phone push).

1. **Clone and read the config reference.**
   ```bash
   git clone https://github.com/Leamonlime/ghost && cd ghost
   cat .env.example
   ```
2. **Create the namespace.**
   ```bash
   kubectl create namespace ghost
   ```
3. **Install Postgres and Redis** with the Bitnami Helm charts, using release
   names `ghost-postgres` and `ghost-redis` in the `ghost` namespace. The charts
   create the secrets the manifests expect (`ghost-postgres-postgresql` and
   `ghost-redis`).
4. **Create the app secret.** Keys are lowercase and hyphenated:
   ```bash
   kubectl -n ghost create secret generic ghost-app-secrets \
     --from-literal=bot-token=... \
     --from-literal=telegram-user-id=... \
     --from-literal=anthropic-api-key=... \
     --from-literal=ghost-user-name=... \
     --from-literal=ghost-tz=Australia/Melbourne \
     --from-literal=ghost-brand-primary=... \
     --from-literal=ghost-brand-secondary=... \
     --from-literal=ghost-brand-domain=... \
     --from-literal=ghost-brand-vault-dir=...
   ```
   The legacy Habitica and FatSecret keys (`habitica-*`, `fatsecret-*`) are only
   needed if you still use those integrations.
5. **Run Ollama** for the finance route (`llama3.2`), reachable from the `ghost`
   namespace. A sanitised Ollama and Helm setup lives in
   [hivequeen-k3s](https://github.com/Leamonlime/hivequeen-k3s).
6. **Adjust the manifests.** The deployment manifests ship as `*.example.yaml`
   (bot, scheduler, dashboard); the host-specific real copies are not published.
   Copy each to its real name, then set the `hostPath` volumes (code mounts and
   the notes vault) to your own paths:
   ```bash
   for s in bot/ghost-bot-deployment scheduler/ghost-scheduler-deployment dashboard/ghost-dashboard; do
     cp "$s.example.yaml" "$s.yaml"   # then edit the hostPath values
   done
   ```
7. **Build the bot and scheduler images** and import them into k3s. The
   manifests use `imagePullPolicy: Never`.
   ```bash
   for s in bot scheduler; do
     docker build -t ghost-$s:latest $s/
     docker save ghost-$s:latest | sudo k3s ctr images import -
   done
   ```
8. **Apply the manifests.** The dashboard runs on `python:3.12-slim` with the
   code mounted from the host, so it needs no image build.
   ```bash
   kubectl apply -f bot/ghost-bot-deployment.yaml
   kubectl apply -f scheduler/ghost-scheduler-deployment.yaml
   kubectl apply -f dashboard/ghost-dashboard.yaml
   ```
9. **Check it is healthy.**
   ```bash
   kubectl get pods -n ghost
   ```
   Expect the bot, scheduler, dashboard, Postgres and Redis pods Running. Send
   your bot a message, then open the dashboard over Tailscale.
10. **Optional: the phone app and push.** Create a device token for the app
    API, run an ntfy server, and register the phone's UnifiedPush endpoint
    through the app. The Android app is a separate private project.

To update after changing code, rebuild and re-import the image for `bot` or
`scheduler` and run `kubectl rollout restart` for that deployment; for the
dashboard just restart the pod.

## Tests

```bash
python3 tests/smoke/smoke_static.py           # CI subset: required data-role hooks
GHOST_SMOKE_BASE_URL=http://<dashboard>:8080 python3 tests/smoke/smoke_live.py
```

The static check runs on every push. The live check needs a running dashboard and
a browser, so it runs before each dashboard deploy. See `tests/smoke/README.md`.

## Why

Standard reminders get snoozed and ignored. Ghost is built to feel like a
persistent friend who remembers yesterday, not a notification you swipe away.

## Status

Live and in daily use since July 2026. This repo is published from the live
system and may trail it by a few sessions.
