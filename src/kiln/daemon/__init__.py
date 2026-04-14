"""Kiln daemon — shared coordination for multi-agent systems.

The daemon owns core coordination state: presence, channel subscriptions,
and message routing between agents on the same machine. Optional services
(gateway, scheduler) extend the daemon with additional capabilities — see
``kiln.services``.

Modules:
    kiln.daemon.protocol    — wire message types and serialization
    kiln.daemon.config      — daemon configuration
    kiln.daemon.client      — agent-side daemon client
    kiln.daemon.state       — in-memory registries (presence, channels)
    kiln.daemon.server      — Unix socket server, event bus, service host
    kiln.daemon.management  — session lifecycle actions
"""
