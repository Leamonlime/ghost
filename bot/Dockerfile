FROM python:3.12-slim

WORKDIR /app

# 01/11/2026: Telegram (python-telegram-bot) and voice (faster-whisper + baked model) removed.
RUN pip install --no-cache-dir httpx redis asyncpg

COPY bot.py .
COPY native.py .
COPY habitica.py .
COPY fatsecret.py .
COPY decisions.py .
COPY gcal.py .

CMD ["python", "bot.py"]
