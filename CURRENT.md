# Hikari Current Context

- 项目/系统工程名仍是 Hikari；默认对话人格现在是 Jarvis。
- 当前处于 Architecture Audit / Conversation Rebuild 阶段，M7 的继续扩功能暂时暂停。
- M7-07 Capability-Aware Delegation 已完成。
- 默认 Jarvis 对话入口、Epistemic Boundary、Natural Context 0、Natural Context 1、Memory Context 0 和 Memory Context 1 均已完成物理验收。
- Architecture Cleanup A / Truth Alignment 已完成物理验收：Dashboard、配置和文档已与真实 Engineering Runtime 对齐。
- Architecture Cleanup B / Ownership Cleanup 已完成物理验收：runtime self-state 已退出开发阶段叙事，Operational State 已归 Resident ownership。
- Legacy Forge Removal 已完成针对性测试验收：旧 Forge action、Conversation bridge、examples、tests 和 public exports 已退出仓库主线。
- Conversation Consolidation A / Production Promotion 已实施，等待针对性验收：Whiteboard 实验中通过物理验收的自然对话生命周期已晋升为 `NaturalConversationEngine`；历史 Whiteboard profile 仅保留兼容名称与实验 prompt/context 输入，不再维护第二份对话实现。
- 当前剩余 Conversation 清理重点：standalone `hikari-conversation-host` 仍使用旧 grounded `ConversationEngine`；后续再审旧 Engine 的 heavy JSON grounding 与公共生命周期，决定退役边界。
- 当前方向：Hikari 是系统身份，Jarvis 是默认对话人格；开发进度由 CURRENT.md 表达，运行时 self-state 只描述稳定系统事实；工程动作统一进入 Engineering Runtime；生产自然对话只保留一份实现。
