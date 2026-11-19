from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    jobs_root: Path
    consumer_token: SecretStr
    host: str = "0.0.0.0"
    port: int = 8100
    max_body_bytes: int = 1_048_576
    log_dir: Path = Path("./logs")
