from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from lack_bot.models import ReceiveIdType


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="LACK_BOT_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    lark_app_id: str = ""
    lark_app_secret: str = ""
    lark_receive_id_type: ReceiveIdType = "chat_id"
    lark_receive_id: str = ""
    sqlite_path: Path = Path(".lack-bot/lack_bot.sqlite3")
    cooldown_seconds: int = Field(default=300, ge=0)
    output_tail_lines: int = Field(default=40, ge=1)
    http_timeout_seconds: float = Field(default=10.0, gt=0)
    log_level: str = "INFO"
    lark_base_url: str = "https://open.feishu.cn"


def get_settings() -> Settings:
    return Settings()
