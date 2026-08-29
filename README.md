# Hikari

> 让光在你不和她说话的时候，也依然存在。

Hikari（光 / ひかり）是一个持续存在的个人智能系统。

她不是简单的聊天机器人，也不是等待用户指令的工具。Hikari 的目标是在用户没有主动发起交互时，仍然能够感知数字环境中的变化，理解变化的意义，并在合适的时候主动介入。

## Local Environment

Windows 本地开发只认一个 Python 环境：仓库根目录下的 `.venv`。不要再给 Hikari 复用 Forge 或 sibling venv。

从仓库根目录执行：

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned
.\scripts\bootstrap.ps1
& .\.venv\Scripts\Activate.ps1
hikari-resident doctor --env-file .\.env
```

`bootstrap.ps1` 会：

- 创建 `<repo>\.venv`
- 安装 `.[dev,windows-notify]`
- 在缺少 `.env` 时从 `.env.example` 复制一份

真实 `.env` 不进入 Git。模型配置使用：

```dotenv
HIKARI_MODEL_BASE_URL=...
HIKARI_MODEL_NAME=...
HIKARI_MODEL_API_KEY=...
```

运行时优先级是：**当前进程环境变量 > env 文件**。因此 CI / 部署可以安全注入变量，本地开发则不必每开一个 PowerShell 都重新 `$env:...`。

### QQ / NapCat OneBot

M6-08D 将显式聊天与 QQ transport 分成两个进程。QQ 私聊仍然进入 M6-07 的 `ConversationEngine`，不会伪装成 Presence Sensor Event，也不会经过 Attention 决定是否值得回复。

```text
QQ
 ↓
NapCatQQ Desktop
 ↓ OneBot V11 Reverse WebSocket
hikari-qq
 ↓ hikari.conversation.v1 WebSocket
hikari-conversation-host
 ↓
ConversationEngine / Memory / Hikari identity
```

`hikari-qq` 使用 NoneBot2 + OneBot V11 adapter，但这些平台 SDK 只存在于 `integrations/qq_bridge/`。共享 `conversation/`、`brain/`、`memory/` 和 `personality/` 不依赖 NoneBot、NapCat 或 OneBot。

第一道 Physical Gate 故意很窄：只接受 allowlist 中用户的**私聊纯文本**。群聊、notice、图片、文件和非白名单用户不会触发模型调用。

`.env` 至少配置自己的 QQ 号和模型：

```dotenv
HIKARI_ONEBOT_ALLOWED_USER_IDS=123456789
HIKARI_CONVERSATION_HOST=127.0.0.1
HIKARI_CONVERSATION_PORT=8765
HIKARI_CONVERSATION_URL=ws://127.0.0.1:8765
HIKARI_ONEBOT_HOST=127.0.0.1
HIKARI_ONEBOT_PORT=8081
```

M6-08D 的 Conversation Host 和 NapCat reverse WebSocket listener 都强制保持在 loopback。`HIKARI_CONVERSATION_SHARED_SECRET` 与 `HIKARI_ONEBOT_ACCESS_TOKEN` 可以继续用于本机边界鉴权，但它们不是 TLS 的替代品。以后 Core 真正迁移到远端时，再通过 `wss://` / secure ingress 正式开放，而不是把明文 `ws://` 暴露到局域网或公网。

先启动 Hikari Conversation Host：

```powershell
hikari-conversation-host --env-file .\.env
```

再开第二个终端启动 QQ Bridge：

```powershell
hikari-qq --env-file .\.env
```

NapCatQQ Desktop 由用户自行安装、登录和处理二维码/风控。Hikari 不启动、不登录、也不自动重启 NapCat。NapCat 中新增 **WebSocket 客户端 / Reverse WebSocket**，URL 配置为：

```text
ws://127.0.0.1:8081/onebot/v11/ws
```

若启用了 OneBot access token，NapCat 与 `.env` 的 `HIKARI_ONEBOT_ACCESS_TOKEN` 必须一致。

QQ Bridge 会把待处理 turn 和待发送 reply 存进本地 `qq_bridge.db`；Conversation Host 会用独立 receipt store 按 request id 去重。短暂断线后可以重试，而不会因为同一个 OneBot `message_id` 再次上报就重复调用模型。OneBot 链路长时间没有任何事件时，Bridge 会主动调用 `get_status()` 区分“群/账号很安静”和“链路真的断了”，但不会替用户重启 NapCat。

后台启动示例：

```powershell
hikari-resident start G:\work\LAB\code\hikari-m0-gate `
  --interval 1 `
  --output windows `
  --reasoner model `
  --env-file .\.env
```

## Core Idea

大多数 AI 的模式：

```
用户提出需求
    ↓
AI 响应
    ↓
任务结束
```

Hikari 希望探索：

```
持续存在
    ↓
感知变化
    ↓
理解上下文
    ↓
形成记忆
    ↓
判断重要性
    ↓
主动协助
```

## Hikari is not a model

Hikari 不属于任何单一模型。

GPT、Claude、Qwen 等模型只是她使用的认知能力。Hikari 的连续性来自：

- Identity
- Memory
- Experience
- Context
- Personality

## Core Loop

```
World
 ↓
Sense
 ↓
Attention
 ↓
Reasoning
 ↓
Memory
 ↓
Decision
 ↓
Action
 ↓
Experience
```

## Current Goal

Hikari v0.1 不追求完整的数字生命，而是验证一个核心命题：

> 当用户没有和 Hikari 对话时，她仍然能够发现一件值得用户知道的事情。

## Relationship with Forge

Forge 是 Hikari 的工程执行能力。

Hikari 负责理解、判断和提出改进方向。

Forge 负责实现变化、修改代码和验证结果。

```
Hikari
  ↓
Growth Proposal
  ↓
Forge
  ↓
New Capability
```
