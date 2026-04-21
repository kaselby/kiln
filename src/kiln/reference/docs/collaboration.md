# Collaboration

How Kiln spawns, identifies, and coordinates multi-agent work — the `kiln run` lifecycle, templates, tags, and the agents registry.

## Overview

Every Kiln agent session runs inside a dedicated tmux window, launched by `kiln run`. A single agent home can host many concurrent sessions, each with its own auto-generated session ID (`<agent-name>-<adj>-<noun>`), its own inbox, its own system prompt, and its own state. Collaboration is a matter of running multiple sessions and letting them talk to each other through the messaging layer (see `messaging.md`) or the gateway (see `gateway.md`).

The CLI is the canonical spawn surface. A session can spawn peers by calling `kiln run <agent>` from its shell — it becomes a parent, and the child inherits a parent-child relationship that the harness tracks (depth, parent session ID) but doesn't enforce. Agents are peers, not a tree; the hierarchy is advisory.

Two configuration layers shape any spawned session:

- **Agent spec** (`agent.yml`) — the persistent identity of a named agent. Home directory, model, identity doc, default tools, skills, memory layout.
- **Session template** (`<home>/templates/<name>.yml`) — a named, partial override applied on top of the base spec at spawn time. Same fields as `agent.yml`; only the ones set in the template are overridden.

Templates are how a single agent home hosts multiple operating roles (a "reviewer" template with a different orientation prompt, a "dispatched" template that starts quietly with no cleanup prompt, etc.) without duplicating the base spec.

## Architecture

```
~/.kiln/
  agents.yml                     # registry — agent-name → home path
  daemon/…                       # shared daemon + state

<home>/
  agent.yml                      # base agent spec
  templates/
    reviewer.yml                 # session templates
    dispatched.yml
    persistent-peer.yml
  inbox/                         # per-session inboxes (see messaging.md)
  state/session-config-<id>.yml  # live mutable per-session config
```

Spawning flow (`kiln run <agent> --detach --mode yolo --prompt "..."`):

```
_find_agent_spec()                    # resolve <agent> → spec path
load_agent_spec(spec)                 # parse agent.yml → AgentConfig
apply_template(config, name)          # optional — overlay template fields
apply_cli_flags                       # --model, --mode, --effort, --var, --tag, ...
generate_agent_name(agent_name)       # new session ID
_launch_in_tmux                       # tmux new-session -d -s <id> -- kiln run … (inner)
  └─ harness.start()                  # builds prompt, starts backend, registers in daemon
```

The `_TMUX_GUARD` env var distinguishes the outer launcher from the inner run. On first invocation the CLI wraps itself in a tmux session; inside that session, `KILN_IN_TMUX=1` is set and the same CLI proceeds to construct the harness directly.

### Parent / child / continuation

Three relationships between sessions:

- **Parent → child** — a session spawned by another via `kiln run --parent <id>`. The child's config carries `parent=<id>` and `depth=parent.depth + 1`. Used by harness hooks for things like branch-thread routing and presence-label formatting.
- **Continuation** — a session relaunched from `ExitSession(continue=True)`. The inner CLI re-execs with `--continuation --parent <old-id>`; the new session inherits subscriptions, transfers unread inbox, and receives the handoff as its first inbox message. See `lifecycle.md`.
- **Persistent peer** — `--persistent` flag. Marked as a long-running peer that self-continues across context limits and coordinates with a parent. Used for agents that should stay alive across many sub-tasks.

The depth counter is advisory — nothing hard-limits it. Agents typically stop spawning at depth 2-3 by convention.

### Agents registry

`~/.kiln/agents.yml` maps agent names to home paths (the daemon extracts the prefix — everything before the first `-` — from a session ID to look up its home):

```yaml
<agent>:  ~/.<agent>
otheragent: ~/.kiln/agents/otheragent
thirdagent: ~/.kiln/agents/thirdagent
```

The daemon reads this to resolve inboxes for cross-agent messaging (`<agent-id>` → `~/.<agent>/inbox/<agent-id>/`), and the CLI reads it to resolve `<agent>` arguments to spec paths. `kiln init` registers new agents here automatically.

If an agent isn't registered, the daemon falls back to `~/.<prefix>/`. This preserves legacy agents whose homes predate the registry.

## Reference

### `kiln run` arguments

```
kiln run <spec> [options]
```

| Flag | Effect |
|------|--------|
| `<spec>` | Agent name, path to `agent.yml`, or dir. Defaults to `./agent.yml`. |
| `--id <id>` | Override auto-generated session ID. Rare — mostly used for tests. |
| `--model <name>` | Override `agent.yml` model. Accepts suffixes like `[1m]` for 1M context. |
| `--mode safe \| supervised \| yolo` | Initial permission mode. |
| `--effort low \| medium \| high \| xhigh \| max` | Thinking-effort level. |
| `--template <name>` | Apply `<home>/templates/<name>.yml` on top of the base spec. |
| `--var KEY=VALUE` | Set/override a template variable (repeatable). |
| `--tag TAG` | Seed a tag into the live session-config (repeatable). |
| `--heartbeat <min>` | Heartbeat interval (minutes). |
| `--idle-nudge <min>` | Inactivity nudge threshold. `0` = disable. |
| `--prompt "..."` / `--prompt-file <path>` | First user turn on session start. |
| `--detach` | Don't attach to the tmux window after launch. |
| `--resume <id>` | Resume an existing session exactly as-is. |
| `--last` | Resume the most recently registered session. |
| `--parent <id>` | Record parent session ID (for spawn tracking). |
| `--persistent` | Mark as persistent peer (self-continues). |
| `--continuation` | Internal — set by `ExitSession(continue=True)` self-exec. |

### Agent spec (`agent.yml`) essentials

```yaml
name: <agent>
home: ~/.<agent>
identity_doc: <AGENT>.md
model: claude-opus-4-7[1m]
effort: max
heartbeat: 0
initial_mode: yolo

tools:
  - Kiln::Bash
  - Kiln::Read
  - Kiln::Edit
  - Kiln::Write
  - Kiln::Plan
  - Kiln::Message
  - Kiln::ActivateSkill
  - Kiln::ExitSession

context_injection:
  - {path: memory/core.md, label: "Core Memory"}
  - {path: memory/active.md, label: "Active — Don't Forget"}

orientation: |
  Session {agent_id} started at {now}. Today is {today}.
cleanup: |
  Write session summary to {summary_path}.
```

Fields omitted from `agent.yml` fall back to `AgentConfig` dataclass defaults.

### Session templates

`<home>/templates/<name>.yml` follows the same schema as `agent.yml` but is partial — only the fields present override. Typical uses:

```yaml
# templates/dispatched.yml — a minimal template for subagent work
orientation: ""           # suppress orientation; inbox carries the prompt
cleanup: ""               # no session-end cleanup
heartbeat: 0
initial_mode: yolo
```

```yaml
# templates/reviewer.yml — a reviewer role with a different model
model: gpt-5.4
orientation: |
  You are reviewing work at {target_path}. Today is {today}.
template_vars:
  role: reviewer
```

`--var` accumulates into `template_vars`. Variables are available in `{name}` substitutions inside `orientation` and `cleanup`:

| Variable | Value |
|----------|-------|
| `{agent_id}` | Full session ID. |
| `{today}` | `YYYY-MM-DD`. |
| `{now}` | `YYYY-MM-DDTHH:MM:SS`. |
| `{summary_path}` | Deduped session-summary path. |
| `{<custom>}` | Anything from `--var KEY=VALUE` or `template_vars`. |

### Tags

`--tag <name>` seeds a tag into `<home>/state/session-config-<id>.yml`. Tags are short strings the daemon and agent-level tools can query — typical uses include marking a session as part of a named group, flagging special modes ("dispatch", "experiment"), or signaling membership in an ad-hoc cohort. Tags are runtime-mutable: sessions can add, remove, and query their own tags via the session-config file.

The daemon reads tags when populating `SessionRecord.tags` and exposes them in `list_sessions` responses, so cross-session queries can filter by tag.

### Daemon spawn RPC

`mgmt spawn_session` lets a running session spawn another through the daemon:

```python
DaemonClient.mgmt("spawn_session", {
    "agent": "<agent>",
    "mode": "yolo",
    "template": "dispatched",
    "prompt": "Investigate the DM delivery race. Write findings to scratch/...",
})
```

The daemon's `ManagementActions.spawn_session` builds a `kiln run` command, detaches, and returns an `ActionResult`. Same effect as running `kiln run` from a shell, with the benefit of being callable from any session that can reach the daemon.

Related management actions: `resume_session`, `stop_session` (kills tmux), `interrupt_session` (sends ESC), `capture_session` (grabs terminal output), `set_session_mode`.

## Examples

Spawn a peer session with a one-shot prompt:

```bash
kiln run <agent> --detach --mode yolo \
  --prompt "Investigate token counting discrepancy. Compare CLI vs API numbers for opus-4-7. Report back via DM."
```

Spawn a reviewer with a template and custom variables:

```bash
kiln run <agent> --detach --mode yolo \
  --template reviewer \
  --var target_path=docs/collaboration.md \
  --prompt "Review the collaboration doc for technical accuracy."
```

Programmatic spawn from inside a session:

```python
# In a shell tool or custom agent code
import asyncio
from kiln.daemon.client import DaemonClient

client = DaemonClient(agent="<agent>", session="<agent-id>")
result = asyncio.run(client.mgmt("spawn_session", {
    "agent": "<agent>",
    "template": "dispatched",
    "prompt": "Draft release notes for 0.3.0. Post to #docs when ready.",
}))
```

Coordinate via a channel:

```python
Message(action="subscribe", channel="release-notes")
# Spawn three agents, all prompted to subscribe to release-notes and post drafts.
# Compare as they land; send final via gateway when the group converges.
```

Apply a template at runtime from an existing launch:

```bash
# Resume with a different template (e.g. switch from work to persistent-peer)
kiln run <agent> --resume <agent-id> --template persistent-peer
```

Register a new agent in the shared registry:

```bash
kiln init newagent --dir ~/.newagent --model claude-sonnet-4-6
# Writes ~/.newagent/agent.yml and registers "newagent" in ~/.kiln/agents.yml
```

## Conventions

- **Invitations, not instructions.** When prompting another agent, describe the goal and the context; let them structure the work. Hard-coded checklists turn a collaborator into a script-runner.
- **Codebase-as-boundary for cross-repo collabs.** When the work crosses repositories or clear subsystems, split on that line — one agent per area, meet at the API contract. Structurally harder to violate than social "you take X, I'll take Y" splits.
- **First concrete claim wins.** When negotiating a split, whoever posts first with a concrete allocation wins. Second message accepts. Counter-proposals just extend the negotiation indefinitely.
- **Use channels for N>2 coordination.** Point-to-point messaging works fine for a pair; channels scale to conclaves without N² message traffic.
- **Tag agents doing related work.** If three sessions are investigating the same question, tag them (`--tag quant-research`) so anyone can enumerate them via `list_sessions` or the `sessions` tool.
- **Templates encode roles, not one-off configs.** If you're templating a specific task, you probably just want `--prompt` and `--var`. Templates earn their keep when the same role runs repeatedly.
- **Don't spawn inside a subshell and lose the daemon connection.** `kiln run` from an agent session inherits the daemon socket naturally; only use the `mgmt spawn_session` RPC when you need daemon-brokered spawn (e.g. from a non-agent tool).

## Gotchas

- **Template values win over `agent.yml` but lose to CLI flags.** Precedence is `CLI > template > agent.yml > AgentConfig default`. Forgetting this produces "why is my `--model` flag getting overridden?" confusion — it's not; the template's `model:` is being applied *before* CLI and then CLI wins. If CLI isn't winning, check that `args.<flag>` is being tracked as explicit (e.g. `_model_explicit`).
- **`--template` is sticky across resume and continuation.** Once applied, the template name is saved in session state and re-applied on resume and on self-continuation unless CLI explicitly overrides. Good for persistence, surprising if you forget.
- **Template-vars merge rather than replace.** `--var KEY=VALUE` adds to `config.template_vars`; it doesn't clear the dict. A template that sets `role: reviewer` plus a CLI `--var task=doc-review` yields both. Override by re-specifying the key.
- **Depth is advisory.** Nothing stops a session from spawning infinite children. Watch your tree by convention — `kiln run` depth typically stays ≤ 2-3 unless you have a reason.
- **Agents not in the registry use `~/.<prefix>/`.** This fallback exists for legacy agents but means typos in the agent name can silently route to a non-existent home. The daemon logs "Cannot resolve inbox" — check there if messages to a new agent disappear.
- **`--persistent` doesn't survive crashes on its own.** The flag tells the session "self-continue at context limit." A SIGKILL'd session just dies; persistence requires the normal `ExitSession(continue=True)` path. If you need crash recovery, layer an external watchdog on top.
- **Auto-generated agent IDs can collide across days.** The name space is ~2,400 (`<adj>-<noun>`); collision-avoidance only checks the current day. The harness sweeps stale inbox + plan on fresh-session claim to prevent data leaks — but if two sessions with the same ID are live on the same day, both will see each other's inbox messages. Continuations skip this cleanup intentionally.
- **`_TMUX_GUARD` is the re-entry guard.** `kiln run` inside tmux (with the guard set) executes the harness directly. `kiln run` outside tmux wraps itself. Nested `kiln run` calls inside an existing session with the guard set but wanting a new session need `--detach` to force an outer launch.
