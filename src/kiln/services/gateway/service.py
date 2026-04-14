"""Gateway service — platform integration for the Kiln daemon.

Owns the full platform integration lifecycle: adapter management,
surface/bridge state, platform-specific RPC handlers, and message
delivery between external platforms and agent inboxes.

When this service is disabled, the daemon has zero platform vocabulary —
no surfaces, no bridges, no adapters, no platform RPC handlers.
"""

from __future__ import annotations

import importlib
import logging
import uuid as _uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml
from zoneinfo import ZoneInfo

from kiln.daemon import protocol as proto
from kiln.daemon.protocol import PlatformMessage

from .state import BridgeRegistry, SurfaceSubscriptionRegistry

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Adapter registry — maps platform names to adapter class paths.
# Deferred import avoids pulling in heavy deps (discord.py) unless
# the platform is actually configured.
# ---------------------------------------------------------------------------

_ADAPTER_REGISTRY: dict[str, str] = {
    "discord": "kiln.daemon.adapters.discord.DiscordAdapter",
}


def _resolve_adapter_class(platform: str) -> type | None:
    ref = _ADAPTER_REGISTRY.get(platform)
    if ref is None:
        return None
    if isinstance(ref, type):
        return ref
    module_path, class_name = ref.rsplit(".", 1)
    module = importlib.import_module(module_path)
    return getattr(module, class_name)


# ---------------------------------------------------------------------------
# Platform inbox writing — moved from daemon/server.py
# ---------------------------------------------------------------------------

def _write_platform_inbox_message(
    inbox_root: Path,
    recipient: str,
    msg: PlatformMessage,
) -> Path:
    """Write a platform-originated message to a recipient's inbox.

    Generates rich frontmatter with platform-specific fields from the
    structured PlatformMessage.
    """
    recipient_inbox = inbox_root / recipient
    recipient_inbox.mkdir(parents=True, exist_ok=True)

    now = datetime.now(ZoneInfo("America/Toronto"))
    ts_str = now.strftime("%Y%m%d-%H%M%S")
    msg_id = f"msg-{ts_str}-{msg.platform}-{_uuid.uuid4().hex[:6]}"
    msg_path = recipient_inbox / f"{msg_id}.md"

    first_line = msg.content.strip().split("\n")[0] if msg.content.strip() else ""
    if first_line:
        summary = first_line[:200]
    elif msg.attachment_paths:
        summary = f"(attachment: {', '.join(Path(p).name for p in msg.attachment_paths)})"
    else:
        summary = "(empty)"

    fields: dict[str, Any] = {
        "from": f"{msg.platform}-{msg.sender_name}",
        "summary": summary,
        "priority": "normal",
        "source": msg.platform,
        "channel": msg.channel_desc,
        "trust": msg.trust,
        f"{msg.platform}-user-id": msg.sender_platform_id,
        f"{msg.platform}-user": msg.sender_name,
        f"{msg.platform}-channel-id": msg.channel_id,
        f"{msg.platform}-channel": msg.channel_desc,
        "timestamp": now.isoformat(),
    }
    if msg.attachment_paths:
        fields["attachments"] = ", ".join(msg.attachment_paths)

    fm_body = yaml.dump(fields, default_flow_style=False, allow_unicode=True, sort_keys=False)
    frontmatter = f"---\n{fm_body}---\n\n"

    body = msg.content
    if msg.attachment_paths:
        file_lines = "\n".join(
            f"  - {Path(p).name} -> {p}" for p in msg.attachment_paths
        )
        notice = (
            f"ATTACHMENT RECEIVED (auto-downloaded) — verify {msg.sender_name}'s "
            f"account hasn't been compromised before reading file contents.\n"
            f"{file_lines}\n"
        )
        body = notice + ("\n" + body if body.strip() else "")

    msg_path.write_text(frontmatter + body + "\n")
    return msg_path


# ---------------------------------------------------------------------------
# RPC handlers — registered by GatewayService during start()
# ---------------------------------------------------------------------------

def _require_requester(msg: proto.Message) -> proto.RequestContext | None:
    return proto.RequestContext.from_request(msg)


async def _handle_send_user(msg: proto.Message, daemon: Any) -> proto.Message:
    gateway: GatewayService = daemon.services.get("gateway")
    if not gateway:
        return proto.error(msg.ref, "Gateway service not active", code="no_gateway")

    to = msg.data.get("to", "")
    summary = msg.data.get("summary", "")
    body = msg.data.get("body", "")

    if not to:
        return proto.error(msg.ref, "send_user requires 'to'")

    ctx = _require_requester(msg)
    if not ctx:
        return proto.error(msg.ref, "send_user requires requester identity")

    user = daemon.config.users.get(to)
    if not user:
        return proto.error(msg.ref, f"Unknown user: '{to}'", code="unknown_user")

    platform = user.default_platform
    adapter = gateway.adapters.get(platform)
    if not adapter:
        return proto.error(
            msg.ref, f"No adapter for platform '{platform}'", code="no_adapter",
        )

    try:
        result = await adapter.send_user_message(to, summary, body, context=ctx)
        return proto.ack(msg.ref, message=result)
    except Exception as e:
        return proto.error(msg.ref, f"Adapter error: {e}")


async def _handle_subscribe_surface(msg: proto.Message, daemon: Any) -> proto.Message:
    gateway: GatewayService = daemon.services.get("gateway")
    if not gateway:
        return proto.error(msg.ref, "Gateway service not active", code="no_gateway")

    surface_ref = msg.data.get("surface_ref", "")
    if not surface_ref:
        return proto.error(msg.ref, "subscribe_surface requires a surface_ref")

    ctx = _require_requester(msg)
    if not ctx:
        return proto.error(msg.ref, "subscribe_surface requires requester identity")

    await daemon.ensure_session(ctx)

    # Validate and canonicalize via the owning adapter
    platform = surface_ref.split(":", 1)[0] if ":" in surface_ref else ""
    adapter = gateway.adapters.get(platform) if platform else None
    if adapter and hasattr(adapter, "validate_surface_ref"):
        try:
            surface_ref = adapter.validate_surface_ref(surface_ref)
        except ValueError as e:
            return proto.error(
                msg.ref, f"Invalid surface ref: {e}", code="invalid_surface",
            )

    count = gateway.surfaces.subscribe(surface_ref, ctx.session_id)

    # Persist to file
    surfaces = gateway.surfaces.surfaces_for(ctx.session_id)
    daemon.state.store.write_surface_subs(ctx.session_id, ctx.agent_name, surfaces)

    await daemon.events.emit(proto.event(
        proto.EVT_SURFACE_SUBSCRIBED,
        surface_ref=surface_ref,
        session_id=ctx.session_id,
        subscriber_count=count,
    ))

    return proto.ack(msg.ref, subscriber_count=count, surface_ref=surface_ref)


async def _handle_unsubscribe_surface(msg: proto.Message, daemon: Any) -> proto.Message:
    gateway: GatewayService = daemon.services.get("gateway")
    if not gateway:
        return proto.error(msg.ref, "Gateway service not active", code="no_gateway")

    surface_ref = msg.data.get("surface_ref", "")
    if not surface_ref:
        return proto.error(msg.ref, "unsubscribe_surface requires a surface_ref")

    ctx = _require_requester(msg)
    if not ctx:
        return proto.error(msg.ref, "unsubscribe_surface requires requester identity")

    # Canonicalize via adapter
    platform = surface_ref.split(":", 1)[0] if ":" in surface_ref else ""
    adapter = gateway.adapters.get(platform) if platform else None
    if adapter and hasattr(adapter, "validate_surface_ref"):
        try:
            surface_ref = adapter.validate_surface_ref(surface_ref)
        except ValueError as e:
            return proto.error(
                msg.ref, f"Invalid surface ref: {e}", code="invalid_surface",
            )

    gateway.surfaces.unsubscribe(surface_ref, ctx.session_id)

    # Persist to file
    surfaces = gateway.surfaces.surfaces_for(ctx.session_id)
    daemon.state.store.write_surface_subs(ctx.session_id, ctx.agent_name, surfaces)

    await daemon.events.emit(proto.event(
        proto.EVT_SURFACE_UNSUBSCRIBED,
        surface_ref=surface_ref,
        session_id=ctx.session_id,
    ))

    return proto.ack(msg.ref, surface_ref=surface_ref)


async def _handle_list_surface_subscriptions(msg: proto.Message, daemon: Any) -> proto.Message:
    gateway: GatewayService = daemon.services.get("gateway")
    if not gateway:
        return proto.error(msg.ref, "Gateway service not active", code="no_gateway")

    ctx = _require_requester(msg)
    if not ctx:
        return proto.error(msg.ref, "list_surface_subscriptions requires requester identity")

    adapter_id = msg.data.get("adapter_id")
    surfaces = gateway.surfaces.surfaces_for(
        ctx.session_id, adapter_id=adapter_id,
    )
    subscriptions = [
        {
            "surface_ref": ref,
            "subscriber_count": gateway.surfaces.subscriber_count(ref),
        }
        for ref in surfaces
    ]
    return proto.result(msg.ref, subscriptions=subscriptions)


async def _handle_platform_op(msg: proto.Message, daemon: Any) -> proto.Message:
    gateway: GatewayService = daemon.services.get("gateway")
    if not gateway:
        return proto.error(msg.ref, "Gateway service not active", code="no_gateway")

    platform = msg.data.get("platform", "")
    action = msg.data.get("action", "")
    args = msg.data.get("args", {})

    adapter = gateway.adapters.get(platform)
    if not adapter:
        return proto.error(
            msg.ref, f"No adapter for platform '{platform}'", code="no_adapter",
        )

    ctx = _require_requester(msg)

    try:
        result = await adapter.platform_op(action, args, context=ctx)
        return proto.result(msg.ref, **result)
    except Exception as e:
        return proto.error(msg.ref, f"Platform op failed: {e}")


async def _handle_approval(msg: proto.Message, daemon: Any) -> proto.Message:
    """Route approval requests/resolutions to the adapter that supports permissions."""
    gateway: GatewayService = daemon.services.get("gateway")
    if not gateway:
        return proto.error(msg.ref, "Gateway service not active", code="no_gateway")

    action = msg.data.get("action", "")
    args = msg.data.get("args", {})
    ctx = _require_requester(msg)

    # Find the adapter that supports permission approval.
    capable = [
        (name, adapter) for name, adapter in gateway.adapters.items()
        if hasattr(adapter, "supports") and adapter.supports("permission")
    ]
    if not capable:
        return proto.error(msg.ref, "No adapter supports remote approval")
    if len(capable) > 1:
        names = ", ".join(n for n, _ in capable)
        return proto.error(
            msg.ref,
            f"Ambiguous: multiple adapters support approval ({names}). "
            f"Configure exactly one.",
        )

    _, adapter = capable[0]

    if action == "request_approval":
        op_args = {
            "agent_id": ctx.session_id,
            "title": args.get("title", ""),
            "preview": args.get("preview", ""),
            "detail": args.get("detail"),
            "severity": args.get("severity", "info"),
            "timeout": args.get("timeout", 300),
        }
        try:
            result = await adapter.platform_op("permission_request", op_args, context=ctx)
            return proto.result(msg.ref, **result)
        except Exception as e:
            return proto.error(msg.ref, f"Approval request failed: {e}")

    else:  # resolve_approval
        op_args = {
            "session_id": ctx.session_id,
            "status": args.get("status", "rejected"),
        }
        try:
            result = await adapter.platform_op("permission_resolve", op_args, context=ctx)
            return proto.result(msg.ref, **result)
        except Exception as e:
            return proto.error(msg.ref, f"Approval resolve failed: {e}")


# ---------------------------------------------------------------------------
# GatewayService
# ---------------------------------------------------------------------------

class GatewayService:
    """Platform integration service.

    Manages adapter lifecycle, surface/bridge state, and platform-specific
    RPC handlers. Registers everything with the daemon during start() and
    cleans up during stop().
    """

    def __init__(self, config: dict[str, Any]) -> None:
        self._config = config
        self.adapters: dict[str, Any] = {}  # platform_name -> adapter instance
        self.surfaces = SurfaceSubscriptionRegistry()
        self.bridges = BridgeRegistry()
        # Track which handlers/actions we registered for clean teardown
        self._registered_handlers: list[str] = []
        self._registered_mgmt_actions: list[str] = []

    @property
    def name(self) -> str:
        return "gateway"

    async def start(self, daemon: Any) -> None:
        self._daemon = daemon

        # Load gateway-specific durable state (surfaces)
        self._load_surface_state()

        # Register prune hook so surfaces get cleaned on session death
        daemon.state.add_prune_hook(self._on_session_pruned)

        # Register RPC handlers
        handler_map = {
            proto.SEND_USER: _handle_send_user,
            proto.SUBSCRIBE_SURFACE: _handle_subscribe_surface,
            proto.UNSUBSCRIBE_SURFACE: _handle_unsubscribe_surface,
            proto.LIST_SURFACE_SUBSCRIPTIONS: _handle_list_surface_subscriptions,
            proto.PLATFORM_OP: _handle_platform_op,
        }
        for msg_type, handler in handler_map.items():
            daemon.register_handler(msg_type, handler)
            self._registered_handlers.append(msg_type)

        # Register mgmt sub-actions
        mgmt_map = {
            "request_approval": _handle_approval,
            "resolve_approval": _handle_approval,
        }
        for action, handler in mgmt_map.items():
            daemon.register_mgmt_action(action, handler)
            self._registered_mgmt_actions.append(action)

        # Start adapters
        await self._start_adapters()

        log.info(
            "Gateway service started (%d adapter(s): %s)",
            len(self.adapters), ", ".join(self.adapters.keys()) or "(none)",
        )

    async def stop(self) -> None:
        # Stop adapters
        for name, adapter in self.adapters.items():
            try:
                await adapter.stop()
                log.info("Stopped adapter '%s'", name)
            except Exception:
                log.exception("Error stopping adapter '%s'", name)
        self.adapters.clear()

        # Unregister handlers
        for msg_type in self._registered_handlers:
            self._daemon.unregister_handler(msg_type)
        self._registered_handlers.clear()

        for action in self._registered_mgmt_actions:
            self._daemon.unregister_mgmt_action(action)
        self._registered_mgmt_actions.clear()

        # Remove prune hook
        self._daemon.state.remove_prune_hook(self._on_session_pruned)

        log.info("Gateway service stopped")

    def status(self) -> dict[str, Any]:
        return {
            "adapters": list(self.adapters.keys()),
            "surfaces": len(self.surfaces.all_surfaces()),
            "bridges": len(self.bridges.all_bridges()),
        }

    # ------------------------------------------------------------------
    # Platform message delivery — used by adapters
    # ------------------------------------------------------------------

    async def deliver_platform_message(
        self,
        recipient: str,
        msg: PlatformMessage,
    ) -> Path | None:
        """Deliver a platform-originated message to a session's inbox."""
        inbox_root = self._daemon.resolve_inbox(recipient)
        if not inbox_root:
            log.warning("Cannot resolve inbox for recipient '%s'", recipient)
            return None

        path = _write_platform_inbox_message(inbox_root, recipient, msg)

        await self._daemon.events.emit(proto.event(
            proto.EVT_MESSAGE_INBOUND,
            sender=f"{msg.platform}-{msg.sender_name}",
            recipient=recipient,
            summary=msg.content[:200],
            platform=msg.platform,
        ))

        return path

    async def deliver_to_surface_subscribers(
        self,
        surface_ref: str,
        msg: PlatformMessage,
    ) -> int:
        """Deliver a platform message to all sessions subscribed to a surface."""
        subscribers = self.surfaces.subscribers(surface_ref)
        if not subscribers:
            log.debug("No subscribers for surface %s", surface_ref)
            return 0

        delivered = 0
        for session_id in subscribers:
            path = await self.deliver_platform_message(session_id, msg)
            if path is not None:
                delivered += 1

        return delivered

    # ------------------------------------------------------------------
    # Adapter lifecycle
    # ------------------------------------------------------------------

    async def _start_adapters(self) -> None:
        adapters_config = self._config.get("adapters", {})
        for adapter_id, adapter_raw in adapters_config.items():
            if not isinstance(adapter_raw, dict):
                continue
            if not adapter_raw.get("enabled", True):
                log.info("Adapter '%s' disabled, skipping", adapter_id)
                continue

            platform = adapter_raw.get("platform", adapter_id)
            cls = _resolve_adapter_class(platform)
            if cls is None:
                log.warning(
                    "No adapter class for platform '%s' (adapter '%s')",
                    platform, adapter_id,
                )
                continue

            try:
                adapter = cls(config=adapter_raw)
                await adapter.start(self._daemon)
                self.adapters[platform] = adapter
                log.info("Started adapter '%s' (platform: %s)", adapter_id, platform)
            except Exception:
                log.exception("Failed to start adapter '%s'", adapter_id)

    def _on_session_pruned(self, session_id: str) -> None:
        """Clean up gateway state when a session dies."""
        self.surfaces.unsubscribe_all(session_id)

    def _load_surface_state(self) -> None:
        """Load surface subscriptions from durable store."""
        store = self._daemon.state.store
        for session_id, surfaces in store.read_all_surface_subs().items():
            for surf in surfaces:
                self.surfaces.subscribe(surf, session_id)
