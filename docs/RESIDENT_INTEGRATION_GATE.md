# Resident production integration gate

Executed 2026-09-10 (Asia/Shanghai). **Attempt 3 passed** the real managed-Resident path.

## Scope

Each attempt used a new scratch Git fixture and a new state directory. The real `resident.app` process ran with `--reasoner simple --output console --no-qq`, private WebSocket port `18765`, and the Codex engineering backend. No application services were replaced or restarted.
The existing model `.env` was read only. Child credentials were supplied in process environment; scratch `.env` files contain a pointer and nonsecret overrides only. Conversation used the configured `deepseek-chat` route. QQ/NapCat, deployment, remote publication, and GitHub mutations were excluded.

## Actual timings

Engineering result/outbox timings are measured from the one engineering request; startup and greeting have their own clocks.

| Attempt | Fresh listener + Worker | Greeting | Accepted reply | Durable result | Durable outbox | Outcome |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| 1 | 1.27 s | 2.91 s | 1.45 s | 88.17 s | 89.19 s | Failed: backend misclassified Hikari-owned commit as blocked; greeting guard also failed |
| 2 | 1.25 s | 2.12 s | 2.02 s | 42.36 s | 44.39 s | Engineering passed; greeting guard provider error remained |
| 3 | 1.03 s | 2.38 s | 1.94 s | 39.31 s | 40.33 s | Passed |

## Attempt 3 evidence

- Authenticated WebSocket handshake returned `hello_ack`; a normal greeting returned in 2.38 seconds.
- Greeting receipt and immutable extraction job were already durable when the reply arrived. Its background job subsequently completed in one attempt.
- The Worker import trace named the candidate checkout `engineering/worker.py`, rather than the older editable install. Fresh Conversation telemetry reported `engineering_enabled=true`, `qq_enabled=false`, and port 18765; the Worker heartbeat identified Resident ownership.
- Exactly one engineering request produced one goal/turn attempt. The managed Worker created `resident_gate.txt`, verified its exact 24 UTF-8 bytes `resident-managed-gate-ok` with no newline/BOM, and Hikari committed only that file.
- Commit: `a1029ba007e22baf2c647de30cc151050652c5d3` on `hikari/engineering/b34d0d69b603411f81b1c0fdf9ec0ce1`.
- Durable turn `f158514014375688ae60b86035ec652e`, goal `831553984c124b9fa5a2ae6100bb8e93`, and source-linked task receipt all reached `completed` without a user resend.
- Exactly one terminal outbox item existed: `engineering-goal:831553984c124b9fa5a2ae6100bb8e93`. Its state remains `pending` because QQ transport was intentionally disabled; actual QQ delivery is not claimed.
- The source fixture remained clean; all code/file work occurred in its isolated engineering worktree.

## Failures found and repairs checked

1. Attempt 1 exposed a backend-stage contract error: after editing, Codex marked the task blocked because the human goal required a commit while the backend was forbidden to commit. The backend contract and maintainer prompt now distinguish completed edit/validation work from Hikari-owned later commit/publication. Attempts 2 and 3 produced real local commits.
2. Both early greetings reached the guard but failed with HTTP 400: `Prompt must contain the word 'json' in some form to use 'response_format' of type 'json_object'.` A scratch-only provider trace showed a normal proposed greeting; this was a request-schema error, not an unsupported action. Adding explicit `Return JSON only` yielded `supported:true` in the real-builder probe and a normal greeting in attempt 3.
3. Attempt 2 terminal model wording deferred a commit that machine evidence already showed complete. Terminal rendering was made deterministic while attempt 3 was running. The already-started attempt 3 used its earlier imported renderer and happened to report completion correctly. After shutdown, the latest renderer was separately checked against attempt 3 immutable result: zero model calls, existing commit evidence preserved, 0.018 ms. The historical outbox was not overwritten.
4. Attempt 1 requested an unspecified newline; CRLF therefore cannot be counted as a product error. Attempts 2 and 3 requested no newline and passed exact-byte verification.

## Cleanup and limits

Every attempt stopped only its captured test process tree. Post-stop PID inventories found no captured Resident/Worker/conhost process still alive; attempts 2 and 3 also verified port 18765 was closed. Attempt 3 captured PIDs: `2552, 59400, 61032, 61392, 67596, 68244`.
These were targeted forced process-tree stops after result/outbox persistence, not a demonstration of graceful production shutdown. No live state, NapCat, permanent autostart, or production environment was modified.
This gate proves a fresh managed service can accept one private request and finish it. It does not claim live QQ delivery, external publishing, or crash/restart recovery; those need their separate evidence.

## Local evidence locations

- Attempt 1: `C:\Users\29719\Documents\Codex\2026-09-09\ni-x\work\resident-gate-20260910-021813` (`gate-evidence.json`, `resident.log`, state databases, Worker log, and fixture worktree).
- Attempt 2: `C:\Users\29719\Documents\Codex\2026-09-09\ni-x\work\resident-gate-20260910-022158` (`gate-evidence.json`, `resident.log`, state databases, Worker log, and fixture worktree).
- Attempt 3: `C:\Users\29719\Documents\Codex\2026-09-09\ni-x\work\resident-gate-20260910-022517` (`gate-evidence.json`, `resident.log`, state databases, Worker log, and fixture worktree).
- Latest deterministic terminal rendering check: `C:\Users\29719\Documents\Codex\2026-09-09\ni-x\work\resident-gate-20260910-022517\terminal-render-check.json`.
- Guard diagnosis/fix probes: `work/resident-guard-probe-022313` and `work/resident-guard-fixed-022503` (synthetic outputs only; no credentials).
