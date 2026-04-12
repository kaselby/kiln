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
            log.exception("Error in event handler for %s", event.event_type)


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
# Connection handler
# ---------------------------------------------------------------------------

class ClientConnection:
    """Represents a single connected client (agent session)."""

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        daemon: KilnDaemon,
    ):
        self.reader = reader
        self.writer = writer
        self.daemon = daemon
        self.session_id: str | None = None
        self.agent_name: str | None = None
        self._closed = False

    async def send(self, msg: proto.Message) -> None:
        if self._closed:
            return
        try:
            self.writer.write(msg.to_line())
            await self.writer.drain()
        except (ConnectionResetError, BrokenPipeError, OSError):
            self._closed = True

    async def handle(self) -> None:
        """Main loop: read messages and dispatch."""
        try:
            while not self._closed:
                line = await self.reader.readline()
                if not line:
                    break  # EOF

                try:
                    msg = proto.Message.from_line(line)
                except (json.JSONDecodeError, KeyError) as e:
                    log.warning("Malformed message from client: %s", e)
                    continue

                await self._dispatch(msg)
        except (ConnectionResetError, BrokenPipeError, asyncio.CancelledError):
            pass
        except Exception:
            log.exception("Error handling client connection")
        finally:
            await self._cleanup()

    async def _dispatch(self, msg: proto.Message) -> None:
        """Route a message to the appropriate handler."""
        handlers = {
            proto.REGISTER: self._handle_register,
            proto.DEREGISTER: self._handle_deregister,
            proto.SUBSCRIBE: self._handle_subscribe,
            proto.UNSUBSCRIBE: self._handle_unsubscribe,
            proto.PUBLISH: self._handle_publish,
            proto.SEND_DIRECT: self._handle_send_direct,
            proto.SEND_USER: self._handle_send_user,
            proto.LIST_SUBSCRIPTIONS: self._handle_list_subscriptions,
            proto.LIST_SESSIONS: self._handle_list_sessions,
            proto.GET_STATUS: self._handle_get_status,
            proto.PLATFORM_OP: self._handle_platform_op,
            proto.MGMT: self._handle_mgmt,
        }

        handler = handlers.get(msg.type)
        if handler:
            try:
                await handler(msg)
            except Exception as e:
                log.exception("Error handling %s", msg.type)
                if msg.ref:
                    await self.send(proto.error(msg.ref, str(e)))
        else:
            if msg.ref:
                await self.send(proto.error(msg.ref, f"Unknown message type: {msg.type}"))

    # ----- Request handlers -----

    async def _handle_register(self, msg: proto.Message) -> None:
        agent = msg.data.get("agent", "")
        session = msg.data.get("session", "")
        pid = msg.data.get("pid", 0)

        if not agent or not session:
            await self.send(proto.error(msg.ref, "register requires agent and session"))
            return

        self.session_id = session
        self.agent_name = agent

        # Resolve agent home from registry
        agents = load_agents_registry(self.daemon.config.agents_registry)
        agent_home = str(agents.get(agent, ""))

        record = SessionRecord(
            session_id=session,
            agent_name=agent,
            agent_home=agent_home,
            pid=pid,
            _writer=self.writer,
        )
        self.daemon.state.presence.register(record)
        self.daemon.connections[session] = self

        await self.send(proto.ack(msg.ref, session=session))

        await self.daemon.events.emit(proto.event(
            proto.EVT_SESSION_CONNECTED,
            session_id=session,
            agent_name=agent,
        ))
        log.info("Session registered: %s (%s)", session, agent)

    async def _handle_deregister(self, msg: proto.Message) -> None:
        if msg.ref:
            await self.send(proto.ack(msg.ref))
        self._closed = True

    async def _handle_subscribe(self, msg: proto.Message) -> None:
        channel = msg.data.get("channel", "")
        if not channel:
            await self.send(proto.error(msg.ref, "subscribe requires a channel name"))
            return
        if not self.session_id:
            await self.send(proto.error(msg.ref, "must register before subscribing"))
            return

        count = self.daemon.state.channels.subscribe(channel, self.session_id)
        await self.send(proto.ack(msg.ref, subscriber_count=count))

        await self.daemon.events.emit(proto.event(
            proto.EVT_CHANNEL_SUBSCRIBED,
            channel=channel,
            session_id=self.session_id,
            subscriber_count=count,
        ))

    async def _handle_unsubscribe(self, msg: proto.Message) -> None:
        channel = msg.data.get("channel", "")
        if not channel:
            await self.send(proto.error(msg.ref, "unsubscribe requires a channel name"))
            return
        if not self.session_id:
            await self.send(proto.error(msg.ref, "must register before unsubscribing"))
            return

        self.daemon.state.channels.unsubscribe(channel, self.session_id)
        await self.send(proto.ack(msg.ref))

        await self.daemon.events.emit(proto.event(
            proto.EVT_CHANNEL_UNSUBSCRIBED,
            channel=channel,
            session_id=self.session_id,
        ))

    async def _handle_publish(self, msg: proto.Message) -> None:
        channel = msg.data.get("channel", "")
        summary = msg.data.get("summary", "")
        body = msg.data.get("body", "")
        priority = msg.data.get("priority", "normal")

        if not channel:
            await self.send(proto.error(msg.ref, "publish requires a channel name"))
            return
        if not self.session_id:
            await self.send(proto.error(msg.ref, "must register before publishing"))
            return

        count = await self.daemon.publish_to_channel(
            channel, self.session_id, summary, body, priority,
        )
        await self.send(proto.ack(msg.ref, recipient_count=count))

    async def _handle_send_direct(self, msg: proto.Message) -> None:
        to = msg.data.get("to", "")
        summary = msg.data.get("summary", "")
        body = msg.data.get("body", "")
        priority = msg.data.get("priority", "normal")

        if not to:
            await self.send(proto.error(msg.ref, "send_direct requires 'to'"))
            return
        if not self.session_id:
            await self.send(proto.error(msg.ref, "must register before sending"))
            return

        # Resolve recipient inbox
        prefix = to.split("-")[0]
        agents = load_agents_registry(self.daemon.config.agents_registry)
        agent_home = agents.get(prefix)

        if not agent_home:
            # Fallback: try ~/.{prefix}/
            candidate = Path.home() / f".{prefix}"
            if candidate.is_dir():
                agent_home = candidate
            else:
                await self.send(proto.error(
                    msg.ref, f"Cannot resolve inbox for '{to}'",
                    code="unknown_recipient",
                ))
                return

        inbox_root = agent_home / "inbox"
        msg_path = _write_inbox_message(
            inbox_root, to, self.session_id, summary, body, priority,
        )

        # Push notification if recipient is connected
        conn = self.daemon.connections.get(to)
        if conn:
            await conn.send(proto.event(
                proto.EVT_MESSAGE_DIRECT,
                sender=self.session_id,
                summary=summary,
                path=str(msg_path),
            ))

        await self.daemon.events.emit(proto.event(
            proto.EVT_MESSAGE_DIRECT,
            sender=self.session_id,
            recipient=to,
            summary=summary,
        ))

        await self.send(proto.ack(msg.ref, message=f"Message sent to {to}"))

    @property
    def _request_context(self) -> proto.RequestContext | None:
        """Build request context from this connection's identity."""
        if self.session_id and self.agent_name:
            return proto.RequestContext(
                agent_name=self.agent_name,
                session_id=self.session_id,
            )
        return None

    async def _handle_send_user(self, msg: proto.Message) -> None:
        to = msg.data.get("to", "")
        summary = msg.data.get("summary", "")
        body = msg.data.get("body", "")

        if not to:
            await self.send(proto.error(msg.ref, "send_user requires 'to'"))
            return

        # Resolve user to a platform adapter
        user = self.daemon.config.users.get(to)
        if not user:
            await self.send(proto.error(
                msg.ref, f"Unknown user: '{to}'", code="unknown_user",
            ))
            return

        # Route to adapter — for now, check if any adapter is registered
        # that supports the user's default platform
        platform = user.default_platform
        adapter = self.daemon.adapters.get(platform)
        if not adapter:
            await self.send(proto.error(
                msg.ref,
                f"No adapter for platform '{platform}'",
                code="no_adapter",
            ))
            return

        # Delegate to adapter with requester context
        try:
            ctx = self._request_context
            result = await adapter.send_user_message(to, summary, body, context=ctx)
            await self.send(proto.ack(msg.ref, message=result))
        except Exception as e:
            await self.send(proto.error(msg.ref, f"Adapter error: {e}"))

    async def _handle_list_subscriptions(self, msg: proto.Message) -> None:
        if not self.session_id:
            await self.send(proto.error(msg.ref, "must register first"))
            return

        channels = self.daemon.state.channels.channels_for(self.session_id)
        await self.send(proto.result(msg.ref, channels=channels))

    async def _handle_list_sessions(self, msg: proto.Message) -> None:
        agent_filter = msg.data.get("agent")
        sessions = self.daemon.management.list_sessions(agent=agent_filter)
        await self.send(proto.result(msg.ref, sessions=sessions))

    async def _handle_get_status(self, msg: proto.Message) -> None:
        status = {
            "sessions": len(self.daemon.state.presence),
            "channels": len(self.daemon.state.channels.all_channels()),
            "bridges": len(self.daemon.state.bridges.all_bridges()),
            "adapters": list(self.daemon.adapters.keys()),
            "lockdown": self.daemon.config.lockdown_file.exists(),
        }
        await self.send(proto.result(msg.ref, **status))

    async def _handle_platform_op(self, msg: proto.Message) -> None:
        platform = msg.data.get("platform", "")
        action = msg.data.get("action", "")
        args = msg.data.get("args", {})

        adapter = self.daemon.adapters.get(platform)
        if not adapter:
            await self.send(proto.error(
                msg.ref, f"No adapter for platform '{platform}'",
                code="no_adapter",
            ))
            return

        try:
            ctx = self._request_context
            result = await adapter.platform_op(action, args, context=ctx)
            await self.send(proto.result(msg.ref, **result))
        except Exception as e:
            await self.send(proto.error(msg.ref, f"Platform op failed: {e}"))

    async def _handle_mgmt(self, msg: proto.Message) -> None:
        action = msg.data.get("action", "")
        args = msg.data.get("args", {})

        # Management actions — Phase 5+ will flesh these out
        if action == "list_sessions":
            await self._handle_list_sessions(msg)
        elif action == "get_status":
            await self._handle_get_status(msg)
        else:
            await self.send(proto.error(
                msg.ref,
                f"Management action '{action}' not implemented",
                code="not_implemented",
            ))

    # ----- Cleanup -----

    async def _cleanup(self) -> None:
        """Clean up when connection closes (graceful or crash)."""
        if self.session_id:
            # Remove from all channels
            departed = self.daemon.state.channels.unsubscribe_all(self.session_id)
            for channel in departed:
                await self.daemon.events.emit(proto.event(
                    proto.EVT_CHANNEL_UNSUBSCRIBED,
                    channel=channel,
                    session_id=self.session_id,
                ))

            # Remove from presence
            self.daemon.state.presence.deregister(self.session_id)
            self.daemon.connections.pop(self.session_id, None)

            await self.daemon.events.emit(proto.event(
                proto.EVT_SESSION_DISCONNECTED,
                session_id=self.session_id,
                agent_name=self.agent_name or "",
            ))
            log.info("Session disconnected: %s", self.session_id)

        try:
            self.writer.close()
            await self.writer.wait_closed()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Daemon server
# ---------------------------------------------------------------------------

class KilnDaemon:
    """The main daemon process."""

    def __init__(self, config: DaemonConfig | None = None):
        from .management import ManagementActions

        self.config = config or load_daemon_config()
        self.state = DaemonState()
        self.events = EventBus()
        self.management = ManagementActions(self.state, self.config)
        self.connections: dict[str, ClientConnection] = {}  # session_id -> connection
        self.adapters: dict[str, Any] = {}  # platform_name -> adapter instance
        self._server: asyncio.Server | None = None

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

        Core delivery path used by both socket client handlers and
        platform adapters. Writes to subscriber inboxes, appends
        channel history, pushes to connected subscribers, and emits
        the event bus event.

        Args:
            source: Origin tag for echo prevention (e.g. "discord").
                    Carried in the event so outbound adapters can skip
                    messages that originated from their own platform.
            exclude_sender: If True, don't deliver back to sender.

        Returns number of recipients delivered to.
        """
        subscribers = self.state.channels.subscribers(channel)
        recipients = subscribers - {sender} if exclude_sender else subscribers

        # Write to each subscriber's inbox
        for sub_id in recipients:
            record = self.state.presence.get(sub_id)
            if record and record.agent_home:
                inbox_root = Path(record.agent_home) / "inbox"
                _write_inbox_message(
                    inbox_root, sub_id, sender,
                    summary, body, priority, channel=channel,
                )

        # Write channel history
        _write_channel_history(
            self.config.channels_dir,
            channel, sender, summary, body, priority,
        )

        # Build event
        event_data = dict(
            channel=channel, sender=sender,
            summary=summary, body=body, priority=priority,
        )
        if source:
            event_data["source"] = source
        event = proto.event(proto.EVT_MESSAGE_CHANNEL, **event_data)

        # Push to connected subscribers
        for sub_id in recipients:
            conn = self.connections.get(sub_id)
            if conn:
                await conn.send(event)

        await self.events.emit(event)
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
        # Resolve recipient's inbox location
        record = self.state.presence.get(recipient)
        if record and record.agent_home:
            inbox_root = Path(record.agent_home) / "inbox"
        else:
            # Fallback: resolve from agents registry
            prefix = recipient.split("-")[0]
            agents = load_agents_registry(self.config.agents_registry)
            agent_home = agents.get(prefix)
            if not agent_home:
                candidate = Path.home() / f".{prefix}"
                if candidate.is_dir():
                    agent_home = candidate
            if not agent_home:
                log.warning("Cannot resolve inbox for recipient '%s'", recipient)
                return None
            inbox_root = agent_home / "inbox"

        path = _write_platform_inbox_message(inbox_root, recipient, msg)

        # Push notification if recipient is connected
        conn = self.connections.get(recipient)
        if conn:
            await conn.send(proto.event(
                proto.EVT_MESSAGE_INBOUND,
                sender=f"{msg.platform}-{msg.sender_name}",
                summary=msg.content[:200],
                path=str(path),
                platform=msg.platform,
            ))

        await self.events.emit(proto.event(
            proto.EVT_MESSAGE_INBOUND,
            sender=f"{msg.platform}-{msg.sender_name}",
            recipient=recipient,
            summary=msg.content[:200],
            platform=msg.platform,
        ))

        return path

    # ------------------------------------------------------------------
    # Server lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start the daemon server."""
        # Ensure directories exist
        self.config.socket_path.parent.mkdir(parents=True, exist_ok=True)
        self.config.channels_dir.mkdir(parents=True, exist_ok=True)

        # Clean stale socket
        if self.config.socket_path.exists():
            self.config.socket_path.unlink()

        self._server = await asyncio.start_unix_server(
            self._on_connect,
            path=str(self.config.socket_path),
        )

        # Write PID file
        self.config.pid_file.write_text(str(os.getpid()))

        log.info(
            "Daemon started on %s (PID %d)",
            self.config.socket_path, os.getpid(),
        )

    async def stop(self) -> None:
        """Stop the daemon server."""
        if self._server:
            self._server.close()
            await self._server.wait_closed()

        # Close all client connections
        for conn in list(self.connections.values()):
            await conn._cleanup()

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
        """Handle a new client connection."""
        conn = ClientConnection(reader, writer, self)
        await conn.handle()


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
