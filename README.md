# Personal AI Assistant

This project provides a FastAPI-based autonomous personal assistant that ingests WhatsApp messages, stores task state in PostgreSQL, and schedules background work through Celery/Redis.

## Local development

1. Create a local environment file:
   ```bash
   copy .env.example .env
   ```
2. Start PostgreSQL and Redis:
   ```bash
   docker-compose up -d postgres redis
   ```
3. Install Python dependencies:
   ```bash
   python -m venv .venv
   .venv\Scripts\activate
   pip install -r requirements.txt
   ```
4. Run the app:
   ```bash
   uvicorn app.main:app --reload
   ```
5. Visit:
   - http://localhost:8000/health
   - http://localhost:8000/docs

## WhatsApp webhook verification

Use the following query parameters to verify the Meta subscription handshake:

```text
GET /api/v1/webhook/whatsapp?hub.mode=subscribe&hub.challenge=123&hub.verify_token=your-token
```

## Notes

The current scaffold intentionally focuses on a working project skeleton with validated webhook and schema logic while preserving the architecture needed for production extension.
