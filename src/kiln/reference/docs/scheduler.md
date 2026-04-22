# Scheduler

The daemon-hosted service for timed triggers — cron-style recurrence and one-shot ISO datetime fires. Agents declare entries in a YAML file; the scheduler evaluates due-ness on each tick and dispatches actions through the daemon's management layer. Like the gateway, it's an optional service (see `services.md` for the service model generally).

## Overview

The scheduler is three cleanly separated layers:

- **Schema** (`kiln.services.scheduler.models`) — declarative entries with a trigger and an action, loaded from YAML on every tick (no in-memory cache, so edits land immediately).
- **Engine** (`kiln.services.scheduler.engine`) — policy-free loop. `is_due()` answers "should this fire?"; `SchedulerLoop` polls on a configurable interval; `SchedulerExecutor` is an abstract dispatch interface. The engine has zero knowledge of daemons, agents, or platforms.
- **Executor** (`kiln.services.scheduler.executor`) — concrete bridge. `DaemonExecutor` translates abstract actions into real work via `ManagementActions.spawn_session` and tag-resolved inbox delivery.

The engine layer is testable against a mock executor with no daemon in sight; the executor layer is where agent-specific concerns (tag matching, durable inbox fallback) live. Keep the split in mind when extending.

## Architecture

```
~/.kiln/daemon/state/
  schedule.yml                  # declarative entries (user-managed)
  scheduler-state.json          # runtime state (daemon-written)

daemon/
  SchedulerService              # plugs into DaemonHost lifecycle
    SchedulerLoop               # background task, polls every check_interval
      is_due() ──> for each entry, decide fire/skip
      DaemonExecutor            # concrete executor
        execute_spawn  ──> daemon.management.spawn_session
        execute_deliver ──> resolve_by_tags → write inbox files
                         (or fallback to durable <agent>/inbox/_scheduled/)
```

### Triggers

Two kinds, both declarative:

- **`cron`** — recurring. Standard 5-field cron expression (`min hour dom month dow`) with optional `timezone` (IANA name like `America/Toronto`). Backed by `croniter`.
- **`at`** — one-shot. ISO 8601 datetime (with or without timezone offset — naive times are assumed UTC). Fires once; subsequent ticks see `completed: true` in state and skip.

### Actions

Two kinds, both declarative:

- **`spawn`** — launch a fresh session via `ManagementActions.spawn_session`. Fields: `agent`, `template`, `mode`, `prompt`. The new session gets `requested_by: "scheduler"` in its launch context.
- **`deliver`** — drop a message into one or more agent inboxes. Fields: `target`, `summary`, `body`, `priority`. See "Delivery targeting" below for the nuance.

### Delivery targeting

`deliver.target` is the part that rewards reading carefully:

```yaml
target:
  kind: agent            # only supported kind today
  agent: beth            # required
  tags: [canonical]      # optional; filters live sessions by tag
  match: any             # "any" | "all" — tag match mode
  fallback: inbox        # "inbox" | "drop" | "error"
```

Resolution at fire time:

1. `resolve_by_tags(agent, tags, match)` finds all matching **live** sessions.
2. **If any match:** the executor writes a message to *every* matching session's inbox. This is intentional — it enables broadcast patterns ("checkpoint all running workers") — but it means `target.tags: []` resolves to every live session of the agent.
3. **If none match:** the `fallback` decides:
   - `inbox` (default) — write to `<agent>/inbox/_scheduled/` on disk. The harness promotes these to the next session's main inbox on startup. Durable; survives daemon restarts.
   - `drop` — no-op. Useful for "only if someone's listening" patterns.
   - `error` — fail the action. Shows up in logs and state.

The fan-out-when-no-tags behavior is the single sharpest edge in the scheduler API. For "ping the PA" use `target.tags: [canonical]` with `match: any` — that targets exactly the canonical session. For broadcast, leave `tags` empty and accept the fan-out.

### Catchup

Cron and `at` entries both have a `catchup` field — a duration (`30m`, `4h`, `1d`) or `false` to disable. When the daemon wakes up after being stopped (or a service restart), entries whose trigger tick passed during the outage will fire if the miss is within the catchup window and hasn't already been recorded as fired. Default: `1h`. Disabling with `catchup: false` means missed ticks are lost silently.

The engine also caps lookback to roughly two check intervals when `catchup` is disabled, so a long-running loop with paused tasks doesn't backfire everything at once when it resumes.

### Loop cadence

`check_interval` (default 60s) is how often the loop polls. This is also the practical resolution of `at` triggers — an `at` for 12:00:30 fires within ~60s of the target, not at exactly 12:00:30. Cron triggers with sub-minute granularity don't make sense; the scheduler will silently drop ticks that happen between polls.

## Reference

### Daemon config — `services.scheduler`

```yaml
# ~/.kiln/daemon/config.yml
services:
  scheduler:
    enabled: true
    # schedule_path: ~/.kiln/daemon/state/schedule.yml     # optional override
    # state_path:    ~/.kiln/daemon/state/scheduler-state.json
    # check_interval: 60                                   # seconds
```

Enabling requires a daemon restart (`gateway restart`). When disabled, the service isn't instantiated, `croniter` isn't imported, and schedule.yml is ignored entirely.

### Schedule file

Declarative YAML at `~/.kiln/daemon/state/schedule.yml`. Loaded fresh on every tick — edit the file and changes take effect within `check_interval`. Single top-level key `schedules:` with a list of entries:

```yaml
schedules:
  - id: morning-buffer-check
    enabled: true
    trigger:
      kind: cron
      expr: "0 8 * * *"
      timezone: America/Toronto
    action:
      kind: deliver
      target:
        kind: agent
        agent: beth
        tags: [canonical]
        match: any
        fallback: inbox
      summary: "Morning buffer check"
      body: "Walk through the inbox and volatile.md for anything that needs attention."
      priority: normal
    catchup: 4h

  - id: weekly-review
    enabled: true
    trigger:
      kind: at
      time: "2026-04-26T10:00:00-04:00"
    action:
      kind: spawn
      agent: beth
      mode: yolo
      prompt: "Run the weekly review."
    catchup: 1d
```

Entries with unknown keys are rejected at load time with loud warnings (see `models._check_unknown_keys`). Duplicates by `id` are skipped. Invalid entries log and are ignored; the rest of the file still loads.

### Runtime state

`~/.kiln/daemon/state/scheduler-state.json` — daemon-owned; **do not edit by hand**. Contains:

```json
{
  "_last_check": "2026-04-22T12:43:30.681310+00:00",
  "entries": {
    "morning-buffer-check": {
      "fire_count": 3,
      "last_fired": "2026-04-22T12:00:00.123456+00:00"
    },
    "weekly-review": {
      "fire_count": 1,
      "last_fired": "2026-04-19T14:00:00.000000+00:00",
      "completed": true
    }
  }
}
```

Written atomically (tmp → rename). State for removed schedule entries lingers — it's small and harmless, but feel free to clean it up manually if the file gets noisy.

### `schedule` shell tool (agent-side)

Lives at `tools/core/schedule` in agent homes. Thin wrapper around the scheduler's own models — validation delegates to `parse_entry()`, so anything the tool accepts the engine will too.

| Subcommand | Purpose |
|------------|---------|
| `schedule list` | All entries with status (active / disabled / completed). |
| `schedule show <id>` | Full entry detail + runtime state (fire count, last fired). |
| `schedule add-spawn --agent A [flags]` | Add a spawn-action entry. |
| `schedule add-deliver --agent A --summary S --body B [flags]` | Add a deliver-action entry. |
| `schedule remove <id>` | Remove an entry. |
| `schedule enable <id>` / `disable <id>` | Flip the `enabled` flag. |
| `schedule validate` | Parse `schedule.yml` and report errors without firing. |

Trigger flags are shared across the add verbs: `--cron EXPR` or `--at TIME` (exactly one required), plus `--timezone TZ`, `--catchup DURATION`, `--no-catchup`, `--disabled`. Deliver-specific flags: `--tags T1,T2`, `--match any|all`, `--fallback inbox|drop|error`, `--priority`. Spawn-specific: `--template`, `--mode`, `--prompt`.

IDs are auto-generated (`<agent>-<action>-<hex6>`) if `--id` is omitted.

Run `tool-info schedule` for the full flag reference.

### Executor interface

Custom executors implement `kiln.services.scheduler.engine.SchedulerExecutor`:

```python
class MyExecutor(SchedulerExecutor):
    async def execute_spawn(self, action: SpawnAction) -> ActionResult: ...
    async def execute_deliver(self, action: DeliverAction) -> ActionResult: ...
```

The engine calls `execute()` which dispatches by action type. Useful for testing — build a mock executor that records invocations, drive `SchedulerLoop.run_once()` with synthetic `now` values, and assert fire ordering without spinning up a daemon.

## Examples

### Recurring delivery to canonical

```bash
schedule add-deliver \
  --agent beth \
  --summary "Morning inbox check" \
  --body "Walk through inbox and volatile.md for anything that needs attention." \
  --tags canonical \
  --cron "0 8 * * *" \
  --timezone America/Toronto
```

### One-shot spawn for a scheduled task

```bash
schedule add-spawn \
  --agent beth \
  --mode yolo \
  --prompt "Run the weekly review — check open todos, draft summary." \
  --at "2026-04-26T10:00:00-04:00"
```

### Broadcast to every live session

```bash
schedule add-deliver \
  --agent beth \
  --summary "Checkpoint now" \
  --body "All beth workers: save progress and report status." \
  --cron "0 */4 * * *"
# No --tags → fans out to every live beth session at fire time.
```

### Durable delivery when no session is live

```bash
schedule add-deliver \
  --agent beth \
  --summary "Tomorrow's review reminder" \
  --body "Start the weekly review when you come online." \
  --tags canonical \
  --fallback inbox \
  --at "2026-04-26T08:00:00-04:00"
# If no canonical session is live, lands in inbox/_scheduled/
# and is promoted on the next session startup.
```

### Inspecting what fired

```bash
schedule show morning-buffer-check
# Shows enabled, trigger, action, last_fired, fire_count.
gateway logs --limit 50 | grep -i scheduler
```

## Conventions

- **Tag every scheduled `deliver` target.** Empty `tags` fans out to every live session of the agent. Unless broadcast is what you want, target specifically — `tags: [canonical]` for the PA, `tags: [worker]` for a specific role, etc.
- **Use `at` for one-shots, `cron` for recurrence.** Don't fake one-shots with `cron` expressions — the engine's dedupe logic assumes cron is recurring.
- **Prompts in `spawn` actions should be self-contained.** The scheduled session has no chat history — treat the prompt as the full briefing.
- **Keep deliver bodies actionable.** The receiving session reads the body as an inbox notification. "Run the weekly review" is useful; "heads up" is not.
- **Don't hand-edit `scheduler-state.json`.** Runtime state is daemon-owned. If you need to reset an `at` entry's `completed: true`, delete the relevant entry from the JSON while the daemon is stopped.
- **Restart the daemon when enabling the service.** There's no hot-reload for service enablement. Schedule entries themselves hot-reload every tick — only the service being on/off requires restart.

## Gotchas

- **`at` triggers fire within ~`check_interval` of the target.** A 60s poll means an `at: 12:00:00` entry fires somewhere in `[12:00:00, 12:01:00]`. For second-level precision, you'd need to lower `check_interval` — but the engine wasn't designed for that, and the whole scheduler model is coarse-grained by intent.
- **No tags = broadcast.** The executor iterates *all* matching sessions. If four `beth` sessions are live and your entry has `target.tags: []`, all four get the message. This surprised me during smoke testing — now documented.
- **Fallback writes to `<agent>/inbox/_scheduled/`, not a session inbox.** The harness's `inbox_check_hook` promotes these into the next session's real inbox on startup. If you see a message stuck in `_scheduled/`, the target agent hasn't started a session since the fire.
- **`catchup` defaults to 1 hour.** A daemon offline for 45 minutes will replay recent misses; one offline for 2 hours won't. Tune `catchup` per entry based on how important catching the miss is. `catchup: false` disables entirely — missed ticks are lost.
- **State for removed entries lingers.** `scheduler-state.json` keeps last-fired data even after you remove an entry from schedule.yml. Harmless but can accumulate noise over time. Clean up by hand while the daemon is stopped if it bothers you.
- **Spawned sessions run with `requested_by: "scheduler"`.** If a spawned session's startup logic keys off the requester identity (most don't), scheduler-spawned sessions won't match other spawn contexts.
- **Unknown YAML keys are rejected.** The loader uses strict key-set validation — a typo in an entry (`trigger: cronn`, `catchup_window: 4h`) silently skips the whole entry with a warning. Run `schedule validate` to see the rejection reasons.
- **Timezone matters for cron, not for `at` with explicit offset.** An `at` string with a `-04:00` offset is unambiguous; a cron entry without `timezone` defaults to UTC, which rarely matches human intent. Set `--timezone America/Toronto` (or equivalent) for anything tied to local time.
- **The scheduler can't bypass an agent to post to Discord.** There's no "deliver to platform" action. If you want "text me at 8am," schedule a `deliver` to canonical with a body instructing them to DM the user. This is intentional — keeps the scheduler agent-centric.
