from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    APP_ENV: str = "development"
    SECRET_KEY: str = "dev-secret-key"
    DATABASE_URL: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/personal_ai"
    REDIS_URL: str = "redis://localhost:6379/0"
    WHATSAPP_VERIFY_TOKEN: str = "secret-token"
    META_APP_SECRET: str = "test-secret"
    WHATSAPP_ACCESS_TOKEN: str = "dev-access-token"
    PHONE_NUMBER_ID: str = "1234567890"
    OPENAI_API_KEY: str = ""
    OPENAI_MODEL: str = "gpt-4o-mini"
    LLM_PROVIDER: str = "openai"
    GEMINI_API_KEY: str = ""
    GEMINI_MODEL: str = "gemini-1.5-flash"
    POSTGRES_DB: str = "personal_ai"
    POSTGRES_USER: str = "postgres"
    POSTGRES_PASSWORD: str = "postgres"
    GOOGLE_CLIENT_ID: str = ""
    GOOGLE_CLIENT_SECRET: str = ""
    GOOGLE_REDIRECT_URI: str = "http://localhost:8000/auth/google/callback"
    GOOGLE_CALENDAR_ID: str = "primary"

    model_config = SettingsConfigDict(
        env_file=".env",
        case_sensitive=False,
        extra="ignore",
    )


settings = Settings()
