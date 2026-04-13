### Exit Session

Exit the current session cleanly. The harness will handle session summaries and memory commits before shutdown.

- Call this when your task is complete (ephemeral agents) or when handing off to a continuation (autonomous agents).
- Set `continue` to true for self-continuation: the harness runs normal shutdown, then automatically launches a fresh session that picks up from the handoff.
- Use `handoff` to pass context to the continuation session. Describe what's in flight and what needs to happen next. Delivered as an inbox message to the new session.
- Do NOT use in interactive sessions — let the user decide when to end the conversation.
