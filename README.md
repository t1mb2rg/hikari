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
