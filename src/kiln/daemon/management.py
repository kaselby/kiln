"""Daemon management actions — session lifecycle, status, bridges.

These are the platform-agnostic management semantics that live in the
daemon, not in any adapter. Adapters consume management events and
expose management actions through their platform UX, but the logic
lives here.

Phase 1 implements the query/status path. Mutating actions (spawn, stop,
interrupt, mode changes) are stubbed for Phase 5 when the Discord adapter
needs them.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from . import protocol as proto
from .config import DaemonConfig, load_agents_registry
from .state import BridgeRecord, DaemonState, _load_live_session_metadata


log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass
class ActionResult:
    success: bool
    message: str
    data: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# Management actions
# ---------------------------------------------------------------------------

class ManagementActions:
    """Provides management operations over daemon state.

    Instantiated by the daemon server and wired into the mgmt request
    handler. Adapter management commands route through here.
    """

    def __init__(self, state: DaemonState, config: DaemonConfig):
        self._state = state
        self._config = config

    # ----- Session queries -----

    def _refresh_session_record(self, record) -> None:
        """Refresh cached live session metadata from session-config.

        Presence is a cache. The authoritative live mutable session state lives
        in the session-config file, so tag- or mode-sensitive reads should
        refresh from disk before making routing/status decisions.
        """
        if not record or not record.agent_home:
            return
        mode, tags = _load_live_session_metadata(Path(record.agent_home), record.session_id)
        record.mode = mode
        record.tags = tags

    def _refresh_presence_for_agent(self, agent: str) -> list:
        sessions = self._state.presence.by_agent(agent)
        for record in sessions:
            self._refresh_session_record(record)
        return sessions

    def _refresh_session_summary(self, session_id: str):
        record = self._state.presence.get(session_id)
        if record:
            self._refresh_session_record(record)
        return record

    def list_sessions(
        self,
        agent: str | None = None,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        sessions = self._state.presence.all_sessions()
        for record in sessions:
            self._refresh_session_record(record)
        if agent:
            sessions = [s for s in sessions if s.agent_name == agent]
        if status:
            sessions = [s for s in sessions if s.status == status]
        return [s.to_summary() for s in sessions]

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        record = self._refresh_session_summary(session_id)
        if record:
            return record.to_summary()
        return None


    def resolve_dm_target(self, agent: str) -> str | None:
        """Resolve the single session that should receive platform DMs for an agent.

        Policy (daemon-owned — adapters call this, don't choose themselves):
        1. Canonical session for the agent, if one is connected.
        2. Most-recently-connected session for the agent.
        3. None if no sessions are connected.

        Canonical detection currently checks the live session tag set for
        ``canonical``.

        """
        sessions = self._refresh_presence_for_agent(agent)
        if not sessions:
            return None


        # Prefer canonical-tagged sessions
        for s in sessions:
            if "canonical" in s.tags:
                return s.session_id


        # Fallback: most recently seen
        sessions.sort(key=lambda s: s.first_seen_at, reverse=True)
        return sessions[0].session_id

    def list_historical_sessions(self, agent: str) -> set[str]:
        """List all historical session IDs for an agent.

        Reads from the durable session registry in the agent's home
        directory. Used for resume operations where the target session
        isn't currently running.
        """
        home = self._resolve_agent_home(agent)
        if not home:
            return set()

        registry_path = home / "logs" / "session-registry.json"
        if not registry_path.exists():
            return set()

        try:
            import json
            data = json.loads(registry_path.read_text())
            return set(data.keys()) if isinstance(data, dict) else set()
        except (json.JSONDecodeError, OSError):
            return set()

    # ----- Status -----

    def get_status(self, scope: str | None = None) -> dict[str, Any]:
        return {
            "sessions": len(self._state.presence),
            "channels": self._state.channels.all_channels(),
            "bridges": len(self._state.bridges.all_bridges()),
        }

    # ----- Bridge management -----

    def bind_bridge(
        self,
        bridge_id: str,
        source_kind: str,
        source_name: str,
        adapter_id: str,
        platform_target: str,
        mode: str = "mirror",
    ) -> BridgeRecord:
        record = BridgeRecord(
            bridge_id=bridge_id,
            source_kind=source_kind,
            source_name=source_name,
            adapter_id=adapter_id,
            platform_target=platform_target,
            mode=mode,
        )
        self._state.bridges.bind(record)
        log.info("Bridge bound: %s -> %s/%s", source_name, adapter_id, platform_target)
        return record

    def unbind_bridge(self, bridge_id: str) -> ActionResult:
        record = self._state.bridges.unbind(bridge_id)
        if record:
            log.info("Bridge unbound: %s", bridge_id)
            return ActionResult(True, f"Bridge '{bridge_id}' removed")
        return ActionResult(False, f"Bridge '{bridge_id}' not found")

    def list_bridges(self, adapter_id: str | None = None) -> list[dict[str, Any]]:
        if adapter_id:
            bridges = self._state.bridges.by_adapter(adapter_id)
        else:
            bridges = self._state.bridges.all_bridges()
        return [b.to_dict() for b in bridges]

    # ----- Query seam for adapters -----

    def get_channel_subscribers(self, channel: str) -> set[str]:
        """Get subscriber session IDs for a channel."""
        return self._state.channels.subscribers(channel)

    def active_channels(self) -> list[str]:
        """List channels with at least one subscriber."""
        return self._state.channels.all_channels()

    def resolve_session_ref(
        self,
        ref: str,
        prefix: str = "",
        candidates: set[str] | None = None,
    ) -> str | None:
        """Resolve a short session reference to a full session ID.

        Matching rules (in order, first match wins):
        1. Exact match
        2. Prefix-prepended exact match (e.g. "storm-jay" → "beth-storm-jay")
        3. Unambiguous prefix match (ref is a prefix of exactly one candidate)

        No arbitrary substring matching — this feeds destructive control
        commands, so false positives are dangerous.

        Args:
            candidates: Session IDs to search. Defaults to live daemon
                presence. Pass a durable set (e.g. from session registry)
                for resume operations where the target isn't currently running.
        """
        pool = candidates if candidates is not None else self._state.presence.session_ids()
        if ref in pool:
            return ref
        if prefix:
            full = f"{prefix}{ref}"
            if full in pool:
                return full
        # Unambiguous prefix match only
        matches = [sid for sid in pool if sid.startswith(ref)]
        if not matches and prefix:
            matches = [sid for sid in pool if sid.startswith(f"{prefix}{ref}")]
        if len(matches) == 1:
            return matches[0]
        return None

    # ----- Session lifecycle -----

    def _resolve_agent_home(self, agent: str) -> Path | None:
        agents = load_agents_registry(self._config.agents_registry)
        home = agents.get(agent)
        if home:
            return home
        candidate = Path.home() / f".{agent}"
        return candidate if candidate.is_dir() else None

    def _session_config_path(self, session_id: str) -> Path | None:
        """Find the session config YAML for a session."""
        record = self._state.presence.get(session_id)
        if record and record.agent_home:
            return Path(record.agent_home) / "state" / f"session-config-{session_id}.yml"
        # Fallback: try agent name from session ID prefix
        prefix = session_id.split("-")[0]
        home = self._resolve_agent_home(prefix)
        if home:
            return home / "state" / f"session-config-{session_id}.yml"
        return None

    def _build_launch_cmd(
        self,
        agent: str,
        mode: str = "yolo",
        prompt: str | None = None,
        resume_id: str | None = None,
    ) -> list[str] | None:
        """Build the command to launch/resume an agent session.

        This is the single seam for launch command construction. Prefer a
        direct agent wrapper binary when one exists (e.g. ``beth``), but
        fall back to the Kiln-native path for agents without wrappers
        (e.g. ``kiln run dalet`` / ``kiln run gimel``).
        """
        cli_path = shutil.which(agent)
        if cli_path:
            cmd = [cli_path]
        else:
            kiln_path = shutil.which("kiln")
            if not kiln_path:
                return None
            cmd = [kiln_path, "run", agent]

        cmd.extend(["--detach", "--mode", mode])
        if prompt:
            cmd.extend(["--prompt", prompt])
        if resume_id:
            cmd.extend(["--resume", resume_id])
        return cmd


    async def _run_launch_cmd(self, cmd: list[str], label: str) -> ActionResult:
        """Execute a launch/resume command and return the result."""
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
            if proc.returncode == 0:
                output = stdout.decode().strip()
                return ActionResult(True, output or label)
            else:
                err = stderr.decode().strip()[:500]
                return ActionResult(False, f"Exit {proc.returncode}: {err}")
        except asyncio.TimeoutError:
            return ActionResult(False, f"{label} timed out (30s)")
        except Exception as e:
            return ActionResult(False, f"{label} error: {e}")

    async def spawn_session(
        self,
        agent: str,
        prompt: str | None = None,
        mode: str | None = None,
        requested_by: str | None = None,
    ) -> ActionResult:
        """Spawn a new agent session."""
        cmd = self._build_launch_cmd(agent, mode=mode or "yolo", prompt=prompt)
        if not cmd:
            return ActionResult(False, f"Cannot find '{agent}' on PATH")
        log.info("Spawning %s session (by %s)", agent, requested_by or "daemon")
        return await self._run_launch_cmd(cmd, "Session launched")

    async def resume_session(
        self,
        agent: str,
        session_id: str,
        requested_by: str | None = None,
    ) -> ActionResult:
        """Resume a previously ended session."""
        rc = await self._tmux_check(session_id)
        if rc == 0:
            return ActionResult(False, f"'{session_id}' is still running")
        cmd = self._build_launch_cmd(agent, resume_id=session_id)
        if not cmd:
            return ActionResult(False, f"Cannot find '{agent}' on PATH")
        log.info("Resuming %s (by %s)", session_id, requested_by or "daemon")
        return await self._run_launch_cmd(cmd, f"Resumed {session_id}")

    async def stop_session(
        self,
        session_id: str,
        requested_by: str | None = None,
    ) -> ActionResult:
        """Kill a running session via tmux."""
        log.info("Stopping %s (by %s)", session_id, requested_by or "daemon")
        rc = await self._tmux_run("kill-session", "-t", session_id)
        if rc == 0:
            return ActionResult(True, f"Killed {session_id}")
        return ActionResult(False, f"Failed to kill '{session_id}' (tmux session not found?)")

    async def interrupt_session(
        self,
        session_id: str,
        requested_by: str | None = None,
    ) -> ActionResult:
        """Send ESC to a running session to unstick it."""
        log.info("Interrupting %s (by %s)", session_id, requested_by or "daemon")
        rc = await self._tmux_run("send-keys", "-t", session_id, "Escape")
        if rc == 0:
            return ActionResult(True, f"Sent ESC to {session_id}")
        return ActionResult(False, f"Failed to interrupt '{session_id}' (tmux session not found?)")

    async def capture_session(
        self,
        session_id: str,
        lines: int = 50,
    ) -> ActionResult:
        """Capture terminal output from a running session."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "tmux", "capture-pane", "-t", session_id, "-p", "-S", f"-{lines}",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()
            if proc.returncode != 0:
                return ActionResult(False, f"Failed to capture '{session_id}'")

            content = stdout.decode(errors="replace")
            # Strip trailing blank lines
            result_lines = [line.rstrip() for line in content.splitlines()]
            while result_lines and not result_lines[-1]:
                result_lines.pop()
            return ActionResult(True, "\n".join(result_lines))
        except Exception as e:
            return ActionResult(False, f"Capture error: {e}")

    async def set_session_mode(
        self,
        session_id: str,
        mode: str,
        requested_by: str | None = None,
    ) -> ActionResult:
        """Change a session's permission mode."""
        valid_modes = {"safe", "supervised", "yolo"}
        if mode not in valid_modes:
            return ActionResult(False, f"Invalid mode: '{mode}'. Valid: {', '.join(sorted(valid_modes))}")

        config_path = self._session_config_path(session_id)
        if not config_path or not config_path.exists():
            return ActionResult(False, f"No session config for '{session_id}'")

        try:
            data = yaml.safe_load(config_path.read_text()) or {}
        except (yaml.YAMLError, OSError):
            data = {}

        old_mode = data.get("mode", "?")
        data["mode"] = mode
        tmp = config_path.with_suffix(".tmp")
        tmp.write_text(yaml.dump(data, default_flow_style=False, sort_keys=False))
        tmp.rename(config_path)

        log.info("Mode %s: %s -> %s (by %s)", session_id, old_mode, mode, requested_by or "daemon")
        return ActionResult(
            True,
            f"{session_id}: {old_mode} -> {mode}",
            data={"old_mode": old_mode, "new_mode": mode},
        )

    # ----- tmux helpers -----

    @staticmethod
    async def _tmux_run(*args: str) -> int:
        proc = await asyncio.create_subprocess_exec(
            "tmux", *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await proc.communicate()
        return proc.returncode

    @staticmethod
    async def _tmux_check(session_id: str) -> int:
        """Check if a tmux session exists. Returns 0 if it does."""
        return await ManagementActions._tmux_run("has-session", "-t", session_id)
