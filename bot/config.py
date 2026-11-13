"""Configuration from environment variables."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


class ConfigError(Exception):
    pass


def _bool(name: str, value: str) -> bool:
    v = value.strip().lower()
    if v in {"1", "true", "yes", "on"}:
        return True
    if v in {"0", "false", "no", "off", ""}:
        return False
    raise ConfigError(f"{name} must be a boolean, got {value!r}")


@dataclass(frozen=True)
class Config:
    telegram_token: str
    github_token: str | None = None
    check_interval: int = 900
    db_path: Path = Path("data/tracker.db")
    allowed_chat_ids: frozenset[int] | None = None
    github_fallback_to_tags: bool = True
    log_level: str = "INFO"
    log_format: str = "json"

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Config:
        env = os.environ if env is None else env

        token = env.get("TELEGRAM_BOT_TOKEN", "").strip()
        if not token:
            raise ConfigError("TELEGRAM_BOT_TOKEN is required")

        raw_interval = env.get("CHECK_INTERVAL_SECONDS", "900").strip()
        try:
            interval = int(raw_interval)
        except ValueError:
            raise ConfigError(f"CHECK_INTERVAL_SECONDS must be an integer, got {raw_interval!r}") from None
        if interval < 60:
            raise ConfigError("CHECK_INTERVAL_SECONDS must be >= 60")

        allowed: frozenset[int] | None = None
        raw_allowed = env.get("ALLOWED_CHAT_IDS", "").strip()
        if raw_allowed:
            try:
                allowed = frozenset(int(x) for x in raw_allowed.replace(" ", "").split(",") if x)
            except ValueError:
                raise ConfigError(f"ALLOWED_CHAT_IDS must be comma-separated integers, got {raw_allowed!r}") from None

        log_format = env.get("LOG_FORMAT", "json").strip().lower()
        if log_format not in {"json", "text"}:
            raise ConfigError("LOG_FORMAT must be 'json' or 'text'")

        return cls(
            telegram_token=token,
            github_token=env.get("GITHUB_TOKEN", "").strip() or None,
            check_interval=interval,
            db_path=Path(env.get("DB_PATH", "data/tracker.db").strip()),
            allowed_chat_ids=allowed,
            github_fallback_to_tags=_bool("GITHUB_FALLBACK_TO_TAGS", env.get("GITHUB_FALLBACK_TO_TAGS", "true")),
            log_level=env.get("LOG_LEVEL", "INFO").strip().upper(),
            log_format=log_format,
        )
