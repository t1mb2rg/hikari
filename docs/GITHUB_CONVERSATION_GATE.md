# Natural GitHub conversation workflow physical gate

**PASS — 2026-09-10 (Asia/Shanghai).** The real Gemma provider autonomously selected `list_runs → jobs → logs`, carrying actual observed run/job IDs between calls, and returned a grounded read-only result. No IDs or SHAs were supplied by the user or preselected in the gate.

## User request and actual execution

> 看看 hikari 最近的 Actions，如果有失败，读对应任务日志告诉我实际失败原因；不要修改任何东西

The gate used `GitHubConversationWorkflow`, the real `GitHubActionService`, current local `gh` authentication, and **`google/gemma-4-31B-it`** loaded through `load_runtime_environment` from the explicit local `.env.gemma.local`. The loaded environment was forwarded to the GitHub service. No secret values or credentials were written to the report or model transcript.

The request preserved the no-write constraint and failure-log acceptance condition. It supplied only `initial_action=list_runs` and `requested_actions=[list_runs]`; logs were not unconditionally forced when no failure existed. A gate-only service wrapper exposed the real read catalog and rejected every non-read action before dispatch. The frozen plan contained no write actions. This is an additional gate safety boundary, not a replacement implementation of GitHub reads.

| Attempt | Wall seconds | Actual model calls | Actual GitHub reads | Outcome |
| --- | --- | --- | --- | --- |
| 1 | 46.063 | 4 | list_runs, jobs, logs | passed; raw observations contained cause |
| 2 | 31.203 | 4 | list_runs, jobs, logs | passed after concise evidence/coverage rendering |

The first attempt did not incorrectly stop after one read. It correctly discovered and read the latest failure, but its generic completion message required digging into raw observations. The workflow now derives short error-line excerpts from actual job-linked logs and explicitly lists uninspected failures. The second actual attempt verified this presentation improvement. Both original ledgers remain intact.

## Actual dependency discovery

| Step | Action | Actual arguments | Seconds | Service result |
| --- | --- | --- | --- | --- |
| 1 | `list_runs` | `{"repository": "t1mb2rg/hikari"}` | 2.406 | `ok` |
| 2 | `jobs` | `{"repository": "t1mb2rg/hikari", "run_id": 34382679797}` | 0.875 | `ok` |
| 3 | `logs` | `{"job_id": 102571143205, "repository": "t1mb2rg/hikari"}` | 3.000 | `ok` |

- Repository: [`t1mb2rg/hikari`](https://github.com/t1mb2rg/hikari)
- Latest failed run selected from real results: [34382679797](https://github.com/t1mb2rg/hikari/actions/runs/34382679797)
- Actual failed job: [102571143205](https://github.com/t1mb2rg/hikari/actions/runs/34382679797/job/102571143205), `Hikari isolated marker check`.
- Job/run head: `2bde30352d6b8188efca94396c43435bbfa53c96`.
- Stable action sources: `github-conversation-gate:attempt-2:readonly:step:1`, `:step:2`, `:step:3`.
- The model's final `done` referenced observed steps `[1, 2, 3]`; completion was accepted only from actual service results and the frozen read plan.

## Verified failure cause and coverage

The actual log contains:

```text
2026-09-09T17:24:16.4507460Z cat: docs/hikari-isolated-gate-20260909T172359033110Z.txt: No such file or directory
2026-09-09T17:24:16.4529642Z ##[error]Process completed with exit code 1.
```

This was the isolated integration workflow's first run, triggered when the head branch was created at its base commit before the marker document existed. The missing marker made its test exit with code 1. The later two runs on final head `7b7868d19ee671f59d40214e0e75fd0e3e757321` passed and were the accepted merge evidence in [the GitHub physical gate](GITHUB_PHYSICAL_GATE.md). That report has been corrected to retain this earlier setup failure; no failed history was deleted.

Two older failed runs also appeared in the recent list: `33320232834` and `33320214547`, both from `m7-02-conversation-forge-bridge`. Their job logs were not read in this request, and no cause is claimed for them. The final workflow output explicitly reports that coverage:

```text
GitHub 读取已完成，结果来自实际服务观测
实际任务日志中的错误行：
2026-09-09T17:24:16.4507460Z cat: docs/hikari-isolated-gate-20260909T172359033110Z.txt: No such file or directory
2026-09-09T17:24:16.4529642Z ##[error]Process completed with exit code 1.
尚未读取日志的其他失败运行：33320232834、33320214547
```

The `data.failure_evidence` structure binds every displayed excerpt to its step/source, job ID, run ID and head SHA. Error excerpts remain untrusted remote text, not instructions or permission grants. They are extracted from received log lines; a model-written diagnosis does not substitute for the actual observations.

## Durability, tests and boundaries

Both attempts replayed the exact same source request after completion and returned their saved workflow result with **zero additional model or GitHub calls**. Requests/plans/step decisions/results are persisted in their scratch SQLite workflow ledgers. Unknown writes and permission-bearing operations were not exercised remotely by this read-only gate.

The focused workflow suite passed **38 tests**, including run/job lookup, exact file blob and commit readback, PR/head linkage, source/actor scoping, unknown-outcome no-replay, restart recovery, frozen write authority, prompt injection as remote data, deadline behavior, caller-owned repair handoff, and honest failure-log coverage. The earlier workflow plus GitHub facade suite passed 92 tests before the final evidence-presentation test was added.

All retained files are under `work/github-conversation-gate/attempt-1/` and `attempt-2/`: `request.json`, append-only `events.jsonl`, `workflow_snapshot.json`, `workflow_result.json`, `gate_result.json`, and `state/workflow.db`. No GitHub branch/PR/file, live state, live policy, env file or repository permission was changed. No actual QQ message, deployment, token expansion, commit or push occurred.

This verifies this one natural read-only GitHub sequence and immutable replay; it does not claim all failures were diagnosed, an unrestricted tool agent, production activation, or remote write workflow verification. Final read-only gate completed at `2026-09-09T18:19:34.798761+00:00` (UTC).
