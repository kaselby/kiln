"""Daemon in-memory state — presence, channels, and bridges.

These registries are the daemon's live truth for shared coordination.
They are NOT persisted to disk as primary storage — the daemon rebuilds
from connected clients on startup. Lightweight snapshots for crash
recovery are handled separately.

Important distinction:
    - This module = live shared state (who's connected, who's subscribed)
    - kiln.registry = durable per-home session history (for resume/CLI)
    Do NOT conflate them.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


# ---------------------------------------------------------------------------
# Session presence
# ---------------------------------------------------------------------------

@dataclass
class SessionRecord:
    """A connected agent session."""

    session_id: str
    agent_name: str
    agent_home: str
    pid: int
    connected_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_seen_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    mode: str = "supervised"
    status: str = "running"
    thread_ids: dict[str, str] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    # The asyncio transport for this session's socket connection.
    # Not serialized — only meaningful while connected.
    _writer: asyncio.StreamWriter | None = field(default=None, repr=False)

    def touch(self) -> None:
        self.last_seen_at = datetime.now(timezone.utc)

    def to_summary(self) -> dict[str, Any]:
        """Serializable summary (for list_sessions responses, etc.)."""
        return {
            "session_id": self.session_id,
            "agent_name": self.agent_name,
            "connected_at": self.connected_at.isoformat(),
            "last_seen_at": self.last_seen_at.isoformat(),
            "mode": self.mode,
            "status": self.status,
            "thread_ids": self.thread_ids,
        }


class PresenceRegistry:
    """Tracks connected sessions."""

    def __init__(self) -> None:
        self._sessions: dict[str, SessionRecord] = {}  # session_id -> record

    def register(self, record: SessionRecord) -> None:
        self._sessions[record.session_id] = record

    def deregister(self, session_id: str) -> SessionRecord | None:
        return self._sessions.pop(session_id, None)

    def get(self, session_id: str) -> SessionRecord | None:
        return self._sessions.get(session_id)

    def by_agent(self, agent_name: str) -> list[SessionRecord]:
        return [s for s in self._sessions.values() if s.agent_name == agent_name]

    def all_sessions(self) -> list[SessionRecord]:
        return list(self._sessions.values())

    def session_ids(self) -> set[str]:
        return set(self._sessions.keys())

    def __len__(self) -> int:
        return len(self._sessions)


# ---------------------------------------------------------------------------
# Channel subscriptions
# ---------------------------------------------------------------------------

class ChannelRegistry:
    """Tracks channel subscriptions. Session-scoped — cleaned up on disconnect."""

    def __init__(self) -> None:
        # channel_name -> set of session_ids
        self._channels: dict[str, set[str]] = {}

    def subscribe(self, channel: str, session_id: str) -> int:
        """Add a subscriber. Returns total subscriber count."""
        if channel not in self._channels:
            self._channels[channel] = set()
        self._channels[channel].add(session_id)
        return len(self._channels[channel])

    def unsubscribe(self, channel: str, session_id: str) -> None:
        """Remove a subscriber. Cleans up empty channels."""
        subs = self._channels.get(channel)
        if subs:
            subs.discard(session_id)
            if not subs:
                del self._channels[channel]

    def unsubscribe_all(self, session_id: str) -> list[str]:
        """Remove a session from all channels. Returns list of channels left."""
        departed = []
        for channel in list(self._channels):
            if session_id in self._channels[channel]:
                self._channels[channel].discard(session_id)
                departed.append(channel)
                if not self._channels[channel]:
                    del self._channels[channel]
        return departed

    def subscribers(self, channel: str) -> set[str]:
        """Get subscriber session_ids for a channel."""
        return set(self._channels.get(channel, set()))

    def channels_for(self, session_id: str) -> list[str]:
        """Get channels a session is subscribed to."""
        return [ch for ch, subs in self._channels.items() if session_id in subs]

    def all_channels(self) -> list[str]:
        """List all channels with at least one subscriber."""
        return list(self._channels.keys())

    def subscriber_count(self, channel: str) -> int:
        return len(self._channels.get(channel, set()))


# ---------------------------------------------------------------------------
# Bridge definitions
# ---------------------------------------------------------------------------

@dataclass
class BridgeRecord:
    """A bridge between a Kiln source and a platform target."""

    bridge_id: str
    source_kind: str       # "channel" | "session" | "control" | "status"
    source_name: str
    adapter_id: str
    platform_target: str
    mode: str = "mirror"   # "mirror" | "interactive" | "read_only"
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "bridge_id": self.bridge_id,
            "source_kind": self.source_kind,
            "source_name": self.source_name,
            "adapter_id": self.adapter_id,
            "platform_target": self.platform_target,
            "mode": self.mode,
        }


class BridgeRegistry:
    """Tracks bridge definitions between Kiln sources and platform targets."""

    def __init__(self) -> None:
        self._bridges: dict[str, BridgeRecord] = {}  # bridge_id -> record

    def bind(self, record: BridgeRecord) -> None:
        self._bridges[record.bridge_id] = record

    def unbind(self, bridge_id: str) -> BridgeRecord | None:
        return self._bridges.pop(bridge_id, None)

    def get(self, bridge_id: str) -> BridgeRecord | None:
        return self._bridges.get(bridge_id)

    def by_adapter(self, adapter_id: str) -> list[BridgeRecord]:
        return [b for b in self._bridges.values() if b.adapter_id == adapter_id]

    def by_source(self, source_kind: str, source_name: str) -> list[BridgeRecord]:
        return [
            b for b in self._bridges.values()
            if b.source_kind == source_kind and b.source_name == source_name
        ]

    def all_bridges(self) -> list[BridgeRecord]:
        return list(self._bridges.values())


# ---------------------------------------------------------------------------
# Combined daemon state
# ---------------------------------------------------------------------------

class DaemonState:
    """Aggregate of all daemon-managed live state."""

    def __init__(self) -> None:
        self.presence = PresenceRegistry()
        self.channels = ChannelRegistry()
        self.bridges = BridgeRegistry()
