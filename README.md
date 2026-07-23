# Ghost

Personal AI accountability assistant running on a self-hosted Kubernetes cluster.
Talks to me over Telegram, tracks money and habits, and nudges me back on track
when I'm scrolling instead of living.

## What it does
- Conversational interface via Telegram, Claude API for reasoning, with a local
  Ollama model handling anything financial so that data never leaves the home server
- Two-way integration with Habitica (reads dailies/todos, completes tasks from chat)
- Two-way integration with an Obsidian vault (reads for context, writes notes on request)
- Budget tracking logged conversationally ("spent $12 on lunch") into Postgres
- Voice note transcription via a local Whisper model
- Proactive, schedule-aware nudges for water, meals, movement, and doomscrolling,
  escalating in tone rather than repeating the same reminder
- Voice-to-blog pipeline: a voice memo becomes a drafted blog post

## Architecture
- Python with [python-telegram-bot](https://python-telegram-bot.org/) (async) as the
  primary interface, talking to the Telegram Bot API
- Postgres for durable state, Redis for session memory
- Claude API for general reasoning; local Ollama (llama3.2) exclusively for
  financial messages, enforced at the routing layer
- Runs as pods on a self-hosted k3s cluster, secrets via Kubernetes Secrets,
  zero inline values

## Why
Standard reminders get snoozed and ignored. Built to feel like a persistent
friend who remembers yesterday, not a notification you swipe away.

## Status
Live and in daily use since July 2026.
