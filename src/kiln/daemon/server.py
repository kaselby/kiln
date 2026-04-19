"""Kiln daemon — Unix socket server, event bus, message routing.

The daemon process. Accepts connections from agent sessions over a Unix
domain socket, manages shared live state (presence, channels), routes
messages between agents, and hosts optional services.

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

import yaml

from . import protocol as proto
from .config import (
    DaemonConfig,
    load_agents_registry,
    load_daemon_config,
)
from .state import (
    DaemonState,
    SessionRecord,
    _load_live_session_metadata,
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
    handler = daemon.get_handler(msg.type)
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


async def _handle_list_subscriptions(msg: proto.Message, daemon: KilnDaemon) -> proto.Message:
    ctx = _require_requester(msg)
    if not ctx:
        return proto.error(msg.ref, "list_subscriptions requires requester identity")

    channels = daemon.state.channels.channels_for(ctx.session_id)
    return proto.result(msg.ref, channels=channels)


async def _handle_list_sessions(msg: proto.Message, daemon: KilnDaemon) -> proto.Message:
    agent_filter = msg.data.get("agent")
    sessions = daemon.management.list_sessions(agent=agent_filter)
    return proto.result(msg.ref, sessions=sessions)


async def _handle_get_status(msg: proto.Message, daemon: KilnDaemon) -> proto.Message:
    status: dict[str, Any] = {
        "sessions": len(daemon.state.presence),
        "channels": len(daemon.state.channels.all_channels()),
        "lockdown": daemon.config.lockdown_file.exists(),
        "services": {},
    }
    # Each service contributes its own status section
    for name, service in daemon.services.items():
        try:
            status["services"][name] = service.status()
        except Exception:
            status["services"][name] = {"error": "status unavailable"}
    return proto.result(msg.ref, **status)


async def _handle_mgmt(msg: proto.Message, daemon: KilnDaemon) -> proto.Message:
    action = msg.data.get("action", "")

    # Core mgmt actions
    _core_actions: dict[str, Any] = {
        "list_sessions": _handle_list_sessions,
        "get_status": _handle_get_status,
    }

    handler = _core_actions.get(action) or daemon._mgmt_actions.get(action)
    if handler:
        return await handler(msg, daemon)

    return proto.error(
        msg.ref,
        f"Management action '{action}' not implemented",
        code="not_implemented",
    )


# ---------------------------------------------------------------------------
# Daemon server
# ---------------------------------------------------------------------------

class KilnDaemon:
    """The main daemon process — service host and coordination substrate.

    Owns core primitives (messaging, presence, event bus) and manages
    the lifecycle of optional services that extend its capabilities.
    """

    def __init__(self, config: DaemonConfig | None = None):
        from .management import ManagementActions

        self.config = config or load_daemon_config()
        self.state = DaemonState(subscriptions_dir=self.config.subscriptions_dir)
        self.events = EventBus()
        self.management = ManagementActions(self.state, self.config)
        self.services: dict[str, Any] = {}  # name -> Service instance
        self._server: asyncio.Server | None = None
        self._reconcile_task: asyncio.Task | None = None

        # Mgmt sub-action registry — services extend the mgmt RPC
        # with their own actions (e.g. approval routing).
        self._mgmt_actions: dict[str, Any] = {}

        # RPC handler registry — core handlers registered here,
        # services add their own during start().
        self._handlers: dict[str, Any] = {
            proto.SUBSCRIBE: _handle_subscribe,
            proto.UNSUBSCRIBE: _handle_unsubscribe,
            proto.PUBLISH: _handle_publish,
            proto.SEND_DIRECT: _handle_send_direct,
            proto.LIST_SUBSCRIPTIONS: _handle_list_subscriptions,
            proto.LIST_SESSIONS: _handle_list_sessions,
            proto.GET_STATUS: _handle_get_status,
            proto.MGMT: _handle_mgmt,
        }

    # ------------------------------------------------------------------
    # Handler registration — used by services to extend RPC surface
    # ------------------------------------------------------------------

    def register_handler(self, msg_type: str, handler: Any) -> None:
        """Register an RPC handler for a message type.

        Services call this during start() to add their handlers.
        Raises if a handler is already registered for the type.
        """
        if msg_type in self._handlers:
            raise ValueError(
                f"Handler already registered for '{msg_type}' — "
                f"cannot overwrite core or other service handlers"
            )
        self._handlers[msg_type] = handler
        log.debug("Registered handler for '%s'", msg_type)

    def unregister_handler(self, msg_type: str) -> None:
        """Remove an RPC handler. Services call this during stop()."""
        self._handlers.pop(msg_type, None)

    def get_handler(self, msg_type: str) -> Any | None:
        """Look up the handler for a message type."""
        return self._handlers.get(msg_type)

    def register_mgmt_action(self, action: str, handler: Any) -> None:
        """Register a management sub-action handler.

        Services call this to extend the 'mgmt' RPC with new actions.
        """
        if action in self._mgmt_actions:
            raise ValueError(f"Mgmt action '{action}' already registered")
        self._mgmt_actions[action] = handler

    def unregister_mgmt_action(self, action: str) -> None:
        """Remove a management sub-action handler."""
        self._mgmt_actions.pop(action, None)

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
        services. Writes to subscriber inboxes, appends channel
        history, and emits the event bus event.

        Args:
            source: Origin tag for echo prevention (e.g. "discord").
            exclude_sender: If True, don't deliver back to sender.

        Returns number of recipients delivered to.
        """
        subscribers = self.state.channels.subscribers(channel)
        recipients = subscribers - {sender} if exclude_sender else subscribers

        # Write to each subscriber's inbox
        for sub_id in recipients:
            inbox_root = self.resolve_inbox(sub_id)
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



    # ------------------------------------------------------------------
    # Inbox resolution
    # ------------------------------------------------------------------

    def resolve_inbox(self, recipient: str) -> Path | None:
        """Resolve a recipient's inbox directory.

        Checks presence registry first, then falls back to agents registry.
        Public — services use this to deliver messages to agent inboxes.
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

        mode, tags = ("supervised", [])
        if agent_home:
            mode, tags = _load_live_session_metadata(Path(agent_home), ctx.session_id)

        record = SessionRecord(
            session_id=ctx.session_id,
            agent_name=ctx.agent_name,
            agent_home=agent_home,
            mode=mode,
            tags=tags,
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
        """Start the daemon server and configured services."""
        # Ensure directories exist
        self.config.socket_path.parent.mkdir(parents=True, exist_ok=True)
        self.config.channels_dir.mkdir(parents=True, exist_ok=True)
        self.config.subscriptions_dir.mkdir(parents=True, exist_ok=True)

        # Kill any existing daemon process before taking over
        _kill_existing_daemon(self.config.pid_file)

        # Clean stale socket
        if self.config.socket_path.exists():
            self.config.socket_path.unlink()

        # Load durable state from files
        self.state.load_from_files()

        # Initial reconciliation — populate state but defer events until
        # services are running so they can observe startup discoveries.
        agents = load_agents_registry(self.config.agents_registry)
        pruned, discovered = self.state.reconcile(agents_registry=agents)

        self._server = await asyncio.start_unix_server(
            self._on_connect,
            path=str(self.config.socket_path),
        )

        # Write PID file
        self.config.pid_file.write_text(str(os.getpid()))

        # Start configured services
        await self._start_services()

        # Now emit initial reconcile events — services are listening
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

        service_names = list(self.services.keys()) or ["(none)"]
        log.info(
            "Daemon started on %s (PID %d, services: %s)",
            self.config.socket_path, os.getpid(), ", ".join(service_names),
        )

    # ------------------------------------------------------------------
    # Service registry
    # ------------------------------------------------------------------

    # Service name -> dotted class path. Lazily imported so optional
    # deps (discord.py, croniter, etc.) aren't loaded unless the
    # service is enabled.
    _service_registry: dict[str, str] = {
        "gateway": "kiln.services.gateway.service.GatewayService",
        "scheduler": "kiln.services.scheduler.service.SchedulerService",
    }

    async def _start_services(self) -> None:
        """Instantiate and start services from daemon config."""
        for svc_name, svc_cfg in self.config.services.items():
            if not svc_cfg.get("enabled", True):
                log.info("Service '%s' disabled, skipping", svc_name)
                continue

            cls = self._resolve_service_class(svc_name)
            if cls is None:
                log.warning("No service class for '%s'", svc_name)
                continue

            try:
                service = cls(config=svc_cfg)
                # Register before start() so adapters/subcomponents can
                # find the service via daemon.services during their init.
                self.services[service.name] = service
                await service.start(self)
                log.info("Started service '%s'", service.name)
            except Exception:
                self.services.pop(svc_name, None)
                log.exception("Failed to start service '%s'", svc_name)

    @classmethod
    def _resolve_service_class(cls, name: str) -> type | None:
        """Look up and import the service class by name."""
        ref = cls._service_registry.get(name)
        if ref is None:
            return None
        if isinstance(ref, type):
            return ref
        module_path, class_name = ref.rsplit(".", 1)
        import importlib
        module = importlib.import_module(module_path)
        return getattr(module, class_name)

    async def stop(self) -> None:
        """Stop the daemon server and all services."""
        # Cancel reconcile loop
        if self._reconcile_task:
            self._reconcile_task.cancel()
            try:
                await self._reconcile_task
            except asyncio.CancelledError:
                pass

        # Stop services in reverse startup order
        for name in reversed(list(self.services)):
            try:
                await self.services[name].stop()
                log.info("Stopped service '%s'", name)
            except Exception:
                log.exception("Error stopping service '%s'", name)
        self.services.clear()

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

def _kill_existing_daemon(pid_file: Path) -> None:
    """Kill any existing daemon process identified by the PID file.

    Called at the top of start() to enforce single-instance. Without this,
    restarting the daemon leaves the old process running with its own
    reconcile loop and Discord client, causing duplicate branch threads
    and other state divergence.
    """
    if not pid_file.exists():
        return
    try:
        pid = int(pid_file.read_text().strip())
    except (ValueError, OSError):
        pid_file.unlink(missing_ok=True)
        return

    if pid == os.getpid():
        return

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        pid_file.unlink(missing_ok=True)
        return
    except PermissionError:
        log.warning("Cannot signal existing daemon PID %d — permission denied", pid)
        return

    log.info("Killing existing daemon (PID %d)", pid)
    os.kill(pid, signal.SIGTERM)

    import time
    for _ in range(50):  # 5 seconds
        try:
            os.kill(pid, 0)
            time.sleep(0.1)
        except ProcessLookupError:
            log.info("Existing daemon (PID %d) terminated", pid)
            pid_file.unlink(missing_ok=True)
            return

    log.warning("Daemon PID %d didn't exit after SIGTERM, sending SIGKILL", pid)
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    pid_file.unlink(missing_ok=True)


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
