FROM python:3.11-slim

WORKDIR /app

# Install build deps for asyncpg
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Railway injects PORT; default to 8000 for local docker.
ENV PORT=8000
EXPOSE 8000

# Run pending Alembic migrations before serving (idempotent: no-op when up to date).
CMD alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port ${PORT}
