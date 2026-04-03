"""Subscription manager — routes platform messages to agent sessions.

Sessions declare which platform surfaces they want to receive messages from
via their session config files (state/session-config-*.yml). The gateway
scans these periodically to build a surface_id → [agent_ids] lookup.

Surface IDs are platform-specific strings: Discord channel/thread IDs,
"dm/<user_id>" for DMs, etc. The subscription manager doesn't interpret
them — it just maps surfaces to subscribers.
"""

import logging
import time
from pathlib import Path

import yaml

REFRESH_TTL = 5.0  # seconds between full rescans

log = logging.getLogger("gateway.subscriptions")


class SubscriptionManager:
    """Maps platform surface IDs to subscribing agent sessions."""

    def __init__(self, state_dir: Path):
        self._state_dir = state_dir
        # surface_id → set of agent IDs
        self._subscriptions: dict[str, set[str]] = {}
        self._last_refresh: float = 0

    def refresh(self, force: bool = False) -> None:
        """Scan session config files and rebuild the subscription map.

        Skips rescan if called within REFRESH_TTL seconds (unless force=True).
        """
        now = time.monotonic()
        if not force and (now - self._last_refresh) < REFRESH_TTL:
            return
        self._last_refresh = now

        new_subs: dict[str, set[str]] = {}

        for config_file in self._state_dir.glob("session-config-*.yml"):
            agent_id = config_file.stem.removeprefix("session-config-")
            try:
                data = yaml.safe_load(config_file.read_text()) or {}
            except (yaml.YAMLError, OSError):
                log.warning("Failed to read session config: %s", config_file)
                continue

            surfaces = data.get("subscriptions", [])
            for surface_id in surfaces:
                surface_id = str(surface_id)
                new_subs.setdefault(surface_id, set()).add(agent_id)

        self._subscriptions = new_subs

    def get_subscribers(self, surface_id: str) -> set[str]:
        """Get all agent IDs subscribed to a surface."""
        return self._subscriptions.get(surface_id, set())

    def all_surfaces(self) -> set[str]:
        """Get all surfaces with at least one subscriber."""
        return set(self._subscriptions.keys())

    def subscriptions_for(self, agent_id: str) -> set[str]:
        """Get all surfaces an agent is subscribed to."""
        return {
            surface
            for surface, agents in self._subscriptions.items()
            if agent_id in agents
        }

    @staticmethod
    def subscribe(state_dir: Path, agent_id: str, surface_id: str) -> None:
        """Add a subscription to a session's config file."""
        config_file = state_dir / f"session-config-{agent_id}.yml"
        try:
            data = yaml.safe_load(config_file.read_text()) or {} if config_file.exists() else {}
        except (yaml.YAMLError, OSError):
            data = {}

        surfaces = data.get("subscriptions", [])
        surface_id = str(surface_id)
        if surface_id not in surfaces:
            surfaces.append(surface_id)
            data["subscriptions"] = surfaces
            config_file.write_text(yaml.dump(data, default_flow_style=False))

    @staticmethod
    def unsubscribe(state_dir: Path, agent_id: str, surface_id: str) -> None:
        """Remove a subscription from a session's config file."""
        config_file = state_dir / f"session-config-{agent_id}.yml"
        if not config_file.exists():
            return

        try:
            data = yaml.safe_load(config_file.read_text()) or {}
        except (yaml.YAMLError, OSError):
            return

        surfaces = data.get("subscriptions", [])
        surface_id = str(surface_id)
        if surface_id in surfaces:
            surfaces.remove(surface_id)
            data["subscriptions"] = surfaces
            config_file.write_text(yaml.dump(data, default_flow_style=False))
