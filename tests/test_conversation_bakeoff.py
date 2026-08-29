from __future__ import annotations

import json
from pathlib import Path
from urllib.error import HTTPError, URLError

import pytest

from brain.model_reasoner import ChatMessage
from conversation import bakeoff
from conversation.bakeoff import RetryingProvider, run_bakeoff
from conversation.models import AssistantReply, UserTurn


class FlakyProvider:
    def __init__(self, failures: int, reply: str = "ok") -> None:
        self.failures = failures
        self.reply = reply
        self.calls = 0

    def complete(self, messages):
        self.calls += 1
        if self.calls <= self.failures:
            raise URLError("temporary tls failure")
        return self.reply


class HttpFailureProvider:
    def __init__(self) -> None:
        self.calls = 0

    def complete(self, messages):
        self.calls += 1
        raise HTTPError("https://example.invalid", 401, "Unauthorized", {}, None)


class FakeEngine:
    def __init__(self, label: str) -> None:
        self.label = label
        self.turns: list[UserTurn] = []

    def respond(self, turn: UserTurn) -> AssistantReply:
        self.turns.append(turn)
        return AssistantReply(
            channel=turn.channel,
            conversation_id=turn.conversation_id,
            text=f"reply-from-{self.label}-{len(self.turns)}",
        )


def test_retrying_provider_retries_transient_transport_failure(monkeypatch):
    monkeypatch.setattr(bakeoff.time, "sleep", lambda _: None)
    inner = FlakyProvider(failures=2, reply="通过")
    provider = RetryingProvider(inner, attempts=3, delay=0)

    result = provider.complete([ChatMessage(role="user", content="hi")])

    assert result == "通过"
    assert inner.calls == 3


def test_retrying_provider_does_not_retry_http_auth_failure():
    inner = HttpFailureProvider()
    provider = RetryingProvider(inner, attempts=3, delay=0)

    with pytest.raises(HTTPError):
        provider.complete([ChatMessage(role="user", content="hi")])

    assert inner.calls == 1


def test_bakeoff_requires_exactly_two_candidates(tmp_path: Path):
    env = tmp_path / "one.env"
    env.write_text("x=1\n", encoding="utf-8")

    with pytest.raises(ValueError, match="exactly two"):
        run_bakeoff([env], output_root=tmp_path / "runs", turns=["hi"])


def test_bakeoff_hides_models_in_transcript_and_writes_separate_reveal(
    tmp_path: Path,
    monkeypatch,
):
    first = tmp_path / "first.env"
    second = tmp_path / "second.env"
    first.write_text("secret-one\n", encoding="utf-8")
    second.write_text("secret-two\n", encoding="utf-8")

    built: list[tuple[str, Path]] = []

    def fake_build_candidate(env_file: Path, memory_path: Path):
        if env_file == first:
            model, reply_label = "model-31", "first"
        else:
            model, reply_label = "model-26", "second"
        built.append((model, memory_path))
        return FakeEngine(reply_label), model

    monkeypatch.setattr(bakeoff, "_build_candidate", fake_build_candidate)

    run_dir = run_bakeoff(
        [first, second],
        output_root=tmp_path / "runs",
        turns=["hikari", "你知道 Forge 吗？"],
        shuffle=False,
    )

    transcript = (run_dir / "transcript.txt").read_text(encoding="utf-8")
    reveal = json.loads((run_dir / "reveal.json").read_text(encoding="utf-8"))

    assert "model-31" not in transcript
    assert "model-26" not in transcript
    assert str(first) not in transcript
    assert str(second) not in transcript
    assert "A> reply-from-first-1" in transcript
    assert "B> reply-from-second-2" in transcript
    assert reveal["A"]["model"] == "model-31"
    assert reveal["B"]["model"] == "model-26"
    assert reveal["A"]["env_file"] == str(first)
    assert reveal["B"]["env_file"] == str(second)
    assert built[0][1] != built[1][1]
    assert built[0][1].name == "candidate-A.db"
    assert built[1][1].name == "candidate-B.db"
