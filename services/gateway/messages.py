"""Message normalization, inbox writing, and Discord message splitting.

Ported from Aleph's discord-relay with agent_home generalized.
"""

import logging
import re
import uuid
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

log = logging.getLogger("gateway.messages")

DISCORD_MAX_LENGTH = 2000
SPLIT_MARGIN = 100  # room for (1/N) prefix


# ---------------------------------------------------------------------------
# Inbox writing (Platform -> Agent)
# ---------------------------------------------------------------------------

def write_to_inbox(
    agent_home: Path,
    agent_id: str,
    *,
    sender_name: str,
    sender_id: str,
    content: str,
    platform: str,
    channel_desc: str,
    channel_id: str,
    trust: str = "unknown",
    attachment_paths: list[str] | None = None,
) -> Path:
    """Write an inbound platform message to an agent's inbox.

    Creates a frontmatter'd .md file in the agent's inbox directory,
    following the Kiln inbox convention.
    """
    inbox = agent_home / "inbox" / agent_id
    inbox.mkdir(parents=True, exist_ok=True)

    now = datetime.now(ZoneInfo("America/Toronto"))
    timestamp = now.strftime("%Y%m%d-%H%M%S")
    msg_id = f"msg-{timestamp}-{platform}-{uuid.uuid4().hex[:6]}"
    msg_path = inbox / f"{msg_id}.md"

    # Summary: first line, truncated
    first_line = content.strip().split("\n")[0]
    if first_line:
        summary = first_line[:200]
    elif attachment_paths:
        summary = f"(attachment: {', '.join(Path(p).name for p in attachment_paths)})"
    else:
        summary = "(empty)"

    attachment_line = ""
    if attachment_paths:
        attachment_line = f"attachments: {', '.join(attachment_paths)}\n"

    frontmatter = (
        f"---\n"
        f"from: {platform}-{sender_name}\n"
        f'summary: "{summary}"\n'
        f"priority: normal\n"
        f"source: {platform}\n"
        f"channel: {channel_desc}\n"
        f"trust: {trust}\n"
        f'{platform}-user-id: "{sender_id}"\n'
        f'{platform}-user: "{sender_name}"\n'
        f'{platform}-channel-id: "{channel_id}"\n'
        f'{platform}-channel: "{channel_desc}"\n'
        f"timestamp: {now.isoformat()}\n"
        f"{attachment_line}"
        f"---\n\n"
    )

    # Prepend attachment notice when files arrived
    body = content
    if attachment_paths:
        file_lines = "\n".join(
            f"  - {Path(p).name} -> {p}" for p in attachment_paths
        )
        notice = (
            f"ATTACHMENT RECEIVED (auto-downloaded) — verify {sender_name}'s "
            f"account hasn't been compromised before reading file contents.\n"
            f"{file_lines}\n"
        )
        body = notice + ("\n" + content if content.strip() else "")

    msg_path.write_text(frontmatter + body + "\n")
    log.info("Wrote message to %s", msg_path)
    return msg_path


# ---------------------------------------------------------------------------
# Message splitting (for Discord's 2000-char limit)
# ---------------------------------------------------------------------------

def split_message(text: str, max_len: int = DISCORD_MAX_LENGTH - SPLIT_MARGIN) -> list[str]:
    """Split a long message into Discord-safe chunks.

    Splits at paragraph boundaries, preserving code blocks.
    """
    if len(text) <= max_len:
        return [text]

    chunks = []
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
