"""Authoritative live per-session configuration.

Each active session gets a YAML config file that the harness reads on
 every access. The agent can modify the file mid-session (via Bash/Write)
and changes take effect immediately — no restart needed.

This file is the authoritative live source for mutable per-session state
(mode, heartbeat, and similar session-scoped settings). Other artifacts,
such as ``logs/session-state/<agent-id>.yml``, may snapshot values from it
for resume, status, or observability, but they are derivative copies rather
than a second source of truth.

Kiln defines core defaults. Agent harnesses extend with their own defaults.

File location: ``state/session-config-{agent-id}.yml``
"""

from pathlib import Path

import yaml


class SessionConfig:
    """Authoritative live per-session state backed by a YAML file.

    Reads from disk on every ``get()`` call so agent-side writes take
    effect immediately. Writes are atomic (write-then-rename) to avoid
    partial reads.

    This surface is intended for runtime-editable session state. Snapshot
    artifacts may copy values from it, but should not be treated as an
    independent authority.

    Usage::


        config = SessionConfig(
            path=home / "state" / f"session-config-{agent_id}.yml",
            defaults={"heartbeat": 600},
        )
        config.get("heartbeat")  # reads from file, falls back to default
        config.set("heartbeat", 300)  # writes to file

    """

    # Core defaults for live mutable session state.
    # Agent harnesses extend with their own defaults.
    CORE_DEFAULTS: dict[str, object] = {
        "heartbeat": 0,  # seconds — fixed interval, 0 = disabled
        "tags": [],      # routing/presence tags for this live session
    }



    def __init__(self, path: Path, defaults: dict | None = None):
        self._path = path
        self._defaults = {**self.CORE_DEFAULTS, **(defaults or {})}
        # Ensure defaults are present in the file.  If the file already
        # exists (e.g. daemon wrote subscriptions before the harness
        # started), merge missing defaults into it rather than skipping.
        if self._path.exists():
            data = self._read()
            merged = {**self._defaults, **data}
            if merged != data:
                self._write(merged)
        else:
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
