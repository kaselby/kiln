"""Gateway service — platform integration for the Kiln daemon.

Connects the daemon's internal messaging world to external platforms
(Discord, Slack, etc.) via platform adapters. Owns surfaces, bridges,
platform-specific RPC handlers, and adapter lifecycle.
"""
