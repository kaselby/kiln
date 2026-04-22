# Messaging

How agents exchange messages through the daemon — direct sends, channel pub/sub, inbox delivery, and trust resolution.

## Overview

Every Kiln agent has an inbox directory under its home (`<home>/inbox/<agent-id>/`) where incoming messages land as `.md` files with YAML frontmatter. The daemon routes between inboxes: a `Message` tool call opens a Unix-socket connection, sends a single request, gets a single response, and closes. No long-lived client connections; the daemon owns all shared state.

Two routing modes. **Direct** — address another session by agent ID; the daemon resolves their inbox and writes the file. **Channel** — publish to a named channel; the daemon fans out to every subscriber's inbox. Subscriptions are per-session and persist in YAML files, so they survive a daemon restart.

Delivery is asynchronous. A message that lands in an idle session's inbox gets picked up by the harness's inbox watcher between turns and injected as a user message. A message that arrives mid-turn gets picked up by the `inbox_check` PostToolUse hook and injected as `additionalContext` (a short ping, not the full body — the agent reads the file on demand). The two paths use `.read` marker files to dedupe.

## Architecture

```
<daemon_dir>/state/subscriptions/
  channels/<session-id>.yml     # agent-subscribed Kiln channels
  surfaces/<session-id>.yml     # gateway-owned (see gateway.md)

<daemon_dir>/state/channels/
  <channel-name>/history.jsonl  # append-only broadcast log (per channel)

<home>/inbox/<agent-id>/
  msg-<ts>-<rand>.md            # incoming message
  msg-<ts>-<rand>.read          # dedup marker — present = agent has seen it
  _scheduled/                   # scheduler-service fallback drop; promoted on next hook fire
```

Daemon-side actors:

- `KilnDaemon` (`daemon/server.py`) — the socket server. Owns `state.presence` (live sessions) and `state.channels` (subscriptions).
- `ChannelRegistry` (`daemon/state.py`) — in-memory `channel_name → {session_id}`. Rebuilt from files on startup; mutations written through `SubscriptionStore`.
- `EventBus` — daemon-internal pub/sub. Emits `message.direct`, `message.channel`, `channel.subscribed`, etc. Services subscribe to these to bridge outbound (e.g. the Discord adapter listens for channel broadcasts to mirror them into bridged threads).

Agent-side actors:

- `DaemonClient` (`daemon/client.py`) — stateless async RPC. Auto-starts the daemon if the socket is missing.
- `Message` built-in tool — calls into `DaemonClient` (daemon path) with a filesystem fallback (`send_to_inbox`) if the daemon is unreachable.
- `inbox_check` PostToolUse hook — scans the inbox between tool calls, injects `additionalContext`, writes `.read` markers.
- Inbox watcher (harness-side) — delivers messages as full user turns when the session is idle.

### Send flow

```
Agent → Message(action="send", to=... | channel=..., summary, body)
  → DaemonClient.send_direct / .publish
  → Unix socket → _handle_send_direct / _handle_publish
  → KilnDaemon.publish_to_channel (for channel) or _write_inbox_message (for direct)
    ├─ resolve recipient inbox via presence registry → agents registry → ~/.<prefix>/
    ├─ write msg-<ts>-<rand>.md to inbox
    ├─ (channel only) append to <channel>/history.jsonl
    └─ events.emit(message.direct | message.channel)
  → ACK { recipient_count | message: "sent to <to>" }
```

If the daemon isn't reachable, the built-in falls back to direct-to-inbox writes via `kiln.tools.send_to_inbox`. Channel broadcast without the daemon tries a legacy `channels.json` lookup — fine for direct messages, meaningfully degraded for channels.

### Delivery flow (recipient side)

Two non-overlapping windows:

- **Idle** — harness inbox watcher fires between turns. Any unread `.md` is delivered as a proper user turn (full body) and marked `.read`.
- **Mid-turn** — `inbox_check` PostToolUse hook fires after every tool call. Any unread `.md` is summarized as an `additionalContext` ping:

  ```
  [Notification | <header>]
  <absolute path to msg file>
  ```

  `.read` marker written immediately so the watcher doesn't re-deliver. The agent reads the file itself when it wants the body.

Session re-start: subscriptions come back from `subscriptions_dir/channels/<session-id>.yml` via `DaemonClient.restore_subscriptions()`, called asynchronously after daemon attach. The harness tracks its desired set in `_desired_subscriptions` so a lost-then-recovered daemon eventually converges.

## Reference

### Wire protocol

JSON-line over Unix domain socket `~/.kiln/daemon/daemon.sock`. One request, one response, close.

| Type | Direction | Required fields | Notes |
|------|-----------|-----------------|-------|
| `subscribe` | C→D | `channel`, `requester` | Returns `subscriber_count`. |
| `unsubscribe` | C→D | `channel`, `requester` | |
| `publish` | C→D | `channel`, `summary`, `body`, `requester` | Returns `recipient_count`. Excludes sender. |
| `send_direct` | C→D | `to`, `summary`, `body`, `requester` | Writes to recipient's inbox. |
| `list_subscriptions` | C→D | `requester` | Returns `channels: [str]`. |
| `ack` | D→C | `ref`, `status` | Successful mutation. |
| `result` | D→C | `ref`, ...data | Structured query response. |
| `error` | D→C | `ref`, `message`, optional `code` | |

The `requester` envelope is `{"agent": "<agent>", "session": "<agent-id>"}`. Mutating requests without it get `error`. Pure queries (`list_sessions`, `get_status`) omit it.

### Message file format

`.md` files under `<home>/inbox/<agent-id>/`:

```markdown
---
from: <agent-id>
summary: "Spec ready for review"
priority: normal
channel: kiln-docs            # omitted for direct messages
timestamp: 2026-04-21T20:43:32.283701+00:00
source: kiln                  # kiln | agent | <platform> (discord, slack, ...)
trust: full                   # gateway-originated only
discord-user-id: 11637...     # {platform}-user-id, gateway-originated only
---

<body text>
```

`parse_message()` recognizes: `from`, `summary`, `priority`, `channel`, `source`, `trust`, `timestamp`, and any `{key}-user-id`. Other frontmatter fields are ignored. Files without frontmatter are accepted — first line becomes summary, whole content becomes body.

### Notification format

`format_message_source()` produces the header that appears in mid-turn injections:

```
GATEWAY MESSAGE from <user> | source: discord/#general | trust: full (verified ✓) | sent 10:12:03
AGENT MESSAGE from <agent-id> | source: kiln/#design-review | sent 10:20:01
AGENT MESSAGE from <agent-id> | source: kiln/dm | sent 10:20:01
```

### Trust levels

Platform messages carry a `trust` field. For per-platform trust state (set by `security-check` or gateway adapter config), `_resolve_live_trust` overrides the file-recorded trust at read time based on `<home>/state/trust-<platform>.yml`:

| Level | Meaning |
|-------|---------|
| `unknown` | Default for unrecognized senders. |
| `known` | Sender is on the platform's allowlist. |
| `full` | Sender is the trusted owner. |
| `full (verified ✓)` | `full` plus a fresh OTP verification (45-min window). |

Agent-to-agent and purely internal messages (`source: kiln` / `agent`) skip trust resolution — the trust concept only applies to platform-originated traffic.

### Subscription persistence

`SubscriptionStore` (`daemon/state.py`) writes one YAML per session:

```yaml
# subscriptions/channels/<agent-id>.yml
version: 1
agent: <agent>
session: <agent-id>
channels:
  - kiln-docs
  - next-steps
```

Daemon is the single writer. Empty subscription lists remove the file entirely.

## Examples

Subscribe and publish to a channel:

```python
Message(action="subscribe", channel="kiln-docs")
Message(action="send", channel="kiln-docs",
        summary="First draft of messaging.md posted",
        body="scratch/msg-draft.md — §3 still TBD, looking for eyes on the notification format.")
```

Direct message to another agent:

```python
Message(action="send", to="<agent-id>",
        summary="spec question",
        body="Does the skeleton rule mean §4 headings must render even with no content?")
```

Read a queued message (the inbox hook ping names the file; the agent reads it directly):

```bash
# The PostToolUse injection is:
#   [Notification | AGENT MESSAGE from <agent-id> | source: kiln/dm | sent 20:43:32]
#   <home>/inbox/<agent-id>/msg-20260421-204332-2d1d7b.md

# Agent then does:
Read(file_path="<home>/inbox/<agent-id>/msg-20260421-204332-2d1d7b.md")
```

Query the daemon directly (e.g. from a shell tool):

```python
import asyncio
from kiln.daemon.client import DaemonClient

client = DaemonClient(agent="<agent>", session="<agent-id>")
channels = asyncio.run(client.list_subscriptions())
```

Poll for unread messages from the shell:

```bash
ls "$AGENT_HOME/inbox/$KILN_AGENT_ID/" | grep '\.md$' | while read f; do
  [ -f "$AGENT_HOME/inbox/$KILN_AGENT_ID/${f%.md}.read" ] || echo "unread: $f"
done
```

## Conventions

- **`summary` is for notifications; `body` is for detail.** The summary appears in the header; the body is only read when the agent chooses to. Keep the summary one line.
- **Subscribe early, unsubscribe rarely.** Channel subs are cheap (one file, one set entry). Leaving a stale subscription around until session end is fine.
- **Use channels for broadcast, not 1:1 fanout.** A `send_direct` per recipient is fine for small N; a channel is cleaner beyond 3-4 subscribers.
- **Reply in the same mode you received.** Channel → reply on the channel. DM → reply with `to=`. Mixing looks like you didn't read the context.
- **Don't hand-write `.md` files into someone else's inbox.** The filesystem fallback path exists for daemon-down emergencies; routing through the daemon is strictly preferable because it writes channel history, emits events, and respects presence.

## Gotchas

- **Filesystem fallback skips channel history.** When the daemon is down, `Message(action="send", channel=...)` falls back to legacy direct-to-inbox writes via `channels.json`. The canonical `channels/<name>/history.jsonl` log doesn't get the entry. Adapters that mirror via the event bus also miss it. Bring the daemon back up before sending anything that needs to appear in channel history.
- **`.read` markers are written by both the hook and the watcher.** The hook writes them eagerly so the watcher won't re-inject; the watcher writes them after actually delivering. If you see a message with a `.read` marker but no injection, the hook won before the watcher got to the body — the agent has to `Read` the file manually.
- **Agent ID prefix drives inbox resolution.** `<agent>-*` routes to `~/.<agent>/inbox/`, etc. Prefix is looked up in the agents registry first (`~/.kiln/agents.yml`) and falls back to `~/.<prefix>/`. An unregistered prefix with no matching directory gives `unknown_recipient`.
- **Subscription restore is async and needs the daemon up.** On session start, the harness kicks off `restore_subscriptions` after attach; if the daemon is still coming up, the first few inbound messages miss. The gateway DM path has a retry-plus-verify flow for this — see `gateway.md`.
- **`_scheduled/` promotion runs inside `inbox_check`.** The scheduler service writes late-firing messages to `<inbox>/_scheduled/` as a durability fallback. The check hook promotes them to the main inbox on next fire. If the agent never calls a tool, they sit there — heartbeat usually unsticks it.
- **Platform messages carry `source != "kiln"`; agent messages don't.** Trust resolution (`_resolve_live_trust`) only fires when `source` names a platform. A malformed message with `source: kiln` but a `discord-user-id` field won't get trust-evaluated. This is defensive: agent-authored messages are inherently trusted at their authoring session's trust level.
- **Session ID collisions mean inbox collisions.** The harness cleans inbox files on fresh-session start (`_clean_stale_agent_state`) precisely because two sessions with the same `<agent-id>` name across days would share `<home>/inbox/<agent-id>/`. Continuation sessions skip this cleanup — they inherit their parent's unread inbox on purpose.
