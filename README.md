# Hikari

> 让光在你不和她说话的时候，也依然存在。

Hikari（光 / ひかり）是一个长期常驻的个人 AI 系统，工程目标是 Jarvis 式个人助手：保持上下文，观察数字环境，在合适的时候提醒，并把用户委托的工作交给有明确权限和验证过程的执行器。

Hikari 不以模拟意识或虚构感官为目标。人格和自然表达维持交互连续性；涉及能力、进度、权限、记忆和执行结果时，以实际观测与持久记录为准。模型是可替换的认知组件。

## 当前实现

| 路径 | 当前行为 |
| --- | --- |
| 私人对话 | `ConversationTaskRouter` 区分讨论、澄清、状态、工程、GitHub 和能力请求；普通对话进入默认 Jarvis/Natural 引擎。 |
| 工程任务 | 保存来源、目标、设计约束和验收标准，经持久 Goal / EngineeringSession 与 typed effect 执行；Worker 负责隔离工作区、验证和工程分支提交。 |
| 工程发布 | 已实现非受保护 engineering 分支 push 和草稿 PR 创建/维护；是否执行仍由请求的 effect 与项目 mandate 决定。 |
| GitHub | 提供仓库、PR、文件、Actions 读取，以及受限分支写入、PR 维护、授权工作流重跑和条件化合并；明确授权的失败修复可携实际日志交给既有 Engineering，远端写入有不可变来源凭据。 |
| 能力增长 | 缺失能力可进入持久实现请求；纯文本/列表 recipe 在宿主解释器中实测，精确候选版本经操作人启用后才可调用。Native 代码仍是待审查、验证和部署的候选。 |
| 群聊 | 与私人任务路径分开，必须满足群和群成员白名单、@Hikari、纯文本条件；不取得私人工程、GitHub 或能力增长权限。 |
| 运行面板 | 读取进程、心跳、真实模型调用、连接和任务/投递记录；过期或缺失证据保持 `unknown`。设置保存与运行时生效分开。 |

上述是此候选代码的实现范围，并不表示已经替换日常运行版本，也不表示所有入口均已完成端到端验收。具体启动、候选验证、启用和回滚见 [日常运行指南](docs/JARVIS_DAILY_DRIVER.md)。

## 首次本机安装

Windows 使用 Hikari 自己的 Python 环境。依赖由 `uv.lock` 锁定；首次在一个新的 checkout 中执行：

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned
.\scripts\bootstrap.ps1
& .\.venv\Scripts\python.exe -m resident.windows_host doctor --env-file .\.env
```

`bootstrap.ps1` 用 `uv sync --locked` 安装 `dev` 和 `windows-notify` extras，并仅在缺少 `.env` 时从示例复制。不要对正在运行的环境重复 bootstrap 来做升级；先验证独立候选环境。候选环境管理器使用非 editable 安装，并绑定源码路径/指纹与锁文件；已提升或运行中的环境不能被原地重建。

真实 `.env` 不进入 Git。对话/Presence 模型使用：

```dotenv
HIKARI_MODEL_BASE_URL=...
HIKARI_MODEL_NAME=...
HIKARI_MODEL_API_KEY=...
```

当前进程中的同名变量优先于 env 文件。面板修改 env 文件后，需要受控重启才能应用；旧进程不会因设置已保存就自动使用新模型。

工程模型单独配置：

```dotenv
HIKARI_ENGINEERING_ENABLED=true
HIKARI_ENGINEERING_BACKEND=claude
HIKARI_ENGINEERING_MODEL=sonnet
# 切换到 codex 时使用以下独立项；留空采用本机 Codex 模型配置。
HIKARI_ENGINEERING_CODEX_MODEL=
HIKARI_ENGINEERING_BACKEND_TIMEOUT_SECONDS=300
```

Claude 是默认工程后端，也可选择 `codex`。两者都要求结构化 `completed / blocked / failed` 结果；进程退出成功或一句“完成了”不足以证明任务完成。Worker 继续负责实际文件、测试、提交和交付证据。Codex 不继承桌面会话权限配置、插件或 hooks。当前受限读取 profile 在已审计的 Windows `unelevated` 沙箱上会于模型启动前 blocked；旧功能 Gate 使用内置宽泛读取 profile，不能证明新边界已可用。平台设置需由操作人决定并完成新的完整 Gate，见 [Codex 沙箱边界](docs/CODEX_SANDBOX_BOUNDARY.md)。

## 常驻启动与面板

以下用于已经确认要运行的 checkout；候选试运行应使用独立 state、env 和端口：

```powershell
& .\.venv\Scripts\python.exe -m resident.windows_host start . --env-file .\.env
& .\.venv\Scripts\python.exe -m resident.windows_host status
& .\.venv\Scripts\python.exe -m dashboard.app . --env-file .\.env
```

本机面板默认在 `http://127.0.0.1:8787`。Resident 管理 Conversation Host、按配置启用的 Engineering Worker / QQ Bridge，以及 Presence。每个后台组件保留自己的故障和恢复边界。默认持久状态位于 `%LOCALAPPDATA%\Hikari\resident`；显式 `--state-dir` 可隔离另一套运行。

只读诊断使用 `hikari-doctor` 或 `hikari-doctor --json`。面板“已配置”、最近成功观测、当前组件存活、任务验收和外部交付成功是不同事实。

## QQ / NapCat

```text
QQ → NapCat → OneBot V11 Reverse WebSocket → QQ Bridge
   → Conversation Host → 私人 TaskRouter / 群聊 Natural 路径
```

NoneBot、OneBot、NapCat SDK 只属于 transport 边界。显式消息不经 Presence Attention 决定是否值得回复。

```dotenv
HIKARI_QQ_ENABLED=true
# 私聊入口；只放允许进入私人会话的人。
HIKARI_ONEBOT_ALLOWED_USER_IDS=123456789
# 群聊必须同时允许群和群成员；不会继承私人名单。
HIKARI_ONEBOT_ALLOWED_GROUP_IDS=
HIKARI_ONEBOT_ALLOWED_GROUP_USER_IDS=
# 主动 QQ 投递的固定目标，必须也在私人名单内；留空禁用。
HIKARI_QQ_PROACTIVE_USER_ID=
```

群聊还要求 @Hikari 且剩余内容是纯文本；群名单或群成员名单留空即不开放该群入口。给某人群聊资格不会给他私人任务权限。

NapCat 的 WebSocket 客户端地址为 `ws://127.0.0.1:8081/onebot/v11/ws`；Conversation 默认使用 `ws://127.0.0.1:8765`。监听保持 loopback。若配置 OneBot token，双方必须一致。

QQ 登录、手机扫码和风控验证由用户完成。NapCat Login Guard 可以在一次持久断连中对指定任务做一次受限恢复；持续登录失败后保留人工处理状态，不绕过扫码。面板可显示/刷新二维码，也不能替代用户确认。需要调试分进程时，可分别运行 `hikari-conversation-host --env-file .\.env` 与 `hikari-qq --env-file .\.env`。

## GitHub 与影响边界

仓库默认取当前 GitHub `origin`，可用 `HIKARI_GITHUB_REPOSITORY` 选择默认仓库，并用 `HIKARI_GITHUB_ALLOWED_REPOSITORIES` 配置允许列表。认证复用本机 `gh`。

自动合并默认关闭。操作人策略保存在运行 state 的 `github_policy.json`，位于候选 worktree 之外，且不属于对话 action catalog。放行需要明确的目标分支和实际检查名称、Hikari 创建的 PR 所有权、当前 head 的通过证据、无阻塞审查、无权限/验收边界改动，以及配置要求的真实验收记录。草稿满足实质条件后可以由 gate 转为待审查，再次验证后合并。历史 M7 验收 PR #77、#78、#79 保留用户决定。

重跑失败任务另需操作人固定的工作流文件路径和 blob SHA；重跑请求被接收不等于 CI 已通过。部署、权限扩张、密钥修改、共享历史强推等高影响动作不能由模型自行授权。远端写入结果不确定时保持未知并禁止自动重复。

私人用户明确要求修复 CI 失败时，工作流取得实际失败 run 的 job 日志，再以固定子来源将原目标、约束和验收交给 Engineering。只请求修复不自动发布；明确要求的安全 engineering 分支 push / Draft PR 可顺序继续。本地修复或发布成功仍需另外核验远端 CI。恢复、去重和总步骤上限见 [GitHub 修复交接](docs/GITHUB_REPAIR_HANDOFF.md)。

## 验证证据与文档

当前记录的真实门槛各自有明确范围：

- [自然对话到行动 gate](docs/NATURAL_ACTION_GATE.md)：真实 Gemma 讨论/同意、持久 Goal、真实 Codex 文件执行、精确回读，以及无需模型调用的完成状态查询；保留一次接单语气生成超时。
- [Codex Worker gate](docs/CODEX_BACKEND_GATE.md)：保留首次失败；修复后真实 Worker 创建文件并由 Hikari 提交。
- [GitHub gate](docs/GITHUB_PHYSICAL_GATE.md)：隔离 PR #82、实际 Actions、精确文件回读和仅向隔离 base 的条件化合并。
- [能力增长 gate](docs/CAPABILITY_GROWTH_GATE.md)：真实 Codex 生成 recipe、15 个实际案例验证、仅 scratch 启用与原请求恢复。

这些记录不替代生产 QQ、Native 能力、部署或整套长期运行验收。完整设计见 [ARCHITECTURE.md](ARCHITECTURE.md)，能力边界见 [CAPABILITY_GROWTH.md](docs/CAPABILITY_GROWTH.md)，日常操作见 [JARVIS_DAILY_DRIVER.md](docs/JARVIS_DAILY_DRIVER.md)。

CI 对所有 PR 目标运行，并覆盖 `main`、`m7-*`、`m8-*`、`hikari/engineering/**` 的 push，保留 Windows/Linux 与 Python 3.11/3.12 矩阵和 locked dependencies。CI 只做测试，不部署、不提升授权。
