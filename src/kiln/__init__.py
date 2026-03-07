"""Kiln — agent runtime library for Claude Code.

Composable building blocks for building agent harnesses.
Simple agents use the default KilnHarness; complex agents
import modules directly and compose their own.

Modules:
    kiln.config     — AgentConfig + load_agent_spec
    kiln.harness    — KilnHarness (default session manager)
    kiln.hooks      — Infrastructure hook factories
    kiln.names      — Agent name generation
    kiln.permissions — Permission system
    kiln.prompt     — Tool/skill discovery, session context builder
    kiln.registry   — Session tracking
    kiln.shell      — Persistent shell management
    kiln.tools      — MCP tool functions + schemas
"""

from .config import AgentConfig, load_agent_spec
from .harness import KilnHarness
from .names import generate_agent_name
from .registry import lookup_session, register_session

__all__ = [
    "AgentConfig",
    "KilnHarness",
    "generate_agent_name",
    "load_agent_spec",
    "lookup_session",
    "register_session",
]
