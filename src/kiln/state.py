"""Agent state — presence tracking and trust state.

Simple file-based state that multiple processes (TUI, gateway, tools) read and
write without coordination. Each writer owns its own file to avoid races.

State files live in ``{agent_home}/state/``.
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

# Presence idle threshold: if a presence file is older than this, the user
# is considered away from that surface.
PRESENCE_IDLE_SECONDS = 300  # 5 minutes

# Trust TTL: a verified trust state expires after this long.
TRUST_TTL_SECONDS = 2700  # 45 minutes


# ---------------------------------------------------------------------------
# Presence
# ---------------------------------------------------------------------------

def write_presence(state_dir: Path, surface: str, agent_id: str | None = None) -> None:
    """Write a presence timestamp for a surface (terminal or discord).

    Args:
        state_dir: The agent's state directory.
        surface: "terminal" or "discord".
        agent_id: Session ID (written for terminal presence).
    """
    state_dir.mkdir(parents=True, exist_ok=True)
    path = state_dir / f"presence-{surface}"
    lines = [datetime.now(timezone.utc).isoformat()]
    if agent_id:
        lines.append(agent_id)
    path.write_text("\n".join(lines) + "\n")


def read_presence(state_dir: Path, idle_seconds: int = PRESENCE_IDLE_SECONDS) -> dict:
    """Read presence state and derive the user's location.

    Returns a dict with:
        location: "terminal" | "discord" | "away"
        terminal_ago: seconds since last terminal activity (or None)
        discord_ago: seconds since last discord activity (or None)
        terminal_agent: agent ID from terminal presence (or None)
        summary: human-readable string like "terminal (2m ago)"
    """
    now = datetime.now(timezone.utc)
    terminal = _read_presence_file(state_dir / "presence-terminal", now)
    discord = _read_presence_file(state_dir / "presence-discord", now)

    result = {
        "location": "away",
        "terminal_ago": terminal.get("ago"),
        "discord_ago": discord.get("ago"),
        "terminal_agent": terminal.get("agent_id"),
        "summary": "away",
    }

    terminal_active = terminal.get("ago") is not None and terminal["ago"] < idle_seconds
    discord_active = discord.get("ago") is not None and discord["ago"] < idle_seconds

    if terminal_active and discord_active:
        # Both active — terminal wins (local presence is higher-signal)
        if terminal["ago"] <= discord["ago"]:
            result["location"] = "terminal"
        else:
            result["location"] = "discord"
    elif terminal_active:
        result["location"] = "terminal"
    elif discord_active:
        result["location"] = "discord"

    # Build summary
    loc = result["location"]
    if loc == "terminal":
        result["summary"] = f"terminal ({_format_ago(terminal['ago'])})"
    elif loc == "discord":
        result["summary"] = f"discord ({_format_ago(discord['ago'])})"
    else:
        # Away — show when last seen, if ever
        parts = []
        if terminal.get("ago") is not None:
            parts.append(f"terminal {_format_ago(terminal['ago'])}")
        if discord.get("ago") is not None:
            parts.append(f"discord {_format_ago(discord['ago'])}")
        if parts:
            result["summary"] = f"away (last: {', '.join(parts)})"
        else:
            result["summary"] = "away"

    return result


def _read_presence_file(path: Path, now: datetime) -> dict:
    """Parse a presence file into {timestamp, ago, agent_id}."""
    if not path.exists():
        return {}
    try:
        lines = path.read_text().strip().splitlines()
        if not lines:
            return {}
        ts = datetime.fromisoformat(lines[0])
        ago = (now - ts).total_seconds()
        result = {"timestamp": ts, "ago": ago}
        if len(lines) > 1:
            result["agent_id"] = lines[1]
        return result
    except (ValueError, OSError):
        return {}


def _format_ago(seconds: float) -> str:
    """Format seconds-ago as a compact human string."""
    s = int(seconds)
    if s < 60:
        return f"{s}s ago"
    m = s // 60
    if m < 60:
        return f"{m}m ago"
    h = m // 60
    return f"{h}h{m % 60:02d}m ago"


# ---------------------------------------------------------------------------
# Trust state
# ---------------------------------------------------------------------------

def write_trust(
    state_dir: Path,
    *,
    verified_by: str,
    discord_user_id: str,
    ttl_seconds: int = TRUST_TTL_SECONDS,
) -> None:
    """Write a verified trust state file after a successful security check."""
    state_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    data = {
        "verified": True,
        "verified_at": now.isoformat(),
        "expires_at": (now + timedelta(seconds=ttl_seconds)).isoformat(),
        "verified_by": verified_by,
        "discord_user_id": discord_user_id,
    }
    path = state_dir / "discord-trust.yml"
    path.write_text(yaml.dump(data, default_flow_style=False, sort_keys=False))


def clear_trust(state_dir: Path) -> bool:
    """Remove the trust state file (called by lockdown). Returns True if file existed."""
    path = state_dir / "discord-trust.yml"
    if path.exists():
        path.unlink()
        return True
    return False


def read_trust(state_dir: Path) -> dict:
    """Read the current trust state.

    Returns a dict with:
        verified: bool (True only if within TTL)
        verified_at: ISO timestamp string or None
        expires_at: ISO timestamp string or None
        verified_by: agent ID or None
        expired: bool (True if file exists but TTL has passed)
    """
    path = state_dir / "discord-trust.yml"
    if not path.exists():
        return {"verified": False, "expired": False}

    try:
        data = yaml.safe_load(path.read_text())
    except (yaml.YAMLError, OSError):
        return {"verified": False, "expired": False}

    if not data or not data.get("verified"):
        return {"verified": False, "expired": False}

    expires_at = data.get("expires_at")
    if expires_at:
        try:
            expiry = datetime.fromisoformat(expires_at)
            if datetime.now(timezone.utc) > expiry:
                return {
                    "verified": False,
                    "expired": True,
                    "verified_at": data.get("verified_at"),
                    "expires_at": expires_at,
                    "verified_by": data.get("verified_by"),
                }
        except ValueError:
            pass

    return {
        "verified": True,
        "expired": False,
        "verified_at": data.get("verified_at"),
        "expires_at": expires_at,
        "verified_by": data.get("verified_by"),
        "discord_user_id": data.get("discord_user_id", ""),
    }


def trust_label(
    state_dir: Path,
    *,
    config_trust: str = "",
    sender_user_id: str = "",
) -> str:
    """Return the trust string for message headers.

    Combines the config-level trust (full/known/unknown) with live
    verification state. Verification is a modifier on ``full`` only —
    other levels pass through as-is.

    Examples: ``full (verified ✓)``, ``full``, ``known``, ``unknown``.
    """
    # If no config trust provided, fall back to legacy binary label
    if not config_trust:
        trust = read_trust(state_dir)
        if trust["verified"]:
            return "verified ✓"
        return "unverified ⚠"

    if config_trust != "full":
        return config_trust

    # Full trust — check verification state with sender match
    trust = read_trust(state_dir)
    if trust["verified"]:
        verified_uid = trust.get("discord_user_id", "")
        if not sender_user_id or verified_uid == sender_user_id:
            return "full (verified ✓)"
    return "full"
