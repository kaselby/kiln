"""Daemon state — in-memory registries backed by durable files.

Files are the source of truth. In-memory registries are derived caches
rebuilt from files on startup and kept in sync on mutation. The daemon
is the single writer for subscription files.

Layout under ``subscriptions_dir`` (default ``~/.kiln/daemon/state/subscriptions/``):
    channels/<session_id>.yml   — Kiln channel subscriptions
    surfaces/<session_id>.yml   — platform surface subscriptions

Important distinction:
    - This module = shared coordination state (subscriptions, presence, bridges)
    - kiln.registry = durable per-home session history (for resume/CLI)
    Do NOT conflate them.
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger(__name__)


def _load_live_session_metadata(agent_home: Path, session_id: str) -> tuple[str, list[str]]:
    """Read live mutable session metadata from session-config.

    Returns ``(mode, tags)``. Missing files or malformed data soft-fail to
    defaults so presence discovery stays robust.
    """
    config_path = agent_home / "state" / f"session-config-{session_id}.yml"
    if not config_path.exists():
        return "supervised", []
    try:
        data = yaml.safe_load(config_path.read_text()) or {}
    except (OSError, yaml.YAMLError):
        return "supervised", []

    mode = data.get("mode", "supervised")
    if not isinstance(mode, str) or not mode:
        mode = "supervised"

    raw_tags = data.get("tags", [])
    if isinstance(raw_tags, list):
        tags = [t for t in raw_tags if isinstance(t, str) and t]
    else:
        tags = []

    return mode, tags



# ---------------------------------------------------------------------------
# Session presence
# ---------------------------------------------------------------------------

@dataclass
class SessionRecord:
    """A known live agent session (derived from tmux + live session state files)."""

    session_id: str
    agent_name: str
    agent_home: str
    pid: int = 0
    first_seen_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_seen_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    mode: str = "supervised"
    status: str = "running"
    thread_ids: dict[str, str] = field(default_factory=dict)
    tags: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


    def touch(self) -> None:
        self.last_seen_at = datetime.now(timezone.utc)

    def to_summary(self) -> dict[str, Any]:
        """Serializable summary (for list_sessions responses, etc.)."""
        return {
            "session_id": self.session_id,
            "agent_name": self.agent_name,
            "first_seen_at": self.first_seen_at.isoformat(),
            "last_seen_at": self.last_seen_at.isoformat(),
            "mode": self.mode,
            "status": self.status,
            "thread_ids": self.thread_ids,
            "tags": list(self.tags),
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
    """Tracks channel subscriptions. In-memory cache derived from files."""

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
# Surface subscriptions
# ---------------------------------------------------------------------------

class SurfaceSubscriptionRegistry:
    """Tracks adapter-defined surface subscriptions. In-memory cache derived from files.

    Maps canonical surface refs (opaque strings like ``discord:user:116377...``)
    to sets of subscribed session IDs. This is the daemon's many-to-many
    ingress routing primitive for platform surfaces.

    **Routing invariant:** Surface subscriptions, session bindings (one-to-one),
    and bridges are distinct routing mechanisms. A given surface is expected to
    participate in only one of them at a time. Overlapping configuration is
    unsupported and should be treated as an invariant violation / adapter bug.
    The daemon registry does not enforce this; enforcement belongs at the
    adapter/configuration layer when routing constructs are created.
    """

    def __init__(self) -> None:
        self._surfaces: dict[str, set[str]] = {}  # surface_ref -> set of session_ids

    def subscribe(self, surface_ref: str, session_id: str) -> int:
        """Add a subscriber. Returns total subscriber count for this surface."""
        if surface_ref not in self._surfaces:
            self._surfaces[surface_ref] = set()
        self._surfaces[surface_ref].add(session_id)
        return len(self._surfaces[surface_ref])

    def unsubscribe(self, surface_ref: str, session_id: str) -> None:
        """Remove a subscriber. Cleans up empty surfaces."""
        subs = self._surfaces.get(surface_ref)
        if subs:
            subs.discard(session_id)
            if not subs:
                del self._surfaces[surface_ref]

    def unsubscribe_all(self, session_id: str) -> list[str]:
        """Remove a session from all surfaces. Returns list of surfaces left."""
        departed: list[str] = []
        for ref in list(self._surfaces):
            if session_id in self._surfaces[ref]:
                self._surfaces[ref].discard(session_id)
                departed.append(ref)
                if not self._surfaces[ref]:
                    del self._surfaces[ref]
        return departed

    def subscribers(self, surface_ref: str) -> set[str]:
        """Get subscriber session_ids for a surface."""
        return set(self._surfaces.get(surface_ref, set()))

    def surfaces_for(self, session_id: str, adapter_id: str | None = None) -> list[str]:
        """Get surfaces a session is subscribed to.

        If adapter_id is given, filter to surfaces whose ref starts with
        ``adapter_id:`` (the canonical prefix convention).
        """
        results = [ref for ref, subs in self._surfaces.items() if session_id in subs]
        if adapter_id is not None:
            prefix = f"{adapter_id}:"
            results = [ref for ref in results if ref.startswith(prefix)]
        return results

    def all_surfaces(self) -> list[str]:
        """List all surfaces with at least one subscriber."""
        return list(self._surfaces.keys())

    def subscriber_count(self, surface_ref: str) -> int:
        return len(self._surfaces.get(surface_ref, set()))


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

# ---------------------------------------------------------------------------
# File-backed subscription storage
# ---------------------------------------------------------------------------

class SubscriptionStore:
    """Reads and writes per-session subscription files.

    The daemon is the single writer. Files are YAML under:
        subscriptions_dir/channels/<session_id>.yml
        subscriptions_dir/surfaces/<session_id>.yml
    """

    def __init__(self, subscriptions_dir: Path) -> None:
        self._dir = subscriptions_dir
        self._channels_dir = subscriptions_dir / "channels"
        self._surfaces_dir = subscriptions_dir / "surfaces"

    def ensure_dirs(self) -> None:
        self._channels_dir.mkdir(parents=True, exist_ok=True)
        self._surfaces_dir.mkdir(parents=True, exist_ok=True)

    # --- Channel subscriptions ---

    def read_channel_subs(self, session_id: str) -> list[str]:
        path = self._channels_dir / f"{session_id}.yml"
        if not path.exists():
            return []
        try:
            data = yaml.safe_load(path.read_text()) or {}
            return data.get("channels", [])
        except Exception:
            log.warning("Failed to read channel subs for %s", session_id)
            return []

    def write_channel_subs(self, session_id: str, agent: str,
                           channels: list[str]) -> None:
        path = self._channels_dir / f"{session_id}.yml"
        if not channels:
            path.unlink(missing_ok=True)
            return
        data = {"version": 1, "agent": agent, "session": session_id,
                "channels": sorted(channels)}
        path.write_text(yaml.dump(data, default_flow_style=False))

    def read_all_channel_subs(self) -> dict[str, list[str]]:
        """Read all channel subscription files. Returns session_id -> channels."""
        result: dict[str, list[str]] = {}
        if not self._channels_dir.exists():
            return result
        for path in self._channels_dir.glob("*.yml"):
            session_id = path.stem
            channels = self.read_channel_subs(session_id)
            if channels:
                result[session_id] = channels
        return result

    # --- Surface subscriptions ---

    def read_surface_subs(self, session_id: str) -> list[str]:
        path = self._surfaces_dir / f"{session_id}.yml"
        if not path.exists():
            return []
        try:
            data = yaml.safe_load(path.read_text()) or {}
            return data.get("surfaces", [])
        except Exception:
            log.warning("Failed to read surface subs for %s", session_id)
            return []

    def write_surface_subs(self, session_id: str, agent: str,
                           surfaces: list[str]) -> None:
        path = self._surfaces_dir / f"{session_id}.yml"
        if not surfaces:
            path.unlink(missing_ok=True)
            return
        data = {"version": 1, "agent": agent, "session": session_id,
                "surfaces": sorted(surfaces)}
        path.write_text(yaml.dump(data, default_flow_style=False))

    def read_all_surface_subs(self) -> dict[str, list[str]]:
        """Read all surface subscription files. Returns session_id -> surfaces."""
        result: dict[str, list[str]] = {}
        if not self._surfaces_dir.exists():
            return result
        for path in self._surfaces_dir.glob("*.yml"):
            session_id = path.stem
            surfaces = self.read_surface_subs(session_id)
            if surfaces:
                result[session_id] = surfaces
        return result

    def remove_session(self, session_id: str) -> None:
        """Remove all subscription files for a session."""
        for subdir in (self._channels_dir, self._surfaces_dir):
            path = subdir / f"{session_id}.yml"
            path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Tmux-based session liveness
# ---------------------------------------------------------------------------

def get_live_tmux_sessions() -> set[str] | None:
    """Get the set of tmux session names that currently exist.

    Returns None on error (tmux missing, timeout, command failure) to
    distinguish "no sessions" from "couldn't check." Callers must treat
    None as "uncertain" and avoid destructive actions.
    """
    try:
        result = subprocess.run(
            ["tmux", "list-sessions", "-F", "#{session_name}"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            # tmux returns 1 when no server is running (no sessions) —
            # that's a real answer, not an error.
            if "no server running" in (result.stderr or "").lower():
                return set()
            log.warning("tmux list-sessions failed (exit %d): %s",
                        result.returncode, result.stderr.strip())
            return None
        return {line.strip() for line in result.stdout.strip().split("\n")
                if line.strip()}
    except subprocess.TimeoutExpired:
        log.warning("tmux list-sessions timed out")
        return None
    except FileNotFoundError:
        log.warning("tmux not found on PATH")
        return None


# ---------------------------------------------------------------------------
# Combined daemon state
# ---------------------------------------------------------------------------

class DaemonState:
    """Aggregate of all daemon-managed state.

    In-memory registries are derived caches. The ``store`` provides
    durable file-backed subscription truth.
    """

    def __init__(self, subscriptions_dir: Path | None = None) -> None:
        self.presence = PresenceRegistry()
        self.channels = ChannelRegistry()
        self.surfaces = SurfaceSubscriptionRegistry()
        self.bridges = BridgeRegistry()
        self.store = SubscriptionStore(
            subscriptions_dir or Path.home() / ".kiln" / "daemon" / "state" / "subscriptions"
        )

    def load_from_files(self) -> None:
        """Rebuild in-memory registries from durable subscription files."""
        self.store.ensure_dirs()

        # Load channel subscriptions
        for session_id, channels in self.store.read_all_channel_subs().items():
            for ch in channels:
                self.channels.subscribe(ch, session_id)

        # Load surface subscriptions
        for session_id, surfaces in self.store.read_all_surface_subs().items():
            for surf in surfaces:
                self.surfaces.subscribe(surf, session_id)

        log.info(
            "Loaded state from files: %d channel subs, %d surface subs",
            len(self.channels.all_channels()),
            len(self.surfaces.all_surfaces()),
        )

    def reconcile(
        self, agents_registry: dict[str, Path] | None = None,
    ) -> tuple[list[str], list[str]]:
        """Reconcile in-memory state against tmux liveness.

        Two-way reconciliation:
        1. **Discover** — register live tmux sessions that match known
           agent prefixes but aren't in presence yet.
        2. **Prune** — remove subscriptions and presence for sessions
           that no longer exist in tmux.

        Returns ``(pruned, discovered)`` session ID lists.

        If tmux is unavailable (returns None), skips entirely to avoid
        destroying subscription truth on a transient failure.
        """
        live_sessions = get_live_tmux_sessions()
        if live_sessions is None:
            log.warning("Reconcile skipped — tmux status uncertain")
            return [], []

        # --- Discover live agent sessions not yet in presence ---
        discovered: list[str] = []
        if agents_registry:
            for tmux_name in live_sessions:
                if self.presence.get(tmux_name):
                    continue
                # Agent session names follow <prefix>-<adj>-<noun>
                parts = tmux_name.split("-")
                if len(parts) >= 3 and parts[0] in agents_registry:
                    agent_home = agents_registry[parts[0]]
                    mode, tags = _load_live_session_metadata(agent_home, tmux_name)
                    record = SessionRecord(
                        session_id=tmux_name,
                        agent_name=parts[0],
                        agent_home=str(agent_home),
                        mode=mode,
                        tags=tags,
                    )

                    self.presence.register(record)
                    discovered.append(tmux_name)

        if discovered:
            log.info("Reconcile discovered %d live sessions: %s",
                     len(discovered), discovered)

        # --- Prune dead sessions ---
        known_sessions = (
            self.presence.session_ids()
            | set(self._sessions_with_subscriptions())
        )

        pruned: list[str] = []
        for session_id in known_sessions:
            if session_id not in live_sessions:
                self._prune_session(session_id)
                pruned.append(session_id)

        if pruned:
            log.info("Reconcile pruned %d dead sessions: %s", len(pruned), pruned)
        return pruned, discovered

    def _sessions_with_subscriptions(self) -> set[str]:
        """Get all session IDs that have any subscriptions."""
        sessions: set[str] = set()
        for ch in self.channels.all_channels():
            sessions.update(self.channels.subscribers(ch))
        for surf in self.surfaces.all_surfaces():
            sessions.update(self.surfaces.subscribers(surf))
        return sessions

    def _prune_session(self, session_id: str) -> None:
        """Remove all state for a dead session."""
        self.channels.unsubscribe_all(session_id)
        self.surfaces.unsubscribe_all(session_id)
        self.presence.deregister(session_id)
        self.store.remove_session(session_id)
