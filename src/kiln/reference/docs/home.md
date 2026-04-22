# Agent Home

The layout and ownership model of an agent's home directory — the single root that holds everything Kiln and the agent itself write.

## Overview

Every Kiln agent has a **home directory** — a single root (typically `~/.<agent>/`) containing the agent's identity, configuration, state, tools, skills, memory, and all runtime artifacts. Kiln doesn't scatter data across `~/.config/`, `~/.cache/`, or other XDG-ish locations. Everything lives under one tree so the agent can inspect and modify its own environment with plain shell commands.

Paths throughout Kiln's reference docs use `<home>` as a placeholder for the absolute path to the current agent's home. At runtime, `KILN_AGENT_HOME` (and the shorter alias `AGENT_HOME`) point at this directory for any shell tool that needs it.

## Architecture

Top-level layout of a populated home:

```
<home>/
  <AGENT>.md                 # identity doc — auto-injected as first chunk of system prompt
  agent.yml                  # agent config (model, tools, hooks, context_injection, ...)

  memory/                    # agent-written persistent state
    sessions/                # session summaries (<YYYY-MM-DD>-<agent-id>.md)
    <other>.md               # agent's own shape (durable/volatile/...)

  tools/                     # agent's shell tools — added to PATH at session start
    core/                    # tier 1 — full specs in session context
    library/                 # tier 2 — one-liner listing + registry.yml
    definitions/             # (optional) managed Python tools
    bin/                     # (optional) manually managed scripts

  skills/                    # SKILL.md-based skill packages (core/ + library/)

  state/                     # Kiln-written per-session runtime state
    session-config-<id>.yml  # live mutable config (mode, heartbeat, tags)
    trust-<platform>.yml     # gateway trust state

  logs/                      # Kiln-written observability
    session-registry.json    # all known sessions
    session-state/<id>.yml   # per-session snapshot (system_prompt, subs, tokens)
    stderr-<id>.log          # harness stderr
    conversations/           # archived transcripts
    tool-usage.jsonl         # tool/skill invocation log

  inbox/<agent-id>/          # messages waiting for a specific session
  plans/<agent-id>.yml       # current Plan tool output

  sessions/                  # (optional) agent-owned session workspace
    live/                    # running-session working files
    archived/                # post-session artefacts

  projects/<name>/           # (optional) project-scoped workspaces
  templates/                 # (optional) agent.yml templates for repeated session shapes
  scratch/                   # ephemeral work — not indexed, fair game to delete
  tmp/                       # shorter-lived scratch
  credentials/               # per-env-var secret files (agent-owned, never checked in)

  kiln-doc/                  # (optional) per-agent Kiln reference overrides
    skeleton.md
    <Heading>.md
    placeholders.yml
```

Not every agent has every directory — minimal agents may only have `agent.yml`, `identity.md`, `memory/`, and `tools/`. The rest is created on demand.

### Ownership

Three ownership tiers, roughly corresponding to who writes what:

| Tier | Who writes | What goes there |
|------|------------|-----------------|
| Kiln-written | The harness / daemon | `state/`, `logs/`, `sessions/live/`, `inbox/`, `plans/` |
| Agent-written | The running agent | `memory/`, `tools/`, `skills/`, `scratch/`, `tmp/`, `projects/`, `kiln-doc/`, identity doc |
| User-provided | The user, out-of-band | `credentials/`, `agent.yml` (usually) |

Kiln treats agent-written files as read-only except through the narrow channels it owns (copying `defaults/` into a new agent at `kiln init`, running agent-authored hooks, etc.). Conversely, Kiln-written files are live runtime state — agents can read them (and do, for the `sessions` tool etc.) but shouldn't edit them by hand mid-session.

### Scaffolding

`kiln init <name>` creates a new home with:

- `agent.yml` from the selected template,
- an empty `<AGENT>.md` identity stub,
- empty `inbox/`, `logs/`, `memory/`, `plans/`, `scratch/`, `state/` directories,
- `defaults/tools/` and `defaults/skills/` copied in as starter content.

After init, the tools and skills are the agent's — edits, additions, and deletions belong to the agent, not Kiln. Kiln doesn't re-sync defaults on upgrade.

## Reference

### The `<home>` placeholder

Throughout this reference, `<home>` stands in for the agent's absolute home path. At runtime:

- Shell tools see `$KILN_AGENT_HOME` (primary) and `$AGENT_HOME` (alias) pointing at the same path.
- Python code uses `AgentConfig.home` (a `pathlib.Path`).
- The Kiln reference chunk of the system prompt receives `{home_dir}` as a placeholder substitution — see `kiln.md`'s header.

### Cross-references

- `memory.md` — what goes in `memory/` and how `context_injection` exposes it.
- `tools.md` — how Kiln discovers and renders `tools/core/` and `tools/library/`.
- `skills.md` — same for `skills/`.
- `lifecycle.md` — what writes `state/`, `logs/`, `inbox/`, `plans/`, and when.
- `messaging.md` — inbox delivery format, trust state.
- `gateway.md` — `credentials/` conventions for platform adapters.
- `customization.md` — `kiln-doc/` override layers, session templates, and custom harness scaffolding.

## Conventions

- **One home per agent.** Agents don't share `memory/`, `state/`, or `inbox/` — separate roots even for agents on the same machine.
- **Agent homes are agent-owned after init.** `kiln init` seeds defaults once; after that, don't treat Kiln's `defaults/` as authoritative.
- **Secrets live under `credentials/`.** Each file is named after the env var it populates (`credentials/TAVILY_API_KEY` → `$TAVILY_API_KEY`). Shell tools look here; nothing else should.
- **Ephemeral work goes in `scratch/` or `tmp/`.** Don't dump half-formed artifacts into `memory/` — they'll end up in context every session.

## Gotchas

- **Session-ID-namespaced dirs (`inbox/<agent-id>/`, `plans/<agent-id>.yml`) are per-session, not per-agent.** Two simultaneous sessions of the same agent have separate inboxes and separate plans.
- **`kiln init` copies `defaults/` — it doesn't symlink or track.** Upgrading Kiln does not update an existing home's tools or skills.
- **`home` in `agent.yml` matters for Claude's conversation archive.** The backend derives the JSONL path from the home dir's encoded form; renaming the home after an agent has been running will orphan past archives silently. Pick a home path before first run and keep it.
- **`credentials/` should be gitignored.** Agent homes are frequently git-tracked for auto-commit on session end — credentials must not land in a repo.
