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
    ephemeral: bool = False,
    worklogs_dir: Path | None = None,
) -> str:
    """Generate a human-readable agent name like 'aleph-frost-hawk'.

    When ephemeral=True, uses '_{prefix}' (e.g. '_aleph-frost-hawk')
    to visually distinguish ephemeral workers in tmux list-sessions.

    Checks running tmux sessions AND today's worklogs to avoid collisions
    with both active agents and agents that already ran today.

    Falls back to hex UUID after 20 attempts.

    Args:
        prefix: Name prefix (e.g. "aleph", "kiln"). Determines both the
            generated name prefix and the tmux session prefix to check.
        ephemeral: If True, prepend underscore to prefix.
        worklogs_dir: Directory to check for today's worklogs. If None,
            only checks tmux sessions.
    """
    full_prefix = f"_{prefix}" if ephemeral else prefix
    # Also check the opposite prefix to avoid aleph-X colliding with _aleph-X
    other_prefix = f"_{prefix}" if not ephemeral else prefix

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
                if s.startswith(f"{full_prefix}-") or s.startswith(f"{other_prefix}-")
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
        name = f"{full_prefix}-{random.choice(_ADJECTIVES)}-{random.choice(_NOUNS)}"
        if name not in used:
            return name
    # Extremely unlikely fallback
    return f"{full_prefix}-{uuid.uuid4().hex[:8]}"
