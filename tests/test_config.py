import pytest

from kimi_agent_spike.config import Settings


def test_live_mode_requires_api_key(monkeypatch):
    monkeypatch.delenv("KIMI_API_KEY", raising=False)
    with pytest.raises(ValueError, match="KIMI_API_KEY"):
        Settings.from_env(require_api_key=True)


def test_invalid_limits_are_rejected(monkeypatch):
    monkeypatch.setenv("AGENT_MAX_TURNS", "0")
    with pytest.raises(ValueError, match="AGENT_MAX_TURNS"):
        Settings.from_env(require_api_key=False)


def test_defaults_can_be_loaded_without_live_key(monkeypatch):
    for name in (
        "KIMI_API_KEY",
        "KIMI_BASE_URL",
        "KIMI_MODEL",
        "AGENT_TIMEOUT_SECONDS",
        "AGENT_MAX_TURNS",
        "TRACE_PATH",
    ):
        monkeypatch.delenv(name, raising=False)
    settings = Settings.from_env(require_api_key=False)
    assert settings.base_url == "https://api.moonshot.cn/v1"
    assert settings.model == "kimi-k2.6"
    assert settings.max_turns == 6
