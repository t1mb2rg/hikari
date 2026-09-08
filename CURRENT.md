# Hikari Current Context

- 项目/系统工程名仍是 Hikari；默认对话人格现在是 Jarvis。
- Architecture Audit / Cleanup 已收官；Awareness 0 也已完成物理验收，主线现在进入 M7 收尾阶段的 Maintainer capability / persistent loop closure。
- M7-07 Capability-Aware Delegation 已完成。
- 默认 Jarvis 对话入口、Epistemic Boundary、Natural Context 0、Natural Context 1、Memory Context 0 和 Memory Context 1 均已完成物理验收。
- Architecture Cleanup A / Truth Alignment 已完成物理验收：Dashboard、配置和文档已与真实 Engineering Runtime 对齐。
- Architecture Cleanup B / Ownership Cleanup 已完成物理验收：runtime self-state 已退出开发阶段叙事，Operational State 已归 Resident ownership。
- Legacy Forge Removal 已完成针对性测试验收：旧 Forge action、Conversation bridge、examples、tests 和 public exports 已退出仓库主线。
- Conversation Consolidation 已完成验收：Resident 与 standalone `hikari-conversation-host` 均统一走 `NaturalConversationEngine` + Jarvis production + thin Natural Context；Whiteboard 仅保留兼容名称/实验输入，旧 `ConversationEngine.respond()` heavy JSON grounding 已退出正式 Host 路径。
- `ConversationEngine` 当前仍同时承担公共 conversation lifecycle 与 legacy grounded fallback；其最终命名/拆分已登记为发布前 Release Cleanup blocker，不阻塞当前主线。
- Learning Ownership Cleanup 已完成针对性验收：`user_model/` 独占用户当前稳定事实/偏好；`learning/` 只允许从 episodic / experience memory 提炼并召回 reviewed semantic learning，不再生成或召回 `MemoryKind.USER_MODEL`。
- Awareness 0 已完成物理验收：Natural Conversation 通过统一的 selected-context 入口组合 MC0、MC1 与按当前问题选择的 Awareness；Foreground / Input Activity 只有在当前问题直接相关时才被读取并自然化，普通对话不会读取这些信号。
- Awareness 0 的真实 Windows Foreground reader、前台 Resident + QQ 链路和 fresh detached Resident + QQ 链路均已成功。最新物理验收中，Jarvis 在 detached Resident 下正确识别了 Edge 前台窗口及其当前标签页标题。
- Awareness selection 已收敛为 provider-level selection：只问前台窗口只读取 Foreground，只问输入活动只读取 Input Activity；对应 targeted tests 已由用户运行并全绿。
- Maintainer Capability Closure 已开始：`engineering.commands.run` 已完成 targeted tests 与真实 QQ 物理验收并判定 PASS。显式项目命令会建立 `repository_read + run_commands` 的 Engineering turn，在隔离 worktree 中执行，不获得 repository write、tests、network 或 publish 权限；terminal result 会经现有 DeliveryOutbox 回到原 QQ 会话。物理 Gate 中 `python -V` 实际执行返回 `Python 3.12.10`，并由 Worker grounded result 正确交付。
- `engineering.git.push_non_protected` 已实现，正在等待 targeted tests 与真实远端物理验收后再判 PASS。实现只允许 dedicated Worker turn 推送当前 durable EngineeringSession 的 `hikari/engineering/*` clean branch 到配置的 `origin`；branch/refspec 由 Worker 根据 durable state 决定，不由模型选择，不允许 force push、merge、保护分支、dirty worktree 或没有 committed delta 的空 engineering branch。普通 maintainer turn 仍不获得 network/publish 权限；只有单独的 push turn 获得 narrow `network + publish` authority。
- 当前 `engineering.git.open_or_update_draft_pr` 仍是 delegated capability gap；不提前实现到当前 push Gate 里。
- 当前方向：Hikari 是系统身份，Jarvis 是默认对话人格；Conversation Context 负责决定这一轮模型有资格知道什么，内部结构化状态不直接进入 prompt；工程动作统一进入 Engineering Runtime。M7 剩余重点是补齐 Maintainer 必要执行能力、Persistent Task Planning / Maintainer Loop 与最终 Long-Run Graduation。
