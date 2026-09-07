# Hikari Current Context

- 项目/系统工程名仍是 Hikari；默认对话人格现在是 Jarvis。
- Architecture Audit / Cleanup 已收官，主线恢复并进入 Awareness Integration；M7 的继续扩功能仍暂缓，等当前感知主线完成后再重新评估。
- M7-07 Capability-Aware Delegation 已完成。
- 默认 Jarvis 对话入口、Epistemic Boundary、Natural Context 0、Natural Context 1、Memory Context 0 和 Memory Context 1 均已完成物理验收。
- Architecture Cleanup A / Truth Alignment 已完成物理验收：Dashboard、配置和文档已与真实 Engineering Runtime 对齐。
- Architecture Cleanup B / Ownership Cleanup 已完成物理验收：runtime self-state 已退出开发阶段叙事，Operational State 已归 Resident ownership。
- Legacy Forge Removal 已完成针对性测试验收：旧 Forge action、Conversation bridge、examples、tests 和 public exports 已退出仓库主线。
- Conversation Consolidation 已完成验收：Resident 与 standalone `hikari-conversation-host` 均统一走 `NaturalConversationEngine` + Jarvis production + thin Natural Context；Whiteboard 仅保留兼容名称/实验输入，旧 `ConversationEngine.respond()` heavy JSON grounding 已退出正式 Host 路径。
- `ConversationEngine` 当前仍同时承担公共 conversation lifecycle 与 legacy grounded fallback；其最终命名/拆分已登记为发布前 Release Cleanup blocker，不阻塞当前主线。
- Learning Ownership Cleanup 已完成针对性验收：`user_model/` 独占用户当前稳定事实/偏好；`learning/` 只允许从 episodic / experience memory 提炼并召回 reviewed semantic learning，不再生成或召回 `MemoryKind.USER_MODEL`。
- Awareness 0 已进入实现验收：Natural Conversation 现在通过统一的 selected-context 入口组合 MC0、MC1 与按当前问题选择的 Awareness；Foreground / Input Activity 只有在当前问题直接相关时才被读取并自然化，普通对话不会读取这些信号。
- 当前方向：Hikari 是系统身份，Jarvis 是默认对话人格；Conversation Context 负责决定这一轮模型有资格知道什么，内部结构化状态不直接进入 prompt；工程动作统一进入 Engineering Runtime。
