"""Default harness — batteries-included session manager for simple agents.

Reads an agent spec (agent.yml), assembles the system prompt, wires
infrastructure hooks, and manages the session lifecycle. This is what
`kiln run <agent>` uses.

Complex agents write their own harness that imports kiln's building
blocks directly.
"""

import json
import logging
import os
import shutil
import subprocess
from datetime import date, datetime
from pathlib import Path

import yaml

log = logging.getLogger("kiln.harness")

from claude_agent_sdk import HookMatcher

from .backends.claude import ClaudeBackend
from .daemon.client import DaemonClient, DaemonUnavailableError
from .guardrails import detect_role_injection
from .types import (
    Backend,
    BackendConfig,
    ContentBlock,
    ContentBlockDeltaEvent,
    ContentBlockStartEvent,
    DocumentContent,
    ErrorEvent,
    HookDispatcher,
    HookRule,
    TextContent,
    TextEvent,
    ToolDef,
    TurnCompleteEvent,
)

from .config import AgentConfig
from .hooks import (
    create_session_state_hook,

    create_inbox_check_hook,
    create_message_sent_hook,
    create_plan_nudge_hook,
    create_queued_message_hook,
    create_read_tracking_hook,
    create_skill_context_hook,
    create_supplemental_content_hook,
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
from .shell import safe_getcwd  # noqa: F401 — used by subclasses
from .tools import FileState, SessionControl, SupplementalContent, create_mcp_server


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


# ---------------------------------------------------------------------------
# Codex OAuth token refresh
# ---------------------------------------------------------------------------

_CODEX_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
_CODEX_TOKEN_URL = "https://auth.openai.com/oauth/token"
_CODEX_REFRESH_MARGIN = 300  # refresh if expiring within 5 minutes


def _decode_jwt_exp(token: str) -> float | None:
    """Extract expiry timestamp from a JWT without validating signature."""
    import base64
    try:
        payload = token.split(".")[1]
        # Pad to multiple of 4
        payload += "=" * (4 - len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
        return claims.get("exp")
    except Exception:
        return None


def _refresh_codex_token_if_needed(data: dict, auth_path: Path) -> str | None:
    """Check Codex OAuth token expiry and refresh if needed.

    Returns a valid access_token, or None if refresh fails.
    """
    import time
    import urllib.request
    import urllib.parse

    tokens = data.get("tokens", {})
    access_token = tokens.get("access_token", "")
    refresh_token = tokens.get("refresh_token", "")

    if not access_token:
        return None

    # Check if token is still valid
    exp = _decode_jwt_exp(access_token)
    if exp and exp > time.time() + _CODEX_REFRESH_MARGIN:
        log.debug("Codex token valid until %s", datetime.fromtimestamp(exp))
        return access_token

    # Token expired or expiring soon — refresh
    if not refresh_token:
        log.warning("Codex token expired but no refresh_token available")
        return None

    log.info("Codex OAuth token expired — refreshing")
    try:
        body = urllib.parse.urlencode({
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": _CODEX_CLIENT_ID,
        }).encode()
        req = urllib.request.Request(
            _CODEX_TOKEN_URL, data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read())

        new_access = result.get("access_token")
        if not new_access:
            log.warning("Codex refresh returned no access_token")
            return None

        # Update tokens in auth.json
        tokens["access_token"] = new_access
        if result.get("refresh_token"):
            tokens["refresh_token"] = result["refresh_token"]
        if result.get("id_token"):
            tokens["id_token"] = result["id_token"]
        data["last_refresh"] = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.%fZ")

        tmp = auth_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        tmp.rename(auth_path)
        log.info("Codex OAuth token refreshed successfully")
        return new_access

    except Exception as e:
        log.warning("Codex token refresh failed: %s", e)
        # Return the old token — it might still work for a few seconds
        return access_token if exp and exp > time.time() else None


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
        self._backend: Backend | None = None
        self._expected_model = resolve_model(config.model)
        self._model_verified = False
        self._permission_hook = None
        self._permission_callbacks = None
        self._initial_mode = PermissionMode(config.initial_mode) if config.initial_mode else PermissionMode.SUPERVISED
        self._daemon_client: DaemonClient | None = None
        self._desired_subscriptions: list[str] = []
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
        self.session_config: SessionConfig | None = None  # created in _build_backend_config
        self._resume_uuid: str | None = None  # set in _build_backend_config if resuming
        self._resume_transcript: str | None = None  # custom backend JSONL path for resume
        self._transcript_path: str | None = None  # live transcript path (custom backend)
        self._worklog_path = self._resolve_worklog_path()

        # Spawned subagents default to yolo — no human watching
        if self.config.parent and self.config.initial_mode is None:
            self.config.initial_mode = "yolo"

    @property
    def permission_mode(self) -> PermissionMode:
        """Current permission mode. Reads from session config so external
        changes (daemon control channel, other tools) take effect on next
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
        extras = {}
        # For custom backend sessions, store the transcript path so
        # resume can locate the JSONL without a Claude session_uuid.
        if self._transcript_path:
            extras["transcript_path"] = self._transcript_path
        register_session(
            self._registry_path,
            self.agent_id,
            cwd=str(self.config.home),
            model=self.config.model,
            session_uuid=self.session_id,
            extras=extras or None,
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
        context_tokens: int | None = None,
    ) -> None:
        state: dict = {"system_prompt": system_prompt}
        if session_config is not None:
            state["session_config"] = session_config
        if channel_subscriptions is not None:
            state["channel_subscriptions"] = channel_subscriptions
        if context_tokens is not None:
            state["context_tokens"] = context_tokens

        if self.config.template:
            state["template"] = self.config.template
        if self.config.template_vars:
            state["template_vars"] = dict(self.config.template_vars)
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
        """Snapshot channel subscriptions for session state persistence.

        Uses the harness's desired subscriptions list, which is kept in
        sync with daemon state on subscribe/unsubscribe operations.
        """
        return list(self._desired_subscriptions)

    async def _restore_channel_subscriptions(self, subscriptions: list[str]) -> None:
        if not subscriptions:
            return
        self._desired_subscriptions = list(subscriptions)
        if self._daemon_client:
            try:
                await self._daemon_client.restore_subscriptions(subscriptions)
            except DaemonUnavailableError:
                log.debug("Daemon unavailable — subscriptions will restore on next use")

    def _on_channel_subscriptions_changed(self, action: str, channel: str) -> None:
        """Keep desired subscription state in sync with tool-level channel changes."""
        if action == "subscribe":
            if channel not in self._desired_subscriptions:
                self._desired_subscriptions.append(channel)
        elif action == "unsubscribe":
            self._desired_subscriptions = [
                ch for ch in self._desired_subscriptions if ch != channel
            ]

    def _cleanup_stale_session_configs(self) -> None:

        """Remove session config files for sessions that are no longer running.

        Catches orphans left behind by crashes or hard kills where stop()
        never ran.  Uses tmux session list as the source of truth for
        what's alive.
        """
        import subprocess

        config_dir = self.config.home / "state"
        prefix = "session-config-"
        try:
            result = subprocess.run(
                ["tmux", "list-sessions", "-F", "#{session_name}"],
                capture_output=True, text=True, timeout=5,
            )
            live = set(result.stdout.strip().splitlines()) if result.returncode == 0 else set()
        except Exception:
            return  # Can't determine live sessions — skip cleanup

        for path in config_dir.glob(f"{prefix}*.yml"):
            agent_id = path.stem.removeprefix(prefix)
            if agent_id != self.agent_id and agent_id not in live:
                try:
                    path.unlink()
                except OSError:
                    pass

    # -- Options builder ----------------------------------------------------

    def _select_backend(self) -> Backend:
        """Choose backend based on config."""
        from .config import infer_backend
        backend_name = infer_backend(self.config.model)
        if backend_name == "claude":
            return ClaudeBackend()
        elif backend_name == "openai":
            from .backends.custom import CustomBackend
            from .providers.openai_responses import OpenAIResponsesProvider
            api_key, base_url = self._resolve_openai_auth()
            provider = OpenAIResponsesProvider(
                api_key=api_key, base_url=base_url, session_id=self.agent_id,
            )
            return CustomBackend(provider)
        raise ValueError(f"Unknown backend: {backend_name}")

    # Codex OAuth tokens use the ChatGPT backend, not the standard API.
    _CODEX_BASE_URL = "https://chatgpt.com/backend-api/codex"

    def _resolve_openai_auth(self) -> tuple[str, str | None]:
        """Resolve OpenAI credentials. Returns (api_key, base_url).

        Priority order:
        1. Codex CLI OAuth token (~/.codex/auth.json) — zero-config default.
           Uses the ChatGPT backend endpoint (not api.openai.com) since Codex
           OAuth tokens lack the api.responses.write scope.
        2. OPENAI_API_KEY env var — standard API endpoint.
        3. credentials/OPENAI_API_KEY file — standard API endpoint.
        """
        # 1. Codex CLI OAuth — routes to ChatGPT backend
        codex_auth = Path.home() / ".codex" / "auth.json"
        if codex_auth.exists():
            try:
                data = json.loads(codex_auth.read_text())
                tokens = data.get("tokens", {})
                if isinstance(tokens, dict):
                    access_token = tokens.get("access_token", "")
                    if access_token:
                        access_token = _refresh_codex_token_if_needed(
                            data, codex_auth,
                        )
                        if access_token:
                            return access_token, self._CODEX_BASE_URL
            except (json.JSONDecodeError, OSError) as e:
                log.warning("Failed to read Codex auth: %s", e)

        # 2. Environment variable — standard API
        api_key = os.environ.get("OPENAI_API_KEY", "")
        if api_key:
            return api_key, None

        # 3. Credentials file — standard API
        creds = self.config.home / "credentials" / "OPENAI_API_KEY"
        if creds.exists():
            api_key = creds.read_text().strip()
            if api_key:
                return api_key, None

        raise RuntimeError(
            "OpenAI backend requires authentication. Options:\n"
            "  1. Run 'codex login' to set up Codex OAuth (recommended)\n"
            "  2. Set OPENAI_API_KEY env var\n"
            "  3. Write key to credentials/OPENAI_API_KEY"
        )

    def _build_backend_config(self) -> BackendConfig:
        """Build backend-agnostic config from agent spec."""
        # Restore saved state if resuming (--resume and --last both set resume_session).
        saved_state = self._load_session_state() if self.config.resume_session else None

        # On self-continuation, carry over channel subscriptions from parent session.
        # The parent's state file has them, but we can't use resume_session (different
        # agent_id, and we don't want to resume the full state — just subscriptions).
        if not saved_state and self.config.continuation and self.config.parent:
            parent_state_path = (
                self.config.home / "logs" / "session-state" / f"{self.config.parent}.yml"
            )
            if parent_state_path.exists():
                try:
                    data = yaml.safe_load(parent_state_path.read_text())
                    if isinstance(data, dict) and data.get("channel_subscriptions"):
                        saved_state = {
                            "channel_subscriptions": data["channel_subscriptions"]
                        }
                except (OSError, yaml.YAMLError):
                    pass

        # Re-apply template from saved state so cleanup, hooks, and other
        # runtime config fields are restored.  CLI --template wins if set.
        if saved_state and not self.config.template:
            saved_template = saved_state.get("template")
            if saved_template:
                try:
                    from .config import apply_template
                    apply_template(self.config, saved_template)
                except FileNotFoundError:
                    log.warning("Saved template '%s' not found — skipping", saved_template)
            saved_vars = saved_state.get("template_vars")
            if saved_vars and not self.config.template_vars:
                self.config.template_vars.update(saved_vars)

        cwd = str(self.config.home)

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
        self._supplemental = SupplementalContent()

        # Per-session runtime config — seeded from harness config, agent-writable.
        # On resume, saved values override harness defaults so state is restored.
        config_defaults = {
            "mode": self._initial_mode.value,
            "heartbeat_enabled": self.config.heartbeat,
            "heartbeat_max": self.config.heartbeat_max,
            "heartbeat_override": self.config.heartbeat_override,
            "stream_timeout": self.config.stream_timeout,
        }
        if saved_state and saved_state.get("session_config"):
            config_defaults.update(saved_state["session_config"])
        self.session_config = SessionConfig(
            path=self.config.home / "state" / f"session-config-{self.agent_id}.yml",
            defaults=config_defaults,
        )

        # Clean up stale session config files from dead sessions
        self._cleanup_stale_session_configs()

        # Record desired subscriptions for async restore in start()
        if saved_state and saved_state.get("channel_subscriptions"):
            self._desired_subscriptions = list(saved_state["channel_subscriptions"])

        # Build infrastructure hooks
        state_dir = self.config.home / "state"
        inbox_check = create_inbox_check_hook(inbox, ui_events=self.ui_events, state_dir=state_dir)
        read_tracker = create_read_tracking_hook(inbox, file_state=file_state)
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
        supplemental_hook = create_supplemental_content_hook(self._supplemental)

        plans_path = self.config.plans_path
        plan_nudge = create_plan_nudge_hook(plans_path / f"{self.agent_id}.yml")

        # Wrap output-producing hooks with terminal visibility if enabled.
        # Silent hooks (read_tracker, usage_log, message_sent) are excluded —
        # they never produce agent-facing output so there's nothing to show.
        if self.config.hook_visibility:
            ui = self.ui_events
            inbox_check = wrap_hook_visibility(inbox_check, "inbox_check", ui)
            queued_messages = wrap_hook_visibility(queued_messages, "queued_messages", ui)
            session_state = wrap_hook_visibility(session_state, "session_state", ui)
            plan_nudge = wrap_hook_visibility(plan_nudge, "plan_nudge", ui)
            skill_context = wrap_hook_visibility(skill_context, "skill_context", ui)
            supplemental_hook = wrap_hook_visibility(supplemental_hook, "supplemental_content", ui)

        hooks = {
            "PostToolUse": [
                HookMatcher(matcher=None, hooks=[
                    inbox_check, queued_messages,
                    session_state, usage_log, plan_nudge,
                ]),
                HookMatcher(matcher="mcp__kiln__Read", hooks=[read_tracker, supplemental_hook]),
                HookMatcher(matcher="mcp__kiln__activate_skill", hooks=[skill_context]),
                HookMatcher(matcher="mcp__kiln__message", hooks=[message_sent]),
            ],
            "Stop": [],
        }

        # Permission handler — always active, even in headless mode.
        # TUI provides interactive callbacks; headless passes no terminal
        # handler (daemon-only for confirm-tier guardrails).
        if self._permission_callbacks:
            get_mode, terminal_handler = self._permission_callbacks
        else:
            get_mode = lambda: self.permission_mode
            terminal_handler = None
        self._permission_handler = PermissionHandler(
            get_mode=get_mode,
            terminal_handler=terminal_handler,
            get_cwd=lambda: self._get_shell_cwd() if self._get_shell_cwd else str(self.config.home),
            agent_id=self.agent_id,
            agent_home=str(self.config.home),
        )
        hooks["PreToolUse"] = [HookMatcher(matcher=None, hooks=[self._permission_handler.hook])]

        # Agent-specific hooks — subclasses override _agent_hooks() to inject.
        for event, matchers in self._agent_hooks().items():
            if event in hooks:
                hooks[event].extend(matchers)
            else:
                hooks[event] = list(matchers)

        # Resolve tools from agent spec
        resolved = self.config.resolve_tools()
        base_tools = resolved.get("Base", ["WebSearch"])

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

        # Build MCP server and extract tool definitions
        mcp_server, self._shell_cleanup, self._get_shell_cwd, mcp_tools = create_mcp_server(
            self.config.inbox_path, self.config.skills_path,
            agent_id=self.agent_id,
            cwd=cwd, env=env, file_state=file_state,
            session_control=self.session_control,
            plans_path=plans_path,
            supplemental=self._supplemental,
            daemon_client=self._daemon_client,
            on_channel_subscriptions_changed=self._on_channel_subscriptions_changed,
        )
        mcp_servers = {"kiln": mcp_server}

        # Backend-agnostic tool definitions from MCP tool instances.
        # ClaudeBackend ignores these; CustomBackend calls handler() directly.
        tool_defs = [
            ToolDef(
                name=t.name,
                description=t.description,
                input_schema=t.input_schema if isinstance(t.input_schema, dict) else {},
                handler=t.handler,
            )
            for t in mcp_tools
        ]

        # Resolve conversation continuity for --resume / --last.
        resume_uuid = None
        resume_transcript: str | None = None
        if self.config.resume_session:
            entry = lookup_session(self._registry_path, self.config.resume_session)
            if not entry:
                raise RuntimeError(
                    f"Cannot resume: no session found for '{self.config.resume_session}'."
                )
            # Claude backend: needs session_uuid for CC SDK resume
            resume_uuid = entry.get("session_uuid")
            # Custom backend: needs transcript path for JSONL-based resume
            resume_transcript = entry.get("transcript_path")
            if not resume_transcript:
                # Infer from deterministic path convention
                candidate = self.config.home / "logs" / "conversations" / "live" / f"{self.config.resume_session}.jsonl"
                if candidate.exists():
                    resume_transcript = str(candidate)
            if not resume_uuid and not resume_transcript:
                raise RuntimeError(
                    f"Cannot resume: no session UUID or transcript found for "
                    f"'{self.config.resume_session}'. "
                    f"The session may have exited before completing its first turn."
                )
            if entry.get("cwd"):
                cwd = entry["cwd"]
        # Expose resume state so the TUI can locate the prior conversation.
        self._resume_uuid = resume_uuid
        self._resume_transcript = resume_transcript

        # Determine transcript path for custom backend sessions.
        # For new sessions: deterministic path under agent home.
        # For resumed sessions: reuse the existing transcript (append to it).
        from .config import infer_backend
        backend_name = infer_backend(self.config.model)
        transcript_path: str | None = None
        if backend_name != "claude":
            if resume_transcript:
                transcript_path = resume_transcript
            else:
                tp = self.config.home / "logs" / "conversations" / "live" / f"{self.agent_id}.jsonl"
                transcript_path = str(tp)
        self._transcript_path = transcript_path

        # Stderr logging
        log_dir = self.config.home / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        self._stderr_log = log_dir / f"stderr-{self.agent_id}.log"
        self._stderr_fh = open(self._stderr_log, "a")

        def _stderr_callback(line: str) -> None:
            self._stderr_fh.write(line)
            self._stderr_fh.flush()

        return BackendConfig(
            system_prompt=full_prompt,
            model=self.config.model,
            mcp_servers=mcp_servers,
            tool_defs=tool_defs,
            hooks=hooks,
            hook_dispatcher=self._build_hook_dispatcher(hooks),
            cwd=cwd,
            env=env,
            effort=self.config.effort,
            temperature=getattr(self.config, "temperature", None),
            max_output_tokens=getattr(self.config, "max_output_tokens", None),
            session_id=self.agent_id,
            resume_conversation_id=resume_uuid,
            transcript_path=transcript_path,
            stream_timeout=self.config.stream_timeout,
            stderr_callback=_stderr_callback,
            supplemental=self._supplemental,
            base_tools=base_tools,
            extra_args={"setting-sources": ""},
        )

    def _agent_hooks(self) -> dict[str, list[HookMatcher]]:
        """Return agent-specific hooks to merge into the infrastructure hooks.

        Subclasses override this to inject custom PostToolUse/PreToolUse hooks
        without reimplementing _build_backend_config. Called after all infra hooks are
        assembled. Returned matchers are appended to the corresponding event lists.

        Returns:
            Dict mapping hook event names to lists of HookMatchers.
        """
        return {}

    @staticmethod
    def _strip_mcp_prefix(name: str) -> str:
        """Strip MCP server namespace prefix: mcp__server__Tool → Tool."""
        if "__" in name:
            return name.rsplit("__", 1)[-1]
        return name

    def _build_hook_dispatcher(self, hooks: dict[str, list]) -> HookDispatcher:
        """Translate CC SDK HookMatcher hooks into a HookDispatcher for CustomBackend.

        HookMatchers use MCP-namespaced patterns (mcp__kiln__Read) but
        CustomBackend tools have short names (Read). Patterns are stripped
        during translation so HookRule.matches() works correctly.
        """
        pre_rules: list[HookRule] = []
        post_rules: list[HookRule] = []

        for matcher in hooks.get("PreToolUse", []):
            pattern = self._strip_mcp_prefix(matcher.matcher) if matcher.matcher else None
            for hook in matcher.hooks:
                pre_rules.append(HookRule(pattern=pattern, hook=hook))

        for matcher in hooks.get("PostToolUse", []):
            pattern = self._strip_mcp_prefix(matcher.matcher) if matcher.matcher else None
            for hook in matcher.hooks:
                post_rules.append(HookRule(pattern=pattern, hook=hook))

        return HookDispatcher(pre_tool_hooks=pre_rules, post_tool_hooks=post_rules)

    def _template_vars(self) -> dict[str, str]:
        """Build template variable dict for orientation and cleanup messages.

        Base vars: {agent_id}, {today}, {now}, {summary_path}.
        Config vars (from --var flags or programmatic assignment) are merged on
        top. Subclasses can override to add agent-specific variables.
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

        result = dict(
            agent_id=self.agent_id,
            today=today,
            now=now,
            summary_path=str(summary_path),
        )
        # Merge extra vars from config (CLI --var, programmatic assignment)
        result.update(self.config.template_vars)
        return result

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

    def _create_daemon_client(self) -> None:
        """Create a stateless daemon client for this session."""
        self._daemon_client = DaemonClient(
            agent=self.config.name,
            session=self.agent_id,
        )

    async def start(self):
        """Start the agent session.

        Queues orientation message (if configured) onto followup_queue.
        If --prompt is also set, it's delivered as an inbox message so the
        agent discovers it naturally during orientation (via inbox_check hook).
        If no orientation, --prompt is the startup message on followup_queue.
        """
        self._run_startup_commands()

        # Create stateless daemon client before building config so message tool has it
        self._create_daemon_client()

        self._backend = self._select_backend()
        config = self._build_backend_config()
        await self._backend.start(config)
        self.register_session()

        # Restore channel subscriptions from previous session (async, needs daemon)
        if self._desired_subscriptions:
            await self._restore_channel_subscriptions(self._desired_subscriptions)

        # Queue startup messages onto followup_queue (programmatic user turns).
        # steering_queue is for user-typed mid-turn input only.
        # Skip orientation on resume — the prior conversation already has it.
        is_resume = bool(self._resume_uuid or self._resume_transcript)
        orientation = self._build_orientation()
        if orientation and not is_resume:
            self.followup_queue.append(orientation)

        if self.config.prompt:
            if orientation and not is_resume:
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
            session_prefix=self.config.session_prefix,
        )

    def session_state_labels(self) -> list[str]:
        """Extra labels for the session state hook. Override in subclasses."""
        labels = []
        if self._desired_subscriptions:
            labels.append(f"Channels: {', '.join(self._desired_subscriptions)}")
        return labels

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

    async def send(self, message: str | list[ContentBlock]):
        """Send a user message to the agent."""
        if not self._backend:
            raise RuntimeError("Harness not started. Call start() first.")
        await self._backend.send(message)

    async def receive(self):
        """Yield Kiln Events from the backend until the turn ends.

        Scans assistant text output for role injection (fabricated Human
        turns).  If detected, interrupts generation immediately to prevent
        tool calls from executing on fabricated input.

        After each receive cycle, checks for pending supplemental content
        (e.g. PDF document blocks). If found, transparently injects it as
        a new user turn and continues yielding.

        Stream timeout is handled by the backend. If the backend yields an
        ErrorEvent(is_retryable=True), queues a recovery message so the
        agent gets another turn automatically.
        """
        if not self._backend:
            raise RuntimeError("Harness not started. Call start() first.")

        async for event in self._receive_guarded():
            yield event

        # Transparent supplemental content injection.
        while self._supplemental and self._supplemental.has_pending:
            items = self._supplemental.drain()
            rich_blocks = self._build_supplemental_blocks(items)
            await self._backend.send(rich_blocks)
            async for event in self._receive_guarded():
                yield event

    async def _receive_guarded(self):
        """Yield events from the backend with output guardrail scanning.

        Accumulates streaming text deltas per content block and runs
        detect_role_injection() on the buffer.  On detection: interrupts
        the backend (cancelling pending tool calls), yields an ErrorEvent,
        and stops.

        Also scans complete TextEvents as a fallback — streaming deltas
        aren't always available (e.g. include_partial_messages=False).
        """
        # Per-block text accumulator for streaming detection.
        text_buf = ""
        in_text_block = False
        injection_detected = False

        async for event in self._backend.receive():
            # Retryable errors → queue recovery, skip.
            if isinstance(event, ErrorEvent) and event.is_retryable:
                self.followup_queue.append(
                    "[SYSTEM] The previous model generation stalled — "
                    + event.message
                    + " Your partial response (if any) is preserved in "
                    "context above. Please continue where you left off."
                )
                continue

            # Track text block boundaries for streaming accumulation.
            if isinstance(event, ContentBlockStartEvent):
                if event.content_type == "text":
                    in_text_block = True
                    text_buf = ""
                else:
                    in_text_block = False

            # Streaming text — accumulate and scan.
            if isinstance(event, ContentBlockDeltaEvent) and in_text_block and event.text:
                text_buf += event.text
                # Only scan the leading portion — role injection happens at or
                # near the start.  Cap buffer to avoid waste on long outputs.
                if not injection_detected and len(text_buf) <= 500:
                    desc = detect_role_injection(text_buf)
                    if desc:
                        injection_detected = True
                        log.warning(
                            "\033[1;31mROLE INJECTION DETECTED: %s\033[0m", desc
                        )
                        await self._backend.interrupt()
                        yield ErrorEvent(
                            message=(
                                f"ROLE INJECTION DETECTED — {desc}. "
                                "Generation interrupted. The model attempted to "
                                "fabricate a Human turn in its output. Tool calls "
                                "from this turn have been cancelled."
                            ),
                            is_retryable=False,
                        )
                        return

            # Complete text block — fallback scan for non-streaming paths.
            if isinstance(event, TextEvent) and not injection_detected:
                desc = detect_role_injection(event.text)
                if desc:
                    injection_detected = True
                    log.warning(
                        "\033[1;31mROLE INJECTION DETECTED: %s\033[0m", desc
                    )
                    await self._backend.interrupt()
                    yield ErrorEvent(
                        message=(
                            f"ROLE INJECTION DETECTED — {desc}. "
                            "Generation interrupted. The model attempted to "
                            "fabricate a Human turn in its output. Tool calls "
                            "from this turn have been cancelled."
                        ),
                        is_retryable=False,
                    )
                    return

            yield event

    def _build_supplemental_blocks(self, items: list[dict]) -> list[ContentBlock]:
        """Convert pending supplemental content to backend-agnostic ContentBlocks."""
        blocks: list[ContentBlock] = []
        labels = []
        for item in items:
            labels.append(item.get("label", "file"))
            blocks.append(DocumentContent(
                data=item["data"],
                mime_type=item["mime_type"],
                label=item.get("label", "file"),
            ))
        blocks.append(TextContent(
            text=f"Document content loaded for: {', '.join(labels)}. Continue with your task.",
        ))
        return blocks

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
        if self._backend:
            await self._backend.interrupt()

    async def force_stop(self):
        """Force-kill the backend."""
        if self._shell_cleanup:
            await self._shell_cleanup()
            self._shell_cleanup = None
        if self._backend:
            await self._backend.stop()
            self._backend = None
        if self.session_config:
            self.session_config.cleanup()

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
        """Copy conversation JSONL to agent logs. Returns path or None.

        Claude backend: copies from ~/.claude/projects/<encoded-cwd>/<uuid>.jsonl
        Custom backend: copies from the live transcript path (already durable).
        """
        dest_dir = self.config.home / "logs" / "conversations"
        dest_dir.mkdir(parents=True, exist_ok=True)
        today = date.today().strftime("%Y-%m-%d")

        # Custom backend: live transcript is the source of truth
        if self._transcript_path:
            source = Path(self._transcript_path)
            if not source.exists():
                return None
            dest = self._dedup_path(dest_dir / f"{today}-{self.agent_id}.jsonl")
            shutil.copy2(source, dest)
            return str(dest)

        # Claude backend: copy from CC's storage
        if not self.session_id:
            return None
        cwd = str(self.config.home.resolve())
        project_dir_name = cwd.replace("/", "-").replace(".", "-")
        source = Path.home() / ".claude" / "projects" / project_dir_name / f"{self.session_id}.jsonl"
        if not source.exists():
            return None
        dest = self._dedup_path(dest_dir / f"{today}-{self.agent_id}.jsonl")
        shutil.copy2(source, dest)
        return str(dest)

    def get_prior_conversation_jsonl(self) -> "Path | None":
        """Return the JSONL path for the resumed session's conversation, or None.

        Used by the TUI to render prior message history when resuming.
        Only returns a path if (a) this session was started as a resume/continue
        and (b) the JSONL file actually exists on disk.

        Custom backend: uses the transcript path directly.
        Claude backend: looks in ~/.claude/projects/<encoded-cwd>/<uuid>.jsonl
        """
        # Custom backend: transcript path is the JSONL
        if self._resume_transcript:
            path = Path(self._resume_transcript)
            return path if path.exists() else None

        # Claude backend: look up in CC's storage
        if not self._resume_uuid:
            return None
        cwd = str(self.config.home.resolve())
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

    # No daemon deregistration needed — tmux reconciliation handles cleanup.

    def _snapshot_session_state(self) -> None:
        """Update the session state file with final config, channels, and context."""

        path = self._session_state_path
        if not path.exists():
            return
        state = self._load_session_state() or {}
        if self.session_config:
            state["session_config"] = self.session_config.all
        state["channel_subscriptions"] = self._snapshot_channel_subscriptions()
        if self.session_control and self.session_control.context_tokens > 0:
            state["context_tokens"] = self.session_control.context_tokens

        tmp = path.with_suffix(".tmp")
        try:
            tmp.write_text(yaml.dump(state, Dumper=_BlockDumper, default_flow_style=False, sort_keys=False, allow_unicode=True))
            tmp.rename(path)
        except OSError:
            pass

    def persist_live_session_state(self) -> None:
        """Persist the current session state so external status surfaces can read it live."""
        self._snapshot_session_state()

    async def stop(self):
        """Disconnect the agent session and clean up resources."""
        self._snapshot_session_state()

        if self._shell_cleanup:
            await self._shell_cleanup()
            self._shell_cleanup = None
        if self._backend:
            await self._backend.stop()
            self._backend = None
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
