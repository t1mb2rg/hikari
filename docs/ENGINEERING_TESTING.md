# Hikari Engineering Testing Strategy

Hikari 的测试目标不是追求“所有东西都能被 Fake”，而是让测试与真实运行边界尽量一致，同时把昂贵、不可控的外部副作用隔离掉。

## Core principle

> **Fake Hikari-owned abstractions, not external contracts.**
>
> 模拟 Hikari 自己定义的抽象，不重新发明第三方世界。

如果第三方库已经提供正式的数据类型、事件模型或协议对象，contract / adapter 测试应优先直接使用它们，而不是手写一个“长得差不多”的替身。

例如：

- 可以 Fake：`ChatProvider`、`ConversationCoreClient`、`EngineeringBackend`、发送结果 sink 等 Hikari 自己拥有的 seam。
- 不应优先 Fake：NoneBot `GroupMessageEvent`、OneBot `MessageSegment`、WebSocket protocol object、SQLite migration contract、Git CLI output shape 等第三方或系统边界。

只有当真实第三方对象无法合理构造时，才允许建立最小 contract double；这种 double 必须由一条真实 contract test 兜底，防止随第三方版本漂移。

## Four test layers

### L1 — Pure unit

验证 Hikari 自己的纯逻辑，例如：

- mapper / parser
- authority policy
- scope policy
- memory filtering
- deterministic planner / retry rules

这里允许使用小型 dict、fixture 和纯函数输入。

### L2 — Contract / adapter

凡是跨第三方或系统边界，优先使用真实库对象和真实协议结构，例如：

- NoneBot / OneBot events and message segments
- WebSocket wire messages
- SQLite schema / migration
- Git repository / ref semantics
- subprocess result contracts

Contract test 的职责是证明“Hikari 对第三方世界的理解与真实库一致”。

### L3 — Repository regression

验证一个改动没有破坏受影响的 Hikari 子系统。开发过程中优先跑 affected / targeted suite，不要求每次微调都运行全仓库。

### L4 — Physical gate

对真实关键链路做物理验收，例如：

```text
QQ
↓
NapCat
↓
OneBot
↓
Hikari
↓
Conversation / Engineering
↓
真实 QQ / GitHub side effect
```

Physical Gate 验证现实世界；Unit / Contract Test 验证逻辑。两者不能互相替代。

## Execution cadence

默认节奏：

```text
开发中
→ focused unit / contract tests

准备进入 Physical Gate
→ affected contract + regression suite

准备 merge
→ full `python -m pytest -q`

关键 transport / publish / recovery 能力
→ real Physical Gate
```

因此，“减少测试成本”应通过减少无意义 Fake 和缩小开发期测试范围实现，而不是跳过测试。

## Regression rule from M8-01

M8-01 QQ group Physical Gate #1 暴露了典型 contract drift：NoneBot OneBot V11 会在 matcher 之前处理 `@bot`，将 at-self 从 `event.message` 移除，同时保留 `event.original_message`。旧手写 `FakeGroupEvent` 没有表达这个真实 contract，因此此前的单元测试无法发现生产 ingress 会静默拒绝合法群消息。

从此类问题得到的长期规则：

1. 第三方 event / protocol 类型优先直接实例化真实对象。
2. 如果测试需要 Fake Hikari 自己的外部副作用，Fake 只停在 Hikari-owned seam。
3. 任何 minimal external contract double 都必须有真实 contract regression 兜底。
4. 不因为测试替身过时而弱化生产 contract；优先修测试或新增 contract test。
5. Physical Gate 的失败记录保留为工程事实，不用后来的通过结果改写历史。
