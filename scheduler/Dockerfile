FROM python:3.12-slim
WORKDIR /app
COPY scheduler.py .
COPY habitica.py .
COPY habitica_sync.py .
COPY fatsecret.py .
COPY gcal.py .
RUN pip install httpx redis asyncpg
CMD ["python", "scheduler.py"]
