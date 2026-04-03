"""Gateway configuration loading and validation."""

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("gateway.config")

DEFAULT_PORT = 18820
DEFAULT_BIND = "127.0.0.1"


@dataclass
class ChannelAccess:
    """Per-channel access policy."""

    mode: str = "open"  # open, allowlist, read_only, post_restricted
    allowlist: dict[str, dict] = field(default_factory=dict)  # user_id -> {name, trust}

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
    access: ChannelAccess = field(default_factory=ChannelAccess)
    dm_access: ChannelAccess = field(default_factory=ChannelAccess)
    voice_enabled: bool = False

    @classmethod
    def from_dict(cls, data: dict) -> "DiscordConfig":
        cfg = cls(
            enabled=data.get("enabled", True),
            guild_id=str(data.get("guild_id", "")),
            channels=data.get("channels", {}),
            voice_enabled=data.get("voice", {}).get("enabled", False),
        )

        # Channel access
        access_mode = data.get("access", "open")
        allowlist = {}
        if "allowlist" in data:
            allowlist = data["allowlist"]
        cfg.access = ChannelAccess(mode=access_mode, allowlist=allowlist)

        # DM access
        dm_mode = data.get("dm_access", "allowlist")
        dm_allowlist = {}
        if "dm_allowlist" in data:
            dm_allowlist = {uid: {"name": uid, "trust": "known"} for uid in data["dm_allowlist"]}
        elif dm_mode == "allowlist":
            dm_allowlist = allowlist  # fall back to channel allowlist
        cfg.dm_access = ChannelAccess(mode=dm_mode, allowlist=dm_allowlist)

        return cfg


@dataclass
class GatewayConfig:
    """Top-level gateway configuration."""

    bind: str = DEFAULT_BIND
    port: int = DEFAULT_PORT
    agent_home: Path = field(default_factory=lambda: Path.home())
    default_agent: str = ""
    preferred_session_file: Path | None = None  # file containing preferred agent ID
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
    preferred_file = routing.get("preferred_session_file")
    if preferred_file:
        preferred_file = (agent_home / preferred_file).resolve()

    cfg = GatewayConfig(
        bind=raw.get("bind", DEFAULT_BIND),
        port=raw.get("port", DEFAULT_PORT),
        agent_home=agent_home,
        default_agent=routing.get("default_agent", ""),
        preferred_session_file=preferred_file,
    )

    if "channels" in raw and "discord" in raw["channels"]:
        cfg.discord = DiscordConfig.from_dict(raw["channels"]["discord"])

    return cfg
