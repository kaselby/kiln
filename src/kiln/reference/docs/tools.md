# Shell Tools

How Kiln discovers, renders, and exposes agent-owned shell tools.

## Overview

Agent capabilities beyond the built-in API tools are provided by shell scripts living under the agent's home directory. Unlike MCP-served tools, shell tools don't consume any protocol surface — they're just executables on `PATH`, invoked via `Bash`. Kiln's only runtime responsibility is discovering them at startup and rendering a listing into the system prompt.

Tools are organized into two tiers so high-signal tools get full descriptions in context while the long tail is represented by one-liners the agent can expand on demand:

- **Core tools** (`<home>/tools/core/`) — discovered via `# ---` YAML headers in each script, rendered with full `name`, `arguments`, and `description`.
- **Library tools** (`<home>/tools/library/`) — listed in `library/registry.yml` as `name: one-liner`. Full docs come from the `tool-info` shell tool when the agent needs them.

A flat layout (no `core/` or `library/` subdirectory) is also supported — all discovered tools are rendered with full specs. Tiered layout is the recommended shape once an agent accumulates more than a handful of tools.

## Architecture

```
<home>/tools/
  core/                      # tier 1 — full specs in context
    tool-a
    tool-b
  library/                   # tier 2 — one-liner listing
    registry.yml             # authoritative name → brief map
    tool-x
    tool-y
  definitions/               # (optional) managed Python tools
    some_tool.py
  bin/                        # (optional) manually managed scripts, on PATH only
```

Tool discovery happens once per session, in `prompt.py:discover_tool_layout()`, called from `harness.py:_build_backend_config()` via `PromptBuilder`. The result is rendered as the `{tool_index}` placeholder inside the Kiln reference chunk of the system prompt.

At startup the harness also prepends `tools/`, `tools/core/`, `tools/library/`, and `tools/bin/` to the session's `PATH`, so tools are callable by bare name from Bash.

### Discovery

Two formats are recognized:

1. **Standalone scripts** — any executable file (or `.py` file) containing a `# ---` YAML comment header. Scanned at the top level and in immediate subdirectories of `tools_path`. Subdirectories `__pycache__`, `definitions`, and `lib` are skipped.

2. **Managed tools** — Python files in `tools/definitions/*.py` that expose a module-level `meta = { "name": ..., "description": ..., "cost_per_call": ... }` dict. Parsed with `ast.literal_eval` (no import, no execution).

Library tool briefs come from `tools/library/registry.yml`. If the registry is absent, library tools fall back to their own headers (same format as core).

### Rendering

The rendered listing has two sections when a tiered layout is detected:

```
Custom tools (invoke via Bash):
- **<name>** `<arguments>` — <description> [**[$X/call]** if cost set]

Tool library (use `tool-info <name>` for details):
- **<name>** — <one-liner>
```

A flat layout drops the second section and emits all tools under the first. Description whitespace is collapsed before rendering (so YAML block scalars don't produce ragged lines).

## Reference

### Core tool header

YAML inside a `# ---` / `# ---` comment fence. Must be the first heading-style block in the file (leading shebang is fine).

| Field         | Required | Notes                                                                 |
|---------------|----------|-----------------------------------------------------------------------|
| `name`        | yes      | Invocation name (usually the filename)                                |
| `description` | yes\*    | Multi-line OK. Used verbatim in context. Whitespace collapsed on render. |
| `brief`       | no       | If present, wins over `description` for the context listing           |
| `arguments`   | no       | Short usage signature, shown in backticks after the name              |
| `cost`        | no       | Per-call cost; renders as `**[$X/call]**` tag                         |

\* `description` or `brief` — at least one. Both missing = tool is skipped.

### Library registry

`tools/library/registry.yml`:

```yaml
# name: one-liner description
tavily:     Web search via Tavily API **[$0.008/call]**
hn:         Search and read Hacker News stories
```

Keys are tool names, values are the rendered one-liner. Non-string values are ignored. Tools present in `library/` but missing from the registry fall back to header-based discovery.

### Managed tool meta dict

```python
# tools/definitions/some_tool.py

meta = {
    "name": "some_tool",
    "description": "What this tool does.",
    "cost_per_call": 0.01,   # optional
}

def run(...): ...
```

Parsed statically via `ast.literal_eval` — the module is never imported during discovery, so heavyweight imports at module top-level are safe.

### Environment

Every session has these env vars set for tool scripts:

| Variable             | Meaning                                               |
|----------------------|-------------------------------------------------------|
| `KILN_AGENT_HOME`    | Absolute path to the agent's home directory           |
| `AGENT_HOME`         | Short alias for `KILN_AGENT_HOME`                     |
| `KILN_AGENT_ID`      | Current session ID (e.g. `<agent>-<adj>-<noun>`)      |
| `VIRTUAL_ENV`        | Agent's venv path, if `<home>/venv` exists            |
| `PATH`               | `tools/`, `tools/core/`, `tools/library/`, `tools/bin/`, then inherited |

## Examples

A minimal core tool:

```bash
#!/usr/bin/env bash
# ---
# name: ping
# brief: Check daemon heartbeat
# arguments: "[--verbose]"
# ---
set -euo pipefail
curl -s "${KILN_DAEMON_URL:-http://localhost:9876}/ping"
```

A Python tool with a multi-line description:

```python
#!/usr/bin/env python3
# ---
# name: recall
# brief: Session recall — hybrid search, passage extraction, Q&A synthesis
# arguments: "<find|dig|ask|files|show|list|reindex|stats> [args]"
# description: |
#   Search, rank, and synthesize across archived session summaries and JSONLs.
#   FTS5 index at $KILN_AGENT_HOME/state/history.db.
# ---

import sys
...
```

Registering the same tool in the library tier instead — `tools/library/registry.yml`:

```yaml
recall: Session recall — hybrid search, passage extraction, Q&A synthesis
```

Discovering tools programmatically (inside a harness):

```python
from kiln.prompt import discover_tool_layout

layout = discover_tool_layout(Path("~/.<agent>/tools").expanduser())
# -> {"core": [ {name, description, arguments, cost?}, ... ],
#     "library": {name: one_liner, ...}}
```

## Conventions

- **Combine related functionality into one tool with subcommands**, not many small tools. `todo add|list|show|done|...` rather than ten separate scripts. Fewer lines in the context listing.
- **Use `brief` + `description` together** when the one-line summary needs context. `brief` gets rendered; `description` lives in the header for `tool-info`.
- **Quote arguments strings** that contain shell metacharacters — YAML happily parses `[foo|bar]` as a flow sequence otherwise.
- **Library tier is the default destination.** Most tools belong in `library/` with a registry entry. Promote to `core/` only when the tool is used in nearly every session.
- **Cost tags for paid tools.** If a tool hits a metered API, set `cost` so the listing renders `**[$X/call]**` — agents can see the price before invoking.

## Gotchas

- **Header must be strictly `# ` prefixed, no blank lines inside the fence.** The parser stops at the first non-`# ` line after the opener. Comments with a leading `#!` (shebang) before the fence are fine; blank lines *inside* the fence break parsing silently.
- **Executable bit matters.** Standalone scripts without `+x` are skipped unless they end in `.py`. `chmod +x` is part of adding a tool.
- **Library registry.yml is authoritative for the listing.** If a tool exists in `library/` but isn't in `registry.yml`, the fallback kicks in only if the registry is empty or unparseable. Partial registries silently drop the unlisted tools from the context listing.
- **`tools/definitions/` is not on PATH.** Managed Python tools are expected to be invoked by their own harness (e.g. via `python -m`), not run directly as scripts.
- **Discovery is session-scoped.** Adding a new tool mid-session doesn't update the listing — it won't appear until the next session starts. The tool itself is callable immediately (it's just a file on disk), it just isn't advertised in the prompt.
