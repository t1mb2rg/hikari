from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
from queue import Empty, Queue
from threading import Thread
from urllib.request import Request, urlopen

from memory.store import MemoryStore
from resident.environment import load_runtime_environment
from resident.windows_host import default_state_dir

from .cli import build_chat_provider, default_context_collector
from .engine import ConversationEngine, INTERACTIVE_SYSTEM_INSTRUCTIONS
from .gateway import ConversationGateway
from .models import AssistantReply, UserTurn


DEFAULT_ONEBOT_API_BASE_URL = "http://127.0.0.1:3000"
DEFAULT_ONEBOT_WEBHOOK_HOST = "127.0.0.1"
DEFAULT_ONEBOT_WEBHOOK_PORT = 8081
MAX_EVENT_BYTES = 1024 * 1024
RECEIVE_POLL_SECONDS = 0.25

PRIMARY_LOCAL_RELATIONSHIP_CONTEXT = {
    "kind": "primary_local_user",
    "basis": "trusted_runtime_binding",
    "memory_claim": "continuity_without_implied_episode_recall",
    "continuity": (
        "This QQ private chat is an explicit trusted conversation with Hikari's primary local user. "
        "This binding establishes the ongoing relationship but does not mean exact prior "
        "conversations, development episodes, or elapsed gaps are independently remembered."
    ),
}


def parse_allowed_user_ids(value: str | None) -> frozenset[str]:
    if value is None:
        return frozenset()
    return frozenset(part.strip() for part in value.split(",") if part.strip())


def _extract_text(message: object) -> str | None:
    if isinstance(message, str):
        text = message.strip()
        if not text or "[CQ:" in text:
            return None
        return text

    if isinstance(message, list):
        parts: list[str] = []
        for segment in message:
            if not isinstance(segment, Mapping) or segment.get("type") != "text":
                return None
            data = segment.get("data")
            if not isinstance(data, Mapping):
                return None
            text = data.get("text")
            if not isinstance(text, str):
                return None
            parts.append(text)
        joined = "".join(parts).strip()
        return joined or None

    return None


def normalize_onebot_private_event(
    event: Mapping[str, object],
    *,
    allowed_user_ids: frozenset[str],
) -> UserTurn | None:
    if event.get("post_type") != "message":
        return None
    if event.get("message_type") != "private":
        return None

    user_id = event.get("user_id")
    if user_id is None:
        return None
    user_id_text = str(user_id)
    if user_id_text not in allowed_user_ids:
        return None

    text = _extract_text(event.get("message"))
    if text is None:
        return None

    return UserTurn(
        channel="qq",
        conversation_id=f"private:{user_id_text}",
        text=text,
    )


def verify_onebot_signature(body: bytes, signature: str | None, secret: str | None) -> bool:
    if not secret:
        return True
    if not signature or not signature.startswith("sha1="):
        return False
    expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha1).hexdigest()
    actual = signature.removeprefix("sha1=")
    return hmac.compare_digest(expected, actual)


class OneBotTransport:
    def __init__(
        self,
        *,
        api_base_url: str,
        allowed_user_ids: frozenset[str],
        access_token: str | None = None,
    ) -> None:
        if not allowed_user_ids:
            raise ValueError("HIKARI_ONEBOT_ALLOWED_USER_IDS must contain at least one user id")
        self.api_base_url = api_base_url.rstrip("/")
        self.allowed_user_ids = allowed_user_ids
        self.access_token = access_token.strip() if access_token else None
        self._queue: Queue[UserTurn] = Queue()

    def enqueue_event(self, event: Mapping[str, object]) -> bool:
        turn = normalize_onebot_private_event(
            event,
            allowed_user_ids=self.allowed_user_ids,
        )
        if turn is None:
            return False
        self._queue.put(turn)
        return True

    def receive(self) -> UserTurn | None:
        try:
            return self._queue.get(timeout=RECEIVE_POLL_SECONDS)
        except Empty:
            return None

    def send(self, reply: AssistantReply) -> None:
        if reply.channel != "qq":
            raise ValueError("OneBot reply requires channel='qq'")

        prefix = "private:"
        if not reply.conversation_id.startswith(prefix):
            raise ValueError("OneBot reply requires a private QQ conversation id")
        user_id_text = reply.conversation_id[len(prefix):]
        if user_id_text not in self.allowed_user_ids:
            raise ValueError("OneBot reply target must be allowlisted")

        try:
            user_id: int | str = int(user_id_text)
        except ValueError:
            user_id = user_id_text

        payload = json.dumps(
            {"user_id": user_id, "message": reply.text},
            ensure_ascii=False,
        ).encode("utf-8")
        headers = {"Content-Type": "application/json; charset=utf-8"}
        if self.access_token:
            headers["Authorization"] = f"Bearer {self.access_token}"

        request = Request(
            f"{self.api_base_url}/send_private_msg",
            data=payload,
            headers=headers,
            method="POST",
        )
        with urlopen(request, timeout=15) as response:
            raw = response.read()
        if not raw:
            return
        result = json.loads(raw.decode("utf-8"))
        if isinstance(result, Mapping):
            status = result.get("status")
            retcode = result.get("retcode")
            if status not in (None, "ok") or retcode not in (None, 0):
                raise RuntimeError(f"OneBot send failed: status={status!r}, retcode={retcode!r}")


class OneBotWebhookServer:
    def __init__(
        self,
        transport: OneBotTransport,
        *,
        host: str = DEFAULT_ONEBOT_WEBHOOK_HOST,
        port: int = DEFAULT_ONEBOT_WEBHOOK_PORT,
        secret: str | None = None,
    ) -> None:
        if not 1 <= int(port) <= 65535:
            raise ValueError("OneBot webhook port must be between 1 and 65535")
        self.transport = transport
        self.host = host
        self.port = int(port)
        self.secret = secret.strip() if secret else None
        server_self = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    self.send_error(400)
                    return
                if length <= 0 or length > MAX_EVENT_BYTES:
                    self.send_error(413 if length > MAX_EVENT_BYTES else 400)
                    return

                body = self.rfile.read(length)
                if not verify_onebot_signature(
                    body,
                    self.headers.get("X-Signature"),
                    server_self.secret,
                ):
                    self.send_error(403)
                    return

                try:
                    event = json.loads(body.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    self.send_error(400)
                    return
                if not isinstance(event, Mapping):
                    self.send_error(400)
                    return

                server_self.transport.enqueue_event(event)
                self.send_response(204)
                self.end_headers()

            def log_message(self, format: str, *args: object) -> None:
                return

        self._server = ThreadingHTTPServer((self.host, self.port), Handler)
        self._thread: Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("OneBot webhook server is already started")
        self._thread = Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hikari-qq",
        description="通过 NapCat / OneBot 11 私聊接入 Hikari。",
    )
    parser.add_argument("--env-file", default=None)
    parser.add_argument("--db", default=None)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        runtime = load_runtime_environment(env_file=args.env_file)
        values = runtime.values
        provider = build_chat_provider(values)
        allowed = parse_allowed_user_ids(values.get("HIKARI_ONEBOT_ALLOWED_USER_IDS"))
        transport = OneBotTransport(
            api_base_url=values.get(
                "HIKARI_ONEBOT_API_BASE_URL",
                DEFAULT_ONEBOT_API_BASE_URL,
            ).strip(),
            access_token=values.get("HIKARI_ONEBOT_ACCESS_TOKEN"),
            allowed_user_ids=allowed,
        )
        port_text = values.get(
            "HIKARI_ONEBOT_WEBHOOK_PORT",
            str(DEFAULT_ONEBOT_WEBHOOK_PORT),
        ).strip()
        try:
            webhook_port = int(port_text)
        except ValueError as exc:
            raise ValueError("HIKARI_ONEBOT_WEBHOOK_PORT must be an integer") from exc

        memory_path = (
            Path(args.db).expanduser().resolve()
            if args.db
            else (default_state_dir() / "memory.db").resolve()
        )
        engine = ConversationEngine(
            provider,
            MemoryStore(memory_path),
            context_collector=default_context_collector(),
            personality_profile=None,
            voice_profile=None,
            relationship_context=PRIMARY_LOCAL_RELATIONSHIP_CONTEXT,
            system_instructions=INTERACTIVE_SYSTEM_INSTRUCTIONS,
        )
        gateway = ConversationGateway(engine, transport)
        webhook = OneBotWebhookServer(
            transport,
            host=values.get(
                "HIKARI_ONEBOT_WEBHOOK_HOST",
                DEFAULT_ONEBOT_WEBHOOK_HOST,
            ).strip(),
            port=webhook_port,
            secret=values.get("HIKARI_ONEBOT_WEBHOOK_SECRET"),
        )
    except ValueError as exc:
        print(f"Hikari QQ 启动失败：{exc}")
        return 2

    webhook.start()
    print("Hikari QQ 已连接，等待 allowlist 私聊。Ctrl+C 退出。")
    print(f"Webhook：http://{webhook.host}:{webhook.port}/")
    print(f"模型：{getattr(provider, 'model', type(provider).__name__)}")
    print(f"对话记忆：{memory_path}")

    try:
        while True:
            gateway.cycle_once()
    except KeyboardInterrupt:
        print("\nHikari QQ 已断开。")
        return 0
    finally:
        webhook.close()


if __name__ == "__main__":
    raise SystemExit(main())
