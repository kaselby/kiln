"""Default harness — batteries-included session manager for simple agents.

Reads an agent spec (agent.yml), assembles the system prompt, wires
infrastructure hooks, and manages the session lifecycle. This is what
`kiln run <agent>` uses.

Complex agents write their own harness that imports kiln's building
blocks directly. See the Aleph example for that pattern.
"""

import json
import os
import shutil
from datetime import date, datetime
from pathlib import Path

from claude_agent_sdk import (
    ClaudeAgentOptions,
    ClaudeSDKClient,
    HookMatcher,
)

from .config import AgentConfig
from .hooks import (
    create_active_agents_hook,
    create_context_warning_hook,
    create_inbox_check_hook,
    create_message_sent_hook,
    create_queued_message_hook,
    create_read_tracking_hook,
    create_skill_context_hook,
    create_usage_log_hook,
)
from .names import generate_agent_name
from .permissions import create_permission_hook
from .prompt import (
    build_session_context,
    discover_skills,
    discover_tools,
    resolve_model,
)
from .registry import lookup_session, register_session
from .shell import safe_getcwd
from .tools import FileState, SessionControl, create_mcp_server


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
        self.agent_id = config.agent_id or generate_agent_name(
            prefix=config.session_prefix.rstrip("-"),
            ephemeral=config.ephemeral,
            worklogs_dir=config.worklogs_path,
        )
        self.session_id: str | None = None
        self._client: ClaudeSDKClient | None = None
        self._expected_model = resolve_model(config.model)
        self._model_verified = False
        self._permission_hook = None
        self._permission_callbacks = None
        self._shell_cleanup = None
        self._get_shell_cwd = None
        self._stderr_log: Path | None = None
        self._stderr_fh = None
        self.session_control: SessionControl | None = None
        self.restart_requested = False
        self.continue_requested = False
        self.handoff_text: str | None = None
        self.user_message_queue: list[str] = []
        self.ui_events: list[dict] = []
        self._worklog_path = self._resolve_worklog_path()

        # Spawned subagents default to yolo — no human watching
        if self.config.parent and self.config.initial_mode is None:
            self.config.initial_mode = "yolo"

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

    def _build_options(self) -> ClaudeAgentOptions:
        """Build ClaudeAgentOptions from config."""
        # Load identity document
        identity = self.config.load_identity()

        # Build session context
        cwd = self.config.project or safe_getcwd()
        custom_tools = discover_tools(self.config.tools_path)
        skills = discover_skills(self.config.skills_path)

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

        # Load context injection files
        context_parts = []
        for label, content in self.config.load_context_files():
            context_parts.append(f"\n\n---\n## {label}\n\n{content}")

        full_prompt = identity + session_ctx + "".join(context_parts)

        # Save/restore system prompt for faithful resume
        prompt_store = self.config.home / "logs" / "system-prompts"
        prompt_store.mkdir(parents=True, exist_ok=True)
        is_resuming = self.config.continue_session or self.config.resume_session
        if is_resuming:
            saved = prompt_store / f"{self.config.resume_session or self.agent_id}.txt"
            if saved.exists():
                full_prompt = saved.read_text()
        else:
            (prompt_store / f"{self.agent_id}.txt").write_text(full_prompt)

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
        self.session_control = SessionControl(ephemeral=self.config.ephemeral)

        # Build infrastructure hooks
        inbox_check = create_inbox_check_hook(inbox, ui_events=self.ui_events)
        read_tracker = create_read_tracking_hook(inbox, file_state=file_state)
        context_warning = create_context_warning_hook(self.session_control)
        active_agents = create_active_agents_hook(
            interval=15,
            channels_path=self.config.home / "channels.json",
            session_prefix=self.config.session_prefix,
        )
        skill_context = create_skill_context_hook(self.config.skills_path)
        usage_log = create_usage_log_hook(
            self.config.home / "logs", self.agent_id,
            self.config.tools_path / "bin",
        )
        queued_messages = create_queued_message_hook(
            self.user_message_queue, self.ui_events,
        )
        message_sent = create_message_sent_hook(self.ui_events)

        hooks = {
            "PostToolUse": [
                HookMatcher(matcher=None, hooks=[
                    inbox_check, queued_messages, context_warning,
                    active_agents, usage_log,
                ]),
                HookMatcher(matcher="Read", hooks=[read_tracker]),
                HookMatcher(matcher="mcp__kiln__Read", hooks=[read_tracker]),
                HookMatcher(matcher="mcp__kiln__activate_skill", hooks=[skill_context]),
                HookMatcher(matcher="mcp__kiln__message", hooks=[message_sent]),
            ],
            "Stop": [],
        }

        # Permission hook
        if self._permission_callbacks:
            get_mode, request_permission = self._permission_callbacks
            perm_hook = create_permission_hook(
                get_mode=get_mode,
                request_permission=request_permission,
                get_cwd=lambda: self._get_shell_cwd() if self._get_shell_cwd else safe_getcwd(),
            )
            hooks["PreToolUse"] = [HookMatcher(matcher=None, hooks=[perm_hook])]

        # Resolve tools from agent spec
        resolved = self.config.resolve_tools()
        base_tools = resolved.get("Base", ["Read", "WebSearch"])

        # Environment
        venv_path = self.config.home / "venv"
        tools_dir = str(self.config.tools_path)
        base_path = os.environ.get("PATH", "")
        env = {
            "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
            "CLAUDE_CODE_DISABLE_FILE_CHECKPOINTING": "1",
            "KILN_AGENT_HOME": str(self.config.home),
            "KILN_AGENT_ID": self.agent_id,
        }
        if venv_path.exists():
            venv_bin = venv_path / "bin"
            env["VIRTUAL_ENV"] = str(venv_path)
            env["PATH"] = f"{tools_dir}:{venv_bin}:{base_path}"
        else:
            env["PATH"] = f"{tools_dir}:{base_path}"

        # Build MCP server
        plans_path = self.config.plans_path
        mcp_server, self._shell_cleanup, self._get_shell_cwd = create_mcp_server(
            self.config.inbox_path, self.config.skills_path,
            agent_id=self.agent_id,
            cwd=cwd, env=env, file_state=file_state,
            session_control=self.session_control,
            plans_path=plans_path,
        )
        mcp_servers = {"kiln": mcp_server}

        # Resolve resume
        resume_uuid = None
        if self.config.resume_session:
            entry = lookup_session(self._registry_path, self.config.resume_session)
            if not entry:
                raise RuntimeError(
                    f"Cannot resume: no session found for '{self.config.resume_session}'."
                )
            resume_uuid = entry["session_uuid"]
            if entry.get("cwd"):
                cwd = entry["cwd"]

        # Stderr logging
        log_dir = self.config.home / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        self._stderr_log = log_dir / f"stderr-{self.agent_id}.log"
        self._stderr_fh = open(self._stderr_log, "a")

        def _stderr_callback(line: str) -> None:
            self._stderr_fh.write(line)
            self._stderr_fh.flush()

        return ClaudeAgentOptions(
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
            continue_conversation=self.config.continue_session,
            resume=resume_uuid,
            stderr=_stderr_callback,
        )

    async def start(self):
        """Start the agent session."""
        options = self._build_options()
        self._client = ClaudeSDKClient(options)
        self.register_session()
        await self._client.connect()

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
        return (
            f"Model mismatch: expected '{self._expected_model}' "
            f"but got '{actual_model}'. Update MODEL_ALIASES in prompt.py."
        )

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
        dest = dest_dir / f"{today}-{self.agent_id}.jsonl"
        shutil.copy2(source, dest)
        return str(dest)

    async def stop(self):
        """Disconnect the agent session and clean up resources."""
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

    async def __aenter__(self):
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.stop()
        return False
