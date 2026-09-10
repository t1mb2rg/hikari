# GitHub physical integration gate

The real GitHub gate passed on **2026-09-10 (Asia/Shanghai)**. Hikari created a draft PR through its action facade, verified a real successful Actions check and exact remote document bytes, then used its merge gate to mark the PR ready and squash-merge it into a newly created isolated base branch.

This is a bounded integration check for the GitHub adapter and merge path. It is not evidence that a product feature, production deployment, or the entire Hikari system has passed acceptance.

## Remote evidence

- Repository: [`t1mb2rg/hikari`](https://github.com/t1mb2rg/hikari)
- PR: [#82](https://github.com/t1mb2rg/hikari/pull/82) — final state `closed`, `draft=false`, `merged=true`.
- Base: `hikari/engineering/integration-base-20260909T172359033110Z`
- Head: `hikari/engineering/integration-head-20260909T172359033110Z`
- Verified PR head: `7b7868d19ee671f59d40214e0e75fd0e3e757321`
- Base immediately before merge: `2bde30352d6b8188efca94396c43435bbfa53c96`
- Confirmed squash merge: [`6451c836b8f057456b75c262acbd515db3077a25`](https://github.com/t1mb2rg/hikari/commit/6451c836b8f057456b75c262acbd515db3077a25)
- Both new branch refs were read back and remain available. No cleanup deletion was performed.

| Workflow run | Check/job ID | Exact checked head | Result |
| --- | --- | --- | --- |
| [34382695648](https://github.com/t1mb2rg/hikari/actions/runs/34382695648) | `102571194727` | `7b7868d19ee671f59d40214e0e75fd0e3e757321` | success |
| [34382685461](https://github.com/t1mb2rg/hikari/actions/runs/34382685461) | `102571162541` | `7b7868d19ee671f59d40214e0e75fd0e3e757321` | success |

The required check name was observed from GitHub as `Hikari isolated marker check`; it was not guessed. Both push and pull-request runs reported `completed/success` on the exact head. The job list and the actual completed job log were also read through the facade.

## Exact document verification

The PR changed exactly one added file: `docs/hikari-isolated-gate-20260909T172359033110Z.txt`. GitHub returned blob `e28f886eb56998387137cbfab6dc32a9272405eb` and these exact UTF-8 bytes before merge:

```text
hikari-github-gate:20260909T172359033110Z
```

The content includes a final newline. The same bytes were read again from the isolated base after merge. This remote readback and the real marker check were recorded as the scratch physical-gate evidence for the exact head.

## Authorized scope and isolated setup

The only repository affected was `t1mb2rg/hikari`. Every remote write was preceded by a repository/permission check and validation against the two exact new branch refs or the newly created PR number. The existing `main` tip was `4656a60626d8781a6da9a1b38fe214210a8fd875` both before and after the exercise. No existing development branch or existing PR was written.

The base branch was created from the observed default-branch commit. Trusted test setup added `.github/workflows/hikari-isolated-gate-20260909T172359033110Z.yml` to that new base only; the head branch was then created from that base. The workflow has one marker-check job, `permissions: contents: read`, `persist-credentials: false`, no declared secrets, no environment, and no deployment. Its only test command compares the marker document to the known string. Workflow setup is deliberately outside the conversational action catalog and does not authorize conversational workflow changes.

Only scratch state at `work/github-physical-gate/state` was used. Its operator-owned policy enabled conditional merge exclusively into `hikari/engineering/integration-base-20260909T172359033110Z`, required the observed check name, required physical evidence, and used squash merge. The real gate rechecked ownership, current head/base, reviews, changed paths, readiness, and the policy revision before merging. No live Hikari policy, repository permission, token scope, account permission, or protection setting was changed.

## Facade and durable receipts

Read operations physically exercised: `list_prs`, `read_pr`, `list_runs`, `read_file`, `jobs`, and `logs`.

| Write action | Immutable source step | Receipt state |
| --- | --- | --- |
| `create_branch` | `create-isolated-base` | `completed` |
| `create_branch` | `create-isolated-head` | `completed` |
| `write_file` | `write-marker` | `completed` |
| `create_pr` | `create-isolated-pr` | `completed` |
| `merge_pr` | `merge-isolated-pr` | `completed` |

The scratch database contains `1` owned-PR receipt, `1` exact-head physical-gate receipt, and `1` merge receipt. PR ownership was recorded only after GitHub confirmed the new PR creation. Draft-to-ready was performed within `GitHubMergeGate` after substantive checks passed and before the confirmed merge.

`update_pr` and `rerun_failed` were not remotely exercised in this gate. Both workflows on the final accepted head succeeded. A failed run on the earlier setup head was discovered during the later read-only conversation gate, as recorded below; no rerun was attempted. Their behavior remains covered by focused automated tests; this report does not claim remote verification of those two effects.

## Failures and retained local evidence

No authentication, workflow-scope, network, readiness, or merge failure occurred during this exercise. No unknown or pending write receipt remained. No development-worktree commit or push was performed.

The later [read-only GitHub conversation gate](GITHUB_CONVERSATION_GATE.md) found an initial setup failure that was not inspected by the original final-head observation: [run 34382679797](https://github.com/t1mb2rg/hikari/actions/runs/34382679797), job `102571143205`, checked head `2bde30352d6b8188efca94396c43435bbfa53c96`. Creating the head branch triggered the workflow before the marker-file commit existed. Its actual log says `cat: docs/hikari-isolated-gate-20260909T172359033110Z.txt: No such file or directory` and `Process completed with exit code 1`. This setup run was not acceptance evidence. The later two runs on exact final head `7b7868d19ee671f59d40214e0e75fd0e3e757321` passed and were the evidence used for merge. The earlier statement that there was no failed run available was too broad and has been corrected; the failed run remains retained.

The additional real job-log read initially failed because the installed GitHub CLI refuses terminal escape bytes unless explicitly allowed: `the response contains terminal escape sequences; pass --allow-escape-sequences to output it anyway`. This was a client transport limitation, not a permission denial. The raw-log adapter now opts into transport output and strips ANSI/terminal control sequences before exposing log text. A focused regression test and a repeated read-only call to the same real job both passed. The focused GitHub/governance/draft-PR suite passed **76 tests** after this fix.

Retained evidence under `work/github-physical-gate/`: `manifest.json`, append-only `events.jsonl`, `latest_observation.json`, `job_log_initial_failure.json`, `job_log_result.json`, `final_verification.json`, and scratch `state/github_evidence.db` / `state/github_policy.json`. The manifest and event ledger contain the actual response IDs, verification timestamps, exact authorized refs, and remote write preflights. The isolated PR and branches are also retained on GitHub.

Final remote readback: `2026-09-09T17:30:14.491115+00:00` (UTC).
