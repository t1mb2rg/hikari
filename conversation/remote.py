from __future__ import annotations

import argparse
import asyncio
from collections.abc import Mapping, Sequence
import hmac
import ipaddress
from pathlib import Path

from memory.store import MemoryStore
from resident.environment import load_runtime_environment
from resident.paths import default_state_dir
from user_model import build_user_model_runtime
from websockets.asyncio.server import ServerConnection, serve

from .cli import build_chat_provider, default_context_collector
from .engine import ConversationEngine, INTERACTIVE_SYSTEM_INSTRUCTIONS
from .models import AssistantReply, UserTurn
from .protocol import (
    ConversationProtocolError,
    decode_envelope,
    encode_envelope,
    error_envelope,
    hello_ack_envelope,
    parse_hello,
    parse_turn,
    reply_envelope,
)
from .receipts import ConversationReceiptStore


DEFAULT_CONVERSATION_HOST = "127.0.0.1"
DEFAULT_CONVERSATION_PORT = 8765

PRIMARY_REMOTE_RELATIONSHIP_CONTEXT = {
    "kind": "primary_local_user",
    "basis": "trusted_adapter_binding",
    "memory_claim": "continuity_without_implied_episode_recall",
    "continuity": (
        "This explicit conversation arrived through an authenticated Hikari channel adapter "
        "that is responsible for enforcing its caller allowlist. The adapter binding establishes "
        "the ongoing relationship with Hikari's primary user but does not imply recall of any "
        "specific prior episode unless stored conversation history or durable memory supports it."
    ),
}


def _is_loopback_host(host: str) -> bool:
    value = host.strip().lower()
    if value in {"localhost", "::1"}:
        return True
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return False


class ConversationRequestProcessor:
    """Idempotently route remote explicit chat turns through ConversationEngine."""

    def __init__(
        self,
        engine: ConversationEngine,
        receipts: ConversationReceiptStore,
    ) -> None:
        if not isinstance(engine, ConversationEngine):
            raise TypeError("ConversationRequestProcessor requires ConversationEngine")
        if not isinstance(receipts, ConversationReceiptStore):
            raise TypeError("ConversationRequestProcessor requires ConversationReceiptStore")
        self.engine = engine
        self.receipts = receipts

    def process(self, request_id: str, turn: UserTurn) -> tuple[AssistantReply, bool]:
        existing = self.receipts.get(request_id)
        if existing is not None:
            if existing.turn != turn:
                raise ValueError("request_id was reused for a different user turn")
            return existing.reply, True

        reply = self.engine.respond(turn, source_ref=request_id)
        self.receipts.save(request_id, turn, reply)
        return reply, False


class ConversationWebSocketHost:
    """Platform-neutral WebSocket boundary around Hikari's direct conversation core."""

    def __init__(
        self,
        processor: ConversationRequestProcessor,
        *,
        shared_secret: str | None = None,
    ) -> None:
        if not isinstance(processor, ConversationRequestProcessor):
            raise TypeError("ConversationWebSocketHost requires ConversationRequestProcessor")
        self.processor = processor
        self.shared_secret = shared_secret.strip() if shared_secret else None

    async def handle(self, websocket: ServerConnection) -> None:
        try:
            first = await websocket.recv()
            hello = decode_envelope(first)
            adapter_id, channel, provided_secret = parse_hello(hello)
            if self.shared_secret is not None:
                if provided_secret is None or not hmac.compare_digest(
                    provided_secret,
                    self.shared_secret,
                ):
                    await websocket.send(
                        encode_envelope(
                            error_envelope(
                                code="unauthorized",
                                message="conversation adapter authentication failed",
                            )
                        )
                    )
                    await websocket.close(code=1008, reason="unauthorized")
                    return

            await websocket.send(
                encode_envelope(hello_ack_envelope(adapter_id=adapter_id))
            )

            async for raw in websocket:
                request_id: str | None = None
                try:
                    payload = decode_envelope(raw)
                    request_id, turn = parse_turn(payload)
                    if turn.channel != channel:
                        raise ConversationProtocolError(
                            "turn channel must match the authenticated adapter channel"
                        )
                    reply, duplicate = await asyncio.to_thread(
                        self.processor.process,
                        request_id,
                        turn,
                    )
                    await websocket.send(
                        encode_envelope(
                            reply_envelope(
                                request_id=request_id,
                                reply=reply,
                                duplicate=duplicate,
                            )
                        )
                    )
                except (ConversationProtocolError, ValueError) as exc:
                    await websocket.send(
                        encode_envelope(
                            error_envelope(
                                code="invalid_request",
                                message=str(exc),
                                request_id=request_id,
                            )
                        )
                    )
                except Exception:
                    await websocket.send(
                        encode_envelope(
                            error_envelope(
                                code="processing_failed",
                                message="Hikari could not process this conversation turn",
                                request_id=request_id,
                            )
                        )
                    )
        except ConversationProtocolError as exc:
            await websocket.send(
                encode_envelope(
                    error_envelope(code="invalid_handshake", message=str(exc))
                )
            )
            await websocket.close(code=1002, reason="invalid handshake")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hikari-conversation-host",
        description="运行 Hikari 的平台无关显式对话 WebSocket 边界。",
    )
    parser.add_argument("--env-file", default=None)
    parser.add_argument("--db", default=None)
    parser.add_argument("--receipt-db", default=None)
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--history-limit", type=int, default=12)
    parser.add_argument(
        "--desktop-context",
        action="store_true",
        help="显式允许远程聊天读取当前前台窗口和输入活跃度。默认关闭。",
    )
    return parser


def _runtime_value(values: Mapping[str, str], name: str, default: str) -> str:
    value = values.get(name, default).strip()
    return value or default


async def _run_host(
    host: ConversationWebSocketHost,
    *,
    bind_host: str,
    bind_port: int,
) -> None:
    async with serve(
        host.handle,
        bind_host,
        bind_port,
        max_size=1024 * 1024,
        ping_interval=20,
        ping_timeout=20,
    ) as server:
        await server.serve_forever()


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        runtime = load_runtime_environment(env_file=args.env_file)
        values = runtime.values
        provider = build_chat_provider(values)
        bind_host = (
            args.host.strip()
            if isinstance(args.host, str) and args.host.strip()
            else _runtime_value(
                values,
                "HIKARI_CONVERSATION_HOST",
                DEFAULT_CONVERSATION_HOST,
            )
        )
        if not _is_loopback_host(bind_host):
            raise ValueError(
                "M6-08D Conversation Host is loopback-only; remote deployment requires a secure WSS ingress"
            )

        port_text = str(
            args.port
            if args.port is not None
            else _runtime_value(
                values,
                "HIKARI_CONVERSATION_PORT",
                str(DEFAULT_CONVERSATION_PORT),
            )
        )
        bind_port = int(port_text)
        if not 1 <= bind_port <= 65535:
            raise ValueError("HIKARI_CONVERSATION_PORT must be between 1 and 65535")

        shared_secret = values.get("HIKARI_CONVERSATION_SHARED_SECRET")
        shared_secret = shared_secret.strip() if shared_secret else None

        state_dir = default_state_dir()
        memory_path = (
            Path(args.db).expanduser().resolve()
            if args.db
            else (state_dir / "memory.db").resolve()
        )
        receipt_path = (
            Path(args.receipt_db).expanduser().resolve()
            if args.receipt_db
            else (state_dir / "conversation_receipts.db").resolve()
        )
        user_model_service, user_fact_extractor = build_user_model_runtime(
            provider,
            memory_path.parent / "user_model.db",
        )
        engine = ConversationEngine(
            provider,
            MemoryStore(memory_path),
            context_collector=default_context_collector(
                include_desktop_activity=args.desktop_context,
            ),
            personality_profile=None,
            voice_profile=None,
            relationship_context=PRIMARY_REMOTE_RELATIONSHIP_CONTEXT,
            history_limit=args.history_limit,
            user_model_service=user_model_service,
            user_fact_extractor=user_fact_extractor,
            system_instructions=INTERACTIVE_SYSTEM_INSTRUCTIONS,
        )
        processor = ConversationRequestProcessor(
            engine,
            ConversationReceiptStore(receipt_path),
        )
        websocket_host = ConversationWebSocketHost(
            processor,
            shared_secret=shared_secret,
        )
    except (TypeError, ValueError) as exc:
        print(f"Hikari Conversation Host 启动失败：{exc}")
        return 2

    print(f"Hikari Conversation Host：ws://{bind_host}:{bind_port}")
    print(f"模型：{getattr(provider, 'model', type(provider).__name__)}")
    print(f"对话记忆：{memory_path}")
    print(f"请求回执：{receipt_path}")
    try:
        asyncio.run(
            _run_host(
                websocket_host,
                bind_host=bind_host,
                bind_port=bind_port,
            )
        )
    except KeyboardInterrupt:
        print("\nHikari Conversation Host 已停止。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
