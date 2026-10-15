from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_SECRET = "dev-secret-key"


class Settings(BaseSettings):
    APP_ENV: str = "development"
    SECRET_KEY: str = DEFAULT_SECRET
    # Required outside development. Sent by clients as the X-API-Key header.
    API_KEY: str = ""

    # SQLite works out of the box; Postgres: postgresql+asyncpg://user:pw@host/db
    DATABASE_URL: str = "sqlite+aiosqlite:///./assistant.db"
    # Create tables on startup. Turn off when you manage the schema with Alembic.
    AUTO_CREATE_TABLES: bool = True

    # Default timezone until you change it (via chat or PUT /api/v1/preferences).
    TIMEZONE: str = "UTC"

    LLM_PROVIDER: str = "gemini"  # gemini | openai
    GEMINI_API_KEY: str = ""
    GEMINI_MODEL: str = "gemini-2.5-flash"
    OPENAI_API_KEY: str = ""
    OPENAI_MODEL: str = "gpt-4o-mini"
    # Reply with the tool's own confirmation instead of asking the LLM to phrase it: half the LLM calls per message.
    # Turn off for chattier replies if your API quota allows it.
    FAST_REPLIES: bool = True

    TELEGRAM_BOT_TOKEN: str = ""
    TELEGRAM_CHAT_ID: str = ""  # the only chat the bot will talk to
    TELEGRAM_MODE: str = "polling"  # polling | webhook
    TELEGRAM_WEBHOOK_SECRET: str = ""

    GOOGLE_CLIENT_ID: str = ""
    GOOGLE_CLIENT_SECRET: str = ""
    GOOGLE_REDIRECT_URI: str = "http://localhost:8000/auth/google/callback"
    # Calendar that receives planned blocks/events. "primary" writes to your main calendar.
    GOOGLE_CALENDAR_NAME: str = "Assistant"

    # Web Push (notifications even when the tab is closed). Push services want a contact: mailto: or https: URL.
    WEB_PUSH_ENABLED: bool = True
    VAPID_SUBJECT: str = "mailto:assistant@example.com"

    SCHEDULER_ENABLED: bool = True
    SCHEDULER_TICK_SECONDS: int = 30

    model_config = SettingsConfigDict(env_file=".env", case_sensitive=False, extra="ignore")

    @property
    def is_dev(self) -> bool:
        return self.APP_ENV.lower() in {"development", "dev", "test"}

    def validate_for_runtime(self) -> None:
        if self.is_dev:
            return
        if not self.API_KEY:
            raise RuntimeError("API_KEY must be set when APP_ENV is not 'development'.")
        if self.SECRET_KEY == DEFAULT_SECRET:
            raise RuntimeError("SECRET_KEY must be changed when APP_ENV is not 'development'.")


@lru_cache
def get_settings() -> Settings:
    return Settings()
