"""
Budget tracking for paid tools.

Maintains a ledger of API costs and enforces spending limits.
Config lives at <agent_home>/usage/budget.json, ledger at <agent_home>/usage/ledger.json.
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path

USAGE_DIR = Path(os.environ.get("AGENT_HOME", str(Path.home() / ".agent"))) / "usage"
BUDGET_FILE = USAGE_DIR / "budget.json"
LEDGER_FILE = USAGE_DIR / "ledger.json"

DEFAULT_BUDGET = {
    "period": "monthly",       # "monthly" or "weekly"
    "limit": 5.00,             # dollars
    "hard_stop": True,         # refuse calls over budget vs warn
}


def _load_json(path, default):
    if path.exists():
        with open(path) as f:
            return json.load(f)
    return default


def _save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")


def get_budget():
    """Load budget config, creating default if missing."""
    budget = _load_json(BUDGET_FILE, None)
    if budget is None:
        budget = DEFAULT_BUDGET.copy()
        _save_json(BUDGET_FILE, budget)
    return budget


def get_ledger():
    """Load the full ledger."""
    return _load_json(LEDGER_FILE, {"entries": []})


def _period_start(period):
    """Get the start of the current budget period."""
    now = datetime.now(timezone.utc)
    if period == "monthly":
        return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    elif period == "weekly":
        # Monday of current week
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        start = start.replace(day=start.day - start.weekday())
        return start
    else:
        raise ValueError(f"Unknown budget period: {period}")


def spend_in_period(budget=None, ledger=None):
    """Calculate total spend in the current budget period."""
    if budget is None:
        budget = get_budget()
    if ledger is None:
        ledger = get_ledger()

    cutoff = _period_start(budget["period"]).isoformat()
    total = 0.0
    for entry in ledger["entries"]:
        if entry["timestamp"] >= cutoff:
            total += entry["cost"]
    return total


def check_budget(cost):
    """
    Check if a call costing $cost is within budget.
    Returns (allowed: bool, spend_so_far: float, limit: float, message: str).
    """
    budget = get_budget()
    ledger = get_ledger()
    spent = spend_in_period(budget, ledger)
    limit = budget["limit"]
    remaining = limit - spent

    if cost <= remaining:
        return True, spent, limit, f"${spent:.3f} / ${limit:.2f} spent this {budget['period']} period"

    msg = (
        f"Budget exceeded: ${spent:.3f} spent + ${cost:.3f} requested "
        f"= ${spent + cost:.3f} > ${limit:.2f} {budget['period']} limit"
    )
    if budget.get("hard_stop", True):
        return False, spent, limit, msg
    else:
        return True, spent, limit, f"WARNING: {msg} (soft limit, proceeding)"


def log_usage(tool_name, cost, params=None, note=None):
    """Record a tool call to the ledger."""
    ledger = get_ledger()
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "tool": tool_name,
        "cost": cost,
    }
    if params:
        entry["params"] = params
    if note:
        entry["note"] = note
    ledger["entries"].append(entry)
    _save_json(LEDGER_FILE, ledger)
    return entry


def summary():
    """Return a human-readable budget summary."""
    budget = get_budget()
    ledger = get_ledger()
    spent = spend_in_period(budget, ledger)
    limit = budget["limit"]
    period = budget["period"]

    # Per-tool breakdown for current period
    cutoff = _period_start(period).isoformat()
    by_tool = {}
    for entry in ledger["entries"]:
        if entry["timestamp"] >= cutoff:
            tool = entry["tool"]
            by_tool[tool] = by_tool.get(tool, 0.0) + entry["cost"]

    lines = [
        f"Budget: ${limit:.2f} / {period}",
        f"Spent:  ${spent:.3f} ({spent/limit*100:.1f}%)" if limit > 0 else f"Spent: ${spent:.3f}",
        f"Remaining: ${limit - spent:.3f}",
        "",
    ]
    if by_tool:
        lines.append("By tool:")
        for tool, cost in sorted(by_tool.items(), key=lambda x: -x[1]):
            lines.append(f"  {tool}: ${cost:.3f}")
    else:
        lines.append("No usage this period.")

    return "\n".join(lines)
