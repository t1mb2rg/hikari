from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import ipaddress
from pathlib import Path

from resident.paths import default_state_dir


DEFAULT_ONEBOT_HOST = "127.0.0.1"
DEFAULT_ONEBOT_PORT = 8081
DEFAULT_CORE_URL = "ws://127.0.0.1:8765"
DEFAULT_ADAPTER_ID = "qq.main"
DEFAULT_LINK_TIMEOUT_SECONDS = 300.0
DEFAULT_LINK_CHECK_SECONDS = 60.0
DEFAULT_DELIVERY_POLL_SECONDS = 1.0
DEFAULT_CONVERSATION_RETRY_INITIAL_SECONDS = 5.0
DEFAULT_CONVERSATION_RETRY_MAX_SECONDS = 60.0


def parse_allowed_user_ids(value: str | None) -> frozenset[str]:
    if value is None:
        return frozenset()
    return frozenset(part.strip() for part in value.split(",") if part.strip())


def _is_loopback(host: str) -> bool:
    value = host.strip().lower()
    if value in {"localhost", "::1"}:
        return True
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return False


def _float_value(values: Mapping[str, str], name: str, default: float) -> float:
    raw = values.get(name, str(default)).strip()
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be numeric") from exc
    if value <= 0:
        raise ValueError(f"{name} must be > 0")
    return value


@dataclass(frozen=True)
class QQBridgeConfig:
    onebot_host: str
    onebot_port: int
    allowed_user_ids: frozenset[str]
    core_url: str
    adapter_id: str = DEFAULT_ADAPTER_ID
    onebot_access_token: str | None = None
    core_shared_secret: str | None = None
    spool_path: Path | None = None
    proactive_user_id: str | None = None
    delivery_outbox_path: Path | None = None
    delivery_poll_seconds: float = DEFAULT_DELIVERY_POLL_SECONDS
    conversation_retry_initial_seconds: float = (
        DEFAULT_CONVERSATION_RETRY_INITIAL_SECONDS
    )
    conversation_retry_max_seconds: float = DEFAULT_CONVERSATION_RETRY_MAX_SECONDS
    link_timeout_seconds: float = DEFAULT_LINK_TIMEOUT_SECONDS
    link_check_seconds: float = DEFAULT_LINK_CHECK_SECONDS

    def __post_init__(self) -> None:
        host = self.onebot_host.strip()
        if not host:
            raise ValueError("onebot_host must not be empty")
        if not 1 <= int(self.onebot_port) <= 65535:
            raise ValueError("onebot_port must be between 1 and 65535")
        if not self.allowed_user_ids:
            raise ValueError("HIKARI_ONEBOT_ALLOWED_USER_IDS must contain at least one user id")
        if not self.core_url.strip().startswith(("ws://", "wss://")):
            raise ValueError("HIKARI_CONVERSATION_URL must use ws:// or wss://")
        if not self.adapter_id.strip():
            raise ValueError("adapter_id must not be empty")
        if not _is_loopback(host):
            raise ValueError(
                "M6-08D OneBot listener is loopback-only; keep NapCat and hikari-qq on the same host"
            )
        proactive_user_id = (
            self.proactive_user_id.strip()
            if isinstance(self.proactive_user_id, str) and self.proactive_user_id.strip()
            else None
        )
        if proactive_user_id is not None and proactive_user_id not in self.allowed_user_ids:
            raise ValueError(
                "HIKARI_QQ_PROACTIVE_USER_ID must be present in HIKARI_ONEBOT_ALLOWED_USER_IDS"
            )
        if self.delivery_poll_seconds <= 0:
            raise ValueError("delivery poll interval must be > 0")
        if self.conversation_retry_initial_seconds <= 0:
            raise ValueError("conversation retry initial interval must be > 0")
        if self.conversation_retry_max_seconds < self.conversation_retry_initial_seconds:
            raise ValueError(
                "conversation retry max interval must be >= initial interval"
            )
        if self.link_timeout_seconds <= 0 or self.link_check_seconds <= 0:
            raise ValueError("link monitor intervals must be > 0")
        object.__setattr__(self, "proactive_user_id", proactive_user_id)

    @classmethod
    def from_mapping(
        cls,
        values: Mapping[str, str],
        *,
        state_dir: Path | None = None,
    ) -> "QQBridgeConfig":
        host = values.get("HIKARI_ONEBOT_HOST", DEFAULT_ONEBOT_HOST).strip()
        port_text = values.get("HIKARI_ONEBOT_PORT", str(DEFAULT_ONEBOT_PORT)).strip()
        try:
            port = int(port_text)
        except ValueError as exc:
            raise ValueError("HIKARI_ONEBOT_PORT must be an integer") from exc

        allowed = parse_allowed_user_ids(values.get("HIKARI_ONEBOT_ALLOWED_USER_IDS"))
        access_token = values.get("HIKARI_ONEBOT_ACCESS_TOKEN")
        access_token = access_token.strip() if access_token and access_token.strip() else None
        core_secret = values.get("HIKARI_CONVERSATION_SHARED_SECRET")
        core_secret = core_secret.strip() if core_secret and core_secret.strip() else None
        proactive_user_id = values.get("HIKARI_QQ_PROACTIVE_USER_ID")
        proactive_user_id = (
            proactive_user_id.strip()
            if proactive_user_id and proactive_user_id.strip()
            else None
        )
        root = (state_dir or default_state_dir()).expanduser().resolve()

        return cls(
            onebot_host=host,
            onebot_port=port,
            allowed_user_ids=allowed,
            core_url=values.get("HIKARI_CONVERSATION_URL", DEFAULT_CORE_URL).strip(),
            adapter_id=values.get("HIKARI_ONEBOT_ADAPTER_ID", DEFAULT_ADAPTER_ID).strip(),
            onebot_access_token=access_token,
            core_shared_secret=core_secret,
            spool_path=(root / "qq_bridge.db").resolve(),
            proactive_user_id=proactive_user_id,
            delivery_outbox_path=(root / "proactive_delivery.db").resolve(),
            delivery_poll_seconds=_float_value(
                values,
                "HIKARI_QQ_DELIVERY_POLL_SECONDS",
                DEFAULT_DELIVERY_POLL_SECONDS,
            ),
            conversation_retry_initial_seconds=_float_value(
                values,
                "HIKARI_QQ_CONVERSATION_RETRY_INITIAL_SECONDS",
                DEFAULT_CONVERSATION_RETRY_INITIAL_SECONDS,
            ),
            conversation_retry_max_seconds=_float_value(
                values,
                "HIKARI_QQ_CONVERSATION_RETRY_MAX_SECONDS",
                DEFAULT_CONVERSATION_RETRY_MAX_SECONDS,
            ),
            link_timeout_seconds=_float_value(
                values,
                "HIKARI_ONEBOT_LINK_TIMEOUT_SECONDS",
                DEFAULT_LINK_TIMEOUT_SECONDS,
            ),
            link_check_seconds=_float_value(
                values,
                "HIKARI_ONEBOT_LINK_CHECK_SECONDS",
                DEFAULT_LINK_CHECK_SECONDS,
            ),
        )
