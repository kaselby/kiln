"""Session registry — track running and recent agent sessions.

Uses a JSON file with fcntl locking for concurrency safety.
"""

import fcntl
import json
from datetime import datetime
from pathlib import Path

from .shell import safe_getcwd


def register_session(
    registry_path: Path,
    agent_id: str,
    *,
    cwd: str | None = None,
    model: str | None = None,
    session_uuid: str | None = None,
    extras: dict | None = None,
) -> None:
    """Persist agent_id → session metadata to the registry.

    Called at startup (without session_uuid) so the agent is immediately
    visible to branches/sitrep, then again when the session UUID is
    captured from the first ResultMessage.

    extras: arbitrary key-value pairs merged into the entry (e.g. thread name).
    """
    registry_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        with open(registry_path, "a+") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            f.seek(0)
            try:
                registry = json.loads(f.read() or "{}")
            except json.JSONDecodeError:
                registry = {}

            entry = registry.get(agent_id, {})
            entry.update({
                "cwd": cwd or safe_getcwd(),
                "model": model,
                "started_at": entry.get("started_at") or datetime.now().isoformat(),
            })
            if session_uuid:
                entry["session_uuid"] = session_uuid
            if extras:
                entry.update(extras)
            registry[agent_id] = entry

            f.seek(0)
            f.truncate()
            f.write(json.dumps(registry, indent=2) + "\n")
    except OSError:
        pass


def lookup_session(registry_path: Path, agent_id: str) -> dict | None:
    """Look up a session registry entry by agent ID.

    Returns the full entry dict (session_uuid, cwd, model, started_at)
    or None if not found.
    """
    if not registry_path.exists():
        return None
    try:
        registry = json.loads(registry_path.read_text())
        return registry.get(agent_id)
    except (json.JSONDecodeError, OSError):
        return None


def most_recent_agent_id(registry_path: Path) -> str | None:
    """Look up the most recently started agent ID from the session registry.

    Used by --continue to reuse the same agent ID instead of generating
    a new one. Returns None if the registry doesn't exist or is empty.
    """
    if not registry_path.exists():
        return None
    try:
        registry = json.loads(registry_path.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    if not registry:
        return None
    # Sort by started_at descending, return the most recent
    return max(registry, key=lambda k: registry[k].get("started_at", ""))
