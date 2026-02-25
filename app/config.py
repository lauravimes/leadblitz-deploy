from pydantic_settings import BaseSettings
from functools import lru_cache


class Settings(BaseSettings):
    database_url: str = "postgresql://user:pass@localhost:5432/leadblitz_v2"
    session_secret: str = "change-me"
    google_maps_api_key: str = ""
    openai_api_key: str = ""

    # Encryption
    encryption_key: str = ""

    # Stripe
    stripe_secret_key: str = ""
    stripe_publishable_key: str = ""
    stripe_webhook_secret: str = ""

    # System email (SMTP)
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from_email: str = "noreply@leadblitz.co"

    # Email enrichment
    hunter_api_key: str = ""

    # Gmail OAuth
    gmail_client_id: str = ""
    gmail_client_secret: str = ""

    # Outlook OAuth
    outlook_client_id: str = ""
    outlook_client_secret: str = ""

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


@lru_cache
def get_settings() -> Settings:
    return Settings()
