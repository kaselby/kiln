"""Agent-side daemon client.

Created once per session. Connects to the daemon over Unix socket,
handles registration, and provides async methods for all daemon
operations. Maintains a local subscription cache for sync snapshot
reads (avoids async I/O in harness save paths).

Auto-starts the daemon if the socket is missing.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

from . import protocol as proto
from .config import DAEMON_DIR, SOCKET_PATH, PID_FILE, load_daemon_config

log = logging.getLogger(__name__)

# How long to wait for daemon to start (seconds)
AUTO_START_TIMEOUT = 5.0
AUTO_START_POLL_INTERVAL = 0.1

# How long to wait for a response to a request
REQUEST_TIMEOUT = 10.0


class DaemonUnavailableError(Exception):
    """Raised when the daemon cannot be reached."""


class DaemonError(Exception):
    """Raised when the daemon returns an error response."""

    def __init__(self, message: str, code: str | None = None):
        super().__init__(message)
        self.code = code


class DaemonClient:
    """Async client for the Kiln daemon.

    Usage::

        client = DaemonClient()
        await client.connect()
        await client.register("beth", "beth-swift-crane", os.getpid())

        result = await client.subscribe("next-steps")
        await client.publish("next-steps", "update", "Tests pass now")

        subs = client.subscriptions  # sync local read

        await client.deregister()
    """

    def __init__(self, socket_path: Path | None = None):
        self._socket_path = socket_path or SOCKET_PATH
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._connected = False

        # Local subscription cache — updated on successful subscribe/unsubscribe.
        # Sync-readable for harness snapshot without async roundtrip.
        self._subscriptions: set[str] = set()

        # Pending response futures, keyed by ref
        self._pending: dict[str, asyncio.Future[proto.Message]] = {}

        # Event callback — set by the harness to receive pushed events
        self._event_handler: Callable[[proto.Message], Any] | None = None

        # Background reader task
        self._reader_task: asyncio.Task | None = None

        # Session identity (set on register)
        self._session_id: str | None = None

    # ----- Connection lifecycle -----

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def subscriptions(self) -> list[str]:
        """Current channel subscriptions (sync local read)."""
        return sorted(self._subscriptions)

    async def connect(self, auto_start: bool = True) -> None:
        """Connect to the daemon. Auto-starts if socket is missing.

        Raises DaemonUnavailableError if connection fails.
        """
        if self._connected:
            return

        if not self._socket_path.exists():
            if auto_start:
                await self._auto_start_daemon()
            else:
                raise DaemonUnavailableError(
                    f"Daemon socket not found: {self._socket_path}"
                )

        try:
            self._reader, self._writer = await asyncio.open_unix_connection(
                str(self._socket_path)
            )
            self._connected = True
            self._reader_task = asyncio.create_task(self._read_loop())
            log.info("Connected to daemon at %s", self._socket_path)
        except (ConnectionRefusedError, FileNotFoundError, OSError) as e:
            if auto_start:
                # Socket exists but connection failed — stale socket?
                self._cleanup_stale_socket()
                await self._auto_start_daemon()
                try:
                    self._reader, self._writer = await asyncio.open_unix_connection(
                        str(self._socket_path)
                    )
                    self._connected = True
                    self._reader_task = asyncio.create_task(self._read_loop())
                    log.info("Connected to daemon at %s (after auto-start)",
                             self._socket_path)
                except OSError as e2:
                    raise DaemonUnavailableError(
                        f"Failed to connect after auto-start: {e2}"
                    ) from e2
            else:
                raise DaemonUnavailableError(
                    f"Failed to connect to daemon: {e}"
                ) from e

    async def disconnect(self) -> None:
        """Close the connection without deregistering."""
        if self._reader_task:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass
            self._reader_task = None

        if self._writer:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except OSError:
                pass
            self._writer = None
            self._reader = None

        self._connected = False
        self._cancel_pending("disconnected")

    # ----- Registration -----

    async def register(self, agent: str, session: str, pid: int) -> None:
        """Register this session with the daemon."""
        self._session_id = session
        msg = proto.register(agent, session, pid)
        await self._request(msg)
        log.info("Registered session %s with daemon", session)

    async def deregister(self) -> None:
        """Deregister this session and disconnect."""
        if self._connected:
            try:
                msg = proto.deregister()
                await self._request(msg, timeout=3.0)
            except (DaemonUnavailableError, asyncio.TimeoutError, OSError):
                pass  # best-effort on shutdown
        await self.disconnect()
        self._subscriptions.clear()
        log.info("Deregistered from daemon")

    # ----- Channel operations -----

    async def subscribe(self, channel: str) -> int:
        """Subscribe to a channel. Returns subscriber count."""
        msg = proto.subscribe(channel)
        resp = await self._request(msg)
        self._subscriptions.add(channel)
        return resp.data.get("subscriber_count", 0)

    async def unsubscribe(self, channel: str) -> None:
        """Unsubscribe from a channel."""
        msg = proto.unsubscribe(channel)
        await self._request(msg)
        self._subscriptions.discard(channel)

    async def publish(self, channel: str, summary: str, body: str,
                      priority: str = "normal") -> int:
        """Publish to a channel. Returns recipient count."""
        msg = proto.publish(channel, summary, body, priority)
        resp = await self._request(msg)
        return resp.data.get("recipient_count", 0)

    # ----- Direct messaging -----

    async def send_direct(self, to: str, summary: str, body: str,
                          priority: str = "normal") -> str:
        """Send a direct message to another agent. Returns status message."""
        msg = proto.send_direct(to, summary, body, priority)
        resp = await self._request(msg)
        return resp.data.get("message", "sent")

    async def send_user(self, to: str, summary: str, body: str) -> str:
        """Send a message to an external user. Returns status message."""
        msg = proto.send_user(to, summary, body)
        resp = await self._request(msg)
        return resp.data.get("message", "sent")

    # ----- Queries -----

    async def list_subscriptions(self) -> list[str]:
        """Query subscriptions from daemon (async, authoritative)."""
        msg = proto.list_subscriptions()
        resp = await self._request(msg)
        subs = resp.data.get("channels", [])
        # Sync local cache with daemon truth
        self._subscriptions = set(subs)
        return subs

    async def list_sessions(self, agent: str | None = None) -> list[dict]:
        """Query registered sessions, optionally filtered by agent name."""
        msg = proto.list_sessions(agent)
        resp = await self._request(msg)
        return resp.data.get("sessions", [])

    async def get_status(self, scope: str | None = None) -> dict:
        """Query daemon status."""
        msg = proto.get_status(scope)
        resp = await self._request(msg)
        return resp.data

    # ----- Platform operations -----

    async def platform_op(self, platform: str, action: str,
                          args: dict | None = None) -> dict:
        """Execute a platform-specific operation."""
        msg = proto.platform_op(platform, action, args)
        resp = await self._request(msg)
        return resp.data

    # ----- Management -----

    async def mgmt(self, action: str, args: dict | None = None) -> dict:
        """Execute a management action."""
        msg = proto.mgmt(action, args)
        resp = await self._request(msg)
        return resp.data

    # ----- Event handling -----

    def set_event_handler(self, handler: Callable[[proto.Message], Any]) -> None:
        """Set a callback for pushed events from the daemon."""
        self._event_handler = handler

    # ----- Subscription restore -----

    async def restore_subscriptions(self, channels: list[str]) -> None:
        """Re-subscribe to channels from a saved session state.

        Best-effort — logs warnings for failures but doesn't raise.
        """
        for channel in channels:
            try:
                await self.subscribe(channel)
            except Exception:
                log.warning("Failed to restore subscription: %s", channel)

    # ----- Internal transport -----

    async def _send(self, msg: proto.Message) -> None:
        """Send a message to the daemon."""
        if not self._writer or not self._connected:
            raise DaemonUnavailableError("Not connected to daemon")
        self._writer.write(msg.to_line())
        await self._writer.drain()

    async def _request(self, msg: proto.Message,
                       timeout: float = REQUEST_TIMEOUT) -> proto.Message:
        """Send a request and wait for the correlated response."""
        if not msg.ref:
            msg.ref = proto.make_ref()

        future: asyncio.Future[proto.Message] = asyncio.get_event_loop().create_future()
        self._pending[msg.ref] = future

        try:
            await self._send(msg)
            resp = await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            self._pending.pop(msg.ref, None)
            raise DaemonUnavailableError(
                f"Request timed out ({msg.type}, ref={msg.ref})"
            )

        if resp.type == proto.ERROR:
            raise DaemonError(
                resp.data.get("message", "unknown error"),
                resp.data.get("code"),
            )

        return resp

    async def _read_loop(self) -> None:
        """Background task: read messages from daemon, dispatch responses and events."""
        assert self._reader is not None
        try:
            while True:
                line = await self._reader.readline()
                if not line:
                    # EOF — daemon closed connection
                    log.warning("Daemon closed connection")
                    self._connected = False
                    self._cancel_pending("connection closed")
                    break

                try:
                    msg = proto.Message.from_line(line)
                except (json.JSONDecodeError, KeyError) as e:
                    log.warning("Malformed message from daemon: %s", e)
                    continue

                if msg.is_response and msg.ref and msg.ref in self._pending:
                    self._pending.pop(msg.ref).set_result(msg)
                elif msg.is_event:
                    self._handle_event(msg)
                else:
                    log.debug("Unhandled daemon message: %s", msg.type)

        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Error in daemon read loop")
            self._connected = False
            self._cancel_pending("read error")

    def _handle_event(self, msg: proto.Message) -> None:
        """Dispatch a pushed event to the event handler."""
        if self._event_handler:
            try:
                result = self._event_handler(msg)
                if asyncio.iscoroutine(result):
                    asyncio.create_task(result)
            except Exception:
                log.exception("Error in event handler for %s", msg.event_type)

    def _cancel_pending(self, reason: str) -> None:
        """Cancel all pending request futures."""
        for ref, future in self._pending.items():
            if not future.done():
                future.set_exception(
                    DaemonUnavailableError(f"Request cancelled: {reason}")
                )
        self._pending.clear()

    # ----- Auto-start -----

    def _cleanup_stale_socket(self) -> None:
        """Remove a stale socket file if the PID is dead."""
        if not PID_FILE.exists():
            if self._socket_path.exists():
                self._socket_path.unlink(missing_ok=True)
            return

        try:
            pid = int(PID_FILE.read_text().strip())
            os.kill(pid, 0)  # check if alive
        except (ValueError, ProcessLookupError, PermissionError):
            # PID is dead or invalid — clean up
            self._socket_path.unlink(missing_ok=True)
            PID_FILE.unlink(missing_ok=True)
            log.info("Cleaned up stale daemon socket/PID")

    async def _auto_start_daemon(self) -> None:
        """Start the daemon process and wait until it accepts connections."""
        log.info("Auto-starting daemon...")

        DAEMON_DIR.mkdir(parents=True, exist_ok=True)

        # Start daemon in background
        subprocess.Popen(
            [sys.executable, "-m", "kiln.daemon.server", "--background"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

        # Wait for socket to appear AND accept a connection
        elapsed = 0.0
        while elapsed < AUTO_START_TIMEOUT:
            if self._socket_path.exists():
                try:
                    r, w = await asyncio.open_unix_connection(
                        str(self._socket_path)
                    )
                    w.close()
                    await w.wait_closed()
                    log.info("Daemon started and accepting connections")
                    return
                except (ConnectionRefusedError, OSError):
                    pass  # socket exists but not ready yet
            await asyncio.sleep(AUTO_START_POLL_INTERVAL)
            elapsed += AUTO_START_POLL_INTERVAL

        raise DaemonUnavailableError(
            f"Daemon failed to start within {AUTO_START_TIMEOUT}s"
        )
