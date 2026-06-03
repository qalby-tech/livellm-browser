from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    # Browser page limits
    max_pages_per_browser: int = 50

    model_config = {"env_prefix": "", "case_sensitive": False}


settings = Settings()