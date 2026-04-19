"""Service protocol — the contract between daemon and services.

Services are optional extensions that plug into the daemon at startup.
The daemon manages their lifecycle and provides access to core
primitives (event bus, messaging, presence) through the DaemonHost
protocol. Services register RPC handlers and event subscriptions
during start().
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class DaemonHost(Protocol):
    """What the daemon exposes to services.

    Typed as a protocol to avoid circular imports — services never
    import the daemon module directly.
    """

    @property
    def state(self) -> Any:
        """Core daemon state (presence, channels)."""
        ...

    @property
    def events(self) -> Any:
        """Event bus for subscribing to and emitting daemon events."""
        ...

    @property
    def config(self) -> Any:
        """Daemon configuration."""
        ...

    @property
    def management(self) -> Any:
        """Session lifecycle and introspection actions."""
        ...

    @property
    def services(self) -> dict[str, Any]:
        """Registry of running services (name -> instance).

        Services can look up siblings through this — e.g. a handler
        registered by the gateway service uses ``daemon.services.get("gateway")``
        to access the service instance holding surface state.
        """
        ...

    def register_handler(self, msg_type: str, handler: Any) -> None:
        """Register an RPC handler for a message type."""
        ...

    def unregister_handler(self, msg_type: str) -> None:
        """Remove an RPC handler."""
        ...

    async def publish_to_channel(
        self, channel: str, sender: str, summary: str, body: str, **kwargs: Any,
    ) -> int:
        """Publish to a Kiln channel (core messaging primitive)."""
        ...

    def resolve_inbox(self, recipient: str) -> Any:
        """Resolve a recipient's inbox directory path."""
        ...

    async def ensure_session(self, ctx: Any) -> None:
        """Ensure a session is registered in presence."""
        ...


@runtime_checkable
class Service(Protocol):
    """A daemon service — self-contained optional capability.

    Services own their state, RPC handlers, and event subscriptions.
    The daemon calls start/stop and queries name/status for reporting.
    """

    @property
    def name(self) -> str:
        """Service name (e.g. 'gateway', 'scheduler')."""
        ...

    async def start(self, daemon: DaemonHost) -> None:
        """Start the service.

        Register RPC handlers, subscribe to events, initialize state.
        Called after the daemon's core server is running.
        """
        ...

    async def stop(self) -> None:
        """Stop the service and clean up."""
        ...

    def status(self) -> dict[str, Any]:
        """Return service-specific status for daemon introspection.

        Called by the daemon's status RPC. Return an empty dict if
        there's nothing interesting to report.
        """
        ...
