# GitHub failure diagnosis and Engineering repair handoff

这份记录说明 2026-09-10 候选代码的修复交接与恢复语义，不是生产部署记录或新的端到端 physical PASS。GitHub 诊断沿用 `GitHubConversationWorkflow`；实际代码修改、验证、提交和按请求发布由既有 Engineering Goal / Worker 执行。

## 从原始请求到修复结果

1. 只有具有明确身份的私人请求、当前用户执行意图和严格布尔值 `allow_local_repair=true` 才能授权修复。只读诊断、讨论、模型建议和远端日志不能授予本地修改权限；群聊不进入这条路径。
2. 工作流冻结原始用户 turn、目标、约束、验收标准、仓库和来源。已观测到失败或超时的 CI run 时，必须先取得这些 run 对应的实际 job 日志。没有日志时保持 `pending` 并说明缺失证据；若实际读到的 runs 全部成功，诊断返回 `done` 可仅完成读取，不会被自动转成修复。
3. 用户已授权修复且失败日志齐备时，即使诊断模型仅返回 `done`，工作流也会准备修复交接。交接本身是 `repair_needed`，不能声称代码已修复。日志、错误摘录和诊断以不可信数据传入 Engineering，不能改写授权或验收标准。
4. Router 核对原始私人身份、目标与约束、同来源观测以及 GitHub 仓库和本地 `origin` 的一致性，再以 `<原始 source_ref>:github-local-repair` 建立子来源。用户侧仍保留原来的父任务；子 Goal 使用原始目标、约束和验收标准。
5. 默认子 Goal 只有 `maintain_project`。Worker 在既有隔离工作区和权限边界内修改、验证并提交。接单、读取结束、进程正常退出和模型文字都不能代替持久 Engineering 结果。

## 发布只沿用明确请求

修复后 push 或 Draft PR 必须是原始用户请求中的受支持效果。确定性 planner 可以补齐 Draft PR 必需的 engineering 分支 push，形成 `maintain_project → push_engineering_branch → open_or_update_draft_pr`。只要求修复不会自动加入发布；明确“不发布”或“不创建 PR”与所提取效果冲突时会阻塞交接。

这条延续只接受本地维护、安全 engineering 分支 push 和 Draft PR。已有分支、精确 head、clean worktree、项目 mandate 和 PR 所有权检查继续生效。诊断阶段不能因为后续需要 Draft PR 就自行加入直接远端写入。已持久化的效果序列不能在恢复或升级时扩张；旧的仅维护交接不会自动升级为发布。

合并、部署、权限变更、强推和任意命令不属于这条修复延续。部署与权限决定仍由用户保留，QQ 扫码与风控确认仍由用户完成。

## 状态与完成范围

| 状态或证据 | 含义 |
| --- | --- |
| `pending` | 诊断尚缺日志、回读或本次时间预算已到；保留实际步骤，后续继续。 |
| `repair_needed` | 诊断证据已准备，尚未启动本地修复。 |
| `repair_pending` | 交接已保存，等待执行入口或当前工程任务结束，或正在核对中断前的派发记录。 |
| `repair_running` | 已找到本来源的 Engineering Goal / turn；仍等待真实结果。 |
| `completed` + `completion_scope=local_repair` | 本地修复和验证结果已确认；未发布到远端。 |
| `completed` + `completion_scope=engineering_repair_and_publication` | 修复及原请求中的发布步骤均有持久成功结果。 |
| `completed` + `completion_scope=github_observation` | 只确认 GitHub 读取；`local_repair_started=false` 表示未启动修复。 |
| `blocked` / `failed` / `unknown` | 分别保留明确阻塞、执行失败或无法确认的结果，不能投影为修复成功。 |

本地修复或发布成功仍返回 `remote_ci_verified=false`。远端 CI 是否通过需要另外读取实际运行与目标 head 的证据。只有只读工程结果、缺失 terminal result 或效果不匹配时，修复不能完成；旧交接缺少原请求的后续步骤时，也不能把整个请求标为成功。

## 崩溃窗口、去重与执行上限

- 父任务写入后、GitHub intake 回执写回前中断：pump 只恢复已经存在且与冻结请求完全匹配的 workflow ledger。仅有父任务记录、缺少 ledger 或身份/约束不符时记为 `unknown`，不重新调用模型猜测请求。
- 修复派发前先保存子来源和 `dispatch_state`。派发中断后先查同一来源的持久 Goal / turn；无论中断发生在 Goal 保存前还是保存后，都沿用同一来源恢复。按来源的进程间锁阻止并发重复派发。
- 远端写入曾开始但缺少完整结果时保持 `unknown`，不自动重放写入，也不借此启动本地修复。已经记录的只读步骤恢复时可复用证据。
- 内部 Engineering 交接使用 `record_exchange=false`，不会把原始用户消息重复加入会话记忆，也不会把远端注入文字当作新的用户记忆。真实 Engineering 子任务的终态由原有 Engineering delivery 投递；GitHub pump 不再为它另建一条 terminal outbox 消息。
- 能力增长的相邻 intake 崩溃窗口也按完整原始 turn 和 source 查找唯一持久请求。先写 outbox，再把父任务投影为终态；投递入队失败时父任务保留可恢复状态，避免丢结果或重复请求。
- 单次时间片耗尽可以继续；整个 GitHub workflow 的总步骤预算耗尽会写入 terminal `blocked`，携带 `step_limit_reached`、已用步数和缺失操作。即使仅差最终确认，也不猜完成；重启或提高配置上限不会复活这个已终止来源。

## 软件回归与平台限制

`tests/test_github_repair_handoff.py` 覆盖私人授权、实际失败日志交接、绿色 CI 不虚构修复、原始约束与仓库绑定、明确发布步骤、派发前后崩溃恢复、并发去重、记忆与 outbox 单次投影，以及 GitHub/能力增长 intake 缺口。`tests/test_github_conversation_workflow.py` 覆盖时间片恢复、总步数终止和不确定远端写入。这里的合成 Engineering 结果用于验证编排与恢复，不是新的真实 GitHub 修复发布验收。

所有已注册 CLI 入口均在参数解析前配置 UTF-8 输出；回归动态读取 `pyproject.toml` 中的入口，在旧 Windows codepage 环境下检查帮助输出，并验证中文 Engineering JSON 和环境状态读取不会修改原记录。相关覆盖位于 `tests/test_cli_utf8_output.py`。最终全量测试与远端 CI 结论须绑定实际候选提交，本文不填写尚未确认的最终测试数量。

最新 Codex 后端要求受限读取 profile。本机已审计的 Windows `unelevated` 沙箱无法执行该策略，预检会在模型启动前返回 blocked，不退回宽泛权限。旧 Codex、Natural Action、Growth 和 Resident 功能 Gate 使用早先的内置宽泛读取 profile，不能证明当前受限策略下的完整工程能力已可用。详见 [Codex sandbox boundary](CODEX_SANDBOX_BOUNDARY.md)。

## 仍需完成的操作

1. 将完整测试、远端 CI、交付报告和安装环境验证绑定到最终候选提交；源码再变更后重新构建对应候选环境。
2. 由操作人决定是否配置受支持的 Codex 沙箱。若选择变更，完成平台设置验证及完整受限沙箱下的隔离 coding Gate；此前保留真实 blocked 状态。
3. 在明确授权的隔离仓库/分支与独立 state 下，完成真实失败日志 → 本地修复 → 验证的验收；如原请求包括发布，再核验 engineering head、Draft PR 与后续实际 CI，保留失败和恢复证据。
4. 准备并核对旧 checkout、解释器、env、state 和回滚坐标后，由用户决定日常部署。完成用户本人 QQ 扫码/风控确认，再验证私聊、群聊隔离和一次 terminal 投递。具体过程见 [日常运行指南](JARVIS_DAILY_DRIVER.md)。
