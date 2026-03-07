### Plan

Externalize your working plan via the `plan` MCP tool. Takes a `goal` (string) and `tasks` (array of `{description, status}` where status is `pending`, `in_progress`, or `done`). Each call replaces the entire plan.

- **Use this before starting complex work** — multi-file changes, new features, anything with more than a couple of steps. The act of writing the plan forces you to commit to an approach.
- **Update as you work** — mark tasks done, add new ones, adjust when requirements change. A periodic hook will remind you to keep it current.
- Plans are visible to coordinators and other agents.
- For simple tasks (one-step, obvious approach), skip this — don't over-plan a one-liner.
