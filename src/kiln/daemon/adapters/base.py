"""Platform adapter protocol.

Adapters are optional subsystems that connect external platforms to the
daemon. Each adapter implements whatever subset of daemon surfaces makes
sense for its platform:

    - Messaging: mirror channels, route DMs, deliver notifications
    - Management: render status/control surfaces, accept commands
    - Platform ops: expose platform-specific operations to agents

There is intentionally no named adapter taxonomy (interactive vs read-only
vs data-source). Discord and Slack are capability profiles, not framework
categories. The daemon does not assume adapters are symmetric or
bidirectional.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from ..protocol import RequestContext


@runtime_checkable
class PlatformAdapter(Protocol):
    """Interface for platform adapters.

    Adapters register with the daemon on startup and receive events
    through the daemon's event bus. They can also query daemon state
    and call management actions directly through the DaemonContext.

    Not all methods need to be meaningful for every adapter. A read-only
    Slack adapter might implement start/stop and supports() but return
    empty results from send_user_message and platform_op.
    """

    @property
    def adapter_id(self) -> str:
        """Unique identifier for this adapter instance."""
        ...

    @property
    def platform_name(self) -> str:
        """Platform this adapter connects to (e.g. 'discord', 'slack')."""
        ...

    async def start(self, daemon: Any) -> None:
        """Start the adapter.

        Called by the daemon after the server is running. The daemon
        argument provides access to state queries, management actions,
        and event subscription. Its type is KilnDaemon but typed as Any
        here to avoid circular imports.
        """
        ...

    async def stop(self) -> None:
        """Stop the adapter and clean up platform connections."""
        ...

    async def send_user_message(
        self,
        user: str,
        summary: str,
        body: str,
        context: RequestContext | None = None,
    ) -> str:
        """Send a message to an external user on this platform.

        The context identifies who requested the send — use it for
        permission checks and audit trails. Returns a status string.
        Raise if the platform doesn't support outbound user messaging.
        """
        ...

    async def platform_op(
        self,
        action: str,
        args: dict[str, Any],
        context: RequestContext | None = None,
    ) -> dict[str, Any]:
        """Execute a platform-specific operation.

        The context identifies who requested the operation — use it for
        permission checks. Returns result data. Raise for unsupported
        operations.
        """
        ...

    def supports(self, feature: str) -> bool:
        """Check whether this adapter supports a named feature.

        Not a formal capability framework — just an internal convenience
        for the daemon to query adapter capabilities without try/except.
        Feature names are adapter-defined strings.
        """
        ...
