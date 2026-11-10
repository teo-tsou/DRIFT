"""
Application Configuration
"""

from pydantic_settings import BaseSettings
from typing import Optional


class Settings(BaseSettings):
    """Application settings loaded from environment variables"""

    # Application
    app_name: str = "DRIFT Platform"
    app_version: str = "0.1.0"
    debug: bool = False

    # API
    api_prefix: str = "/api/v1"
    allowed_origins: list = ["*"]  # CORS - restrict in production

    # Database
    database_url: str = ""
    sql_echo: bool = False

    # Background Tasks (future)
    # redis_url: str = "redis://localhost:6379/0"
    # celery_broker_url: str = "redis://localhost:6379/0"

    # Authentication (future)
    secret_key: str = "change-me-in-production"
    algorithm: str = "HS256"
    access_token_expire_minutes: int = 30

    class Config:
        env_file = ".env"
        case_sensitive = False


# Global settings instance
settings = Settings()
