FROM python:3.12-slim

WORKDIR /app

RUN pip install --no-cache-dir python-telegram-bot httpx redis asyncpg faster-whisper

# bake the Whisper model into the image so pods never download it at runtime
RUN python -c "from faster_whisper import WhisperModel; WhisperModel('base', device='cpu', compute_type='int8')"

COPY bot.py .
COPY native.py .
COPY habitica.py .
COPY fatsecret.py .
COPY decisions.py .
COPY gcal.py .

CMD ["python", "bot.py"]
