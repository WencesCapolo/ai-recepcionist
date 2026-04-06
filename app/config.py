# app/config.py
from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # WhatsApp
    whatsapp_verify_token: SecretStr
    whatsapp_access_token: SecretStr
    whatsapp_phone_number_id: str = ""

    # Supabase
    supabase_url: str = ""
    supabase_service_key: SecretStr

    # Redis
    UPSTASH_REDIS_REST_URL: str = ""
    UPSTASH_REDIS_REST_TOKEN: SecretStr

    # LLM
    anthropic_api_key: SecretStr
    openai_api_key: SecretStr

    # Google
    google_service_account_json: str = ""

    @model_validator(mode="after")
    def validate_upstash(self):
        if not self.UPSTASH_REDIS_REST_URL or not self.UPSTASH_REDIS_REST_TOKEN.get_secret_value():
            raise ValueError("UPSTASH_REDIS_REST_URL and UPSTASH_REDIS_REST_TOKEN must be set")
        return self

    # Observability
    logfire_token: str = ""

    # App
    sentry_dsn: str = ""
    log_level: str = "INFO"
    environment: str = "development"

    model_config = SettingsConfigDict(
        env_file=".env",
        case_sensitive=False,
        extra="ignore",
        populate_by_name=True,
    )


settings = Settings()