"""Default harness — batteries-included session manager for simple agents.

Reads an agent spec (agent.yml), assembles the system prompt, wires
infrastructure hooks, and manages the session lifecycle. This is what
`kiln run <agent>` uses.

Complex agents write their own harness that imports kiln's building
blocks directly.
"""

import fcntl
import json
import logging
import os
import shutil
import subprocess
from datetime import date, datetime
from pathlib import Path

import yaml

log = logging.getLogger("kiln.harness")

from claude_agent_sdk import (
    ClaudeAgentOptions,
    ClaudeSDKClient,
    HookMatcher,
)

from .config import AgentConfig
from .hooks import (
    create_session_state_hook,
    create_context_warning_hook,
    create_inbox_check_hook,
    create_message_sent_hook,
    create_plan_nudge_hook,
    create_queued_message_hook,
    create_read_tracking_hook,
    create_skill_context_hook,
    create_usage_log_hook,
    wrap_hook_visibility,
)
from .names import generate_agent_name
from .permissions import PermissionHandler, PermissionMode
from .prompt import (
    build_session_context,
    discover_skill_layout,
    discover_skills,
    discover_tool_layout,
    discover_tools,
    get_knowledge_cutoff,
    load_tool_docs,
    resolve_model,
)
from .registry import lookup_session, register_session
from .session_config import SessionConfig
from .shell import safe_getcwd
from .tools import FileState, SessionControl, create_mcp_server


class _BlockDumper(yaml.SafeDumper):
    """YAML dumper that uses literal block scalars for multiline strings."""
    pass

def _block_str_representer(dumper, data):
    if "\n" in data:
        # Strip trailing whitespace per line — block scalars can't represent it,
        # and it's never meaningful in prompt/config content.
        cleaned = "\n".join(line.rstrip() for line in data.split("\n"))
        return dumper.represent_scalar("tag:yaml.org,2002:str", cleaned, style="|")
    return dumper.represent_scalar("tag:yaml.org,2002:str", data)

_BlockDumper.add_representer(str, _block_str_representer)


def _tools_path_dirs(tools_path: Path) -> str:
    """Build a colon-separated PATH string for the agent's tools directory.

    When using a tiered layout (core/ and/or library/ subdirs), includes
    those subdirectories on the PATH so tools are callable by name.
    Always includes bin/ if it exists (for manually managed scripts).
    The top-level tools dir is always included.
    """
    dirs = [str(tools_path)]
    for subdir in ("core", "library", "bin"):
        p = tools_path / subdir
        if p.is_dir():
            dirs.append(str(p))
    return ":".join(dirs)


class KilnHarness:
    """Default session manager for agents using agent.yml configuration.

    Handles prompt assembly, hook wiring, MCP server setup, and session
    lifecycle. For simple agents that don't need custom behavior.

    Complex agents should write their own harness class that imports
    kiln's building blocks (kiln.tools, kiln.hooks, kiln.prompt, etc.)
    and composes them however they want.
    """

    def __init__(self, config: AgentConfig):
        self.config = config
        base_prefix = config.session_prefix.rstrip("-")
        self.agent_id = config.agent_id or generate_agent_name(
            prefix=base_prefix,
            worklogs_dir=config.worklogs_path,
        )
        self.session_id: str | None = None
        self._client: ClaudeSDKClient | None = None
        self._expected_model = resolve_model(config.model)
        self._model_verified = False
        self._permission_hook = None
        self._permission_callbacks = None
        self._initial_mode = PermissionMode(config.initial_mode) if config.initial_mode else PermissionMode.SUPERVISED
        self._shell_cleanup = None
        self._get_shell_cwd = None
        self._stderr_log: Path | None = None
        self._stderr_fh = None
        self.session_control: SessionControl | None = None
        self.restart_requested = False
        self.continue_requested = False
        self.handoff_text: str | None = None
        self.steering_queue: list[str] = []
        self.followup_queue: list[str] = []
        self.ui_events: list[dict] = []
        self.session_config: SessionConfig | None = None  # created in _build_options
        self._resume_uuid: str | None = None  # set in _build_options if resuming
        self._worklog_path = self._resolve_worklog_path()

        # Spawned subagents default to yolo — no human watching
        if self.config.parent and self.config.initial_mode is None:
            self.config.initial_mode = "yolo"

    @property
    def permission_mode(self) -> PermissionMode:
        """Current permission mode. Reads from session config so external
        changes (gateway control channel, other tools) take effect on next
        tool use. Falls back to initial mode before session config exists."""
        if self.session_config is not None:
            raw = self.session_config.get("mode")
            if raw:
                try:
                    mode = PermissionMode(raw)
                    # Trusted cannot be set via config file — TUI only
                    if mode == PermissionMode.TRUSTED:
                        return self._initial_mode
                    return mode
                except ValueError:
                    pass
        return self._initial_mode

    @permission_mode.setter
    def permission_mode(self, value: PermissionMode) -> None:
        """Set permission mode. Persists to session config so external
        tools can observe the current mode."""
        self._initial_mode = value
        if self.session_config is not None:
            self.session_config.set("mode", value.value)

    @property
    def show_thinking(self) -> bool:
        """Whether to display thinking blocks in the TUI.

        Reads from session config on every access so mid-session changes
        take effect immediately.
        """
        if self.session_config is not None:
            return bool(self.session_config.get("show_thinking", True))
        return True

    @show_thinking.setter
    def show_thinking(self, value: bool) -> None:
        if self.session_config is not None:
            self.session_config.set("show_thinking", value)

    def _resolve_worklog_path(self) -> Path:
        """Compute the worklog path for this session."""
        worklogs_dir = self.config.worklogs_path
        # Check for existing worklog for this agent ID (handles resume)
        new_suffix = f"-{self.agent_id}.md"
        new_prefix = "worklog-"
        if worklogs_dir.exists():
            for f in worklogs_dir.iterdir():
                if f.name.startswith(new_prefix) and f.name.endswith(new_suffix):
                    middle = f.name[len(new_prefix):-len(new_suffix)]
                    if len(middle) == 10 and middle[4] == "-" and middle[7] == "-":
                        return f
        today = date.today().strftime("%Y-%m-%d")
        return worklogs_dir / f"worklog-{today}-{self.agent_id}.md"

    @property
    def worklog_path(self) -> Path:
        return self._worklog_path

    @staticmethod
    def _dedup_path(path: Path) -> Path:
        """Return a non-colliding path, appending _2, _3, etc. if needed."""
        if not path.exists():
            return path
        stem, suffix = path.stem, path.suffix
        parent = path.parent
        n = 2
        while (parent / f"{stem}_{n}{suffix}").exists():
            n += 1
        return parent / f"{stem}_{n}{suffix}"

    @property
    def _registry_path(self) -> Path:
        return self.config.home / "logs" / "session-registry.json"

    def register_session(self) -> None:
        """Register this session in the registry."""
        register_session(
            self._registry_path,
            self.agent_id,
            cwd=self.config.project or safe_getcwd(),
            model=self.config.model,
            session_uuid=self.session_id,
        )

    def set_permission_callbacks(self, get_mode, request_permission) -> None:
        """Register TUI callbacks for permission handling."""
        self._permission_callbacks = (get_mode, request_permission)

    # -- Session state persistence -----------------------------------------

    @property
    def _session_state_path(self) -> Path:
        return self.config.home / "logs" / "session-state" / f"{self.agent_id}.yml"

    def _save_session_state(
        self,
        system_prompt: str,
        session_config: dict | None = None,
        channel_subscriptions: list[str] | None = None,
    ) -> None:
        state: dict = {"system_prompt": system_prompt}
        if session_config is not None:
            state["session_config"] = session_config
        if channel_subscriptions is not None:
            state["channel_subscriptions"] = channel_subscriptions
        path = self._session_state_path
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(yaml.dump(state, Dumper=_BlockDumper, default_flow_style=False, sort_keys=False, allow_unicode=True))
        tmp.rename(path)

    def _load_session_state(self) -> dict | None:
        path = self._session_state_path
        if not path.exists():
            return None
        try:
            data = yaml.safe_load(path.read_text())
            return data if isinstance(data, dict) else None
        except (OSError, yaml.YAMLError):
            return None

    def _snapshot_channel_subscriptions(self) -> list[str]:
        channels_path = self.config.home / "channels.json"
        if not channels_path.exists():
            return []
        try:
            with open(channels_path) as f:
                fcntl.flock(f, fcntl.LOCK_SH)
                data = json.loads(f.read() or "{}")
        except (OSError, json.JSONDecodeError):
            return []
        return [ch for ch, subs in data.items() if self.agent_id in subs]

    def _restore_channel_subscriptions(self, subscriptions: list[str]) -> None:
        if not subscriptions:
            return
        channels_path = self.config.home / "channels.json"
        try:
            channels_path.parent.mkdir(parents=True, exist_ok=True)
            with open(channels_path, "a+") as f:
                fcntl.flock(f, fcntl.LOCK_EX)
                f.seek(0)
                try:
                    channels = json.loads(f.read() or "{}")
                except json.JSONDecodeError:
                    channels = {}
                for channel in subscriptions:
                    subs = channels.get(channel, [])
                    if self.agent_id not in subs:
                        subs.append(self.agent_id)
                    channels[channel] = subs
                f.seek(0)
                f.truncate()
                f.write(json.dumps(channels, indent=2) + "\n")
        except OSError:
            pass

    # -- Options builder ----------------------------------------------------

    def _build_options(self) -> ClaudeAgentOptions:
        """Build ClaudeAgentOptions from config."""
        # Restore saved state if resuming (--resume and --last both set resume_session).
        saved_state = self._load_session_state() if self.config.resume_session else None

        cwd = self.config.project or safe_getcwd()

        if saved_state and "system_prompt" in saved_state:
            full_prompt = saved_state["system_prompt"]
        else:
            # Build fresh prompt
            identity = self.config.load_identity()
            custom_tools = discover_tool_layout(self.config.tools_path)
            skills = discover_skill_layout(self.config.skills_path)

            extra_lines = [f"Inbox: {self.config.agent_inbox(self.agent_id)}"]
            session_ctx = build_session_context(
                self.agent_id,
                self.config.model,
                tools=custom_tools,
                skills=skills,
                parent=self.config.parent,
                depth=self.config.depth,
                cwd=cwd,
                extra_lines=extra_lines,
            )

            tool_docs = load_tool_docs(self.config.tools)

            context_parts = []
            for label, content in self.config.load_context_files():
                context_parts.append(f"\n\n---\n## {label}\n\n{content}")

            full_prompt = identity
            if tool_docs:
                full_prompt += "\n\n" + tool_docs
            full_prompt += session_ctx + "".join(context_parts)

        # Save session state — on resume, preserve saved config + channels so a crash
        # between here and shutdown doesn't lose them. Updated again at shutdown.
        self._save_session_state(
            full_prompt,
            session_config=saved_state.get("session_config") if saved_state else None,
            channel_subscriptions=saved_state.get("channel_subscriptions") if saved_state else None,
        )

        # Set up inbox
        inbox = self.config.agent_inbox(self.agent_id)
        inbox.mkdir(parents=True, exist_ok=True)

        # Transfer unread messages on self-continuation
        if self.config.continuation and self.config.parent:
            parent_inbox = self.config.agent_inbox(self.config.parent)
            if parent_inbox.exists():
                for msg in parent_inbox.iterdir():
                    if msg.suffix == ".read":
                        continue
                    if msg.suffix == ".md" and msg.with_suffix(".read").exists():
                        continue
                    dest = inbox / msg.name
                    try:
                        if dest.exists():
                            dest.unlink()
                        msg.rename(dest)
                    except OSError:
                        pass

        # Shared state
        file_state = FileState()
        self.session_control = SessionControl()

        # Per-session runtime config — seeded from harness config, agent-writable.
        # On resume, saved values override harness defaults so state is restored.
        config_defaults = {
            "mode": self._initial_mode.value,
            "heartbeat_enabled": self.config.heartbeat,
            "heartbeat_max": self.config.heartbeat_max,
            "heartbeat_override": self.config.heartbeat_override,
        }
        if saved_state and saved_state.get("session_config"):
            config_defaults.update(saved_state["session_config"])
        self.session_config = SessionConfig(
            path=self.config.home / "state" / f"session-config-{self.agent_id}.yml",
            defaults=config_defaults,
        )

        # Restore channel subscriptions from saved state
        if saved_state and saved_state.get("channel_subscriptions"):
            self._restore_channel_subscriptions(saved_state["channel_subscriptions"])

        # Build infrastructure hooks
        state_dir = self.config.home / "state"
        inbox_check = create_inbox_check_hook(inbox, ui_events=self.ui_events, state_dir=state_dir)
        read_tracker = create_read_tracking_hook(inbox, file_state=file_state)
        context_warning = create_context_warning_hook(self.session_control)
        session_state = self._create_session_state_hook()
        skill_context = create_skill_context_hook(self.config.skills_path)
        usage_log = create_usage_log_hook(
            self.config.home / "logs", self.agent_id,
            self.config.tools_path / "bin",
        )
        queued_messages = create_queued_message_hook(
            self.steering_queue, self.ui_events,
        )
        message_sent = create_message_sent_hook(self.ui_events)

        plans_path = self.config.plans_path
        plan_nudge = create_plan_nudge_hook(plans_path / f"{self.agent_id}.yml")

        # Wrap output-producing hooks with terminal visibility if enabled.
        # Silent hooks (read_tracker, usage_log, message_sent) are excluded —
        # they never produce agent-facing output so there's nothing to show.
        if self.config.hook_visibility:
            ui = self.ui_events
            inbox_check = wrap_hook_visibility(inbox_check, "inbox_check", ui)
            queued_messages = wrap_hook_visibility(queued_messages, "queued_messages", ui)
            context_warning = wrap_hook_visibility(context_warning, "context_warning", ui)
            session_state = wrap_hook_visibility(session_state, "session_state", ui)
            plan_nudge = wrap_hook_visibility(plan_nudge, "plan_nudge", ui)
            skill_context = wrap_hook_visibility(skill_context, "skill_context", ui)

        hooks = {
            "PostToolUse": [
                HookMatcher(matcher=None, hooks=[
                    inbox_check, queued_messages, context_warning,
                    session_state, usage_log, plan_nudge,
                ]),
                HookMatcher(matcher="Read", hooks=[read_tracker]),
                HookMatcher(matcher="mcp__kiln__Read", hooks=[read_tracker]),
                HookMatcher(matcher="mcp__kiln__activate_skill", hooks=[skill_context]),
                HookMatcher(matcher="mcp__kiln__message", hooks=[message_sent]),
            ],
            "Stop": [],
        }

        # Permission handler — always active, even in headless mode.
        # TUI provides interactive callbacks; headless passes no terminal
        # handler (gateway-only for confirm-tier guardrails).
        if self._permission_callbacks:
            get_mode, terminal_handler = self._permission_callbacks
        else:
            get_mode = lambda: self.permission_mode
            terminal_handler = None
        self._permission_handler = PermissionHandler(
            get_mode=get_mode,
            terminal_handler=terminal_handler,
            get_cwd=lambda: self._get_shell_cwd() if self._get_shell_cwd else safe_getcwd(),
            agent_id=self.agent_id,
            agent_home=str(self.config.home),
        )
        hooks["PreToolUse"] = [HookMatcher(matcher=None, hooks=[self._permission_handler.hook])]

        # Resolve tools from agent spec
        resolved = self.config.resolve_tools()
        base_tools = resolved.get("Base", ["Read", "WebSearch"])

        # Environment
        venv_path = self.config.home / "venv"
        tools_path = self.config.tools_path
        tools_dirs = _tools_path_dirs(tools_path)
        base_path = os.environ.get("PATH", "")
        env = {
            "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
            "CLAUDE_CODE_DISABLE_FILE_CHECKPOINTING": "1",
            "CLAUDE_CODE_SESSIONEND_HOOKS_TIMEOUT_MS": "30000",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_PAGER": "cat",
            "PAGER": "cat",
            "KILN_AGENT_HOME": str(self.config.home),
            "AGENT_HOME": str(self.config.home),  # short alias for tools
            "KILN_AGENT_ID": self.agent_id,
        }
        if venv_path.exists():
            venv_bin = venv_path / "bin"
            env["VIRTUAL_ENV"] = str(venv_path)
            env["PATH"] = f"{tools_dirs}:{venv_bin}:{base_path}"
        else:
            env["PATH"] = f"{tools_dirs}:{base_path}"

        # Build MCP server
        mcp_server, self._shell_cleanup, self._get_shell_cwd = create_mcp_server(
            self.config.inbox_path, self.config.skills_path,
            agent_id=self.agent_id,
            cwd=cwd, env=env, file_state=file_state,
            session_control=self.session_control,
            plans_path=plans_path,
        )
        mcp_servers = {"kiln": mcp_server}

        # Resolve conversation continuity for --resume / --last.
        resume_uuid = None
        if self.config.resume_session:
            entry = lookup_session(self._registry_path, self.config.resume_session)
            if not entry:
                raise RuntimeError(
                    f"Cannot resume: no session found for '{self.config.resume_session}'."
                )
            resume_uuid = entry.get("session_uuid")
            if not resume_uuid:
                raise RuntimeError(
                    f"Cannot resume: no session UUID recorded for '{self.config.resume_session}'. "
                    f"The session may have exited before completing its first turn."
                )
            if entry.get("cwd"):
                cwd = entry["cwd"]
        # Expose resume UUID so the TUI can locate the prior conversation.
        self._resume_uuid = resume_uuid

        # Stderr logging
        log_dir = self.config.home / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        self._stderr_log = log_dir / f"stderr-{self.agent_id}.log"
        self._stderr_fh = open(self._stderr_log, "a")

        def _stderr_callback(line: str) -> None:
            self._stderr_fh.write(line)
            self._stderr_fh.flush()

        opts = dict(
            system_prompt=full_prompt,
            tools=base_tools,
            allowed_tools=[],
            hooks=hooks,
            mcp_servers=mcp_servers,
            model=self.config.model,
            cwd=cwd,
            env=env,
            permission_mode="bypassPermissions",
            include_partial_messages=True,
            continue_conversation=False,
            resume=resume_uuid,
            stderr=_stderr_callback,
            extra_args={"setting-sources": ""},  # don't inherit user's CLI settings/MCP servers
        )
        if self.config.effort:
            opts["effort"] = self.config.effort
        return ClaudeAgentOptions(**opts)

    def _template_vars(self) -> dict[str, str]:
        """Build template variable dict for orientation and cleanup messages.

        Provides: {agent_id}, {today}, {now}, {summary_path}.
        Subclasses can override to add agent-specific variables.
        """
        today = date.today().strftime("%Y-%m-%d")
        now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

        sessions_path = self.config.sessions_path
        summary_path = sessions_path / f"{today}-{self.agent_id}.md"
        if summary_path.exists():
            n = 2
            while (sessions_path / f"{today}-{self.agent_id}_{n}.md").exists():
                n += 1
            summary_path = sessions_path / f"{today}-{self.agent_id}_{n}.md"

        return dict(
            agent_id=self.agent_id,
            today=today,
            now=now,
            summary_path=str(summary_path),
        )

    def _run_startup_commands(self) -> None:
        """Run startup commands from agent config before session begins.

        Each command runs as a subprocess with the agent's environment
        (AGENT_HOME set, tools dir on PATH). Failures log warnings but
        don't block the session.
        """
        if not self.config.startup:
            return

        env = os.environ.copy()
        env["AGENT_HOME"] = str(self.config.home)
        env["KILN_AGENT_HOME"] = str(self.config.home)
        tools_dirs = _tools_path_dirs(self.config.tools_path)
        base_path = env.get("PATH", "")
        venv_path = self.config.home / "venv"
        if venv_path.exists():
            venv_bin = str(venv_path / "bin")
            env["VIRTUAL_ENV"] = str(venv_path)
            env["PATH"] = f"{tools_dirs}:{venv_bin}:{base_path}"
        else:
            env["PATH"] = f"{tools_dirs}:{base_path}"

        for cmd in self.config.startup:
            try:
                result = subprocess.run(
                    cmd, shell=True, env=env,
                    capture_output=True, text=True, timeout=30,
                )
                if result.returncode != 0:
                    log.warning(
                        "Startup command failed (exit %d): %s\n%s",
                        result.returncode, cmd, result.stderr.strip(),
                    )
            except subprocess.TimeoutExpired:
                log.warning("Startup command timed out (30s): %s", cmd)
            except Exception:
                log.exception("Startup command error: %s", cmd)

    async def start(self):
        """Start the agent session.

        Queues orientation message (if configured) onto followup_queue.
        If --prompt is also set, it's delivered as an inbox message so the
        agent discovers it naturally during orientation (via inbox_check hook).
        If no orientation, --prompt is the startup message on followup_queue.
        """
        self._run_startup_commands()
        options = self._build_options()
        self._client = ClaudeSDKClient(options)
        self.register_session()
        await self._client.connect()

        # Queue startup messages onto followup_queue (programmatic user turns).
        # steering_queue is for user-typed mid-turn input only.
        # Skip orientation on resume — the prior conversation already has it.
        orientation = self._build_orientation()
        if orientation and not self._resume_uuid:
            self.followup_queue.append(orientation)

        if self.config.prompt:
            if orientation and not self._resume_uuid:
                # Deliver as inbox message — discovered naturally during orientation
                self._deliver_prompt_to_inbox()
            else:
                # No orientation (or resumed) — prompt is the startup message
                self.followup_queue.append(self.config.prompt)

    def _deliver_prompt_to_inbox(self) -> None:
        """Write --prompt text as an inbox message for discovery during orientation.

        When both orientation and --prompt are set, the prompt is delivered as
        an inbox message rather than queued as a followup. This way the agent
        discovers it naturally via the inbox_check hook during its first turn,
        instead of waiting for the entire turn to complete.
        """
        inbox = self.config.agent_inbox(self.agent_id)
        inbox.mkdir(parents=True, exist_ok=True)

        sender = self.config.parent or "system"
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        msg_path = inbox / f"msg-{ts}-prompt.md"
        msg_path.write_text(
            f"---\n"
            f"from: {sender}\n"
            f'summary: "Startup prompt"\n'
            f"priority: high\n"
            f"timestamp: {datetime.now().isoformat()}\n"
            f"---\n\n"
            f"{self.config.prompt}\n"
        )

    def _create_session_state_hook(self):
        """Create the periodic session state hook. Override for custom state display."""
        return create_session_state_hook(
            self,
            interval=15,
            channels_path=self.config.home / "channels.json",
            session_prefix=self.config.session_prefix,
        )

    def session_state_labels(self) -> list[str]:
        """Extra labels for the session state hook. Override in subclasses."""
        return []

    def _build_orientation(self) -> str | None:
        """Build the startup orientation message.

        Returns the formatted orientation string, or None if not configured.
        Subclasses can override to provide role-based defaults.
        """
        if self.config.orientation is None:
            return None
        if not self.config.orientation.strip():
            return None
        return self.config.orientation.rstrip().format(**self._template_vars())

    async def send(self, message: str):
        """Send a user message to the agent."""
        if not self._client:
            raise RuntimeError("Harness not started. Call start() first.")
        await self._client.query(message)

    async def receive(self):
        """Yield messages from the agent until the turn ends."""
        if not self._client:
            raise RuntimeError("Harness not started. Call start() first.")
        async for msg in self._client.receive_response():
            yield msg

    def check_model(self, actual_model: str) -> str | None:
        """Check actual model against expected. Returns warning or None."""
        if self._model_verified:
            return None
        self._model_verified = True
        if actual_model == self._expected_model:
            return None
        warning = (
            f"Model mismatch: expected '{self._expected_model}' "
            f"but got '{actual_model}'. Update MODEL_ALIASES in prompt.py."
        )
        cutoff = get_knowledge_cutoff(actual_model)
        if cutoff == "unknown":
            warning += (
                f" Knowledge cutoff for '{actual_model}' is also unknown — "
                f"update KNOWLEDGE_CUTOFFS too."
            )
        return warning

    async def interrupt(self):
        """Interrupt the agent's current turn."""
        if self._client:
            await self._client.interrupt()

    async def force_stop(self):
        """Force-kill the CLI subprocess."""
        if self._shell_cleanup:
            await self._shell_cleanup()
            self._shell_cleanup = None
        if self._client:
            await self._client.disconnect()
            self._client = None

    def commit_memory(self) -> str | None:
        """Commit changed files to git. Returns summary or None."""
        import subprocess
        import time

        repo = self.config.home
        if not (repo / ".git").exists():
            return None

        for attempt in range(5):
            try:
                subprocess.run(
                    ["git", "add", "-A"],
                    cwd=repo, capture_output=True, timeout=10,
                )
                result = subprocess.run(
                    ["git", "diff", "--cached", "--quiet"],
                    cwd=repo, capture_output=True, timeout=10,
                )
                if result.returncode == 0:
                    return None

                msg = f"Session end: {self.agent_id}"
                result = subprocess.run(
                    ["git", "commit", "-m", msg],
                    cwd=repo, capture_output=True, text=True, timeout=10,
                )
                if result.returncode == 0:
                    return result.stdout.strip().split("\n")[0]
                else:
                    if (repo / ".git" / "index.lock").exists():
                        raise FileExistsError("index.lock")
                    return None
            except (FileExistsError, subprocess.TimeoutExpired):
                if attempt < 4:
                    time.sleep(1 * (2 ** attempt))
                continue
            except Exception:
                return None
        return None

    def archive_conversation(self) -> str | None:
        """Copy conversation JSONL to agent logs. Returns path or None."""
        if not self.session_id:
            return None

        cwd = self.config.project or safe_getcwd()
        cwd = str(Path(cwd).resolve())
        project_dir_name = cwd.replace("/", "-").replace(".", "-")
        source = Path.home() / ".claude" / "projects" / project_dir_name / f"{self.session_id}.jsonl"

        if not source.exists():
            return None

        dest_dir = self.config.home / "logs" / "conversations"
        dest_dir.mkdir(parents=True, exist_ok=True)

        today = date.today().strftime("%Y-%m-%d")
        dest = self._dedup_path(dest_dir / f"{today}-{self.agent_id}.jsonl")
        shutil.copy2(source, dest)
        return str(dest)

    def get_prior_conversation_jsonl(self) -> "Path | None":
        """Return the JSONL path for the resumed session's conversation, or None.

        Used by the TUI to render prior message history when resuming.
        Only returns a path if (a) this session was started as a resume/continue
        and (b) the JSONL file actually exists on disk.
        """
        if not self._resume_uuid:
            return None
        cwd = self.config.project or safe_getcwd()
        cwd = str(Path(cwd).resolve())
        project_dir_name = cwd.replace("/", "-").replace(".", "-")
        path = Path.home() / ".claude" / "projects" / project_dir_name / f"{self._resume_uuid}.jsonl"
        return path if path.exists() else None

    def prepare_shutdown(self) -> None:
        """Push session-end prompts onto followup_queue.

        If config.cleanup is set, formats it with template variables and
        queues it. If cleanup is explicitly empty string, no prompt is sent.
        If cleanup is None (not configured), subclasses can override to
        provide their own session-end behavior.
        """
        for prompt in self._get_cleanup_prompts():
            self.followup_queue.append(prompt)

    def _get_cleanup_prompts(self) -> list[str]:
        """Return formatted cleanup prompts for session end.

        Subclasses can override to provide role-based defaults when
        config.cleanup is None.
        """
        if self.config.cleanup is None:
            return []
        if not self.config.cleanup.strip():
            return []
        return [self.config.cleanup.format(**self._template_vars())]

    def _cleanup_channel_subscriptions(self) -> None:
        """Remove this agent from all channel subscriptions on exit.

        Returns early on corrupt JSON — can't safely assume empty when the
        intent is removal (vs _restore which can safely start from {}).
        """
        channels_path = self.config.home / "channels.json"
        if not channels_path.exists():
            return
        try:
            with open(channels_path, "a+") as f:
                fcntl.flock(f, fcntl.LOCK_EX)
                f.seek(0)
                try:
                    channels = json.loads(f.read() or "{}")
                except json.JSONDecodeError:
                    return
                changed = False
                for channel in list(channels):
                    subs = channels[channel]
                    if self.agent_id in subs:
                        subs.remove(self.agent_id)
                        changed = True
                    if not subs:
                        del channels[channel]
                if changed:
                    f.seek(0)
                    f.truncate()
                    f.write(json.dumps(channels, indent=2) + "\n")
        except OSError:
            pass  # Best-effort — don't block shutdown

    def _snapshot_session_state(self) -> None:
        """Update the session state file with final config and channel subscriptions."""
        path = self._session_state_path
        if not path.exists():
            return
        state = self._load_session_state() or {}
        if self.session_config:
            state["session_config"] = self.session_config.all
        state["channel_subscriptions"] = self._snapshot_channel_subscriptions()
        tmp = path.with_suffix(".tmp")
        try:
            tmp.write_text(yaml.dump(state, Dumper=_BlockDumper, default_flow_style=False, sort_keys=False, allow_unicode=True))
            tmp.rename(path)
        except OSError:
            pass

    async def stop(self):
        """Disconnect the agent session and clean up resources."""
        self._snapshot_session_state()
        self._cleanup_channel_subscriptions()
        if self._shell_cleanup:
            await self._shell_cleanup()
            self._shell_cleanup = None
        if self._client:
            await self._client.disconnect()
            self._client = None
        if self._stderr_fh:
            self._stderr_fh.close()
            self._stderr_fh = None
        if self._stderr_log and self._stderr_log.exists() and self._stderr_log.stat().st_size == 0:
            self._stderr_log.unlink()
            self._stderr_log = None
        if self.session_config:
            self._continuation_state = self.session_config.all
            self.session_config.cleanup()

    async def __aenter__(self):
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.stop()
        return False
