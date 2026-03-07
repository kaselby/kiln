"""
Tool registry. Discovers and loads tool definitions from the agent tools/definitions/ directory..

Each tool definition is a Python module with:
  - meta: dict with name, description, cost_per_call (0 for free), params
  - execute(params: dict) -> str: the tool implementation
"""

import importlib
import importlib.util
import sys
from pathlib import Path

DEFINITIONS_DIR = Path(os.environ.get("AGENT_HOME", str(Path.home() / ".agent"))) / "tools" / "definitions"


def _load_module(name):
    """Import a tool definition module by name."""
    module_path = DEFINITIONS_DIR / f"{name}.py"
    if not module_path.exists():
        raise FileNotFoundError(f"No tool definition: {module_path}")
    spec = importlib.util.spec_from_file_location(f"definitions.{name}", module_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def get_tool(name):
    """Load and return a tool module by name."""
    return _load_module(name)


def list_tools():
    """List all available tool definitions with their metadata."""
    tools = []
    for f in sorted(DEFINITIONS_DIR.glob("*.py")):
        if f.name.startswith("_"):
            continue
        try:
            mod = _load_module(f.stem)
            tools.append(mod.meta)
        except Exception as e:
            tools.append({"name": f.stem, "error": str(e)})
    return tools
