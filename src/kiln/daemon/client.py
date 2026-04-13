"""Agent-side daemon client — stateless request/response RPC.

Each operation opens a fresh Unix socket connection, sends one request,
reads one response, and closes. No persistent connections, no event
push, no local caches. The daemon owns all state.

Auto-starts the daemon if the socket is missing.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from . import protocol as proto
from .config import DAEMON_DIR, SOCKET_PATH, PID_FILE

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
    """Stateless async client for the Kiln daemon.

    Each method opens a connection, sends a request, gets a response,
    and closes. No persistent state — the daemon and files are truth.

    Usage::

        client = DaemonClient(agent="beth", session="beth-lone-heron")

        count = await client.subscribe("next-steps")
        await client.publish("next-steps", "update", "Tests pass now")

        subs = await client.list_subscriptions()
    """

    def __init__(
        self,
        agent: str,
        session: str,
        socket_path: Path | None = None,
        auto_start: bool = True,
    ):
        self.agent = agent
        self.session = session
        self._socket_path = socket_path or SOCKET_PATH
        self._auto_start = auto_start

    # ----- Channel operations -----

    async def subscribe(self, channel: str) -> int:
        """Subscribe to a channel. Returns subscriber count."""
        resp = await self._request(proto.subscribe(
            channel, agent=self.agent, session=self.session,
        ))
        return resp.data.get("subscriber_count", 0)

    async def unsubscribe(self, channel: str) -> None:
        """Unsubscribe from a channel."""
        await self._request(proto.unsubscribe(
            channel, agent=self.agent, session=self.session,
        ))

    async def publish(self, channel: str, summary: str, body: str,
                      priority: str = "normal") -> int:
        """Publish to a channel. Returns recipient count."""
        resp = await self._request(proto.publish(
            channel, summary, body, priority,
            agent=self.agent, session=self.session,
        ))
        return resp.data.get("recipient_count", 0)

    async def list_subscriptions(self) -> list[str]:
        """Query this session's channel subscriptions."""
        resp = await self._request(proto.list_subscriptions(
            agent=self.agent, session=self.session,
        ))
        return resp.data.get("channels", [])

    # ----- Direct messaging -----

    async def send_direct(self, to: str, summary: str, body: str,
                          priority: str = "normal") -> str:
        """Send a direct message to another agent. Returns status message."""
        resp = await self._request(proto.send_direct(
            to, summary, body, priority,
            agent=self.agent, session=self.session,
        ))
        return resp.data.get("message", "sent")

    async def send_user(self, to: str, summary: str, body: str) -> str:
        """Send a message to an external user. Returns status message."""
        resp = await self._request(proto.send_user(
            to, summary, body,
            agent=self.agent, session=self.session,
        ))
        return resp.data.get("message", "sent")

    # ----- Surface subscriptions -----

    async def subscribe_surface(self, surface_ref: str) -> int:
        """Subscribe to an adapter-defined surface. Returns subscriber count."""
        resp = await self._request(proto.subscribe_surface(
            surface_ref, agent=self.agent, session=self.session,
        ))
        return resp.data.get("subscriber_count", 0)

    async def unsubscribe_surface(self, surface_ref: str) -> None:
        """Unsubscribe from an adapter-defined surface."""
        await self._request(proto.unsubscribe_surface(
            surface_ref, agent=self.agent, session=self.session,
        ))

    async def list_surface_subscriptions(
        self, adapter_id: str | None = None,
    ) -> list[dict]:
        """Query this session's surface subscriptions."""
        resp = await self._request(proto.list_surface_subscriptions(
            adapter_id, agent=self.agent, session=self.session,
        ))
        return resp.data.get("subscriptions", [])

    # ----- Queries -----

    async def list_sessions(self, agent: str | None = None) -> list[dict]:
        """Query known sessions, optionally filtered by agent name."""
        resp = await self._request(proto.list_sessions(agent))
        return resp.data.get("sessions", [])

    async def get_status(self, scope: str | None = None) -> dict:
        """Query daemon status."""
        resp = await self._request(proto.get_status(scope))
        return resp.data

    # ----- Platform operations -----

    async def platform_op(self, platform: str, action: str,
                          args: dict | None = None,
                          timeout: float | None = None) -> dict:
        """Execute a platform-specific operation."""
        kw = {"timeout": timeout} if timeout is not None else {}
        resp = await self._request(proto.platform_op(
            platform, action, args,
            agent=self.agent, session=self.session,
        ), **kw)
        return resp.data

    # ----- Management -----

    async def mgmt(self, action: str, args: dict | None = None,
                   timeout: float | None = None) -> dict:
        """Execute a management action."""
        kw = {"timeout": timeout} if timeout is not None else {}
        resp = await self._request(proto.mgmt(
            action, args,
            agent=self.agent, session=self.session,
        ), **kw)
        return resp.data

    # ----- Subscription restore (convenience) -----

    async def restore_subscriptions(self, channels: list[str]) -> None:
        """Re-subscribe to channels from a saved session state."""
        for channel in channels:
            try:
                await self.subscribe(channel)
            except Exception:
                log.warning("Failed to restore subscription: %s", channel)

    # ----- Internal transport -----

    async def _request(
        self,
        msg: proto.Message,
        timeout: float = REQUEST_TIMEOUT,
    ) -> proto.Message:
        """Open a connection, send a request, read one response, close."""
        if not msg.ref:
            msg.ref = proto.make_ref()

        reader, writer = await self._connect()
        try:
            writer.write(msg.to_line())
            await writer.drain()

            line = await asyncio.wait_for(reader.readline(), timeout=timeout)
            if not line:
                raise DaemonUnavailableError("Daemon closed connection without responding")

            resp = proto.Message.from_line(line)

            if resp.type == proto.ERROR:
                raise DaemonError(
                    resp.data.get("message", "unknown error"),
                    resp.data.get("code"),
                )

            return resp
        except asyncio.TimeoutError:
            raise DaemonUnavailableError(
                f"Request timed out ({msg.type}, ref={msg.ref})"
            )
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except OSError:
                pass

    async def _connect(self) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        """Open a connection to the daemon socket."""
        if not self._socket_path.exists():
            if self._auto_start:
                await self._auto_start_daemon()
            else:
                raise DaemonUnavailableError(
                    f"Daemon socket not found: {self._socket_path}"
                )

        try:
            return await asyncio.open_unix_connection(str(self._socket_path))
        except (ConnectionRefusedError, FileNotFoundError, OSError) as e:
            if self._auto_start:
                self._cleanup_stale_socket()
                await self._auto_start_daemon()
                try:
                    return await asyncio.open_unix_connection(str(self._socket_path))
                except OSError as e2:
                    raise DaemonUnavailableError(
                        f"Failed to connect after auto-start: {e2}"
                    ) from e2
            raise DaemonUnavailableError(f"Failed to connect to daemon: {e}") from e

    # ----- Auto-start -----

    def _cleanup_stale_socket(self) -> None:
        """Remove a stale socket file if the PID is dead."""
        if not PID_FILE.exists():
            if self._socket_path.exists():
                self._socket_path.unlink(missing_ok=True)
            return

        try:
            pid = int(PID_FILE.read_text().strip())
            os.kill(pid, 0)
        except (ValueError, ProcessLookupError, PermissionError):
            self._socket_path.unlink(missing_ok=True)
            PID_FILE.unlink(missing_ok=True)
            log.info("Cleaned up stale daemon socket/PID")

    async def _auto_start_daemon(self) -> None:
        """Start the daemon process and wait until it accepts connections."""
        log.info("Auto-starting daemon...")

        DAEMON_DIR.mkdir(parents=True, exist_ok=True)

        subprocess.Popen(
            [sys.executable, "-m", "kiln.daemon.server", "--background"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

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
                    pass
            await asyncio.sleep(AUTO_START_POLL_INTERVAL)
            elapsed += AUTO_START_POLL_INTERVAL

        raise DaemonUnavailableError(
            f"Daemon failed to start within {AUTO_START_TIMEOUT}s"
        )
