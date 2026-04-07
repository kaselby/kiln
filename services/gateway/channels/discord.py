"""Discord channel plugin for the Kiln gateway."""

import asyncio
import json
import logging
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml

import discord

# Optional kiln import — available when kiln/src is on sys.path
try:
    from kiln.state import trust_label as _trust_label
except ImportError:
    _trust_label = None

from ..bridges import BridgeManager, Bridge
from ..config import DiscordConfig, GatewayConfig
from ..messages import split_message, write_to_inbox
from ..subscriptions import SubscriptionManager
from .base import Channel

log = logging.getLogger("gateway.discord")

STATUS_UPDATE_INTERVAL = 30  # seconds
CHANNEL_POLL_INTERVAL = 1.0  # seconds

# Embed colors
COLOR_ACTIVE = 0x2ecc71   # green
COLOR_IDLE = 0x95a5a6     # gray

# Context window size for token usage display
MAX_CONTEXT_TOKENS = 200_000
CLAUDE_PROJECTS = Path.home() / ".claude" / "projects"


# ---------------------------------------------------------------------------
# Permission approval UI
# ---------------------------------------------------------------------------


class _PermissionView(discord.ui.View):
    """Discord view with Approve/Reject/Details buttons for permission approval."""

    def __init__(
        self, future: asyncio.Future, discord_config: DiscordConfig,
        detail: str | None = None,
    ):
        super().__init__(timeout=None)  # timeout handled by the caller
        self._future = future
        self._discord_config = discord_config
        self._detail = detail
        if not detail:
            self.details.disabled = True
            self.details.style = discord.ButtonStyle.secondary

    def _check_trust(self, user_id: str) -> bool:
        """Only users with 'full' max_trust can approve permissions."""
        entry = self._discord_config.users.get(str(user_id), {})
        return (entry.get("max_trust") or entry.get("trust")) == "full"

    @discord.ui.button(label="Approve", style=discord.ButtonStyle.green, emoji="\u2705")
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._check_trust(str(interaction.user.id)):
            await interaction.response.send_message(
                "You don't have permission to approve commands.", ephemeral=True
            )
            return
        if not self._future.done():
            name = interaction.user.display_name
            self._future.set_result((True, name))
        await interaction.response.defer()
        self.stop()

    @discord.ui.button(label="Reject", style=discord.ButtonStyle.red, emoji="\u274c")
    async def reject(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._check_trust(str(interaction.user.id)):
            await interaction.response.send_message(
                "You don't have permission to reject commands.", ephemeral=True
            )
            return
        if not self._future.done():
            name = interaction.user.display_name
            self._future.set_result((False, name))
        await interaction.response.defer()
        self.stop()

    @discord.ui.button(label="Details", style=discord.ButtonStyle.secondary, emoji="\U0001f50d")
    async def details(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._detail:
            await interaction.response.send_message("No details available.", ephemeral=True)
            return
        if len(self._detail) < 1800:
            await interaction.response.send_message(
                f"```\n{self._detail}\n```", ephemeral=True,
            )
        else:
            import io
            buf = io.BytesIO(self._detail.encode("utf-8"))
            buf.seek(0)
            await interaction.response.send_message(
                file=discord.File(buf, filename="details.txt"), ephemeral=True,
            )


class _DismissView(discord.ui.View):
    """View with a single Dismiss button that deletes the message."""

    @discord.ui.button(label="Dismiss", style=discord.ButtonStyle.secondary, emoji="✖️")
    async def dismiss(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.message.delete()
        self.stop()


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

_registry_cache: dict = {}
_registry_cache_time: float = 0
_REGISTRY_CACHE_TTL = 10  # seconds


def _read_registry(agent_home: Path) -> dict:
    """Read session registry, filtered to actually running sessions.

    Results are cached for 10s to avoid repeated tmux subprocess calls
    from multiple background tasks.
    """
    global _registry_cache, _registry_cache_time
    import time
    now = time.monotonic()
    if now - _registry_cache_time < _REGISTRY_CACHE_TTL:
        return _registry_cache

    path = agent_home / "logs" / "session-registry.json"
    raw = _load_json_state(path)
    if not raw:
        _registry_cache = {}
        _registry_cache_time = now
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

    _registry_cache = {k: v for k, v in raw.items() if k in active_tmux}
    _registry_cache_time = now
    return _registry_cache


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


def _read_canonical(agent_home: Path) -> str | None:
    """Read the canonical agent ID from the lock file (<agent_home>/state/canonical)."""
    path = agent_home / "state" / "canonical"
    try:
        return path.read_text().strip() or None
    except OSError:
        return None


def _get_context_usage(registry_entry: dict) -> tuple[int, int] | None:
    """Get (used_tokens, max_tokens) from Claude's conversation JSONL.

    Reads from the end of the file to avoid scanning megabytes of JSON
    on every status update.
    """
    session_uuid = registry_entry.get("session_uuid")
    cwd = registry_entry.get("cwd", "")
    if not session_uuid or not cwd:
        return None

    encoded_cwd = str(Path(cwd).resolve()).replace("/", "-").replace(".", "-")
    jsonl_path = CLAUDE_PROJECTS / encoded_cwd / f"{session_uuid}.jsonl"

    if not jsonl_path.exists():
        return None

    try:
        # Read the last ~32KB — enough to find the most recent assistant message
        tail_size = 32 * 1024
        with open(jsonl_path, "rb") as f:
            f.seek(0, 2)  # end
            size = f.tell()
            f.seek(max(0, size - tail_size))
            tail = f.read().decode("utf-8", errors="replace")

        last_usage = None
        for line in tail.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if obj.get("type") == "assistant":
                usage = obj.get("message", {}).get("usage", {})
                if usage:
                    last_usage = usage

        if not last_usage:
            return None

        total = (
            last_usage.get("input_tokens", 0)
            + last_usage.get("cache_read_input_tokens", 0)
            + last_usage.get("cache_creation_input_tokens", 0)
        )
        return (total, MAX_CONTEXT_TOKENS)
    except OSError:
        return None


def _format_context(usage: tuple[int, int] | None) -> str:
    if usage is None:
        return "?"
    used, total = usage
    pct = int(used / total * 100)
    return f"{pct}%"


def _format_uptime(started_at: str) -> str:
    try:
        start = datetime.fromisoformat(started_at)
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        delta = datetime.now(timezone.utc) - start
        hours, remainder = divmod(int(delta.total_seconds()), 3600)
        minutes = remainder // 60
        if hours > 0:
            return f"{hours}h{minutes:02d}m"
        return f"{minutes}m"
    except (ValueError, TypeError):
        return "?"


def _build_presence_text(agents: list[dict], canonical_id: str | None = None) -> str:
    if not agents:
        return "No agents running"
    count = len(agents)
    # Show canonical agent's context if known; fall back to most recent
    canonical = next((a for a in agents if a["id"] == canonical_id), None) if canonical_id else None
    target = canonical or agents[-1]
    name = target["id"].removeprefix(f"{target['id'].split('-')[0]}-")
    ctx = target.get("context", "?")
    text = f"{count} agent{'s' if count != 1 else ''} | {name} {ctx}"
    return text[:128]


def _build_status_embeds(agents: list[dict], canonical_id: str | None = None) -> list[discord.Embed]:
    if not agents:
        embed = discord.Embed(
            title="No agents running",
            color=COLOR_IDLE,
            timestamp=datetime.now(timezone.utc),
        )
        return [embed]

    embeds = []
    for agent in agents:
        # Staleness detection — no context data suggests idle/stale
        context_pct = agent.get("context_pct")
        color = COLOR_ACTIVE if context_pct is not None else COLOR_IDLE
        is_canonical = agent["id"] == canonical_id

        title = f"\U0001f7e2 {agent['id']}"
        if is_canonical:
            title += " \u2b50"

        embed = discord.Embed(
            title=title,
            color=color,
        )

        meta = [
            f"**Uptime:** {agent['uptime']}",
            f"**Context:** {agent.get('context', '?')}",
        ]
        if is_canonical:
            meta.append("\u2b50 **canonical**")
        if agent["inbox"] > 0:
            meta.append(f"**Inbox:** {agent['inbox']}")
        embed.description = " \u00b7 ".join(meta)

        plan = agent.get("plan")
        if plan:
            tasks = plan.get("tasks", [])
            done = sum(1 for t in tasks if t.get("status") == "done")
            in_progress = sum(1 for t in tasks if t.get("status") == "in_progress")
            total = len(tasks)
            goal = plan.get("goal", "?")
            if len(goal) > 60:
                goal = goal[:59] + "\u2026"
            progress = f"{goal} ({done}/{total}"
            if in_progress:
                progress += f", {in_progress} active"
            progress += ")"
            embed.add_field(name="Plan", value=progress, inline=False)

        embeds.append(embed)

    return embeds


# ---------------------------------------------------------------------------
# DiscordChannel — public API for the daemon
# ---------------------------------------------------------------------------

class DiscordChannel(Channel):
    """Discord integration via discord.py."""

    def __init__(self, config: GatewayConfig,
                 bridge_manager: BridgeManager,
                 subscription_manager: SubscriptionManager):
        self._config = config
        self._discord_config: DiscordConfig = config.discord
        self._bridge_manager = bridge_manager
        self._subscription_manager = subscription_manager
        self._client: _GatewayClient | None = None
        self._ready = asyncio.Event()
        self._pending_permissions: dict[str, dict] = {}  # agent_id -> {future, message, embed}
        self._security_challenge_state: dict | None = None  # pending challenge state
        self._security_message_ids: list[int] = []  # accumulated msg IDs for cleanup

    async def connect(self) -> None:
        token = self._config.load_credential("DISCORD_BOT_TOKEN")
        if not token:
            raise RuntimeError(
                f"DISCORD_BOT_TOKEN not found in {self._config.credentials_dir}"
            )

        self._client = _GatewayClient(
            config=self._config,
            bridge_manager=self._bridge_manager,
            subscription_manager=self._subscription_manager,
            ready_event=self._ready,
            discord_channel=self,
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

    async def delete_message(self, target: str, message_id: str) -> dict:
        channel = await self._resolve_target(target)
        if not channel:
            return {"ok": False, "error": f"Could not resolve target: {target}"}

        try:
            msg = await channel.fetch_message(int(message_id))
            await msg.delete()
            return {"ok": True}
        except discord.NotFound:
            return {"ok": False, "error": f"Message {message_id} not found"}
        except discord.Forbidden:
            return {"ok": False, "error": "Bot lacks permission to delete this message"}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    async def create_thread(self, channel_name: str, name: str) -> dict:
        channel = await self._resolve_target(channel_name)
        if not channel or not hasattr(channel, "create_thread"):
            return {"ok": False, "error": f"Cannot create thread in {channel_name}"}

        thread = await channel.create_thread(
            name=name, type=discord.ChannelType.public_thread,
        )
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
            from voice import generate_speech, send_voice_message
        except ImportError:
            return {"ok": False, "error": "Voice service not available"}

        # Voice and instructions: kwargs override config defaults
        discord_cfg = self._config.discord
        voice = kwargs.get("voice") or (discord_cfg.voice_default if discord_cfg else "") or None
        instructions = kwargs.get("instructions") or (discord_cfg.voice_instructions if discord_cfg else "") or None

        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as f:
            audio_path = Path(f.name)

        try:
            tts_kwargs: dict[str, Any] = {"agent_home": self._config.agent_home}
            if voice:
                tts_kwargs["voice"] = voice
            if instructions:
                tts_kwargs["instructions"] = instructions

            result = await generate_speech(text, audio_path, **tts_kwargs)
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

    async def request_permission(
        self, agent_id: str, *, title: str, preview: str,
        detail: str | None = None, severity: str = "info",
        timeout: float = 300,
    ) -> dict:
        if not self._client:
            return {"error": "not connected"}

        thread_id = self._client._branch_threads.get(agent_id)
        if not thread_id:
            return {"error": f"no branch thread for {agent_id}"}

        try:
            thread = self._client.get_channel(thread_id)
            if not thread:
                thread = await self._client.fetch_channel(thread_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException) as e:
            log.error("Failed to get branch thread for %s: %s", agent_id, e)
            return {"error": f"branch thread inaccessible: {e}"}

        # Severity-based coloring
        color = 0xe74c3c if severity == "warn" else 0x3498db  # red or blue

        # Build the approval prompt — gateway renders blind, Kiln owns text
        embed = discord.Embed(
            title=title or "Permission Required",
            description=preview[:2000],
            color=color,
        )
        embed.set_footer(text=f"Agent: {agent_id}")

        # Create view with buttons; the Future resolves when a button is clicked
        future: asyncio.Future[tuple[bool, str]] = asyncio.get_event_loop().create_future()
        view = _PermissionView(future, self._discord_config, detail=detail)

        # Ping full-trust users if the owner isn't at terminal
        mention_content = self._client._permission_ping_content()

        try:
            msg = await thread.send(content=mention_content, embed=embed, view=view)
        except (discord.Forbidden, discord.HTTPException) as e:
            log.error("Failed to send permission prompt: %s", e)
            return {"error": f"failed to send permission prompt: {e}"}

        # Register as pending so resolve_permission can find it
        pending = {
            "future": future,
            "message": msg,
            "embed": embed,
            "externally_resolved": False,
        }
        self._pending_permissions[agent_id] = pending

        try:
            approved, responder = await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            view.stop()
            if not pending["externally_resolved"]:
                try:
                    embed.color = 0x95a5a6  # gray
                    embed.title = "\u23f0 Permission Timed Out"
                    await msg.edit(embed=embed, view=None)
                except discord.HTTPException:
                    pass
            return {"approved": False, "timed_out": True, "responder": ""}
        finally:
            self._pending_permissions.pop(agent_id, None)

        # Update the message only if not already updated by resolve_permission
        if not pending["externally_resolved"]:
            try:
                if approved:
                    embed.color = 0x2ecc71  # green
                    embed.title = "\u2705 Approved"
                else:
                    embed.color = 0xe74c3c  # red
                    embed.title = "\u274c Rejected"
                embed.set_footer(text=f"Agent: {agent_id} | By: {responder}")
                await msg.edit(embed=embed, view=None)
            except discord.HTTPException:
                pass

        return {"approved": approved, "timed_out": False, "responder": responder}

    async def resolve_permission(self, agent_id: str, status: str) -> dict:
        """Externally resolve a pending permission request.

        Called when the terminal (or timeout) resolves the approval before
        Discord. Updates the Discord message and unblocks the waiting handler.

        Args:
            agent_id: The agent whose pending request to resolve.
            status: One of "approved", "rejected", "timed_out".

        Returns:
            {"ok": bool, "error": str | None}
        """
        pending = self._pending_permissions.get(agent_id)
        if not pending:
            return {"ok": False, "error": f"No pending permission for {agent_id}"}

        pending["externally_resolved"] = True
        future = pending["future"]
        msg = pending["message"]
        embed = pending["embed"]

        # Update the Discord message
        status_display = {
            "approved": ("\u2705 Approved (terminal)", 0x2ecc71),
            "rejected": ("\u274c Rejected (terminal)", 0xe74c3c),
            "timed_out": ("\u23f0 Timed Out", 0x95a5a6),
        }
        title, color = status_display.get(status, (f"\u2139\ufe0f {status}", 0x95a5a6))
        try:
            embed.title = title
            embed.color = color
            await msg.edit(embed=embed, view=None)
        except discord.HTTPException:
            pass

        # Resolve the future to unblock request_permission
        if not future.done():
            approved = status == "approved"
            future.set_result((approved, "terminal"))

        return {"ok": True}

    # --- Security challenge ---

    async def security_challenge(
        self, reason: str, *,
        timeout: float = 60,
        attempt: int = 1,
        max_attempts: int = 2,
        previous_result: str | None = None,
    ) -> dict:
        if not self._client:
            return {"response": None, "author_id": "", "timed_out": False,
                    "error": "not connected"}

        # Resolve security channel — config key, then fallback to name
        security_ch = await self._resolve_target("#security")
        if not security_ch:
            return {"response": None, "author_id": "", "timed_out": False,
                    "error": "no #security channel found"}

        # Build the challenge embed
        if attempt == 1:
            color = 0xf39c12  # orange
            title = "\U0001f512 Security Verification Required"
            desc = f"**Reason:** {reason or 'Unspecified'}\n\nSend a password from your OTP list."
        else:
            if previous_result == "timeout":
                color = 0xe74c3c  # red
                title = f"\u23f0 No Response — Strike {attempt - 1}/{max_attempts}"
                desc = f"**Reason:** {reason or 'Unspecified'}\n\nTry again — send a password."
            elif previous_result == "invalid":
                color = 0xe74c3c
                title = f"\u274c Wrong Password — Strike {attempt - 1}/{max_attempts}"
                desc = f"**Reason:** {reason or 'Unspecified'}\n\nTry again — send a password."
            elif previous_result == "used":
                color = 0xf39c12
                title = "\u26a0\ufe0f Password Already Used"
                desc = f"**Reason:** {reason or 'Unspecified'}\n\nThat one's been used — send a different password."
            else:
                color = 0xf39c12
                title = "\U0001f512 Security Verification — Retry"
                desc = f"**Reason:** {reason or 'Unspecified'}\n\nSend a password from your OTP list."

        embed = discord.Embed(title=title, description=desc, color=color)
        embed.set_footer(text=f"Attempt {attempt}/{max_attempts} · {int(timeout)}s timeout")

        # Mention full-trust users
        mention_content = self._client._permission_ping_content()

        # Set up the future before sending (avoid race)
        future: asyncio.Future[tuple[str, str]] = asyncio.get_event_loop().create_future()
        self._security_challenge_state = {
            "future": future,
            "channel_id": security_ch.id,
        }

        try:
            log.info("Security challenge: sending embed to #security (channel %s)", security_ch.id)
            msg = await security_ch.send(content=mention_content, embed=embed)
            self._security_message_ids.append(msg.id)
            log.info("Security challenge: embed sent (msg %s), waiting for response (timeout=%ds)",
                     msg.id, int(timeout))
        except (discord.Forbidden, discord.HTTPException) as e:
            self._security_challenge_state = None
            log.error("Security challenge: failed to send embed: %s", e)
            return {"response": None, "author_id": "", "timed_out": False,
                    "error": f"failed to send challenge: {e}"}

        # Wait for response
        try:
            content, author_id = await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            # Post timeout indicator and update embed
            try:
                embed.color = 0x95a5a6
                embed.title = "\u23f0 Timed Out"
                await msg.edit(embed=embed)
            except discord.HTTPException:
                pass
            return {"response": None, "author_id": "", "timed_out": True}
        finally:
            self._security_challenge_state = None

        # Update embed to show response received
        try:
            embed.color = 0x3498db  # blue — "processing"
            embed.title = "\U0001f50d Verifying..."
            await msg.edit(embed=embed)
        except discord.HTTPException:
            pass

        return {"response": content, "author_id": author_id, "timed_out": False}

    async def security_challenge_cleanup(self) -> dict:
        if not self._client:
            return {"ok": False, "error": "not connected"}

        security_ch = await self._resolve_target("#security")
        if not security_ch:
            self._security_message_ids.clear()
            return {"ok": False, "error": "no #security channel found"}

        deleted = 0
        log.info("Security cleanup: %d message ID(s) to delete: %s",
                 len(self._security_message_ids), self._security_message_ids)
        for msg_id in self._security_message_ids:
            try:
                msg = await security_ch.fetch_message(msg_id)
                await msg.delete()
                deleted += 1
                log.info("Security cleanup: deleted message %s", msg_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException) as e:
                log.warning("Security cleanup: failed to delete %s: %s", msg_id, e)

        self._security_message_ids.clear()
        self._security_challenge_state = None
        log.info("Security challenge cleanup: deleted %d message(s)", deleted)
        return {"ok": True, "deleted": deleted}

    # --- Internal helpers ---

    async def _resolve_target(self, target: str) -> discord.abc.Messageable | None:
        if not self._client:
            return None

        clean = target.lstrip("#")
        if clean in self._discord_config.channels:
            channel_id = int(self._discord_config.channels[clean])
            return self._client.get_channel(channel_id)

        if target.isdigit():
            ch = self._client.get_channel(int(target))
            if ch:
                return ch
            # Cache miss — try API fetch (needed for DM channels)
            try:
                return await self._client.fetch_channel(int(target))
            except discord.NotFound:
                return None

        if target.startswith("@"):
            user_ref = target[1:]
            # Resolve by numeric ID or by name from users registry
            if user_ref.isdigit():
                user_id = user_ref
            else:
                # Reverse lookup: find user ID by name
                user_id = None
                for uid, entry in self._discord_config.users.items():
                    if entry.get("name") == user_ref:
                        user_id = uid
                        break
            if user_id:
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
        return await channel.create_thread(name=name, type=discord.ChannelType.public_thread)


# ---------------------------------------------------------------------------
# _GatewayClient — Discord event handler + background tasks
# ---------------------------------------------------------------------------

class _GatewayClient(discord.Client):
    """Discord client for the gateway.

    Handles inbound messages, branch threads, channel mirroring, and status.
    """

    def __init__(self, config: GatewayConfig,
                 bridge_manager: BridgeManager,
                 subscription_manager: SubscriptionManager,
                 ready_event: asyncio.Event,
                 discord_channel: "DiscordChannel | None" = None,
                 **kwargs):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(intents=intents, **kwargs)

        self._config = config
        self._discord_config = config.discord
        self._bridge_manager = bridge_manager
        self._subscription_manager = subscription_manager
        self._ready_event = ready_event
        self._discord_channel = discord_channel

        state_dir = config.agent_home / "state"
        session_prefix = config.default_agent + "-" if config.default_agent else ""
        self._session_prefix = session_prefix

        # Branch threads: agent_id -> thread_id (for thread lifecycle management)
        self._branch_threads_path = state_dir / "discord-branch-threads.json"
        self._branch_threads: dict[str, int] = {
            k: int(v) for k, v in _load_json_state(self._branch_threads_path).items()
        }

        # Channel threads: channel_name -> thread_id (for bridge lifecycle)
        self._channel_threads_path = state_dir / "discord-channel-threads.json"
        self._channel_threads: dict[str, int] = {
            k: int(v) for k, v in _load_json_state(self._channel_threads_path).items()
        }

        # Status message ID
        self._status_msg_path = state_dir / "discord-status-msg-id"
        self._status_message_id: int | None = _load_int_state(self._status_msg_path)

    async def setup_hook(self) -> None:
        """Start background tasks after connection."""
        self.loop.create_task(self._sync_branch_threads_loop())
        self.loop.create_task(self._sync_channel_threads_loop())
        self.loop.create_task(self._outbound_bridge_loop())
        self.loop.create_task(self._update_presence_loop())
        self.loop.create_task(self._update_status_loop())

    async def on_ready(self) -> None:
        log.info("Discord client ready as %s (id: %s)", self.user, self.user.id)
        self._ready_event.set()

    async def on_message(self, message: discord.Message) -> None:
        if message.author == self.user:
            return

        sender_id = str(message.author.id)

        # Security challenge intercept — resolve pending challenge if this
        # message is in the security channel from a non-bot user.
        sc = self._discord_channel._security_challenge_state if self._discord_channel else None
        if sc and str(message.channel.id) == str(sc["channel_id"]):
            self._discord_channel._security_message_ids.append(message.id)
            if not sc["future"].done():
                sc["future"].set_result((message.content, sender_id))
            return  # consumed — don't route normally

        # Access control
        is_dm = isinstance(message.channel, discord.DMChannel)
        access = self._discord_config.dm_access if is_dm else self._discord_config.channel_access
        if not access.is_allowed(sender_id):
            log.debug("Blocked message from %s (%s) — access denied",
                      message.author, sender_id)
            return

        # Identity resolution — always from the shared users registry
        sender_name, trust = self._discord_config.resolve_user(
            sender_id, fallback_name=message.author.name
        )

        # Update presence for full-trust users
        if trust == "full":
            self._write_presence_discord()

        content = message.content
        if not content.strip() and not message.attachments:
            return

        # Control channel: handle as command, skip normal routing
        control_id = self._discord_config.channels.get("control")
        if control_id and str(message.channel.id) == control_id:
            if trust != "full":
                await message.reply("⛔ Control commands require full trust.")
                return
            await self._handle_control_command(message, content.strip())
            return

        # Determine surface ID for routing
        if is_dm:
            surface_id = f"dm/{sender_id}"
            channel_desc = "dm"
        else:
            surface_id = str(message.channel.id)
            if hasattr(message.channel, "name"):
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

        # --- Routing ---

        # Resolve trust label (combines config trust with live verification)
        resolved_trust = trust
        if _trust_label is not None:
            state_dir = self._config.agent_home / "state"
            resolved_trust = _trust_label(
                state_dir, "discord",
                config_trust=trust, sender_user_id=sender_id,
            )

        # 1. Bridge: inject into Kiln channel if this surface is bridged
        self._bridge_manager.inject_inbound(surface_id, sender_name, content)

        # 2. Subscriptions: deliver to all subscribers
        self._subscription_manager.refresh()
        subscribers = self._subscription_manager.get_subscribers(surface_id)
        for agent_id in subscribers:
            write_to_inbox(
                self._config.agent_home, agent_id,
                sender_name=sender_name, sender_id=sender_id,
                content=content, platform="discord",
                channel_desc=channel_desc, channel_id=str(message.channel.id),
                trust=resolved_trust, attachment_paths=attachment_paths or None,
            )

        if subscribers:
            log.info("Discord -> %d subscriber(s) from %s in %s",
                     len(subscribers), sender_name, channel_desc)

    # -----------------------------------------------------------------------
    # Presence
    # -----------------------------------------------------------------------

    def _write_presence_discord(self) -> None:
        """Write discord presence timestamp to state file."""
        state_dir = self._config.agent_home / "state"
        state_dir.mkdir(parents=True, exist_ok=True)
        path = state_dir / "presence-discord"
        path.write_text(datetime.now(timezone.utc).isoformat() + "\n")

    def _permission_ping_content(self) -> str | None:
        """Build mention string for permission prompts when the owner isn't at terminal.

        Returns None if the owner is at terminal (no ping needed), or a string
        of Discord user mentions for all full-trust users.

        Uses read_presence() to properly compare terminal vs discord activity
        rather than just checking terminal freshness alone.
        """
        state_dir = self._config.agent_home / "state"

        # Use kiln's presence logic if available — compares both surfaces
        # and picks the most recent one.
        try:
            from kiln.state import read_presence
            presence = read_presence(state_dir)
            if presence["location"] == "terminal":
                return None  # at terminal, no ping needed
        except ImportError:
            # Fallback: raw terminal presence check
            presence_file = state_dir / "presence-terminal"
            idle_threshold = 300  # 5 minutes
            if presence_file.exists():
                try:
                    ts_str = presence_file.read_text().strip().splitlines()[0]
                    ts = datetime.fromisoformat(ts_str)
                    ago = (datetime.now(timezone.utc) - ts).total_seconds()
                    if ago < idle_threshold:
                        return None
                except (ValueError, IndexError, OSError):
                    pass

        # Not at terminal — mention full-trust users
        mentions = []
        for uid, entry in self._discord_config.users.items():
            trust = entry.get("max_trust") or entry.get("trust")
            if trust == "full":
                mentions.append(f"<@{uid}>")
        return " ".join(mentions) if mentions else None

    # -----------------------------------------------------------------------
    # Control channel
    # -----------------------------------------------------------------------

    async def _handle_control_command(
        self, message: discord.Message, text: str,
    ) -> None:
        """Parse and execute a control channel command."""
        parts = text.split()
        if not parts:
            return

        cmd = parts[0].lower()
        try:
            if cmd == "mode":
                await self._cmd_mode(message, parts[1:])
            elif cmd == "spawn":
                await self._cmd_spawn(message, text[len("spawn"):].strip())
            elif cmd == "kill":
                await self._cmd_kill(message, parts[1:])
            elif cmd == "show":
                await self._cmd_show(message, parts[1:])
            elif cmd == "help":
                await message.reply(
                    "**Commands:**\n"
                    "`mode <agent> <mode>` — change permission mode "
                    "(safe, supervised, yolo)\n"
                    "`spawn [instructions]` — launch a new session\n"
                    "`kill <agent>` — kill a session (immediate)\n"
                    "`show <agent>` — capture current terminal pane\n"
                    "`help` — this message"
                )
            else:
                await message.reply(f"Unknown command: `{cmd}`. Try `help`.")
        except Exception as e:
            log.exception("Control command error: %s", text)
            await message.reply(f"❌ Error: {e}")

    async def _cmd_mode(
        self, message: discord.Message, args: list[str],
    ) -> None:
        """Handle: mode <agent-id> <mode>"""
        if len(args) != 2:
            await message.reply("Usage: `mode <agent> <mode>`")
            return

        agent_ref, mode_str = args[0], args[1].lower()

        # Validate mode — trusted is not allowed remotely
        valid_modes = {"safe", "supervised", "yolo"}
        if mode_str == "trusted":
            await message.reply("⛔ Trusted mode is TUI-only.")
            return
        if mode_str not in valid_modes:
            await message.reply(
                f"Invalid mode: `{mode_str}`. "
                f"Valid: {', '.join(sorted(valid_modes))}"
            )
            return

        # Resolve agent ID — accept short names (e.g. "storm-jay")
        agent_id = self._resolve_agent_id(agent_ref)
        if not agent_id:
            await message.reply(f"No running session matching `{agent_ref}`.")
            return

        # Write to session config
        config_path = (
            self._config.agent_home / "state" / f"session-config-{agent_id}.yml"
        )
        if not config_path.exists():
            await message.reply(f"No session config for `{agent_id}`.")
            return

        try:
            data = yaml.safe_load(config_path.read_text()) or {}
        except (yaml.YAMLError, OSError):
            data = {}

        old_mode = data.get("mode", "?")
        data["mode"] = mode_str
        tmp = config_path.with_suffix(".tmp")
        tmp.write_text(yaml.dump(data, default_flow_style=False, sort_keys=False))
        tmp.rename(config_path)

        await message.reply(f"✅ `{agent_id}`: {old_mode} → **{mode_str}**")
        log.info("Control: mode %s -> %s (by %s)", agent_id, mode_str,
                 message.author.name)

    async def _cmd_spawn(
        self, message: discord.Message, instructions: str,
    ) -> None:
        """Handle: spawn [instructions]"""
        agent_name = self._config.default_agent
        if not agent_name:
            await message.reply("❌ No default agent configured (`routing.default_agent` in gateway config).")
            return
        cli_path = shutil.which(agent_name)
        if not cli_path:
            await message.reply(f"❌ Cannot find `{agent_name}` on PATH.")
            return

        cmd = [cli_path, "run", "--mode", "yolo", "--detach"]
        if instructions:
            cmd.extend(["--prompt", instructions])

        log.info("Control: spawn by %s — %s", message.author.name,
                 instructions[:100] if instructions else "(no instructions)")

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=30,
            )
            if proc.returncode == 0:
                output = stdout.decode().strip()
                await message.reply(
                    f"✅ Session launched.\n```\n{output[:500]}\n```"
                    if output else "✅ Session launched."
                )
            else:
                err = stderr.decode().strip()[:500]
                await message.reply(
                    f"❌ Spawn failed (exit {proc.returncode}):\n```\n{err}\n```"
                )
        except asyncio.TimeoutError:
            await message.reply("❌ Spawn timed out (30s).")
        except Exception as e:
            await message.reply(f"❌ Spawn error: {e}")

    async def _cmd_kill(
        self, message: discord.Message, args: list[str],
    ) -> None:
        """Handle: kill <agent-id>"""
        if not args:
            await message.reply("Usage: `kill <agent>`")
            return

        agent_ref = args[0]
        agent_id = self._resolve_agent_id(agent_ref)
        if not agent_id:
            await message.reply(f"No running session matching `{agent_ref}`.")
            return

        log.info("Control: kill %s (by %s)", agent_id, message.author.name)

        proc = await asyncio.create_subprocess_exec(
            "tmux", "kill-session", "-t", agent_id,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await proc.communicate()
        if proc.returncode == 0:
            await message.reply(f"💀 `{agent_id}` killed.")
        else:
            await message.reply(f"❌ Failed to kill `{agent_id}` (tmux session not found?).")

    async def _cmd_show(
        self, message: discord.Message, args: list[str],
    ) -> None:
        """Handle: show <agent-id>"""
        if not args:
            await message.reply("Usage: `show <agent>`")
            return

        agent_ref = args[0]
        agent_id = self._resolve_agent_id(agent_ref)
        if not agent_id:
            await message.reply(f"No running session matching `{agent_ref}`.")
            return

        # Read mode from session config
        config_path = (
            self._config.agent_home / "state" / f"session-config-{agent_id}.yml"
        )
        mode = "unknown"
        if config_path.exists():
            try:
                data = yaml.safe_load(config_path.read_text()) or {}
                mode = data.get("mode", "unknown")
            except (yaml.YAMLError, OSError):
                pass

        # Capture the tmux pane (50 lines of scrollback, plain text)
        proc = await asyncio.create_subprocess_exec(
            "tmux", "capture-pane", "-t", agent_id, "-p", "-S", "-50",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()

        if proc.returncode != 0:
            await message.reply(
                f"❌ Failed to capture `{agent_id}` (tmux session not found?)."
            )
            return

        lines = [line.rstrip() for line in stdout.decode(errors="replace").splitlines()]
        while lines and not lines[-1]:
            lines.pop()

        header = f"📟 **{agent_id}** | mode: `{mode}`\n"
        if not lines:
            await message.reply(f"{header}*(empty pane)*")
            return

        # Trim from the top to fit within Discord's 2000-char message limit
        max_content = 2000 - len(header) - len("```\n\n```")
        content = "\n".join(lines)
        if len(content) > max_content:
            content = content[-max_content:]
            newline_pos = content.find("\n")
            if newline_pos != -1:
                content = content[newline_pos + 1:]

        log.info("Control: show %s (by %s)", agent_id, message.author.name)
        await message.reply(f"{header}```\n{content}\n```", view=_DismissView())

    def _resolve_agent_id(self, ref: str) -> str | None:
        """Resolve a short agent reference to a full agent ID.

        Accepts full IDs (beth-storm-jay) or short names (storm-jay).
        Matches against running sessions from the registry.
        """
        registry = _read_registry(self._config.agent_home)
        if not registry:
            return None

        # Exact match
        if ref in registry:
            return ref

        # Short name: try prefixing with session_prefix
        prefix = self._session_prefix
        full = f"{prefix}{ref}"
        if full in registry:
            return full

        # Partial match: find unique match containing ref
        matches = [aid for aid in registry if ref in aid]
        if len(matches) == 1:
            return matches[0]

        return None

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
        state_dir = self._config.agent_home / "state"

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
                # Register subscription so messages route to this session
                SubscriptionManager.subscribe(state_dir, agent_id, str(thread.id))
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
        state_dir = self._config.agent_home / "state"
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
                changed = True
                log.info("Created branch thread '%s' for %s", short_name, agent_id)

                # Register subscription so messages route to this session
                SubscriptionManager.subscribe(state_dir, agent_id, str(thread.id))

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
    # Channel bridges (Kiln channel ↔ Discord thread)
    # -----------------------------------------------------------------------

    async def _sync_channel_threads_loop(self) -> None:
        """Manage Discord thread lifecycle for Kiln channel bridges."""
        await self.wait_until_ready()

        channels_ch = self._resolve_config_channel("channels")
        if not channels_ch:
            log.warning("No #channels channel configured — channel bridges disabled")
            return

        log.info("Channel bridge manager started")

        # Register send callback for outbound bridge messages
        async def send_to_surface(surface_id: str, content: str) -> None:
            thread = self.get_channel(int(surface_id))
            if not thread:
                thread = await self.fetch_channel(int(surface_id))
            if hasattr(thread, "archived") and thread.archived:
                await thread.edit(archived=False)
            for chunk in split_message(content):
                await thread.send(chunk)

        self._bridge_manager.register_send_callback("discord", send_to_surface)

        # Reconcile
        try:
            await self._reconcile_channel_threads(channels_ch)
        except Exception:
            log.exception("Channel thread reconciliation error")

        # Lifecycle sync loop (thread creation/archival only)
        while not self.is_closed():
            try:
                await self._sync_channel_lifecycle(channels_ch)
            except Exception:
                log.exception("Channel thread sync error")
            await asyncio.sleep(STATUS_UPDATE_INTERVAL)

    async def _outbound_bridge_loop(self) -> None:
        """Poll bridged Kiln channels and forward new messages to platforms."""
        await self.wait_until_ready()
        while not self.is_closed():
            try:
                await self._bridge_manager.forward_outbound()
            except Exception:
                log.exception("Outbound bridge error")
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
                # Register bridge: Discord thread ↔ Kiln channel
                self._bridge_manager.register(Bridge(
                    kiln_channel=name,
                    platform="discord",
                    surface_id=str(thread.id),
                    surface_desc=f"#channels/{name}",
                ))
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
                changed = True
                log.info("Created channel thread '%s'", name)

                # Register bridge
                bridge = Bridge(
                    kiln_channel=name,
                    platform="discord",
                    surface_id=str(thread.id),
                    surface_desc=f"#channels/{name}",
                )
                self._bridge_manager.register(bridge)

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
                            formatted = BridgeManager._format_outbound(msg)
                            if formatted:
                                parts.append(formatted)
                        except json.JSONDecodeError:
                            continue
                    if parts:
                        catchup = "**[history]**\n" + "\n".join(parts)
                        for chunk in split_message(catchup):
                            await thread.send(chunk)
            except discord.HTTPException as e:
                log.error("Failed to create channel thread '%s': %s", name, e)

        # Archive dead channels
        dead = set(self._channel_threads.keys()) - set(active.keys())
        for name in dead:
            thread_id = self._channel_threads.pop(name)
            self._bridge_manager.unregister_channel(name)
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



    # -----------------------------------------------------------------------
    # Status display
    # -----------------------------------------------------------------------

    async def _update_presence_loop(self) -> None:
        await self.wait_until_ready()
        log.info("Presence updater started")

        while not self.is_closed():
            try:
                agents = self._collect_agent_data()
                canonical_id = _read_canonical(self._config.agent_home)
                text = _build_presence_text(agents, canonical_id)
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
                canonical_id = _read_canonical(self._config.agent_home)
                embeds = _build_status_embeds(agents, canonical_id)
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
            usage = _get_context_usage(entry)
            agents.append({
                "id": agent_id,
                "uptime": _format_uptime(entry.get("started_at", "")),
                "context": _format_context(usage),
                "context_pct": int(usage[0] / usage[1] * 100) if usage else None,
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

    async def _download_attachments(
        self, attachments: list[discord.Attachment], sender_name: str
    ) -> list[str]:
        download_dir = self._config.agent_home / "scratch" / "discord-attachments"
        download_dir.mkdir(parents=True, exist_ok=True)

        paths = []
        for att in attachments:
            import uuid as _uuid
            dest = download_dir / f"{sender_name}-{_uuid.uuid4().hex[:6]}-{att.filename}"
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
            from voice.openai import transcribe
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
            voice_prefix = f"[Voice message transcript — may contain errors]\n{transcript}"
            if content.strip():
                return f"{voice_prefix}\n\n{content}"
            return voice_prefix
        else:
            return (
                f"[Voice message received — transcription failed. "
                f"Audio saved at: {audio_path}]"
                + (f"\n\n{content}" if content.strip() else "")
            )
