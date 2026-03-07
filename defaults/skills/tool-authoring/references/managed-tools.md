# Managed Tools

Managed tools are Python modules in `tools/definitions/` that go through the
runner framework (`tools/lib/runner.py`). The runner provides argument parsing,
budget tracking, cost logging, and credential loading. Use these for paid API
integrations that need budget controls.

## Creating a Managed Tool

A managed tool is a Python module in `tools/definitions/` with two required
exports: a `meta` dict and an `execute` function.

```python
"""Tool description."""

meta = {
    "name": "tool-name",
    "description": "What it does.",
    "cost_per_call": 0.01,  # USD, conservative estimate
    "params": [
        {"name": "query", "required": True, "description": "Search query"},
        {"name": "max_results", "default": "5", "description": "Result count"},
    ],
}

def execute(params: dict) -> str | dict:
    """Run the tool. Return a string or dict with 'output' key."""
    # Implementation here
    return {"output": "result text", "actual_cost": 0.008}
```

Then create a shell wrapper in `tools/bin/`:

```bash
#!/usr/bin/env bash
# Invoke tool-name via the runner framework.
exec <agent_home>/venv/bin/python3 <agent_home>/tools/lib/runner.py tool-name "$@"
```

Make the wrapper executable: `chmod +x <agent_home>/tools/bin/tool-name`

The `meta` dict is parsed via AST at discovery time (no import side effects),
so the tool will appear in session context automatically.

## Budget System

The runner checks `<agent_home>/usage/budget.json` before each paid call. If
spending exceeds the limit, the call is refused (hard mode) or a warning is
printed (soft mode).

Manage the budget with `tool-budget`:

```
tool-budget                    # spending summary
tool-budget set-limit 10.00   # monthly limit in USD
tool-budget set-period weekly  # weekly or monthly
tool-budget set-mode hard     # hard = refuse, soft = warn
tool-budget list-tools        # list all managed tools with costs
tool-budget ledger            # recent usage entries
```

Usage is logged to `<agent_home>/usage/ledger.json`. Each entry records the tool
name, timestamp, estimated cost, and actual cost (if the tool reports one).

## Credentials

API keys are stored as files in `<agent_home>/credentials/`, one file per key.
The filename becomes the environment variable name:

```
<agent_home>/credentials/EXA_API_KEY     # contents: sk-...
<agent_home>/credentials/TAVILY_API_KEY  # contents: tvly-...
```

The runner loads these automatically before executing managed tools. The
credentials directory is gitignored.

## Runner Architecture

```
tools/
  lib/
    budget.py       -- budget tracking and enforcement
    budget_cli.py   -- CLI for budget management
    registry.py     -- tool discovery and loading
    runner.py       -- execution pipeline (parse args -> check budget -> run -> log)
  definitions/
    exa.py          -- tool definition (meta dict + execute function)
    tavily.py       -- tool definition
  bin/
    exa             -- shell wrapper -> runner.py exa
    tavily          -- shell wrapper -> runner.py tavily
    tool-budget     -- shell wrapper -> budget_cli.py
    tool-run        -- shell wrapper -> runner.py (generic)
```

The runner pipeline for a paid tool call:

1. Parse arguments against the `meta.params` spec
2. Load credentials from `<agent_home>/credentials/`
3. Check budget — refuse or warn if over limit
4. Execute the tool's `execute(params)` function
5. Log the call to `<agent_home>/usage/ledger.json`
6. Return the result
