"""Gateway daemon — HTTP server bridging external platforms to Kiln agents.

Usage:
    python -m kiln.services.gateway.daemon --config <path>
    python -m kiln.services.gateway.daemon --agent-home ~/.beth
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

from .config import GatewayConfig, load_config
from .channels.base import Channel

log = logging.getLogger("gateway")


class GatewayDaemon:
    """Main gateway daemon. Manages channel plugins and serves the HTTP API."""

    def __init__(self, config: GatewayConfig):
        self.config = config
        self.channels: dict[str, Channel] = {}
        self._app: web.Application | None = None
        self._runner: web.AppRunner | None = None

    async def start(self) -> None:
        """Start channel plugins and HTTP server."""
        log.info("Starting gateway daemon (bind=%s, port=%d)", self.config.bind, self.config.port)

        # Initialize channel plugins
        if self.config.discord and self.config.discord.enabled:
            from .channels.discord import DiscordChannel
            discord_channel = DiscordChannel(self.config)
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
        return app

    def _get_channel(self, platform: str) -> Channel | None:
        return self.channels.get(platform)

    async def _handle_status(self, request: web.Request) -> web.Response:
        status = {
            "running": True,
            "pid": os.getpid(),
            "channels": {},
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
