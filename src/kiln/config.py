"""Agent configuration and spec loading."""

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml


# Tool namespaces. Tools in the agent spec are namespaced as
# "Source::ToolName" to make the source explicit.
#
#   Base::<name>    — Claude Code built-in tools (passed via SDK --tools flag).
#                     SDK treats tools=[] as "no built-ins"; tools=None as
#                     "CC default set". Kiln always passes an explicit list.
#   Kiln::<name>    — Kiln's standard MCP server tools
#   <Agent>::<name> — Agent's custom MCP server tools
#
NAMESPACE_KILN = "Kiln"

# Authoritative set of tools served by Kiln's built-in MCP server.
# The MCP server registration in tools.py:create_mcp_server must match this.
# Used by resolve_tools() to fail loud when an agent.yml references a tool
# that Kiln doesn't expose (e.g. a stale snake_case name).
KILN_TOOL_NAMES = frozenset({
    "Bash", "Read", "Write", "Edit",
    "Plan", "Message", "ActivateSkill", "ExitSession",
})

# Renamed built-in tools (0.2 → 0.3). Mapping from old snake_case name to
# new CamelCase name, used to produce a clear error message when an
# agent.yml still references the old name.
_RENAMED_KILN_TOOLS = {
    "plan": "Plan",
    "message": "Message",
    "activate_skill": "ActivateSkill",
    "exit_session": "ExitSession",
}

# Default tool set when agent spec doesn't specify.
DEFAULT_TOOLS = [
    "Kiln::Bash",
    "Kiln::Read",
    "Kiln::Write",
    "Kiln::Edit",
    "Kiln::Message",
    "Kiln::Plan",
    "Kiln::ExitSession",
    "Kiln::ActivateSkill",
]


@dataclass
class AgentConfig:
    """Configuration for a single agent session.

    Can be constructed directly or loaded from an agent spec file (agent.yml).
    """

    # Agent identity
    name: str = ""                    # agent name (e.g. "assistant")
    owner_name: str = "User"          # display name for the agent's owner (used in presence labels etc.)
    home: Path = field(default_factory=lambda: Path.home())
    identity_doc: str = "identity.md"  # relative to home

    # Session
    agent_id: str | None = None       # unique session ID (generated if None)
    model: str | None = None          # model name or alias

    # Spawning hierarchy
    parent: str | None = None
    depth: int = 0
    persistent: bool = False
    continuation: bool = False

    # Session lifecycle
    resume_session: str | None = None
    prompt: str | None = None

    # Session messages — orientation (startup) and cleanup (shutdown) prompts.
    # Both support template variables: {agent_id}, {today}, {now}, {summary_path}.
    # Set to empty string to explicitly suppress (e.g. cleanup: "" disables
    # session-end prompts even if a subclass would normally provide them).
    orientation: str | None = None    # startup message (first user turn)
    cleanup: str | None = None        # session-end prompt

    # Permission mode
    initial_mode: str | None = None   # safe, supervised, yolo (trusted is TUI-only)

    # Thinking effort — controls depth of reasoning ("low", "medium", "high")
    # None means use the SDK/CLI default.
    effort: str | None = None

    # Backend selection — "claude" (default), "openai", "openai-compat", "litellm".
    # None means infer from model name. Explicit setting overrides inference.


    # Heartbeat interval in seconds (0 = disabled)
    heartbeat: float = 0.0

    # Idle nudge: send a message after prolonged inactivity (seconds, 0 = disabled)
    idle_nudge_timeout: float = 0.0


    # Stream stall timeout: if no SDK message arrives for this many seconds during
    # a model turn, interrupt the stalled generation and auto-retry. Mitigates
    # Claude Code bug where API streaming connections stall silently (CC #25979).
    # 0 = disabled.
    stream_timeout: float = 0.0

    # Context limit — behavioral ceiling on context tokens.
    #   "off"  — no enforcement; nothing fires.
    #   "soft" — at the limit: warn in TUI, disable auto-firing (inbox,
    #            heartbeat, idle-nudge), interrupt current turn, ping owner
    #            on Discord. User/agent can still interact.
    #   "hard" — same as soft, plus refuse to send further turns.
    # Default applies regardless of the model's true max context — raising
    # the cap is an explicit opt-in. Both values are runtime-mutable via
    # session_config.
    context_limit_mode: str = "soft"
    context_limit_tokens: int = 200_000

    # Steering delivery mode — how queued user-typed mid-turn input is drained.
    #   "all"           — inject all queued messages at once as a single user turn (default)
    #   "one-at-a-time" — drip-feed: deliver one message per injection cycle
    # Runtime-mutable via session_config.
    steering_delivery: str = "all"

    # Tools — namespaced list: "Base::Read", "Kiln::Bash", "MyAgent::CustomTool"
    tools: list[str] = field(default_factory=lambda: list(DEFAULT_TOOLS))
    mcp_server: str | None = None     # path to custom MCP server module (relative to home)
    scripts_dir: str = "tools"        # shell tools directory (relative to home)

    # Context injection — files to include in the system prompt.
    # Each entry is either a plain path string or a dict with 'path' and
    # optional 'label' (used as the section header instead of the path).
    context_injection: list[str | dict] = field(default_factory=list)

    # Skills
    skills_dir: str = "skills"        # relative to home

    # Memory / worklogs / sessions
    worklogs_dir: str = "memory/worklogs"   # relative to home
    sessions_dir: str = "memory/sessions"   # relative to home

    # Startup commands — run as subprocesses before session begins.
    # Each entry is a shell command string. Runs with agent's env (AGENT_HOME, tools on PATH).
    # Failures log warnings but don't block the session.
    startup: list[str] = field(default_factory=list)

    # Hooks — agent-defined configuration for hook behavior.
    # The default harness reads this to configure infrastructure hooks.
    # Custom harnesses may read it, extend it, or ignore it entirely.
    # Keys and values are agent-defined — kiln imposes no schema.
    hooks: dict[str, dict] = field(default_factory=dict)

    # TUI: show hook firings in terminal when they produce output (opt-in)
    hook_visibility: bool = False

    # Tmux session prefix for agent naming
    session_prefix: str = "kiln-"

    # CLI binary for agents with custom harnesses.
    # When set, `kiln run <agent>` execs to this binary instead of running
    # in-process with stock KilnHarness.
    cli: str | None = None

    # Inbox / messaging
    inbox_dir: str = "inbox"          # relative to home

    # Plans
    plans_dir: str = "plans"          # relative to home

    # Session template — set automatically by apply_template().
    # Persisted in session state so resume and continuation can re-apply.
    template: str | None = None

    # Extra template variables for orientation/cleanup formatting.
    # Merged into _template_vars() at format time. CLI --var and
    # programmatic config.template_vars["key"] = "value" both land here.
    template_vars: dict[str, str] = field(default_factory=dict)

    # Startup/default tags for the live session-config file. These seed the
    # session's own mutable state at launch time.
    tags: list[str] = field(default_factory=list)

    # --- Derived paths ---


    @property
    def identity_path(self) -> Path:
        return self.home / self.identity_doc

    @property
    def inbox_path(self) -> Path:
        return self.home / self.inbox_dir

    @property
    def tools_path(self) -> Path:
        return self.home / self.scripts_dir

    @property
    def skills_path(self) -> Path:
        return self.home / self.skills_dir

    @property
    def scratch_path(self) -> Path:
        return self.home / "scratch"

    @property
    def worklogs_path(self) -> Path:
        return self.home / self.worklogs_dir

    @property
    def sessions_path(self) -> Path:
        return self.home / self.sessions_dir

    @property
    def plans_path(self) -> Path:
        return self.home / self.plans_dir

    def agent_inbox(self, agent_id: str) -> Path:
        return self.inbox_path / agent_id

    def load_identity(self) -> str:
        """Load the agent's identity document."""
        path = self.identity_path
        if path.exists():
            return path.read_text()
        return ""

    def load_context_files(self) -> list[tuple[str, str]]:
        """Load all context injection files.

        Returns list of (label, content) tuples. Files that don't exist
        are silently skipped. Each entry in context_injection is either a
        plain path string or a dict with 'path' and optional 'label'.
        """
        results = []
        for entry in self.context_injection:
            if isinstance(entry, dict):
                rel_path = entry.get("path", "")
                label = entry.get("label", rel_path)
            else:
                rel_path = entry
                label = rel_path
            if not rel_path:
                continue
            full_path = self.home / rel_path
            if full_path.exists():
                try:
                    results.append((label, full_path.read_text()))
                except OSError:
                    continue
        return results

    def resolve_mcp_server_path(self) -> Path | None:
        """Resolve the custom MCP server module path, or None for default."""
        if not self.mcp_server:
            return None
        return self.home / self.mcp_server

    def resolve_tools(self) -> dict[str, list[str]]:
        """Parse the namespaced tools list into per-source tool lists.

        Returns a dict mapping namespace → list of tool names:
            {
                "Base": ["Read"],                    # CC built-in tools
                "Kiln": ["Bash", "Read", "Write", ...],
                "MyAgent": ["Bash", "CustomTool"],   # agent-specific
            }

        The harness uses this to:
        - Pass Base tools to Claude Code's --tools flag
        - Include Kiln tools from kiln's standard MCP server
        - Include agent tools from the agent's custom MCP server

        Validates that every ``Kiln::<name>`` entry matches a tool Kiln's
        MCP server actually exposes, raising ValueError with a clear message
        (including rename guidance) otherwise. Unknown namespaces are not
        validated — agents can freely introduce their own namespaces.
        """
        result: dict[str, list[str]] = {}
        for entry in self.tools:
            if "::" not in entry:
                # Unnamespaced — treat as Kiln for backward compat
                result.setdefault(NAMESPACE_KILN, []).append(entry)
                continue
            namespace, tool_name = entry.split("::", 1)
            result.setdefault(namespace, []).append(tool_name)

        # Validate Kiln:: tool names — fail loud on old names / typos
        kiln_tools = result.get(NAMESPACE_KILN, [])
        for name in kiln_tools:
            if name in KILN_TOOL_NAMES:
                continue
            if name in _RENAMED_KILN_TOOLS:
                raise ValueError(
                    f"agent.yml references Kiln::{name} — this tool was "
                    f"renamed to Kiln::{_RENAMED_KILN_TOOLS[name]} in the "
                    f"prompt-refactor release. Update your tools list."
                )
            raise ValueError(
                f"agent.yml references Kiln::{name}, which isn't a tool "
                f"Kiln's MCP server exposes. Known Kiln tools: "
                f"{', '.join(sorted(KILN_TOOL_NAMES))}."
            )

        return result


# ---------------------------------------------------------------------------
# Backend inference
# ---------------------------------------------------------------------------

# Model name prefixes that map to known backends.
_BACKEND_PREFIXES = {
    "claude": "claude",
    "gpt": "openai",
    "o1": "openai",
    "o3": "openai",
    "o4": "openai",
}


def infer_backend(model: str | None) -> str:
    """Infer the backend from a model name.

    Returns "claude" (default), "openai", or raises ValueError for
    unrecognized models with no explicit backend set.
    """
    if not model:
        return "claude"
    model_lower = model.lower()
    for prefix, backend in _BACKEND_PREFIXES.items():
        if model_lower.startswith(prefix):
            return backend
    return "claude"  # default — ClaudeBackend handles unknown models


def _apply_raw_fields(config: AgentConfig, raw: dict) -> None:
    """Apply raw YAML fields to an AgentConfig.

    Shared by load_agent_spec (base config) and apply_template (overrides).
    Unknown fields are silently ignored.
    """
    # Scalar fields — simple setattr
    for field_name in [
        "identity_doc", "owner_name", "model", "effort", "session_prefix",
        "scripts_dir", "skills_dir", "worklogs_dir", "sessions_dir",
        "inbox_dir", "plans_dir", "mcp_server", "hook_visibility",
        "orientation", "cleanup", "initial_mode", "cli",

    ]:
        if field_name in raw:
            setattr(config, field_name, raw[field_name])

    if "startup" in raw:
        config.startup = raw["startup"]

    if "tags" in raw and isinstance(raw["tags"], list):
        config.tags = [t for t in raw["tags"] if isinstance(t, str) and t]


    # Tools — flat namespaced list or structured dict
    tools_raw = raw.get("tools")
    if isinstance(tools_raw, list):
        config.tools = tools_raw
    elif isinstance(tools_raw, dict):
        if "list" in tools_raw:
            config.tools = tools_raw["list"]
        if "scripts_dir" in tools_raw:
            config.scripts_dir = tools_raw["scripts_dir"]

    if "context_injection" in raw:
        config.context_injection = raw["context_injection"]

    if "hooks" in raw:
        config.hooks = raw["hooks"]

    # Heartbeat — interval in minutes (0 = disabled)
    if "heartbeat" in raw:
        hb = raw["heartbeat"]
        if isinstance(hb, (int, float)) and not isinstance(hb, bool):
            config.heartbeat = max(float(hb) * 60, 0.0)
        else:
            config.heartbeat = 0.0



    # Idle nudge — value in minutes
    idle_key = "idle_nudge" if "idle_nudge" in raw else "idle-nudge" if "idle-nudge" in raw else None
    if idle_key:
        val = raw[idle_key]
        if isinstance(val, (int, float)) and not isinstance(val, bool) and val > 0:
            config.idle_nudge_timeout = float(val) * 60

    # Stream stall timeout — value in seconds (0 to disable)
    st_key = "stream_timeout" if "stream_timeout" in raw else "stream-timeout" if "stream-timeout" in raw else None
    if st_key:
        val = raw[st_key]
        if isinstance(val, (int, float)) and not isinstance(val, bool):
            config.stream_timeout = max(float(val), 0.0)

    # Context limit — mode + cap in tokens
    if "context_limit_mode" in raw:
        mode = str(raw["context_limit_mode"]).lower()
        if mode in ("off", "soft", "hard"):
            config.context_limit_mode = mode
    if "context_limit_tokens" in raw:
        val = raw["context_limit_tokens"]
        if isinstance(val, (int, float)) and not isinstance(val, bool) and val > 0:
            config.context_limit_tokens = int(val)

    # Steering delivery mode — "all" or "one-at-a-time"
    sd_key = (
        "steering_delivery" if "steering_delivery" in raw
        else "steering-delivery" if "steering-delivery" in raw else None
    )
    if sd_key:
        val = str(raw[sd_key]).lower()
        if val in ("all", "one-at-a-time"):
            config.steering_delivery = val


def load_agent_spec(spec_path: Path) -> AgentConfig:
    """Load an AgentConfig from an agent.yml spec file.

    The spec file is YAML with fields matching AgentConfig.
    Unknown fields are silently ignored.
    """
    if not spec_path.exists():
        raise FileNotFoundError(f"Agent spec not found: {spec_path}")

    raw = yaml.safe_load(spec_path.read_text()) or {}

    # Resolve home relative to spec file location
    home = spec_path.parent
    if "home" in raw:
        home = Path(os.path.expanduser(raw["home"]))

    config = AgentConfig(
        name=raw.get("name", spec_path.parent.name),
        home=home,
    )
    _apply_raw_fields(config, raw)
    return config


def apply_template(config: AgentConfig, name: str) -> None:
    """Apply a session template to an existing config.

    Templates live at <config.home>/templates/<name>.yml and provide
    partial config overrides — same fields as agent.yml.
    """
    templates_dir = config.home / "templates"
    path = templates_dir / f"{name}.yml"
    if not path.exists():
        path = templates_dir / name
        if not path.exists():
            raise FileNotFoundError(
                f"Template not found: {name} "
                f"(looked in {templates_dir})"
            )

    raw = yaml.safe_load(path.read_text()) or {}
    _apply_raw_fields(config, raw)
    config.template = name
