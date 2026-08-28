import pytest

from brain.providers import OpenAICompatibleProvider
from conversation.cli import DEFAULT_CHAT_TEMPERATURE, build_chat_provider


def _environment(**extra: str) -> dict[str, str]:
    values = {
        "HIKARI_MODEL_BASE_URL": "https://example.invalid",
        "HIKARI_MODEL_NAME": "test-model",
        "HIKARI_MODEL_API_KEY": "test-key",
    }
    values.update(extra)
    return values


def test_direct_chat_uses_less_rigid_temperature_by_default():
    provider = build_chat_provider(_environment())

    assert isinstance(provider, OpenAICompatibleProvider)
    assert provider.temperature == DEFAULT_CHAT_TEMPERATURE == 0.65


def test_direct_chat_temperature_can_be_overridden():
    provider = build_chat_provider(
        _environment(HIKARI_CHAT_TEMPERATURE="0.8")
    )

    assert isinstance(provider, OpenAICompatibleProvider)
    assert provider.temperature == 0.8


@pytest.mark.parametrize("value", ["nope", "-0.1", "2.1"])
def test_invalid_direct_chat_temperature_is_rejected(value: str):
    with pytest.raises(ValueError, match="HIKARI_CHAT_TEMPERATURE"):
        build_chat_provider(_environment(HIKARI_CHAT_TEMPERATURE=value))
