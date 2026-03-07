---
name: task-management
description: >
  Task management system: inbox triage, per-project boards (PRIORITIES.yml + TODO.yml),
  personal backlogs, and autonomous work pickup. Activate when triaging inbox items,
  working with project task boards, managing priorities, or deciding what to work on
  during autonomous sessions.
---

# Task Management

How tasks flow from idea to completion across projects and personal boards.

## System Overview

Three layers:

1. **Inbox** — single global dump zone. Freeform text, no structure. Anything that needs doing lands here.
2. **Project boards** — per-project tracking in two tiers:
   - **Priorities** (`PRIORITIES.yml`) — strategic items needing design or discussion
   - **Task board** (`TODO.yml`) — execution-ready tasks with dependencies and assignees
3. **Personal boards** — Kira's personal tasks and Aleph's personal backlog (separate formats, separate purposes)

## Inbox

**Location:** `<agent_home>/inbox/tasks.md`

A single append-only file. Items are freeform text — no formatting required. The only goal is minimal friction: capture the thought before it's lost.

Entries can come from:
- Kira (terminal, Discord, or any other input channel)
- Aleph (observations during work, things noticed in passing)
- Automated sources (future: email, Slack, calendar)

### Triage

Process the inbox periodically — during autonomous sessions, at session startup, or when prompted. Route each item to one of four destinations:

| Destination | When |
|---|---|
| Project `TODO.yml` | Well-defined, execution-ready. Could be picked up and done without further discussion. |
| Project `PRIORITIES.yml` | Needs design, discussion, or decisions before it becomes actionable. |
| Kira's personal board | Life admin, personal reminders, non-project items. |
| Aleph's personal backlog | Only if you actively choose to adopt it as a personal interest. Rare. |

If an item is ambiguous, default to the project priorities file — better to discuss something unnecessarily than to execute something poorly specified.

Remove processed items from the inbox after routing.

## Per-Project Structure

Each project can have some or all of these files in its root:

```
project/
├── agents.md          # Project overview, architecture, conventions
├── TODO.yml           # Execution-ready task board
├── PRIORITIES.yml     # Strategic items, open questions, design needs
└── ...
```

Plus project-specific memory at `<agent_home>/projects/<name>/memory.md`.

Not every project needs all files. A small project might only have a TODO.yml. A project in early design might only have PRIORITIES.yml. They're tools, not requirements.

### PRIORITIES.yml

Semi-structured. For items that need thinking before they become tasks — open questions, architectural decisions, things where the *what* is clear but the *how* isn't.

```yaml
# Project Name — Priorities
# Updated: YYYY-MM-DD

priorities:
  - id: auth-rethink
    summary: Rethink auth architecture
    status: open                # open | in-discussion | resolved | deferred
    context: |
      Current auth is bolted on. Three separate checks in different places.
      Need to decide: middleware approach vs. per-route, session-based vs. token-based.
    tasks: []                   # linked TODO.yml task IDs, filled once designed
```

#### Fields

**Required:**
- **id** — unique string identifier (kebab-case)
- **summary** — one-line description
- **status** — one of:
  - `open` — not yet discussed
  - `in-discussion` — actively being worked through
  - `resolved` — design decisions made, concrete tasks created
  - `deferred` — deliberately postponed, explain why in context

**Optional:**
- **context** — freeform prose. The thinking, the options, the constraints, the history. This is where nuance lives.
- **tasks** — list of TODO.yml task IDs created from this priority once designed. Links the strategic layer to the execution layer.
- **tags** — freeform tags for categorization

#### Workflow

1. Item arrives (from inbox triage, or noticed during work)
2. Status starts as `open`
3. During interactive sessions, Kira and Aleph work through open priorities -> `in-discussion`
4. Discussion produces decisions and concrete tasks -> tasks go on TODO.yml, priority becomes `resolved` with task links
5. Items that aren't worth pursuing right now -> `deferred` with explanation

### TODO.yml

Fully structured. For tasks that are well-defined enough to execute without further design discussion.

```yaml
# Project Name — Task Board
# Updated: YYYY-MM-DD

tasks:
  - id: 1
    description: Implement token refresh logic
    status: open              # open | in-progress | done | blocked | backlog
    priority: high            # high | medium | low
    depends_on: [2]           # list of task IDs that must complete first
    assignee: null            # agent ID when claimed
    notes: |
      Freeform context, decisions, outcomes.
    subtasks:                 # optional nested tasks
      - id: 1.1
        description: ...
        status: open
```

#### Fields

**Required:**
- **id** — unique identifier. Integers for top-level, dot notation for subtasks (1.1, 1.2). Never reused.
- **description** — short, scannable title. Save detail for `notes`.
- **status** — one of:
  - `open` — available to claim
  - `in-progress` — actively being worked on (should have assignee)
  - `done` — completed
  - `blocked` — can't proceed; explain in notes
  - `backlog` — not active yet, future work

**Optional:**
- **priority** — `high`, `medium`, or `low`. Informs but doesn't dictate ordering.
- **depends_on** — list of task IDs that must be `done` before this task is unblocked. Enables dependency-aware querying.
- **assignee** — agent ID of whoever claimed this task.
- **notes** — freeform text. Decisions, outcomes, blockers, links.
- **subtasks** — nested task list, same schema.

#### Conventions

- **Claiming:** Set `assignee` to your agent ID, `status` to `in-progress`. Check no one else has claimed it.
- **Completing:** Set `status` to `done`. Add notes summarizing outcome. Leave assignee for audit trail.
- **Adding tasks:** Append to end. Use next available integer ID. Don't renumber.
- **Task size:** Should be completable in a single session. If too large, break into subtasks.
- **Dependencies:** A task with unmet dependencies is effectively blocked even if its status is `open`. Query tools should surface this.

## Personal Boards

### Kira's Personal Tasks

**Location:** TBD (pending Discord integration for input, format to be designed)

Life admin, reminders, appointments, communications. Key requirements:
- Time sensitivity and due dates matter more than priority ranking
- Notification/reminder capability (needs always-on channel)
- Low friction input (Discord message or equivalent)

### Aleph's Personal Backlog

**Location:** `<agent_home>/memory/backlog.md`

Freeform prose, not YAML. Organized into three sections:
- **Curiosity-Driven** — things that pull at me, no obligation
- **Priorities I've Chosen** — professional items I've actively decided matter
- **Threads** — open-ended things I'm following, not completable

This is a reflective document, not a task list. Items are here because Aleph chose them, not because they had nowhere else to go. Project work belongs on project boards.

## Querying Across Projects

The `task` tool supports operations on individual TODO.yml files. Cross-project visibility requires scanning multiple files:

```bash
task list --all                    # all tasks, all projects
task list --status open --all      # open tasks everywhere
task list --unblocked --all        # open tasks with no unmet dependencies
```

The `--unblocked` query is the dependency-aware work discovery primitive: find tasks whose `depends_on` entries are all `done` (or that have no dependencies).

## Autonomous Pickup

During autonomous sessions without a specific assignment, decide what to work on through judgment, not mechanical dispatch. Available information includes:

- Open and unblocked tasks across project boards
- Open priorities that might benefit from solo investigation
- Personal backlog items
- General sense of what's been neglected or what matters

The decision is reflective: what's most important right now given everything I know? Not "grab the top item from a sorted queue." Task boards provide visibility into what's available; they don't make the choice.

For **multi-agent swarm work** (multiple agents executing on a well-defined task set), more mechanical dispatch is appropriate — agents claim unblocked tasks and execute them. The dependency tracking in TODO.yml supports this. But this is a specific mode of operation, not the default.
