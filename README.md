# Kiln

An agent runtime library for [Claude Code](https://docs.anthropic.com/en/docs/claude-code). Kiln separates **agent infrastructure from agent identity** — persistent shell, permission system, multi-agent messaging, TUI, session tracking — so agent developers can focus on who the agent is and what it does.

**Library, not framework.** Kiln exports composable building blocks. There are no extension points to implement, no lifecycle to inherit. Your harness calls Kiln — not the other way around. The default `KilnHarness` is one particular wiring of these pieces; complex agents write their own.

---

## Philosophy

Inspired by [Mario Zechner's Pi](https://mariozechner.at/posts/2025-11-30-pi-coding-agent/): four core tools (read, write, edit, bash), a minimal system prompt, and a bash-literate model is all it takes for an effective agent. Bash already does most of it. MCP tool sprawl costs thousands of tokens per session for marginal benefit. Kiln inherits this tooling philosophy directly — shell scripts over MCP servers, minimal core, let the model figure it out.

Where Kiln extends beyond Pi:

**Persistent identity.** An agent's home directory (`~/.agent/`) is its world — memory files, session history, tools it has built, mistakes it has recorded. Sessions end; identity persists across them. This is the core use case: not ephemeral coding assistants, but agents that accumulate state and get better over time.

**Recursive self-improvement.** On `kiln init`, the standard library is *copied* into the agent's home directory. After that, it belongs to the agent. The agent can read its own harness, edit its own prompting, add tools, delete tools. There is no framework boundary between the agent and the infrastructure that runs it.

**Recursive spawning.** To spawn a subagent, an agent runs `kiln run` as a shell command. No special API, no graph topology. Coordination happens through messaging channels — not framework primitives.

---

## Quick Start

```bash
pip install -e .

kiln init my-agent
cd my-agent

# Edit identity.md (system prompt) and agent.yml, then:
kiln run my-agent
```

---

## Two Ways to Use Kiln

### Simple: Agent Spec

Define your agent in `agent.yml` and an `identity.md` system prompt:

```yaml
name: assistant
identity_doc: identity.md
model: claude-sonnet-4-6
initial_mode: supervised
```

Run `kiln run assistant`. The default `KilnHarness` handles prompt assembly, tool registration, hook wiring, and session management. The spec also supports orientation/cleanup prompts, heartbeat configuration, context injection (inject memory files into the system prompt), startup commands, and namespaced tool selection.

### Advanced: Custom Harness

For agents that need custom behavior, write your own harness that imports Kiln's building blocks directly:

```python
from kiln.shell import PersistentShell
from kiln.tools import create_mcp_server, execute_bash, read_file, edit_file, write_file
from kiln.hooks import create_inbox_check_hook, create_context_warning_hook
from kiln.permissions import PermissionHandler, PermissionMode
from kiln.prompt import build_session_context, discover_tool_layout
from kiln.config import load_agent_spec
from kiln.harness import KilnHarness
```

Compose exactly the pieces you need. The harness is not special — it's just Python calling library functions in a particular order.

---

## Key Capabilities

### Persistent Shell

Every agent session runs inside a tmux session. The shell persists between tool calls — environment variables, working directory, and background jobs all survive. Most agent systems give you stateless subprocess calls; Kiln gives you a workspace. Multiple agents run as parallel tmux sessions on the same machine, coordinating through messaging.

### Permission System

Four modes (`safe` → `supervised` → `yolo` → `trusted`), cycling via TUI keybinding or set at startup.

**Guardrails** run on every Bash call regardless of mode. Two tiers: **block** (catastrophic ops — always denied) and **confirm** (destructive-but-legitimate — always prompts, even in yolo). When a prompt fires, it races the terminal TUI against the gateway (Discord) in parallel — first response wins, the other source is cleaned up.

### Services

**Gateway** (`services/gateway/`) — Discord bridge. Bridges Kiln messaging channels to Discord threads bidirectionally. Handles remote permission approval via Approve/Reject button embeds — approve agent actions from your phone when the terminal isn't at hand.

**Voice** (`services/voice/`) — Whisper STT + OpenAI TTS. Used by the gateway for Discord voice messages.

### Session Lifecycle

**Self-continuation** — when context fills up, an agent calls `exit_session(continue=true, handoff='...')`. The harness `exec`s into a fresh session with the handoff as the startup message. New context window, same task, same identity.

**Session resume** — `--continue` resumes the most recent session; `--resume <agent-id>` resumes a specific one. Conversation history is restored and rendered in the TUI.

**Heartbeat** — configurable idle nudges with exponential backoff, keeping autonomous agents alive across quiet periods.

### Multi-Agent

Agents message each other via point-to-point delivery or channel broadcasts. Messages are markdown files in `~/.agent/inbox/`. Cross-home discovery (`~/.kiln/agents.yml`) lets agents in different home directories find each other — a message `to: "aleph"` resolves to `~/.aleph/inbox/` at runtime.

To spawn a subagent, an agent runs `kiln run --detach --prompt "..."` from its Bash tool. No spawning API — just a shell command. Coordination flows back through messaging.

---

## Architecture

### Modules

| Module | Purpose |
|--------|---------|
| `kiln.shell` | Persistent shell (tmux-backed — env, cwd, background jobs survive between tool calls) |
| `kiln.tools` | Core MCP tools: Bash, Read, Write, Edit, message, plan, exit_session, activate_skill |
| `kiln.hooks` | Infrastructure hook factories (inbox check, context warnings, plan nudges, usage logging) |
| `kiln.permissions` | `PermissionHandler` — modes, guardrail enforcement, terminal+gateway approval racing |
| `kiln.guardrails` | Regex-based dangerous command detection (block and confirm tiers) |
| `kiln.prompt` | Tool/skill discovery, session context builder, model resolution |
| `kiln.config` | `AgentConfig` dataclass + `load_agent_spec()` |
| `kiln.harness` | Default `KilnHarness` — batteries-included session manager |
| `kiln.tui` | Terminal UI with scrollback-mode interface and channel browser |
| `kiln.cli` | CLI entry point (`kiln run`, `kiln init`, `kiln list`) |

### Namespaced Tools

Agents declare which tools they want and where they come from:

```yaml
tools:
  - Base::Read         # Claude Code built-in
  - Kiln::Bash         # Kiln's persistent shell
  - MyAgent::Bash      # Agent's own custom wrapper
```

`Base::` exposes Claude Code built-ins. `Kiln::` provides Kiln's MCP implementations. Agents define their own namespace via the `mcp_server` field in agent.yml.

### Standard Library

`kiln init` copies 16 shell-script tools and 6 skills into the agent's home directory. Tools are auto-discovered via header comments; skills are loaded on demand via `activate_skill`. Both support a tiered `core/` + `library/` layout for managing context budget.

After init, the agent owns them. Edits, additions, deletions — all theirs.

---

## Project Status

Kiln is extracted from a production persistent-agent project. It's functional and in active daily use, but the API is not yet stable. Expect breaking changes between versions.

## Requirements

- Python 3.12+
- [Claude Code](https://docs.anthropic.com/en/docs/claude-code) (`claude-agent-sdk` — installed as a dependency)
- tmux (for persistent shell sessions)
- macOS or Linux
