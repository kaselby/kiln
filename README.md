# Kiln

An agent runtime library for [Claude Code](https://docs.anthropic.com/en/docs/claude-code). Kiln provides the infrastructure layer — persistent shell, tool management, permission system, TUI, session tracking — so agent developers can focus on identity and behavior.

Kiln is a **library, not a framework.** Simple agents use the default harness with a YAML spec. Complex agents write their own harness and import Kiln's modules directly.

## Quick Start

```bash
pip install -e .

# Create a new agent
kiln init my-agent
cd my-agent

# Edit agent.yml and identity.md, then:
kiln run my-agent
```

## Two Ways to Use Kiln

### Simple: Default Harness + Agent Spec

Define your agent in `agent.yml`:

```yaml
name: assistant
identity_doc: identity.md
model: claude-sonnet-4-6
```

Write an `identity.md` with your agent's system prompt, and run `kiln run assistant`. The default `KilnHarness` handles everything: tool registration, hook wiring, session management, prompt assembly.

See [`examples/minimal-agent.yml`](examples/minimal-agent.yml) for a complete example.

### Advanced: Custom Harness

For agents that need custom behavior — specialized tools, unique session lifecycle, identity-specific hooks — write your own harness that imports from Kiln:

```python
from kiln.shell import PersistentShell
from kiln.tools import create_mcp_server, execute_bash, read_file
from kiln.hooks import create_inbox_check_hook, create_context_warning_hook
from kiln.prompt import build_session_context, discover_tools
from kiln.names import generate_agent_name
from kiln.registry import register_session
from kiln.permissions import create_permission_hook
```

Compose exactly the pieces you need. No extension points to navigate, no framework boundary — just Python calling library functions.

See the [Architecture](#architecture) section below for how the pieces fit together.

## Architecture

### Modules

| Module | Purpose |
|--------|---------|
| `kiln.shell` | Persistent shell management (tmux-backed subprocess) |
| `kiln.tools` | MCP tool functions + JSON schemas, standalone and importable |
| `kiln.hooks` | Infrastructure hook factories (inbox, context warning, plan nudge, etc.) |
| `kiln.permissions` | Permission system with notification-based approval flow |
| `kiln.config` | `AgentConfig` dataclass + `load_agent_spec()` for YAML specs |
| `kiln.prompt` | Tool/skill discovery, session context builder, model resolution |
| `kiln.names` | Parameterized agent name generation |
| `kiln.registry` | Session tracking (register, lookup, list) |
| `kiln.harness` | Default `KilnHarness` — batteries-included session manager |
| `kiln.cli` | CLI entry point (`kiln run/init/list`) |
| `kiln.tui` | Terminal UI (`KilnApp`) — prompt_toolkit scrollback-mode interface |

### Namespaced Tools

Agents declare exactly which tools they want and where they come from:

```yaml
tools:
  - Base::Read        # Claude Code built-in
  - Base::WebSearch   # Claude Code built-in
  - Kiln::Bash        # Kiln's persistent shell
  - Kiln::Edit        # Kiln's file editing
  - MyAgent::Bash     # Agent's custom wrapper (e.g. adding logging)
```

The `Base::` namespace exposes Claude Code's built-in tools. `Kiln::` provides Kiln's implementations (persistent shell, file ops with guardrails). Agents can define their own namespace by providing a custom MCP server that wraps or extends Kiln's standalone tool functions.

### Standard Library

Kiln ships with a standard library of shell-script tools and skills in `defaults/`:

**Tools** (16): `research`, `fetch`, `exa`, `tavily`, `reddit`, `hn`, `twitter-search`, `yt`, `session-query`, `log-analysis`, `read-sessions`, `list-sessions`, `task`, `todo`, and more.

**Skills** (6): `autonomy`, `collaboration`, `programming`, `research`, `tool-authoring`, `skill-authoring`.

On `kiln init`, these are copied into the agent's home directory. After that, the agent owns them — edits, additions, and deletions are theirs.

## Project Status

Kiln is extracted from a persistent AI agent project. It's functional and in active use, but the API is not yet stable. Expect breaking changes.

## Requirements

- Python 3.12+
- [Claude Code](https://docs.anthropic.com/en/docs/claude-code) (provides the underlying agent SDK)
- tmux (for persistent shell sessions)
