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
NaturalConversationEngine
       ↓
Natural Context + Memory + User Model + Persona
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
NaturalConversationEngine
```

Platform SDK types remain outside cognition packages. NoneBot / OneBot types belong only to the QQ integration package.

The bridge is a transport edge, not a second brain. It may authenticate callers, normalize platform identifiers, buffer/retry transport work, and report connection health. It does not own personality, memory semantics, prompt construction, action authority, or autonomous participation policy.

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

`NaturalConversationEngine` is the current production conversation implementation. Resident Conversation Host and standalone `hikari-conversation-host` both use this Natural/Jarvis path, with recent conversation plus small selected Natural Context near the current utterance. Durable persistence and User Model assimilation continue behind that boundary.

`ConversationEngine` currently remains as a shared lifecycle/base implementation plus an explicit legacy grounded fallback. Its old `respond()` path builds the historical heavy JSON grounding payload and is not part of the default production Host path. Final naming/decomposition of this compatibility surface is a pre-release cleanup requirement so the production and legacy responsibilities are unambiguous before release.

Historical Whiteboard profiles reuse the production natural conversation lifecycle and vary only prompt/context inputs; they do not maintain a second conversation engine implementation.

### Brain Interface

Abstracts model providers. Models are replaceable cognition components, not Hikari's identity.

### Engineering Runtime

Engineering Runtime is Hikari's internal bounded engineering capability. Forge is no longer an active runtime component in the Resident conversation path.

```text
Conversation engineering intent
        ↓
Capability / authority assessment
        ↓
EngineeringSession
        ↓
Engineering Worker / backend
        ↓
isolated workspace + validation
        ↓
terminal result
        ↓
Hikari delivery / next decision
```

Routine delegated repository work does not require per-action confirmation. High-impact effects remain outside the standing mandate and are escalated.

Conversation itself does not gain direct filesystem perception merely because Engineering Runtime exists. Engineering state and results must come from durable runtime state.

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
