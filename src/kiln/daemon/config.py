"""Daemon configuration.

Loads daemon-level config from ``~/.kiln/daemon/config.yml``. This is
separate from agent config (``kiln.config.AgentConfig``) — agent config
describes a single agent; daemon config describes the shared coordination
layer.

Also provides well-known path constants so the client and server agree
on socket/PID locations without passing them around.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


# Well-known daemon paths (relative to KILN_ROOT)
KILN_ROOT = Path.home() / ".kiln"
DAEMON_DIR = KILN_ROOT / "daemon"
SOCKET_PATH = DAEMON_DIR / "kiln.sock"
PID_FILE = DAEMON_DIR / "kiln.pid"
LOG_FILE = DAEMON_DIR / "daemon.log"
LOCKDOWN_FILE = DAEMON_DIR / "lockdown"
CONFIG_FILE = DAEMON_DIR / "config.yml"
AGENTS_REGISTRY = KILN_ROOT / "agents.yml"
CHANNELS_DIR = KILN_ROOT / "channels"
STATE_DIR = DAEMON_DIR / "state"
SUBSCRIPTIONS_DIR = STATE_DIR / "subscriptions"


@dataclass
class UserConfig:
    """An external user known to the daemon (e.g. Kira)."""
    name: str
    platforms: dict[str, str] = field(default_factory=dict)
    default_platform: str = ""


@dataclass
class AdapterConfig:
    """Configuration for a single platform adapter."""
    adapter_id: str
    platform: str
    enabled: bool = True
    config: dict[str, Any] = field(default_factory=dict)


@dataclass
class ServiceConfig:
    """Configuration for an optional daemon service."""
    enabled: bool = True
    config: dict[str, Any] = field(default_factory=dict)


@dataclass
class DaemonConfig:
    """Top-level daemon configuration.

    Loaded from ``~/.kiln/daemon/config.yml``. All paths are resolved
    to absolute at load time.
    """

    socket_path: Path = field(default_factory=lambda: SOCKET_PATH)
    pid_file: Path = field(default_factory=lambda: PID_FILE)
    log_file: Path = field(default_factory=lambda: LOG_FILE)
    lockdown_file: Path = field(default_factory=lambda: LOCKDOWN_FILE)
    agents_registry: Path = field(default_factory=lambda: AGENTS_REGISTRY)
    channels_dir: Path = field(default_factory=lambda: CHANNELS_DIR)
    state_dir: Path = field(default_factory=lambda: STATE_DIR)
    subscriptions_dir: Path = field(default_factory=lambda: SUBSCRIPTIONS_DIR)

    users: dict[str, UserConfig] = field(default_factory=dict)
    adapters: dict[str, AdapterConfig] = field(default_factory=dict)
    services: dict[str, ServiceConfig] = field(default_factory=dict)

    @property
    def kiln_home(self) -> Path:
        return KILN_ROOT


def load_daemon_config(path: Path | None = None) -> DaemonConfig:
    """Load daemon config from YAML, falling back to defaults.

    If the config file doesn't exist, returns a config with all defaults.
    This is intentional — the daemon should work with zero configuration.
    """
    path = path or CONFIG_FILE
    if not path.exists():
        return DaemonConfig()

    raw = yaml.safe_load(path.read_text()) or {}
    config = DaemonConfig()

    # Path overrides — resolve ~ and make absolute
    for key in ("socket_path", "pid_file", "log_file", "lockdown_file",
                "agents_registry", "channels_dir", "state_dir",
                "subscriptions_dir"):
        if key in raw:
            setattr(config, key, Path(raw[key]).expanduser().resolve())

    # Users
    for name, user_raw in raw.get("users", {}).items():
        if isinstance(user_raw, dict):
            platforms = {k: v for k, v in user_raw.items()
                        if k != "default_platform"}
            config.users[name] = UserConfig(
                name=name,
                platforms=platforms,
                default_platform=user_raw.get("default_platform", ""),
            )

    # Adapters
    for adapter_id, adapter_raw in raw.get("adapters", {}).items():
        if isinstance(adapter_raw, dict):
            config.adapters[adapter_id] = AdapterConfig(
                adapter_id=adapter_id,
                platform=adapter_raw.get("platform", adapter_id),
                enabled=adapter_raw.get("enabled", True),
                config=adapter_raw,
            )

    # Services
    for svc_name, svc_raw in raw.get("services", {}).items():
        if isinstance(svc_raw, dict):
            config.services[svc_name] = ServiceConfig(
                enabled=svc_raw.get("enabled", True),
                config=svc_raw,
            )
        elif isinstance(svc_raw, bool):
            config.services[svc_name] = ServiceConfig(enabled=svc_raw)

    return config


def load_agents_registry(path: Path | None = None) -> dict[str, Path]:
    """Load the agent prefix -> home directory mapping.

    Returns a dict like ``{"beth": Path("/Users/kaselby/.beth")}``.
    """
    path = path or AGENTS_REGISTRY
    if not path.exists():
        return {}

    raw = yaml.safe_load(path.read_text()) or {}
    result = {}
    for prefix, home_str in raw.items():
        if isinstance(home_str, str) and not home_str.startswith("#"):
            result[prefix] = Path(home_str).expanduser().resolve()
    return result
