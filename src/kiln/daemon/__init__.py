"""Kiln daemon — shared coordination for multi-agent systems.

The daemon owns live state that is intrinsically shared across agents:
presence, channel subscriptions, message routing, and platform adapter
lifecycle. Durable storage (inboxes, session logs, agent config) stays
in agent homes.

Modules:
    kiln.daemon.protocol    — wire message types and serialization
    kiln.daemon.config      — daemon configuration
    kiln.daemon.client      — agent-side daemon client
    kiln.daemon.state       — in-memory registries (presence, channels, surfaces, bridges)
    kiln.daemon.server      — Unix socket server and event bus
    kiln.daemon.management  — session lifecycle actions
"""
