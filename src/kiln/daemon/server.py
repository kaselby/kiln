"""Kiln daemon — Unix socket server, event bus, message routing.

The daemon process. Accepts connections from agent sessions over a Unix
domain socket, manages shared live state (presence, channels, bridges),
routes messages, and hosts platform adapters.

Can be run directly::

    python -m kiln.daemon.server [--background]

Or started via the CLI::

    kiln daemon start [--background]
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import sys
import uuid as _uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

import yaml

from . import protocol as proto
from .config import (
    DaemonConfig,
    load_agents_registry,
    load_daemon_config,
)
from .protocol import PlatformMessage
from .state import (
    BridgeRecord,
    BridgeRegistry,
    ChannelRegistry,
    DaemonState,
    PresenceRegistry,
    SessionRecord,
    SurfaceSubscriptionRegistry,
    get_live_tmux_sessions,
)

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Event bus
# ---------------------------------------------------------------------------

EventHandler = Callable[[proto.Message], Any]


class EventBus:
    """Pub/sub event bus for daemon-internal event distribution.

    Handlers are dispatched as fire-and-forget tasks so slow adapters
    (e.g. Discord network calls) don't block core message routing.
    Errors in handlers are logged, never propagated to the emitter.
    """

    def __init__(self) -> None:
        self._handlers: list[EventHandler] = []
        self._tasks: set[asyncio.Task] = set()

    def add_handler(self, handler: EventHandler) -> None:
        self._handlers.append(handler)

    def remove_handler(self, handler: EventHandler) -> None:
        self._handlers = [h for h in self._handlers if h is not handler]

    async def emit(self, event: proto.Message) -> None:
        """Dispatch event to all handlers as background tasks."""
        for handler in self._handlers:
            task = asyncio.create_task(self._safe_call(handler, event))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    async def drain(self, timeout: float = 5.0) -> None:
        """Wait for outstanding handler tasks to complete, with timeout.

        Called during daemon shutdown to avoid orphaning adapter work.
        """
        if not self._tasks:
            return
        done, pending = await asyncio.wait(self._tasks, timeout=timeout)
        for task in pending:
            task.cancel()
        if pending:
            log.warning("Cancelled %d event handler tasks on shutdown", len(pending))

    @staticmethod
    async def _safe_call(handler: EventHandler, event: proto.Message) -> None:
        try:
            result = handler(event)
            if asyncio.iscoroutine(result):
                await result
        except Exception:
            log.exception("Error in event handler for %s", event.type)


# ---------------------------------------------------------------------------
# Inbox and history helpers
# ---------------------------------------------------------------------------

def _write_inbox_message(
    inbox_root: Path,
    recipient: str,
    sender: str,
    summary: str,
    body: str,
    priority: str = "normal",
    channel: str | None = None,
) -> Path:
    """Write a message to a recipient's inbox directory.

    Matches the format used by kiln.tools.send_to_inbox. These two
    functions should be unified post-refactor — kept separate for now
    because they have different call sites and slightly different
    frontmatter needs.
    """
    recipient_inbox = inbox_root / recipient
    recipient_inbox.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    msg_id = f"msg-{timestamp}-{_uuid.uuid4().hex[:6]}"
    msg_path = recipient_inbox / f"{msg_id}.md"

    channel_line = f"channel: {channel}\n" if channel else ""
    content = (
        f"---\n"
        f"from: {sender}\n"
        f"summary: \"{summary}\"\n"
        f"priority: {priority}\n"
        f"{channel_line}"
        f"timestamp: {datetime.now(timezone.utc).isoformat()}\n"
        f"---\n\n"
        f"{body}\n"
    )

    msg_path.write_text(content)
    return msg_path


def _write_platform_inbox_message(
    inbox_root: Path,
    recipient: str,
    msg: PlatformMessage,
) -> Path:
    """Write a platform-originated message to a recipient's inbox.

    Generates rich frontmatter with platform-specific fields from the
    structured PlatformMessage. This is the daemon-owned counterpart to
    the adapter's identity resolution — the adapter populates the struct,
    the daemon writes the artifact.
    """
    recipient_inbox = inbox_root / recipient
    recipient_inbox.mkdir(parents=True, exist_ok=True)

    now = datetime.now(ZoneInfo("America/Toronto"))
    ts_str = now.strftime("%Y%m%d-%H%M%S")
    msg_id = f"msg-{ts_str}-{msg.platform}-{_uuid.uuid4().hex[:6]}"
    msg_path = recipient_inbox / f"{msg_id}.md"

    first_line = msg.content.strip().split("\n")[0] if msg.content.strip() else ""
    if first_line:
        summary = first_line[:200]
    elif msg.attachment_paths:
        summary = f"(attachment: {', '.join(Path(p).name for p in msg.attachment_paths)})"
    else:
        summary = "(empty)"

    # Build frontmatter as a dict and serialize safely — values come from
    # an external system boundary (Discord usernames, channel names, etc.)
    # and could contain quotes, colons, or newlines.
    fields: dict[str, Any] = {
        "from": f"{msg.platform}-{msg.sender_name}",
        "summary": summary,
        "priority": "normal",
        "source": msg.platform,
        "channel": msg.channel_desc,
        "trust": msg.trust,
        f"{msg.platform}-user-id": msg.sender_platform_id,
        f"{msg.platform}-user": msg.sender_name,
        f"{msg.platform}-channel-id": msg.channel_id,
        f"{msg.platform}-channel": msg.channel_desc,
        "timestamp": now.isoformat(),
    }
    if msg.attachment_paths:
        fields["attachments"] = ", ".join(msg.attachment_paths)

    fm_body = yaml.dump(fields, default_flow_style=False, allow_unicode=True, sort_keys=False)
    frontmatter = f"---\n{fm_body}---\n\n"

    body = msg.content
    if msg.attachment_paths:
        file_lines = "\n".join(
            f"  - {Path(p).name} -> {p}" for p in msg.attachment_paths
        )
        notice = (
            f"ATTACHMENT RECEIVED (auto-downloaded) — verify {msg.sender_name}'s "
            f"account hasn't been compromised before reading file contents.\n"
            f"{file_lines}\n"
        )
        body = notice + ("\n" + body if body.strip() else "")

    msg_path.write_text(frontmatter + body + "\n")
    return msg_path


def _write_channel_history(
    channels_dir: Path,
    channel: str,
    sender: str,
    summary: str,
    body: str,
    priority: str = "normal",
) -> None:
    """Append a message to the shared channel history file."""
    history_dir = channels_dir / channel
    history_dir.mkdir(parents=True, exist_ok=True)

    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "from": sender,
        "summary": summary,
        "body": body,
        "priority": priority,
    }
    with open(history_dir / "history.jsonl", "a") as f:
        f.write(json.dumps(entry) + "\n")


# ---------------------------------------------------------------------------
# Request handler — one request per connection
# ---------------------------------------------------------------------------

async def _handle_request(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    daemon: KilnDaemon,
) -> None:
    """Handle a single request on a short-lived connection.

    Reads one JSON-line request, dispatches, sends one response, closes.
    """
    try:
        line = await asyncio.wait_for(reader.readline(), timeout=10.0)
        if not line:
            return

        try:
            msg = proto.Message.from_line(line)
        except (json.JSONDecodeError, KeyError) as e:
            log.warning("Malformed request: %s", e)
            return

        response = await _dispatch(msg, daemon)
        if response:
            writer.write(response.to_line())
            await writer.drain()

    except asyncio.TimeoutError:
        log.debug("Client connection timed out waiting for request")
    except (ConnectionResetError, BrokenPipeError, OSError):
        pass
    except Exception:
        log.exception("Error handling request")
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except OSError:
            pass


async def _dispatch(msg: proto.Message, daemon: KilnDaemon) -> proto.Message | None:
    """Route a request to the appropriate handler. Returns the response message."""
    handlers: dict[str, Any] = {
        proto.SUBSCRIBE: _handle_subscribe,
        proto.UNSUBSCRIBE: _handle_unsubscribe,
        proto.PUBLISH: _handle_publish,
        proto.SEND_DIRECT: _handle_send_direct,
        proto.SEND_USER: _handle_send_user,
        proto.LIST_SUBSCRIPTIONS: _handle_list_subscriptions,
        proto.SUBSCRIBE_SURFACE: _handle_subscribe_surface,
        proto.UNSUBSCRIBE_SURFACE: _handle_unsubscribe_surface,
        proto.LIST_SURFACE_SUBSCRIPTIONS: _handle_list_surface_subscriptions,
        proto.LIST_SESSIONS: _handle_list_sessions,
        proto.GET_STATUS: _handle_get_status,
        proto.PLATFORM_OP: _handle_platform_op,
        proto.MGMT: _handle_mgmt,
    }

    handler = handlers.get(msg.type)
    if handler:
        try:
            return await handler(msg, daemon)
        except Exception as e:
            log.exception("Error handling %s", msg.type)
            if msg.ref:
                return proto.error(msg.ref, str(e))
    else:
        if msg.ref:
            return proto.error(msg.ref, f"Unknown message type: {msg.type}")
    return None


def _require_requester(msg: proto.Message) -> proto.RequestContext | None:
    """Extract and validate requester identity from request.

    Returns None and the caller should return an error response
    if the requester envelope is missing.
    """
    return proto.RequestContext.from_request(msg)


# ----- Request handlers -----

async def _handle_subscribe(msg: proto.Message, daemon: KilnDaemon) -> proto.Message:
    channel = msg.data.get("channel", "")
    if not channel:
        return proto.error(msg.ref, "subscribe requires a channel name")

    ctx = _require_requester(msg)
    if not ctx:
        return proto.error(msg.ref, "subscribe requires requester identity")

    # Reconcile this session before mutating
    await daemon.ensure_session(ctx)

    count = daemon.state.channels.subscribe(channel, ctx.session_id)

    # Persist to file
    channels = daemon.state.channels.channels_for(ctx.session_id)
    daemon.state.store.write_channel_subs(ctx.session_id, ctx.agent_name, channels)

    await daemon.events.emit(proto.event(
        proto.EVT_CHANNEL_SUBSCRIBED,
        channel=channel,
        session_id=ctx.session_id,
        subscriber_count=count,
    ))

    return proto.ack(msg.ref, subscriber_count=count)


async def _handle_unsubscribe(msg: proto.Message, daemon: KilnDaemon) -> proto.Message:
    channel = msg.data.get("channel", "")
    if not channel:
        return proto.error(msg.ref, "unsubscribe requires a channel name")

    ctx = _require_requester(msg)
    if not ctx:
        return proto.error(msg.ref, "unsubscribe requires requester identity")

    daemon.state.channels.unsubscribe(channel, ctx.session_id)

    # Persist to file
    channels = daemon.state.channels.channels_for(ctx.session_id)
    daemon.state.store.write_channel_subs(ctx.session_id, ctx.agent_name, channels)

    await daemon.events.emit(proto.event(
        proto.EVT_CHANNEL_UNSUBSCRIBED,
        channel=channel,
        session_id=ctx.session_id,
    ))

    return proto.ack(msg.ref)


async def _handle_publish(msg: proto.Message, daemon: KilnDaemon) -> proto.Message:
    channel = msg.data.get("channel", "")
    summary = msg.data.get("summary", "")
    body = msg.data.get("body", "")
    priority = msg.data.get("priority", "normal")

    if not channel:
        return proto.error(msg.ref, "publish requires a channel name")

    ctx = _require_requester(msg)
    if not ctx:
        return proto.error(msg.ref, "publish requires requester identity")

    count = await daemon.publish_to_channel(
        channel, ctx.session_id, summary, body, priority,
    )
    return proto.ack(msg.ref, recipient_count=count)


async def _handle_send_direct(msg: proto.Message, daemon: KilnDaemon) -> proto.Message:
    to = msg.data.get("to", "")
    summary = msg.data.get("summary", "")
    body = msg.data.get("body", "")
    priority = msg.data.get("priority", "normal")

    if not to:
        return proto.error(msg.ref, "send_direct requires 'to'")

    ctx = _require_requester(msg)
    if not ctx:
        return proto.error(msg.ref, "send_direct requires requester identity")

    # Resolve recipient inbox
    prefix = to.split("-")[0]
    agents = load_agents_registry(daemon.config.agents_registry)
    agent_home = agents.get(prefix)

    if not agent_home:
        candidate = Path.home() / f".{prefix}"
        if candidate.is_dir():
            agent_home = candidate
        else:
            return proto.error(
                msg.ref, f"Cannot resolve inbox for '{to}'",
                code="unknown_recipient",
            )

    inbox_root = agent_home / "inbox"
    _write_inbox_message(
        inbox_root, to, ctx.session_id, summary, body, priority,
    )

    await daemon.events.emit(proto.event(
        proto.EVT_MESSAGE_DIRECT,
        sender=ctx.session_id,
        recipient=to,
        summary=summary,
    ))

    return proto.ack(msg.ref, message=f"Message sent to {to}")


async def _handle_send_user(msg: proto.Message, daemon: KilnDaemon) -> proto.Message:
    to = msg.data.get("to", "")
    summary = msg.data.get("summary", "")
    body = msg.data.get("body", "")

    if not to:
        return proto.error(msg.ref, "send_user requires 'to'")

    ctx = _require_requester(msg)
    if not ctx:
        return proto.error(msg.ref, "send_user requires requester identity")

    user = daemon.config.users.get(to)
    if not user:
        return proto.error(msg.ref, f"Unknown user: '{to}'", code="unknown_user")

    platform = user.default_platform
    adapter = daemon.adapters.get(platform)
    if not adapter:
        return proto.error(
            msg.ref, f"No adapter for platform '{platform}'", code="no_adapter",
        )

    try:
        result = await adapter.send_user_message(to, summary, body, context=ctx)
        return proto.ack(msg.ref, message=result)
    except Exception as e:
        return proto.error(msg.ref, f"Adapter error: {e}")


async def _handle_list_subscriptions(msg: proto.Message, daemon: KilnDaemon) -> proto.Message:
    ctx = _require_requester(msg)
    if not ctx:
        return proto.error(msg.ref, "list_subscriptions requires requester identity")

    channels = daemon.state.channels.channels_for(ctx.session_id)
    return proto.result(msg.ref, channels=channels)


async def _handle_subscribe_surface(msg: proto.Message, daemon: KilnDaemon) -> proto.Message:
    surface_ref = msg.data.get("surface_ref", "")
    if not surface_ref:
        return proto.error(msg.ref, "subscribe_surface requires a surface_ref")

    ctx = _require_requester(msg)
    if not ctx:
        return proto.error(msg.ref, "subscribe_surface requires requester identity")

    await daemon.ensure_session(ctx)

    # Validate and canonicalize via the owning adapter
    platform = surface_ref.split(":", 1)[0] if ":" in surface_ref else ""
    adapter = daemon.adapters.get(platform) if platform else None
    if adapter and hasattr(adapter, "validate_surface_ref"):
        try:
            surface_ref = adapter.validate_surface_ref(surface_ref)
        except ValueError as e:
            return proto.error(
                msg.ref, f"Invalid surface ref: {e}", code="invalid_surface",
            )

    count = daemon.state.surfaces.subscribe(surface_ref, ctx.session_id)

    # Persist to file
    surfaces = daemon.state.surfaces.surfaces_for(ctx.session_id)
    daemon.state.store.write_surface_subs(ctx.session_id, ctx.agent_name, surfaces)

    await daemon.events.emit(proto.event(
        proto.EVT_SURFACE_SUBSCRIBED,
        surface_ref=surface_ref,
        session_id=ctx.session_id,
        subscriber_count=count,
    ))

    return proto.ack(msg.ref, subscriber_count=count, surface_ref=surface_ref)


async def _handle_unsubscribe_surface(msg: proto.Message, daemon: KilnDaemon) -> proto.Message:
    surface_ref = msg.data.get("surface_ref", "")
    if not surface_ref:
        return proto.error(msg.ref, "unsubscribe_surface requires a surface_ref")

    ctx = _require_requester(msg)
    if not ctx:
        return proto.error(msg.ref, "unsubscribe_surface requires requester identity")

    # Canonicalize via adapter
    platform = surface_ref.split(":", 1)[0] if ":" in surface_ref else ""
    adapter = daemon.adapters.get(platform) if platform else None
    if adapter and hasattr(adapter, "validate_surface_ref"):
        try:
            surface_ref = adapter.validate_surface_ref(surface_ref)
        except ValueError as e:
            return proto.error(
                msg.ref, f"Invalid surface ref: {e}", code="invalid_surface",
            )

    daemon.state.surfaces.unsubscribe(surface_ref, ctx.session_id)

    # Persist to file
    surfaces = daemon.state.surfaces.surfaces_for(ctx.session_id)
    daemon.state.store.write_surface_subs(ctx.session_id, ctx.agent_name, surfaces)

    await daemon.events.emit(proto.event(
        proto.EVT_SURFACE_UNSUBSCRIBED,
        surface_ref=surface_ref,
        session_id=ctx.session_id,
    ))

    return proto.ack(msg.ref, surface_ref=surface_ref)


async def _handle_list_surface_subscriptions(msg: proto.Message, daemon: KilnDaemon) -> proto.Message:
    ctx = _require_requester(msg)
    if not ctx:
        return proto.error(msg.ref, "list_surface_subscriptions requires requester identity")

    adapter_id = msg.data.get("adapter_id")
    surfaces = daemon.state.surfaces.surfaces_for(
        ctx.session_id, adapter_id=adapter_id,
    )
    subscriptions = [
        {
            "surface_ref": ref,
            "subscriber_count": daemon.state.surfaces.subscriber_count(ref),
        }
        for ref in surfaces
    ]
    return proto.result(msg.ref, subscriptions=subscriptions)


async def _handle_list_sessions(msg: proto.Message, daemon: KilnDaemon) -> proto.Message:
    agent_filter = msg.data.get("agent")
    sessions = daemon.management.list_sessions(agent=agent_filter)
    return proto.result(msg.ref, sessions=sessions)


async def _handle_get_status(msg: proto.Message, daemon: KilnDaemon) -> proto.Message:
    status = {
        "sessions": len(daemon.state.presence),
        "channels": len(daemon.state.channels.all_channels()),
        "surfaces": len(daemon.state.surfaces.all_surfaces()),
        "bridges": len(daemon.state.bridges.all_bridges()),
        "adapters": list(daemon.adapters.keys()),
        "lockdown": daemon.config.lockdown_file.exists(),
    }
    return proto.result(msg.ref, **status)


async def _handle_platform_op(msg: proto.Message, daemon: KilnDaemon) -> proto.Message:
    platform = msg.data.get("platform", "")
    action = msg.data.get("action", "")
    args = msg.data.get("args", {})

    adapter = daemon.adapters.get(platform)
    if not adapter:
        return proto.error(
            msg.ref, f"No adapter for platform '{platform}'", code="no_adapter",
        )

    ctx = _require_requester(msg)

    try:
        result = await adapter.platform_op(action, args, context=ctx)
        return proto.result(msg.ref, **result)
    except Exception as e:
        return proto.error(msg.ref, f"Platform op failed: {e}")


async def _handle_mgmt(msg: proto.Message, daemon: KilnDaemon) -> proto.Message:
    action = msg.data.get("action", "")
    args = msg.data.get("args", {})

    if action == "list_sessions":
        return await _handle_list_sessions(msg, daemon)
    elif action == "get_status":
        return await _handle_get_status(msg, daemon)
    elif action in ("request_approval", "resolve_approval"):
        return await _handle_approval(msg, daemon, action, args)
    else:
        return proto.error(
            msg.ref,
            f"Management action '{action}' not implemented",
            code="not_implemented",
        )


async def _handle_approval(
    msg: proto.Message, daemon: KilnDaemon, action: str, args: dict,
) -> proto.Message:
    """Route approval requests/resolutions to the adapter that supports permissions.

    The requester context scopes the request — the caller cannot target
    a different session's approval state.
    """
    ctx = _require_requester(msg)

    # Find the configured adapter that supports permission approval.
    # Exactly one must exist; ambiguity is an error, not a silent choice.
    capable = [
        (name, adapter) for name, adapter in daemon.adapters.items()
        if hasattr(adapter, "supports") and adapter.supports("permission")
    ]
    if not capable:
        return proto.error(msg.ref, "No adapter supports remote approval")
    if len(capable) > 1:
        names = ", ".join(n for n, _ in capable)
        return proto.error(
            msg.ref,
            f"Ambiguous: multiple adapters support approval ({names}). "
            f"Configure exactly one.",
        )

    _, adapter = capable[0]

    if action == "request_approval":
        # Route to the adapter's permission_request platform op.
        # The session_id comes from ctx, not caller args.
        op_args = {
            "agent_id": ctx.session_id,
            "title": args.get("title", ""),
            "preview": args.get("preview", ""),
            "detail": args.get("detail"),
            "severity": args.get("severity", "info"),
            "timeout": args.get("timeout", 300),
        }
        try:
            result = await adapter.platform_op("permission_request", op_args, context=ctx)
            return proto.result(msg.ref, **result)
        except Exception as e:
            return proto.error(msg.ref, f"Approval request failed: {e}")

    else:  # resolve_approval
        op_args = {
            "session_id": ctx.session_id,
            "status": args.get("status", "rejected"),
        }
        try:
            result = await adapter.platform_op("permission_resolve", op_args, context=ctx)
            return proto.result(msg.ref, **result)
        except Exception as e:
            return proto.error(msg.ref, f"Approval resolve failed: {e}")


# ---------------------------------------------------------------------------
# Daemon server
# ---------------------------------------------------------------------------

class KilnDaemon:
    """The main daemon process."""

    # Platform name -> adapter class. Deferred import avoids pulling in
    # heavy deps (discord.py) unless the platform is actually configured.
    _adapter_registry: dict[str, str] = {
        "discord": "kiln.daemon.adapters.discord.DiscordAdapter",
    }

    def __init__(self, config: DaemonConfig | None = None):
        from .management import ManagementActions

        self.config = config or load_daemon_config()
        self.state = DaemonState(subscriptions_dir=self.config.subscriptions_dir)
        self.events = EventBus()
        self.management = ManagementActions(self.state, self.config)
        self.adapters: dict[str, Any] = {}  # platform_name -> adapter instance
        self._server: asyncio.Server | None = None
        self._reconcile_task: asyncio.Task | None = None

    # ------------------------------------------------------------------
    # Core ingress — shared by socket handlers and adapters
    # ------------------------------------------------------------------

    async def publish_to_channel(
        self,
        channel: str,
        sender: str,
        summary: str,
        body: str,
        priority: str = "normal",
        source: str = "",
        exclude_sender: bool = True,
    ) -> int:
        """Publish a message to a channel.

        Core delivery path used by both socket request handlers and
        platform adapters. Writes to subscriber inboxes, appends
        channel history, and emits the event bus event.

        Args:
            source: Origin tag for echo prevention (e.g. "discord").
            exclude_sender: If True, don't deliver back to sender.

        Returns number of recipients delivered to.
        """
        subscribers = self.state.channels.subscribers(channel)
        recipients = subscribers - {sender} if exclude_sender else subscribers

        # Write to each subscriber's inbox
        for sub_id in recipients:
            inbox_root = self._resolve_inbox(sub_id)
            if inbox_root:
                _write_inbox_message(
                    inbox_root, sub_id, sender,
                    summary, body, priority, channel=channel,
                )

        # Write channel history
        _write_channel_history(
            self.config.channels_dir,
            channel, sender, summary, body, priority,
        )

        # Emit event for adapters (e.g. Discord bridge outbound)
        event_data = dict(
            channel=channel, sender=sender,
            summary=summary, body=body, priority=priority,
        )
        if source:
            event_data["source"] = source
        await self.events.emit(proto.event(proto.EVT_MESSAGE_CHANNEL, **event_data))

        return len(recipients)

    async def deliver_platform_message(
        self,
        recipient: str,
        msg: PlatformMessage,
    ) -> Path | None:
        """Deliver a platform-originated message to a session's inbox.

        The adapter resolves identity and populates the PlatformMessage;
        the daemon owns choosing the write path and emitting events.

        Returns the inbox message path, or None if the recipient
        couldn't be resolved.
        """
        inbox_root = self._resolve_inbox(recipient)
        if not inbox_root:
            log.warning("Cannot resolve inbox for recipient '%s'", recipient)
            return None

        path = _write_platform_inbox_message(inbox_root, recipient, msg)

        await self.events.emit(proto.event(
            proto.EVT_MESSAGE_INBOUND,
            sender=f"{msg.platform}-{msg.sender_name}",
            recipient=recipient,
            summary=msg.content[:200],
            platform=msg.platform,
        ))

        return path

    async def deliver_to_surface_subscribers(
        self,
        surface_ref: str,
        msg: PlatformMessage,
    ) -> int:
        """Deliver a platform message to all sessions subscribed to a surface.

        Called by adapters when an inbound message arrives on a subscribed
        surface. Looks up subscribers in the surface registry and delivers
        to each via ``deliver_platform_message``.

        Returns number of successful deliveries.
        """
        subscribers = self.state.surfaces.subscribers(surface_ref)
        if not subscribers:
            log.debug("No subscribers for surface %s", surface_ref)
            return 0

        delivered = 0
        for session_id in subscribers:
            path = await self.deliver_platform_message(session_id, msg)
            if path is not None:
                delivered += 1

        return delivered

    # ------------------------------------------------------------------
    # Inbox resolution
    # ------------------------------------------------------------------

    def _resolve_inbox(self, recipient: str) -> Path | None:
        """Resolve a recipient's inbox directory.

        Checks presence registry first, then falls back to agents registry.
        """
        record = self.state.presence.get(recipient)
        if record and record.agent_home:
            return Path(record.agent_home) / "inbox"

        prefix = recipient.split("-")[0]
        agents = load_agents_registry(self.config.agents_registry)
        agent_home = agents.get(prefix)
        if not agent_home:
            candidate = Path.home() / f".{prefix}"
            if candidate.is_dir():
                agent_home = candidate
        if agent_home:
            return agent_home / "inbox"
        return None

    # ------------------------------------------------------------------
    # Session liveness
    # ------------------------------------------------------------------

    async def ensure_session(self, ctx: proto.RequestContext) -> None:
        """Ensure a session is registered in presence from its request context.

        Called on requests that mutate session-scoped state. If the session
        isn't already in the presence registry, registers it now and emits
        EVT_SESSION_LIVE.
        """
        existing = self.state.presence.get(ctx.session_id)
        if existing:
            existing.touch()
            return

        agents = load_agents_registry(self.config.agents_registry)
        agent_home = str(agents.get(ctx.agent_name, ""))

        record = SessionRecord(
            session_id=ctx.session_id,
            agent_name=ctx.agent_name,
            agent_home=agent_home,
        )
        self.state.presence.register(record)

        await self.events.emit(proto.event(
            proto.EVT_SESSION_LIVE,
            session_id=ctx.session_id,
            agent_name=ctx.agent_name,
        ))

    async def _reconcile_loop(self, interval: float = 30.0) -> None:
        """Periodic reconciliation against tmux session liveness."""
        agents = load_agents_registry(self.config.agents_registry)
        while True:
            try:
                await asyncio.sleep(interval)
                pruned, discovered = self.state.reconcile(
                    agents_registry=agents,
                )
                for session_id in discovered:
                    await self.events.emit(proto.event(
                        proto.EVT_SESSION_LIVE,
                        session_id=session_id,
                        agent_name=session_id.split("-")[0],
                    ))
                for session_id in pruned:
                    await self.events.emit(proto.event(
                        proto.EVT_SESSION_GONE,
                        session_id=session_id,
                    ))
            except asyncio.CancelledError:
                return
            except Exception:
                log.exception("Error in reconcile loop")

    # ------------------------------------------------------------------
    # Server lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start the daemon server and configured adapters."""
        # Ensure directories exist
        self.config.socket_path.parent.mkdir(parents=True, exist_ok=True)
        self.config.channels_dir.mkdir(parents=True, exist_ok=True)
        self.config.subscriptions_dir.mkdir(parents=True, exist_ok=True)

        # Clean stale socket
        if self.config.socket_path.exists():
            self.config.socket_path.unlink()

        # Load durable state from files
        self.state.load_from_files()

        # Initial reconciliation — populate state but defer events until
        # adapters are running so they can observe startup discoveries.
        agents = load_agents_registry(self.config.agents_registry)
        pruned, discovered = self.state.reconcile(agents_registry=agents)

        self._server = await asyncio.start_unix_server(
            self._on_connect,
            path=str(self.config.socket_path),
        )

        # Write PID file
        self.config.pid_file.write_text(str(os.getpid()))

        # Start configured adapters
        await self._start_adapters()

        # Now emit initial reconcile events — adapters are listening
        for session_id in discovered:
            await self.events.emit(proto.event(
                proto.EVT_SESSION_LIVE,
                session_id=session_id,
                agent_name=session_id.split("-")[0],
            ))
        for session_id in pruned:
            await self.events.emit(proto.event(
                proto.EVT_SESSION_GONE,
                session_id=session_id,
            ))

        # Start periodic reconciliation
        self._reconcile_task = asyncio.create_task(self._reconcile_loop())

        log.info(
            "Daemon started on %s (PID %d, %d adapter(s))",
            self.config.socket_path, os.getpid(), len(self.adapters),
        )

    async def _start_adapters(self) -> None:
        """Instantiate and start adapters from daemon config."""
        for adapter_id, adapter_cfg in self.config.adapters.items():
            if not adapter_cfg.enabled:
                log.info("Adapter '%s' disabled, skipping", adapter_id)
                continue

            platform = adapter_cfg.platform
            cls = self._resolve_adapter_class(platform)
            if cls is None:
                log.warning(
                    "No adapter class for platform '%s' (adapter '%s')",
                    platform, adapter_id,
                )
                continue

            try:
                adapter = cls(config=adapter_cfg.config)
                await adapter.start(self)
                self.adapters[platform] = adapter
                log.info("Started adapter '%s' (platform: %s)", adapter_id, platform)
            except Exception:
                log.exception("Failed to start adapter '%s'", adapter_id)

    @classmethod
    def _resolve_adapter_class(cls, platform: str) -> type | None:
        """Look up and import the adapter class for a platform."""
        ref = cls._adapter_registry.get(platform)
        if ref is None:
            return None
        if isinstance(ref, type):
            return ref
        # Dotted path — import lazily
        module_path, class_name = ref.rsplit(".", 1)
        import importlib
        module = importlib.import_module(module_path)
        return getattr(module, class_name)

    async def stop(self) -> None:
        """Stop the daemon server and all adapters."""
        # Cancel reconcile loop
        if self._reconcile_task:
            self._reconcile_task.cancel()
            try:
                await self._reconcile_task
            except asyncio.CancelledError:
                pass

        # Stop adapters
        for name, adapter in self.adapters.items():
            try:
                await adapter.stop()
                log.info("Stopped adapter '%s'", name)
            except Exception:
                log.exception("Error stopping adapter '%s'", name)
        self.adapters.clear()

        if self._server:
            self._server.close()
            await self._server.wait_closed()

        # Drain outstanding event handler tasks
        await self.events.drain()

        # Clean up files
        self.config.socket_path.unlink(missing_ok=True)
        self.config.pid_file.unlink(missing_ok=True)

        log.info("Daemon stopped")

    async def serve_forever(self) -> None:
        """Start and run until interrupted."""
        await self.start()

        # Handle shutdown signals
        loop = asyncio.get_event_loop()
        stop_event = asyncio.Event()

        def _signal_handler():
            stop_event.set()

        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, _signal_handler)

        try:
            await stop_event.wait()
        finally:
            await self.stop()

    async def _on_connect(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """Handle a new client connection (single request, then close)."""
        await _handle_request(reader, writer, self)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _setup_logging(log_file: Path | None = None) -> None:
    """Configure logging for the daemon process."""
    handlers: list[logging.Handler] = []

    if log_file:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_file))
    else:
        handlers.append(logging.StreamHandler())

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=handlers,
    )


def _daemonize(log_file: Path) -> None:
    """Double-fork to detach from terminal."""
    if os.fork() > 0:
        sys.exit(0)

    os.setsid()

    if os.fork() > 0:
        sys.exit(0)

    # Redirect stdio
    sys.stdin.close()
    log_fh = open(log_file, "a")
    os.dup2(log_fh.fileno(), sys.stdout.fileno())
    os.dup2(log_fh.fileno(), sys.stderr.fileno())


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Kiln daemon server")
    parser.add_argument("--background", action="store_true",
                        help="Run as a background daemon")
    parser.add_argument("--config", type=Path, default=None,
                        help="Path to daemon config file")
    args = parser.parse_args()

    config = load_daemon_config(args.config)

    if args.background:
        _daemonize(config.log_file)

    _setup_logging(config.log_file if args.background else None)
    asyncio.run(KilnDaemon(config).serve_forever())


if __name__ == "__main__":
    main()
