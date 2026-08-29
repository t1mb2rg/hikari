from __future__ import annotations

import argparse
from collections.abc import Sequence

import nonebot
from nonebot.adapters.onebot.v11 import Adapter as OneBotV11Adapter

from resident.environment import load_runtime_environment

from .config import QQBridgeConfig
from .core_client import ConversationCoreClient
from .health import OneBotLinkHealth
from .runtime import QQBridgeRuntime, install_nonebot_handlers
from .spool import BridgeSpool


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hikari-qq",
        description="通过 NapCat / OneBot V11 反向 WebSocket 接入 Hikari Conversation Host。",
    )
    parser.add_argument("--env-file", default=None)
    parser.add_argument(
        "--check",
        action="store_true",
        help="初始化并校验 QQ Bridge 运行时装配，然后在启动网络服务前退出。",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        runtime_environment = load_runtime_environment(env_file=args.env_file)
        config = QQBridgeConfig.from_mapping(runtime_environment.values)
    except ValueError as exc:
        print(f"Hikari QQ Bridge 启动失败：{exc}")
        return 2

    nonebot.init(
        driver="~fastapi",
        host=config.onebot_host,
        port=config.onebot_port,
        onebot_access_token=config.onebot_access_token,
    )
    driver = nonebot.get_driver()
    driver.register_adapter(OneBotV11Adapter)

    if config.spool_path is None:
        raise RuntimeError("QQ bridge spool path is not configured")
    bridge = QQBridgeRuntime(
        config,
        ConversationCoreClient(
            config.core_url,
            adapter_id=config.adapter_id,
            channel="qq",
            shared_secret=config.core_shared_secret,
        ),
        BridgeSpool(config.spool_path),
        OneBotLinkHealth(timeout_seconds=config.link_timeout_seconds),
    )
    install_nonebot_handlers(bridge)

    reverse_websocket_url = (
        f"ws://{config.onebot_host}:{config.onebot_port}/onebot/v11/ws"
    )
    print("Hikari QQ Bridge 运行时装配完成。")
    print(f"NapCat Reverse WebSocket：{reverse_websocket_url}")
    print(f"Hikari Conversation Host：{config.core_url}")
    print(f"QQ allowlist：{len(config.allowed_user_ids)} 个用户")
    print(f"Bridge spool：{config.spool_path}")
    if args.check:
        print("Hikari QQ Bridge check：PASS")
        return 0

    nonebot.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
