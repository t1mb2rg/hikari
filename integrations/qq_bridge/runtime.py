from __future__ import annotations

import asyncio

from conversation.models import AssistantReply
from nonebot import get_driver, logger, on_message
from nonebot.adapters import Event
from nonebot.adapters.onebot.v11 import Bot, PrivateMessageEvent
from nonebot.message import event_preprocessor

from .config import QQBridgeConfig
from .core_client import ConversationCoreClient
from .health import OneBotLinkHealth
from .mapper import normalize_private_message
from .spool import BridgeSpool, BridgeSpoolItem


class QQBridgeRuntime:
    """OneBot transport edge. It owns no Hikari cognition, memory, or personality."""

    def __init__(
        self,
        config: QQBridgeConfig,
        core: ConversationCoreClient,
        spool: BridgeSpool,
        health: OneBotLinkHealth,
    ) -> None:
        self.config = config
        self.core = core
        self.spool = spool
        self.health = health
        self._bot: Bot | None = None
        self._monitor_task: asyncio.Task[None] | None = None

    def observe_event(self) -> None:
        self.health.mark_event()

    async def on_bot_connect(self, bot: Bot) -> None:
        self._bot = bot
        self.health.mark_connected(bot.self_id)
        logger.info(f"Hikari QQ OneBot connected: self_id={bot.self_id}")
        await self.drain_unsent(bot)

    async def on_bot_disconnect(self, bot: Bot) -> None:
        if self._bot is bot:
            self._bot = None
        self.health.mark_disconnected()
        logger.warning(f"Hikari QQ OneBot disconnected: self_id={bot.self_id}")

    async def handle_private_message(self, bot: Bot, event: PrivateMessageEvent) -> None:
        normalized = normalize_private_message(
            bot_self_id=bot.self_id,
            user_id=event.user_id,
            message_id=event.message_id,
            message=event.message,
            allowed_user_ids=self.config.allowed_user_ids,
        )
        if normalized is None:
            return
        request_id, turn = normalized
        item = self.spool.record_turn(request_id, turn)
        if item.state == "sent":
            return
        await self._deliver_item(bot, item)

    async def _deliver_item(self, bot: Bot, item: BridgeSpoolItem) -> None:
        reply = item.reply
        if reply is None:
            reply = await self.core.request(item.request_id, item.turn)
            item = self.spool.set_reply(item.request_id, reply)
            reply = item.reply
        if reply is None:
            raise RuntimeError("QQ bridge spool lost assistant reply")
        self._validate_outbound(reply)
        user_id_text = reply.conversation_id.removeprefix("private:")
        try:
            target: int | str = int(user_id_text)
        except ValueError:
            target = user_id_text
        await bot.send_private_msg(
            user_id=target,
            message=reply.text,
            auto_escape=True,
        )
        self.spool.mark_sent(item.request_id)

    def _validate_outbound(self, reply: AssistantReply) -> None:
        if reply.channel != "qq":
            raise ValueError("QQ bridge refuses non-QQ replies")
        if not reply.conversation_id.startswith("private:"):
            raise ValueError("QQ bridge refuses non-private replies")
        user_id = reply.conversation_id.removeprefix("private:")
        if user_id not in self.config.allowed_user_ids:
            raise ValueError("QQ bridge refuses replies outside the allowlist")

    async def drain_unsent(self, bot: Bot) -> None:
        for item in self.spool.unsent():
            try:
                await self._deliver_item(bot, item)
            except Exception as exc:
                logger.warning(
                    f"Hikari QQ deferred spool item {item.request_id}: {type(exc).__name__}"
                )
                break

    async def monitor_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(self.config.link_check_seconds)
                bot = self._bot
                if bot is None or not self.health.needs_probe():
                    continue
                try:
                    await bot.get_status()
                except Exception as exc:
                    self.health.mark_probe(False)
                    logger.warning(
                        f"Hikari QQ OneBot health probe failed: {type(exc).__name__}"
                    )
                else:
                    self.health.mark_probe(True)
        except asyncio.CancelledError:
            raise

    async def start(self) -> None:
        if self._monitor_task is None:
            self._monitor_task = asyncio.create_task(
                self.monitor_loop(),
                name="hikari-qq-link-monitor",
            )

    async def close(self) -> None:
        task, self._monitor_task = self._monitor_task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        await self.core.close()


def install_nonebot_handlers(runtime: QQBridgeRuntime) -> None:
    """Register all OneBot-specific hooks inside the integration package."""

    driver = get_driver()

    @event_preprocessor
    async def _observe_all_onebot_events(event: Event) -> None:
        runtime.observe_event()

    @driver.on_startup
    async def _start_runtime() -> None:
        await runtime.start()

    @driver.on_shutdown
    async def _close_runtime() -> None:
        await runtime.close()

    @driver.on_bot_connect
    async def _connected(bot: Bot) -> None:
        await runtime.on_bot_connect(bot)

    @driver.on_bot_disconnect
    async def _disconnected(bot: Bot) -> None:
        await runtime.on_bot_disconnect(bot)

    matcher = on_message(priority=1, block=True)

    @matcher.handle()
    async def _handle_message(bot: Bot, event: Event) -> None:
        if not isinstance(event, PrivateMessageEvent):
            return
        try:
            await runtime.handle_private_message(bot, event)
        except Exception as exc:
            logger.error(
                f"Hikari QQ failed to handle private message: {type(exc).__name__}: {exc}"
            )
