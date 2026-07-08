"""設定ロード。`.env` / 環境変数から読み込む（pydantic-settings）。

カンマ区切りの一覧は文字列で受け取り、`*_set` プロパティで集合に変換する
（pydantic の complex-type パースを避け、空文字を安全に扱うため）。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---- Slack ----
    slack_bot_token: str = ""
    slack_app_token: str = ""
    slack_watch_channels: str = ""
    slack_allowed_users: str = ""

    # ---- Discord ----
    discord_bot_token: str = ""
    discord_watch_channels: str = ""
    discord_allowed_users: str = ""

    # ---- 翻訳（pdf2zh-next / OpenAI互換）----
    translate_base_url: str = "http://localhost:8000/v1"
    translate_api_key: str = "dummy"
    translate_model: str = ""
    lang_in: str = "auto"
    lang_out: str = "ja"

    # ---- 要約（OpenAI互換）----
    summary_base_url: str = "http://localhost:11434/v1"
    summary_api_key: str = "dummy"
    summary_model: str = ""

    # ---- 動作 ----
    max_concurrency: int = 1
    max_pdf_mb: int = 50
    file_retention_days: int = 7
    work_dir: Path = Path("./work")
    log_level: str = "INFO"

    # ------------------------------------------------------------------
    @staticmethod
    def _split(value: str) -> set[str]:
        return {item.strip() for item in value.split(",") if item.strip()}

    @property
    def slack_enabled(self) -> bool:
        return bool(self.slack_bot_token and self.slack_app_token)

    @property
    def discord_enabled(self) -> bool:
        return bool(self.discord_bot_token)

    @property
    def slack_watch_channel_set(self) -> set[str]:
        return self._split(self.slack_watch_channels)

    @property
    def slack_allowed_user_set(self) -> set[str]:
        return self._split(self.slack_allowed_users)

    @property
    def discord_watch_channel_set(self) -> set[str]:
        return self._split(self.discord_watch_channels)

    @property
    def discord_allowed_user_set(self) -> set[str]:
        return self._split(self.discord_allowed_users)

    @property
    def max_pdf_bytes(self) -> int:
        return self.max_pdf_mb * 1024 * 1024


@lru_cache
def get_settings() -> Settings:
    """プロセス内で単一の Settings を返す。"""
    return Settings()
