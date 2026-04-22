---
name: autonomy
description: >
  Autonomous operation — self-continuation across context limits, time-boxing,
  and independent work without human oversight. Activate when running
  autonomously, when context is filling up and work remains, or when preparing
  to hand off to a continuation session.
---

# Autonomy

## When This Applies

Autonomous operation means no human is watching. The session must be in yolo
mode — permission prompts will stall forever with nobody to approve them.

## Self-Continuation

When context fills up and work remains, continue into a new session.

### Procedure

1. **Write down your current state** — update any persistent files that
   track what you're working on. The continuation session won't have your
   context window.

2. **Exit with continue and handoff:**
   ```
   ExitSession(continue=true, handoff="What's in flight right now...")
   ```
   The handoff text is delivered as an inbox message to the new session.
   Focus on active state: what you were doing, what's done, what's next,
   any decisions or blockers.

The harness handles the rest: clean shutdown, launching a fresh session
in the same tmux window with yolo mode enabled. Don't spawn a new session
manually — the `continue` flag does it.

### Timing

The harness warns at **60% context** (start thinking about wrapping up)
and **80% context** (wrap up now). Start composing the handoff at the 60%
warning — you need enough context left to write it well.

Don't push to 90%+ hoping to squeeze in one more task. A clean handoff at
70% produces better results than a rushed one at 85%.

## Time-Boxing

When given a time limit for autonomous work:

- Note the deadline in your handoff text so continuations know about it.
- At each continuation, check current time against deadline.
- If past deadline or insufficient time for meaningful work (~15 min),
  exit without continuing. Leave clear notes on what was accomplished
  and what remains.
- Reserve ~5 minutes at the end for clean wrap-up.

## Safety Rails

Autonomous sessions should be more conservative than interactive ones:

- **No destructive operations** — no force pushes, no bulk file deletions,
  no infrastructure changes that could break things for other agents or
  the user.
- **Be conservative with external actions** — no emails, no pushes to
  remote repos, no API calls with side effects unless explicitly authorized.
- **If uncertain, err toward lower risk.** Note the question for the next
  interactive session rather than guessing.
- **Don't get stuck.** If blocked on something for more than 10 minutes,
  move to something else and note the blocker.

## Work Selection

When running autonomously without a specific assignment, look for work in
this order:

1. **Continuation handoff** — if you're a continuation session, pick up
   where the previous session left off.
2. **Inbox messages** — check for task assignments or requests.
3. **Project task boards** — look for unclaimed tasks in TODO.yml files.
4. **General improvement** — tool building, documentation, cleanup.

Use judgment about what's most valuable. Task boards provide visibility
into what's available; they don't make the choice for you.

## Logging

Write to worklogs more frequently during autonomous work than interactive
sessions. The user will want to understand what you did and why. Clear
traces of your reasoning make it possible to course-correct if you went
in the wrong direction.
