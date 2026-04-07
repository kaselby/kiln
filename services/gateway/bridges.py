"""Bridge manager — bidirectional sync between platform surfaces and Kiln channels.

A bridge connects a platform surface (Discord thread, Slack channel, etc.) to
a Kiln inter-agent channel. Messages flow both directions:

  Inbound:  platform surface → Kiln channel (history + subscriber delivery)
  Outbound: Kiln channel → platform surface (forwarded by polling history)

Bridges are platform-agnostic at this level. Platform plugins provide the
transport (send_message), and register bridges when surfaces are created.
The BridgeManager handles the Kiln channel side uniformly.
"""

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Coroutine

log = logging.getLogger("gateway.bridges")


@dataclass
class Bridge:
    """A bidirectional link between a platform surface and a Kiln channel."""

    kiln_channel: str       # Kiln channel name (e.g. "gateway", "general")
    platform: str           # Platform name (e.g. "discord")
    surface_id: str         # Platform-specific surface ID (e.g. thread ID)
    surface_desc: str = ""  # Human-readable description (e.g. "#channels/gateway")


# Type for the async send callback: (surface_id, content) -> None
SendCallback = Callable[[str, str], Coroutine[Any, Any, None]]


class BridgeManager:
    """Manages bridges between platform surfaces and Kiln channels.

    Platform plugins register bridges and provide send callbacks for outbound
    messages. The BridgeManager handles inbound injection and outbound polling.
    """

    def __init__(self, agent_home: Path):
        self._agent_home = agent_home
        # surface_id → Bridge
        self._by_surface: dict[str, Bridge] = {}
        # kiln_channel → Bridge
        self._by_channel: dict[str, Bridge] = {}
        # platform → send callback for outbound messages
        self._send_callbacks: dict[str, SendCallback] = {}
        # kiln_channel → file position for outbound polling
        self._file_positions: dict[str, int] = {}

    def register_send_callback(self, platform: str, callback: SendCallback) -> None:
        """Register the send function for a platform.

        Called once per platform during setup. The callback sends a formatted
        message to a platform surface by its surface_id.
        """
        self._send_callbacks[platform] = callback

    def register(self, bridge: Bridge) -> None:
        """Register a bridge. Skips to end of channel history to avoid replay."""
        self._by_surface[bridge.surface_id] = bridge
        self._by_channel[bridge.kiln_channel] = bridge

        # Skip to current end of history to avoid replaying old messages
        history = self._agent_home / "channels" / bridge.kiln_channel / "history.jsonl"
        if history.exists():
            self._file_positions[bridge.kiln_channel] = history.stat().st_size

        log.info("Registered bridge: %s ↔ %s [%s]",
                 bridge.kiln_channel, bridge.surface_desc or bridge.surface_id,
                 bridge.platform)

    def unregister_surface(self, surface_id: str) -> None:
        """Remove a bridge by surface ID."""
        bridge = self._by_surface.pop(surface_id, None)
        if bridge:
            self._by_channel.pop(bridge.kiln_channel, None)
            self._file_positions.pop(bridge.kiln_channel, None)
            log.info("Unregistered bridge: %s", bridge.kiln_channel)

    def unregister_channel(self, kiln_channel: str) -> None:
        """Remove a bridge by Kiln channel name."""
        bridge = self._by_channel.pop(kiln_channel, None)
        if bridge:
            self._by_surface.pop(bridge.surface_id, None)
            self._file_positions.pop(kiln_channel, None)
            log.info("Unregistered bridge: %s", kiln_channel)

    def get_by_surface(self, surface_id: str) -> Bridge | None:
        """Look up a bridge by platform surface ID."""
        return self._by_surface.get(surface_id)

    def get_by_channel(self, kiln_channel: str) -> Bridge | None:
        """Look up a bridge by Kiln channel name."""
        return self._by_channel.get(kiln_channel)

    @property
    def bridges(self) -> list[Bridge]:
        """All registered bridges."""
        return list(self._by_surface.values())

    # -------------------------------------------------------------------
    # Inbound: platform surface → Kiln channel
    # -------------------------------------------------------------------

    def inject_inbound(self, surface_id: str, sender_name: str, content: str) -> bool:
        """Inject a platform message into the bridged Kiln channel.

        Returns True if a bridge was found and the message was injected.
        """
        bridge = self._by_surface.get(surface_id)
        if not bridge:
            return False

        sender_id = f"{bridge.platform}-{sender_name}"
        summary = content[:200]

        # 1. Deliver to Kiln channel subscribers
        self._deliver_to_subscribers(bridge.kiln_channel, sender_id, summary, content)

        # 2. Append to channel history with source tag for echo prevention
        self._append_history(bridge.kiln_channel, sender_id, summary, content,
                            source=bridge.platform)

        log.info("Bridge inbound: %s → channel '%s' from %s",
                 bridge.surface_desc or surface_id, bridge.kiln_channel, sender_name)
        return True

    def _deliver_to_subscribers(self, channel_name: str, sender_id: str,
                                summary: str, content: str) -> None:
        """Deliver a message to all Kiln channel subscribers."""
        from kiln.tools import send_to_inbox, _resolve_recipient_inbox

        inbox_root = self._agent_home / "inbox"
        channels_path = self._agent_home / "channels.json"

        try:
            ch_data = json.loads(channels_path.read_text()) if channels_path.exists() else {}
        except (json.JSONDecodeError, OSError):
            ch_data = {}

        subscribers = ch_data.get(channel_name, [])
        delivered = 0
        for subscriber in subscribers:
            recipient_inbox_root = _resolve_recipient_inbox(subscriber, inbox_root)
            send_to_inbox(
                recipient_inbox_root, subscriber, sender_id,
                summary, content, "normal", channel=channel_name,
            )
            delivered += 1

        if delivered:
            log.info("Delivered to %d channel subscriber(s)", delivered)

    def _append_history(self, channel_name: str, sender_id: str,
                        summary: str, content: str, source: str = "") -> None:
        """Append a message to Kiln channel history."""
        history_dir = self._agent_home / "channels" / channel_name
        history_dir.mkdir(parents=True, exist_ok=True)

        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "from": sender_id,
            "summary": summary,
            "body": content,
            "priority": "normal",
        }
        if source:
            entry["source"] = source

        with open(history_dir / "history.jsonl", "a") as f:
            f.write(json.dumps(entry) + "\n")

    # -------------------------------------------------------------------
    # Outbound: Kiln channel → platform surface
    # -------------------------------------------------------------------

    async def forward_outbound(self) -> None:
        """Poll all bridged Kiln channels and forward new messages to platforms.

        Call this periodically from the gateway's event loop.
        """
        for channel_name, bridge in list(self._by_channel.items()):
            messages = self._read_new_messages(channel_name)
            if not messages:
                continue

            callback = self._send_callbacks.get(bridge.platform)
            if not callback:
                log.warning("No send callback for platform '%s'", bridge.platform)
                continue

            for msg in messages:
                # Echo prevention: skip messages that originated from this platform
                if msg.get("source") == bridge.platform:
                    continue

                formatted = self._format_outbound(msg)
                if formatted:
                    try:
                        await callback(bridge.surface_id, formatted)
                    except Exception:
                        log.exception("Failed to forward to %s", bridge.surface_id)

    def _read_new_messages(self, channel_name: str) -> list[dict]:
        """Read new messages from a Kiln channel's history file."""
        history = self._agent_home / "channels" / channel_name / "history.jsonl"
        if not history.exists():
            return []

        pos = self._file_positions.get(channel_name, 0)
        current_size = history.stat().st_size
        if current_size <= pos:
            return []

        messages = []
        with open(history) as f:
            f.seek(pos)
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    messages.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
            self._file_positions[channel_name] = f.tell()

        return messages

    @staticmethod
    def _format_outbound(msg: dict) -> str | None:
        """Format a Kiln channel message for platform display."""
        sender = msg.get("from", "unknown")
        body = msg.get("body", "")
        summary = msg.get("summary", "")
        text = body or summary
        if not text:
            return None
        return f"**{sender}:** {text}"
