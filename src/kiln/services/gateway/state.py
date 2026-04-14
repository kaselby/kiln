"""Gateway-owned state — surface subscriptions and bridge definitions.

These registries are only meaningful when the gateway service is active.
When gateway is disabled, these classes are never instantiated and the
daemon carries zero platform vocabulary in its state.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


class SurfaceSubscriptionRegistry:
    """Tracks adapter-defined surface subscriptions. In-memory cache derived from files.

    Maps canonical surface refs (opaque strings like ``discord:user:116377...``)
    to sets of subscribed session IDs. This is the gateway's many-to-many
    ingress routing primitive for platform surfaces.
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
