FROM python:3.12-slim
WORKDIR /app
COPY scheduler.py .
COPY habitica.py .
COPY fatsecret.py .
RUN pip install httpx redis asyncpg
CMD ["python", "scheduler.py"]
