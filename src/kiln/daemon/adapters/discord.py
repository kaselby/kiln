"""Discord platform adapter for the Kiln daemon.

Connects Discord to the daemon's event bus and management layer.
Handles inbound message routing, control commands, outbound bridge
rendering, and Discord-specific UX (status embeds, branch threads,
permission approval UI).

The adapter authenticates/translates platform input; the daemon owns
durable delivery, routing, and coordination semantics.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import tempfile
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

import discord

from .. import protocol as proto
from ..protocol import PlatformMessage, RequestContext
from ..state import BridgeRecord

log = logging.getLogger(__name__)

# Discord message length limit
DISCORD_MAX_LENGTH = 2000
SPLIT_MARGIN = 100  # room for (1/N) prefix


# ---------------------------------------------------------------------------
# Filename sanitization
# ---------------------------------------------------------------------------

def _sanitize_filename(name: str) -> str:
    """Sanitize an external filename to a safe local basename.

    Strips path separators, replaces dangerous characters, and truncates
    to a reasonable length. Returns 'attachment' if nothing usable remains.
    """
    # Strip any path components — only keep the basename
    name = os.path.basename(name)
    # Replace path-traversal and shell-dangerous characters
    name = re.sub(r'[/\\<>:"|?*\x00-\x1f]', '_', name)
    # Collapse runs of underscores/dots
    name = re.sub(r'[_.]{2,}', '_', name)
    name = name.strip('._ ')
    # Truncate to 200 chars (preserving extension)
    if len(name) > 200:
        base, _, ext = name.rpartition('.')
        if ext and len(ext) <= 10:
            name = base[:200 - len(ext) - 1] + '.' + ext
        else:
            name = name[:200]
    return name or "attachment"


# ---------------------------------------------------------------------------
# Message formatting utilities
# ---------------------------------------------------------------------------

def split_message(text: str, max_len: int = DISCORD_MAX_LENGTH - SPLIT_MARGIN) -> list[str]:
    """Split a long message into Discord-safe chunks.

    Splits at paragraph boundaries, preserving code blocks.
    """
    if len(text) <= max_len:
        return [text]

    chunks: list[str] = []
    remaining = text

    while remaining:
        if len(remaining) <= max_len:
            chunks.append(remaining)
            break

        split_at = _find_split_point(remaining, max_len)
        chunks.append(remaining[:split_at].rstrip())
        remaining = remaining[split_at:].lstrip("\n")

    if len(chunks) > 1:
        total = len(chunks)
        chunks = [f"({i+1}/{total})\n{chunk}" for i, chunk in enumerate(chunks)]

    return chunks


def _find_split_point(text: str, max_len: int) -> int:
    """Find the best place to split text, respecting structure."""
    # Don't split inside code blocks if possible
    code_blocks = list(re.finditer(r"```.*?```", text[:max_len + 500], re.DOTALL))
    for block in code_blocks:
        if block.start() < max_len < block.end():
            candidate = block.start()
            if candidate > max_len // 2:
                return candidate
            break

    search_region = text[:max_len]

    # Try paragraph boundary
    last_para = search_region.rfind("\n\n")
    if last_para > max_len // 2:
        return last_para + 1

    # Try line boundary
    last_line = search_region.rfind("\n")
    if last_line > max_len // 2:
        return last_line + 1

    return max_len


def format_outbound(sender: str, body: str, summary: str = "") -> str | None:
    """Format a Kiln channel message for Discord display.

    Returns None if there's nothing to display.
    """
    text = body or summary
    if not text:
        return None
    return f"**{sender}:** {text}"


# ---------------------------------------------------------------------------
# Status embed colors and constants
# ---------------------------------------------------------------------------

COLOR_ACTIVE = 0x2ECC71   # green — session has context data
COLOR_IDLE = 0x95A5A6     # gray — session present, no context data
COLOR_CONCLAVE = 0x9B59B6 # purple — conclave group

STATUS_REFRESH_INTERVAL = 60  # seconds between periodic refreshes
STATUS_REFRESH_DEBOUNCE = 5   # minimum seconds between event-triggered refreshes
USAGE_CACHE_TTL = 600         # 10 min cache for subscription usage data

# Max context tokens — used by old CC JSONL context% calculation.
# This is a Beth/Claude-specific assumption; other backends may differ.
MAX_CONTEXT_TOKENS = 200_000


# ---------------------------------------------------------------------------
# Status/presence — pure formatting functions
# ---------------------------------------------------------------------------

def _format_uptime(started_at: str) -> str:
    """Format a connected_at ISO timestamp as relative uptime."""
    from datetime import datetime, timezone
    try:
        start = datetime.fromisoformat(started_at)
        if start.tzinfo is None:
            delta = datetime.now() - start
        else:
            delta = datetime.now(timezone.utc) - start
        hours, remainder = divmod(int(delta.total_seconds()), 3600)
        minutes = remainder // 60
        if hours > 0:
            return f"{hours}h{minutes:02d}m"
        return f"{minutes}m"
    except (ValueError, TypeError):
        return "?"


def _format_context(usage: tuple[int, int] | None) -> str:
    """Format (used, total) token pair as percentage string."""
    if usage is None:
        return "?"
    used, total = usage
    if total <= 0:
        return "?"
    return f"{int(used / total * 100)}%"


def _format_usage_reset(resets_at: str | None) -> str:
    """Format an ISO reset timestamp as relative time."""
    from datetime import datetime, timezone
    if not resets_at:
        return ""
    try:
        reset = datetime.fromisoformat(resets_at)
        total_seconds = int((reset - datetime.now(timezone.utc)).total_seconds())
        if total_seconds <= 0:
            return "resetting"
        hours, remainder = divmod(total_seconds, 3600)
        minutes = remainder // 60
        if hours > 24:
            days = hours // 24
            return f"resets {days}d {hours % 24}h"
        elif hours > 0:
            return f"resets {hours}h {minutes}m"
        return f"resets {minutes}m"
    except (ValueError, TypeError):
        return ""


def _format_codex_reset(reset_at: int | None) -> str:
    """Format a unix timestamp as relative time string."""
    from datetime import datetime, timezone
    if not reset_at:
        return ""
    try:
        total_seconds = int(reset_at - datetime.now(timezone.utc).timestamp())
        if total_seconds <= 0:
            return "resetting"
        hours, remainder = divmod(total_seconds, 3600)
        minutes = remainder // 60
        if hours > 24:
            days = hours // 24
            return f"resets {days}d {hours % 24}h"
        elif hours > 0:
            return f"resets {hours}h {minutes}m"
        return f"resets {minutes}m"
    except (ValueError, TypeError):
        return ""


def _format_usage_lines(data: dict) -> list[str]:
    """Format subscription usage as Discord-friendly lines (one per provider)."""
    lines = []

    # Anthropic
    anthropic = data.get("anthropic", data if "five_hour" in data else {})
    parts = []
    for key, label in [("five_hour", "5h"), ("seven_day", "7d"), ("seven_day_sonnet", "Sonnet")]:
        limit = anthropic.get(key)
        if not limit or limit.get("utilization") is None:
            continue
        util = limit["utilization"]
        reset = _format_usage_reset(limit.get("resets_at"))
        part = f"**{label}:** {util:.0f}%"
        if reset:
            part += f" ({reset})"
        parts.append(part)
    if parts:
        lines.append("\U0001f4ca Anthropic: " + " \u00b7 ".join(parts))

    # OpenAI/Codex
    openai_data = data.get("openai", {})
    rate_limit = openai_data.get("rate_limit", {})
    parts = []
    for window_key, label in [("primary_window", "5h"), ("secondary_window", "7d")]:
        window = rate_limit.get(window_key)
        if not window:
            continue
        used = window.get("used_percent", 0)
        reset_at = window.get("reset_at")
        reset = _format_codex_reset(reset_at)
        part = f"**{label}:** {used}%"
        if reset:
            part += f" ({reset})"
        parts.append(part)
    if parts:
        lines.append("\U0001f4ca OpenAI: " + " \u00b7 ".join(parts))

    return lines


def _build_presence_text(agents: list[dict], canonical_id: str | None = None) -> str:
    """Build bot activity text from agent data.

    Shows agent count and canonical (or most recent) agent's context%.
    """
    if not agents:
        return "No agents running"
    count = len(agents)
    canonical = next((a for a in agents if a["id"] == canonical_id), None) if canonical_id else None
    target = canonical or agents[-1]
    name = target["id"].removeprefix(f"{target['id'].split('-')[0]}-")
    ctx = target.get("context", "?")
    text = f"{count} agent{'s' if count != 1 else ''} | {name} {ctx}"
    return text[:128]


def _build_agent_embed(agent: dict, canonical_id: str | None) -> discord.Embed:
    """Build a single-agent status embed."""
    context_pct = agent.get("context_pct")
    color = COLOR_ACTIVE if context_pct is not None else COLOR_IDLE
    is_canonical = agent["id"] == canonical_id

    title = f"\U0001f7e2 {agent['id']}"
    if is_canonical:
        title += " \u2b50"

    embed = discord.Embed(title=title, color=color)

    meta = [
        f"**Uptime:** {agent['uptime']}",
        f"**Context:** {agent.get('context', '?')}",
    ]
    mode = agent.get("mode")
    if mode and mode != "supervised":
        meta.append(f"**Mode:** {mode}")
    if is_canonical:
        meta.append("\u2b50 **canonical**")
    if agent.get("inbox", 0) > 0:
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

    return embed


def _build_conclave_embed(
    name: str, members: list[dict], canonical_id: str | None,
) -> discord.Embed:
    """Build a single embed for a conclave group."""
    embed = discord.Embed(
        title=f"\U0001f52e {name}",
        color=COLOR_CONCLAVE,
    )
    lines = []
    for agent in members:
        is_fac = agent.get("_role") == "facilitator"
        is_can = agent["id"] == canonical_id
        prefix = "\u2605" if is_fac else "\u2003"
        label = f"{prefix} **{agent['id']}**"
        if is_fac:
            label += " (facilitator)"
        if is_can:
            label += " \u2b50"
        meta = f"up {agent['uptime']} \u00b7 ctx {agent.get('context', '?')}"
        if agent.get("inbox", 0) > 0:
            meta += f" \u00b7 {agent['inbox']} msg"
        lines.append(f"{label}\n\u2003 {meta}")

    embed.description = "\n".join(lines)

    # Show facilitator's plan (or first plan found)
    for agent in members:
        plan = agent.get("plan")
        if plan:
            tasks = plan.get("tasks", [])
            done = sum(1 for t in tasks if t.get("status") == "done")
            total = len(tasks)
            goal = plan.get("goal", "?")
            if len(goal) > 60:
                goal = goal[:59] + "\u2026"
            embed.add_field(name="Plan", value=f"{goal} ({done}/{total})", inline=False)
            break

    return embed


def _build_status_embeds(
    agents: list[dict],
    canonical_id: str | None = None,
    membership: dict[str, dict] | None = None,
) -> list[discord.Embed]:
    """Build the full set of status embeds from agent data.

    Partitions agents into standalone and conclave groups.
    Returns a list of embeds (max 10 for Discord's per-message limit).
    """
    from datetime import datetime, timezone

    if not agents:
        return [discord.Embed(
            title="No agents running",
            color=COLOR_IDLE,
            timestamp=datetime.now(timezone.utc),
        )]

    membership = membership or {}

    standalone = []
    conclaves: dict[str, list[dict]] = {}
    for agent in agents:
        m = membership.get(agent["id"])
        if m:
            agent_with_role = {**agent, "_role": m["role"]}
            conclaves.setdefault(m["conclave"], []).append(agent_with_role)
        else:
            standalone.append(agent)

    embeds = []
    for agent in standalone:
        embeds.append(_build_agent_embed(agent, canonical_id))
    for cname, members in sorted(conclaves.items()):
        members.sort(key=lambda a: (0 if a.get("_role") == "facilitator" else 1, a["id"]))
        embeds.append(_build_conclave_embed(cname, members, canonical_id))

    return embeds[:10]


# ---------------------------------------------------------------------------
# Inbound message types (adapter-local — not wire protocol)
# ---------------------------------------------------------------------------

@dataclass
class InboundMessage:
    """Pre-extracted Discord message data for routing.

    The discord.py on_message handler extracts raw Discord data into
    this struct, then passes it to ``handle_message()``. This decouples
    routing logic from the Discord client, enabling testing without
    a live connection.
    """

    sender_id: str              # Discord user ID
    sender_display_name: str    # Discord display name (fallback if not in user registry)
    channel_id: str             # Discord channel or thread ID
    content: str                # Text content (already transcribed if voice)
    is_dm: bool
    message_id: str = ""        # Discord message ID (for reply threading)
    attachment_paths: list[str] = field(default_factory=list)


@dataclass
class ControlResponse:
    """Result of a control command, formatted for the adapter's platform.

    The adapter returns these from control command parsing. The outbound
    path (Slice C) formats them as Discord messages. Tests assert against
    the fields directly.
    """

    success: bool
    message: str
    data: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# Route classification
# ---------------------------------------------------------------------------

class RouteBucket(str, Enum):
    """The three inbound delivery paths, plus control intercept."""

    BRANCH = "branch"     # session-bound thread → one session
    BRIDGE = "bridge"     # channel thread → Kiln channel
    SURFACE = "surface"   # DMs / watched channels → N surface subscribers


@dataclass
class RouteDecision:
    """Result of classifying an inbound message."""

    bucket: RouteBucket
    session_id: str = ""      # BRANCH: target session
    channel_name: str = ""    # BRIDGE: target Kiln channel
    surface_ref: str = ""     # SURFACE: canonical surface ref


class RoutingError(Exception):
    """Raised when an inbound message matches multiple route buckets.

    This is an invariant violation — every message must classify to
    exactly one bucket. Multiple matches indicate overlapping routing
    configuration (e.g. a thread that's both a branch and has surface
    subscribers).
    """


# ---------------------------------------------------------------------------
# Access control
# ---------------------------------------------------------------------------

@dataclass
class AccessPolicy:
    """Access control policy for a message surface (channels or DMs).

    Modes:
        open        — anyone can interact
        allowlist   — only user IDs in the allowlist
        read_only   — no interaction
    """

    mode: str = "open"
    allowlist: set[str] = field(default_factory=set)

    def is_allowed(self, user_id: str) -> bool:
        if self.mode == "open":
            return True
        if self.mode == "allowlist":
            return user_id in self.allowlist
        return False  # read_only or unknown


# ---------------------------------------------------------------------------
# Adapter config
# ---------------------------------------------------------------------------

@dataclass
class DiscordAdapterConfig:
    """Discord-specific adapter configuration.

    Parsed from the adapter's config dict in daemon config.yml.
    """

    guild_id: str = ""
    channels: dict[str, str] = field(default_factory=dict)  # name -> discord channel ID
    users: dict[str, dict[str, str]] = field(default_factory=dict)  # discord user ID -> {name, max_trust, ...}
    channel_access: AccessPolicy = field(default_factory=AccessPolicy)
    dm_access: AccessPolicy = field(default_factory=lambda: AccessPolicy(mode="allowlist"))
    default_agent: str = ""
    session_prefix: str = ""
    credentials_dir: str = ""      # path to credentials dir (for voice service)
    voice_default: str = ""        # default TTS voice name
    voice_instructions: str = ""   # default TTS voice instructions
    usage_command: str = ""        # command to fetch usage JSON (e.g. "/path/to/usage --json")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DiscordAdapterConfig:
        """Parse from raw config dict."""
        cfg = cls(
            guild_id=str(data.get("guild_id", "")),
            channels=data.get("channels", {}),
            users={str(k): v for k, v in data.get("users", {}).items()},
            default_agent=data.get("default_agent", ""),
            session_prefix=data.get("session_prefix", ""),
            credentials_dir=data.get("credentials_dir", ""),
            voice_default=data.get("voice_default", ""),
            voice_instructions=data.get("voice_instructions", ""),
        )

        # If no session_prefix set, derive from default_agent
        if not cfg.session_prefix and cfg.default_agent:
            cfg.session_prefix = f"{cfg.default_agent}-"

        # Channel access
        ch_mode = data.get("channel_access", "open")
        ch_allowlist = set(str(uid) for uid in data.get("channel_allowlist", cfg.users.keys()))
        cfg.channel_access = AccessPolicy(mode=ch_mode, allowlist=ch_allowlist)

        # DM access — defaults to allowlist with all known users
        dm_mode = data.get("dm_access", "allowlist")
        dm_allowlist = set(str(uid) for uid in data.get("dm_allowlist", cfg.users.keys()))
        cfg.dm_access = AccessPolicy(mode=dm_mode, allowlist=dm_allowlist)

        return cfg

    def resolve_user(self, user_id: str, fallback_name: str = "") -> tuple[str, str]:
        """Look up a user's display name and base trust level.

        Returns (name, max_trust). Falls back to fallback_name if unknown.
        """
        entry = self.users.get(str(user_id), {})
        name = entry.get("name", fallback_name or user_id)
        trust = entry.get("max_trust") or entry.get("trust", "unknown")
        return name, trust


def _load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _save_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n")


# ---------------------------------------------------------------------------
# Permission approval UI — discord.py Views
# ---------------------------------------------------------------------------

class _PermissionView(discord.ui.View):
    """Discord view with Approve/Reject/Details buttons for permission approval.

    Trust-gated: only users with ``max_trust == "full"`` in the adapter's
    user registry can approve or reject.  The *future* resolves with
    ``(approved: bool, responder_name: str)`` when a trusted user clicks.
    """

    def __init__(
        self, future: asyncio.Future, discord_config: DiscordAdapterConfig,
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
        _name, trust = self._discord_config.resolve_user(str(user_id))
        return trust == "full"

    @discord.ui.button(label="Approve", style=discord.ButtonStyle.green, emoji="\u2705")
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._check_trust(str(interaction.user.id)):
            await interaction.response.send_message(
                "You don't have permission to approve commands.", ephemeral=True,
            )
            return
        if not self._future.done():
            self._future.set_result((True, interaction.user.display_name))
        await interaction.response.defer()
        self.stop()

    @discord.ui.button(label="Reject", style=discord.ButtonStyle.red, emoji="\u274c")
    async def reject(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self._check_trust(str(interaction.user.id)):
            await interaction.response.send_message(
                "You don't have permission to reject commands.", ephemeral=True,
            )
            return
        if not self._future.done():
            self._future.set_result((False, interaction.user.display_name))
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


# ---------------------------------------------------------------------------
# Security challenge transport — Discord implementation
# ---------------------------------------------------------------------------

class _DiscordChallengeTransport:
    """Discord-specific transport for :func:`run_security_challenge`.

    Handles posting/deleting messages in #security, @mentioning full-trust
    users, and bridging the adapter's message intercept into the transport's
    ``wait_for_response`` future.

    Constructed by the adapter for each challenge invocation.
    """

    def __init__(self, adapter: DiscordAdapter, channel: discord.abc.Messageable):
        self._adapter = adapter
        self._channel = channel
        self._message_ids: list[int] = []

    async def post_challenge(self, text: str) -> None:
        mentions = self._adapter._build_owner_mentions()
        prefix = f"{mentions}\n" if mentions else ""
        try:
            msg = await self._channel.send(f"{prefix}{text}")
            self._message_ids.append(msg.id)
        except discord.HTTPException as e:
            raise RuntimeError(f"Failed to post security challenge: {e}") from e

    async def wait_for_response(self, timeout: float) -> tuple[str, str, str] | None:
        loop = asyncio.get_running_loop()
        future: asyncio.Future[tuple[str, str, str]] = loop.create_future()
        self._adapter._security_challenge_state = {
            "future": future,
            "channel_id": str(self._channel.id)
                          if hasattr(self._channel, "id") else "",
            "message_ids": self._message_ids,
        }
        try:
            result = await asyncio.wait_for(future, timeout=timeout)
            content, author_id, msg_id = result
            # Track response message for cleanup
            try:
                self._message_ids.append(int(msg_id))
            except (ValueError, TypeError):
                pass
            return (content, author_id, str(msg_id))
        except asyncio.TimeoutError:
            return None
        finally:
            self._adapter._security_challenge_state = None

    async def post_message(self, text: str) -> None:
        try:
            msg = await self._channel.send(text)
            self._message_ids.append(msg.id)
        except discord.HTTPException:
            pass

    async def delete_message(self, message_id: str) -> None:
        try:
            partial = self._channel.get_partial_message(int(message_id))
            await partial.delete()
        except (discord.NotFound, discord.Forbidden, discord.HTTPException,
                ValueError, TypeError):
            pass

    async def cleanup(self) -> None:
        for mid in self._message_ids:
            try:
                partial = self._channel.get_partial_message(mid)
                await partial.delete()
            except (discord.NotFound, discord.Forbidden,
                    discord.HTTPException):
                pass
        log.info("Security challenge cleanup: %d message(s)", len(self._message_ids))


# ---------------------------------------------------------------------------
# Discord client — thin extraction layer
# ---------------------------------------------------------------------------

class _DiscordClient(discord.Client):
    """Thin discord.py client owned by the adapter.

    Responsibilities are strictly limited to:
    - Discord event extraction (on_message → InboundMessage)
    - Readiness signaling (on_ready → ready event)
    - Attachment download to local temp files

    The client does NOT own routing, state, thread maps, or access policy.
    Those belong to the adapter.
    """

    def __init__(self, adapter: DiscordAdapter, ready_event: asyncio.Event):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(intents=intents)
        self._adapter = adapter
        self._ready_event = ready_event

    async def on_ready(self) -> None:
        log.info("Discord client ready as %s (id: %s)", self.user, self.user.id)
        self._ready_event.set()

    async def on_message(self, message: discord.Message) -> None:
        if message.author == self.user:
            return

        content = message.content or ""
        is_dm = isinstance(message.channel, discord.DMChannel)

        if not content.strip() and not message.attachments:
            return

        # Download attachments
        attachment_paths: list[str] = []
        if message.attachments:
            attachment_paths = await self._download_attachments(message.attachments)

        # Voice memo transcription
        if getattr(message.flags, "voice", False) and attachment_paths:
            content = await self._transcribe_voice(
                attachment_paths[0], content,
                message.author.display_name,
            )

        inbound = InboundMessage(
            sender_id=str(message.author.id),
            sender_display_name=message.author.display_name,
            channel_id=str(message.channel.id),
            content=content,
            is_dm=is_dm,
            message_id=str(message.id),
            attachment_paths=attachment_paths,
        )

        try:
            await self._adapter.handle_message(inbound)
        except RoutingError:
            # Routing invariant violation — this is a structural bug,
            # not a transient failure. Let it propagate loudly.
            log.error(
                "Routing invariant violation for message from %s in %s",
                message.author, message.channel,
            )
            raise
        except Exception:
            log.exception(
                "Unexpected error handling message from %s in %s",
                message.author, message.channel,
            )

    async def _download_attachments(
        self, attachments: list[discord.Attachment],
    ) -> list[str]:
        download_dir = self._adapter._get_attachment_dir()
        paths: list[str] = []
        for att in attachments:
            # Sanitize filename: strip path components, replace unsafe chars
            safe_name = _sanitize_filename(att.filename)
            dest = download_dir / f"{uuid.uuid4().hex[:8]}-{safe_name}"
            try:
                await att.save(dest)
                paths.append(str(dest))
                log.info("Downloaded attachment: %s (%d bytes)", dest.name, att.size)
            except Exception:
                log.exception("Failed to download attachment %s", att.filename)
        return paths

    async def _transcribe_voice(
        self, audio_path: str, existing_content: str, sender_name: str,
    ) -> str:
        """Transcribe a voice memo attachment using Whisper STT.

        Returns augmented content with the transcript prepended. Falls
        back gracefully if the voice service is unavailable or fails.
        """
        creds_dir = self._adapter._discord_config.credentials_dir
        if not creds_dir:
            return self._voice_fallback(audio_path, existing_content, "no credentials configured")

        try:
            from voice.openai import WhisperSTT
        except ImportError:
            return self._voice_fallback(audio_path, existing_content, "transcription unavailable")

        log.info("Transcribing voice message from %s: %s", sender_name, audio_path)
        try:
            stt = WhisperSTT(Path(creds_dir).expanduser())
            transcript = await stt.transcribe(Path(audio_path))
        except Exception:
            log.exception("Voice transcription failed for %s", audio_path)
            return self._voice_fallback(audio_path, existing_content, "transcription failed")

        if transcript:
            prefix = f"[Voice message transcript \u2014 may contain errors]\n{transcript}"
            if existing_content.strip():
                return f"{prefix}\n\n{existing_content}"
            return prefix

        return self._voice_fallback(audio_path, existing_content, "transcription returned empty")

    @staticmethod
    def _voice_fallback(audio_path: str, existing_content: str, reason: str) -> str:
        """Build fallback content when voice transcription fails."""
        fallback = f"[Voice message received \u2014 {reason}. Audio saved at: {audio_path}]"
        if existing_content.strip():
            return f"{fallback}\n\n{existing_content}"
        return fallback


class DiscordAdapter:
    """Discord adapter for the Kiln daemon.

    Implements the PlatformAdapter protocol. Started by the daemon
    after the server is running; receives the daemon instance for
    access to management actions, event subscription, and query methods.
    """

    def __init__(self, config: dict[str, Any] | None = None):
        self._raw_config = config or {}
        self._discord_config = DiscordAdapterConfig.from_dict(self._raw_config)
        self._daemon: Any = None  # KilnDaemon, set on start()
        self._state_dir: Path | None = None
        self._event_handler = self._handle_event  # stable reference for add/remove

        # Live Discord client (created on start, None until then)
        self._client: _DiscordClient | None = None
        self._client_task: asyncio.Task | None = None
        self._ready_event: asyncio.Event | None = None

        # Adapter-local branch/channel thread mappings
        self._branch_threads: dict[str, int] = {}  # session_id -> discord thread_id
        self._channel_threads: dict[str, int] = {}  # channel_name -> discord thread_id

        # Reverse indexes (rebuilt on load/mutation)
        self._thread_to_session: dict[int, str] = {}  # thread_id -> session_id
        self._thread_to_channel: dict[int, str] = {}  # thread_id -> channel_name

        # Security challenge state (D3b) — non-None while a challenge is active
        # Shape: {future: Future, channel_id: str, message_ids: list[int]}
        self._security_challenge_state: dict | None = None

        # Pending permission requests (D3b) — keyed by session_id
        # Shape: {future: Future, message: discord.Message, embed: Embed, externally_resolved: bool}
        self._pending_permissions: dict[str, dict] = {}

        # Status display state (D3c)
        self._status_refresh_task: asyncio.Task | None = None
        self._status_refresh_signal: asyncio.Event | None = None
        self._status_message_id: int | None = None
        self._usage_cache: dict | None = None
        self._usage_cache_time: float = 0.0

    def _rebuild_reverse_indexes(self) -> None:
        """Rebuild reverse lookup dicts from forward mappings."""
        self._thread_to_session = {v: k for k, v in self._branch_threads.items()}
        self._thread_to_channel = {v: k for k, v in self._channel_threads.items()}

    @property
    def adapter_id(self) -> str:
        return "discord"

    @property
    def platform_name(self) -> str:
        return "discord"

    async def start(self, daemon: Any) -> None:
        """Start the adapter. Called by the daemon after server is running.

        Creates and connects the Discord client, waiting for on_ready
        before returning. Raises on login failure or timeout so the daemon
        surfaces the error honestly instead of silently running a dead client.
        """
        self._daemon = daemon
        self._state_dir = daemon.config.state_dir / "discord"
        self._state_dir.mkdir(parents=True, exist_ok=True)

        # Load persisted adapter state
        self._branch_threads = {
            k: int(v)
            for k, v in _load_json(self._state_dir / "branch-threads.json").items()
        }
        self._channel_threads = {
            k: int(v)
            for k, v in _load_json(self._state_dir / "channel-threads.json").items()
        }
        self._rebuild_reverse_indexes()

        # Resolve bot token and start Discord client (before registering
        # event handler — if client startup fails, we don't want a half-started
        # adapter wired into daemon events)
        token = self._resolve_token()
        if token:
            try:
                await self._start_client(token)
            except Exception:
                self._daemon = None
                raise

        # Subscribe to daemon events only after successful startup
        daemon.events.add_handler(self._event_handler)

        # Reconstruct bridge records from persisted channel threads
        for channel in self._channel_threads:
            await self._ensure_channel_bridge(channel)

        # Load persisted status message ID and start status loop
        self._status_message_id = self._load_status_message_id()
        await self._start_status_loop()

        log.info("Discord adapter started (state: %s)", self._state_dir)

    async def _start_client(self, token: str, timeout: float = 30.0) -> None:
        """Create, connect, and wait for the Discord client to be ready."""
        self._ready_event = asyncio.Event()
        self._client = _DiscordClient(self, self._ready_event)

        # Launch client.start() as a background task
        self._client_task = asyncio.create_task(
            self._client.start(token), name="discord-client",
        )

        # Wait for on_ready or failure — whichever comes first
        ready_waiter = asyncio.create_task(self._ready_event.wait())
        try:
            done, _ = await asyncio.wait(
                [self._client_task, ready_waiter],
                timeout=timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
        except Exception:
            ready_waiter.cancel()
            await self._cleanup_client()
            raise
        finally:
            # Always clean up the ready waiter if it's still pending
            if not ready_waiter.done():
                ready_waiter.cancel()

        # If client_task finished first, it crashed during login
        if self._client_task.done():
            exc = self._client_task.exception()
            await self._cleanup_client()
            if exc:
                raise RuntimeError(f"Discord client failed to connect: {exc}") from exc
            raise RuntimeError("Discord client exited unexpectedly during startup")

        # If neither finished, we timed out
        if not self._ready_event.is_set():
            await self._cleanup_client()
            raise RuntimeError(
                f"Discord client did not become ready within {timeout}s"
            )

        log.info("Discord client connected and ready")

    def _resolve_token(self) -> str | None:
        """Resolve bot token from config.

        Checks token_file first (path to a file containing the token),
        then falls back to an inline token value. Returns None if
        no token is configured (adapter runs without a live client).
        """
        # Primary: read from file
        token_file = self._raw_config.get("token_file")
        if token_file:
            path = Path(token_file).expanduser()
            if path.exists():
                token = path.read_text().strip()
                if token:
                    return token
                log.warning("Token file %s is empty", path)
            else:
                log.warning("Token file %s does not exist", path)

        # Fallback: inline token (for tests/dev)
        token = self._raw_config.get("token")
        if token:
            return token

        log.info("No Discord bot token configured — running without live client")
        return None

    async def stop(self) -> None:
        """Stop the adapter and clean up."""
        # Stop status refresh loop
        await self._stop_status_loop()

        # Stop Discord client
        await self._cleanup_client()

        if self._daemon:
            self._daemon.events.remove_handler(self._event_handler)

        # Persist adapter state
        if self._state_dir:
            _save_json(self._state_dir / "branch-threads.json", self._branch_threads)
            _save_json(self._state_dir / "channel-threads.json", self._channel_threads)

        self._daemon = None
        log.info("Discord adapter stopped")

    async def _cleanup_client(self) -> None:
        """Close the Discord client and cancel its task."""
        if self._client and not self._client.is_closed():
            await self._client.close()
        if self._client_task and not self._client_task.done():
            self._client_task.cancel()
            try:
                await self._client_task
            except (asyncio.CancelledError, Exception):
                pass
        self._client = None
        self._client_task = None
        self._ready_event = None

    async def send_user_message(
        self,
        user: str,
        summary: str,
        body: str,
        context: RequestContext | None = None,
    ) -> str:
        """Send a message to a Discord user.

        Routed here by the daemon when an agent calls send_user for a
        user whose default platform is Discord. The daemon has already
        resolved the user name to this adapter — we look up their
        Discord ID from the daemon's user config, not the adapter's
        inbound trust registry.
        """
        text = body or summary
        if not text:
            raise ValueError("Cannot send empty message")

        if not self._client:
            raise ValueError("No Discord client connected")

        # Resolve Discord user ID from daemon config
        discord_id = self._resolve_user_platform_id(user)
        if not discord_id:
            raise ValueError(
                f"No Discord platform ID for user '{user}' in daemon config"
            )

        try:
            discord_user = await self._client.fetch_user(int(discord_id))
            dm_channel = await discord_user.create_dm()
        except (discord.NotFound, discord.HTTPException) as e:
            raise ValueError(f"Could not open DM with Discord user {discord_id}: {e}")

        chunks = split_message(text)
        for chunk in chunks:
            await dm_channel.send(chunk)
        return f"Sent {len(chunks)} message(s) to {user}"

    def _resolve_user_platform_id(self, user_name: str) -> str | None:
        """Look up a daemon user's Discord ID from daemon config.

        This is the outbound resolution path — uses the daemon's
        external user registry, not the adapter's inbound trust map.
        """
        if not self._daemon:
            return None
        user_config = self._daemon.config.users.get(user_name)
        if not user_config:
            return None
        return user_config.platforms.get("discord")

    async def platform_op(
        self,
        action: str,
        args: dict[str, Any],
        context: RequestContext | None = None,
    ) -> dict[str, Any]:
        """Execute a Discord-specific operation requested by an agent.

        This is the agent-facing API surface — agents call platform_op
        through the daemon client, the daemon routes it here.
        """
        handlers = {
            "send": self._op_send,
            "read_history": self._op_read_history,
            "branch_post": self._op_branch_post,
            "thread_create": self._op_thread_create,
            "thread_archive": self._op_thread_archive,
            "list_channels": self._op_list_channels,
            "voice_send": self._op_voice_send,
            "delete": self._op_delete,
            "security_challenge": self._op_security_challenge,
            "permission_request": self._op_permission_request,
            "permission_resolve": self._op_permission_resolve,
        }
        handler = handlers.get(action)
        if not handler:
            raise ValueError(f"Unknown Discord platform op: '{action}'")
        return await handler(args, context)

    def validate_surface_ref(self, surface_ref: str) -> str:
        """Validate and canonicalize a Discord surface reference.

        Accepted forms:
            discord:user:<id>       — a user's DM surface
            discord:channel:<id>    — a Discord channel/thread

        Returns the canonical ref (unchanged if already canonical).
        Raises ValueError for malformed or unrecognized refs.
        """
        parts = surface_ref.split(":", 2)
        if len(parts) != 3 or parts[0] != "discord":
            raise ValueError(
                f"Invalid Discord surface ref: '{surface_ref}' "
                f"(expected discord:<type>:<id>)"
            )
        surface_type, surface_id = parts[1], parts[2]
        if surface_type not in ("user", "channel"):
            raise ValueError(
                f"Unknown Discord surface type: '{surface_type}' "
                f"(expected 'user' or 'channel')"
            )
        if not surface_id:
            raise ValueError("Surface ID cannot be empty")
        return surface_ref  # already canonical

    def supports(self, feature: str) -> bool:
        return feature in {
            "send_message", "read_history", "voice",
            "security_challenge", "permission",
            "branch_post", "threads",
        }

    # ------------------------------------------------------------------
    # Event handling (daemon → adapter)
    # ------------------------------------------------------------------

    async def _handle_event(self, event: proto.Message) -> None:
        """Route daemon events to the appropriate handler.

        This is the adapter's main event intake. The daemon event bus
        calls this for every event; the adapter classifies and routes
        to specific handler methods. Each handler category corresponds
        to a distinct concern — keep them separate.

        Categories:
            Channel outbound — Kiln channel messages rendered to Discord
            Session lifecycle — branch thread create/archive
            Channel lifecycle — channel thread create/archive
            Bridge lifecycle — bridge-level bookkeeping
            Status — status display updates
        """
        etype = event.type
        handler_name = self._EVENT_HANDLERS.get(etype)
        if handler_name:
            handler = getattr(self, handler_name)
            await handler(event)

    # --- Channel outbound (Kiln channel → Discord) ---

    async def _on_channel_message(self, event: proto.Message) -> None:
        """A message was published to a Kiln channel.

        If the channel has a bridge to Discord, format and send the
        message to the bridged Discord surface. Skip messages that
        originated from Discord (echo prevention).
        """
        # Echo prevention — don't send Discord-originated messages back
        if event.data.get("source") == "discord":
            return

        channel = event.data.get("channel", "")
        if not channel or not self._daemon:
            return

        # Auto-bridge if needed (handles publish-without-subscribe)
        await self._ensure_channel_bridge(channel)

        # Look up bridge for this channel
        bridges = self._daemon.state.bridges.by_source("channel", channel)
        discord_bridges = [b for b in bridges if b.adapter_id == "discord"]
        if not discord_bridges:
            return

        sender = event.data.get("sender", "unknown")
        body = event.data.get("body", "")
        summary = event.data.get("summary", "")

        formatted = format_outbound(sender, body, summary)
        if not formatted:
            return

        # Send to each bridged Discord surface
        for bridge in discord_bridges:
            chunks = split_message(formatted)
            for chunk in chunks:
                try:
                    await self._discord_post_to_surface(
                        bridge.platform_target, chunk,
                    )
                except Exception:
                    log.exception(
                        "Failed to post to bridge target %s for channel '%s'",
                        bridge.platform_target, channel,
                    )

    # --- Session lifecycle (branch threads) ---

    async def _on_session_live(self, event: proto.Message) -> None:
        """A session became live (discovered via tmux or first request).

        Create or reuse a branch thread in #branches for this session.
        Branch threads are one-to-one session bindings (adapter-local state),
        distinct from surface subscriptions and bridges.
        """
        session_id = event.data.get("session_id", "")
        agent_name = event.data.get("agent_name", "")
        if not session_id:
            return

        # Status refresh on any session connect, regardless of branch thread outcome
        self._signal_status_refresh()

        # Check if this session already has a branch thread (e.g. from resume)
        if session_id in self._branch_threads:
            log.debug(
                "Session %s already has branch thread %d",
                session_id, self._branch_threads[session_id],
            )
            return

        # Create a new branch thread
        branches_channel = self._discord_config.channels.get("branches")
        if not branches_channel:
            log.debug("No #branches channel configured, skipping branch thread for %s", session_id)
            return

        try:
            thread_id = await self._discord_create_thread(
                branches_channel, session_id,
                f"Session {session_id} ({agent_name})",
            )
            if thread_id:
                self._branch_threads[session_id] = thread_id
                self._rebuild_reverse_indexes()
                self._persist_state()
                log.info("Created branch thread %d for %s", thread_id, session_id)
        except Exception:
            log.exception("Failed to create branch thread for %s", session_id)

    async def _on_session_gone(self, event: proto.Message) -> None:
        """A session is no longer live (pruned by tmux reconciliation).

        Archive the branch thread. The thread mapping is kept in
        _branch_threads so the thread can be reused on resume.
        """
        session_id = event.data.get("session_id", "")
        if not session_id:
            return

        # Status refresh on any session disconnect, regardless of branch thread
        self._signal_status_refresh()

        thread_id = self._branch_threads.get(session_id)
        if not thread_id:
            return

        try:
            await self._discord_archive_thread(thread_id)
            log.info("Archived branch thread %d for %s", thread_id, session_id)
        except Exception:
            log.exception("Failed to archive branch thread for %s", session_id)

    async def _on_session_mode_changed(self, event: proto.Message) -> None:
        """A session's mode was changed. Refresh status display."""
        log.debug("Session mode changed: %s", event.data)
        self._signal_status_refresh()

    # --- Channel lifecycle (channel threads) ---

    async def _on_channel_subscribed(self, event: proto.Message) -> None:
        """A session subscribed to a Kiln channel.

        Auto-creates a Discord bridge (thread in #channels) if one
        doesn't already exist, so Kira can see channel activity.
        """
        channel = event.data.get("channel", "")
        if channel:
            await self._ensure_channel_bridge(channel)

    async def _on_channel_unsubscribed(self, event: proto.Message) -> None:
        """A session unsubscribed from a Kiln channel."""
        log.debug(
            "Channel unsubscribed: %s by %s",
            event.data.get("channel"), event.data.get("session_id"),
        )

    # --- Auto-bridge ---

    async def _ensure_channel_bridge(self, channel: str) -> None:
        """Ensure a Discord bridge exists for a Kiln channel.

        Creates a thread in #channels and registers a bridge record
        if one doesn't already exist. Idempotent.
        """
        if not self._daemon:
            return

        # Already bridged?
        existing = self._daemon.state.bridges.by_source("channel", channel)
        if any(b.adapter_id == "discord" for b in existing):
            return

        channels_channel = self._discord_config.channels.get("channels")
        if not channels_channel:
            log.debug("No #channels configured, skipping auto-bridge for '%s'", channel)
            return

        # Reuse existing thread if we have one (e.g. from a previous daemon run)
        thread_id = self._channel_threads.get(channel)
        if not thread_id:
            thread_id = await self._discord_create_thread(
                channels_channel, channel,
                f"Channel: {channel}",
            )
            if not thread_id:
                return
            self._channel_threads[channel] = thread_id
            self._rebuild_reverse_indexes()
            self._persist_state()

        bridge = BridgeRecord(
            bridge_id=f"auto-discord-channel-{channel}",
            source_kind="channel",
            source_name=channel,
            adapter_id="discord",
            platform_target=str(thread_id),
        )
        self._daemon.state.bridges.bind(bridge)
        log.info("Auto-bridged channel '%s' to Discord thread %d", channel, thread_id)

    # --- Bridge lifecycle (channel threads) ---

    async def _on_bridge_bound(self, event: proto.Message) -> None:
        """A bridge was created between a Kiln source and Discord.

        Creates a channel thread in #channels for the bridged Kiln channel.
        Channel threads are keyed by Kiln channel name in _channel_threads.
        """
        adapter_id = event.data.get("adapter_id", "")
        if adapter_id != "discord":
            return

        source_kind = event.data.get("source_kind", "")
        source_name = event.data.get("source_name", "")
        if source_kind != "channel" or not source_name:
            return

        # Check if thread already exists for this channel
        if source_name in self._channel_threads:
            log.debug("Channel thread already exists for '%s'", source_name)
            return

        channels_channel = self._discord_config.channels.get("channels")
        if not channels_channel:
            log.debug("No #channels channel configured, skipping thread for '%s'", source_name)
            return

        try:
            thread_id = await self._discord_create_thread(
                channels_channel, source_name,
                f"Bridge: {source_name}",
            )
            if thread_id:
                self._channel_threads[source_name] = thread_id
                self._rebuild_reverse_indexes()
                self._persist_state()
                log.info("Created channel thread %d for bridge '%s'", thread_id, source_name)
        except Exception:
            log.exception("Failed to create channel thread for '%s'", source_name)

    async def _on_bridge_unbound(self, event: proto.Message) -> None:
        """A bridge was removed.

        Archives the channel thread if one exists.
        """
        adapter_id = event.data.get("adapter_id", "")
        if adapter_id != "discord":
            return

        source_name = event.data.get("source_name", "")
        thread_id = self._channel_threads.get(source_name)
        if not thread_id:
            return

        try:
            await self._discord_archive_thread(thread_id)
            log.info("Archived channel thread %d for bridge '%s'", thread_id, source_name)
        except Exception:
            log.exception("Failed to archive channel thread for '%s'", source_name)

    # Event type → handler method name mapping.
    # Uses method name strings so getattr(self, name) picks up instance
    # overrides/patches, making event routing testable.
    _EVENT_HANDLERS: dict[str, str] = {
        proto.EVT_MESSAGE_CHANNEL: "_on_channel_message",
        proto.EVT_SESSION_LIVE: "_on_session_live",
        proto.EVT_SESSION_GONE: "_on_session_gone",
        proto.EVT_SESSION_MODE_CHANGED: "_on_session_mode_changed",
        proto.EVT_CHANNEL_SUBSCRIBED: "_on_channel_subscribed",
        proto.EVT_CHANNEL_UNSUBSCRIBED: "_on_channel_unsubscribed",
        proto.EVT_BRIDGE_BOUND: "_on_bridge_bound",
        proto.EVT_BRIDGE_UNBOUND: "_on_bridge_unbound",
    }

    # ------------------------------------------------------------------
    # Status display + presence (D3c)
    # ------------------------------------------------------------------

    def _signal_status_refresh(self) -> None:
        """Signal the status refresh loop to wake up.

        Safe to call from any event handler or command handler.
        The refresh loop debounces rapid signals.
        """
        if self._status_refresh_signal is not None:
            self._status_refresh_signal.set()

    async def _start_status_loop(self) -> None:
        """Start the status refresh background task.

        Called during start() after the client is ready. Only starts
        if a #status channel is configured.
        """
        status_channel_id = self._discord_config.channels.get("status")
        if not status_channel_id:
            log.info("No #status channel configured — status display disabled")
            return

        self._status_refresh_signal = asyncio.Event()
        self._status_refresh_signal.set()  # Trigger immediate first refresh
        self._status_refresh_task = asyncio.create_task(
            self._status_refresh_loop(status_channel_id),
        )

    async def _stop_status_loop(self) -> None:
        """Cancel the status refresh task. Called during stop()."""
        if self._status_refresh_task and not self._status_refresh_task.done():
            self._status_refresh_task.cancel()
            try:
                await self._status_refresh_task
            except asyncio.CancelledError:
                pass
        self._status_refresh_task = None
        self._status_refresh_signal = None

    async def _status_refresh_loop(self, status_channel_id: str) -> None:
        """Unified status refresh loop.

        Wakes on either:
        - periodic timer (STATUS_REFRESH_INTERVAL)
        - event signal (_status_refresh_signal) with debounce

        Each wake collects data and updates both the status embed and
        bot presence text.
        """
        import time as _time
        last_refresh = 0.0

        while True:
            # Wait for either timer or signal
            try:
                await asyncio.wait_for(
                    self._status_refresh_signal.wait(),
                    timeout=STATUS_REFRESH_INTERVAL,
                )
                # Signal received — clear it
                self._status_refresh_signal.clear()
            except asyncio.TimeoutError:
                pass  # Periodic wake

            # Debounce: skip if we refreshed very recently
            now = _time.monotonic()
            if now - last_refresh < STATUS_REFRESH_DEBOUNCE:
                continue
            last_refresh = now

            try:
                await self._do_status_refresh(status_channel_id)
            except Exception:
                log.exception("Status refresh failed")

    async def _do_status_refresh(self, status_channel_id: str) -> None:
        """Perform a single status refresh cycle.

        Collects data from daemon state and filesystem, builds embeds,
        and updates both the status message and bot presence.
        """
        agents = self._collect_daemon_status()
        self._enrich_with_home_decorators(agents)

        # Resolve canonical — check each unique agent home
        canonical_id = None
        seen_homes: set[str] = set()
        for a in agents:
            home = a.get("agent_home", "")
            if home and home not in seen_homes:
                seen_homes.add(home)
                cid = self._read_canonical(Path(home))
                if cid:
                    canonical_id = cid
                    break

        membership = self._get_conclave_membership()
        embeds = _build_status_embeds(agents, canonical_id=canonical_id, membership=membership)

        from datetime import datetime
        from zoneinfo import ZoneInfo
        now = datetime.now(ZoneInfo("America/Toronto")).strftime("%I:%M:%S %p EST")
        content = f"**Agent Status** \u2014 last updated {now}"

        usage_data = await self._collect_usage_data()
        if usage_data:
            for line in _format_usage_lines(usage_data):
                content += f"\n{line}"

        # Update status message (edit-or-create)
        await self._update_status_message(status_channel_id, content, embeds)

        # Update bot presence
        presence_text = _build_presence_text(agents, canonical_id=canonical_id)
        await self._update_presence(presence_text)

    def _collect_daemon_status(self) -> list[dict]:
        """Collect session data from daemon presence registry.

        Returns a list of agent dicts with daemon-truth fields only.
        Home decorators are added separately by _enrich_with_home_decorators.
        """
        sessions = self._daemon.state.presence.all_sessions()
        agents = []
        for s in sessions:
            agents.append({
                "id": s.session_id,
                "agent_name": s.agent_name,
                "agent_home": s.agent_home,
                "uptime": _format_uptime(s.first_seen_at.isoformat()),
            })
        agents.sort(key=lambda a: a["id"])
        return agents

    def _read_canonical(self, agent_home: Path) -> str | None:
        """Read canonical session ID from agent home. Soft-fail."""
        if not agent_home:
            return None
        path = Path(agent_home) / "state" / "canonical"
        try:
            text = path.read_text().strip()
            return text or None
        except OSError:
            return None

    def _enrich_with_home_decorators(self, agents: list[dict]) -> None:
        """Add filesystem-derived decorator fields to agent dicts.

        Reads from agent home directories. All reads are soft-fail:
        missing files or errors produce graceful defaults, never exceptions.

        This is presentation-only — no side effects, no repair behavior.
        """
        for agent in agents:
            home = Path(agent.get("agent_home", ""))
            session_id = agent["id"]

            # Mode — from session config YAML
            agent["mode"] = self._read_session_mode(home, session_id)

            # Context% ��� from CC conversation JSONL
            usage = self._read_context_usage(home, session_id)
            agent["context"] = _format_context(usage)
            agent["context_pct"] = int(usage[0] / usage[1] * 100) if usage else None

            # Plan — from plans directory
            agent["plan"] = self._read_plan(home, session_id)

            # Inbox count
            agent["inbox"] = self._count_inbox(home, session_id)

    @staticmethod
    def _read_session_mode(agent_home: Path, session_id: str) -> str:
        """Read session mode from live session-config YAML. Soft-fail."""
        if not agent_home:
            return ""
        config_path = agent_home / "state" / f"session-config-{session_id}.yml"
        if not config_path.exists():
            return ""
        try:
            import yaml
            data = yaml.safe_load(config_path.read_text()) or {}
            return data.get("mode", "")
        except Exception:
            return ""

    @staticmethod
    def _read_context_usage(agent_home: Path, session_id: str) -> tuple[int, int] | None:
        """Read context token usage from CC conversation JSONL. Soft-fail."""
        if not agent_home:
            return None

        # Look up session UUID from session registry
        registry_path = agent_home / "logs" / "session-registry.json"
        if not registry_path.exists():
            return None
        try:
            registry = json.loads(registry_path.read_text())
            entry = registry.get(session_id, {})
            session_uuid = entry.get("session_uuid")
            if not session_uuid:
                return None
        except (json.JSONDecodeError, OSError):
            return None

        # Find JSONL file
        claude_projects = Path.home() / ".claude" / "projects"
        encoded_cwd = str(Path(agent_home).resolve()).replace("/", "-").replace(".", "-")
        jsonl_path = claude_projects / encoded_cwd / f"{session_uuid}.jsonl"
        if not jsonl_path.exists():
            return None

        try:
            tail_size = 32 * 1024
            with open(jsonl_path, "rb") as f:
                f.seek(0, 2)
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

    @staticmethod
    def _read_plan(agent_home: Path, session_id: str) -> dict | None:
        """Read session plan from plans directory. Soft-fail."""
        if not agent_home:
            return None
        path = agent_home / "plans" / f"{session_id}.yml"
        if not path.exists():
            return None
        try:
            import yaml
            return yaml.safe_load(path.read_text())
        except Exception:
            return None

    @staticmethod
    def _count_inbox(agent_home: Path, session_id: str) -> int:
        """Count unread inbox messages for a session. Soft-fail."""
        if not agent_home:
            return 0
        inbox = Path(agent_home) / "inbox" / session_id
        if not inbox.exists():
            return 0
        count = 0
        try:
            for f in inbox.iterdir():
                if f.suffix == ".md" and not f.with_suffix(".read").exists():
                    count += 1
        except OSError:
            pass
        return count

    def _get_conclave_membership(self) -> dict[str, dict]:
        """Parse conclave briefings for display grouping. Soft-fail.

        Returns {session_id: {"conclave": name, "role": role}}.
        Reads from all known agent homes with connected sessions.
        """
        import re
        pattern = re.compile(r"\*\*(Facilitator|Collaborator):\*\*\s+(\S+)")
        membership: dict[str, dict] = {}

        seen_homes: set[str] = set()
        for s in self._daemon.state.presence.all_sessions():
            if s.agent_home in seen_homes:
                continue
            seen_homes.add(s.agent_home)

            conclaves_dir = Path(s.agent_home) / "conclaves"
            if not conclaves_dir.exists():
                continue

            for briefing in conclaves_dir.glob("*/briefing.md"):
                name = briefing.parent.name
                try:
                    text = briefing.read_text()
                except OSError:
                    continue
                in_members = False
                for line in text.splitlines():
                    if line.strip() == "## Members":
                        in_members = True
                        continue
                    if in_members:
                        if line.startswith("## "):
                            break
                        m = pattern.search(line)
                        if m:
                            membership[m.group(2)] = {
                                "conclave": name, "role": m.group(1).lower(),
                            }
        return membership

    async def _collect_usage_data(self) -> dict | None:
        """Fetch subscription usage data, cached.

        Uses the kiln.util.usage library for direct API access.
        Soft-fail: returns cached data or None on error.
        """
        import time as _time
        now = _time.monotonic()
        if self._usage_cache and (now - self._usage_cache_time) < USAGE_CACHE_TTL:
            return self._usage_cache

        try:
            from ...util.usage import get_subscription_usage_async
            data = await get_subscription_usage_async()
            if data:
                self._usage_cache = data
                self._usage_cache_time = now
                return data
        except Exception:
            log.debug("Usage data fetch failed, using cache")
        return self._usage_cache

    async def _update_status_message(
        self, channel_id: str, content: str, embeds: list[discord.Embed],
    ) -> None:
        """Edit-or-create the status message in the status channel.

        Persists the message ID so we edit the same message across restarts.
        """
        if not self._client:
            return

        channel = self._client.get_channel(int(channel_id))
        if not channel:
            try:
                channel = await self._client.fetch_channel(int(channel_id))
            except discord.NotFound:
                log.warning("Status channel %s not found", channel_id)
                return

        # Try editing existing message
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

        # Create new message
        msg = await channel.send(content=content, embeds=embeds)
        self._status_message_id = msg.id
        self._persist_status_message_id()

        try:
            await msg.pin()
        except (discord.Forbidden, discord.HTTPException):
            pass

    def _persist_status_message_id(self) -> None:
        """Save the status message ID to disk for persistence across restarts."""
        if not self._state_dir:
            return
        path = self._state_dir / "status-message-id"
        try:
            path.write_text(str(self._status_message_id or ""))
        except OSError:
            log.debug("Failed to persist status message ID")

    def _load_status_message_id(self) -> int | None:
        """Load the persisted status message ID from disk."""
        if not self._state_dir:
            return None
        path = self._state_dir / "status-message-id"
        if not path.exists():
            return None
        try:
            text = path.read_text().strip()
            return int(text) if text else None
        except (ValueError, OSError):
            return None

    async def _update_presence(self, text: str) -> None:
        """Update bot presence/activity text."""
        if not self._client:
            return
        try:
            activity = discord.Activity(
                type=discord.ActivityType.watching,
                name=text,
            )
            await asyncio.wait_for(
                self._client.change_presence(activity=activity),
                timeout=15,
            )
        except asyncio.TimeoutError:
            log.warning("Presence update timed out")
        except Exception:
            log.exception("Presence update error")

    # ------------------------------------------------------------------
    # Inbound message handling (Discord → daemon)
    # ------------------------------------------------------------------

    async def handle_message(self, msg: InboundMessage) -> RouteDecision | None:
        """Main inbound message handler.

        Called by the discord.py on_message callback after extracting
        raw Discord data into an InboundMessage. This method owns the
        full routing pipeline: access control, classification, delivery.

        Returns the RouteDecision for observability/testing, or None
        if the message was dropped (access denied, unrouted, or
        consumed by a pre-routing intercept like control commands).
        """
        if not self._daemon:
            log.warning("handle_message called before adapter started")
            return None

        # --- Security challenge intercept (before access control) ---
        # Active challenges consume messages from the security channel
        # regardless of normal access policy — this is a recovery/auth
        # flow, not ordinary routed traffic.
        if self._security_challenge_state is not None:
            if self._try_security_intercept(msg):
                return None

        # --- Access control ---
        if not self._check_access(msg):
            return None

        # --- Identity resolution ---
        sender_name, trust = self._discord_config.resolve_user(
            msg.sender_id, msg.sender_display_name,
        )

        # --- Pre-routing intercepts ---
        if self._is_control_channel(msg.channel_id):
            await self._handle_control_message(msg, sender_name, trust)
            return None

        # --- Classify ---
        decision = self._classify_message(msg)
        if decision is None:
            log.debug(
                "Unrouted message from %s in channel %s",
                msg.sender_id, msg.channel_id,
            )
            return None

        # --- Deliver ---
        if decision.bucket == RouteBucket.BRANCH:
            platform_msg = self._build_platform_message(
                msg, sender_name, trust,
                channel_desc=f"branch:{decision.session_id}",
            )
            await self._daemon.deliver_platform_message(
                decision.session_id, platform_msg,
            )

        elif decision.bucket == RouteBucket.BRIDGE:
            await self._daemon.publish_to_channel(
                decision.channel_name, sender_name,
                "", msg.content, source="discord",
            )

        elif decision.bucket == RouteBucket.SURFACE:
            platform_msg = self._build_platform_message(
                msg, sender_name, trust,
            )
            await self._daemon.deliver_to_surface_subscribers(
                decision.surface_ref, platform_msg,
            )

        log.info(
            "Routed %s message from %s → %s",
            decision.bucket.value, sender_name,
            decision.session_id or decision.channel_name or decision.surface_ref,
        )
        return decision

    def _classify_message(self, msg: InboundMessage) -> RouteDecision | None:
        """Classify an inbound message to exactly one route bucket.

        Returns None if no route matches (unrouted).
        Raises RoutingError if multiple routes match.
        """
        matches: list[RouteDecision] = []
        channel_int = int(msg.channel_id) if msg.channel_id.isdigit() else 0

        # Branch thread → session-bound
        session_id = self._thread_to_session.get(channel_int)
        if session_id:
            matches.append(RouteDecision(
                bucket=RouteBucket.BRANCH,
                session_id=session_id,
            ))

        # Channel thread → bridge
        channel_name = self._thread_to_channel.get(channel_int)
        if channel_name:
            matches.append(RouteDecision(
                bucket=RouteBucket.BRIDGE,
                channel_name=channel_name,
            ))

        # Surface subscription
        surface_ref = self._build_surface_ref(msg)
        if self._daemon and self._daemon.state.surfaces.subscribers(surface_ref):
            matches.append(RouteDecision(
                bucket=RouteBucket.SURFACE,
                surface_ref=surface_ref,
            ))

        if len(matches) > 1:
            bucket_names = [m.bucket.value for m in matches]
            raise RoutingError(
                f"Message in channel {msg.channel_id} matched {len(matches)} "
                f"route buckets: {bucket_names}. This is a routing invariant "
                f"violation — check for overlapping thread mappings and "
                f"surface subscriptions."
            )

        return matches[0] if matches else None

    def _check_access(self, msg: InboundMessage) -> bool:
        """Check if the sender is allowed to interact on this surface."""
        policy = (
            self._discord_config.dm_access if msg.is_dm
            else self._discord_config.channel_access
        )
        if not policy.is_allowed(msg.sender_id):
            log.debug(
                "Blocked message from %s — access denied (%s)",
                msg.sender_id, "dm" if msg.is_dm else "channel",
            )
            return False
        return True

    def _is_control_channel(self, channel_id: str) -> bool:
        """Check if a channel ID is the configured control channel."""
        control_id = self._discord_config.channels.get("control")
        return bool(control_id and channel_id == control_id)

    def _try_security_intercept(self, msg: InboundMessage) -> bool:
        """Check if *msg* is a response to an active security challenge.

        If the message is in the challenge channel and the challenge future
        is still pending, resolves the future and returns True (consumed).
        Otherwise returns False to let normal routing proceed.
        """
        state = self._security_challenge_state
        if state is None:
            return False
        if msg.channel_id != str(state["channel_id"]):
            return False

        # Track message for cleanup
        try:
            state["message_ids"].append(int(msg.message_id))
        except (ValueError, TypeError):
            pass

        future: asyncio.Future = state["future"]
        if not future.done():
            future.set_result((msg.content, msg.sender_id, msg.message_id))
        return True

    def _build_owner_mentions(self) -> str | None:
        """Build a Discord @mention string for all full-trust users."""
        mentions = []
        for uid, entry in self._discord_config.users.items():
            trust = entry.get("max_trust") or entry.get("trust")
            if trust == "full":
                mentions.append(f"<@{uid}>")
        return " ".join(mentions) if mentions else None

    def _build_surface_ref(self, msg: InboundMessage) -> str:
        """Build the canonical surface ref for routing lookups."""
        if msg.is_dm:
            return f"discord:user:{msg.sender_id}"
        return f"discord:channel:{msg.channel_id}"

    def _build_platform_message(
        self,
        msg: InboundMessage,
        sender_name: str,
        trust: str,
        channel_desc: str = "",
    ) -> PlatformMessage:
        """Build a PlatformMessage for daemon delivery."""
        if not channel_desc:
            channel_desc = "dm" if msg.is_dm else f"channel:{msg.channel_id}"
        return PlatformMessage(
            sender_name=sender_name,
            sender_platform_id=msg.sender_id,
            platform="discord",
            content=msg.content,
            trust=trust,
            channel_desc=channel_desc,
            channel_id=msg.channel_id,
            attachment_paths=msg.attachment_paths or None,
        )

    async def _handle_control_message(
        self,
        msg: InboundMessage,
        sender_name: str,
        trust: str,
    ) -> None:
        """Handle a message in the control channel.

        Control commands require full trust. The adapter parses and
        renders; all execution goes through the daemon management API.
        """
        if trust != "full":
            log.warning(
                "Control command from %s (%s) denied — requires full trust",
                sender_name, msg.sender_id,
            )
            return

        text = msg.content.strip()
        if not text:
            return

        parts = text.split()
        cmd = parts[0].lower()

        try:
            if cmd == "help":
                await self._cmd_help(msg)
            elif cmd == "mode":
                await self._cmd_mode(msg, parts[1:], sender_name)
            elif cmd == "spawn":
                await self._cmd_spawn(msg, text[len("spawn"):].strip(), sender_name)
            elif cmd == "resume":
                await self._cmd_resume(msg, parts[1:], sender_name)
            elif cmd == "kill":
                await self._cmd_kill(msg, parts[1:], sender_name)
            elif cmd == "interrupt":
                await self._cmd_interrupt(msg, parts[1:], sender_name)
            elif cmd == "show":
                await self._cmd_show(msg, parts[1:])
            else:
                await self._control_respond(
                    msg, f"Unknown command: `{cmd}`. Try `help`.",
                )
        except Exception as e:
            log.exception("Control command error: %s", text)
            await self._control_respond(msg, f"Error: {e}")

    # ------------------------------------------------------------------
    # Control command implementations
    # ------------------------------------------------------------------

    async def _control_respond(
        self,
        msg: InboundMessage,
        content: str,
    ) -> None:
        """Post a response in the control channel.

        Uses reply-threading when message_id is available.
        """
        if not self._client:
            log.warning("_control_respond called without live client")
            return

        channel = self._client.get_channel(int(msg.channel_id))
        if not channel:
            try:
                channel = await self._client.fetch_channel(int(msg.channel_id))
            except discord.NotFound:
                log.warning("Control channel %s not found", msg.channel_id)
                return

        # Reply-thread to the original message when possible
        reference = None
        if msg.message_id:
            reference = discord.MessageReference(
                message_id=int(msg.message_id),
                channel_id=int(msg.channel_id),
            )

        chunks = split_message(content)
        for i, chunk in enumerate(chunks):
            await channel.send(
                chunk,
                reference=reference if i == 0 else None,
            )

    async def _cmd_help(self, msg: InboundMessage) -> None:
        await self._control_respond(
            msg,
            "**Commands:**\n"
            "`spawn <agent> [instructions]` — launch a new session\n"
            "`kill <session-id>` — kill a session (immediate)\n"
            "`interrupt <session-id>` — send ESC to unstick a session\n"
            "`resume <session-id>` — resume a previous session\n"
            "`show <session-id>` — capture current terminal pane\n"
            "`mode <session-id> <mode>` — change permission mode "
            "(safe, supervised, yolo)\n"
            "`help` — this message",
        )

    async def _cmd_mode(
        self, msg: InboundMessage, args: list[str], sender_name: str,
    ) -> None:
        if len(args) != 2:
            await self._control_respond(msg, "Usage: `mode <session-id> <mode>`")
            return

        session_id, mode_str = args[0], args[1].lower()

        if mode_str == "trusted":
            await self._control_respond(msg, "Trusted mode is TUI-only.")
            return

        result = await self._daemon.management.set_session_mode(
            session_id, mode_str, requested_by=sender_name,
        )
        icon = "\u2705" if result.success else "\u274c"
        await self._control_respond(msg, f"{icon} {result.message}")

        if result.success:
            self._signal_status_refresh()

    async def _cmd_spawn(
        self, msg: InboundMessage, instructions: str, sender_name: str,
    ) -> None:
        # Parse: spawn <agent> [instructions]
        parts = instructions.split(None, 1)
        if not parts:
            await self._control_respond(msg, "Usage: `spawn <agent> [instructions]`")
            return

        agent = parts[0].lower()
        prompt = parts[1] if len(parts) > 1 else None

        # Validate agent name against known agents
        known_agents = self._get_known_agents()
        if agent not in known_agents:
            names = ", ".join(sorted(known_agents)) if known_agents else "(none)"
            await self._control_respond(
                msg, f"Unknown agent: `{agent}`. Known agents: {names}",
            )
            return

        result = await self._daemon.management.spawn_session(
            agent, prompt=prompt, requested_by=sender_name,
        )
        icon = "\u2705" if result.success else "\u274c"
        await self._control_respond(msg, f"{icon} {result.message}")

    async def _cmd_resume(
        self, msg: InboundMessage, args: list[str], sender_name: str,
    ) -> None:
        if not args:
            await self._control_respond(msg, "Usage: `resume <session-id>`")
            return

        session_id = args[0]

        # Extract agent name from session ID prefix
        agent = session_id.split("-")[0] if "-" in session_id else ""
        if not agent:
            await self._control_respond(
                msg, f"Cannot determine agent from session ID: `{session_id}`",
            )
            return

        result = await self._daemon.management.resume_session(
            agent, session_id, requested_by=sender_name,
        )
        icon = "\u2705" if result.success else "\u274c"
        await self._control_respond(msg, f"{icon} {result.message}")

    async def _cmd_kill(
        self, msg: InboundMessage, args: list[str], sender_name: str,
    ) -> None:
        if not args:
            await self._control_respond(msg, "Usage: `kill <session-id>`")
            return

        result = await self._daemon.management.stop_session(
            args[0], requested_by=sender_name,
        )
        icon = "\u2705" if result.success else "\u274c"
        await self._control_respond(msg, f"{icon} {result.message}")

    async def _cmd_interrupt(
        self, msg: InboundMessage, args: list[str], sender_name: str,
    ) -> None:
        if not args:
            await self._control_respond(msg, "Usage: `interrupt <session-id>`")
            return

        result = await self._daemon.management.interrupt_session(
            args[0], requested_by=sender_name,
        )
        icon = "\u2705" if result.success else "\u274c"
        await self._control_respond(msg, f"{icon} {result.message}")

    async def _cmd_show(
        self, msg: InboundMessage, args: list[str],
    ) -> None:
        if not args:
            await self._control_respond(msg, "Usage: `show <session-id>`")
            return

        session_id = args[0]
        result = await self._daemon.management.capture_session(session_id)
        if not result.success:
            await self._control_respond(msg, f"\u274c {result.message}")
            return

        # Read mode from session config for header
        mode = "unknown"
        config_path = self._daemon.management._session_config_path(session_id)
        if config_path and config_path.exists():
            try:
                import yaml
                data = yaml.safe_load(config_path.read_text()) or {}
                mode = data.get("mode", "unknown")
            except Exception:
                pass

        header = f"**{session_id}** | mode: `{mode}`\n"
        content = result.message

        # Trim to fit Discord's message limit
        max_content = DISCORD_MAX_LENGTH - len(header) - len("```\n\n```")
        if len(content) > max_content:
            content = content[-max_content:]
            newline_pos = content.find("\n")
            if newline_pos != -1:
                content = content[newline_pos + 1:]

        await self._control_respond(
            msg, f"{header}```\n{content}\n```" if content else f"{header}*(empty pane)*",
        )

    def _get_known_agents(self) -> set[str]:
        """Return the set of known agent names from the daemon's registry."""
        from ..config import load_agents_registry
        registry = load_agents_registry(self._daemon.config.agents_registry)
        return set(registry.keys())

    # ------------------------------------------------------------------
    # State persistence helper
    # ------------------------------------------------------------------

    def _persist_state(self) -> None:
        """Persist adapter state to disk (branch/channel thread mappings)."""
        if self._state_dir:
            _save_json(self._state_dir / "branch-threads.json", self._branch_threads)
            _save_json(self._state_dir / "channel-threads.json", self._channel_threads)

    # ------------------------------------------------------------------
    # Target resolution
    # ------------------------------------------------------------------

    async def _resolve_target(self, target: str) -> discord.abc.Messageable | None:
        """Resolve a target string to a Discord messageable.

        Accepted target forms:
            #name       — named channel from config
            @name       — user by name (from config users registry)
            @12345      — user by Discord ID
            12345       — channel/thread by Discord ID

        Returns None if the target can't be resolved or no client.
        """
        if not self._client:
            return None

        # Named channel from config
        clean = target.lstrip("#")
        if clean != target and clean in self._discord_config.channels:
            channel_id = int(self._discord_config.channels[clean])
            ch = self._client.get_channel(channel_id)
            if ch:
                return ch
            try:
                return await self._client.fetch_channel(channel_id)
            except discord.NotFound:
                return None

        # User target (@name or @id)
        if target.startswith("@"):
            user_ref = target[1:]
            user_id: str | None = None
            if user_ref.isdigit():
                user_id = user_ref
            else:
                # Reverse lookup by name in users registry
                for uid, entry in self._discord_config.users.items():
                    if entry.get("name", "").lower() == user_ref.lower():
                        user_id = uid
                        break
            if user_id:
                try:
                    user = await self._client.fetch_user(int(user_id))
                    if user:
                        return await user.create_dm()
                except discord.NotFound:
                    return None
            return None

        # Numeric channel/thread ID
        if target.isdigit():
            ch = self._client.get_channel(int(target))
            if ch:
                return ch
            try:
                return await self._client.fetch_channel(int(target))
            except discord.NotFound:
                return None

        # Fall back to guild channel name match
        if self._discord_config.guild_id:
            guild = self._client.get_guild(int(self._discord_config.guild_id))
            if guild:
                for ch in guild.text_channels:
                    if ch.name == clean:
                        return ch

        return None

    # ------------------------------------------------------------------
    # Discord API methods — thin async boundary for actual Discord calls
    #
    # These are the only methods that need a live discord.py client.
    # Everything above is logic/routing that can be tested without one.
    # ------------------------------------------------------------------

    def _get_attachment_dir(self) -> Path:
        """Return (and create) the directory for downloaded attachments."""
        if self._state_dir:
            d = self._state_dir / "attachments"
        else:
            d = Path("/tmp/kiln-discord-attachments")
        d.mkdir(parents=True, exist_ok=True)
        return d

    async def _discord_post_to_surface(self, surface_id: str, content: str) -> None:
        """Post a message to a Discord surface (channel or thread)."""
        if not self._client:
            log.warning("_discord_post_to_surface called without live client")
            return
        channel = self._client.get_channel(int(surface_id))
        if not channel:
            try:
                channel = await self._client.fetch_channel(int(surface_id))
            except discord.NotFound:
                log.warning("Discord channel %s not found", surface_id)
                return
        await channel.send(content)

    async def _discord_create_thread(
        self, channel_id: str, name: str, initial_message: str = "",
    ) -> int | None:
        """Create a thread in a Discord channel.

        Returns the thread ID, or None if creation failed.
        """
        if not self._client:
            log.warning("_discord_create_thread called without live client")
            return None
        channel = self._client.get_channel(int(channel_id))
        if not channel:
            try:
                channel = await self._client.fetch_channel(int(channel_id))
            except discord.NotFound:
                log.warning("Discord channel %s not found", channel_id)
                return None
        thread = await channel.create_thread(
            name=name,
            type=discord.ChannelType.public_thread,
        )
        if initial_message:
            await thread.send(initial_message)
        return thread.id

    async def _discord_archive_thread(self, thread_id: int) -> None:
        """Archive a Discord thread."""
        if not self._client:
            log.warning("_discord_archive_thread called without live client")
            return
        thread = self._client.get_channel(thread_id)
        if not thread:
            try:
                thread = await self._client.fetch_channel(thread_id)
            except discord.NotFound:
                log.warning("Discord thread %d not found", thread_id)
                return
        await thread.edit(archived=True)

    # ------------------------------------------------------------------
    # Platform ops (D2)
    # ------------------------------------------------------------------

    async def _op_send(self, args: dict, ctx: RequestContext | None) -> dict:
        """Send a message to a Discord target.

        Args:
            target: channel name (#general), user (@name), or numeric ID
            content: message text
            thread: optional thread name (find-or-create within target)
        """
        target = args.get("target", "")
        content = args.get("content", "")
        if not target or not content:
            return {"ok": False, "error": "target and content are required"}

        channel = await self._resolve_target(target)
        if not channel:
            return {"ok": False, "error": f"Could not resolve target: {target}"}

        # Optional thread within channel
        thread_name = args.get("thread")
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

    async def _op_read_history(self, args: dict, ctx: RequestContext | None) -> dict:
        """Read recent message history from a Discord target."""
        target = args.get("target", "")
        limit = min(int(args.get("limit", 20)), 100)
        if not target:
            return {"ok": False, "error": "target is required"}

        channel = await self._resolve_target(target)
        if not channel:
            return {"ok": False, "error": f"Could not resolve target: {target}"}

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
        return {"ok": True, "messages": messages}

    async def _op_branch_post(self, args: dict, ctx: RequestContext | None) -> dict:
        """Post a message to a session's branch thread."""
        session_id = args.get("session_id", "")
        content = args.get("content", "")
        if not session_id or not content:
            return {"ok": False, "error": "session_id and content are required"}

        thread_id = self._branch_threads.get(session_id)
        if not thread_id:
            return {"ok": False, "error": f"No branch thread for {session_id}"}

        if not self._client:
            return {"ok": False, "error": "No Discord client connected"}

        thread = self._client.get_channel(thread_id)
        if not thread:
            try:
                thread = await self._client.fetch_channel(thread_id)
            except discord.NotFound:
                return {"ok": False, "error": f"Branch thread {thread_id} not found"}

        chunks = split_message(content)
        for chunk in chunks:
            await thread.send(chunk)
        return {"ok": True}

    async def _op_thread_create(self, args: dict, ctx: RequestContext | None) -> dict:
        """Create a thread in a Discord channel."""
        channel_target = args.get("channel", "")
        name = args.get("name", "")
        if not channel_target or not name:
            return {"ok": False, "error": "channel and name are required"}

        channel = await self._resolve_target(channel_target)
        if not channel or not hasattr(channel, "create_thread"):
            return {"ok": False, "error": f"Cannot create thread in {channel_target}"}

        thread = await channel.create_thread(
            name=name, type=discord.ChannelType.public_thread,
        )
        message = args.get("message", "")
        if message:
            await thread.send(message)
        return {"ok": True, "thread_id": str(thread.id), "name": name}

    async def _op_thread_archive(self, args: dict, ctx: RequestContext | None) -> dict:
        """Archive a thread by name within a channel."""
        channel_target = args.get("channel", "")
        name = args.get("name", "")
        if not channel_target or not name:
            return {"ok": False, "error": "channel and name are required"}

        channel = await self._resolve_target(channel_target)
        if not channel or not hasattr(channel, "threads"):
            return {"ok": False, "error": f"Cannot access threads in {channel_target}"}

        for thread in channel.threads:
            if thread.name == name:
                await thread.edit(archived=True)
                return {"ok": True}

        return {"ok": False, "error": f"Thread '{name}' not found"}

    async def _op_list_channels(self, args: dict, ctx: RequestContext | None) -> dict:
        """List text channels in the configured guild."""
        if not self._client or not self._discord_config.guild_id:
            return {"ok": False, "error": "No client or guild configured"}

        guild = self._client.get_guild(int(self._discord_config.guild_id))
        if not guild:
            return {"ok": False, "error": "Guild not found in cache"}

        channels = [
            {
                "name": ch.name,
                "id": str(ch.id),
                "category": ch.category.name if ch.category else None,
            }
            for ch in guild.text_channels
        ]
        return {"ok": True, "channels": channels}

    async def _op_delete(self, args: dict, ctx: RequestContext | None) -> dict:
        """Delete a message by ID in a target channel."""
        target = args.get("target", "")
        message_id = args.get("message_id", "")
        if not target or not message_id:
            return {"ok": False, "error": "target and message_id are required"}

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

    async def _find_or_create_thread(
        self, channel: Any, name: str,
    ) -> Any | None:
        """Find a thread by name in a channel, or create one."""
        for thread in channel.threads:
            if thread.name == name:
                return thread
        async for thread in channel.archived_threads():
            if thread.name == name:
                await thread.edit(archived=False)
                return thread
        return await channel.create_thread(
            name=name, type=discord.ChannelType.public_thread,
        )

    # ------------------------------------------------------------------
    # Platform op stubs (D3 — stateful/UX-heavy)
    # ------------------------------------------------------------------

    async def _op_voice_send(self, args: dict, ctx: RequestContext | None) -> dict:
        """Send a TTS voice message to a Discord channel.

        Args:
            target: channel name (#general), user (@name), or numeric ID
            text: text to synthesize as speech
            voice: TTS voice name (optional, falls back to config default)
            instructions: TTS voice instructions (optional, falls back to config)
        """
        target = args.get("target", "")
        text = args.get("text", "")
        if not target or not text:
            return {"ok": False, "error": "target and text are required"}

        creds_dir = self._discord_config.credentials_dir
        if not creds_dir:
            return {"ok": False, "error": "Voice not configured (no credentials_dir)"}
        creds_path = Path(creds_dir).expanduser()

        try:
            from voice.openai import generate_speech
            from voice.discord import send_voice_message
        except ImportError:
            return {"ok": False, "error": "Voice service not available (import failed)"}

        channel = await self._resolve_target(target)
        if not channel:
            return {"ok": False, "error": f"Could not resolve target: {target}"}

        voice = args.get("voice") or self._discord_config.voice_default or None
        instructions = args.get("instructions") or self._discord_config.voice_instructions or None

        audio_path = Path(tempfile.mktemp(suffix=".ogg"))
        try:
            tts_kwargs: dict[str, Any] = {"agent_home": creds_path}
            if voice:
                tts_kwargs["voice"] = voice
            if instructions:
                tts_kwargs["instructions"] = instructions

            result = await generate_speech(text, audio_path, **tts_kwargs)
            if not result:
                return {"ok": False, "error": "TTS generation failed"}

            ok = await send_voice_message(
                str(channel.id), audio_path, agent_home=creds_path,
            )
            return {"ok": ok}
        finally:
            audio_path.unlink(missing_ok=True)

    async def _op_security_challenge(self, args: dict, ctx: RequestContext | None) -> dict:
        """Run an OTP security challenge via #security channel.

        Delegates core logic to ``run_security_challenge()`` in
        ``kiln.daemon.security``; this method just resolves the Discord
        channel and provides the transport.
        """
        if not self._client:
            return {"result": "error", "error": "not connected"}

        channel = await self._resolve_target("#security")
        if not channel:
            return {"result": "error", "error": "no #security channel found"}

        from ..security import run_security_challenge

        transport = _DiscordChallengeTransport(self, channel)
        try:
            return await run_security_challenge(
                transport,
                reason=args.get("reason") or "Unspecified",
                passwords=args.get("passwords", []),
                timeout=args.get("timeout", 60),
                max_attempts=args.get("max_attempts", 2),
            )
        except Exception as e:
            log.exception("Security challenge transport error")
            return {"result": "error", "error": str(e)}

    async def _op_permission_request(self, args: dict, ctx: RequestContext | None) -> dict:
        """Show a permission approval prompt in the agent's branch thread.

        Creates an embed with Approve/Reject/Details buttons and waits
        for a trusted user to respond (or timeout).

        NOTE: Always pings full-trust users. The old gateway skipped pings
        when the owner was at terminal, but the daemon doesn't have a
        single-agent presence model. This is an intentional temporary
        divergence — refine with daemon-level presence awareness later.
        """
        if not self._client:
            return {"error": "not connected"}

        # Resolve target — permission prompts go to the agent's branch thread
        session_id = ctx.session_id if ctx else args.get("session_id", "")
        thread_id = self._branch_threads.get(session_id)
        if not thread_id:
            return {"error": f"no branch thread for {session_id}"}

        try:
            thread = self._client.get_channel(thread_id)
            if not thread:
                thread = await self._client.fetch_channel(thread_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException) as e:
            log.error("Failed to get branch thread for %s: %s", session_id, e)
            return {"error": f"branch thread inaccessible: {e}"}

        title = args.get("title", "Permission Required")
        preview = args.get("preview", "")
        detail = args.get("detail")
        severity = args.get("severity", "info")
        timeout = args.get("timeout", 300)

        # Severity-based coloring
        color = 0xE74C3C if severity == "warn" else 0x3498DB  # red or blue

        embed = discord.Embed(
            title=title,
            description=preview[:2000],
            color=color,
        )
        embed.set_footer(text=f"Session: {session_id}")

        # Create view with buttons — future resolves on button click
        loop = asyncio.get_running_loop()
        future: asyncio.Future[tuple[bool, str]] = loop.create_future()
        view = _PermissionView(future, self._discord_config, detail=detail)

        # Always ping full-trust users (intentional divergence — see docstring)
        mention = self._build_owner_mentions()
        mention_content = mention or ""

        try:
            msg = await thread.send(content=mention_content, embed=embed, view=view)
        except (discord.Forbidden, discord.HTTPException) as e:
            log.error("Failed to send permission prompt: %s", e)
            return {"error": f"failed to send permission prompt: {e}"}

        # Register as pending so resolve can find it
        pending = {
            "future": future,
            "message": msg,
            "embed": embed,
            "view": view,
            "externally_resolved": False,
        }
        self._pending_permissions[session_id] = pending

        try:
            approved, responder = await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            view.stop()
            if not pending["externally_resolved"]:
                try:
                    embed.color = 0x95A5A6  # gray
                    embed.title = "\u23f0 Permission Timed Out"
                    await msg.edit(embed=embed, view=None)
                except discord.HTTPException:
                    pass
            return {"approved": False, "timed_out": True, "responder": ""}
        finally:
            self._pending_permissions.pop(session_id, None)

        # Update the message only if not already updated by resolve
        if not pending["externally_resolved"]:
            try:
                if approved:
                    embed.color = 0x2ECC71  # green
                    embed.title = "\u2705 Approved"
                else:
                    embed.color = 0xE74C3C  # red
                    embed.title = "\u274c Rejected"
                embed.set_footer(text=f"Session: {session_id} | By: {responder}")
                await msg.edit(embed=embed, view=None)
            except discord.HTTPException:
                pass

        return {"approved": approved, "timed_out": False, "responder": responder}

    async def _op_permission_resolve(self, args: dict, ctx: RequestContext | None) -> dict:
        """Externally resolve a pending permission request.

        Called when the terminal (or timeout) resolves the approval before
        Discord.  Updates the Discord embed and unblocks the waiting handler.
        """
        session_id = args.get("session_id", "")
        status = args.get("status", "")

        pending = self._pending_permissions.get(session_id)
        if not pending:
            return {"ok": False, "error": f"No pending permission for {session_id}"}

        pending["externally_resolved"] = True
        future: asyncio.Future = pending["future"]
        msg = pending["message"]
        embed = pending["embed"]
        view: discord.ui.View | None = pending.get("view")

        # Stop the live View so discord.py cleans up the listener
        if view is not None:
            view.stop()

        # Update the Discord embed
        status_display = {
            "approved": ("\u2705 Approved (terminal)", 0x2ECC71),
            "rejected": ("\u274c Rejected (terminal)", 0xE74C3C),
            "timed_out": ("\u23f0 Timed Out", 0x95A5A6),
        }
        title, color = status_display.get(status, (f"\u2139\ufe0f {status}", 0x95A5A6))
        try:
            embed.title = title
            embed.color = color
            await msg.edit(embed=embed, view=None)
        except discord.HTTPException:
            pass

        # Resolve the future to unblock _op_permission_request
        if not future.done():
            approved = status == "approved"
            future.set_result((approved, "terminal"))

        return {"ok": True}
