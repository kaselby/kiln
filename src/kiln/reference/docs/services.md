# Services

The daemon's optional extension layer — self-contained capabilities that plug into the Kiln daemon at startup, share its event bus, and extend its RPC surface. The gateway (platform bridging) and scheduler (timed triggers) are the two services that ship with Kiln.

## Overview

The core daemon (`KilnDaemon`) owns a small set of primitives: the Unix-socket RPC server, the event bus, presence state, and the channel registry. Everything else — platform adapters, timed triggers, future workloads like metrics collection or task queues — is a **service**.

A service is a small, structurally-typed object that:

- declares a name,
- implements `start(daemon)` / `stop()` / `status()`,
- registers its own RPC handlers and event subscribers during `start()`,
- owns its own state (files, registries, long-running tasks),
- is enabled or disabled per-deployment via daemon config.

The daemon doesn't know what any given service does. It loads the ones the config names, calls lifecycle hooks, and exposes a narrow set of primitives back to them via the `DaemonHost` protocol.

This split keeps the daemon small. It also means optional dependencies (`discord.py`, `croniter`, `PyNaCl`) stay out of the import path when the corresponding service is disabled.

## Architecture

```
┌────────────────────────────────────────────────────────┐
│ KilnDaemon                                              │
│   Unix-socket RPC server                                │
│   EventBus (fan-out pub/sub)                            │
│   presence / channels / subscriptions                   │
│   ManagementActions (spawn, resolve_by_tags, ...)       │
│                                                          │
│   services: { name → Service instance }                 │
│     ├── gateway    (if enabled)                         │
│     ├── scheduler  (if enabled)                         │
│     └── ...                                              │
└────────────────────────────────────────────────────────┘
                         ▲
                         │ DaemonHost protocol
                         │
┌────────────────────────┴──────────────────────────────┐
│ Service                                                │
│   start(host) / stop() / status()                      │
│   registers RPC handlers via host.register_handler     │
│   subscribes to host.events for reactive work          │
│   owns its own long-running tasks and durable state    │
└────────────────────────────────────────────────────────┘
```

### Lifecycle

`KilnDaemon.start()` runs in this order:

1. Bind the socket server. Core RPC handlers (subscribe, publish, presence queries) become answerable.
2. Load durable state from disk (channel subs, bridge registries, service-owned state — each service reads its own).
3. Start the `reconcile` background loop so presence stays fresh.
4. **Instantiate and start services** (`_start_services`). Each configured service is constructed with its raw config dict, registered in `daemon.services[name]` *before* `start()` is called (so sibling services can find each other during init), then started.
5. Emit the initial reconcile event now that services are listening.

Shutdown is the reverse: `stop()` is called on each service in LIFO order before the socket closes, event handlers drain, and the daemon exits.

Services that fail to start (exception during `start()`) are removed from the registry and logged loudly — the daemon itself keeps running. A broken gateway doesn't take down the scheduler.

### Service registry

Services are referenced by dotted string in `ServerDaemon._service_registry`:

```python
_service_registry: dict[str, str] = {
    "gateway": "kiln.services.gateway.service.GatewayService",
    "scheduler": "kiln.services.scheduler.service.SchedulerService",
}
```

Imports are lazy — the module is only loaded when the service is enabled. Adding a new service is one entry here plus one config block; agents don't need to restart their whole world to gain a new capability.

### DaemonHost surface

The `DaemonHost` protocol (`kiln.services.base.DaemonHost`) is the narrow contract between daemon and services. It exposes:

| Member | Purpose |
|--------|---------|
| `state` | Core daemon state — presence, channels. |
| `events` | The `EventBus` — services subscribe to events and emit their own. |
| `config` | Daemon configuration. Read-only; includes `kiln_home` for resolving state file paths. |
| `management` | `ManagementActions` — session lifecycle (`spawn_session`), presence queries (`resolve_by_tags`, `resolve_dm_target`), inbox path resolution. The scheduler's executor is a thin wrapper over this. |
| `services` | `dict[str, Service]` — sibling lookup. Rarely needed, but present for cases where one service's RPC handler needs to delegate to another. |
| `register_handler(msg_type, handler)` | Adds an RPC handler for a message type. Services register during `start()`, unregister during `stop()`. |
| `unregister_handler(msg_type)` | Inverse. |
| `publish_to_channel(channel, sender, summary, body, **kw)` | Core messaging primitive — writes to subscriber inboxes and the channel log. Services use this to broadcast. |
| `resolve_inbox(recipient)` | Resolve a recipient (agent or session id) to an inbox directory. |
| `ensure_session(ctx)` | Register a session in presence. Used by adapters when sessions surface through platform activity. |

`DaemonHost` is a structural protocol — services never import the daemon module directly, which keeps imports acyclic.

## Reference

### Service protocol

```python
from kiln.services.base import DaemonHost


class MyService:
    @property
    def name(self) -> str:
        return "my-service"

    async def start(self, daemon: DaemonHost) -> None:
        # Register RPC handlers, subscribe to events, spin up background tasks.
        daemon.register_handler("my_rpc", self._handle_rpc)
        ...

    async def stop(self) -> None:
        # Cancel tasks, unregister handlers, flush state.
        ...

    def status(self) -> dict[str, Any]:
        # Returned by the daemon's status RPC. Empty dict is fine.
        return {"running": True}
```

Services are duck-typed against `kiln.services.base.Service` — no inheritance needed, just structural compliance. `__init__(config: dict[str, Any] | None)` is called with the service's raw config subtree.

### Daemon config — services

```yaml
# ~/.kiln/daemon/config.yml
services:
  gateway:
    enabled: true
    # ... gateway-specific config (adapters, channels, users, ...)

  scheduler:
    enabled: true
    # schedule_path: ~/.kiln/daemon/state/schedule.yml    # optional override
    # state_path:    ~/.kiln/daemon/state/scheduler-state.json
    # check_interval: 60

  my-service:
    enabled: false
    # ... service-specific fields
```

Every `services.<name>` block is passed as-is to the service's constructor. The only field the daemon itself reads is `enabled` — if falsy, the service is skipped with an info log.

Shorthand form: `services: { my-service: true }` — equivalent to `{enabled: true}`, useful for services with no config.

### Durable state

Services that persist state across restarts put it under `kiln_home/daemon/state/<service>/` by convention. The scheduler uses `daemon/state/schedule.yml` + `daemon/state/scheduler-state.json`; the gateway uses `daemon/state/subscriptions/surfaces/` + `daemon/state/discord/`. Nothing enforces the layout; consistency is the convention.

State files are written atomically (`tmp` → rename) so a crash mid-save doesn't corrupt them. The daemon is the single writer; shell tools that read service state should open read-only.

### Status reporting

The daemon's `status` RPC walks `daemon.services` and calls `status()` on each. Whatever dict you return shows up in:

- `kiln daemon status` output
- Discord status embeds (when the gateway is enabled)
- `gateway status` shell tool

Keep return values small and human-readable — running state, counts, last-check timestamps. Don't dump large registries.

## Examples

### Minimal service

```python
# kiln/services/healthcheck/service.py
from typing import Any

class HealthcheckService:
    def __init__(self, config: dict[str, Any] | None = None):
        self._started_at: float | None = None

    @property
    def name(self) -> str:
        return "healthcheck"

    async def start(self, daemon) -> None:
        import time
        self._started_at = time.time()
        daemon.register_handler("ping", self._ping)

    async def stop(self) -> None:
        pass

    def status(self) -> dict[str, Any]:
        return {"started_at": self._started_at}

    async def _ping(self, request, context):
        return {"status": "ok"}
```

Register it in `ServerDaemon._service_registry` and enable via config. No daemon core changes needed.

### Publishing from a service

```python
# Inside an async handler
await daemon.publish_to_channel(
    channel="alerts",
    sender="healthcheck",
    summary="Daemon healthy",
    body="All services reporting green at 2026-04-22T12:43:30Z.",
)
```

Agents subscribed to `alerts` receive the message; any bridged Discord thread mirrors it via the gateway.

### Reacting to events

```python
async def start(self, daemon):
    self._daemon = daemon
    daemon.events.subscribe("session.started", self._on_session_started)

async def _on_session_started(self, event):
    # event.payload carries session_id, agent, etc.
    ...
```

Subscribe in `start()`; unsubscribe or let the event bus drain during `stop()`.

## Conventions

- **One service per capability.** Don't cram multiple concerns into one service. The gateway and scheduler are cleanly orthogonal — a third service should be too.
- **Never import the daemon module directly.** Use the `DaemonHost` protocol. Services live under `kiln.services.*`; the daemon lives under `kiln.daemon.*`; imports only go one direction.
- **Optional deps go inside service modules.** The daemon's `_start_services` lazy-imports by dotted string. If `croniter` or `discord.py` aren't installed, importing the disabled service should be the only thing that fails — and it won't happen if the service isn't enabled.
- **Daemon is the single writer for service state.** Tools and CLIs read; the daemon (via the service) writes. Atomic writes only.
- **Services own their RPC namespace.** Gateway-registered handler names like `subscribe_surface` are service-owned; the daemon doesn't know about them. A service that stops should unregister its handlers.
- **Status returns should be quick.** The daemon calls `status()` synchronously in response to RPC queries. Don't do I/O there — pre-compute during operation.

## Gotchas

- **`_start_services` swallows exceptions per service.** One failing service logs an exception and is removed; the others continue. If your service isn't doing what you expect, check the daemon logs — it may have errored during `start()`.
- **Service order is config order.** `services` in the config YAML is iterated in dict order (insertion order in Python 3.7+). If service A depends on service B being already registered, list B first. Gateway currently has no ordering dependency on scheduler or vice versa.
- **Enabling a service requires a daemon restart.** There is no hot-reload. Flip the config, run `gateway restart`.
- **Adapter config is nested under the gateway service.** `services.gateway.adapters.discord.*`, not top-level `adapters`. Easy to miss in older config files — see `gateway.md` for the full shape.
- **Lazy-imported service classes surface import errors at enable time.** A syntax error in `kiln.services.foo.service` is invisible until you enable `foo`. CI should import every service module to catch this.
