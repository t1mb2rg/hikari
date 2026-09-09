from __future__ import annotations

import asyncio

from conversation.models import AssistantReply
from core.delivery import DeliveryOutbox, DeliveryRecord
from nonebot import get_driver, logger, on_message
from nonebot.adapters import Event
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, PrivateMessageEvent
from nonebot.message import event_preprocessor

from .config import QQBridgeConfig
from .core_client import ConversationCoreClient
from .health import OneBotLinkHealth
from .mapper import normalize_group_message, normalize_private_message
from .spool import BridgeSpool, BridgeSpoolItem


class QQBridgeRuntime:
    """OneBot transport edge. It owns no Hikari cognition, memory, or personality."""

    def __init__(
        self,
        config: QQBridgeConfig,
        core: ConversationCoreClient,
        spool: BridgeSpool,
        health: OneBotLinkHealth,
        delivery_outbox: DeliveryOutbox | None = None,
    ) -> None:
        self.config = config
        self.core = core
        self.spool = spool
        self.health = health
        self.delivery_outbox = delivery_outbox
        self._bot: Bot | None = None
        self._monitor_task: asyncio.Task[None] | None = None
        self._delivery_task: asyncio.Task[None] | None = None
        self._conversation_retry_task: asyncio.Task[None] | None = None
        # Live inbound, reconnect drain, and periodic recovery share one owner.
        # This prevents overlapping model calls or QQ sends for the same turn
        # while retaining durable at-least-once recovery after process failure.
        self._conversation_lock = asyncio.Lock()

    def observe_event(self) -> None:
        self.health.mark_event()

    async def on_bot_connect(self, bot: Bot) -> None:
        self._bot = bot
        self.health.mark_connected(bot.self_id)
        logger.info(f"Hikari QQ OneBot connected: self_id={bot.self_id}")
        await self.drain_unsent(bot)
        await self.drain_proactive(bot)

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
        item = self.spool.record_turn(
            request_id, turn, sender_user_id=str(event.user_id)
        )
        if item.state == "sent":
            return
        await self._deliver_item(bot, item)

    async def handle_group_message(self, bot: Bot, event: GroupMessageEvent) -> None:
        normalized = normalize_group_message(
            bot_self_id=bot.self_id,
            group_id=event.group_id,
            user_id=event.user_id,
            message_id=event.message_id,
            message=event.message,
            allowed_user_ids=self.config.allowed_user_ids,
            allowed_group_ids=self.config.allowed_group_ids,
        )
        if normalized is None:
            return
        request_id, turn = normalized
        item = self.spool.record_turn(
            request_id, turn, sender_user_id=str(event.user_id)
        )
        if item.state == "sent":
            return
        await self._deliver_item(bot, item)

    async def _deliver_item(self, bot: Bot, item: BridgeSpoolItem) -> None:
        async with self._conversation_lock:
            current = self.spool.get(item.request_id)
            if current is None:
                raise RuntimeError("QQ bridge spool item disappeared before delivery")
            if current.state == "sent":
                return
            # Re-check the allowlist against the recorded sender before spending
            # a model call on a turn whose origin was revoked since it was spooled.
            self._validate_target(
                channel=current.turn.channel,
                conversation_id=current.turn.conversation_id,
                sender_user_id=current.sender_user_id,
            )
            reply = current.reply
            if reply is None:
                reply = await self.core.request(current.request_id, current.turn)
                current = self.spool.set_reply(current.request_id, reply)
                reply = current.reply
            if reply is None:
                raise RuntimeError("QQ bridge spool lost assistant reply")
            await self._send_outbound(
                bot, reply, sender_user_id=current.sender_user_id
            )
            self.spool.mark_sent(current.request_id)

    async def _send_outbound(
        self,
        bot: Bot,
        reply: AssistantReply,
        *,
        sender_user_id: str | None = None,
    ) -> None:
        self._validate_outbound(reply, sender_user_id=sender_user_id)
        if reply.conversation_id.startswith("private:"):
            await bot.send_private_msg(
                user_id=self._onebot_user_id(
                    reply.conversation_id.removeprefix("private:")
                ),
                message=reply.text,
                auto_escape=True,
            )
            return
        await bot.send_group_msg(
            group_id=self._onebot_user_id(
                reply.conversation_id.removeprefix("group:")
            ),
            message=reply.text,
            auto_escape=True,
        )

    @staticmethod
    def _onebot_user_id(user_id_text: str) -> int | str:
        try:
            return int(user_id_text)
        except ValueError:
            return user_id_text

    def _validate_target(
        self,
        *,
        channel: str,
        conversation_id: str,
        sender_user_id: str | None,
    ) -> None:
        if channel != "qq":
            raise ValueError("QQ bridge refuses non-QQ replies")
        if conversation_id.startswith("private:"):
            user_id = conversation_id.removeprefix("private:")
            if user_id not in self.config.allowed_user_ids:
                raise ValueError("QQ bridge refuses replies outside the allowlist")
            return
        if conversation_id.startswith("group:"):
            group_id = conversation_id.removeprefix("group:")
            if group_id not in self.config.allowed_group_ids:
                raise ValueError("QQ bridge refuses replies to an unapproved group")
            if sender_user_id is None:
                raise ValueError(
                    "QQ bridge refuses group replies without a recorded sender"
                )
            if sender_user_id not in self.config.allowed_user_ids:
                raise ValueError(
                    "QQ bridge refuses group replies from a "
                    "no-longer-allowlisted sender"
                )
            return
        raise ValueError("QQ bridge refuses replies outside private or approved groups")

    def _validate_outbound(
        self,
        reply: AssistantReply,
        *,
        sender_user_id: str | None = None,
    ) -> None:
        self._validate_target(
            channel=reply.channel,
            conversation_id=reply.conversation_id,
            sender_user_id=sender_user_id,
        )

    def _validate_proactive(self, item: DeliveryRecord) -> None:
        request = item.request
        if request.channel != "qq":
            raise ValueError("QQ bridge refuses non-QQ proactive deliveries")
        target = self.config.proactive_user_id
        if target is None:
            raise ValueError("HIKARI_QQ_PROACTIVE_USER_ID is not configured")
        if request.recipient != target:
            raise ValueError("QQ bridge refuses proactive delivery to an untrusted recipient")
        if target not in self.config.allowed_user_ids:
            raise ValueError("QQ proactive recipient is outside the allowlist")

    async def _deliver_proactive(self, bot: Bot, item: DeliveryRecord) -> None:
        outbox = self.delivery_outbox
        if outbox is None:
            return
        self._validate_proactive(item)
        try:
            claimed = outbox.claim(item.request.delivery_id)
        except ValueError:
            # Another drain task already owns this delivery, or its state moved on.
            return
        request = claimed.request
        try:
            await bot.send_private_msg(
                user_id=self._onebot_user_id(request.recipient),
                message=request.text,
                auto_escape=True,
            )
        except Exception as exc:
            outbox.release_pending(
                request.delivery_id,
                f"{type(exc).__name__}: {exc}",
            )
            raise
        outbox.mark_sent(request.delivery_id)

    async def drain_unsent(self, bot: Bot) -> bool:
        for item in self.spool.unsent():
            try:
                await self._deliver_item(bot, item)
            except Exception as exc:
                logger.warning(
                    f"Hikari QQ deferred spool item {item.request_id}: {type(exc).__name__}"
                )
                return False
            logger.info(f"Hikari QQ recovered spool item {item.request_id}")
        return True

    async def drain_proactive(self, bot: Bot) -> None:
        outbox = self.delivery_outbox
        if outbox is None:
            return
        for item in outbox.pending(channel="qq"):
            try:
                await self._deliver_proactive(bot, item)
            except Exception as exc:
                logger.warning(
                    "Hikari QQ deferred proactive delivery "
                    f"{item.request.delivery_id}: {type(exc).__name__}"
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

    async def delivery_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(self.config.delivery_poll_seconds)
                bot = self._bot
                if bot is not None:
                    await self.drain_proactive(bot)
        except asyncio.CancelledError:
            raise

    async def conversation_retry_loop(self) -> None:
        delay = self.config.conversation_retry_initial_seconds
        try:
            while True:
                await asyncio.sleep(delay)
                bot = self._bot
                if bot is None:
                    delay = self.config.conversation_retry_initial_seconds
                    continue
                drained = await self.drain_unsent(bot)
                if drained:
                    delay = self.config.conversation_retry_initial_seconds
                else:
                    delay = min(
                        delay * 2,
                        self.config.conversation_retry_max_seconds,
                    )
        except asyncio.CancelledError:
            raise

    async def start(self) -> None:
        if self.delivery_outbox is not None:
            uncertain = self.delivery_outbox.recover_inflight()
            if uncertain:
                logger.warning(
                    f"Hikari QQ quarantined {uncertain} uncertain proactive delivery record(s)"
                )
        if self._monitor_task is None:
            self._monitor_task = asyncio.create_task(
                self.monitor_loop(),
                name="hikari-qq-link-monitor",
            )
        if self._conversation_retry_task is None:
            self._conversation_retry_task = asyncio.create_task(
                self.conversation_retry_loop(),
                name="hikari-qq-conversation-retry",
            )
        if self.delivery_outbox is not None and self._delivery_task is None:
            self._delivery_task = asyncio.create_task(
                self.delivery_loop(),
                name="hikari-qq-proactive-delivery",
            )

    async def close(self) -> None:
        tasks = [
            self._monitor_task,
            self._delivery_task,
            self._conversation_retry_task,
        ]
        self._monitor_task = None
        self._delivery_task = None
        self._conversation_retry_task = None
        for task in tasks:
            if task is None:
                continue
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
        try:
            if isinstance(event, PrivateMessageEvent):
                await runtime.handle_private_message(bot, event)
            elif isinstance(event, GroupMessageEvent):
                # At-self gating keeps model calls out of ordinary group noise and
                # out of any echoed copy of Hikari's own plain-text group replies.
                await runtime.handle_group_message(bot, event)
        except Exception as exc:
            if isinstance(event, PrivateMessageEvent):
                kind = "private message"
            elif isinstance(event, GroupMessageEvent):
                kind = "group message"
            else:
                kind = "message"
            logger.error(
                f"Hikari QQ failed to handle {kind}: {type(exc).__name__}: {exc}"
            )
