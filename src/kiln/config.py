"""Agent configuration and spec loading."""

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml


# Known tool namespaces. Tools in the agent spec are namespaced as
# "Source::ToolName" to make the source explicit.
#
#   Base::<name>    — Claude Code built-in tools (passed via --tools flag)
#   Kiln::<name>    — Kiln's standard MCP server tools
#   <Agent>::<name> — Agent's custom MCP server tools
#
NAMESPACE_BASE = "Base"
NAMESPACE_KILN = "Kiln"

# Claude Code built-in tools that can be referenced as Base::<name>.
KNOWN_BUILTINS = {
    "Read",        # built-in Read (images, PDFs, notebooks)
    "Write",       # built-in Write (replaced by MCP in standard library)
    "Edit",        # built-in Edit (replaced by MCP in standard library)
    "Bash",        # built-in Bash (replaced by MCP in standard library)
    "WebSearch",   # built-in web search
    "WebFetch",    # built-in web fetch (Haiku summary)
    "TodoWrite",   # built-in todo/planning
}

# Default tool set when agent spec doesn't specify.
DEFAULT_TOOLS = [
    "Base::Read",
    "Base::WebSearch",
    "Kiln::Bash",
    "Kiln::Read",
    "Kiln::Write",
    "Kiln::Edit",
    "Kiln::message",
    "Kiln::plan",
    "Kiln::exit_session",
    "Kiln::activate_skill",
]


@dataclass
class AgentConfig:
    """Configuration for a single agent session.

    Can be constructed directly or loaded from an agent spec file (agent.yml).
    """

    # Agent identity
    name: str = ""                    # agent name (e.g. "aleph")
    home: Path = field(default_factory=lambda: Path.home())
    identity_doc: str = "identity.md"  # relative to home

    # Session
    agent_id: str | None = None       # unique session ID (generated if None)
    model: str | None = None          # model name or alias
    project: str | None = None        # project path (sets cwd)

    # Spawning hierarchy
    parent: str | None = None
    depth: int = 0
    persistent: bool = False
    continuation: bool = False

    # Session lifecycle
    continue_session: bool = False
    resume_session: str | None = None
    maintenance: bool = False
    prompt: str | None = None

    # Permission mode
    initial_mode: str | None = None   # safe, default, yolo

    # Heartbeat
    heartbeat: bool = False
    heartbeat_interval: float = 1800.0

    # Idle nudge: send a message after prolonged inactivity (seconds, 0 = disabled)
    idle_nudge_timeout: float = 0.0

    # Tools — namespaced list: "Base::Read", "Kiln::Bash", "Aleph::CustomTool"
    tools: list[str] = field(default_factory=lambda: list(DEFAULT_TOOLS))
    mcp_server: str | None = None     # path to custom MCP server module (relative to home)
    scripts_dir: str = "tools"        # shell tools directory (relative to home)

    # Context injection — files to include in the system prompt
    context_injection: list[str] = field(default_factory=list)

    # Skills
    skills_dir: str = "skills"        # relative to home

    # Memory / worklogs / sessions
    worklogs_dir: str = "memory/worklogs"   # relative to home
    sessions_dir: str = "memory/sessions"   # relative to home

    # Hooks — agent-defined configuration for hook behavior.
    # The default harness reads this to configure infrastructure hooks.
    # Custom harnesses may read it, extend it, or ignore it entirely.
    # Keys and values are agent-defined — kiln imposes no schema.
    hooks: dict[str, dict] = field(default_factory=dict)

    # Tmux session prefix for agent naming
    session_prefix: str = "kiln-"

    # Inbox / messaging
    inbox_dir: str = "inbox"          # relative to home

    # Plans
    plans_dir: str = "plans"          # relative to home

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
        are silently skipped.
        """
        results = []
        for rel_path in self.context_injection:
            full_path = self.home / rel_path
            if full_path.exists():
                try:
                    results.append((rel_path, full_path.read_text()))
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
                "Base": ["Read", "WebSearch"],
                "Kiln": ["Bash", "Read", "Write", ...],
                "Aleph": ["Bash", "CustomTool"],  # agent-specific
            }

        The harness uses this to:
        - Pass Base tools to Claude Code's --tools flag
        - Include Kiln tools from kiln's standard MCP server
        - Include agent tools from the agent's custom MCP server
        """
        result: dict[str, list[str]] = {}
        for entry in self.tools:
            if "::" not in entry:
                # Unnamespaced — treat as Kiln for backward compat
                result.setdefault(NAMESPACE_KILN, []).append(entry)
                continue
            namespace, tool_name = entry.split("::", 1)
            result.setdefault(namespace, []).append(tool_name)
        return result


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

    # Simple scalar fields
    for field_name in [
        "identity_doc", "model", "session_prefix",
        "scripts_dir", "skills_dir", "worklogs_dir", "sessions_dir",
        "inbox_dir", "plans_dir", "mcp_server",
    ]:
        if field_name in raw:
            setattr(config, field_name, raw[field_name])

    # Tools — either a flat namespaced list or a structured dict
    tools_raw = raw.get("tools")
    if isinstance(tools_raw, list):
        # Flat namespaced list: ["Base::Read", "Kiln::Bash", ...]
        config.tools = tools_raw
    elif isinstance(tools_raw, dict):
        # Structured with separate mcp_server/scripts_dir alongside tool list
        if "list" in tools_raw:
            config.tools = tools_raw["list"]
        if "scripts_dir" in tools_raw:
            config.scripts_dir = tools_raw["scripts_dir"]

    if "mcp_server" in raw:
        config.mcp_server = raw["mcp_server"]

    # Context injection
    if "context_injection" in raw:
        config.context_injection = raw["context_injection"]

    # Hooks
    if "hooks" in raw:
        config.hooks = raw["hooks"]

    # Heartbeat
    if "heartbeat" in raw:
        hb = raw["heartbeat"]
        if isinstance(hb, dict):
            config.heartbeat = hb.get("enabled", False)
            config.heartbeat_interval = hb.get("interval", 1800.0)
        else:
            config.heartbeat = bool(hb)

    # Idle nudge — send a message after prolonged inactivity (value in minutes)
    idle_key = "idle_nudge" if "idle_nudge" in raw else "idle-nudge" if "idle-nudge" in raw else None
    if idle_key:
        val = raw[idle_key]
        if isinstance(val, (int, float)) and not isinstance(val, bool) and val > 0:
            config.idle_nudge_timeout = float(val) * 60

    return config
