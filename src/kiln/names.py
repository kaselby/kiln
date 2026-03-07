"""Agent name generation — human-readable names like 'aleph-frost-hawk'."""

import random
import subprocess
import uuid
from datetime import date
from pathlib import Path

_ADJECTIVES = [
    "amber", "ash", "black", "blue", "bold", "bright", "calm", "cold",
    "coral", "crimson", "crystal", "dark", "dawn", "deep", "dusk", "ember",
    "far", "first", "frost", "gold", "green", "grey", "high", "iron",
    "jade", "keen", "last", "lone", "lost", "low", "moss", "new",
    "old", "pale", "quiet", "red", "shadow", "silver", "still", "stone",
    "storm", "sun", "swift", "thorn", "warm", "white", "wild",
]

_NOUNS = [
    "bay", "bear", "blade", "brook", "cairn", "cliff", "cove", "crane",
    "crow", "dale", "deer", "drake", "dune", "elk", "falcon", "fern",
    "field", "forge", "fox", "gate", "glade", "glen", "grove", "hare",
    "haven", "hawk", "heron", "isle", "jay", "keep", "lake", "lark",
    "lynx", "marsh", "moth", "owl", "peak", "pike", "pine", "pond",
    "raven", "reach", "reef", "ridge", "seal", "shore", "spire", "vale",
    "vole", "ward", "wolf", "wren",
]


def generate_agent_name(
    prefix: str = "kiln",
    *,
    worklogs_dir: Path | None = None,
) -> str:
    """Generate a human-readable agent name like 'aleph-frost-hawk'.

    Checks running tmux sessions AND today's worklogs to avoid collisions
    with both active agents and agents that already ran today.

    Falls back to hex UUID after 20 attempts.

    Args:
        prefix: Name prefix (e.g. "aleph", "kiln"). Used as-is
            in the generated name.
        worklogs_dir: Directory to check for today's worklogs. If None,
            only checks tmux sessions.
    """
    # Check both prefix and its underscore variant to avoid collisions
    # (e.g. aleph-frost-hawk shouldn't collide with _aleph-frost-hawk)
    bare = prefix.lstrip("_")
    variants = {bare, f"_{bare}"}

    # Get running tmux session names for collision check.
    used: set[str] = set()
    try:
        result = subprocess.run(
            ["tmux", "list-sessions", "-F", "#{session_name}"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            used = {
                s for s in result.stdout.strip().splitlines()
                if any(s.startswith(f"{v}-") for v in variants)
            }
    except (OSError, subprocess.TimeoutExpired):
        pass

    # Also check today's worklogs — an agent that ran earlier today and
    # exited won't be in tmux anymore but its name shouldn't be reused.
    if worklogs_dir and worklogs_dir.is_dir():
        today = date.today().isoformat()
        for f in worklogs_dir.iterdir():
            # worklog-YYYY-MM-DD-agent-name.md
            if f.name.startswith(f"worklog-{today}-") and f.name.endswith(".md"):
                agent_name = f.name[len(f"worklog-{today}-"):-len(".md")]
                used.add(agent_name)

    for _ in range(20):
        name = f"{prefix}-{random.choice(_ADJECTIVES)}-{random.choice(_NOUNS)}"
        if name not in used:
            return name
    # Extremely unlikely fallback
    return f"{prefix}-{uuid.uuid4().hex[:8]}"
