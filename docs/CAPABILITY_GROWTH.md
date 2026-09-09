# Private capability growth

Hikari can turn a missing capability into a durable implementation request, send
that request through the existing isolated Engineering Worker (configured Codex
or Claude backend), inspect the resulting files, and register a candidate with
actual evidence. The supported executable domain is deliberately small: new
compositions of Hikari-owned pure text and line-list operations. This is useful
for tasks such as extracting and deduplicating TODO items from supplied notes,
counting them, and returning a typed summary.

This does not make arbitrary natural-language requests automatically executable.
New filesystem, network, messaging, credential, native-code, or permission-bearing
services require an operator-reviewed runtime extension. Native implementation
requests are accepted and routed to Engineering, but remain implementation
candidates until that separate validation/deployment work is done.

## Ownership and durable request identity

`capabilities.CapabilityGrowth` owns a SQLite request ledger, append-only evidence
events, immutable candidate snapshots, and explicitly activated versions. It
uses the existing `EngineeringSessionStore` and Worker; it does not run a second
coding agent or import model-generated Python in Resident.

Requests require a stable source reference and a private `UserTurn`. The exact
source text, channel, conversation, actor, scope, constraints, schemas, executable
acceptance examples, requested capability version, and optional original inputs
are persisted before dispatch. Reusing the same source reference with any changed
content fails. Shared turns cannot request, invoke, or resume capability growth.
Invocation also checks the original private conversation and actor; actor loss is
not treated as ownership. Each `private.<name>` version belongs to one request.
Changing implementation content requires an explicit new version; another request
cannot replace an existing version.

Implementation attempts use deterministic session/turn IDs with a narrow existing
project maintainer authority: repository read/write and commands/tests in the
isolated Engineering workspace. Network, publication, and outside-repository
authority remain false. Source intent is embedded as data in structured context,
alongside an exact candidate file boundary. Worker owns its usual isolated-branch
commit lifecycle. Growth independently rejects a completed candidate if its real
Git diff includes any other file. It never merges, pushes, or deploys that branch.

## State and evidence

The normal recipe path is:

`requested → implementing → candidate_tested → active → resumed`

Native candidates stop at `candidate_implemented`. `failed` and `blocked` preserve
their prior evidence; `retry()` increments an attempt without changing the source
request or deleting old outcomes. Retrying is explicit, so an unchanged failure
does not create an infinite coding loop.

An Engineering `completed` label or "all tests passed" sentence is insufficient
for `candidate_tested`. Growth verifies a separate worktree belonging to the owned
Git repository, matching branch, baseline ancestry, clean committed artifacts, and
the allowed file diff. It snapshots committed content and file hashes, and then
runs the recipe itself using the trusted interpreter against both the immutable
acceptance cases and the candidate's extra cases. Every comparison records actual
and expected values in the private event ledger. Failed candidate provenance and
failed comparisons remain available through `events(request_id)`.

`candidate_tested` explicitly has `live=false`. Operator activation requires the
exact stored candidate digest plus an operator decision reference. Invocation
rechecks the stored snapshot digest and calls the recipe interpreter using that
snapshot. Later workspace edits cannot replace the active implementation. No
`available=true` field creates a callable capability.

## Recipe contract, version 1

Candidates occupy:

`capabilities/candidates/private.<name>/v<integer>/recipe.json`

`capabilities/candidates/private.<name>/v<integer>/tests.json`

The closed recipe object contains exactly:

```json
{
  "format": "hikari.recipe.v1",
  "capability_id": "private.unique_lines",
  "version": 1,
  "owner": "hikari.private",
  "permissions": [],
  "input_schema": {
    "type": "object",
    "properties": {"text": {"type": "string"}},
    "required": ["text"],
    "additionalProperties": false
  },
  "output_schema": {"type": "array", "items": {"type": "string"}},
  "steps": [
    {"id": "lines", "service": "text.lines", "args": {"text": {"ref": "input.text"}}},
    {"id": "unique", "service": "lines.unique", "args": {"lines": {"ref": "lines"}}}
  ],
  "return": {"ref": "unique"}
}
```

`tests.json` is a nonempty array of `{"input": ..., "expected": ...}` objects.
Input/output schemas support only `string`, `integer`, `boolean`, `array` with
`items`, and closed `object` with `properties`, `required`, and
`additionalProperties:false`. Unsupported schema keywords fail rather than being
silently ignored. The runtime bounds JSON size, nesting, list length, and steps
(32 maximum), rejects unknown services/arguments, and exposes no loops, imports,
attributes, Python expressions, or effectful services.

The service catalog returned by `describe()["services"]` is authoritative:

| Services | Arguments | Result |
|---|---|---|
| `text.lines` | `text` | List of lines |
| `text.lower`, `text.upper`, `text.trim` | `text` | Text |
| `lines.containing`, `lines.starting` | `lines`, `text` | Filtered list |
| `lines.strip_prefix` | `lines`, `prefix` | List with leading prefix removed |
| `lines.trim`, `lines.nonempty`, `lines.unique`, `lines.sorted` | `lines` | Transformed list |
| `lines.join` | `lines`, `separator` | Text |
| `lines.count` | `lines` | Integer |

Expressions contain literals, arrays, objects, or exact `{"ref":"input.field"}` /
`{"ref":"earlier_step"}` references. Object properties are JSON keys, never Python
attributes. All existing services are pure, deterministic, and permission-free.
Natural-language constraints still require a good acceptance specification; a
finite example suite does not prove every semantic interpretation of a request.

## Integration API

```python
growth = CapabilityGrowth(
    state_dir / "capability_growth.db",
    engineering_store=engineering_store,
    repository=repository,
    implementation_enabled=operator_configuration,
)
request = growth.request(
    source_ref=transport_request_id,
    turn=private_user_turn,
    capability_id="private.unique_lines",
    version=1,
    implementation_kind="recipe",  # or "native"
    input_schema=input_schema,
    output_schema=output_schema,
    acceptance_cases=[{"input": {"text": "a\na"}, "expected": ["a"]}],
    constraints=["Preserve the first-seen line order"],
    resume_input={"text": "original\noriginal"},
)
growth.advance(request["request_id"])
# Resident can call growth.advance_all() alongside normal Engineering maintenance.
# The existing Worker performs the queued implementation.
```

`request`, `advance`, `get`, `retry`, `operator_activate`, and `resume` return JSON
mappings with `request_id`, `status`, `capability_id`, `version`,
`implementation_kind`, `candidate_digest`, `evidence`, source identity, and
`result`. `list_requests()` and `events(request_id)` expose private audit state;
`describe(turn=private_turn)` exposes supported domains, only that exact private
conversation/actor's registry status, and validated active interfaces with their
input/output schemas. Shared calls are rejected. Unscoped `describe()` is reserved
for trusted operator/dashboard inspection and must never enter a model prompt.
These are private
operator/dashboard APIs; their source content must not be inserted into shared
conversation context.

After operator review, trusted administration may call:

```python
growth.operator_activate(request_id, approved_digest=reviewed_digest,
                         operator_ref="operator decision reference")
result = growth.invoke("private.unique_lines", {"text": "a\na"},
                       turn=same_private_owner, version=1)
resumed = growth.resume(request_id, turn=same_private_owner)
```

Do not expose `operator_activate` as a model-requested tool. There is no automatic
activation policy in version 1. If one is added, its permission-free domain and
approval criteria must come from operator configuration, not model wording.
`resume` uses immutable original inputs and records the original source reference
and result digest once; restart/replay returns the durable result. It cannot
substitute new arguments or resume in another conversation. Delivery of this
result remains the existing private transport/outbox owner's responsibility.

## Native implementation seam and current limitation

`implementation_kind="native"` instructs Engineering to create these exact files
in the same versioned directory:

- `implementation.py`, exposing `invoke(inputs)`;
- `test_implementation.py`, containing meaningful acceptance and edge-case tests;
- `manifest.json`, with exactly `format="hikari.native.v1"`,
  `owner="hikari.private"`, capability identity/version, the immutable input/output
  schemas, `entrypoint="implementation.py:invoke"`, and an explicit `permissions`
  list.

The native candidate registry stores the committed source snapshot, hashes, Git
provenance, and the real Engineering terminal record. Host validation is AST syntax
inspection only; `validation.passed` is `null`, never `true`. Native code is never
imported or executed by Growth/Resident, and `operator_activate` rejects native
candidates. Backend-reported tests remain backend evidence, not independently
verified runtime validation.

The extension seam is an operator-reviewed release that adds a typed bounded host
service/adapter to the trusted runtime, independently runs native tests in an
appropriate constrained execution environment, and records build/deployment
identity before activation. A follow-up can then use that reviewed service in a
recipe. Installing a generated Python module directly into Resident or treating a
native manifest's permissions as grants is explicitly unsupported.

## Verification

`tests/test_capability_growth.py` fakes the coding backend seam while using real
temporary SQLite ledgers, Git repositories/worktrees/commits, committed candidate
files, and runtime execution. Coverage includes actual TODO extraction, immutable
source replay, explicit activation and resume after restart, shared/principal
boundaries, failed acceptance evidence, permission/schema/scope rejection,
metadata-only completion rejection, registry tampering, native no-execution, and
version ownership. All repositories and commits in these tests are synthetic;
the user's repository, services, and live data are not touched.
