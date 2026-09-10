# Hikari Architecture

## Overview

Hikari is designed as a long-running personal intelligence system.

The architecture keeps presence, conversation, memory, execution, and transport separate so that Hikari can remain one continuous system while devices, models, and adapters change underneath it.

## Core Components

```text
                Hikari

        Identity / Persona
        Memory / User Model
        Awareness / Context
        Attention / Presence
        Conversation
        Engineering Runtime
        Resident Runtime

                 |

        Device / Edge Nodes

        PC
        Phone
        Smart Glasses
        Chat Bridges
```

## Presence Loop

Presence is the ambient observation path. It is for things Hikari notices without the user explicitly starting a conversation.

```text
Sensor Adapter
     ↓
   Event
     ↓
Ambient Context
     ↓
  Memory
     ↓
 Attention
     ↓
 Reasoning
     ↓
 Feedback / Delivery
```

Sensors report changes. Context providers describe current state. Presence decides whether an observed change deserves deeper cognition or interruption.

## Explicit Conversation Path

A direct message from the user is explicit interaction, not a low-priority ambient observation. It therefore does not enter the Presence Attention gate.

```text
Chat Platform / CLI
       ↓
Conversation Transport
       ↓
    UserTurn
       ↓
Conversation Host
       ↓
private ConversationTaskRouter / shared conversation path
       ↓
chat: NaturalConversationEngine + selected context
task: durable request → authorized effect service → evidence
       ↓
 AssistantReply
       ↓
Conversation Transport
```

Conversation is platform-neutral. QQ, a local CLI, or future mobile/voice clients must not create separate Hikari identities or separate cognition implementations.

For QQ the current process boundary is:

```text
QQ
 ↓
NapCat
 ↓ OneBot V11
QQ Bridge
 ↓ hikari.conversation.v1
Conversation Host
 ↓
private TaskRouter / shared Natural conversation
```

Platform SDK types remain outside cognition packages. NoneBot / OneBot types belong only to the QQ integration package.

The bridge is a transport edge, not a second brain. It may authenticate callers, normalize platform identifiers, buffer/retry transport work, and report connection health. It does not own personality, memory semantics, prompt construction, action authority, or autonomous participation policy.

QQ private users, groups, and group participants have separate configured allowlists. A group turn requires an approved group and participant, an @Hikari mention, and otherwise plain text. It remains shared scope; membership does not grant the private task router, private memory, Engineering, GitHub, or growth authority. Stable transport source identifiers bind accepted work to its originating private principal.

## Natural Context Boundary

Hikari may internally know far more than should appear in one conversation turn.

Conversation context therefore follows a selection boundary:

```text
Runtime state ───────┐
Current project ─────┤
Conversation recall ─┤
User Model ──────────┤
Awareness ───────────┘
        ↓
Natural Context selection
        ↓
only facts relevant to this turn
        ↓
Conversation generation
```

Persona Core determines how Jarvis speaks. Epistemic Boundary determines what may be claimed as fact. Natural Context determines which trusted facts are available for the current turn. These responsibilities remain separate.

## Main Modules

### Resident Runtime

Keeps Hikari continuously running and supervises long-lived local components such as Conversation Host, QQ Bridge, Engineering Worker, Presence, and selected runtime guards.

### Event System

Receives changes from the environment through interchangeable Sensor adapters. Sensors normalize observations into Event objects so downstream code does not depend on concrete source SDKs.

### Awareness / Context

Captures bounded ambient state that gives meaning to Events and, when relevant, can later contribute to Natural Context.

Raw context signals must not overclaim what they mean. Keyboard/mouse idle time is an input-activity signal, not proof that the user is present or away. A foreground window describes what the operating system is presenting, not proof of intent or emotional state.

### Memory / User Model

Memory preserves episodes and durable experience. User Model stores currently active stable facts and preferences about the user.

Conversation recall and User Model are intentionally distinct:

```text
Conversation Memory → what happened / what the user said before
User Model          → what is currently stable and relevant about the user
```

Internal metadata such as confidence, provenance, revision, and database identifiers should stay behind the Natural Context boundary unless explicitly needed for diagnostics.

### Attention / Presence

Attention determines whether an ambient event deserves deeper cognition. Presence policy separately determines whether Hikari may interrupt the user now.

Direct user conversation bypasses ambient Attention because the user has already initiated interaction. It still does not grant shell, filesystem, browser, Engineering, notification, or other action authority by itself.

### Conversation

Owns direct, persistent, channel-neutral dialogue.

The default conversation profile is Jarvis, implemented by `NaturalConversationEngine`. Private ingress passes through `ConversationTaskRouter`: an intent resolver sees bounded same-principal discussion and the current request, then emits a validated intent. It can choose chat, clarification, status, engineering, GitHub, a missing-capability request, or invocation of a verified active capability. Historical assistant proposals do not independently authorize execution.

Chat delegates to the Natural/Jarvis engine with selected context; shared scope bypasses private task routing. Status is read from durable records rather than generated from optimistic conversation history. A task records its original source, principal, goal, constraints, acceptance criteria, chosen effect and resulting evidence. Reusing an existing source cannot silently substitute a different request or repeat an uncertain effect. Resident advances accepted work and durable delivery separately from generating the initial acknowledgement.

`ConversationEngine` remains a shared lifecycle/base implementation and an explicit legacy grounded profile. Its historical heavy JSON grounding path is not the default private chat implementation. Whiteboard/grounded profiles are compatibility or experiment choices, not additional identities.

Historical Whiteboard profiles reuse the production natural conversation lifecycle and vary only prompt/context inputs; they do not maintain a second conversation engine implementation.

### Brain Interface

Abstracts model providers. Models are replaceable cognition components, not Hikari's identity.

### Engineering Runtime

Engineering Runtime is Hikari's internal bounded engineering capability. Forge is no longer an active runtime component in the Resident conversation path.

```text
private request + discussion constraints + acceptance criteria
        ↓
durable request / EngineeringGoal with typed steps
        ↓
capability and authority assessment → EngineeringSession / turn
        ↓
Engineering Worker / selected Claude or Codex backend
        ↓
isolated workspace + validation
        ↓
terminal result
        ↓
Hikari delivery / next decision
```

Routine delegated repository work does not require per-action confirmation. High-impact effects remain outside the standing mandate and are escalated.

The executing effect is a typed field, independent of display text. Implemented effects include project inspection, a bounded project command, maintenance, engineering-branch push, and draft-PR publication. Goal, turn, source request, constraints and acceptance criteria survive dispatch and recovery. The Worker owns validation and commits; source baseline changes create fresh sessions. A terminal result is not equivalent to user delivery, which has its own durable outbox state.

Claude remains the default backend; `HIKARI_ENGINEERING_BACKEND=codex` selects the alternative. Their executable/model settings are separate from Conversation. Both adapters require structured `completed`, `blocked` or `failed` reports and preserve actual activity/session evidence. A successful process exit means transport success only. Missing/invalid structured results fail closed, and a permission-blocked result remains blocked. Timeouts terminate the owned process tree. The Codex adapter copies vetted model-provider settings without inheriting desktop session authority, plugins or hooks.

Conversation itself does not gain direct filesystem perception merely because Engineering Runtime exists. Engineering state and results must come from durable runtime state.

### GitHub effect service

`GitHubActionService` exposes a closed catalog of repository/PR/file/Actions reads and scoped branch, file, PR, rerun and merge effects. Runtime configuration supplies allowed repositories and private source identity. The model cannot provide shell commands, credentials, operator policy, ownership receipts or physical-gate evidence. Every accepted write first reserves an immutable receipt; an incomplete or uncertain external outcome is not replayed automatically.

`GitHubPolicyStore` is a separate operator surface with revision-checked saves. Its `github_policy.json` belongs to the original runtime state outside candidate worktrees. Missing policy disables automatic merge. A configured merge requires an explicitly allowed base and actual named checks, Hikari-created PR ownership, the same repository, exact head/base/ref consistency, resolved blocking reviews, unchanged authority/validation paths (including rename sources), and any required exact-head physical acceptance evidence. A qualifying draft can be marked ready only after substantive checks pass, then reassessed before a SHA-conditioned merge. Historical gate PRs #77–79 in `t1mb2rg/hikari` remain user-owned decisions.

Failed Actions reruns require an operator-pinned workflow path and blob SHA. The returned receipt means rerun requested, not CI success. Deployment and permission changes are not conversational actions. Ordinary tests and reusable pure capabilities do not grant those effects.

### Private capability growth

`CapabilityGrowth` persists missing-capability requests, private ownership, typed contracts, immutable acceptance examples and the original input before dispatching an isolated Engineering turn. The recipe domain composes host-owned pure text/list operations with no external permissions. Growth validates the committed candidate diff, captures an immutable digest, and runs both original and candidate examples itself.

The recipe lifecycle is `requested → implementing → candidate_tested → active → resumed`. `candidate_tested` has `live=false`; activation requires the exact digest and an explicit operator decision. Invocation rechecks the snapshot and original private principal. A restart can recover the original request/result without replacing its identity. Failed attempts retain their evidence.

Native implementation requests may create source candidates, but stop at `candidate_implemented`. Resident does not import generated Python; native validation, runtime extension and deployment remain separate reviewed work. See `docs/CAPABILITY_GROWTH.md` for the executable contract and limits.

### Runtime truth and configuration

Producer-owned observations record component PID, timestamp, actual model/connection outcomes and bounded details. The dashboard combines these with live process checks, Worker heartbeat, Goal/session results, delivery receipts and GitHub reads. Missing, stale or dead-producer observations are `unknown`; a configured model name or open port does not prove successful service.

Dashboard settings are operator-owned dotenv edits with revision conflict detection. Secret values are write-only in the response, existing comments/unknown settings are preserved, and the UI distinguishes saved configuration from process overrides and runtime application. Saving does not mutate process environment or restart services. Local-host and same-origin mutation boundaries protect the dashboard operation surface.

Runtime deployment has three separate coordinates: code checkout, Python/dependency environment, and durable state/env-file paths. New candidate environment IDs bind source checkout/content/revision as well as lock/Python/extras; non-editable builds and validation/promotion checks prevent substituting a changed source. Promoted or running environments cannot be rebuilt in place. An environment `current.json` pointer selects a verified interpreter for the autostart launcher; it does not restore or deploy source files. Candidate validation, controlled startup and permanent autostart migration are recorded separately. See `docs/JARVIS_DAILY_DRIVER.md` for startup, controlled switch and rollback procedures.

## Adapter Boundary

Hikari interacts with the outside world through replaceable boundaries:

```text
Ambient world                    Explicit user chat
     ↓                                  ↓
Sensor Adapters                  Conversation Bridges
     ↓                                  ↓
Presence / Attention             Conversation
     └────────────── Hikari ────────────┘
                       ↓
              Action / Engineering
                       ↓
                     World
```

New sensors should not require changes to Memory or Conversation semantics. New chat platforms should not require changes to Hikari identity, persona, memory semantics, or Engineering authority.

## Transport Reliability

External chat networks and local bridge processes can disconnect. The transport boundary uses stable request identifiers, persistent receipts/spools where necessary, reconnect, and duplicate suppression.

The intended guarantee is **at least once with idempotency guards**. Hikari does not claim perfect exactly-once delivery across every possible process crash boundary.

NapCat QR login and risk-control verification remain human-managed. The configured NapCat Login Guard may perform one bounded restart of the named NapCat task for a durable outage, then requires manual verification if login still does not recover. It must not bypass QR login or platform risk controls.

## Runtime Philosophy

Devices, models, chat platforms, and workers are not Hikari by themselves.

They are ways for one persistent Hikari system to perceive, reason, communicate, and act.
