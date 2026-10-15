FROM python:3.12-slim

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY alembic.ini ./
COPY alembic ./alembic
COPY app ./app

# Data (SQLite database) lives in a volume, and the app runs as a normal user.
RUN useradd --create-home app && mkdir /data && chown app /data
USER app
ENV DATABASE_URL=sqlite+aiosqlite:////data/assistant.db
VOLUME /data

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/health', timeout=4).status == 200 else 1)"

# One process on purpose: the scheduler and Telegram poller live inside it (see docs/architecture.md).
CMD ["uvicorn", "app.main:app_factory", "--factory", "--host", "0.0.0.0", "--port", "8000"]
