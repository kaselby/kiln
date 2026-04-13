### Message

Send messages to other agents and manage channel subscriptions.

- **Point-to-point:** Use `to` with an agent ID to send a direct message. The message is delivered to the recipient's inbox.
- **Broadcast:** Use `channel` to send to all subscribers of a channel. Both `to` and `channel` can be used in the same call.
- **Subscribe/Unsubscribe:** Use `action="subscribe"` or `action="unsubscribe"` with a `channel` name to manage your subscriptions.
- Every sent message requires a `summary` (shown in notifications) and a `body` (full content).
- Use `priority` ("low", "normal", "high") to signal urgency. Default is "normal".
