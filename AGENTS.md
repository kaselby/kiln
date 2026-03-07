# Kiln — Agent Guide

See README.md for project overview and architecture. This document covers what you need to work in the codebase.

## Repo Structure

```
src/kiln/          # Library source — all importable modules
  shell.py         # PersistentShell (tmux subprocess)
  tools.py         # MCP tool functions + schemas (largest module, ~1090 lines)
  hooks.py         # Hook factories — each returns an async callable
  permissions.py   # Permission system with notification flow
  config.py        # AgentConfig dataclass + load_agent_spec()
  prompt.py        # Prompt assembly, tool/skill discovery, model resolution
  names.py         # Agent name generation
  registry.py      # Session tracking (JSON file-based)
  harness.py       # Default KilnHarness — wires everything together
  cli.py           # CLI entry point (kiln run/init/list)
  tui/             # Terminal UI (prompt_toolkit scrollback-mode app)
    app.py         # KilnApp — the main TUI class
    channels.py    # ChannelViewer — standalone channel chat UI
defaults/          # Standard library shipped with kiln
  tools/           # Shell-script tools (discovered via header comments)
  skills/          # Skill packages (SKILL.md + optional references/scripts)
  SYSTEM_PROMPT.md # Default identity doc for simple agents
examples/          # Example agent specs
```

## Key Conventions

**Library, not framework.** Kiln exports functions and classes. There are no extension points, lifecycle hooks to implement, or abstract methods to override. Agents import what they need and compose it in their own code.

**Tools are standalone functions.** `tools.py` exports both the MCP server factory (`create_mcp_server`) and standalone functions (`execute_bash`, `read_file`, `write_file`, `edit_file`, `do_send_message`, etc.). Standalone functions can be imported and wrapped by agent-specific MCP servers.

**Hooks are factories.** Each `create_*_hook()` function returns an async callable matching the Claude Agent SDK hook signature: `async def hook(input_data, tool_use_id, context) -> dict`. The harness wires these into the SDK's hook system.

**Shell-script tools use header comments for discovery.** The prompt module scans `scripts_dir` for executable files and reads structured comments to build the tool list injected into the system prompt:

```bash
#!/usr/bin/env bash
# name: my-tool
# arguments: "<query> [options]"
# description: One-line description shown in session context
# budget: $0.01/call (optional — for paid API tools)
```

## Working in the Codebase

- `tools.py` is the largest module. Tool functions, schemas, and the MCP server factory are all here. When adding a tool, add the standalone function, the JSON schema, and wire it in `create_mcp_server`.
- `hooks.py` contains only infrastructure hooks. Agent-behavioral hooks (worklog capture, memory reminders) belong in agent code, not here.
- `harness.py` is the default harness for simple agents. It reads `agent.yml` and wires everything. Don't add agent-specific behavior here — if it wouldn't make sense for a generic coding assistant, it doesn't belong.
- `config.py` defines the agent spec schema. When adding spec fields, update both `AgentConfig` and `load_agent_spec()`.
- The `defaults/` standard library tools and skills are agent-owned after init. Don't assume they're immutable.

## Tests

```bash
pip install -e ".[dev]"
pytest
```

No test suite yet — this is early-stage code extracted from a working system.
