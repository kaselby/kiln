"""Discord platform adapter for the Kiln daemon.

Connects Discord to the daemon's event bus and management layer.
Handles inbound message routing, control commands, outbound bridge
rendering, and Discord-specific UX (status embeds, branch threads,
permission approval UI).

The adapter authenticates/translates platform input; the daemon owns
durable delivery, routing, and coordination semantics.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..protocol import PlatformMessage, RequestContext

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Inbound message types (adapter-local — not wire protocol)
# ---------------------------------------------------------------------------

@dataclass
class InboundMessage:
    """Pre-extracted Discord message data for routing.

    The discord.py on_message handler extracts raw Discord data into
    this struct, then passes it to ``handle_message()``. This decouples
    routing logic from the Discord client, enabling testing without
    a live connection.
    """

    sender_id: str              # Discord user ID
    sender_display_name: str    # Discord display name (fallback if not in user registry)
    channel_id: str             # Discord channel or thread ID
    content: str                # Text content (already transcribed if voice)
    is_dm: bool
    attachment_paths: list[str] = field(default_factory=list)


@dataclass
class ControlResponse:
    """Result of a control command, formatted for the adapter's platform.

    The adapter returns these from control command parsing. The outbound
    path (Slice C) formats them as Discord messages. Tests assert against
    the fields directly.
    """

    success: bool
    message: str
    data: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# Access control
# ---------------------------------------------------------------------------

@dataclass
class AccessPolicy:
    """Access control policy for a message surface (channels or DMs).

    Modes:
        open        — anyone can interact
        allowlist   — only user IDs in the allowlist
        read_only   — no interaction
    """

    mode: str = "open"
    allowlist: set[str] = field(default_factory=set)

    def is_allowed(self, user_id: str) -> bool:
        if self.mode == "open":
            return True
        if self.mode == "allowlist":
            return user_id in self.allowlist
        return False  # read_only or unknown


# ---------------------------------------------------------------------------
# Adapter config
# ---------------------------------------------------------------------------

@dataclass
class DiscordAdapterConfig:
    """Discord-specific adapter configuration.

    Parsed from the adapter's config dict in daemon config.yml.
    """

    guild_id: str = ""
    channels: dict[str, str] = field(default_factory=dict)  # name -> discord channel ID
    users: dict[str, dict[str, str]] = field(default_factory=dict)  # discord user ID -> {name, max_trust, ...}
    channel_access: AccessPolicy = field(default_factory=AccessPolicy)
    dm_access: AccessPolicy = field(default_factory=lambda: AccessPolicy(mode="allowlist"))
    default_agent: str = ""
    session_prefix: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DiscordAdapterConfig:
        """Parse from raw config dict."""
        cfg = cls(
            guild_id=str(data.get("guild_id", "")),
            channels=data.get("channels", {}),
            users={str(k): v for k, v in data.get("users", {}).items()},
            default_agent=data.get("default_agent", ""),
            session_prefix=data.get("session_prefix", ""),
        )

        # If no session_prefix set, derive from default_agent
        if not cfg.session_prefix and cfg.default_agent:
            cfg.session_prefix = f"{cfg.default_agent}-"

        # Channel access
        ch_mode = data.get("channel_access", "open")
        ch_allowlist = set(str(uid) for uid in data.get("channel_allowlist", cfg.users.keys()))
        cfg.channel_access = AccessPolicy(mode=ch_mode, allowlist=ch_allowlist)

        # DM access — defaults to allowlist with all known users
        dm_mode = data.get("dm_access", "allowlist")
        dm_allowlist = set(str(uid) for uid in data.get("dm_allowlist", cfg.users.keys()))
        cfg.dm_access = AccessPolicy(mode=dm_mode, allowlist=dm_allowlist)

        return cfg

    def resolve_user(self, user_id: str, fallback_name: str = "") -> tuple[str, str]:
        """Look up a user's display name and base trust level.

        Returns (name, max_trust). Falls back to fallback_name if unknown.
        """
        entry = self.users.get(str(user_id), {})
        name = entry.get("name", fallback_name or user_id)
        trust = entry.get("max_trust") or entry.get("trust", "unknown")
        return name, trust


def _load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _save_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n")


class DiscordAdapter:
    """Discord adapter for the Kiln daemon.

    Implements the PlatformAdapter protocol. Started by the daemon
    after the server is running; receives the daemon instance for
    access to management actions, event subscription, and query methods.
    """

    def __init__(self, config: dict[str, Any] | None = None):
        self._raw_config = config or {}
        self._discord_config = DiscordAdapterConfig.from_dict(self._raw_config)
        self._daemon: Any = None  # KilnDaemon, set on start()
        self._state_dir: Path | None = None
        self._event_handler = self._handle_event  # stable reference for add/remove

        # Adapter-local branch/channel thread mappings
        self._branch_threads: dict[str, int] = {}  # session_id -> discord thread_id
        self._channel_threads: dict[str, int] = {}  # channel_name -> discord thread_id

        # Reverse indexes (rebuilt on load/mutation)
        self._thread_to_session: dict[int, str] = {}  # thread_id -> session_id
        self._thread_to_channel: dict[int, str] = {}  # thread_id -> channel_name

    @property
    def adapter_id(self) -> str:
        return "discord"

    @property
    def platform_name(self) -> str:
        return "discord"

    async def start(self, daemon: Any) -> None:
        """Start the adapter. Called by the daemon after server is running."""
        self._daemon = daemon
        self._state_dir = daemon.config.state_dir / "discord"
        self._state_dir.mkdir(parents=True, exist_ok=True)

        # Load persisted adapter state
        self._branch_threads = {
            k: int(v)
            for k, v in _load_json(self._state_dir / "branch-threads.json").items()
        }
        self._channel_threads = {
            k: int(v)
            for k, v in _load_json(self._state_dir / "channel-threads.json").items()
        }
        self._rebuild_reverse_indexes()

        # Subscribe to daemon events
        daemon.events.add_handler(self._event_handler)

        log.info("Discord adapter started (state: %s)", self._state_dir)

    async def stop(self) -> None:
        """Stop the adapter and clean up."""
        if self._daemon:
            self._daemon.events.remove_handler(self._event_handler)

        # Persist adapter state
        if self._state_dir:
            _save_json(self._state_dir / "branch-threads.json", self._branch_threads)
            _save_json(self._state_dir / "channel-threads.json", self._channel_threads)

        self._daemon = None
        log.info("Discord adapter stopped")

    async def send_user_message(
        self,
        user: str,
        summary: str,
        body: str,
        context: RequestContext | None = None,
    ) -> str:
        """Send a message to a Discord user.

        Routed here by the daemon when an agent calls send_user for a
        user whose default platform is Discord.
        """
        # Will be implemented in Slice D (platform ops)
        raise NotImplementedError("send_user_message not yet implemented")

    async def platform_op(
        self,
        action: str,
        args: dict[str, Any],
        context: RequestContext | None = None,
    ) -> dict[str, Any]:
        """Execute a Discord-specific operation requested by an agent.

        This is the agent-facing API surface — agents call platform_op
        through the daemon client, the daemon routes it here.
        """
        handlers = {
            "send": self._op_send,
            "read_history": self._op_read_history,
            "branch_post": self._op_branch_post,
            "thread_create": self._op_thread_create,
            "thread_archive": self._op_thread_archive,
            "list_channels": self._op_list_channels,
            "voice_send": self._op_voice_send,
            "delete": self._op_delete,
            "security_challenge": self._op_security_challenge,
            "permission_request": self._op_permission_request,
            "permission_resolve": self._op_permission_resolve,
        }
        handler = handlers.get(action)
        if not handler:
            raise ValueError(f"Unknown Discord platform op: '{action}'")
        return await handler(args, context)

    def supports(self, feature: str) -> bool:
        return feature in {
            "send_message", "read_history", "voice",
            "security_challenge", "permission",
            "branch_post", "threads",
        }

    # ------------------------------------------------------------------
    # Event handling (daemon → adapter)
    # ------------------------------------------------------------------

    async def _handle_event(self, event: Any) -> None:
        """Handle daemon events for outbound rendering and lifecycle."""
        etype = event.data.get("event_type") if hasattr(event, "data") else None
        if not etype:
            return

        # Will be fully implemented in Slice C
        # EVT_MESSAGE_CHANNEL → outbound bridge rendering
        # EVT_SESSION_CONNECTED → branch thread creation
        # EVT_SESSION_DISCONNECTED → branch thread archival
        # EVT_CHANNEL_SUBSCRIBED → channel thread creation

    # ------------------------------------------------------------------
    # Platform op stubs (Slice B-D)
    # ------------------------------------------------------------------

    async def _op_send(self, args: dict, ctx: RequestContext | None) -> dict:
        raise NotImplementedError("Slice D")

    async def _op_read_history(self, args: dict, ctx: RequestContext | None) -> dict:
        raise NotImplementedError("Slice D")

    async def _op_branch_post(self, args: dict, ctx: RequestContext | None) -> dict:
        raise NotImplementedError("Slice D")

    async def _op_thread_create(self, args: dict, ctx: RequestContext | None) -> dict:
        raise NotImplementedError("Slice D")

    async def _op_thread_archive(self, args: dict, ctx: RequestContext | None) -> dict:
        raise NotImplementedError("Slice D")

    async def _op_list_channels(self, args: dict, ctx: RequestContext | None) -> dict:
        raise NotImplementedError("Slice D")

    async def _op_voice_send(self, args: dict, ctx: RequestContext | None) -> dict:
        raise NotImplementedError("Slice D")

    async def _op_delete(self, args: dict, ctx: RequestContext | None) -> dict:
        raise NotImplementedError("Slice D")

    async def _op_security_challenge(self, args: dict, ctx: RequestContext | None) -> dict:
        raise NotImplementedError("Slice D")

    async def _op_permission_request(self, args: dict, ctx: RequestContext | None) -> dict:
        raise NotImplementedError("Slice D")

    async def _op_permission_resolve(self, args: dict, ctx: RequestContext | None) -> dict:
        raise NotImplementedError("Slice D")
