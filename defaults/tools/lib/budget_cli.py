"""CLI for budget management. Invoked by tool-budget shell wrapper."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lib import budget, registry


def main():
    args = sys.argv[1:]
    cmd = args[0] if args else "summary"

    if cmd == "summary":
        print(budget.summary())

    elif cmd == "set-limit":
        if len(args) < 2:
            print("Usage: tool-budget set-limit <amount>", file=sys.stderr)
            sys.exit(1)
        b = budget.get_budget()
        b["limit"] = float(args[1])
        budget._save_json(budget.BUDGET_FILE, b)
        print(f"Budget limit set to ${float(args[1]):.2f}")

    elif cmd == "set-period":
        if len(args) < 2 or args[1] not in ("weekly", "monthly"):
            print("Usage: tool-budget set-period <weekly|monthly>", file=sys.stderr)
            sys.exit(1)
        b = budget.get_budget()
        b["period"] = args[1]
        budget._save_json(budget.BUDGET_FILE, b)
        print(f"Budget period set to {args[1]}")

    elif cmd == "set-mode":
        if len(args) < 2 or args[1] not in ("hard", "soft"):
            print("Usage: tool-budget set-mode <hard|soft>", file=sys.stderr)
            sys.exit(1)
        b = budget.get_budget()
        b["hard_stop"] = args[1] == "hard"
        budget._save_json(budget.BUDGET_FILE, b)
        print(f"Budget enforcement set to {args[1]} stop")

    elif cmd == "list-tools":
        tools = registry.list_tools()
        for t in tools:
            cost = t.get("cost_per_call", 0)
            cost_str = f"${cost:.3f}/call" if cost > 0 else "free"
            desc = t.get("description", "")
            print(f"  {t['name']:20s} {cost_str:>12s}  {desc}")

    elif cmd == "ledger":
        ledger = budget.get_ledger()
        entries = ledger["entries"][-20:]
        if not entries:
            print("No ledger entries.")
            return
        for e in entries:
            ts = e["timestamp"][:19].replace("T", " ")
            print(f"  {ts}  {e['tool']:12s}  ${e['cost']:.4f}")

    else:
        print(f"Unknown command: {cmd}", file=sys.stderr)
        print("Commands: summary, set-limit, set-period, set-mode, list-tools, ledger", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
