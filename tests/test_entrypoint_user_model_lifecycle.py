from __future__ import annotations

import asyncio
from pathlib import Path
import subprocess
import sys
import threading
from types import SimpleNamespace

import pytest

from brain.providers import OpenAICompatibleProvider
from conversation import cli, remote
from conversation.engine import ConversationEngine
from conversation.models import UserTurn
from memory.store import MemoryStore
from user_model import ModelUserFactExtractor, UserModelService, UserModelStore
from user_model.jobs import UserModelJobStore, UserModelJobWorker


def _queue(tmp_path, extractor):
    store = UserModelJobStore(tmp_path / "jobs.db")
    service = UserModelService(UserModelStore(tmp_path / "facts.db"))
    worker = UserModelJobWorker(store, extractor, service)
    return store, worker


class EmptyExtractor:
    def __init__(self):
        self.extracted = threading.Event()
        self.calls = 0

    def extract(self, **kwargs):
        self.calls += 1
        self.extracted.set()
        return []


def _enqueue(store):
    return store.enqueue(source_ref="source", turn=UserTurn("cli", "local", "hello"), recent_history=[])


def test_cli_owns_and_joins_background_drain(tmp_path: Path, monkeypatch):
    extractor = EmptyExtractor()
    store, worker = _queue(tmp_path, extractor)
    _enqueue(store)
    monkeypatch.setattr(cli, "USER_MODEL_DRAIN_INTERVAL_SECONDS", 0.001)
    with cli._cli_user_model_drain(SimpleNamespace(user_model_worker=worker)):
        assert extractor.extracted.wait(2)
        assert any(thread.name == "hikari-cli-user-model" for thread in threading.enumerate())
    assert store.get("source")["status"] == "completed"
    assert not any(thread.name == "hikari-cli-user-model" for thread in threading.enumerate())


def test_cli_idle_shutdown_leaves_durable_jobs_for_next_launch(tmp_path: Path, monkeypatch):
    extractor = EmptyExtractor()
    store, worker = _queue(tmp_path, extractor)
    _enqueue(store)
    monkeypatch.setattr(cli, "USER_MODEL_DRAIN_INTERVAL_SECONDS", 60)
    with cli._cli_user_model_drain(SimpleNamespace(user_model_worker=worker)):
        pass
    assert extractor.calls == 0
    assert store.get("source")["status"] == "queued"
    assert not any(thread.name == "hikari-cli-user-model" for thread in threading.enumerate())


def test_cli_main_returns_reply_before_background_extraction_finishes(tmp_path: Path, monkeypatch):
    started, release = threading.Event(), threading.Event()
    class BlockingExtractor:
        def extract(self, **kwargs):
            started.set()
            assert release.wait(3)
            return []
    store, worker = _queue(tmp_path, BlockingExtractor())
    captured = []
    class Chat:
        def complete(self, messages):
            return "Immediate chat reply"
    monkeypatch.setattr(cli, "load_runtime_environment", lambda **kwargs: SimpleNamespace(values={}, env_file=None))
    monkeypatch.setattr(cli, "build_chat_provider", lambda _: Chat())
    monkeypatch.setattr(cli, "build_user_model_runtime", lambda *args: (None, None))
    monkeypatch.setattr(cli, "USER_MODEL_DRAIN_INTERVAL_SECONDS", 0.001)
    class Router:
        user_model_worker = worker
        def respond(self, engine, turn, *, source_ref):
            return engine.respond(turn, source_ref=source_ref)
    def build(engine, **kwargs):
        engine.assimilation_sink = store
        return Router()
    monkeypatch.setattr(cli, "build_private_task_router", build)
    monkeypatch.setattr("builtins.print", lambda *args, **kwargs: captured.append(" ".join(str(arg) for arg in args)))
    reads = 0
    def read(prompt):
        nonlocal reads
        reads += 1
        if reads == 1:
            return "hello"
        assert any("Immediate chat reply" in line for line in captured)
        assert started.wait(2)
        release.set()
        return "/exit"
    monkeypatch.setattr("builtins.input", read)
    assert cli.main(["--repository", str(tmp_path), "--state-dir", str(tmp_path / "state"), "--prompt-profile", "production"]) == 0
    assert not any(thread.name == "hikari-cli-user-model" for thread in threading.enumerate())


def test_cli_worker_respects_another_process_drain_lock(tmp_path: Path, monkeypatch):
    extractor = EmptyExtractor()
    store, worker = _queue(tmp_path, extractor)
    _enqueue(store)
    polled = threading.Event()
    original = worker.drain_once
    def observed():
        result = original()
        if result["status"] == "busy":
            polled.set()
        return result
    worker.drain_once = observed
    monkeypatch.setattr(cli, "USER_MODEL_DRAIN_INTERVAL_SECONDS", 0.001)
    child_code = (
        "import sys\nfrom user_model.jobs import UserModelJobStore\n"
        "store = UserModelJobStore(sys.argv[1])\n"
        "with store.drain_lock() as acquired:\n"
        " print('locked' if acquired else 'busy', flush=True)\n"
        " sys.stdin.readline()\n"
    )
    process = subprocess.Popen([sys.executable, "-B", "-c", child_code, str(store.path)],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert process.stdout.readline().strip() == "locked"
        with cli._cli_user_model_drain(SimpleNamespace(user_model_worker=worker)):
            assert polled.wait(2)
            assert extractor.calls == 0
    finally:
        process.communicate("release\n", timeout=3)
    assert process.returncode == 0
    assert original()["status"] == "completed"
    assert extractor.calls == 1


def test_extraction_provider_timeout_is_bounded_without_mutating_chat_provider(tmp_path: Path):
    provider = OpenAICompatibleProvider(base_url="https://example.invalid", model="fake", timeout=30)
    _, worker = _queue(tmp_path, ModelUserFactExtractor(provider))
    bounded = cli._bounded_user_model_worker(worker)
    assert bounded is not worker
    assert bounded.store is worker.store and bounded.service is worker.service
    assert bounded.extractor.provider.timeout == cli.USER_MODEL_DRAIN_TIMEOUT_SECONDS
    assert provider.timeout == worker.extractor.provider.timeout == 30


def test_standalone_cancellation_waits_for_current_job_and_stops_without_backlog(tmp_path: Path, monkeypatch):
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    class Worker:
        calls = 0
        def drain_once(self):
            self.calls += 1
            started.set()
            assert release.wait(3)
            finished.set()
            return {"status": "completed"}
    worker = Worker()
    host = SimpleNamespace(handle=None, processor=SimpleNamespace(action_bridge=SimpleNamespace(user_model_worker=worker)))
    class Server:
        sockets = [SimpleNamespace(getsockname=lambda: ("127.0.0.1", 8765))]
        async def serve_forever(self):
            await asyncio.Future()
    class Listener:
        async def __aenter__(self):
            return Server()
        async def __aexit__(self, *args):
            assert finished.is_set()
    monkeypatch.setattr(remote, "serve", lambda *args, **kwargs: Listener())
    monkeypatch.setattr(remote, "USER_MODEL_DRAIN_INTERVAL_SECONDS", 0.001)
    async def scenario():
        task = asyncio.create_task(remote._run_host(host, bind_host="127.0.0.1", bind_port=8765, state_dir=tmp_path))
        assert await asyncio.to_thread(started.wait, 2)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert finished.is_set()
        assert worker.calls == 1
    asyncio.run(scenario())


def test_standalone_drains_queued_job_after_conversation_reply(tmp_path: Path, monkeypatch):
    extractor = EmptyExtractor()
    store, worker = _queue(tmp_path, extractor)
    class Chat:
        def complete(self, messages):
            return "Reply without waiting"
    engine = ConversationEngine(Chat(), MemoryStore(tmp_path / "memory.db"), assimilation_sink=store)
    host = SimpleNamespace(handle=None, processor=SimpleNamespace(action_bridge=SimpleNamespace(user_model_worker=worker)))
    class Server:
        sockets = []
        async def serve_forever(self):
            reply = engine.respond(UserTurn("cli", "local", "hello"), source_ref="source")
            assert reply.text == "Reply without waiting"
            assert extractor.calls == 0
            assert await asyncio.to_thread(extractor.extracted.wait, 2)
    class Listener:
        async def __aenter__(self):
            return Server()
        async def __aexit__(self, *args):
            pass
    monkeypatch.setattr(remote, "serve", lambda *args, **kwargs: Listener())
    monkeypatch.setattr(remote, "USER_MODEL_DRAIN_INTERVAL_SECONDS", 0.001)
    asyncio.run(remote._run_host(host, bind_host="127.0.0.1", bind_port=8765, state_dir=tmp_path))
    assert store.get("source")["status"] == "completed"
    assert extractor.calls == 1
