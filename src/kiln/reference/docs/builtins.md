# Built-In Tools

Full reference for the tools served by Kiln's standard MCP server — the ones that require harness-level wiring (shared shell, file-state tracking, session control, daemon access) and can't be written as shell scripts.

## Overview

Kiln ships eight built-in tools: `Bash`, `Read`, `Write`, `Edit`, `Plan`, `Message`, `ActivateSkill`, `ExitSession`. They're registered by `tools.py:create_mcp_server()` as a single MCP server (namespace: `kiln`) and wired to session-scoped state — a persistent shell, a `FileState` tracker, the `SessionControl` struct, the `DaemonClient`, and the plans directory.

Every built-in is also available as a standalone async function (`execute_bash`, `read_file`, `edit_file`, `write_file`, `do_send_message`, `do_activate_skill`, `do_update_plan`, `do_exit_session`) importable from `kiln.tools`. Custom harnesses can wrap these directly without instantiating the MCP server — this is how agents add worklog capture, custom permission checks, or extra hooks.

The default tool set in `config.DEFAULT_TOOLS` pulls in all eight via `Kiln::` namespace. Agents can narrow the set in `agent.yml` if they don't want, say, `ActivateSkill` (no skills) or `Plan` (trivial agent).

## Architecture

```
<home>/state/                        # session-scoped scratch used by built-ins
  session-config-<agent-id>.yml      # live mutable config (heartbeat, mode, tags)
<home>/plans/<agent-id>.yml           # Plan tool output
<home>/inbox/<agent-id>/              # Message delivery target when offline
```

Shared runtime objects:

- `PersistentShell` — one per session, owned by the MCP server. Backs `Bash`. Survives across tool calls; cleaned up at session stop.
- `FileState` — one per session. Records read timestamps and partial-read flags. Read by `Write`/`Edit` to enforce "read-before-write" and "modified-since-read" validations. Populated by `Read` directly and by the read-tracking PostToolUse hook.
- `SessionControl` — one per session. Written by `ExitSession`; read by the harness loop to decide whether to continue, skip summary, or launch a continuation.
- `DaemonClient` — connects to the Kiln daemon's Unix socket. Used by `Message` for channel broadcast and direct delivery (with filesystem fallback).
- `SupplementalContent` — one per session. Stashes PDF document blocks for next-turn injection.

### Tool flow

```
Agent invokes mcp__kiln__<Tool>
  → MCP tool wrapper (tools.py)
  → Standalone function (execute_bash, read_file, ...)
  → Hook chain (PreToolUse: permissions; PostToolUse: read-tracking, inbox-check, session-state, ...)
  → Result returned to agent
```

## Reference

Input schemas below are abbreviated — see `tools.py:*_SCHEMA` for the exact JSON Schema.

### Bash

Executes a command in a persistent shell. Environment, `cwd`, aliases, and variables persist between calls.

| Arg                         | Type    | Notes |
|-----------------------------|---------|-------|
| `command`                   | string  | The command to execute. |
| `timeout`                   | int     | Timeout in milliseconds (default 120_000). |
| `description`               | string  | Optional label; aids readability, ignored by the shell. |
| `run_in_background`         | bool    | Start the command detached; returns a `job_id`. |
| `background_job_id`         | string  | Check status of an existing background job. |
| `cleanup_background_job_id` | string  | Clean up temp files for a finished background job. |

Returns combined stdout + stderr followed by a footer: `[timestamp] Exit code: N | elapsed_ms | cwd: ...`. Timeouts render `TIMED OUT after Nms`.

### Read

Reads a file and returns its contents. Also records the read in `FileState` — required before `Write`/`Edit` on the same path.

| Arg         | Type   | Notes |
|-------------|--------|-------|
| `file_path` | string | Absolute path. Required. |
| `offset`    | int    | 1-indexed start line (text files). |
| `limit`     | int    | Number of lines to return. Sets `partial=True` in FileState. |
| `pages`     | string | Page range for PDFs (e.g. `"1-3"`, `"5"`, `"10-"`). |

Output formats by file type:

- **Text** — `cat -n` output, capped at ~45K chars; lines over 2000 chars truncated with `[truncated]` marker. Binary extensions (`.so`, `.exe`, etc.) return an error.
- **Image** (`.png`/`.jpg`/`.gif`/`.webp`) — MCP `ImageContent` block. Oversized images are auto-resized via `sips` (macOS) or Pillow; hard ceiling 20 MB raw.
- **Notebook** (`.ipynb`) — parsed cells with inline outputs.
- **PDF** — not returned directly; stashed as `SupplementalContent` for next-turn injection as a `DocumentContent` block. Always pass `pages` to avoid blowing the payload budget.

### Write

Creates or overwrites a file.

| Arg         | Type   | Notes |
|-------------|--------|-------|
| `file_path` | string | Absolute path. Required. |
| `content`   | string | File contents. Required. |

Validation (via `FileState.check`):

- If the file exists and was not read this session → error.
- If the file was read but has been modified since (mtime advanced) → error.
- Parent directories must exist — `Write` does not create them.

### Edit

Performs an exact string replacement.

| Arg           | Type   | Notes |
|---------------|--------|-------|
| `file_path`   | string | Absolute path. Required. |
| `old_string`  | string | Text to replace. Must appear verbatim. Required. |
| `new_string`  | string | Replacement. Must differ from `old_string`. Required. |
| `replace_all` | bool   | Replace every occurrence; default `false`. |

Validation: same read-before-write / mtime checks as `Write`. With `replace_all=false`, `old_string` must be unique in the file — multiple matches → error, zero matches → error.

### Plan

Writes an externalized task plan to `<home>/plans/<agent-id>.yml`. Each call **replaces** the entire plan.

| Arg     | Type               | Notes |
|---------|--------------------|-------|
| `goal`  | string             | One-line description of the current work. Required. |
| `tasks` | array of `{description, status}` | `status` ∈ `pending` / `in_progress` / `done`. Required. |

Output is a short confirmation plus a rendered progress summary. A periodic PostToolUse hook (`create_plan_nudge_hook`, fires every 20 tool calls) re-injects the current plan as context so the agent stays aware of it.

### Message

Point-to-point messaging and channel pub/sub.

| Arg        | Type   | Notes |
|------------|--------|-------|
| `action`   | string | `send` / `subscribe` / `unsubscribe`. Required. |
| `to`       | string | Recipient agent ID (for `send`). |
| `channel`  | string | Channel name. Required for `subscribe`/`unsubscribe`; optional for `send` (broadcast). |
| `summary`  | string | Short line shown in notifications (for `send`). |
| `body`     | string | Full message body (for `send`). |
| `priority` | string | `low` / `normal` / `high` (default `normal`). |

Routing behavior:

- `action=send` with `channel` → daemon broadcast to all subscribers. Requires the daemon.
- `action=send` with `to` → daemon direct delivery, falling back to filesystem (writing to `<home>/inbox/<to>/`) if the daemon is down.
- `action=send` with both `to` and `channel` → daemon publishes to both in sequence.
- `action=subscribe`/`unsubscribe` → daemon updates subscription map; harness mirrors into `_desired_subscriptions` for resume.

A `send` with neither `to` nor `channel` errors; one with neither `summary` nor `body` errors.

### ActivateSkill

Loads a skill into session context.

| Arg    | Type   | Notes |
|--------|--------|-------|
| `name` | string | Skill name (matches `SKILL.md` frontmatter `name` and the folder). Required. |

Returns `"Skill '<name>' activated."`. A PostToolUse hook (`create_skill_context_hook`, matcher `mcp__kiln__activate_skill`) then reads `<home>/skills/<name>/SKILL.md`, strips frontmatter, and injects the body as `additionalContext` so it lands as a system-level block rather than a tool result. Calling again for the same skill re-injects.

See `skills.md` for the full skill system.

### ExitSession

Signals the harness to shut down cleanly — or to shut down and self-continue.

| Arg            | Type   | Notes |
|----------------|--------|-------|
| `continue`     | bool   | After shutdown, relaunch a new session that inherits subscriptions, canonical status, and handoff text. Default `false`. |
| `handoff`      | string | Prose describing what's in flight for the continuation. Delivered as an inbox message to the new session. Only used with `continue=true`. |
| `skip_summary` | bool   | Skip the session-end summary/memory protocol. Default `false`. |

Doesn't terminate the process itself — it sets flags on `SessionControl` (`quit_requested`, `continue_requested`, `handoff_text`, `skip_summary`). The harness drains remaining hook output, runs cleanup prompts, then returns.

See `lifecycle.md` for the full shutdown path.

## Examples

Bash with background jobs:

```python
# Kick off a long-running build
Bash(command="cd ~/Git/kiln && pytest -x",
     run_in_background=True)
# → "Background job started. Job ID: bg-1 ..."

# Check on it
Bash(background_job_id="bg-1")
# → Running/finished status + accumulated output

# Clean up when done
Bash(cleanup_background_job_id="bg-1")
```

Read → Edit flow:

```python
Read(file_path="/path/to/config.yml")              # required before Edit
Edit(file_path="/path/to/config.yml",
     old_string="heartbeat: 0",
     new_string="heartbeat: 30")
```

Message — direct and broadcast in a single session:

```python
# Subscribe once
Message(action="subscribe", channel="design-review")

# Broadcast to the channel
Message(action="send", channel="design-review",
        summary="Draft ready for review",
        body="Posted draft at scratch/draft.md — looking for feedback on §3.")

# Direct to a specific agent
Message(action="send", to="<agent-id>",
        summary="spec question",
        body="Does the spec require skeleton-only overrides or is content-only also supported?")
```

Plan update:

```python
Plan(goal="Merge scheduler branch",
     tasks=[
       {"description": "Rebase onto main", "status": "done"},
       {"description": "Resolve conflicts in config.py", "status": "in_progress"},
       {"description": "Run full test suite", "status": "pending"},
     ])
```

Self-continuation:

```python
ExitSession(
    continue=True,
    handoff="Left off mid-review of the design spec. Open question in §5: "
            "priority vs task distinction. Next session: pick up there and "
            "draft a pro/con table for the user.")
```

## Conventions

- **Read before you Write or Edit.** The `FileState` check is structural, not advisory — skipping it produces an error, and the error message tells you to Read first.
- **Prefer Edit over Write for modifications.** Write overwrites; Edit scopes the change. Also: Edit's uniqueness requirement catches "I meant the *other* match" bugs that Write would silently commit.
- **Use `Plan` only for multi-step work.** A one-step task doesn't need a plan — the plan nudge hook will ignore empty plans anyway.
- **Message `summary` is what notifications show.** Keep it one line. The `body` is where details go.
- **Shell state is yours to manage.** Bash's working directory, env, and aliases persist — use `cd` once, export variables once. Don't re-`cd` every call.
- **`ExitSession` is terminal only in autonomous sessions.** Calling it in an interactive session ends the user's conversation mid-flow. The tool description spells this out; heed it.

## Gotchas

- **PDFs need `pages`.** Reading a whole PDF through Read injects the entire document as supplemental content, which can swamp context. Always pass `pages="1-3"` or similar unless you know the doc is small.
- **`Edit` indentation must match exactly.** The `old_string` includes leading whitespace. Copying from the `cat -n` output means stripping the `N\t` prefix first (spaces + line number + tab) — include only the content after the tab.
- **`Message` with `to=` to an offline agent falls back to filesystem.** The recipient only sees the message when their inbox-check hook fires. If the recipient isn't running at all, the message queues in their inbox until the next session.
- **`ActivateSkill` doesn't validate the name before the hook fires.** A bogus name produces a cheerful "Skill 'foo' activated." followed by no actual injection (the hook silently drops when `SKILL.md` is absent). If the agent doesn't seem to have picked up a skill, check that the folder + `SKILL.md` actually exist.
- **`Plan` overwrites; incremental updates require passing the full task list.** Sending just the one task you changed wipes the rest. The tool description says this — worth restating.
- **`ExitSession` flags the harness; it doesn't stop execution.** Tool calls after `ExitSession` in the same turn will still run. The shutdown happens at the next harness loop iteration.
- **Background jobs are per-session.** A `run_in_background` job dies with the shell; `cleanup_background_job_id` releases temp files but doesn't resurrect output that's already been discarded.
