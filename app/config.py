# app/config.py
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # WhatsApp
    whatsapp_verify_token: str = ""
    whatsapp_access_token: str = ""
    whatsapp_phone_number_id: str = ""

    # Supabase
    supabase_url: str
    supabase_service_key: str

    # Redis
    UPSTASH_REDIS_REST_URL: str
    UPSTASH_REDIS_REST_TOKEN: str

    # LLM
    anthropic_api_key: str = ""
    openai_api_key: str = ""

    # Google
    google_service_account_json: str = ""

    # Observability
    logfire_token: str = ""

    # App
    sentry_dsn: str = ""
    log_level: str = "INFO"
    environment: str = "development"

    class Config:
        env_file = ".env"
        case_sensitive = False
        extra = "ignore"


settings = Settings()