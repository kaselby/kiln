"""Per-session runtime configuration.

Each active session gets a YAML config file that the harness reads on
every access.  The agent can modify the file mid-session (via Bash/Write)
and changes take effect immediately — no restart needed.

Kiln defines core tunables (heartbeat, show_thinking).  Agent harnesses
extend with their own defaults (e.g. worklog_interval).

File location: ``state/session-config-{agent-id}.yml``
"""

from pathlib import Path

import yaml


class SessionConfig:
    """Per-session runtime config backed by a YAML file.

    Reads from disk on every ``get()`` call so agent-side writes take
    effect immediately.  Writes are atomic (write-then-rename) to avoid
    partial reads.

    Usage::

        config = SessionConfig(
            path=home / "state" / f"session-config-{agent_id}.yml",
            defaults={"heartbeat_enabled": True, "heartbeat_interval": 600},
        )
        config.get("heartbeat_interval")  # reads from file, falls back to default
        config.set("heartbeat_interval", 30)  # writes to file
    """

    # Core tunables with kiln-level defaults.
    # Agent harnesses extend with their own (e.g. Aleph adds show_thinking,
    # worklog_interval).
    CORE_DEFAULTS: dict[str, object] = {
        "heartbeat_enabled": False,
        "heartbeat_interval": 1800,  # seconds
    }

    def __init__(self, path: Path, defaults: dict | None = None):
        self._path = path
        self._defaults = {**self.CORE_DEFAULTS, **(defaults or {})}
        # Write initial config only if the file doesn't already exist
        # (a resumed session may already have one).
        if not self._path.exists():
            self._write(dict(self._defaults))

    @property
    def path(self) -> Path:
        return self._path

    def _read(self) -> dict:
        try:
            data = yaml.safe_load(self._path.read_text())
            return data if isinstance(data, dict) else {}
        except (OSError, yaml.YAMLError):
            return {}

    def _write(self, data: dict) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(yaml.dump(data, default_flow_style=False, sort_keys=False))
        tmp.rename(self._path)

    def get(self, key: str, default=None):
        """Read a single value. Falls back to init defaults, then ``default``."""
        data = self._read()
        if key in data:
            return data[key]
        if key in self._defaults:
            return self._defaults[key]
        return default

    def set(self, key: str, value) -> None:
        """Set a single value and persist to disk."""
        data = self._read()
        data[key] = value
        self._write(data)

    def update(self, updates: dict) -> None:
        """Merge multiple values and persist to disk."""
        data = self._read()
        data.update(updates)
        self._write(data)

    @property
    def all(self) -> dict:
        """Return all values (defaults + file overrides)."""
        result = dict(self._defaults)
        result.update(self._read())
        return result

    def cleanup(self) -> None:
        """Remove the config file (called at session end)."""
        try:
            self._path.unlink(missing_ok=True)
        except OSError:
            pass
