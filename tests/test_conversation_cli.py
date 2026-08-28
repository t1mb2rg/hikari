import pytest

from brain.providers import OpenAICompatibleProvider
from conversation.cli import (
    DEFAULT_CHAT_TEMPERATURE,
    build_chat_provider,
    collect_multiline_turn,
)


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


class FakeInput:
    def __init__(self, lines: list[str]) -> None:
        self.lines = iter(lines)
        self.prompts: list[str] = []

    def __call__(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return next(self.lines)


def test_multiline_paste_returns_one_turn_with_blank_lines_preserved():
    reader = FakeInput(
        [
            "这是旧记录：",
            "你> 你好 hikari",
            "Hikari> 你好。",
            "",
            "你> 你记得我吗",
            "/send",
        ]
    )
    output: list[str] = []

    text = collect_multiline_turn(input_fn=reader, output_fn=output.append)

    assert text == (
        "这是旧记录：\n"
        "你> 你好 hikari\n"
        "Hikari> 你好。\n"
        "\n"
        "你> 你记得我吗"
    )
    assert len(reader.prompts) == 6
    assert all(prompt == "│ " for prompt in reader.prompts)
    assert output == ["多行粘贴模式：粘贴完成后单独输入 /send 发送，/cancel 取消。"]


def test_multiline_paste_cancel_discards_buffer():
    reader = FakeInput(["不会发送", "/cancel"])
    output: list[str] = []

    text = collect_multiline_turn(input_fn=reader, output_fn=output.append)

    assert text is None
    assert output[-1] == "已取消多行粘贴。"


def test_multiline_paste_empty_send_produces_no_turn():
    reader = FakeInput(["", "   ", "/send"])
    output: list[str] = []

    text = collect_multiline_turn(input_fn=reader, output_fn=output.append)

    assert text is None
    assert output[-1] == "没有可发送的内容，已退出多行粘贴模式。"
