# Hikari Architecture

## Overview

Hikari is designed as a long-running personal intelligence system.

The early architecture focuses on establishing a minimal living core.

## Core Components

```
                Hikari Core

        Identity
        Memory
        Reasoning
        Attention
        Runtime

                 ☁️

                 |

        Device / Edge Nodes

        PC
        Phone
        Smart Glasses
```

## v0.1 Loop

```
Event
 ↓
Memory
 ↓
Attention
 ↓
Reasoning
 ↓
Feedback
```

## Main Modules

### Runtime

Keeps Hikari continuously running.

### Event System

Receives changes from the environment.

Initial sources:

- Git
- Calendar
- Device state

### Memory System

Stores:

- Events
- Context
- User model
- Experiences

### Attention Engine

Determines whether an event is meaningful enough to process or present.

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

## Runtime Philosophy

Devices are not Hikari.

They are ways for Hikari to perceive and interact with the world.

The Core preserves continuity across devices and future migrations.
