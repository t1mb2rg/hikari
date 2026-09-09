# Capability growth physical gate — 2026-09-10

Result: **passed**, using the real Codex Engineering backend and the real
Engineering Worker. Activation occurred only in a fresh scratch SQLite registry.
No production capability was activated and no production service was deployed.

## Gate scope

The gate started from a new local Git repository under
`work/growth-physical-gate/source`. It contained only the trusted
`capabilities.runtime.RecipeRuntime`, a minimal package initializer, a README,
and ignore rules. No candidate recipe or tests were seeded. The scratch source
had no Git remote.

The executing process set `HIKARI_ENGINEERING_BACKEND=codex` and an explicit
240-second backend timeout. The existing configured Codex provider/authentication
was reused by `CodexEngineeringBackend`; no credentials were written into the
scratch source or evidence. No production Hikari settings were modified.

The private request asked Hikari to learn a TODO-line extractor with input
`{"text": string}` and output `{"items": [string, ...]}`. Its immutable constraints
were exact uppercase `TODO ` at line start, prefix removal, whitespace trimming,
empty-item removal, and deduplication preserving first-seen order. Five immutable
acceptance cases covered ordinary notes and duplicates, trimming/empty items,
nonmatching prefixes, empty input, and Chinese text with CRLF line endings.
The original notes were saved separately as the immutable resume input.

## Actual execution and evidence

| Evidence | Observed value |
|---|---|
| Durable growth request | `b0da8302aa325e44a3ca358ea2792d3c` |
| Original source reference | `scratch-wire:todo-growth:1` |
| Engineering session | `growth-b0da8302aa325e44a3ca358ea2792d3c-1` |
| Engineering turn | `7b0e116d30e85074aebf181ae3f02b7b` |
| Real Codex session | `codex:01a08733-bd17-7b81-b477-73a33446913e` |
| Worker elapsed time | 94.994 seconds |
| Attempts | One; no failed implementation attempt |
| Scratch source baseline | `9076c4d6a1dc7410fcf0d1ed7b6f27653e4c3e9b` |
| Worker-created candidate commit | `955ecfa917f9335598b2ab1297eab8a58fa6cb2e` |
| Candidate digest approved in scratch | `bb9d77bfd7a9a861e6dfd2bd57c3de042791b377755e8bb554ea6bc2ba3d61d9` |
| Recipe file SHA-256 | `d1d39b99bf90e753f422f6182b122b0e7f92897287b0a0c571b729e40a054f5b` |
| Test file SHA-256 | `739cd10db49370de861cb119eb2d303078e48c60bbdc37da86a197a8f3da0eba` |

Codex created exactly two files in its isolated worktree:

- `capabilities/candidates/private.todo_line_extractor/v1/recipe.json`
- `capabilities/candidates/private.todo_line_extractor/v1/tests.json`

The generated recipe calls `text.lines`, `lines.starting`, `lines.strip_prefix`,
`lines.trim`, `lines.nonempty`, and `lines.unique`, then returns the typed object.
Codex created ten candidate test cases and ran its own validation. After the real
Worker committed the isolated result, Growth independently inspected the actual
Git diff and committed file content and ran the host-owned interpreter against
**all 15 cases: 5 immutable acceptance cases plus 10 candidate tests**. Every
actual/expected comparison is retained in `growth_events.json` and the private
SQLite ledger. A backend completion sentence was not used as the validation
criterion.

Growth first recorded `candidate_tested` with `live=false`. The gate then called
the trusted operator API with the exact reviewed digest and the explicit decision
reference `explicit-root-delegation:scratch-physical-gate-only`. Only that scratch
registry became active. A new invocation with `TODO zeta`, `TODO eta`, a duplicate,
and an empty TODO item returned:

```json
{"items": ["zeta", "eta"]}
```

Resuming the immutable original request returned:

```json
{"items": ["call home", "buy tea"]}
```

The gate reconstructed both `CapabilityGrowth` and `EngineeringSessionStore`
objects from disk and resumed again. The entire durable resumed record matched,
including the original source reference and result. The scratch source HEAD
remained at its baseline, its working tree remained clean, and its remote count
remained zero.

## Evidence files retained locally

All gate execution and evidence remain under
`C:\Users\29719\Documents\Codex\2026-09-09\ni-x\work\growth-physical-gate`:

- `run_gate.py`: gate procedure, using the real Worker and backend;
- `request.json`, `queued.json`, `runtime_catalog.json`: immutable input and dispatch;
- `worker_outcome.json`, `candidate.json`: actual Worker result and host validation;
- `scratch_activation.json`, `resumed.json`, `gate_result.json`: activation/invocation/resume evidence;
- `growth_events.json`, `state/growth.db`, and `state/engineering/`: durable request, attempts, comparison results, and backend activity;
- `source/` and `.hikari-engineering-worktrees/`: baseline and actual generated committed artifacts.

The script refuses to overwrite an existing gate source. A future failed attempt
must remain in its request/event history or a new explicitly named scratch gate;
it must not be deleted to present only successful evidence.

## Handoff and privacy follow-up

Growth dispatch now writes `EngineeringTurn.effect="maintain_project"`,
`source_request_id` equal to its durable growth request ID, and typed constraint
and acceptance-criteria fields. The complete original source reference, text,
schemas, constraints, and executable cases also remain in the immutable context.
Recovery accepts older turns with absent additive fields without rewriting their
durable records.

Model context must call `describe(turn=private_turn)`. It filters request evidence
by exact private channel/conversation/actor and exposes verified active interfaces
with their input/output schemas. Unscoped `describe()` is an operator-only API.
Shared descriptions are rejected; another private principal receives none of the
owner's request evidence or callable interfaces.

The focused suite passed **23 tests** and covers physical-state persistence, crash recovery, legacy
handoffs, contract/permission/scope rejection, failed acceptance evidence,
principal-scoped descriptions, explicit activation, and bounded runtime calls.

## What this proves and what remains

This proves the implemented chain from a durable missing-capability request to
real isolated Codex code generation, Worker commit, independently validated
recipe candidate, explicit scratch activation, actual invocation, and durable
resumption of the original input. It proves a bounded pure text/list capability
domain, not universal autonomous software installation.

This gate did not test production QQ delivery, live Resident deployment, a native
Python capability, new permission-bearing services, or a real external effect.
Native requests remain source candidates pending operator-reviewed execution,
validation, and deployment; Resident never imports generated native Python.
Finite acceptance examples also do not prove every natural-language interpretation
of a request. Production activation remains a separate operator-owned action.
