"""Agent configuration and spec loading."""

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml


# Built-in Claude Code tools that can be included via agent spec.
# These are passed as --tools to the CLI.
KNOWN_BUILTINS = {
    "Read",        # built-in Read (images, PDFs, notebooks)
    "Write",       # built-in Write (replaced by MCP in standard library)
    "Edit",        # built-in Edit (replaced by MCP in standard library)
    "Bash",        # built-in Bash (replaced by MCP in standard library)
    "WebSearch",   # built-in web search
    "WebFetch",    # built-in web fetch (Haiku summary)
    "TodoWrite",   # built-in todo/planning
}

# Default builtins when agent spec doesn't specify.
# Only Read and WebSearch — the rest are replaced by kiln's MCP tools.
DEFAULT_BUILTINS = ["Read", "WebSearch"]


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
    ephemeral: bool = False
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

    # Tools
    builtin_tools: list[str] = field(default_factory=lambda: list(DEFAULT_BUILTINS))
    mcp_server: str | None = None     # path to custom MCP server module (relative to home)
    scripts_dir: str = "tools"        # shell tools directory (relative to home)

    # Context injection — files to include in the system prompt
    context_injection: list[str] = field(default_factory=list)

    # Skills
    skills_dir: str = "skills"        # relative to home

    # Memory / worklogs / sessions
    worklogs_dir: str = "memory/worklogs"   # relative to home
    sessions_dir: str = "memory/sessions"   # relative to home

    # Hooks — which standard library hooks to enable and their config.
    # Keys are hook names, values are parameter dicts.
    # Example: {"worklog": {"interval_minutes": 5}, "plan_nudge": {"interval": 20}}
    hooks: dict[str, dict] = field(default_factory=dict)

    # Agent-provided hook modules — paths relative to home
    custom_hooks: list[str] = field(default_factory=list)

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
        if self.ephemeral:
            # Ephemeral agents may have a separate lean identity doc
            ephemeral_path = self.home / "EPHEMERAL.md"
            if ephemeral_path.exists():
                return ephemeral_path.read_text()
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

    # Tools
    tools_spec = raw.get("tools", {})
    if isinstance(tools_spec, dict):
        if "builtin" in tools_spec:
            config.builtin_tools = tools_spec["builtin"]
        if "mcp_server" in tools_spec:
            config.mcp_server = tools_spec["mcp_server"]
        if "scripts_dir" in tools_spec:
            config.scripts_dir = tools_spec["scripts_dir"]
    
    # Context injection
    if "context_injection" in raw:
        config.context_injection = raw["context_injection"]

    # Hooks
    if "hooks" in raw:
        config.hooks = raw["hooks"]
    if "custom_hooks" in raw:
        config.custom_hooks = raw["custom_hooks"]

    # Heartbeat
    if "heartbeat" in raw:
        hb = raw["heartbeat"]
        if isinstance(hb, dict):
            config.heartbeat = hb.get("enabled", False)
            config.heartbeat_interval = hb.get("interval", 1800.0)
        else:
            config.heartbeat = bool(hb)

    return config
