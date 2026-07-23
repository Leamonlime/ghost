FROM python:3.12-slim

WORKDIR /app

RUN pip install python-telegram-bot httpx redis asyncpg faster-whisper

# bake the Whisper model into the image so pods never download it at runtime
RUN python -c "from faster_whisper import WhisperModel; WhisperModel('base', device='cpu', compute_type='int8')"

COPY bot.py .
COPY habitica.py .
COPY fatsecret.py .

CMD ["python", "bot.py"]
