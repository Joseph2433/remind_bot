from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from lark_bot.models import ReceiveIdType


class ConfigCheck(BaseModel):
    name: str
    ok: bool
    message: str


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="LARK_BOT_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    lark_app_id: str = ""
    lark_app_secret: str = ""
    lark_receive_id_type: ReceiveIdType = "chat_id"
    lark_receive_id: str = ""
    sqlite_path: Path = Path(".lark-bot/lark_bot.sqlite3")
    cooldown_seconds: int = Field(default=300, ge=0)
    output_tail_lines: int = Field(default=40, ge=1)
    http_timeout_seconds: float = Field(default=10.0, gt=0)
    log_level: str = "INFO"
    lark_base_url: str = "https://open.feishu.cn"


def get_settings() -> Settings:
    return Settings()


def build_config_checks(settings: Settings) -> list[ConfigCheck]:
    checks = [
        _required_check("lark_app_id", settings.lark_app_id, "LARK_BOT_LARK_APP_ID is required."),
        _required_check(
            "lark_app_secret",
            settings.lark_app_secret,
            "LARK_BOT_LARK_APP_SECRET is required.",
        ),
        _required_check(
            "lark_receive_id",
            settings.lark_receive_id,
            "LARK_BOT_LARK_RECEIVE_ID is required.",
        ),
        ConfigCheck(
            name="lark_receive_id_type",
            ok=settings.lark_receive_id_type in {"chat_id", "user_id", "open_id"},
            message=f"receive_id_type={settings.lark_receive_id_type}",
        ),
        ConfigCheck(
            name="sqlite_path",
            ok=bool(settings.sqlite_path),
            message=f"sqlite_path={settings.sqlite_path}",
        ),
    ]
    return checks


def public_settings_summary(settings: Settings) -> dict[str, str | int | float]:
    return {
        "lark_app_id": settings.lark_app_id or "[missing]",
        "lark_app_secret": "[set]" if settings.lark_app_secret else "[missing]",
        "lark_receive_id_type": settings.lark_receive_id_type,
        "lark_receive_id": settings.lark_receive_id or "[missing]",
        "sqlite_path": str(settings.sqlite_path),
        "cooldown_seconds": settings.cooldown_seconds,
        "output_tail_lines": settings.output_tail_lines,
        "http_timeout_seconds": settings.http_timeout_seconds,
        "log_level": settings.log_level,
        "lark_base_url": settings.lark_base_url,
    }


def _required_check(name: str, value: str, message: str) -> ConfigCheck:
    return ConfigCheck(
        name=name,
        ok=bool(value),
        message=f"{name} is set." if value else message,
    )
