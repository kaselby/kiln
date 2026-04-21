# Session Lifecycle

How a Kiln session starts, runs, and stops — including resume, self-continuation, and the state artifacts each phase reads and writes.

## Overview

A session runs inside a tmux window managed by the `kiln` CLI. The CLI launches tmux, which launches the harness (default: `KilnHarness` in `harness.py`), which builds the system prompt, wires hooks, and drives the model turn loop. Session state lives in files under the agent's home — no in-memory-only state survives a shutdown, and every durable field is readable by other processes (the TUI, the daemon, the `sessions` tool, resume).

Three phases, each with clear entry and exit points:

1. **Startup** — CLI args → config → tmux → harness construction → prompt assembly → backend start → inbox/subscription restore → orientation.
2. **Active work** — model turn loop, hook-driven context injection, periodic state snapshots.
3. **Shutdown** — cleanup prompt → hook drain → state snapshot → git commit → conversation archive → subprocess cleanup.

Two variants layer on top: **resume** (pick up a prior session's exact state, from its stored system prompt forward) and **self-continuation** (clean shutdown plus automatic relaunch, carrying handoff + subscriptions but starting a fresh conversation).

## Architecture

Key state files per agent home:

```
<home>/
  state/
    session-config-<agent-id>.yml     # live mutable (mode, heartbeat, tags, context_limit)
    trust-<platform>.yml              # gateway trust state
  logs/
    session-registry.json             # all known sessions — cwd, model, session_uuid
    session-state/<agent-id>.yml      # derived snapshot — system_prompt, subscriptions, tokens, model, template
    stderr-<agent-id>.log             # harness stderr
    conversations/
      live/<agent-id>.jsonl           # custom-backend transcript (live)
      <YYYY-MM-DD>-<agent-id>.jsonl   # archived at session end
    tool-usage.jsonl                  # tool/skill invocation log
  inbox/<agent-id>/                   # messages waiting for this session
  plans/<agent-id>.yml                # current Plan output
  memory/sessions/<YYYY-MM-DD>-<agent-id>.md  # (agent-written) session summary
```

Two in-memory structs drive the lifecycle:

- `SessionConfig` (`session_config.py`) — authoritative live per-session config. File-backed. Reads hit disk on every `.get()`, so external writes take effect immediately.
- `SessionControl` (`tools.py`) — lifecycle signal struct. `ExitSession` writes to it; the harness loop polls it to decide whether to continue, continue-with-handoff, or terminate.

### Startup flow

`kiln run <agent>` resolves the spec, applies CLI overrides, and launches tmux (unless already inside one via the `KILN_IN_TMUX` guard). Inside tmux, `cmd_run` constructs the harness and calls `start()`:

```
_run_startup_commands()                  # agent.yml: startup: list
_create_daemon_client()                  # stateless client; daemon may not be up yet
_select_backend()                        # claude | openai, inferred from model
_build_backend_config()
  ├─ _load_session_state_snapshot()      # if resuming
  ├─ (continuation only) copy parent subscriptions
  ├─ apply_template / restore model
  ├─ build prompt via PromptBuilder (identity + kiln reference + session context + memory) OR reuse saved
  ├─ _save_session_state_snapshot()      # initial snapshot
  ├─ _clean_stale_agent_state()          # unless continuation
  ├─ (continuation) transfer unread inbox from parent
  ├─ build hooks (permissions, inbox, read-tracking, session-state, plan-nudge, skill-context, usage-log, ...)
  └─ assemble MCP server + tool defs
backend.start(config)
register_session()                       # writes session-registry.json
_restore_channel_subscriptions()         # async, requires daemon
queue orientation + --prompt             # as inbox msg if both are set
```

`register_session()` is the public signal that this session exists — the `sessions` tool, the daemon, and the TUI all read the registry.

### Active work

The harness runs a receive loop (`receive()` / `_receive_guarded()`) that yields backend events and scans assistant text for role injection. Between yielded events, hooks fire:

- **PreToolUse** — permission handler (`PermissionHandler`) checks mode + confirm rules before every tool call.
- **PostToolUse** (unmatched, run after every tool):
  - `inbox_check` — scans `<home>/inbox/<agent-id>/` for unread `.md` files, injects summaries as `additionalContext`, touches `.read` markers.
  - `session_state` — every 15 tool calls, emits `[Session state] mode=... | context: NkK/MkK | <owner>: presence | Agents: ...`.
  - `plan_nudge` — every 20 tool calls, re-injects the current plan if any tasks are still pending.
  - `usage_log` — appends to `tool-usage.jsonl` for custom tools and skill activations.
  - `queued_messages` — delivers steering messages the user typed mid-turn.
- **PostToolUse** (matched):
  - `Read` → `read_tracker` (records into `FileState`, marks inbox messages read) + `supplemental_content` (stops continuation when a PDF is pending).
  - `activate_skill` → `skill_context` (injects SKILL.md body).
  - `message` → `message_sent` (UI event for the TUI).

Periodic background work:

- `persist_live_session_state()` / `_snapshot_session_state()` — refreshes `logs/session-state/<agent-id>.yml` with live config, subscriptions, and context tokens.
- Heartbeat (`config.heartbeat`, seconds; 0 = disabled) — harness-driven nudge so agents can run quasi-async.

Supplemental content (PDFs) breaks the normal flow: the `Read` PostToolUse hook returns `continue_: False`, interrupting the turn; the harness then injects a `DocumentContent` block as a new user message and resumes.

### Shutdown flow

Triggered by `ExitSession` (sets `SessionControl.quit_requested`) or by an external stop (SIGHUP from tmux kill-session, force_stop, or crash):

```
prepare_shutdown()                       # queues config.cleanup prompt if set
  → harness runs one more turn            # agent writes session summary, commits memory
stop()
  ├─ _snapshot_session_state()            # final snapshot
  ├─ shell cleanup
  ├─ backend.stop()
  ├─ _close_stderr()
  ├─ session_config.cleanup()             # removes session-config-<agent-id>.yml
  └─ _cleanup_agent_state()               # removes plans/<agent-id>.yml + inbox/<agent-id>/
archive_conversation()                   # copies JSONL → logs/conversations/<date>-<agent-id>.jsonl
commit_memory()                          # git add -A && git commit if repo and dirty
```

If `ExitSession(continue=True)` was called, the CLI notices `continue_requested` and `exec`s a new `kiln run` invocation with `--continuation --parent <old-id>` — a fresh session that reuses the parent's subscriptions, transfers unread inbox messages, and receives the handoff as its first inbox message.

Crash path: a SIGHUP handler in `cli.py` runs `session_config.cleanup()` and exits 1. Stale state from crashed sessions is swept by the *next* session's `_cleanup_stale_sessions()` (uses tmux as source of truth for what's alive).

## Reference

### CLI entry points

| Command                       | Effect                                                        |
|-------------------------------|---------------------------------------------------------------|
| `kiln run <spec>`             | Fresh session; auto-generated agent ID.                       |
| `kiln run <spec> --resume <id>` | Resume session `<id>` — restores prompt + config + subs.     |
| `kiln run <spec> --last`      | Resume the most recently registered session.                  |
| `kiln run <spec> --continuation --parent <id>` | Internal — used by self-continuation `exec`.     |
| `kiln run <spec> --detach`    | Don't attach to tmux after launch.                            |
| `kiln run <spec> --prompt "..."` | Queued as first user turn (or inbox msg if orientation set). |

Common overrides: `--model`, `--mode`, `--template`, `--effort`, `--heartbeat MIN`, `--idle-nudge MIN`, `--tag TAG`, `--var K=V`.

### Orientation and cleanup

`AgentConfig.orientation` and `AgentConfig.cleanup` are format-string templates with these variables available:

| Variable         | Value                                                            |
|------------------|------------------------------------------------------------------|
| `{agent_id}`     | Full session ID                                                  |
| `{today}`        | `YYYY-MM-DD`                                                     |
| `{now}`          | `YYYY-MM-DDTHH:MM:SS`                                            |
| `{summary_path}` | Deduped path for today's session summary                         |
| `{...}`          | Any extra vars passed via `--var K=V` or `config.template_vars`  |

Empty string = explicit suppression (useful for disabling inherited defaults). `None` = use the harness/subclass default (may be nothing).

### Session state snapshot fields

`<home>/logs/session-state/<agent-id>.yml`:

```yaml
system_prompt: |
  <full assembled prompt — reused verbatim on resume>
session_config:            # snapshot of live session-config at last checkpoint
  mode: yolo
  heartbeat: 0
  tags: [...]
channel_subscriptions:     # restored async on resume
  - design-review
context_tokens: 71000      # restored for status surfaces
model: claude-opus-4-7     # raw model string — restored unless CLI --model wins
template: beth47           # re-applied on resume
template_vars:             # merged on resume
  canonical: "true"
```

Precedence on resume (highest first): CLI `--model` (marked `_model_explicit`) > restored template's model > saved raw model > `agent.yml` default.

### Self-continuation vs resume

| Aspect                | Resume (`--resume`)                                 | Self-continuation (`ExitSession(continue=True)`) |
|-----------------------|-----------------------------------------------------|--------------------------------------------------|
| Triggered by          | CLI                                                 | Agent                                            |
| Conversation history  | Continued (backend resume)                          | Fresh (new conversation)                         |
| System prompt         | Reused from snapshot                                | Rebuilt from scratch                             |
| Agent ID              | Same as before                                      | New (new session)                                |
| Subscriptions         | Restored from snapshot                              | Restored from parent's snapshot                  |
| Inbox                 | Kept as-is                                          | Transferred from parent; handoff added as msg    |
| Orientation           | Skipped                                             | Runs                                             |
| Permission mode       | From snapshot                                       | Defaults to `yolo`                               |

## Examples

Fresh session, detached, with a one-shot prompt:

```bash
kiln run <agent> --detach --mode yolo --prompt "Draft the release notes for 0.2.0."
```

Resume a specific session:

```bash
kiln run <agent> --resume <agent-id>
```

Resume the most recent:

```bash
kiln run <agent> --last
```

Self-continuation (from inside a session):

```python
ExitSession(
    continue=True,
    handoff="Spec draft at scratch/pa-spec.md is at §4. Open questions tracked inline. "
            "Next session: resolve §4 open question then draft §5.")
```

Suppressing session-end prompts (in `agent.yml`):

```yaml
cleanup: ""   # explicit empty string — no cleanup prompt even if a subclass would provide one
```

Template-based session with vars:

```bash
kiln run <agent> --template dispatched --var project=<project> --var task_id=<task-id>
```

## Conventions

- **Write durable state to disk every checkpoint.** Anything that matters across a session end should live in `state/` or `logs/session-state/`. In-memory-only fields disappear on crash.
- **Resume restores the snapshot, not the live config file.** Live mutations between snapshots are lost on resume. Call `persist_live_session_state()` if you need to force a snapshot.
- **Continuation is for "I'm out of context; keep going."** Resume is for "I want to pick up exactly where I left off." Don't conflate them.
- **Orientation fires once, on fresh sessions only.** Don't put anything in it that needs to run on every restart — use `startup:` commands for that (they run before prompt assembly every time).
- **`skip_summary=true` is rare.** The shutdown summary is how the agent's next self finds out what happened. Skip only for truly ephemeral sessions (a 30-second dispatch that produced no lasting state).

## Gotchas

- **Agent IDs can collide across days.** The namespace is ~2,400 names; collision-avoidance only checks the current day. `_clean_stale_agent_state()` sweeps plan + inbox for a name when a new session claims it — but it's skipped on continuation (legit inheritance).
- **Subscription restore is async and needs the daemon.** If the daemon is still starting when `start()` runs, subscriptions won't restore on the first try — they get re-sent on the next subscribe/unsubscribe. Gateway agents that DM-on-startup lose the first few inbound messages if this window is long; see the DM-delivery fix pattern (retry + post-subscribe verify).
- **`_cleanup_stale_sessions()` uses tmux as truth.** A session whose tmux window is still alive but whose Python process crashed won't get cleaned. Kill the tmux window first, then run any cleanup.
- **SIGHUP handler is minimal.** It cleans up `session_config` and `caffeinate` only — no snapshot, no archive, no commit. If you need those on crash, they have to happen inside the normal `stop()` path (which SIGHUP skips).
- **Template restore happens before CLI override.** A CLI `--model` still wins on resume (via `_model_explicit`), but a CLI `--template` is also marked explicit and wins over the saved template. Raw `--model` without `--template` now persists across resumes — this was historically broken.
- **Continuation starts in `yolo` mode.** The ExitSession docstring mentions this; agents that need a different default mode have to set it in the handoff inbox message or via `agent.yml` defaults.
- **Conversation archiving is path-sensitive for the Claude backend.** CC stores JSONLs under `~/.claude/projects/<encoded-cwd>/<uuid>.jsonl` where `<encoded-cwd>` is the agent's home with `/` and `.` replaced by `-`. If the agent's `home` ever changes, the archive step silently fails to find the file.
