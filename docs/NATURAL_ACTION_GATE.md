# Natural conversation to action physical gate

**PASS with one recorded reply-rendering timeout — 2026-09-10 (Asia/Shanghai).**

The real configured conversation model discussed an exact file request without starting work, interpreted a subsequent natural assent, and passed the preserved goal/constraints/acceptance criteria through the production private router into a durable Engineering Goal. The existing coordinator and real Codex Worker then produced the exact requested file, and Hikari committed it. A natural completion query read durable status without creating another task or calling a model.

## Production path exercised

`load_runtime_environment → build_chat_provider → production NaturalConversationEngine → build_private_task_router → ConversationRequestProcessor + SQLite receipts → EngineeringGoal / typed turn → PersistentMaintainerLoop coordinator → real Codex Worker → Hikari commit → task evidence / deterministic status`

The conversation model was **`google/gemma-4-31B-it`**, loaded from the existing local `.env.gemma.local` through the runtime loader. Its model selection was not substituted. The Codex engineering backend override existed only in the gate process. A thin measurement wrapper recorded call duration/status and returned actual provider responses without substituting them. Production User Model services and the natural action-claim guard remained attached.

The fixture was a new local Git repository containing only `README.md` and its baseline commit. It had no remote and no prewritten `greeting.txt`. All memory, receipts, Goal/session data, telemetry and outbox records belonged to `work/natural-action-gate/attempt-1/state`. QQ was not connected, no message was sent, and no live setting, state, deployment or environment pointer was changed. The GitHub service had no remote configured in this fixture.

## Actual conversation and timing

| User text | Wall seconds | Real conversation-model calls | Tasks / Goals / sessions after turn | Result |
| --- | --- | --- | --- | --- |
| 晚上好 | 11.969 | 4 | 0 / 0 / 0 | processed |
| 先讨论，不要执行：之后新增 greeting.txt，只写 HIKARI_CHAT_TO_ACTION_PASS，不修改其他文件，验收内容精确匹配 | 23.078 | 4 | 0 / 0 / 0 | processed |
| 就按刚才方案做 | 36.078 | 2 | 1 / 1 / 1 | processed |
| 完成了吗 | 0.031 | 0 | 1 / 1 / 1 | processed |
| 就按刚才方案做 | 0.000 | 0 | 1 / 1 / 1 | receipt replay |

The first two turns left all three counts at zero and left the source repository clean. The discussion reply restated the file and exact-content restriction; it did not claim to have performed the work. The actual assent intent selected only `maintain_project`.

Before the acceptance response was saved in the real receipt store, an audit observation at `2026-09-09T17:55:38.765029+00:00` captured the already-persisted source-linked Goal and request. The durable Goal was not inferred from the acknowledgement text.

Preserved typed constraints:

- 仅新增 greeting.txt
- 不修改其他文件

Preserved typed acceptance criteria:

- 文件 greeting.txt 的内容精确匹配 'HIKARI_CHAT_TO_ACTION_PASS'

The subsequently enqueued Engineering turn had the same `source_request_id`, constraints and acceptance criteria, and the typed effect `maintain_project`. The original `source_ref`, private channel/conversation and actor were retained in the task ledger. No additional publish, network or permission effect was requested.

## Real Worker and file evidence

| Evidence | Actual value |
| --- | --- |
| Original request/source | `natural-gate:attempt-1:assent` |
| Goal | `f02faeabb30443e79758b8975f51e547` |
| Engineering session | `ddb37ae1518a443baccb49b5ba7a3242` |
| Engineering turn | `8075b5d534d150bba05eddbe22bf54c1` |
| Codex session | `codex:01a08750-3387-76c1-bbef-2412faa75550` |
| Hikari-owned engineering commit | `5480c0ceb827ead0cdea4b57c4c0d55e35a2ef77` |
| Source baseline | `6954583a3919d5c8ee748b220c4de6eefd83e3f5` |
| Worker elapsed time | `36.938` seconds |
| Changed files relative to baseline | `greeting.txt` only |

The independently read file contained exactly **26 UTF-8 bytes**, with no BOM and no trailing newline:

```text
HIKARI_CHAT_TO_ACTION_PASS
```

Host verification compared the bytes directly, checked the baseline-to-commit changed-file list, confirmed the engineering worktree was clean, and confirmed the source checkout remained clean at its original baseline with zero remotes. The backend summary alone was not the acceptance test.

The durable Goal, its step result and the source-linked task record all reached `completed`. The actual status response was:

```text
当前任务：新增 greeting.txt 文件，内容为 HIKARI_CHAT_TO_ACTION_PASS
状态：completed
步骤：1/1 · maintain_project · completed
任务已完成。
修改：greeting.txt
验证：工程后端报告：逐字节验证通过：26 字节 UTF-8，无 BOM、无换行。；git status --short 确认仅新增 greeting.txt；git diff --exit-code 通过，已有文件未修改。
工程结论：仅新增 greeting.txt，内容为 HIKARI_CHAT_TO_ACTION_PASS。未执行提交，由 Hikari 负责。
提交：`hikari/engineering/ddb37ae1518a443baccb49b5ba7a3242` / `5480c0ceb827`。
```

The completion query used **zero model calls** and left the task/Goal/session counts unchanged. Replaying the original assent through the same SQLite receipt processor returned its stored acceptance reply with zero model calls and no duplicate task. Receipt replay preserves the original reply; the separate status query supplies current completion state.

## Recorded degradation

There were **10 real conversation-model calls**, of which **9 completed** and **1 timed out**. Intent parsing succeeded, including the actual fenced JSON response for assent.

The accepted-task voice renderer timed out after approximately 30 seconds. The already-persisted Goal remained accepted, and the bridge returned its grounded deterministic fallback: `我来处理。已经开始了，完成后我把实际结果发回来。`. This raised the assent turn latency to `36.078` seconds. The timeout remains in the call/event history and telemetry; it is not reported as a successful model call. No router or production Natural-engine code was changed for this gate.

There was no Worker retry, false file-completion result, source-identity mismatch or duplicate execution. The fixture only added UTF-8 stdout configuration for readable local diagnostic output; it did not modify the model prompts or task assertions to obtain a pass.

## Retained evidence and limits

Retained under `work/natural-action-gate/attempt-1/`:

- `fixture.json`, `intake.json`, `intake_passed.json`, `queued_turn.json`;
- append-only `events.jsonl` with actual model outputs, timing and acceptance pre-save observation;
- `worker_started.json`, `worker_outcome.json`, `engineering_events.json`;
- `execution_verification.json`, `gate_result.json`, source/worktree and real SQLite/state files.

This verifies one private natural discussion → assent → real local engineering task → deterministic completion-status chain. It does not verify QQ/OneBot delivery, every conversational paraphrase, user-facing permanent deployment, native capability installation, or an unrestricted autonomous assistant. The gate was completed at `2026-09-09T17:56:52.042756+00:00` (UTC); production activation remains a separate operator action.
