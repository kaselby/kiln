# Gateway

The daemon-hosted service that bridges agents to external platforms — Discord today, designed for more. Covers the service model, platform adapters, surface subscriptions, and the inbound/outbound routing paths.

## Overview

The gateway is one of the daemon's optional services (see `lifecycle.md` for the daemon lifecycle generally). When enabled, it registers a set of platform-facing RPC handlers (`send_user`, `subscribe_surface`, `platform_op`, etc.), loads one or more **adapters** (per-platform plugins), and holds the surface-subscription and bridge state that ties Kiln sessions to platform identities. When disabled, the daemon has zero platform vocabulary — none of those RPC types resolve, no outbound `gateway` commands work.

An **adapter** (`kiln.daemon.adapters.*`) is the bridge between one external platform and the daemon. It owns its own network client (e.g. a `discord.Client` instance), classifies inbound platform events into Kiln-shaped routing decisions, delivers to inboxes via the gateway service, and consumes daemon events from the `EventBus` to mirror outbound traffic (Kiln channel broadcasts → Discord threads, for example).

A **surface** is the adapter's opaque addressable target — a DM with a user, a specific channel, a thread. Surface refs are canonical strings like `discord:user:116377049349881863` or `discord:channel:1396837261841432636`. Sessions subscribe to surfaces just like they subscribe to Kiln channels, but the subscription lives in the gateway's registry, not the core channel registry.

## Architecture

```
<daemon_dir>/
  state/
    subscriptions/surfaces/<session-id>.yml    # durable surface subs
    discord/
      branch-threads.json                       # session-id → discord thread id
      channel-threads.json                      # kiln channel → discord thread id
      <adapter state files>

daemon/
  KilnDaemon                   # owns EventBus, core RPC, presence
    services["gateway"]        # GatewayService — registered only if enabled
      adapters["discord"]      # DiscordAdapter — instantiated per enabled platform
      surfaces (Registry)
      bridges (Registry)
```

### Service layering

```
┌────────────────────────────────────────────────┐
│ Agent session                                   │
│   Bash → gateway tool → DaemonClient           │
└───────────────┬────────────────────────────────┘
                │ Unix socket RPC
┌───────────────▼────────────────────────────────┐
│ KilnDaemon                                      │
│   core handlers (subscribe / publish / ...)     │
│   service-registered handlers:                  │
│     send_user, subscribe_surface, platform_op   │
│   EventBus (fan-out to services + adapters)     │
└───────────────┬────────────────────────────────┘
                │
┌───────────────▼────────────────────────────────┐
│ GatewayService                                  │
│   SurfaceSubscriptionRegistry                   │
│   BridgeRegistry                                │
│   adapters: {platform → PlatformAdapter}        │
└───────────────┬────────────────────────────────┘
                │
┌───────────────▼────────────────────────────────┐
│ DiscordAdapter                                  │
│   discord.Client (gateway WS + REST)            │
│   branch threads / channel threads / DM routing │
└─────────────────────────────────────────────────┘
```

Startup order: `KilnDaemon.start()` brings up the socket server, then `_start_services()` instantiates `GatewayService`, then the gateway's `start()` registers RPC handlers *before* `_start_adapters()`. Adapters subscribe to the `EventBus` only after their own client is connected so half-started adapters don't receive events they can't handle.

### Inbound routing

An adapter receives a platform event (e.g. a Discord message in a thread). It classifies the event into one of three `RouteBucket`s:

| Bucket | Meaning | Target | Delivery |
|--------|---------|--------|----------|
| `BRANCH` | Thread is bound to one specific session | `session_id` | Direct inbox write via `deliver_platform_message` |
| `BRIDGE` | Thread mirrors a Kiln channel two-way | `channel_name` | `publish_to_channel` with `source="discord"` (echo-prevented) |
| `SURFACE` | DM or watched channel with surface subscribers | `surface_ref` | `deliver_to_surface_subscribers` — fan-out to every subscribed session |

Classifying to multiple buckets is an invariant violation (`RoutingError`) — every inbound message hits exactly one. The adapter owns the classifier; the gateway just does the delivery.

Delivery writes a `PlatformMessage` to the inbox as a `.md` file with rich frontmatter (`source: <platform>`, `<platform>-user-id`, `<platform>-channel-id`, etc.). See `messaging.md` for the full frontmatter schema and trust resolution.

### Outbound routing

Two independent paths:

- **`send_user`** — explicit outbound DM. Agent calls `gateway discord send @<user> "..."`; the RPC resolves the target via daemon user config and routes to `DiscordAdapter.send_user_message`.
- **Channel mirror** — a Kiln channel broadcast lands on the `EventBus` as `message.channel`. The adapter's `_on_channel_message` handler checks for a bound Discord bridge (registered in `BridgeRegistry`) and posts the message to the mirrored thread. Messages whose `source == "discord"` are dropped to prevent echo.

`platform_op` is the generic escape hatch: agent asks the adapter to do an arbitrary platform-specific thing (`read_history`, `thread_create`, `permission_request`, `voice_send`, ...). Each adapter advertises supported actions via `supports(feature)` and dispatches in `platform_op(action, args)`.

### Branch and channel threads (Discord)

Two Discord-specific bookkeeping structures, persisted alongside adapter state:

- **Branch threads** — `session_id → discord thread_id`. When a session starts, the adapter creates a per-session thread in `#branches` and binds it. All inbound messages in that thread are routed as `BRANCH` to that session. Used by non-canonical sessions for the owner's back-channel interaction.
- **Channel threads** — `kiln_channel → discord thread_id`. When a Kiln channel has an active Discord bridge, messages are mirrored two-way through a thread in `#channels`. Inbound is `BRIDGE`; outbound is echo-prevented channel broadcast.

Both maps are persisted to JSON so restarts don't orphan threads.

## Reference

### Daemon config — `services.gateway`

```yaml
# ~/.kiln/daemon/config.yml
services:
  gateway:
    enabled: true
    adapters:
      discord:
        enabled: true
        platform: discord              # defaults to adapter_id
        guild_id: "123456789"
        channels:                      # friendly name → discord channel/thread id
          general: "..."
          branches: "..."
          channels: "..."
          docs: "..."
          status: "..."
        users:                         # discord user id → user record
          "<discord-user-id>":
            name: <user>
            max_trust: full
        dm_access:                     # who can DM the bot
          mode: allowlist
          allowlist: ["<discord-user-id>"]
        channel_access:
          mode: open
        default_agent: <agent>
        voice_default: ""
        credentials_dir: /path/to/creds
```

### RPC handlers (gateway-registered)

| Type | Purpose | Notes |
|------|---------|-------|
| `send_user` | Outbound message to a configured user | Resolves user → platform → adapter. |
| `subscribe_surface` | Subscribe a session to a platform surface | Canonicalizes `@user` refs. |
| `unsubscribe_surface` | Inverse. | |
| `list_surface_subscriptions` | Query session's surfaces | Optional `adapter_id` filter. |
| `platform_op` | Arbitrary per-platform action | Adapter advertises via `supports()`. |
| `mgmt request_approval` / `resolve_approval` | Permission flow for gated actions | Routed to the single capable adapter. |

### Surface ref canonicalization

Agents can pass `@<user>` as a shorthand surface ref. The gateway's `_canonicalize_surface_ref` resolves it via daemon user config:

```
@<user> → discord:user:<discord-user-id>
```

Resolution lookup order: `user.default_platform` → first entry in `user.platforms`. If no user or no platform, `ValueError` → error response with `code="invalid_surface"`. Adapters further validate and canonicalize via `validate_surface_ref()`:

```
discord:user:<id>        # DM surface
discord:channel:<id>     # channel or thread surface
```

### PlatformAdapter protocol

Every adapter implements `kiln.daemon.adapters.base.PlatformAdapter`:

| Member | Purpose |
|--------|---------|
| `adapter_id` / `platform_name` | Identity strings. |
| `start(daemon)` / `stop()` | Lifecycle. Called by gateway service. |
| `send_user_message(user, summary, body, context)` | Outbound DM. |
| `platform_op(action, args, context)` | Per-platform RPC. |
| `validate_surface_ref(ref)` | Canonicalize / reject surface refs. |
| `supports(feature)` | String-based capability query. |

Adapters subscribe to `EventBus` in `start()` and unsubscribe in `stop()`. They receive *all* events; they filter themselves.

### `gateway` shell tool (agent-side)

The canonical agent-facing CLI. Lives at `tools/core/gateway` in agent homes, invokes `DaemonClient` under the hood.

| Subcommand | Purpose |
|------------|---------|
| `gateway start \| stop \| restart \| status \| logs` | Daemon lifecycle. |
| `gateway ensure` | No-op if running, start otherwise. Safe for startup hooks. |
| `gateway discord send <@user\|#channel> <text\|-> [--attach P]` | Outbound message; up to 10 attachments. |
| `gateway discord read <target> [--limit N]` | History fetch. |
| `gateway discord post <text\|-> [--attach P]` | Post to this session's branch thread. |
| `gateway discord thread create\|archive <channel> <name>` | Thread ops. |
| `gateway discord paste <file> [--thread <name>]` | Post readable text of a file into `#docs`. |
| `gateway discord subscribe\|unsubscribe <surface-ref>` | Surface subs. |
| `gateway discord channels` | List configured channel names. |
| `gateway discord voice send ...` | TTS via adapter. |

Messages containing `$` should be piped via stdin (`echo 'price is $10' | gateway discord post -`) to avoid shell expansion.

### Surface persistence

Gateway surfaces live alongside channel subs in `daemon/state/subscriptions/surfaces/<session-id>.yml`:

```yaml
version: 1
agent: <agent>
session: <agent-id>
surfaces:
  - discord:user:116377049349881863
  - discord:channel:1396837261841432636
```

Daemon is single writer. Gateway rebuilds the registry on startup from these files and writes through on every mutation.

## Examples

Subscribe to the owner's DM surface (canonical sessions typically do this at startup):

```bash
gateway discord subscribe "@<user>"
# resolves to discord:user:<discord-user-id> and subscribes
```

Send the owner a direct message (canonical-only by convention — see `collaboration.md`):

```bash
gateway discord send "@<user>" "Merged the scheduler branch — commit 1f23e77."
```

Post to the current session's branch thread (non-canonical):

```bash
echo "Investigation summary at scratch/dm-delivery.md" | gateway discord post -
```

Publish to a Kiln channel and let the Discord bridge mirror it:

```python
Message(action="send", channel="kiln-docs",
        summary="messaging.md shipped",
        body="Draft at docs/messaging.md — ready for review.")
# If #channels has a thread bound to kiln-docs, the DiscordAdapter
# picks up message.channel from EventBus and mirrors the message.
```

Daemon config for a minimal Discord adapter:

```yaml
services:
  gateway:
    enabled: true
    adapters:
      discord:
        platform: discord
        guild_id: "123456789"
        channels:
          branches: "111"
          channels: "222"
          docs: "333"
        dm_access:
          mode: allowlist
          allowlist: ["116377049349881863"]
```

Custom adapter skeleton:

```python
# kiln/daemon/adapters/slack.py
class SlackAdapter:
    adapter_id = "slack"
    platform_name = "slack"

    async def start(self, daemon): ...
    async def stop(self): ...
    async def send_user_message(self, user, summary, body, context=None): ...
    async def platform_op(self, action, args, context=None): ...
    def validate_surface_ref(self, ref): ...
    def supports(self, feature): return feature in {"send_message", "read_history"}
```

Then in `GatewayService._ADAPTER_REGISTRY`:

```python
_ADAPTER_REGISTRY = {
    "discord": "kiln.daemon.adapters.discord.DiscordAdapter",
    "slack": "kiln.daemon.adapters.slack.SlackAdapter",
}
```

## Conventions

- **One adapter per platform.** The service registers adapters by `platform_name`. A second adapter for the same platform overwrites the first.
- **Subscribe from the session that should receive.** Surface subscriptions are per-session. An orchestrator that subscribes on behalf of another session delivers to itself, not them.
- **Use `@user` refs in agent code.** They survive user ID changes; resolution happens at RPC time against live daemon config. Only canonicalize to `platform:user:<id>` when writing to durable state (which the daemon does for you).
- **Route DMs to the owner via `send` (canonical) or `post` (non-canonical).** Canonical sessions DM directly; non-canonical sessions post to their branch thread, which the owner can interact with. The `gateway` tool enforces this by routing `post` through the session's branch thread lookup.
- **Branch and channel threads are Discord state, not Kiln state.** The daemon's channel and surface registries are the source of truth; the Discord-thread mapping is secondary bookkeeping owned by the Discord adapter. Losing `branch-threads.json` just means the next session starts a fresh thread.
- **Every outbound message should tolerate adapter failure silently.** The gateway logs and proceeds. A Discord-down condition shouldn't block agent work.

## Gotchas

- **Gateway disabled = no platform RPC handlers.** A daemon with `services.gateway.enabled: false` returns `code="no_gateway"` for every platform-related RPC. This is by design — the daemon shouldn't pretend platforms exist when they don't.
- **Adapter config path is nested.** Agent config lives at `services.gateway.adapters.<adapter_id>`, not top-level `adapters`. Older harness code that looked at `config.adapters` silently found nothing — the DM-delivery fix (April 2026) was partly fixing exactly this. Double-check the path before concluding "the adapter isn't loading."
- **Subscription restore races daemon startup.** A session that subscribes to `@<user>` at init time may complete before the Discord adapter has connected. Fix pattern: retry with backoff, then post-subscribe verification against `list_surface_subscriptions`. The harness implements this for desired subscriptions; custom code should do the same.
- **`@user` refs canonicalize at subscribe time, not at delivery time.** A session that subscribed to `@owner` ends up with `discord:user:<id>` in its surface list — not `@owner`. If the owner's Discord ID ever changes in config, existing subscribers don't migrate. Re-subscribe to pick up the new ID.
- **`validate_surface_ref` is adapter-authoritative.** The gateway delegates canonicalization to the adapter; a ref that *looks* correct (`discord:user:123`) still gets rejected if the adapter's validator says no. For Discord this means "well-formed" — it does not verify the ID exists.
- **`RouteBucket` overlap is a hard error.** A thread that's both a branch and has a surface subscriber raises `RoutingError`. Don't subscribe to a branch thread; don't bridge a thread you've also bound as a branch. The adapter logs loudly on overlap — fix the config.
- **Echo prevention relies on `source` in the event.** An adapter that posts to Discord and then sees the resulting Discord event must tag it with `source: "discord"` in the inbound path; otherwise the outbound mirror handler will re-post it and loop. The Discord adapter handles this; custom adapters need to.
- **Single daemon instance.** `_kill_existing_daemon` enforces this — restarting the daemon kills any prior process to avoid two Discord clients posting duplicates. If you see duplicate branch threads, suspect two daemons.
