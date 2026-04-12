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
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .. import protocol as proto
from ..protocol import PlatformMessage, RequestContext

log = logging.getLogger(__name__)

# Discord message length limit
DISCORD_MAX_LENGTH = 2000
SPLIT_MARGIN = 100  # room for (1/N) prefix


# ---------------------------------------------------------------------------
# Message formatting utilities
# ---------------------------------------------------------------------------

def split_message(text: str, max_len: int = DISCORD_MAX_LENGTH - SPLIT_MARGIN) -> list[str]:
    """Split a long message into Discord-safe chunks.

    Splits at paragraph boundaries, preserving code blocks.
    """
    if len(text) <= max_len:
        return [text]

    chunks: list[str] = []
    remaining = text

    while remaining:
        if len(remaining) <= max_len:
            chunks.append(remaining)
            break

        split_at = _find_split_point(remaining, max_len)
        chunks.append(remaining[:split_at].rstrip())
        remaining = remaining[split_at:].lstrip("\n")

    if len(chunks) > 1:
        total = len(chunks)
        chunks = [f"({i+1}/{total})\n{chunk}" for i, chunk in enumerate(chunks)]

    return chunks


def _find_split_point(text: str, max_len: int) -> int:
    """Find the best place to split text, respecting structure."""
    # Don't split inside code blocks if possible
    code_blocks = list(re.finditer(r"```.*?```", text[:max_len + 500], re.DOTALL))
    for block in code_blocks:
        if block.start() < max_len < block.end():
            candidate = block.start()
            if candidate > max_len // 2:
                return candidate
            break

    search_region = text[:max_len]

    # Try paragraph boundary
    last_para = search_region.rfind("\n\n")
    if last_para > max_len // 2:
        return last_para + 1

    # Try line boundary
    last_line = search_region.rfind("\n")
    if last_line > max_len // 2:
        return last_line + 1

    return max_len


def format_outbound(sender: str, body: str, summary: str = "") -> str | None:
    """Format a Kiln channel message for Discord display.

    Returns None if there's nothing to display.
    """
    text = body or summary
    if not text:
        return None
    return f"**{sender}:** {text}"


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

    def _rebuild_reverse_indexes(self) -> None:
        """Rebuild reverse lookup dicts from forward mappings."""
        self._thread_to_session = {v: k for k, v in self._branch_threads.items()}
        self._thread_to_channel = {v: k for k, v in self._channel_threads.items()}

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

    def validate_surface_ref(self, surface_ref: str) -> str:
        """Validate and canonicalize a Discord surface reference.

        Accepted forms:
            discord:user:<id>       — a user's DM surface
            discord:channel:<id>    — a Discord channel/thread

        Returns the canonical ref (unchanged if already canonical).
        Raises ValueError for malformed or unrecognized refs.
        """
        parts = surface_ref.split(":", 2)
        if len(parts) != 3 or parts[0] != "discord":
            raise ValueError(
                f"Invalid Discord surface ref: '{surface_ref}' "
                f"(expected discord:<type>:<id>)"
            )
        surface_type, surface_id = parts[1], parts[2]
        if surface_type not in ("user", "channel"):
            raise ValueError(
                f"Unknown Discord surface type: '{surface_type}' "
                f"(expected 'user' or 'channel')"
            )
        if not surface_id:
            raise ValueError("Surface ID cannot be empty")
        return surface_ref  # already canonical

    def supports(self, feature: str) -> bool:
        return feature in {
            "send_message", "read_history", "voice",
            "security_challenge", "permission",
            "branch_post", "threads",
        }

    # ------------------------------------------------------------------
    # Event handling (daemon → adapter)
    # ------------------------------------------------------------------

    async def _handle_event(self, event: proto.Message) -> None:
        """Route daemon events to the appropriate handler.

        This is the adapter's main event intake. The daemon event bus
        calls this for every event; the adapter classifies and routes
        to specific handler methods. Each handler category corresponds
        to a distinct concern — keep them separate.

        Categories:
            Channel outbound — Kiln channel messages rendered to Discord
            Session lifecycle — branch thread create/archive
            Channel lifecycle — channel thread create/archive
            Bridge lifecycle — bridge-level bookkeeping
            Status — status display updates
        """
        etype = event.data.get("event_type")
        if not etype:
            return

        handler_name = self._EVENT_HANDLERS.get(etype)
        if handler_name:
            handler = getattr(self, handler_name)
            await handler(event)

    # --- Channel outbound (Kiln channel → Discord) ---

    async def _on_channel_message(self, event: proto.Message) -> None:
        """A message was published to a Kiln channel.

        If the channel has a bridge to Discord, format and send the
        message to the bridged Discord surface. Skip messages that
        originated from Discord (echo prevention).
        """
        # Echo prevention — don't send Discord-originated messages back
        if event.data.get("source") == "discord":
            return

        channel = event.data.get("channel", "")
        if not channel or not self._daemon:
            return

        # Look up bridge for this channel
        bridges = self._daemon.state.bridges.by_source("channel", channel)
        discord_bridges = [b for b in bridges if b.adapter_id == "discord"]
        if not discord_bridges:
            return

        sender = event.data.get("sender", "unknown")
        body = event.data.get("body", "")
        summary = event.data.get("summary", "")

        formatted = format_outbound(sender, body, summary)
        if not formatted:
            return

        # Send to each bridged Discord surface
        for bridge in discord_bridges:
            chunks = split_message(formatted)
            for chunk in chunks:
                try:
                    await self._discord_post_to_surface(
                        bridge.platform_target, chunk,
                    )
                except Exception:
                    log.exception(
                        "Failed to post to bridge target %s for channel '%s'",
                        bridge.platform_target, channel,
                    )

    # --- Session lifecycle (branch threads) ---

    async def _on_session_connected(self, event: proto.Message) -> None:
        """A session registered with the daemon.

        Create or reuse a branch thread in #branches for this session.
        Branch threads are one-to-one session bindings (adapter-local state),
        distinct from surface subscriptions and bridges.
        """
        session_id = event.data.get("session_id", "")
        agent_name = event.data.get("agent_name", "")
        if not session_id:
            return

        # Check if this session already has a branch thread (e.g. from resume)
        if session_id in self._branch_threads:
            log.debug(
                "Session %s already has branch thread %d",
                session_id, self._branch_threads[session_id],
            )
            return

        # Create a new branch thread
        branches_channel = self._discord_config.channels.get("branches")
        if not branches_channel:
            log.debug("No #branches channel configured, skipping branch thread for %s", session_id)
            return

        try:
            thread_id = await self._discord_create_thread(
                branches_channel, session_id,
                f"Session {session_id} ({agent_name})",
            )
            if thread_id:
                self._branch_threads[session_id] = thread_id
                self._rebuild_reverse_indexes()
                self._persist_state()
                log.info("Created branch thread %d for %s", thread_id, session_id)
        except Exception:
            log.exception("Failed to create branch thread for %s", session_id)

    async def _on_session_disconnected(self, event: proto.Message) -> None:
        """A session disconnected from the daemon.

        Archive the branch thread. The thread mapping is kept in
        _branch_threads so the thread can be reused on resume.
        """
        session_id = event.data.get("session_id", "")
        if not session_id:
            return

        thread_id = self._branch_threads.get(session_id)
        if not thread_id:
            return

        try:
            await self._discord_archive_thread(thread_id)
            log.info("Archived branch thread %d for %s", thread_id, session_id)
        except Exception:
            log.exception("Failed to archive branch thread for %s", session_id)

    async def _on_session_mode_changed(self, event: proto.Message) -> None:
        """A session's mode was changed.

        Update the branch thread or status display.
        """
        # Slice C2: update branch thread name/topic, status embed
        log.debug("Session mode changed: %s", event.data)

    # --- Channel lifecycle (channel threads) ---

    async def _on_channel_subscribed(self, event: proto.Message) -> None:
        """A session subscribed to a Kiln channel.

        Channel thread lifecycle is tied to bridges (bridge_bound/unbound),
        not individual subscriptions. This handler is a hook for future
        status updates or notifications.
        """
        log.debug(
            "Channel subscribed: %s by %s",
            event.data.get("channel"), event.data.get("session_id"),
        )

    async def _on_channel_unsubscribed(self, event: proto.Message) -> None:
        """A session unsubscribed from a Kiln channel."""
        log.debug(
            "Channel unsubscribed: %s by %s",
            event.data.get("channel"), event.data.get("session_id"),
        )

    # --- Bridge lifecycle (channel threads) ---

    async def _on_bridge_bound(self, event: proto.Message) -> None:
        """A bridge was created between a Kiln source and Discord.

        Creates a channel thread in #channels for the bridged Kiln channel.
        Channel threads are keyed by Kiln channel name in _channel_threads.
        """
        adapter_id = event.data.get("adapter_id", "")
        if adapter_id != "discord":
            return

        source_kind = event.data.get("source_kind", "")
        source_name = event.data.get("source_name", "")
        if source_kind != "channel" or not source_name:
            return

        # Check if thread already exists for this channel
        if source_name in self._channel_threads:
            log.debug("Channel thread already exists for '%s'", source_name)
            return

        channels_channel = self._discord_config.channels.get("channels")
        if not channels_channel:
            log.debug("No #channels channel configured, skipping thread for '%s'", source_name)
            return

        try:
            thread_id = await self._discord_create_thread(
                channels_channel, source_name,
                f"Bridge: {source_name}",
            )
            if thread_id:
                self._channel_threads[source_name] = thread_id
                self._rebuild_reverse_indexes()
                self._persist_state()
                log.info("Created channel thread %d for bridge '%s'", thread_id, source_name)
        except Exception:
            log.exception("Failed to create channel thread for '%s'", source_name)

    async def _on_bridge_unbound(self, event: proto.Message) -> None:
        """A bridge was removed.

        Archives the channel thread if one exists.
        """
        adapter_id = event.data.get("adapter_id", "")
        if adapter_id != "discord":
            return

        source_name = event.data.get("source_name", "")
        thread_id = self._channel_threads.get(source_name)
        if not thread_id:
            return

        try:
            await self._discord_archive_thread(thread_id)
            log.info("Archived channel thread %d for bridge '%s'", thread_id, source_name)
        except Exception:
            log.exception("Failed to archive channel thread for '%s'", source_name)

    # Event type → handler method name mapping.
    # Uses method name strings so getattr(self, name) picks up instance
    # overrides/patches, making event routing testable.
    _EVENT_HANDLERS: dict[str, str] = {
        proto.EVT_MESSAGE_CHANNEL: "_on_channel_message",
        proto.EVT_SESSION_CONNECTED: "_on_session_connected",
        proto.EVT_SESSION_DISCONNECTED: "_on_session_disconnected",
        proto.EVT_SESSION_MODE_CHANGED: "_on_session_mode_changed",
        proto.EVT_CHANNEL_SUBSCRIBED: "_on_channel_subscribed",
        proto.EVT_CHANNEL_UNSUBSCRIBED: "_on_channel_unsubscribed",
        proto.EVT_BRIDGE_BOUND: "_on_bridge_bound",
        proto.EVT_BRIDGE_UNBOUND: "_on_bridge_unbound",
    }

    # ------------------------------------------------------------------
    # State persistence helper
    # ------------------------------------------------------------------

    def _persist_state(self) -> None:
        """Persist adapter state to disk (branch/channel thread mappings)."""
        if self._state_dir:
            _save_json(self._state_dir / "branch-threads.json", self._branch_threads)
            _save_json(self._state_dir / "channel-threads.json", self._channel_threads)

    # ------------------------------------------------------------------
    # Discord API stubs — thin async boundary for actual Discord calls
    #
    # These are the only methods that need a live discord.py client.
    # Everything above is logic/routing that can be tested without one.
    # Wired to the real Discord client in a later slice.
    # ------------------------------------------------------------------

    async def _discord_post_to_surface(self, surface_id: str, content: str) -> None:
        """Post a message to a Discord surface (channel or thread).

        Args:
            surface_id: Discord channel/thread ID as string.
            content: Pre-formatted message text (already split if needed).
        """
        # Wired to discord.py client in Slice D
        log.debug("_discord_post_to_surface(%s, %d chars) — not yet wired",
                   surface_id, len(content))

    async def _discord_create_thread(
        self, channel_id: str, name: str, initial_message: str = "",
    ) -> int | None:
        """Create a thread in a Discord channel.

        Returns the thread ID, or None if creation failed.
        """
        # Wired to discord.py client in Slice D
        log.debug("_discord_create_thread(%s, %s) — not yet wired", channel_id, name)
        return None

    async def _discord_archive_thread(self, thread_id: int) -> None:
        """Archive a Discord thread."""
        # Wired to discord.py client in Slice D
        log.debug("_discord_archive_thread(%d) — not yet wired", thread_id)

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
