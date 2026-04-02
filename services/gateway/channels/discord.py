"""Discord channel plugin for the Kiln gateway."""

import asyncio
import json
import logging
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import discord

from ..config import DiscordConfig, GatewayConfig
from ..messages import split_message, write_to_inbox
from .base import Channel

log = logging.getLogger("gateway.discord")

STATUS_UPDATE_INTERVAL = 30  # seconds
CHANNEL_POLL_INTERVAL = 1.0  # seconds

# Embed colors
COLOR_ACTIVE = 0x2ecc71   # green
COLOR_IDLE = 0x95a5a6     # gray


# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------

def _load_json_state(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _save_json_state(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n")


def _load_int_state(path: Path) -> int | None:
    if not path.exists():
        return None
    try:
        return int(path.read_text().strip())
    except (ValueError, OSError):
        return None


def _save_int_state(path: Path, value: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(value))


# ---------------------------------------------------------------------------
# Status helpers
# ---------------------------------------------------------------------------

def _read_registry(agent_home: Path) -> dict:
    """Read session registry, filtered to actually running sessions."""
    path = agent_home / "logs" / "session-registry.json"
    raw = _load_json_state(path)
    if not raw:
        return {}

    # Filter to sessions with active tmux sessions
    import subprocess
    try:
        result = subprocess.run(
            ["tmux", "list-sessions", "-F", "#{session_name}"],
            capture_output=True, text=True, timeout=5,
        )
        active_tmux = set(result.stdout.strip().split("\n")) if result.returncode == 0 else set()
    except (subprocess.SubprocessError, FileNotFoundError):
        active_tmux = set()

    return {k: v for k, v in raw.items() if k in active_tmux}


def _read_plan(agent_home: Path, agent_id: str) -> dict | None:
    path = agent_home / "plans" / f"{agent_id}.yml"
    if not path.exists():
        return None
    try:
        import yaml
        return yaml.safe_load(path.read_text())
    except Exception:
        return None


def _count_inbox(agent_home: Path, agent_id: str) -> int:
    inbox = agent_home / "inbox" / agent_id
    if not inbox.exists():
        return 0
    count = 0
    for f in inbox.iterdir():
        if f.suffix == ".md" and not f.with_suffix(".read").exists():
            count += 1
    return count


def _format_uptime(started_at: str) -> str:
    try:
        start = datetime.fromisoformat(started_at)
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        delta = datetime.now(timezone.utc) - start
        hours, remainder = divmod(int(delta.total_seconds()), 3600)
        minutes = remainder // 60
        if hours > 0:
            return f"{hours}h{minutes}m"
        return f"{minutes}m"
    except (ValueError, TypeError):
        return "?"


def _build_presence_text(agents: list[dict]) -> str:
    if not agents:
        return "No agents running"
    count = len(agents)
    return f"{count} agent{'s' if count != 1 else ''}"


def _build_status_embeds(agents: list[dict]) -> list[discord.Embed]:
    if not agents:
        embed = discord.Embed(
            title="No agents running",
            color=COLOR_IDLE,
            timestamp=datetime.now(timezone.utc),
        )
        return [embed]

    embeds = []
    for agent in agents:
        embed = discord.Embed(
            title=f"\U0001f7e2 {agent['id']}",
            color=COLOR_ACTIVE,
        )

        meta = [f"**Uptime:** {agent['uptime']}"]
        if agent["inbox"] > 0:
            meta.append(f"**Inbox:** {agent['inbox']}")
        embed.description = " \u00b7 ".join(meta)

        plan = agent.get("plan")
        if plan:
            tasks = plan.get("tasks", [])
            done = sum(1 for t in tasks if t.get("status") == "done")
            total = len(tasks)
            goal = plan.get("goal", "?")
            if len(goal) > 60:
                goal = goal[:59] + "\u2026"
            embed.add_field(
                name="Plan",
                value=f"{goal} ({done}/{total})",
                inline=False,
            )

        embeds.append(embed)

    return embeds


# ---------------------------------------------------------------------------
# DiscordChannel — public API for the daemon
# ---------------------------------------------------------------------------

class DiscordChannel(Channel):
    """Discord integration via discord.py."""

    def __init__(self, config: GatewayConfig):
        self._config = config
        self._discord_config: DiscordConfig = config.discord
        self._client: _GatewayClient | None = None
        self._ready = asyncio.Event()

    async def connect(self) -> None:
        token = self._config.load_credential("DISCORD_BOT_TOKEN")
        if not token:
            raise RuntimeError(
                f"DISCORD_BOT_TOKEN not found in {self._config.credentials_dir}"
            )

        self._client = _GatewayClient(
            config=self._config,
            ready_event=self._ready,
        )
        connect_task = asyncio.create_task(self._client.start(token))
        try:
            await asyncio.wait_for(self._ready.wait(), timeout=30)
        except asyncio.TimeoutError:
            if connect_task.done() and connect_task.exception():
                raise RuntimeError(
                    f"Discord connection failed: {connect_task.exception()}"
                ) from connect_task.exception()
            raise RuntimeError("Discord connection timed out (30s) — check bot token and network")
        log.info("Discord connected as %s", self._client.user)

    async def disconnect(self) -> None:
        if self._client:
            await self._client.close()
            self._client = None

    async def send_message(self, target: str, content: str, **kwargs: Any) -> dict:
        channel = await self._resolve_target(target)
        if not channel:
            return {"ok": False, "error": f"Could not resolve target: {target}"}

        thread_name = kwargs.get("thread")
        if thread_name and hasattr(channel, "threads"):
            thread = await self._find_or_create_thread(channel, thread_name)
            if thread:
                channel = thread

        chunks = split_message(content)
        sent_ids = []
        for chunk in chunks:
            msg = await channel.send(chunk)
            sent_ids.append(str(msg.id))

        return {"ok": True, "message_ids": sent_ids, "chunks": len(chunks)}

    async def read_history(self, target: str, limit: int = 20) -> list[dict]:
        channel = await self._resolve_target(target)
        if not channel:
            return []

        messages = []
        async for msg in channel.history(limit=limit):
            messages.append({
                "author": msg.author.name,
                "author_id": str(msg.author.id),
                "content": msg.content,
                "timestamp": msg.created_at.isoformat(),
                "id": str(msg.id),
            })
        messages.reverse()
        return messages

    async def create_thread(self, channel_name: str, name: str) -> dict:
        channel = await self._resolve_target(channel_name)
        if not channel or not hasattr(channel, "create_thread"):
            return {"ok": False, "error": f"Cannot create thread in {channel_name}"}

        thread = await channel.create_thread(name=name)
        return {"ok": True, "thread_id": str(thread.id), "name": name}

    async def archive_thread(self, channel_name: str, name: str) -> None:
        channel = await self._resolve_target(channel_name)
        if not channel or not hasattr(channel, "threads"):
            return

        for thread in channel.threads:
            if thread.name == name:
                await thread.edit(archived=True)
                return

    async def list_channels(self) -> list[dict]:
        if not self._client:
            return []

        guild = self._client.get_guild(int(self._discord_config.guild_id))
        if not guild:
            return []

        return [
            {
                "name": ch.name,
                "id": str(ch.id),
                "category": ch.category.name if ch.category else None,
            }
            for ch in guild.text_channels
        ]

    async def send_voice(self, target: str, text: str, **kwargs: Any) -> dict:
        channel = await self._resolve_target(target)
        if not channel:
            return {"ok": False, "error": f"Could not resolve target: {target}"}

        try:
            from ...voice import generate_speech, send_voice_message
        except ImportError:
            return {"ok": False, "error": "Voice service not available"}

        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as f:
            audio_path = Path(f.name)

        try:
            result = await generate_speech(
                text, audio_path,
                agent_home=self._config.agent_home,
                voice=kwargs.get("voice", "fable"),
            )
            if not result:
                return {"ok": False, "error": "TTS generation failed"}

            ok = await send_voice_message(
                str(channel.id), audio_path,
                agent_home=self._config.agent_home,
            )
            return {"ok": ok}
        finally:
            audio_path.unlink(missing_ok=True)

    async def post_to_branch(self, agent_id: str, content: str) -> dict:
        """Post a message to an agent's branch thread."""
        if not self._client:
            return {"ok": False, "error": "Not connected"}

        thread_id = self._client._branch_threads.get(agent_id)
        if not thread_id:
            return {"ok": False, "error": f"No branch thread for {agent_id}"}

        try:
            thread = self._client.get_channel(thread_id)
            if not thread:
                thread = await self._client.fetch_channel(thread_id)

            chunks = split_message(content)
            for chunk in chunks:
                await thread.send(chunk)
            return {"ok": True}
        except (discord.NotFound, discord.Forbidden, discord.HTTPException) as e:
            return {"ok": False, "error": str(e)}

    # --- Internal helpers ---

    async def _resolve_target(self, target: str) -> discord.abc.Messageable | None:
        if not self._client:
            return None

        clean = target.lstrip("#")
        if clean in self._discord_config.channels:
            channel_id = int(self._discord_config.channels[clean])
            return self._client.get_channel(channel_id)

        if target.isdigit():
            return self._client.get_channel(int(target))

        if target.startswith("@"):
            user_id = target[1:]
            if user_id.isdigit():
                user = await self._client.fetch_user(int(user_id))
                if user:
                    return await user.create_dm()

        guild = self._client.get_guild(int(self._discord_config.guild_id))
        if guild:
            for ch in guild.text_channels:
                if ch.name == clean:
                    return ch

        return None

    async def _find_or_create_thread(
        self, channel: discord.TextChannel, name: str
    ) -> discord.Thread | None:
        for thread in channel.threads:
            if thread.name == name:
                return thread
        async for thread in channel.archived_threads():
            if thread.name == name:
                await thread.edit(archived=False)
                return thread
        return await channel.create_thread(name=name)


# ---------------------------------------------------------------------------
# _GatewayClient — Discord event handler + background tasks
# ---------------------------------------------------------------------------

class _GatewayClient(discord.Client):
    """Discord client for the gateway.

    Handles inbound messages, branch threads, channel mirroring, and status.
    """

    def __init__(self, config: GatewayConfig, ready_event: asyncio.Event, **kwargs):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(intents=intents, **kwargs)

        self._config = config
        self._discord_config = config.discord
        self._ready_event = ready_event

        state_dir = config.agent_home / "state"
        session_prefix = config.default_agent + "-" if config.default_agent else ""
        self._session_prefix = session_prefix

        # Branch threads: agent_id -> thread_id
        self._branch_threads_path = state_dir / "discord-branch-threads.json"
        self._branch_threads: dict[str, int] = {
            k: int(v) for k, v in _load_json_state(self._branch_threads_path).items()
        }
        self._thread_to_agent: dict[int, str] = {
            v: k for k, v in self._branch_threads.items()
        }

        # Channel threads: channel_name -> thread_id
        self._channel_threads_path = state_dir / "discord-channel-threads.json"
        self._channel_threads: dict[str, int] = {
            k: int(v) for k, v in _load_json_state(self._channel_threads_path).items()
        }
        self._channel_thread_to_name: dict[int, str] = {
            v: k for k, v in self._channel_threads.items()
        }
        self._channel_file_positions: dict[str, int] = {}

        # Status message ID
        self._status_msg_path = state_dir / "discord-status-msg-id"
        self._status_message_id: int | None = _load_int_state(self._status_msg_path)

    async def setup_hook(self) -> None:
        """Start background tasks after connection."""
        self.loop.create_task(self._sync_branch_threads_loop())
        self.loop.create_task(self._sync_channel_threads_loop())
        self.loop.create_task(self._update_presence_loop())
        self.loop.create_task(self._update_status_loop())

    async def on_ready(self) -> None:
        log.info("Discord client ready as %s (id: %s)", self.user, self.user.id)
        self._ready_event.set()

    async def on_message(self, message: discord.Message) -> None:
        if message.author == self.user:
            return

        sender_id = str(message.author.id)

        # Access control
        is_dm = isinstance(message.channel, discord.DMChannel)
        access = self._discord_config.dm_access if is_dm else self._discord_config.access
        if not access.is_allowed(sender_id):
            log.debug("Blocked message from %s (%s) — access denied",
                      message.author, sender_id)
            return

        sender_entry = access.allowlist.get(sender_id, {})
        sender_name = sender_entry.get("name", message.author.name)
        trust = sender_entry.get("trust", "unknown")

        content = message.content
        if not content.strip() and not message.attachments:
            return

        # --- Route based on thread type ---

        if isinstance(message.channel, discord.Thread):
            thread_id = message.channel.id

            # Channel thread? Inject into the Kiln channel
            if thread_id in self._channel_thread_to_name:
                channel_name = self._channel_thread_to_name[thread_id]
                self._inject_channel_message(channel_name, sender_name, content)
                log.info("Discord -> channel '%s' from %s", channel_name, sender_name)
                return

            # Branch thread? Route to that agent's inbox
            if thread_id in self._thread_to_agent:
                agent_id = self._thread_to_agent[thread_id]
                log.info("Discord -> branch inbox %s from %s", agent_id, sender_name)
                write_to_inbox(
                    self._config.agent_home, agent_id,
                    sender_name=sender_name, sender_id=sender_id,
                    content=content, platform="discord",
                    channel_desc=f"#branches/{message.channel.name}",
                    channel_id=str(thread_id), trust=trust,
                )
                return

        # --- Default routing ---

        if is_dm:
            channel_desc = "DM"
        elif hasattr(message.channel, "name"):
            channel_desc = f"#{message.channel.name}"
        else:
            channel_desc = "unknown"

        # Download attachments
        attachment_paths: list[str] = []
        if message.attachments:
            attachment_paths = await self._download_attachments(message.attachments, sender_name)

        # Voice transcription
        if message.flags.voice and attachment_paths:
            content = await self._handle_voice_message(
                attachment_paths[0], content, sender_name
            )

        # Default target: agent name catch-all inbox
        agent_id = self._config.default_agent or self._find_active_agent() or "gateway-pending"

        write_to_inbox(
            self._config.agent_home, agent_id,
            sender_name=sender_name, sender_id=sender_id,
            content=content, platform="discord",
            channel_desc=channel_desc, channel_id=str(message.channel.id),
            trust=trust, attachment_paths=attachment_paths or None,
        )

    # -----------------------------------------------------------------------
    # Branch threads
    # -----------------------------------------------------------------------

    async def _sync_branch_threads_loop(self) -> None:
        await self.wait_until_ready()

        branches_ch = self._resolve_config_channel("branches")
        if not branches_ch:
            log.warning("No #branches channel configured — branch threads disabled")
            return

        log.info("Branch thread manager started")

        # Reconcile on startup
        try:
            await self._reconcile_branch_threads(branches_ch)
        except Exception:
            log.exception("Branch thread reconciliation error")

        while not self.is_closed():
            try:
                await self._sync_branch_threads(branches_ch)
            except Exception:
                log.exception("Branch thread sync error")
            await asyncio.sleep(STATUS_UPDATE_INTERVAL)

    async def _reconcile_branch_threads(self, branches_ch: discord.TextChannel) -> None:
        """Rebuild thread mapping from Discord state on startup."""
        registry = _read_registry(self._config.agent_home)
        active_agents = set(registry.keys())

        try:
            all_threads = await branches_ch.guild.active_threads()
            threads = [t for t in all_threads if t.parent_id == branches_ch.id]
        except discord.HTTPException as e:
            log.error("Could not fetch active threads: %s", e)
            return

        for thread in threads:
            agent_id = f"{self._session_prefix}{thread.name}"
            if agent_id in active_agents:
                self._branch_threads[agent_id] = thread.id
                self._thread_to_agent[thread.id] = agent_id
                log.info("Reconciled thread '%s' -> %s", thread.name, agent_id)
            else:
                try:
                    await thread.send(f"*{agent_id} has ended.*")
                    await thread.edit(archived=True, locked=True)
                    log.info("Archived orphan thread '%s'", thread.name)
                except (discord.NotFound, discord.Forbidden, discord.HTTPException) as e:
                    log.warning("Could not archive thread '%s': %s", thread.name, e)

        _save_json_state(self._branch_threads_path, self._branch_threads)

    async def _sync_branch_threads(self, branches_ch: discord.TextChannel) -> None:
        """Create threads for new sessions, archive threads for ended ones."""
        registry = _read_registry(self._config.agent_home)
        active_agents = set(registry.keys())
        changed = False

        # Create threads for new sessions
        for agent_id in active_agents:
            if agent_id in self._branch_threads:
                continue
            short_name = agent_id.removeprefix(self._session_prefix)
            try:
                thread = await branches_ch.create_thread(
                    name=short_name,
                    type=discord.ChannelType.public_thread,
                )
                self._branch_threads[agent_id] = thread.id
                self._thread_to_agent[thread.id] = agent_id
                changed = True
                log.info("Created branch thread '%s' for %s", short_name, agent_id)

                # Post intro
                plan = _read_plan(self._config.agent_home, agent_id)
                intro = f"**{agent_id}** is online."
                if plan and plan.get("goal"):
                    intro += f"\n> {plan['goal']}"
                await thread.send(intro)
            except discord.HTTPException as e:
                log.error("Failed to create thread for %s: %s", agent_id, e)

        # Archive threads for ended sessions
        dead = set(self._branch_threads.keys()) - active_agents
        for agent_id in dead:
            thread_id = self._branch_threads.pop(agent_id)
            self._thread_to_agent.pop(thread_id, None)
            changed = True
            try:
                thread = self.get_channel(thread_id)
                if not thread:
                    thread = await self.fetch_channel(thread_id)
                await thread.send(f"*{agent_id} has ended.*")
                await thread.edit(archived=True, locked=True)
                log.info("Archived branch thread for %s", agent_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException) as e:
                log.warning("Could not archive thread for %s: %s", agent_id, e)

        if changed:
            _save_json_state(self._branch_threads_path, self._branch_threads)

    # -----------------------------------------------------------------------
    # Channel mirroring
    # -----------------------------------------------------------------------

    async def _sync_channel_threads_loop(self) -> None:
        await self.wait_until_ready()

        channels_ch = self._resolve_config_channel("channels")
        if not channels_ch:
            log.warning("No #channels channel configured — channel mirroring disabled")
            return

        log.info("Channel thread manager started")

        # Reconcile
        try:
            await self._reconcile_channel_threads(channels_ch)
        except Exception:
            log.exception("Channel thread reconciliation error")

        iteration = 0
        while not self.is_closed():
            try:
                # Lifecycle sync every 30 iterations (~30s at 1s poll)
                if iteration % 30 == 0:
                    await self._sync_channel_lifecycle(channels_ch)
                await self._forward_channel_messages()
            except Exception:
                log.exception("Channel thread sync error")

            iteration += 1
            await asyncio.sleep(CHANNEL_POLL_INTERVAL)

    async def _reconcile_channel_threads(self, channels_ch: discord.TextChannel) -> None:
        active = self._get_active_channels()

        try:
            all_threads = await channels_ch.guild.active_threads()
            threads = [t for t in all_threads if t.parent_id == channels_ch.id]
        except discord.HTTPException as e:
            log.error("Could not fetch channel threads: %s", e)
            return

        for thread in threads:
            name = thread.name
            if name in active:
                self._channel_threads[name] = thread.id
                self._channel_thread_to_name[thread.id] = name
                # Skip to end of history to avoid replaying
                history = self._config.agent_home / "channels" / name / "history.jsonl"
                if history.exists():
                    self._channel_file_positions[name] = history.stat().st_size
                log.info("Reconciled channel thread '%s'", name)
            else:
                try:
                    await thread.send(f"*Channel `{name}` is inactive.*")
                    await thread.edit(archived=True, locked=True)
                    log.info("Archived channel thread '%s'", name)
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    pass

        _save_json_state(self._channel_threads_path, self._channel_threads)

    async def _sync_channel_lifecycle(self, channels_ch: discord.TextChannel) -> None:
        active = self._get_active_channels()
        changed = False

        for name in active:
            if name in self._channel_threads:
                continue
            try:
                thread = await channels_ch.create_thread(
                    name=name,
                    type=discord.ChannelType.public_thread,
                )
                self._channel_threads[name] = thread.id
                self._channel_thread_to_name[thread.id] = name
                changed = True
                log.info("Created channel thread '%s'", name)

                # Post catch-up history
                history = self._config.agent_home / "channels" / name / "history.jsonl"
                if history.exists():
                    lines = history.read_text().splitlines()
                    parts = []
                    for line in lines:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            msg = json.loads(line)
                            formatted = self._format_channel_msg(msg)
                            if formatted:
                                parts.append(formatted)
                        except json.JSONDecodeError:
                            continue
                    if parts:
                        catchup = "**[history]**\n" + "\n".join(parts)
                        for chunk in split_message(catchup):
                            await thread.send(chunk)
                    self._channel_file_positions[name] = history.stat().st_size
            except discord.HTTPException as e:
                log.error("Failed to create channel thread '%s': %s", name, e)

        # Archive dead channels
        dead = set(self._channel_threads.keys()) - set(active.keys())
        for name in dead:
            thread_id = self._channel_threads.pop(name)
            self._channel_thread_to_name.pop(thread_id, None)
            self._channel_file_positions.pop(name, None)
            changed = True
            try:
                thread = self.get_channel(thread_id)
                if not thread:
                    thread = await self.fetch_channel(thread_id)
                await thread.send(f"*Channel `{name}` is inactive.*")
                await thread.edit(archived=True, locked=True)
                log.info("Archived channel thread '%s'", name)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass

        if changed:
            _save_json_state(self._channel_threads_path, self._channel_threads)

    async def _forward_channel_messages(self) -> None:
        """Forward new Kiln channel messages to Discord threads."""
        for name, thread_id in list(self._channel_threads.items()):
            messages = self._read_new_channel_messages(name)
            if not messages:
                continue

            try:
                thread = self.get_channel(thread_id)
                if not thread:
                    thread = await self.fetch_channel(thread_id)
                if hasattr(thread, "archived") and thread.archived:
                    await thread.edit(archived=False)
            except (discord.NotFound, discord.Forbidden):
                continue

            for msg in messages:
                if msg.get("source") == "discord":
                    continue  # prevent echo
                formatted = self._format_channel_msg(msg)
                if formatted:
                    for chunk in split_message(formatted):
                        try:
                            await thread.send(chunk)
                        except discord.HTTPException as e:
                            log.error("Failed to forward channel message: %s", e)

    def _get_active_channels(self) -> dict[str, Path]:
        """Get channels with recent activity (history.jsonl modified in last hour)."""
        channels_dir = self._config.agent_home / "channels"
        if not channels_dir.exists():
            return {}
        import time
        cutoff = time.time() - 3600
        result = {}
        for d in channels_dir.iterdir():
            if not d.is_dir():
                continue
            history = d / "history.jsonl"
            if history.exists() and history.stat().st_mtime > cutoff:
                result[d.name] = history
        return result

    def _read_new_channel_messages(self, name: str) -> list[dict]:
        history = self._config.agent_home / "channels" / name / "history.jsonl"
        if not history.exists():
            return []

        pos = self._channel_file_positions.get(name, 0)
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
            self._channel_file_positions[name] = f.tell()

        return messages

    def _format_channel_msg(self, msg: dict) -> str | None:
        sender = msg.get("from", "unknown")
        body = msg.get("body", "")
        summary = msg.get("summary", "")
        text = body or summary
        if not text:
            return None
        return f"**{sender}:** {text}"

    def _inject_channel_message(self, channel_name: str, sender_name: str, content: str) -> None:
        """Inject a Discord message into a Kiln channel.

        Two steps:
        1. Deliver to all subscriber inboxes (via Kiln's send_to_inbox)
        2. Append to channel history with source=discord to prevent echo

        We can't use do_send_message directly because it doesn't support
        the source tag needed for echo prevention in channel mirroring.
        """
        from kiln.tools import send_to_inbox, _resolve_recipient_inbox

        inbox_root = self._config.agent_home / "inbox"
        channels_path = self._config.agent_home / "channels.json"
        sender_id = f"discord-{sender_name}"
        summary = content[:200]

        # 1. Deliver to subscriber inboxes
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
            log.info("Delivered channel '%s' message to %d subscriber(s)", channel_name, delivered)

        # 2. Append to channel history with source tag for echo prevention
        history_dir = self._config.agent_home / "channels" / channel_name
        history_dir.mkdir(parents=True, exist_ok=True)

        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "from": sender_id,
            "summary": summary,
            "body": content,
            "priority": "normal",
            "source": "discord",
        }
        with open(history_dir / "history.jsonl", "a") as f:
            f.write(json.dumps(entry) + "\n")

    # -----------------------------------------------------------------------
    # Status display
    # -----------------------------------------------------------------------

    async def _update_presence_loop(self) -> None:
        await self.wait_until_ready()
        log.info("Presence updater started")

        while not self.is_closed():
            try:
                agents = self._collect_agent_data()
                text = _build_presence_text(agents)
                activity = discord.Activity(
                    type=discord.ActivityType.watching,
                    name=text,
                )
                await self.change_presence(activity=activity)
            except Exception:
                log.exception("Presence update error")
            await asyncio.sleep(STATUS_UPDATE_INTERVAL)

    async def _update_status_loop(self) -> None:
        await self.wait_until_ready()

        status_ch = self._resolve_config_channel("status")
        if not status_ch:
            log.warning("No #status channel configured — status display disabled")
            return

        log.info("Status updater started")

        while not self.is_closed():
            try:
                agents = self._collect_agent_data()
                embeds = _build_status_embeds(agents)
                now = datetime.now(ZoneInfo("America/Toronto")).strftime("%I:%M:%S %p EST")
                content = f"**Agent Status** \u2014 last updated {now}"

                # Discord limits to 10 embeds per message
                await self._update_or_create_status_message(status_ch, content, embeds[:10])
            except Exception:
                log.exception("Status update error")
            await asyncio.sleep(STATUS_UPDATE_INTERVAL)

    async def _update_or_create_status_message(
        self, channel: discord.abc.Messageable, content: str, embeds: list[discord.Embed],
    ) -> None:
        if self._status_message_id:
            try:
                msg = await channel.fetch_message(self._status_message_id)
                await msg.edit(content=content, embeds=embeds)
                return
            except discord.NotFound:
                self._status_message_id = None
            except discord.Forbidden:
                log.error("Cannot edit status message — check permissions")
                return

        msg = await channel.send(content=content, embeds=embeds)
        self._status_message_id = msg.id
        _save_int_state(self._status_msg_path, msg.id)

        try:
            await msg.pin()
        except (discord.Forbidden, discord.HTTPException):
            pass

    def _collect_agent_data(self) -> list[dict]:
        registry = _read_registry(self._config.agent_home)
        agents = []
        for agent_id, entry in registry.items():
            agents.append({
                "id": agent_id,
                "uptime": _format_uptime(entry.get("started_at", "")),
                "inbox": _count_inbox(self._config.agent_home, agent_id),
                "plan": _read_plan(self._config.agent_home, agent_id),
            })
        agents.sort(key=lambda a: a["id"])
        return agents

    # -----------------------------------------------------------------------
    # Helpers
    # -----------------------------------------------------------------------

    def _resolve_config_channel(self, name: str) -> discord.TextChannel | None:
        """Resolve a channel name from config to a Discord channel object."""
        channel_id = self._discord_config.channels.get(name)
        if not channel_id:
            return None
        return self.get_channel(int(channel_id))

    def _find_active_agent(self) -> str | None:
        registry = _read_registry(self._config.agent_home)
        if not registry:
            return None
        return max(registry, key=lambda k: registry[k].get("started_at", ""))

    async def _download_attachments(
        self, attachments: list[discord.Attachment], sender_name: str
    ) -> list[str]:
        download_dir = self._config.agent_home / "scratch" / "discord-attachments"
        download_dir.mkdir(parents=True, exist_ok=True)

        paths = []
        for att in attachments:
            dest = download_dir / f"{sender_name}-{att.filename}"
            try:
                await att.save(dest)
                paths.append(str(dest))
                log.info("Downloaded attachment: %s (%d bytes)", dest, att.size)
            except Exception:
                log.exception("Failed to download attachment %s", att.filename)
        return paths

    async def _handle_voice_message(
        self, audio_path: str, content: str, sender_name: str
    ) -> str:
        try:
            from ...voice.openai import transcribe
        except ImportError:
            return (
                f"[Voice message received — transcription unavailable. "
                f"Audio saved at: {audio_path}]"
                + (f"\n\n{content}" if content.strip() else "")
            )

        log.info("Transcribing voice message from %s: %s", sender_name, audio_path)
        transcript = await transcribe(
            audio_path, agent_home=self._config.agent_home
        )
        if transcript:
            voice_prefix = f"[Voice message transcript]\n{transcript}"
            if content.strip():
                return f"{voice_prefix}\n\n{content}"
            return voice_prefix
        else:
            return (
                f"[Voice message received — transcription failed. "
                f"Audio saved at: {audio_path}]"
                + (f"\n\n{content}" if content.strip() else "")
            )
