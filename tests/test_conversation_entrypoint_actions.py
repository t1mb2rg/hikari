from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from conversation import cli, remote
from conversation.bootstrap import build_private_task_router
from conversation.engine import ConversationEngine
from conversation.models import AssistantReply, UserTurn
from conversation.natural import NaturalConversationEngine
from memory.store import MemoryStore
from resident.telemetry import read_observation


class Provider:
    def __init__(self, replies=()):
        self.replies = iter(replies)
        self.calls = []

    def complete(self, messages):
        self.calls.append(messages)
        return next(self.replies)


@pytest.mark.parametrize("engine_type", [ConversationEngine, NaturalConversationEngine])
def test_claim_guard_runs_once_before_either_engine_persists_reply(tmp_path: Path, engine_type):
    provider = Provider(["I fixed the bug and all tests passed.", '{"supported":false}'])
    memory = MemoryStore(tmp_path / "memory.db")
    engine = engine_type(provider, memory)
    build_private_task_router(engine, repository=tmp_path, state_dir=tmp_path, values={"HIKARI_GITHUB_REPOSITORY": "test/entrypoint"})
    reply = engine.respond(UserTurn("cli", "local", "Let's discuss the fix"), source_ref="guard-receipt")
    assert "不能说已经开始或完成" in reply.text
    assert len(provider.calls) == 2
    events = memory.recent_events(10)
    assert len(events) == 2
    assert all("I fixed the bug" not in event.content for event in events)


def test_legacy_engine_guard_failure_does_not_persist_unguarded_reply(tmp_path: Path):
    memory = MemoryStore(tmp_path / "memory.db")
    engine = ConversationEngine(Provider(["Already completed"]), memory, response_guard=lambda *args: "")
    with pytest.raises(RuntimeError, match="response guard"):
        engine.respond(UserTurn("cli", "local", "discuss"))
    assert memory.recent_events(10) == []


def test_legacy_engine_persists_private_actor_for_future_handoff(tmp_path: Path):
    memory = MemoryStore(tmp_path / "memory.db")
    engine = ConversationEngine(Provider(["Plan noted"]), memory)
    engine.respond(UserTurn("cli", "local", "Only edit README", actor_id="local-user"))
    assert all(event.context["scope"] == "private" and event.context["actor_id"] == "local-user" for event in memory.recent_events(10))


@pytest.mark.parametrize("enabled", [False, True])
def test_cli_chat_and_paste_use_same_task_router_and_resolved_runtime(tmp_path: Path, monkeypatch, enabled):
    repository, state = tmp_path / "repository", tmp_path / "runtime"
    repository.mkdir()
    values = {"HIKARI_MODEL_NAME": "test-model", "HIKARI_ENGINEERING_ENABLED": str(enabled).lower(), "GH_TOKEN": "synthetic-env-file-value"}
    monkeypatch.setattr(cli, "load_runtime_environment", lambda **kwargs: SimpleNamespace(values=values, env_file=None))
    monkeypatch.setattr(cli, "build_chat_provider", lambda _: Provider())
    monkeypatch.setattr(cli, "build_user_model_runtime", lambda *args: (None, None))
    calls = []
    built = {}

    class Router:
        def respond(self, engine, turn, *, source_ref=None):
            calls.append((turn, source_ref))
            return AssistantReply(turn.channel, turn.conversation_id, "accepted by fake service")

    def build(engine, **kwargs):
        built.update(kwargs)
        built["engine"] = engine
        return Router()

    monkeypatch.setattr(cli, "build_private_task_router", build)
    inputs = iter(["normal chat", "/paste", "Only edit README", "Follow the agreed plan", "/send", "/exit"])
    monkeypatch.setattr("builtins.input", lambda _: next(inputs))
    result = cli.main(["--repository", str(repository), "--state-dir", str(state), "--prompt-profile", "production"])
    assert result == 0
    assert [turn.text for turn, _ in calls] == ["normal chat", "Only edit README\nFollow the agreed plan"]
    assert len({source for _, source in calls}) == 2
    assert all(source.startswith("cli:") for _, source in calls)
    assert built["repository"] == repository
    assert built["state_dir"] == state
    assert built["values"] == values
    assert type(built["engine"]) is ConversationEngine
    if enabled:
        assert built["engineering_bridge"].repository == repository
        assert built["engineering_bridge"].store.root == state / "engineering"
        assert built["engineering_bridge"].store.list_states() == []
    else:
        assert built["engineering_bridge"] is None


@pytest.mark.parametrize("enabled", [False, True])
def test_standalone_main_wires_durable_task_layer_without_starting_worker(tmp_path: Path, monkeypatch, enabled):
    repository, state = tmp_path / "repository", tmp_path / "runtime"
    repository.mkdir()
    values = {"HIKARI_ENGINEERING_ENABLED": str(enabled).lower(), "HIKARI_MODEL_NAME": "test-model", "GH_TOKEN": "synthetic-runtime-only"}
    monkeypatch.setattr(remote, "load_runtime_environment", lambda **kwargs: SimpleNamespace(values=values, env_file=None))
    monkeypatch.setattr(remote, "build_chat_provider", lambda _: Provider())
    monkeypatch.setattr(remote, "build_user_model_runtime", lambda *args: (None, None))
    captured = {}

    class Router:
        def respond(self, engine, turn, *, source_ref=None):
            captured["request"] = (turn, source_ref)
            return AssistantReply(turn.channel, turn.conversation_id, "source-linked result")

    router = Router()

    def build(engine, **kwargs):
        captured.update(kwargs)
        captured["engine"] = engine
        return router

    async def run_host(host, **kwargs):
        captured["host_options"] = kwargs
        assert host.processor.action_bridge is router
        assert host.processor.receipts.path == state / "conversation_receipts.db"
        reply, duplicate = host.processor.process("transport-receipt", UserTurn("qq", "private:42", "Execute the discussed plan", actor_id="42"))
        assert reply.text == "source-linked result" and not duplicate

    monkeypatch.setattr(remote, "build_private_task_router", build)
    monkeypatch.setattr(remote, "_run_host", run_host)
    assert remote.main(["--repository", str(repository), "--state-dir", str(state)]) == 0
    assert captured["values"] == values
    assert captured["repository"] == repository
    assert captured["state_dir"] == state
    assert captured["request"][1] == "transport-receipt"
    assert captured["host_options"]["engineering_enabled"] is enabled
    assert captured["engine"].provider.state_dir == state
    if enabled:
        assert captured["engineering_bridge"].store.root == state / "engineering"
        assert captured["engineering_bridge"].store.list_states() == []
    else:
        assert captured["engineering_bridge"] is None


def test_explicit_memory_path_keeps_related_state_out_of_real_default_directory(tmp_path: Path, monkeypatch):
    checkout = tmp_path / "checkout"
    monkeypatch.setattr(cli, "source_checkout_root", lambda: checkout)
    args = SimpleNamespace(repository=None, state_dir=None, db=str(tmp_path / "custom" / "memory.db"))
    repository, state, memory = cli._entrypoint_paths(args, {"LOCALAPPDATA": str(tmp_path / "ignored")})
    assert repository == checkout
    assert state == tmp_path / "custom"
    assert memory == state / "memory.db"
    args.db = None
    _, state, _ = cli._entrypoint_paths(args, {"LOCALAPPDATA": str(tmp_path / "resolved-local")})
    assert state == tmp_path / "resolved-local" / "Hikari" / "resident"


def test_standalone_listener_observation_is_fresh_and_stops_after_socket_close(tmp_path: Path, monkeypatch):
    observed = []
    lifecycle = []
    original_record = remote.record_observation

    def record(root, component, status, **details):
        observed.append(status)
        lifecycle.append(status)
        return original_record(root, component, status, **details)

    class Server:
        sockets = [SimpleNamespace(getsockname=lambda: ("127.0.0.1", 43210))]

        async def serve_forever(self):
            while observed.count("healthy") < 2:
                await asyncio.sleep(0.001)
            snapshot = read_observation(tmp_path, "conversation")
            assert snapshot["details"]["port"] == 43210
            assert snapshot["details"]["engineering_enabled"] is False

    class Listener:
        async def __aenter__(self):
            lifecycle.append("bound")
            return Server()

        async def __aexit__(self, *args):
            lifecycle.append("closed")

    monkeypatch.setattr(remote, "record_observation", record)
    monkeypatch.setattr(remote, "serve", lambda *args, **kwargs: Listener())
    monkeypatch.setattr(remote, "CONVERSATION_OBSERVATION_INTERVAL_SECONDS", 0.001)
    asyncio.run(remote._run_host(SimpleNamespace(handle=None), bind_host="127.0.0.1", bind_port=0, state_dir=tmp_path))
    assert observed.count("healthy") >= 2
    assert lifecycle.index("bound") < lifecycle.index("healthy")
    assert lifecycle[-2:] == ["closed", "offline"]
    assert read_observation(tmp_path, "conversation")["status"] == "offline"


def test_failed_listener_bind_does_not_publish_a_fake_healthy_or_offline_observation(tmp_path: Path, monkeypatch):
    class FailedListener:
        async def __aenter__(self):
            raise OSError("port already owned")

        async def __aexit__(self, *args):
            return False

    monkeypatch.setattr(remote, "serve", lambda *args, **kwargs: FailedListener())
    with pytest.raises(OSError):
        asyncio.run(remote._run_host(SimpleNamespace(handle=None), bind_host="127.0.0.1", bind_port=8765, state_dir=tmp_path))
    assert read_observation(tmp_path, "conversation") is None
