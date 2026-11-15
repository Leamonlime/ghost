FROM python:3.12-slim
WORKDIR /app
COPY scheduler.py .
COPY fatsecret.py .
COPY decisions.py .
COPY gcal.py .
RUN pip install httpx redis asyncpg
CMD ["python", "scheduler.py"]
