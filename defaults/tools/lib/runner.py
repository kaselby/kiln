"""
Tool runner. Handles argument parsing, budget checks, execution, and logging.

Usage: python -m tools.lib.runner <tool-name> [args...]

This is the entry point that shell wrappers call.
"""

import json
import sys
import os
from pathlib import Path

# Ensure the tools directory is importable
sys.path.insert(0, str(Path(os.environ.get("AGENT_HOME", str(Path.home() / ".agent"))) / "tools"))

# Load credentials from the agent credentials directory. (one file per key, filename = env var name)
_creds_dir = Path(os.environ.get("AGENT_HOME", str(Path.home() / ".agent"))) / "credentials"
if _creds_dir.is_dir():
    for _f in _creds_dir.iterdir():
        if _f.is_file() and not _f.name.startswith("."):
            _key = _f.name
            if _key not in os.environ:
                os.environ[_key] = _f.read_text().strip()

from lib import budget, registry


def parse_args(params_spec, argv):
    """
    Parse CLI arguments against a tool's param spec.

    Supports two styles:
      - Positional: args mapped to params in order
      - Flags: --name value pairs

    Returns a dict of param_name -> value.
    """
    result = {}

    # Check for flag-style args
    if any(a.startswith("--") for a in argv):
        i = 0
        positional_idx = 0
        while i < len(argv):
            if argv[i].startswith("--"):
                key = argv[i][2:].replace("-", "_")
                if i + 1 < len(argv):
                    result[key] = argv[i + 1]
                    i += 2
                else:
                    result[key] = True
                    i += 1
            else:
                # Positional arg mixed with flags
                if positional_idx < len(params_spec):
                    result[params_spec[positional_idx]["name"]] = argv[i]
                    positional_idx += 1
                i += 1
    else:
        # Pure positional
        for i, arg in enumerate(argv):
            if i < len(params_spec):
                result[params_spec[i]["name"]] = arg

    return result


def run(tool_name, argv):
    """Load a tool, check budget, execute, log usage."""
    try:
        tool = registry.get_tool(tool_name)
    except FileNotFoundError:
        print(f"Error: unknown tool '{tool_name}'", file=sys.stderr)
        print(f"Available tools:", file=sys.stderr)
        for t in registry.list_tools():
            print(f"  {t['name']}: {t.get('description', '?')}", file=sys.stderr)
        sys.exit(1)

    meta = tool.meta
    params_spec = meta.get("params", [])
    params = parse_args(params_spec, argv)

    # Validate required params
    for p in params_spec:
        if p.get("required") and p["name"] not in params:
            print(f"Error: missing required parameter '{p['name']}'", file=sys.stderr)
            print(f"Usage: {tool_name} {_usage_str(params_spec)}", file=sys.stderr)
            sys.exit(1)

    # Budget check for paid tools
    cost = meta.get("cost_per_call", 0)
    if cost > 0:
        allowed, spent, limit, msg = budget.check_budget(cost)
        if not allowed:
            print(f"BLOCKED: {msg}", file=sys.stderr)
            sys.exit(2)
        if "WARNING" in msg:
            print(msg, file=sys.stderr)

    # Execute
    try:
        result = tool.execute(params)
    except Exception as e:
        print(f"Error executing {tool_name}: {e}", file=sys.stderr)
        sys.exit(1)

    # Log usage for paid tools
    if cost > 0:
        # Check if the tool returned an actual cost (some APIs report it)
        actual_cost = cost
        if isinstance(result, dict) and "actual_cost" in result:
            actual_cost = result.pop("actual_cost")

        log_params = {k: v for k, v in params.items() if k not in ("api_key",)}
        budget.log_usage(tool_name, actual_cost, params=log_params)

    # Output
    if isinstance(result, dict) and "output" in result:
        print(result["output"])
    elif isinstance(result, dict):
        print(json.dumps(result, indent=2))
    elif result is not None:
        print(result)


def _usage_str(params_spec):
    parts = []
    for p in params_spec:
        name = p["name"]
        if p.get("required"):
            parts.append(f"<{name}>")
        else:
            default = p.get("default", "")
            parts.append(f"[--{name} {default}]" if default else f"[--{name}]")
    return " ".join(parts)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python -m tools.lib.runner <tool-name> [args...]", file=sys.stderr)
        tools = registry.list_tools()
        if tools:
            print("\nAvailable tools:", file=sys.stderr)
            for t in tools:
                cost = t.get("cost_per_call", 0)
                cost_str = f" (${cost}/call)" if cost > 0 else ""
                print(f"  {t['name']}: {t.get('description', '?')}{cost_str}", file=sys.stderr)
        sys.exit(1)
    run(sys.argv[1], sys.argv[2:])
