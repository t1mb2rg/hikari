# Hikari

> 让光在你不和她说话的时候，也依然存在。

Hikari（光 / ひかり）是一个长期常驻的个人 AI 系统。

她不是简单的聊天机器人，也不是只会等待用户指令的工具。Hikari 的目标是在用户没有主动发起交互时，仍然能够理解数字环境中的变化、结合长期上下文判断意义，并在合适的时候主动介入或完成工作。

## Project Direction

Hikari 的工程北极星是 **Jarvis 式个人 AI 管家**：长期在线、了解用户、保留连续上下文、主动提醒、能够调度不同认知与执行能力，并且知道自己当前能做什么、不能做什么。

Hikari **不以模拟人类意识、虚构感官或构造“数字生命”体验为目标**。稳定人格和自然表达用于维持长期交互连续性，但涉及能力、权限、感知、记忆和执行机制时，必须服从真实系统状态。

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

GPT、Claude、Qwen 等模型只是她使用的认知能力。Hikari 的连续性来自持续的系统身份、记忆、上下文、经验和运行状态，而不是某一个模型进程。

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

Hikari v0.1 先验证一个最小但关键的 Jarvis 式命题：

> 当用户没有和 Hikari 对话时，她仍然能够发现一件值得用户知道的事情，并通过合适的入口主动协助。

## Engineering Runtime

Engineering Runtime 是 Hikari 自己的工程执行能力，不再被建模成一个外部 Forge 服务。

Hikari 负责形成工程意图、维护持久 EngineeringSession、约束权限并接收工程结果；独立 Engineering Worker / backend 负责在隔离故障域中执行具体仓库工作和验证。

```text
Hikari
  ↓
EngineeringSession
  ↓
Engineering Worker / backend
  ↓
Validation + persisted result
  ↓
Hikari delivery / next decision
```

当前工程权限仍然按显式 authority boundary 开放。具备 Engineering Runtime 不等于 Conversation 模型拥有直接 shell、文件系统感知或无限制修改权限。

## M7-06 Operational Self Awareness

M7-06 让 Hikari 不只知道“系统设计上有哪些能力”，还能够基于当前观测回答“这些能力现在是否真的在工作”。这是 Jarvis 式运行状态自知，用于诊断和调度，不是意识模拟。

当前只读 Operational State 会区分静态能力与实时事实，并观测：

- Resident：通过持久 host state 和 live PID 判断当前进程状态。
- QQ / NapCat：复用安全的 NapCat Login Guard 探针，并结合 OneBot endpoint 可达性。
- EngineeringSession：直接读取 Hikari 自己的 durable engineering state，区分 idle / pending / running。
- Engineering Worker：通过独立 heartbeat + live PID 判断实际存活状态，而不是根据“最近成功执行过任务”猜测。

`unknown` 会保持为 unknown，不会自动包装成 healthy。探针结果不会把 WebUI token、临时 credential、二维码 URL、API key、环境变量转储或任意日志内容暴露给 Conversation grounding。

当 Engineering Runtime 启用时，Engineering Worker 由 Resident 负责启动、异常重启和停止。Worker 仍然运行在独立 OS 进程 / fault domain 中，并通过 single-worker lease 防止两个 Worker 同时消费同一个 EngineeringSession store。因此 Hikari 的工程能力会跟随 Resident 一起启动和停止，而不再依赖用户额外保持一个手动 PowerShell Worker。

EngineeringSession 的 repository baseline 是固定快照。同一个会话可以继续复用尚未过期的工程上下文，但当 source repository 的已提交 HEAD 前进时，Conversation 会创建新的 EngineeringSession，而不是让旧 worktree 冒充“最新代码”。

## M7-07 Capability-Aware Delegation

M7-07 把权限模型从“每个动作都找用户确认”升级为 **standing project mandate + exception escalation**。核心原则是：**Human 定义 mandate，Hikari 在 mandate 内执行，Human 只处理越界和高影响例外。**

Hikari 会把三种事实分开：任务需要什么能力、该能力实际上有没有实现、当前项目 mandate 是否已经长期委托这个结果。一个能力可以已经被委托但尚未实现，这时属于 capability gap；反过来，一个技术上可能实现的高影响动作也可以明确留在 mandate 之外，需要升级给用户决定。

Hikari 自己的仓库是第一个 `maintainer` 级项目。当前已实现的 maintainer 闭环包括：

```text
Conversation task
  ↓
Task capability assessment
  ↓
EngineeringSession / isolated worktree
  ↓
Claude backend edits project files
  ↓
Hikari Worker runs pytest
  ↓ test failed
same backend session repairs and retries
  ↓ tests passed
Hikari Worker commits engineering branch
  ↓
terminal result → DeliveryOutbox → user
```

因此，普通仓库读取、项目文件修改、项目测试以及隔离 engineering branch commit 已经属于 Hikari 可直接完成的维护工作，不需要用户为每个文件、命令或测试逐步审批。Conversation 模型本身仍然没有直接 shell 或文件系统感知，实际执行由 Engineering Runtime 完成。

当前项目 mandate 还委托了后续可增长的结果，例如 non-protected engineering branch push 与 Draft PR 维护，但这些能力尚未实现时会被明确表示为 capability gap，而不是假装可用或要求用户逐动作授权。

以下影响边界仍然默认升级给用户：protected branch merge、force push shared history、secret 修改或暴露、生产/外部部署、破坏性数据迁移、权限边界扩张、项目北极星改变以及显著外部成本。护栏放在影响边界上，而不是铺满普通维护流程。

## Operations doctor

在 Windows Resident / QQ / NapCat 链路异常时，先运行只读诊断：

```powershell
hikari-doctor
```

它会集中检查 Resident、durable spool、delivery audit、NapCat 计划任务、QQ
进程、8081/6099 端口、WebUI 与二维码路径，并显示少量最近错误线索。诊断不会
输出 WebUI token，也不会自动启动、停止或重发任何东西。

需要结构化输出或打开本机 NapCat WebUI 时：

```powershell
hikari-doctor --json
hikari-doctor --open-webui
```
