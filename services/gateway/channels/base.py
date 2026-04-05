"""Base class for messaging platform integrations."""

from abc import ABC, abstractmethod
from typing import Any


class Channel(ABC):
    """Abstract base for a messaging platform channel plugin.

    Subclasses must implement connect/disconnect/send_message.
    All other methods are optional capabilities — call capabilities()
    to check what's supported before invoking.
    """

    @abstractmethod
    async def connect(self) -> None:
        """Connect to the platform. Called once at daemon startup."""
        ...

    @abstractmethod
    async def disconnect(self) -> None:
        """Disconnect cleanly. Called on daemon shutdown."""
        ...

    @abstractmethod
    async def send_message(self, target: str, content: str, **kwargs: Any) -> dict:
        """Send a text message to a channel or user.

        Args:
            target: Channel name (#general) or user (@name) — resolved by plugin.
            content: Message text.

        Returns:
            Dict with at least {"ok": bool}. May include message_id, etc.
        """
        ...

    async def read_history(self, target: str, limit: int = 20) -> list[dict]:
        """Read recent messages from a channel.

        Returns list of {"author": str, "content": str, "timestamp": str, ...}.
        """
        raise NotImplementedError(f"{type(self).__name__} does not support read_history")

    async def create_thread(self, channel: str, name: str) -> dict:
        """Create a thread in a channel."""
        raise NotImplementedError(f"{type(self).__name__} does not support create_thread")

    async def archive_thread(self, channel: str, name: str) -> None:
        """Archive a thread."""
        raise NotImplementedError(f"{type(self).__name__} does not support archive_thread")

    async def send_voice(self, target: str, text: str, **kwargs: Any) -> dict:
        """Send a voice message (TTS) to a channel."""
        raise NotImplementedError(f"{type(self).__name__} does not support send_voice")

    async def list_channels(self) -> list[dict]:
        """List available channels on the platform.

        Returns list of {"name": str, "id": str, ...}.
        """
        raise NotImplementedError(f"{type(self).__name__} does not support list_channels")

    async def request_permission(
        self, agent_id: str, command: str, reason: str, timeout: float = 300
    ) -> dict:
        """Request interactive approval for a guardrail-blocked command.

        Posts an approval prompt (e.g. with buttons) in the agent's
        session surface and waits for a response.

        Args:
            agent_id: The agent session ID requesting approval.
            command: The bash command that triggered the guardrail.
            reason: Human-readable description of why it was flagged.
            timeout: Seconds to wait for a response before timing out.

        Returns:
            {"approved": bool, "timed_out": bool, "responder": str}
        """
        raise NotImplementedError(f"{type(self).__name__} does not support request_permission")

    async def resolve_permission(self, agent_id: str, status: str) -> dict:
        """Externally resolve a pending permission request.

        Called when the approval is resolved from another source (e.g. terminal)
        before the platform's interactive prompt was answered.

        Args:
            agent_id: The agent whose pending request to resolve.
            status: One of "approved", "rejected", "timed_out".

        Returns:
            {"ok": bool}
        """
        raise NotImplementedError(f"{type(self).__name__} does not support resolve_permission")

    def capabilities(self) -> set[str]:
        """Declare supported operations beyond send_message."""
        caps = {"send_message"}
        for method_name in ("read_history", "create_thread", "archive_thread",
                            "send_voice", "list_channels", "request_permission"):
            method = getattr(type(self), method_name)
            base_method = getattr(Channel, method_name)
            if method is not base_method:
                caps.add(method_name)
        return caps
