# Hikari Architecture

## Overview

Hikari is designed as a long-running personal intelligence system.

The early architecture focuses on establishing a minimal living core and then expanding awareness without coupling the core to specific sensors or devices.

## Core Components

```
                Hikari Core

        Identity
        Memory
        Awareness / Context
        Attention
        Reasoning
        Runtime

                 ☁️

                 |

        Device / Edge Nodes

        PC
        Phone
        Smart Glasses
```

## Presence Loop

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
- foreground / focus signals
- schedule state
- other environment state

Context data is namespaced and attached to Events before Memory, Attention, and Reasoning. Context capture is intended to stay cheap and does not invoke a language model.

Chinese lunar date is part of Hikari's own time awareness. It does not depend on Google Calendar or any calendar application's display features.

Raw context signals must not overclaim what they mean. In particular, keyboard/mouse idle time is an input-activity signal, not proof that the user is present or away.

Future user-state inference may combine multiple signals:

```
recent input ───────┐
session / lock state ┤
foreground activity ─┤
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

### Attention Engine

Determines whether an event is meaningful enough to process or present.

The default path should be cheap. Events that do not deserve deeper cognition must not wake the Reasoner.

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
World
  ↓
Sensor Adapters
  ↓
Hikari Core
  ↓
Action / Feedback Adapters
  ↓
World
```

New sensors should not require changes to Memory, Attention, Reasoning, or Feedback code.

## Runtime Philosophy

Devices are not Hikari.

They are ways for Hikari to perceive and interact with the world.

The Core preserves continuity across devices and future migrations.
