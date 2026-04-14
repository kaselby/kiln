"""Daemon core state — presence, channel subscriptions, file-backed storage.

Files are the source of truth. In-memory registries are derived caches
rebuilt from files on startup and kept in sync on mutation.

Service-specific state (surfaces, bridges) lives in the owning service
module — see ``kiln.services.gateway.state``.

Layout under ``subscriptions_dir`` (default ``~/.kiln/daemon/state/subscriptions/``):
    channels/<session_id>.yml   — Kiln channel subscriptions
    surfaces/<session_id>.yml   — platform surface subscriptions (gateway-owned)
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


# ---------------------------------------------------------------------------
# Session presence
# ---------------------------------------------------------------------------

@dataclass
class SessionRecord:
    """A known live agent session (derived from tmux + files)."""

    session_id: str
    agent_name: str
    agent_home: str
    pid: int = 0
    first_seen_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_seen_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    mode: str = "supervised"
    status: str = "running"
    thread_ids: dict[str, str] = field(default_factory=dict)
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
    """Core daemon state — presence and channel subscriptions.

    In-memory registries are derived caches. The ``store`` provides
    durable file-backed subscription truth.

    Services extend the daemon's state with their own registries
    (e.g. gateway adds surfaces and bridges). The daemon core only
    manages presence and channels.
    """

    def __init__(self, subscriptions_dir: Path | None = None) -> None:
        self.presence = PresenceRegistry()
        self.channels = ChannelRegistry()
        self.store = SubscriptionStore(
            subscriptions_dir or Path.home() / ".kiln" / "daemon" / "state" / "subscriptions"
        )
        # Hooks for services to participate in reconciliation.
        # Each callback receives a session_id and should clean up
        # any service-owned state for that session.
        self._prune_hooks: list[Any] = []

    def add_prune_hook(self, hook: Any) -> None:
        """Register a callback for session pruning. Called with (session_id,)."""
        self._prune_hooks.append(hook)

    def remove_prune_hook(self, hook: Any) -> None:
        self._prune_hooks = [h for h in self._prune_hooks if h is not hook]

    def load_from_files(self) -> None:
        """Rebuild core in-memory registries from durable subscription files."""
        self.store.ensure_dirs()

        # Load channel subscriptions
        for session_id, channels in self.store.read_all_channel_subs().items():
            for ch in channels:
                self.channels.subscribe(ch, session_id)

        log.info(
            "Loaded state from files: %d channel subs",
            len(self.channels.all_channels()),
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
                    record = SessionRecord(
                        session_id=tmux_name,
                        agent_name=parts[0],
                        agent_home=str(agent_home),
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
        return sessions

    def _prune_session(self, session_id: str) -> None:
        """Remove all state for a dead session."""
        self.channels.unsubscribe_all(session_id)
        self.presence.deregister(session_id)
        self.store.remove_session(session_id)
        # Notify services to clean up their own state
        for hook in self._prune_hooks:
            try:
                hook(session_id)
            except Exception:
                log.exception("Error in prune hook for session %s", session_id)
