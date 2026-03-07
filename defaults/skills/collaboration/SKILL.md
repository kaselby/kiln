---
name: collaboration
description: >
  Multi-agent collaboration — spawning agents, coordinating via messages and
  task boards, working as a spawned agent. Activate when launching other agents,
  working alongside peers, or operating as a spawned agent that needs to
  communicate findings back.
---

# Collaboration

## Agent Types

**Persistent agents** get full session lifecycle — worklogs, session summaries, inbox. Use when the agent's observations and thinking matter beyond its direct output: investigation, design work, code review, anything where unplanned discoveries are likely.

**Ephemeral agents** are disposable workers — no session summaries, no persistent state. Use when only the output matters: code changes, data extraction, formatting, well-specified transformations. Named with a `_` prefix automatically.

The deciding factor is not task complexity — ephemeral agents are just as capable. It's whether you care about the agent's *process* or just its *product*.

## Launching Agents

### Persistent agents

```bash
kiln run <agent-spec> --detach --mode yolo [--prompt "task description"]
```

### Ephemeral agents

```bash
kiln run <agent-spec> --detach --ephemeral --mode yolo [--prompt "task description"]
```

**Always use `--mode yolo`** for spawned agents — permission prompts stall forever since nobody is watching their tmux session.

Use `--prompt` to send initial instructions, or send a message after launch:

```
message(action="send", to="<agent-id>", summary="Task assignment", body="detailed instructions...")
```

Prefer smaller models for ephemeral workers when the task allows it — `--model sonnet` for most tasks, `--model haiku` for trivial ones.

## Coordination

### Messages

Point-to-point messages and channel broadcasts are the primary coordination mechanism.

```
# Direct message
message(action="send", to="agent-id", summary="...", body="...")

# Broadcast to all channel subscribers
message(action="send", channel="channel-name", summary="...", body="...")

# Subscribe/unsubscribe
message(action="subscribe", channel="channel-name")
message(action="unsubscribe", channel="channel-name")
```

Use channels when multiple agents need shared awareness of the same work. Use direct messages for task assignments, results, and point-to-point coordination.

### Task Board

Use the `task` tool for TODO.yml operations — it handles file locking to prevent concurrent edit conflicts.

```bash
task list                          # show all tasks
task claim 2.1                     # claim a task (sets assignee + in-progress)
task status 2.1 done               # mark complete
task release 2.1                   # release a claimed task
```

When multiple agents modify the same codebase, assign file-boundary ownership to prevent merge conflicts.

### tmux

```bash
tmux list-sessions                      # list running agents
tmux capture-pane -t <agent-id> -p      # peek at output
tmux kill-session -t <agent-id>         # kill a stuck agent
```

Let persistent agents finish and exit naturally. Check `capture-pane` before killing. Ephemeral workers can be killed if stuck or no longer needed.

## Working as a Spawned Agent

When you've been spawned by another agent:

1. **Check your inbox** for the spawning message — it describes the work and context.
2. **If no message**, check TODO.yml for unclaimed tasks, or ask the spawner what's needed.
3. **Push back if the assignment is wrong.** If the approach is flawed or you see something the spawner missed, say so. The instruction is a starting point, not a mandate.
4. **Stay responsive to messages** during your work. The spawner or peer agents may have questions, corrections, or new information.

### Before Exiting

Message whoever spawned you with your findings:
- What you did and what the outcome was
- Any problems, surprises, or open questions
- Reasoning for non-obvious decisions, not just conclusions

If you wrote code, say which files changed and what the key design choices were.

## Guidelines

- **Don't default to working alone.** Spawning an agent is cheap. If you're stuck, want a second opinion, or would benefit from parallel work, spin one up.
- **Let agents orient before loading them up.** A brief `--prompt` plus a follow-up message works better than a wall of text in the prompt.
- **File-boundary ownership for parallel code changes.** Assign each agent specific files or modules to prevent merge conflicts.
- **Coordinate, don't command.** Spawned agents have full autonomy. Give them context and goals, not step-by-step instructions.
