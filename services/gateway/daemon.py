"""Gateway daemon — HTTP server bridging external platforms to Kiln agents.

Usage:
    python -m kiln.services.gateway.daemon --config <path>
    python -m kiln.services.gateway.daemon --agent-home ~/.agent
"""

import argparse
import asyncio
import json
import logging
import os
import signal
import sys
from pathlib import Path

from aiohttp import web

from .bridges import BridgeManager
from .config import GatewayConfig, load_config
from .channels.base import Channel
from .subscriptions import SubscriptionManager

log = logging.getLogger("gateway")


class GatewayDaemon:
    """Main gateway daemon. Manages channel plugins and serves the HTTP API."""

    def __init__(self, config: GatewayConfig):
        self.config = config
        self.channels: dict[str, Channel] = {}
        self.bridge_manager = BridgeManager(config.agent_home)
        self.subscription_manager = SubscriptionManager(config.agent_home / "state")
        self._app: web.Application | None = None
        self._runner: web.AppRunner | None = None

    async def start(self) -> None:
        """Start channel plugins and HTTP server."""
        log.info("Starting gateway daemon (bind=%s, port=%d)", self.config.bind, self.config.port)

        # Initialize channel plugins
        if self.config.discord and self.config.discord.enabled:
            from .channels.discord import DiscordChannel
            discord_channel = DiscordChannel(
                self.config, self.bridge_manager, self.subscription_manager
            )
            await discord_channel.connect()
            self.channels["discord"] = discord_channel
            log.info("Discord channel connected")

        # Write state file
        self._write_state()

        # Write PID file
        self._write_pid()

        # Start HTTP server
        self._app = self._create_app()
        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.config.bind, self.config.port)
        await site.start()
        log.info("HTTP API listening on %s:%d", self.config.bind, self.config.port)

    async def stop(self) -> None:
        """Graceful shutdown."""
        log.info("Shutting down gateway daemon")

        # Stop HTTP server
        if self._runner:
            await self._runner.cleanup()

        # Disconnect all channels
        for name, channel in self.channels.items():
            try:
                await channel.disconnect()
                log.info("Disconnected %s", name)
            except Exception:
                log.exception("Error disconnecting %s", name)

        # Clean up state/PID files
        self.config.state_file.unlink(missing_ok=True)
        self.config.pid_file.unlink(missing_ok=True)

    def _write_state(self) -> None:
        """Write state file for tool discovery."""
        self.config.state_file.parent.mkdir(parents=True, exist_ok=True)
        state = {
            "pid": os.getpid(),
            "port": self.config.port,
            "bind": self.config.bind,
            "channels": list(self.channels.keys()),
        }
        self.config.state_file.write_text(json.dumps(state, indent=2) + "\n")

    def _write_pid(self) -> None:
        self.config.pid_file.write_text(str(os.getpid()) + "\n")

    # --- HTTP API ---

    def _create_app(self) -> web.Application:
        app = web.Application()
        app.router.add_get("/api/status", self._handle_status)
        app.router.add_post("/api/{platform}/send", self._handle_send)
        app.router.add_get("/api/{platform}/read", self._handle_read)
        app.router.add_post("/api/{platform}/voice/send", self._handle_voice_send)
        app.router.add_post("/api/{platform}/thread/create", self._handle_thread_create)
        app.router.add_post("/api/{platform}/thread/archive", self._handle_thread_archive)
        app.router.add_get("/api/{platform}/channels", self._handle_list_channels)
        app.router.add_post("/api/{platform}/branch/post", self._handle_branch_post)
        app.router.add_post("/api/{platform}/reply", self._handle_reply)
        app.router.add_post("/api/{platform}/delete", self._handle_delete)
        app.router.add_post("/api/subscribe", self._handle_subscribe)
        app.router.add_post("/api/unsubscribe", self._handle_unsubscribe)
        app.router.add_post("/api/permission/request", self._handle_permission_request)
        app.router.add_post("/api/permission/resolve", self._handle_permission_resolve)
        app.router.add_post("/api/{platform}/security/challenge", self._handle_security_challenge)
        return app

    def _get_channel(self, platform: str) -> Channel | None:
        return self.channels.get(platform)

    def _permissions_available(self) -> bool:
        """Check if remote permission approval is configured and the platform is online."""
        perm = self.config.permissions
        if not perm.enabled or not perm.platform:
            return False
        channel = self.channels.get(perm.platform)
        if not channel:
            return False
        return "request_permission" in channel.capabilities()

    async def _handle_status(self, request: web.Request) -> web.Response:
        status = {
            "running": True,
            "pid": os.getpid(),
            "channels": {},
            "permissions": self._permissions_available(),
        }
        for name, ch in self.channels.items():
            status["channels"][name] = {
                "connected": True,
                "capabilities": sorted(ch.capabilities()),
            }
        return web.json_response(status)

    async def _handle_send(self, request: web.Request) -> web.Response:
        platform = request.match_info["platform"]
        channel = self._get_channel(platform)
        if not channel:
            return web.json_response(
                {"ok": False, "error": f"Platform '{platform}' not connected"},
                status=404,
            )

        try:
            body = await request.json()
        except json.JSONDecodeError:
            return web.json_response({"ok": False, "error": "Invalid JSON"}, status=400)

        target = body.get("target", "")
        content = body.get("content", "")
        if not target or not content:
            return web.json_response(
                {"ok": False, "error": "Missing 'target' or 'content'"}, status=400
            )

        kwargs = {}
        if "thread" in body:
            kwargs["thread"] = body["thread"]

        result = await channel.send_message(target, content, **kwargs)
        return web.json_response(result)

    async def _handle_read(self, request: web.Request) -> web.Response:
        platform = request.match_info["platform"]
        channel = self._get_channel(platform)
        if not channel:
            return web.json_response(
                {"ok": False, "error": f"Platform '{platform}' not connected"},
                status=404,
            )

        target = request.query.get("target", "")
        limit = int(request.query.get("limit", "20"))
        if not target:
            return web.json_response(
                {"ok": False, "error": "Missing 'target' query param"}, status=400
            )

        try:
            messages = await channel.read_history(target, limit=limit)
            return web.json_response({"ok": True, "messages": messages})
        except NotImplementedError as e:
            return web.json_response({"ok": False, "error": str(e)}, status=501)

    async def _handle_voice_send(self, request: web.Request) -> web.Response:
        platform = request.match_info["platform"]
        channel = self._get_channel(platform)
        if not channel:
            return web.json_response(
                {"ok": False, "error": f"Platform '{platform}' not connected"},
                status=404,
            )

        try:
            body = await request.json()
        except json.JSONDecodeError:
            return web.json_response({"ok": False, "error": "Invalid JSON"}, status=400)

        target = body.get("target", "")
        text = body.get("text", "")
        if not target or not text:
            return web.json_response(
                {"ok": False, "error": "Missing 'target' or 'text'"}, status=400
            )

        kwargs = {k: v for k, v in body.items() if k not in ("target", "text")}

        try:
            result = await channel.send_voice(target, text, **kwargs)
            return web.json_response(result)
        except NotImplementedError as e:
            return web.json_response({"ok": False, "error": str(e)}, status=501)

    async def _handle_thread_create(self, request: web.Request) -> web.Response:
        platform = request.match_info["platform"]
        channel = self._get_channel(platform)
        if not channel:
            return web.json_response(
                {"ok": False, "error": f"Platform '{platform}' not connected"},
                status=404,
            )

        try:
            body = await request.json()
        except json.JSONDecodeError:
            return web.json_response({"ok": False, "error": "Invalid JSON"}, status=400)

        ch_name = body.get("channel", "")
        name = body.get("name", "")
        if not ch_name or not name:
            return web.json_response(
                {"ok": False, "error": "Missing 'channel' or 'name'"}, status=400
            )

        try:
            result = await channel.create_thread(ch_name, name)
            return web.json_response(result)
        except NotImplementedError as e:
            return web.json_response({"ok": False, "error": str(e)}, status=501)

    async def _handle_thread_archive(self, request: web.Request) -> web.Response:
        platform = request.match_info["platform"]
        channel = self._get_channel(platform)
        if not channel:
            return web.json_response(
                {"ok": False, "error": f"Platform '{platform}' not connected"},
                status=404,
            )

        try:
            body = await request.json()
        except json.JSONDecodeError:
            return web.json_response({"ok": False, "error": "Invalid JSON"}, status=400)

        ch_name = body.get("channel", "")
        name = body.get("name", "")
        if not ch_name or not name:
            return web.json_response(
                {"ok": False, "error": "Missing 'channel' or 'name'"}, status=400
            )

        try:
            await channel.archive_thread(ch_name, name)
            return web.json_response({"ok": True})
        except NotImplementedError as e:
            return web.json_response({"ok": False, "error": str(e)}, status=501)

    async def _handle_list_channels(self, request: web.Request) -> web.Response:
        platform = request.match_info["platform"]
        channel = self._get_channel(platform)
        if not channel:
            return web.json_response(
                {"ok": False, "error": f"Platform '{platform}' not connected"},
                status=404,
            )

        try:
            channels = await channel.list_channels()
            return web.json_response({"ok": True, "channels": channels})
        except NotImplementedError as e:
            return web.json_response({"ok": False, "error": str(e)}, status=501)

    async def _handle_branch_post(self, request: web.Request) -> web.Response:
        platform = request.match_info["platform"]
        channel = self._get_channel(platform)
        if not channel:
            return web.json_response(
                {"ok": False, "error": f"Platform '{platform}' not connected"},
                status=404,
            )

        try:
            body = await request.json()
        except json.JSONDecodeError:
            return web.json_response({"ok": False, "error": "Invalid JSON"}, status=400)

        agent_id = body.get("agent_id", "")
        content = body.get("content", "")
        if not agent_id or not content:
            return web.json_response(
                {"ok": False, "error": "Missing 'agent_id' or 'content'"}, status=400
            )

        if hasattr(channel, "post_to_branch"):
            result = await channel.post_to_branch(agent_id, content)
            return web.json_response(result)
        return web.json_response({"ok": False, "error": "Branch posts not supported"}, status=501)

    async def _handle_reply(self, request: web.Request) -> web.Response:
        """Reply to an inbox message, routing to the original source."""
        platform = request.match_info["platform"]
        channel = self._get_channel(platform)
        if not channel:
            return web.json_response(
                {"ok": False, "error": f"Platform '{platform}' not connected"},
                status=404,
            )

        try:
            body = await request.json()
        except json.JSONDecodeError:
            return web.json_response({"ok": False, "error": "Invalid JSON"}, status=400)

        msg_id = body.get("msg_id", "")
        content = body.get("content", "")
        if not msg_id or not content:
            return web.json_response(
                {"ok": False, "error": "Missing 'msg_id' or 'content'"}, status=400
            )

        # Find the message file by ID prefix match
        inbox_root = self.config.agent_home / "inbox"
        msg_file = None
        for agent_dir in inbox_root.iterdir():
            if not agent_dir.is_dir():
                continue
            for f in agent_dir.glob(f"*{msg_id}*"):
                if f.suffix == ".md":
                    msg_file = f
                    break
            if msg_file:
                break

        if not msg_file:
            return web.json_response(
                {"ok": False, "error": f"Message not found: {msg_id}"}, status=404
            )

        # Parse frontmatter for routing info
        text = msg_file.read_text()
        meta = {}
        if text.startswith("---"):
            lines = text.split("\n")
            for line in lines[1:]:
                if line.strip() == "---":
                    break
                if ":" in line:
                    key, _, val = line.partition(":")
                    meta[key.strip()] = val.strip().strip('"').strip("'")

        # Route to the original channel
        channel_id = meta.get(f"{platform}-channel-id", "")
        if not channel_id:
            return web.json_response(
                {"ok": False, "error": "No channel ID in original message"}, status=400
            )

        result = await channel.send_message(channel_id, content)
        return web.json_response(result)

    async def _handle_delete(self, request: web.Request) -> web.Response:
        """Delete a message by ID from a channel."""
        platform = request.match_info["platform"]
        channel = self._get_channel(platform)
        if not channel:
            return web.json_response(
                {"ok": False, "error": f"Platform '{platform}' not connected"},
                status=404,
            )

        try:
            body = await request.json()
        except json.JSONDecodeError:
            return web.json_response({"ok": False, "error": "Invalid JSON"}, status=400)

        target = body.get("target", "")
        message_id = body.get("message_id", "")
        if not target or not message_id:
            return web.json_response(
                {"ok": False, "error": "Missing 'target' or 'message_id'"}, status=400
            )

        try:
            result = await channel.delete_message(target, message_id)
            return web.json_response(result)
        except NotImplementedError as e:
            return web.json_response({"ok": False, "error": str(e)}, status=501)

    async def _handle_subscribe(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return web.json_response({"ok": False, "error": "Invalid JSON"}, status=400)

        agent_id = body.get("agent_id", "")
        surface_id = body.get("surface_id", "")
        if not agent_id or not surface_id:
            return web.json_response(
                {"ok": False, "error": "Missing 'agent_id' or 'surface_id'"}, status=400
            )

        state_dir = self.config.agent_home / "state"
        SubscriptionManager.subscribe(state_dir, agent_id, surface_id)
        self.subscription_manager.refresh(force=True)
        return web.json_response({"ok": True, "agent_id": agent_id, "surface_id": surface_id})

    async def _handle_unsubscribe(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return web.json_response({"ok": False, "error": "Invalid JSON"}, status=400)

        agent_id = body.get("agent_id", "")
        surface_id = body.get("surface_id", "")
        if not agent_id or not surface_id:
            return web.json_response(
                {"ok": False, "error": "Missing 'agent_id' or 'surface_id'"}, status=400
            )

        state_dir = self.config.agent_home / "state"
        SubscriptionManager.unsubscribe(state_dir, agent_id, surface_id)
        self.subscription_manager.refresh(force=True)
        return web.json_response({"ok": True, "agent_id": agent_id, "surface_id": surface_id})

    async def _handle_permission_request(self, request: web.Request) -> web.Response:
        """Handle a permission approval request. Long-polls until resolved."""
        if not self._permissions_available():
            return web.json_response(
                {"ok": False, "error": "Remote permissions not configured or platform offline"},
                status=503,
            )

        try:
            body = await request.json()
        except json.JSONDecodeError:
            return web.json_response({"ok": False, "error": "Invalid JSON"}, status=400)

        agent_id = body.get("agent_id", "")
        timeout = body.get("timeout", 300)

        # New fields: title/preview/detail/severity. Backward compat: command/reason.
        title = body.get("title") or body.get("reason", "")
        preview = body.get("preview") or body.get("command", "")
        detail = body.get("detail")  # optional, may be None
        severity = body.get("severity", "info")  # "warn" or "info"

        if not agent_id or not preview:
            return web.json_response(
                {"ok": False, "error": "Missing 'agent_id' or 'preview' (or 'command')"}, status=400
            )

        platform = self.config.permissions.platform
        channel = self.channels[platform]

        log.info("Permission request from %s: %s", agent_id, title)
        result = await channel.request_permission(
            agent_id, title=title, preview=preview,
            detail=detail, severity=severity, timeout=timeout,
        )
        log.info("Permission result for %s: %s", agent_id, result)
        return web.json_response({"ok": True, **result})

    async def _handle_permission_resolve(self, request: web.Request) -> web.Response:
        """Externally resolve a pending permission request (e.g. terminal won the race)."""
        if not self._permissions_available():
            return web.json_response(
                {"ok": False, "error": "Remote permissions not configured"}, status=503
            )

        try:
            body = await request.json()
        except json.JSONDecodeError:
            return web.json_response({"ok": False, "error": "Invalid JSON"}, status=400)

        agent_id = body.get("agent_id", "")
        status = body.get("status", "")
        if not agent_id or status not in ("approved", "rejected", "timed_out"):
            return web.json_response(
                {"ok": False, "error": "Missing 'agent_id' or invalid 'status'"}, status=400
            )

        platform = self.config.permissions.platform
        channel = self.channels[platform]
        result = await channel.resolve_permission(agent_id, status)
        return web.json_response(result)

    async def _handle_security_challenge(self, request: web.Request) -> web.Response:
        """Run a full security challenge flow on a platform."""
        platform = request.match_info["platform"]
        channel = self._get_channel(platform)
        if not channel:
            return web.json_response(
                {"ok": False, "error": f"Platform '{platform}' not connected"},
                status=404,
            )

        if "security_challenge" not in channel.capabilities():
            return web.json_response(
                {"ok": False, "error": f"Platform '{platform}' does not support security challenges"},
                status=501,
            )

        try:
            body = await request.json()
        except json.JSONDecodeError:
            return web.json_response({"ok": False, "error": "Invalid JSON"}, status=400)

        reason = body.get("reason", "")
        timeout = body.get("timeout", 60)
        max_attempts = body.get("max_attempts", 2)
        passwords = body.get("passwords", [])

        if not passwords:
            return web.json_response(
                {"ok": False, "error": "No passwords provided"}, status=400,
            )

        log.info("Security challenge on %s: reason=%s max_attempts=%d",
                 platform, reason, max_attempts)
        result = await channel.security_challenge(
            reason, timeout=timeout, max_attempts=max_attempts,
            passwords=passwords,
        )
        log.info("Security challenge result on %s: %s", platform, result.get("result"))
        return web.json_response({"ok": True, **result})


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _find_config(args: argparse.Namespace) -> Path:
    """Resolve config file path."""
    if args.config:
        return Path(args.config)
    if args.agent_home:
        return Path(args.agent_home).expanduser() / "services" / "gateway" / "config.json"
    # Default: look in cwd
    cwd = Path("config.json")
    if cwd.exists():
        return cwd
    raise FileNotFoundError("No gateway config found. Pass --config or --agent-home.")


async def _run(config: GatewayConfig) -> None:
    daemon = GatewayDaemon(config)

    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def _signal_handler():
        log.info("Received shutdown signal")
        stop_event.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _signal_handler)

    await daemon.start()

    try:
        await stop_event.wait()
    finally:
        await daemon.stop()


def main() -> None:
    parser = argparse.ArgumentParser(description="Kiln Gateway Daemon")
    parser.add_argument("--config", help="Path to gateway config.json")
    parser.add_argument("--agent-home", help="Agent home directory (looks for services/gateway/config.json)")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    config_path = _find_config(args)
    config = load_config(config_path)
    log.info("Loaded config from %s", config_path)

    asyncio.run(_run(config))


if __name__ == "__main__":
    main()
