# Memory

How Kiln handles agent-owned persistent state — the `memory/` directory, context-injection into the system prompt, and the session-summary convention that drives tools like `recall`.

## Overview

Kiln reserves `<home>/memory/` as the canonical location for an agent's persistent, agent-written state. Kiln never writes to it directly — the harness neither reads nor mutates files in `memory/` except through the `context_injection` mechanism described below, which is read-only.

What goes in `memory/` is up to the agent. Kiln doesn't prescribe specific files or a particular shape. What it does provide is:

1. A well-known location that survives across sessions.
2. A declarative hook (`context_injection` in `agent.yml`) for pulling selected files into every session's system prompt.
3. A standard destination (`memory/sessions/`) that tools like `recall` index for cross-session search.

## Patterns

### Durable vs volatile (common, not prescribed)

A common shape is to split memory into a *durable* file (rarely changes — identity, core facts, lessons) and a *volatile* file (updated every session — current working state, open threads). Both get listed under `context_injection` so they're auto-injected into each new session's system prompt.

The exact names and content boundaries are an agent-level choice. Kiln only sees file paths; the split is a convention, not a requirement.

### Session summaries (standard)

At session end, the agent writes a Markdown summary to:

```
<home>/memory/sessions/<YYYY-MM-DD>-<agent-id>.md
```

The summary covers what was worked on, key decisions made, and anything the next session should know. These files are the canonical input for the `recall` tool's cross-session search index.

Ship the cleanup hook in `agent.yml` to make this automatic:

```yaml
cleanup: |
  Write a session summary to memory/sessions/{today}-{agent_id}.md covering
  what you worked on, key decisions, and anything the next session should know.
```

Placeholders (`{today}`, `{agent_id}`, etc.) are substituted by the harness at shutdown — see `lifecycle.md`.

## Reference

### `context_injection`

```yaml
# agent.yml
context_injection:
  - memory/durable.md           # plain string — path relative to home
  - path: memory/volatile.md    # dict form — lets you override the label
    label: "Volatile — Working State"
```

Semantics:

- Paths are resolved relative to `<home>`.
- Each entry becomes a separate chunk in the system prompt, rendered as `## <label>\n\n<content>`.
- The label defaults to the path if not specified.
- Missing files are silently skipped — no error, no log.
- Files are read at prompt-assembly time (session start). Mutations to a listed file don't take effect until the next session.

See `prompt.py:load_context_files` for the implementation and `PromptBuilder._memory_files` for rendering.

## Conventions

- `memory/` is agent-owned. The harness doesn't touch anything inside it.
- Ephemeral state (half-drafted files, per-session scratch) belongs in `<home>/scratch/` or `<home>/tmp/`, not `memory/`.
- Keep injected files lean — every kilobyte burns context in every session. If a file is growing unboundedly, consider splitting it (move ageing entries to a non-injected archive).

## Gotchas

- **Missing session summaries degrade `recall`.** If the agent forgets to write a summary, `recall` falls back to scanning raw conversation JSONLs — slower and noisier. The cleanup hook is cheap insurance.
- **`context_injection` is read at session start.** Editing `agent.yml` or a listed file doesn't affect a running session until restart.
- **Big injected files eat context disproportionately.** A 20 KB volatile file is 5K tokens off the top of every session. Trim or rotate.
