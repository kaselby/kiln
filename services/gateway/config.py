"""Gateway configuration loading and validation."""

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("gateway.config")

DEFAULT_PORT = 18820
DEFAULT_BIND = "127.0.0.1"


@dataclass
class AccessPolicy:
    """Access control policy for a message surface (channels or DMs)."""

    mode: str = "open"  # open, allowlist, read_only, post_restricted
    allowlist: set[str] = field(default_factory=set)  # user IDs (only used in allowlist/post_restricted modes)

    def is_allowed(self, user_id: str) -> bool:
        if self.mode == "open":
            return True
        if self.mode in ("allowlist", "post_restricted"):
            return user_id in self.allowlist
        return False  # read_only — no interaction


@dataclass
class DiscordConfig:
    """Discord-specific configuration."""

    enabled: bool = True
    guild_id: str = ""
    channels: dict[str, str] = field(default_factory=dict)  # name -> channel_id
    users: dict[str, dict] = field(default_factory=dict)  # user_id -> {name, trust}
    channel_access: AccessPolicy = field(default_factory=AccessPolicy)
    dm_access: AccessPolicy = field(default_factory=AccessPolicy)
    voice_enabled: bool = False

    def resolve_user(self, user_id: str, fallback_name: str = "") -> tuple[str, str]:
        """Look up a user's display name and trust level.

        Returns (name, trust). Falls back to fallback_name/user_id if unknown.
        """
        entry = self.users.get(user_id, {})
        name = entry.get("name", fallback_name or user_id)
        trust = entry.get("trust", "unknown")
        return name, trust

    @classmethod
    def from_dict(cls, data: dict) -> "DiscordConfig":
        cfg = cls(
            enabled=data.get("enabled", True),
            guild_id=str(data.get("guild_id", "")),
            channels=data.get("channels", {}),
            users=data.get("users", {}),
            voice_enabled=data.get("voice", {}).get("enabled", False),
        )

        # Channel access
        channel_mode = data.get("channel_access", "open")
        channel_allowlist = set(data.get("channel_allowlist", cfg.users.keys()))
        cfg.channel_access = AccessPolicy(mode=channel_mode, allowlist=channel_allowlist)

        # DM access
        dm_mode = data.get("dm_access", "allowlist")
        dm_allowlist = set(data.get("dm_allowlist", cfg.users.keys()))
        cfg.dm_access = AccessPolicy(mode=dm_mode, allowlist=dm_allowlist)

        return cfg


@dataclass
class GatewayConfig:
    """Top-level gateway configuration."""

    bind: str = DEFAULT_BIND
    port: int = DEFAULT_PORT
    agent_home: Path = field(default_factory=lambda: Path.home())
    default_agent: str = ""
    discord: DiscordConfig | None = None

    @property
    def credentials_dir(self) -> Path:
        return self.agent_home / "credentials"

    @property
    def state_file(self) -> Path:
        return self.agent_home / "state" / "gateway.json"

    @property
    def pid_file(self) -> Path:
        return self.agent_home / "gateway.pid"

    def load_credential(self, name: str) -> str | None:
        path = self.credentials_dir / name
        if path.exists():
            return path.read_text().strip()
        return None


def load_config(path: Path) -> GatewayConfig:
    """Load gateway config from a JSON file."""
    if not path.exists():
        raise FileNotFoundError(f"Gateway config not found: {path}")

    with open(path) as f:
        raw = json.load(f)

    agent_home = Path(raw.get("agent_home", "~")).expanduser()

    routing = raw.get("routing", {})

    cfg = GatewayConfig(
        bind=raw.get("bind", DEFAULT_BIND),
        port=raw.get("port", DEFAULT_PORT),
        agent_home=agent_home,
        default_agent=routing.get("default_agent", ""),
    )

    if "channels" in raw and "discord" in raw["channels"]:
        cfg.discord = DiscordConfig.from_dict(raw["channels"]["discord"])

    return cfg
