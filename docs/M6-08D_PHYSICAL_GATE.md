# M6-08D QQ / NapCat Physical Gate

This gate is intentionally the first point that needs a real QQ login and NapCat session.
Everything before it should be testable in CI without a user account.

## Gate boundary

The physical test verifies only the transport and direct-conversation loop:

```text
QQ private text
  -> NapCat
  -> OneBot V11 reverse WebSocket
  -> hikari-qq
  -> hikari.conversation.v1 WebSocket
  -> hikari-conversation-host
  -> ConversationEngine
  -> reply over the same path
```

It does **not** enable group participation, proactive messaging, autonomous Presence policy,
attachments, voice, files, or NapCat process/login management.

## Before touching NapCat

1. Install/update the repository environment with `scripts/bootstrap.ps1`.
2. Ensure the normal model variables in `.env` are valid.
3. Put the QQ account that will be allowed to talk to Hikari into:

```dotenv
HIKARI_ONEBOT_ALLOWED_USER_IDS=<your-qq-id>
```

4. Keep the first gate on loopback:

```dotenv
HIKARI_CONVERSATION_HOST=127.0.0.1
HIKARI_CONVERSATION_PORT=8765
HIKARI_CONVERSATION_URL=ws://127.0.0.1:8765
HIKARI_ONEBOT_HOST=127.0.0.1
HIKARI_ONEBOT_PORT=8081
```

M6-08D rejects non-loopback server binds. A shared secret/access token is useful for authentication but is not a substitute for TLS. Remote Core deployment belongs behind a future `wss://` / secure ingress boundary.

5. Run the QQ integration assembly preflight:

```powershell
hikari-qq --env-file .\.env --check
```

Pass evidence:

```text
Hikari QQ Bridge check：PASS
```

This initializes NoneBot, registers the OneBot V11 adapter, builds the Hikari bridge runtime and persistent spool, then exits before opening the long-running service.

6. Start the platform-neutral conversation host:

```powershell
hikari-conversation-host --env-file .\.env
```

Expected startup evidence includes the local WebSocket endpoint, configured model and memory path.

7. In a second terminal, start the QQ bridge:

```powershell
hikari-qq --env-file .\.env
```

Expected startup evidence includes:

```text
NapCat Reverse WebSocket: ws://127.0.0.1:8081/onebot/v11/ws
Hikari Conversation Host: ws://127.0.0.1:8765
```

At this point the software-only preflight is complete. The next step requires the real QQ/NapCat session.

## Real-machine action 1: connect NapCat

Open NapCatQQ Desktop and complete QQ login manually. Hikari must not automate login, QR confirmation, risk-control verification, or NapCat process restart.

Add a OneBot V11 **WebSocket client / Reverse WebSocket** connection with:

```text
ws://127.0.0.1:8081/onebot/v11/ws
```

If an access token is enabled, set the same value in NapCat and `HIKARI_ONEBOT_ACCESS_TOKEN`.

Pass condition: the QQ bridge reports a connected OneBot bot without restarting either Hikari process.

## Real-machine action 2: inbound + outbound

From the allowlisted QQ account, send a private pure-text message with a unique marker, for example:

```text
hikari physical gate 001
```

Pass conditions:

- the bridge accepts the message once;
- the message reaches `ConversationEngine` as channel `qq` and the same private conversation;
- Hikari produces one model completion;
- the reply returns to the same QQ private chat;
- the user and assistant turns are persisted in the shared Hikari memory store;
- sending from an unapproved QQ account does not invoke the model or produce a reply.

## Real-machine action 3: disconnect + reconnect

Disconnect the NapCat reverse WebSocket without stopping Hikari.

Pass conditions:

- `hikari-conversation-host` remains alive;
- `hikari-qq` remains alive;
- the link is treated as disconnected/unhealthy rather than crashing the process;
- Hikari does not attempt to restart or re-login NapCat.

Restore the NapCat reverse WebSocket connection.

Pass conditions:

- OneBot reconnects without restarting Hikari;
- a new allowlisted private message can complete the same round trip again.

## Real-machine action 4: duplicate guard

The automated suite already checks request-id and spool deduplication. During the physical gate, confirm that reconnect/retry does not visibly send the same Hikari reply twice for one QQ message.

The transport guarantee is intentionally **at least once with idempotency guards**, not a claim of mathematically perfect exactly-once delivery across every possible process crash boundary.

## M6-08D PASS rule

M6-08D may be marked PASS only after all of the following are observed on a real QQ/NapCat session:

- OneBot reverse WebSocket connects;
- allowlisted private text reaches Hikari;
- one Hikari reply returns to the same QQ conversation;
- unapproved/private-boundary checks remain fail-closed;
- disconnect does not kill Hikari;
- reconnect restores the path;
- no visible duplicate reply occurs for the tested message.

Until then, the implementation can be CI-green and **ready for the physical gate**, but M6-08D itself is not complete.
