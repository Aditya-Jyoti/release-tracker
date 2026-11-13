from pathlib import Path

import pytest

from bot.config import Config, ConfigError


def test_defaults():
    c = Config.from_env({"TELEGRAM_BOT_TOKEN": "t"})
    assert c.check_interval == 900 and c.github_token is None and c.allowed_chat_ids is None
    assert c.db_path == Path("data/tracker.db") and c.github_fallback_to_tags


def test_full():
    c = Config.from_env({
        "TELEGRAM_BOT_TOKEN": "t", "GITHUB_TOKEN": "g", "CHECK_INTERVAL_SECONDS": "300",
        "ALLOWED_CHAT_IDS": "123, -100456,", "GITHUB_FALLBACK_TO_TAGS": "no", "LOG_FORMAT": "text",
    })
    assert c.allowed_chat_ids == {123, -100456} and c.check_interval == 300
    assert c.github_token == "g" and not c.github_fallback_to_tags and c.log_format == "text"


@pytest.mark.parametrize("env", [
    {},
    {"TELEGRAM_BOT_TOKEN": "t", "CHECK_INTERVAL_SECONDS": "abc"},
    {"TELEGRAM_BOT_TOKEN": "t", "CHECK_INTERVAL_SECONDS": "5"},
    {"TELEGRAM_BOT_TOKEN": "t", "ALLOWED_CHAT_IDS": "1,x"},
    {"TELEGRAM_BOT_TOKEN": "t", "GITHUB_FALLBACK_TO_TAGS": "maybe"},
])
def test_invalid(env):
    with pytest.raises(ConfigError):
        Config.from_env(env)
