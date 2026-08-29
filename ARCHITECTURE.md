# Hikari Architecture

## Overview

Hikari is designed as a long-running personal intelligence system.

The early architecture focuses on establishing a minimal living core and then expanding awareness without coupling the core to specific sensors, chat platforms, or devices.

## Core Components

```
                Hikari Core

        Identity
        Memory
        Awareness / Context
        Attention
        Reasoning
        Conversation
        Runtime

                 ☁️

                 |

        Device / Edge Nodes

        PC
        Phone
        Smart Glasses
        Chat Bridges
```

## Presence Loop

Presence is the ambient observation path. It is for things Hikari notices without the user explicitly starting a conversation.

```
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
 Feedback Adapter
```

Sensors report changes. Context providers describe current state.

This distinction keeps Hikari from rebuilding the same lifecycle, storage, attention, and reasoning logic for every new source of information.

## Explicit Conversation Path

A direct message from the user is explicit interaction, not a low-priority ambient observation. It therefore does not enter the Presence Sensor/Attention path.

```
Chat Platform / CLI
       ↓
Conversation Transport
       ↓
    UserTurn
       ↓
ConversationEngine
       ↓
Identity / bounded Memory / Context / Model
       ↓
 AssistantReply
       ↓
Conversation Transport
```

`ConversationEngine` is platform-neutral. QQ, Telegram, Discord, a local CLI, or a future mobile client must not create separate Hikari identities or separate cognition implementations.

For transports that need their own runtime, Hikari uses an explicit process boundary:

```
QQ
 ↓
NapCat
 ↓ OneBot V11
QQ Bridge
 ↓ hikari.conversation.v1
Conversation Host
 ↓
ConversationEngine
```

Platform SDK types remain outside the core. In particular, NoneBot / OneBot types belong only to the QQ integration package and must not appear in `conversation`, `brain`, `memory`, `personality`, or other cognition packages.

The bridge is a transport edge, not a second brain. It can authenticate callers, normalize platform identifiers, buffer/retry transport work, and report connection health. It cannot own Hikari personality, memory semantics, model prompting, action authority, or the decision rules for future autonomous participation.

## Main Modules

### Runtime

Keeps Hikari continuously running.

### Event System

Receives changes from the environment through interchangeable Sensor adapters.

Initial sources:

- Git
- Calendar
- File system
- Device state

All sensors normalize observations into Event objects. Downstream core code does not depend on concrete sensors.

### Awareness / Context

Captures cheap ambient state that gives meaning to an Event.

Context providers may describe:

- local time
- Chinese lunar date
- host / device identity
- recent local input
- foreground window activity
- schedule state
- other environment state

Context data is namespaced and attached to Events before Memory, Attention, and Reasoning. Context capture is intended to stay cheap and does not invoke a language model.

Chinese lunar date is part of Hikari's own time awareness. It does not depend on Google Calendar or any calendar application's display features.

Raw context signals must not overclaim what they mean. Keyboard/mouse idle time is an input-activity signal, not proof that the user is present or away. Likewise, a foreground window describes what the operating system is currently presenting, not proof of the user's focus, intent, or emotional state.

Future user-state inference may combine multiple signals:

```
recent input ─────────┐
session / lock state ─┤
foreground activity ──┤
schedule context ─────┤
other device signals ─┘
          ↓
      User State
```

### Schedule Awareness

Schedule state is vendor-neutral. Hikari consumes a common ScheduleSource contract rather than depending on one calendar service.

Possible adapters include:

- Google Calendar
- Outlook / Microsoft 365
- local ICS / CalDAV
- phone or Huawei calendar bridges
- Hikari-native reminders and plans

The calendar product is an adapter. It is not Hikari's canonical time model.

### Memory System

Stores:

- Events
- Context
- User model
- Experiences
- bounded explicit conversation history

### Attention Engine

Determines whether an ambient event is meaningful enough to process or present.

The default path should be cheap. Events that do not deserve deeper cognition must not wake the Reasoner.

Explicit direct user conversation bypasses this ambient Attention decision because the user's message is already an interaction request. It still does not grant shell, browser, Forge, filesystem, notification, or other action authority.

### Conversation

Owns direct, persistent, channel-neutral dialogue with Hikari.

The core contract is currently expressed through `UserTurn`, `AssistantReply`, and `ConversationEngine`. Remote bridges communicate with the core over the versioned `hikari.conversation.v1` wire boundary. Request receipts provide idempotency for network retries without exposing platform-specific message objects to cognition.

### Brain Interface

Abstracts the reasoning model.

Models are replaceable.

### Forge Integration

Forge acts as Hikari's engineering evolution capability.

Flow:

```
Hikari identifies limitation
        ↓
Growth Proposal
        ↓
Forge implementation
        ↓
Validation
        ↓
New capability
```

## Adapter Boundary

Hikari interacts with the outside world through replaceable boundaries:

```
Ambient world                    Explicit user chat
     ↓                                  ↓
Sensor Adapters                  Conversation Bridges
     ↓                                  ↓
Presence / Attention             ConversationEngine
     └────────────── Hikari Core ───────┘
                       ↓
              Action / Feedback
                       ↓
                     World
```

New sensors should not require changes to Memory, Attention, Reasoning, or Feedback code. New chat platforms should not require changes to `ConversationEngine`, identity, personality, or memory semantics.

## Transport Reliability

External chat networks and local bridge processes can disconnect. The transport boundary therefore uses stable request identifiers, persistent receipts/spools where necessary, reconnect, and duplicate suppression.

The intended guarantee is **at least once with idempotency guards**. Hikari does not claim perfect exactly-once delivery across every possible process crash boundary.

NapCat login and process lifecycle remain user-managed. A QQ bridge can observe connection state, consume heartbeat/events, and actively probe OneBot status after a quiet interval, but it must not attempt to bypass QR login, risk-control verification, or automatically restart NapCat.

## Runtime Philosophy

Devices and chat platforms are not Hikari.

They are ways for Hikari to perceive and interact with the world.

The Core preserves continuity across devices, channels, and future migrations.
