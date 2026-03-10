---
name: tool-authoring
description: >
  Creates and modifies tools in the Kiln tool system. Covers the two tool
  types (standalone scripts and managed definitions), the header contract for
  auto-discovery, budget tracking for paid APIs, and conventions for testing
  and documenting tools. Activate when building a new tool, modifying an
  existing one, or debugging tool discovery issues.
---

# Tool Authoring

Tools live at `<agent_home>/tools/` and are invoked via Bash. There are two types:

**Standalone scripts** — executable files at the top level of `tools/`. Simple,
self-contained, no framework overhead. Use these for most tools.

**Managed definitions** — Python modules in `tools/definitions/` that go through
the runner framework (`tools/lib/runner.py`). The runner provides argument
parsing, budget tracking, cost logging, and credential loading. Use these for
paid API integrations that need budget controls.

Both types are auto-discovered at session start and listed in the session
context. The discovery contract is what makes this work.

## Header Contract

Every standalone script must have a YAML comment header after the shebang:

```bash
#!/usr/bin/env bash
# ---
# name: tool-name
# description: One-line description of what the tool does
# arguments: "<required-arg> [optional-arg]"
# ---
```

For Python standalone scripts, use the same `# ---` comment format (before any
docstring):

```python
#!/usr/bin/env python3
# ---
# name: tool-name
# description: One-line description
# arguments: "<subcommand> [options]"
# ---
```

### Field Rules

| Field         | Required | Notes |
|---------------|----------|-------|
| `name`        | Yes      | Must match the filename. Lowercase with hyphens. |
| `description` | Yes      | Single line. Concise but specific. |
| `arguments`   | No       | Usage synopsis. **Must be quoted** — YAML interprets bare `[brackets]` as lists. Use `<angle>` for required args, `[brackets]` for optional. |

### Name Conventions

- Lowercase alphanumeric + hyphens: `fetch-raw`, `log-analysis`, `memory-commit`
- Name should describe the action: verb-noun pattern preferred
- Name must match the filename exactly

## Creating a Standalone Script

1. Create the script file in `<agent_home>/tools/`:

```bash
#!/usr/bin/env bash
# ---
# name: my-tool
# description: Does the thing
# arguments: "<input> [--flag]"
# ---

set -euo pipefail
# implementation here
```

2. Make it executable: `chmod +x <agent_home>/tools/my-tool`

3. Test it: run the tool via Bash and verify output.

4. Verify discovery: the tool will appear in the session context on the next
   session. To test immediately:

```bash
python3 -c "
from kiln.prompt import discover_tools
from pathlib import Path
import os
tools = discover_tools(Path(os.environ['AGENT_HOME']) / 'tools')
for t in tools:
    print(f'{t[\"name\"]}: {t[\"description\"]}')
"
```

Python standalone scripts should use `<agent_home>/venv/bin/python3` in the shebang
or invoke it explicitly, to ensure dependencies from the agent's venv are available.

## Creating a Managed Tool (Paid APIs)

For paid API integrations that need budget controls, use the managed tool
framework instead of standalone scripts. Managed tools go through the runner
(`tools/lib/runner.py`) which handles argument parsing, budget enforcement,
cost logging, and credential loading.

See `references/managed-tools.md` for the full guide — module structure, budget
system, credentials, and runner architecture.

## Discovery Internals

The harness function `_discover_tools()` runs at session start:

1. Scans top-level files in `<agent_home>/tools/` for executable scripts (or `.py`
   files) with `# ---` YAML comment headers. Parses `name`, `description`, and
   `arguments` from the header.

2. Scans `<agent_home>/tools/definitions/*.py` for Python modules. Uses `ast` to
   safely extract the `meta` dict without importing the module (avoiding side
   effects and import errors from missing API keys).

3. Results are injected into the session context under "Custom tools (invoke
   via Bash):" with the tool name, arguments, description, and cost tag (if
   applicable).

## Conventions

- Keep tools focused — one tool, one job. Compose via shell pipelines.
- Tools should be self-documenting: the header is for discovery, but include
  usage examples and notes as additional comments below the header block.
- Error output goes to stderr, results to stdout.
- Use `set -euo pipefail` in bash scripts.
- Exit codes: 0 = success, 1 = user error, 2 = system/infra error.
- Tools that need Python dependencies should use `<agent_home>/venv/bin/python3`
  rather than bare `python3`.
- The `AGENT_HOME` and `AGENT_ID` env vars are available in all tools.

