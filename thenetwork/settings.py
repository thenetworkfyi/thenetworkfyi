from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Database
    database_url: str = "postgresql+psycopg://network:network@localhost:5432/network_db"

    # LLM — provider selected by config string (no vendor lock-in, no LiteLLM)
    agent_model: str = "anthropic:claude-sonnet-4-6"
    embed_model: str = "text-embedding-3-small"

    # API keys (only the ones in use need to be set)
    openai_api_key: str = ""
    anthropic_api_key: str = ""

    # Email
    email_account: str = ""
    email_password: str = ""
    imap_host: str = "imap.gmail.com"
    imap_port: int = 993
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587

    # Optional content scanner
    content_scan_enabled: bool = False

    # Procrastinate worker concurrency (global LLM-spend ceiling)
    worker_concurrency: int = 4

    # Rate limiting: max emails per sender per hour
    rate_limit_per_hour: int = 10


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
