from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from attention import AttentionPolicy
from brain import SimpleReasoner
from brain.model_reasoner import ChatMessage
from conversation.engine import ConversationEngine
from conversation.models import UserTurn
from conversation.receipts import ConversationReceiptStore
from conversation.remote import ConversationRequestProcessor, ConversationWebSocketHost
from core.presence import ConsoleFeedbackSink, PresencePipeline
from core.runtime import ResidentPresenceRuntime
from integrations.qq_bridge.core_client import ConversationCoreClient
from memory.store import MemoryStore
from resident.unified import (
    QQBridgeProcessConfig,
    QQBridgeSupervisor,
    UnifiedResidentService,
    runtime_bool,
)


class FakeProvider:
    def __init__(self, reply: str = "resident ok") -> None:
        self.reply = reply
        self.calls: list[list[ChatMessage]] = []

    def complete(self, messages: list[ChatMessage]) -> str:
        self.calls.append(list(messages))
        return self.reply


def _presence(tmp_path: Path) -> ResidentPresenceRuntime:
    pipeline = PresencePipeline(
        memory=MemoryStore(tmp_path / "memory.db"),
        attention=AttentionPolicy(threshold=1.0),
        reasoner=SimpleReasoner(),
        feedback_sink=ConsoleFeedbackSink(),
    )
    return ResidentPresenceRuntime([], pipeline, poll_interval=0.01)


def _conversation_host(tmp_path: Path, provider: FakeProvider) -> ConversationWebSocketHost:
    engine = ConversationEngine(provider, MemoryStore(tmp_path / "memory.db"))
    return ConversationWebSocketHost(
        ConversationRequestProcessor(
            engine,
            ConversationReceiptStore(tmp_path / "receipts.db"),
        ),
        shared_secret="resident-secret",
    )


def test_runtime_bool_is_explicit_and_fail_closed():
    assert runtime_bool({}, "FLAG", default=True) is True
    assert runtime_bool({"FLAG": "off"}, "FLAG", default=True) is False
    assert runtime_bool({"FLAG": "YES"}, "FLAG", default=False) is True

    with pytest.raises(ValueError, match="FLAG"):
        runtime_bool({"FLAG": "maybe"}, "FLAG", default=False)


def test_qq_bridge_process_command_contains_no_secret_values(tmp_path: Path):
    repository = tmp_path / "repo"
    repository.mkdir()
    env_file = tmp_path / ".env"
    env_file.write_text("HIKARI_MODEL_API_KEY=secret\n", encoding="utf-8")
    config = QQBridgeProcessConfig(
        repository=repository,
        log_path=tmp_path / "qq.log",
        environment={"HIKARI_MODEL_API_KEY": "secret"},
        env_file=env_file,
        python_executable="python-test",
    )

    assert config.argv() == [
        "python-test",
        "-m",
        "integrations.qq_bridge.app",
        "--env-file",
        str(env_file.resolve()),
    ]
    assert "secret" not in " ".join(config.argv())


def test_qq_bridge_supervisor_starts_only_hikari_bridge_child(tmp_path: Path):
    repository = tmp_path / "repo"
    repository.mkdir()
    captured: list[tuple[list[str], dict[str, object]]] = []

    class FakeProcess:
        pid = 4040

        def poll(self):
            return None

        def terminate(self):
            return None

        def kill(self):
            return None

        def wait(self):
            return 0

    def factory(argv, **kwargs):
        captured.append((list(argv), dict(kwargs)))
        return FakeProcess()

    supervisor = QQBridgeSupervisor(
        QQBridgeProcessConfig(
            repository=repository,
            log_path=tmp_path / "qq.log",
            environment={"SAFE": "1"},
            python_executable="python-test",
        ),
        process_factory=factory,  # type: ignore[arg-type]
    )

    process = supervisor.start_once()

    assert process.pid == 4040
    assert captured[0][0][:3] == [
        "python-test",
        "-m",
        "integrations.qq_bridge.app",
    ]
    assert captured[0][1]["cwd"] == repository.resolve()
    assert captured[0][1]["env"] == {"SAFE": "1"}
    assert all("napcat" not in part.lower() for part in captured[0][0])


def test_qq_bridge_supervisor_restarts_crashed_child_without_touching_core(tmp_path: Path):
    async def scenario() -> None:
        repository = tmp_path / "repo"
        repository.mkdir()
        created: list[FakeProcess] = []

        class FakeProcess:
            def __init__(self, pid: int, *, exited: bool) -> None:
                self.pid = pid
                self.returncode: int | None = 1 if exited else None
                self.terminated = False

            def poll(self):
                return self.returncode

            def terminate(self):
                self.terminated = True
                self.returncode = 0

            def kill(self):
                self.returncode = -9

            def wait(self):
                if self.returncode is None:
                    self.returncode = 0
                return self.returncode

        def factory(argv, **kwargs):
            process = FakeProcess(
                5000 + len(created),
                exited=len(created) == 0,
            )
            created.append(process)
            return process

        supervisor = QQBridgeSupervisor(
            QQBridgeProcessConfig(
                repository=repository,
                log_path=tmp_path / "qq.log",
                environment={},
                python_executable="python-test",
                restart_initial_seconds=0.01,
                restart_max_seconds=0.02,
                stable_reset_seconds=0.02,
            ),
            process_factory=factory,  # type: ignore[arg-type]
        )
        stop = asyncio.Event()
        task = asyncio.create_task(supervisor.run(stop))

        for _ in range(100):
            if len(created) >= 2:
                break
            await asyncio.sleep(0.01)

        assert len(created) >= 2
        assert supervisor.restart_count >= 1
        assert created[0].returncode == 1
        assert created[1].poll() is None

        stop.set()
        await asyncio.wait_for(task, timeout=2.0)
        assert created[1].terminated is True

    asyncio.run(scenario())


def test_unified_resident_serves_conversation_while_presence_is_running(tmp_path: Path):
    async def scenario() -> None:
        provider = FakeProvider()
        presence = _presence(tmp_path)
        service = UnifiedResidentService(
            presence,
            _conversation_host(tmp_path, provider),
            bind_host="127.0.0.1",
            bind_port=0,
        )

        task = asyncio.create_task(service.run())
        await asyncio.wait_for(service.started_event.wait(), timeout=2.0)
        assert service.bound_port is not None
        assert service.bound_port > 0
        assert presence.running is True

        client = ConversationCoreClient(
            f"ws://127.0.0.1:{service.bound_port}",
            adapter_id="qq.test",
            channel="qq",
            shared_secret="resident-secret",
        )
        try:
            reply = await client.request(
                "qq:resident:1",
                UserTurn("qq", "private:7", "hello resident"),
            )
        finally:
            await client.close()

        assert reply.text == "resident ok"
        assert len(provider.calls) == 1

        service.stop()
        await asyncio.wait_for(task, timeout=2.0)
        assert presence.running is False

    asyncio.run(scenario())
