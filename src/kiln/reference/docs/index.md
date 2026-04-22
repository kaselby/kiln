# Kiln Reference Docs

Auto-generated from sibling docs — run `scripts/build_docs_index.py`
to regenerate. The drift test in `test_prompt.py` enforces that the
committed index matches the rendered output.

- [`builtins.md`](./builtins.md) — **Built-In Tools.** Full reference for the tools served by Kiln's standard MCP server — the ones that require harness-level wiring (shared shell, file-state tracking, session control, daemon access) and can't be written as shell scripts
- [`collaboration.md`](./collaboration.md) — **Collaboration.** How Kiln spawns, identifies, and coordinates multi-agent work — the `kiln run` lifecycle, templates, tags, and the agents registry
- [`gateway.md`](./gateway.md) — **Gateway.** The daemon-hosted service that bridges agents to external platforms — Discord today, designed for more
- [`home.md`](./home.md) — **Agent Home.** The layout and ownership model of an agent's home directory — the single root that holds everything Kiln and the agent itself write
- [`lifecycle.md`](./lifecycle.md) — **Session Lifecycle.** How a Kiln session starts, runs, and stops — including resume, self-continuation, and the state artifacts each phase reads and writes
- [`memory.md`](./memory.md) — **Memory.** How Kiln handles agent-owned persistent state — the `memory/` directory, context-injection into the system prompt, and the session-summary convention that drives tools like `recall`
- [`messaging.md`](./messaging.md) — **Messaging.** How agents exchange messages through the daemon — direct sends, channel pub/sub, inbox delivery, and trust resolution
- [`skills.md`](./skills.md) — **Skills.** How Kiln discovers, lists, and loads skills — packaged domain knowledge that an agent opts into for a session
- [`tools.md`](./tools.md) — **Shell Tools.** How Kiln discovers, renders, and exposes agent-owned shell tools
