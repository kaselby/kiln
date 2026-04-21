"""Tests for kiln.config — tool-list validation and rename guidance."""

from pathlib import Path

import pytest

from kiln.config import (
    DEFAULT_TOOLS,
    KILN_TOOL_NAMES,
    AgentConfig,
)


def test_default_tools_are_all_valid_kiln_tools():
    """Every entry in DEFAULT_TOOLS should reference a known Kiln tool."""
    for entry in DEFAULT_TOOLS:
        assert entry.startswith("Kiln::"), entry
        _, name = entry.split("::", 1)
        assert name in KILN_TOOL_NAMES, (
            f"DEFAULT_TOOLS contains Kiln::{name} but KILN_TOOL_NAMES "
            f"does not list {name}"
        )


def test_resolve_tools_rejects_renamed_snake_case_names():
    """Old snake_case names fail loud with a rename hint."""
    config = AgentConfig(tools=["Kiln::plan"])
    with pytest.raises(ValueError, match=r"renamed to Kiln::Plan"):
        config.resolve_tools()

    for old, new in [
        ("message", "Message"),
        ("exit_session", "ExitSession"),
        ("activate_skill", "ActivateSkill"),
    ]:
        config = AgentConfig(tools=[f"Kiln::{old}"])
        with pytest.raises(ValueError, match=rf"renamed to Kiln::{new}"):
            config.resolve_tools()


def test_resolve_tools_rejects_unknown_kiln_tool():
    """Unknown Kiln:: names fail loud, listing the known set."""
    config = AgentConfig(tools=["Kiln::Nonesuch"])
    with pytest.raises(ValueError, match=r"isn't a tool Kiln"):
        config.resolve_tools()


def test_resolve_tools_allows_unknown_non_kiln_namespaces():
    """Agents can freely introduce their own namespaces; no validation."""
    config = AgentConfig(tools=["MyAgent::CustomThing", "Kiln::Bash"])
    resolved = config.resolve_tools()
    assert resolved["MyAgent"] == ["CustomThing"]
    assert resolved["Kiln"] == ["Bash"]


def test_resolve_tools_accepts_defaults():
    """Default tool list parses without errors."""
    config = AgentConfig()
    resolved = config.resolve_tools()
    assert set(resolved["Kiln"]) == set(KILN_TOOL_NAMES)


def test_kiln_tool_names_match_mcp_server_registration(tmp_path: Path):
    """KILN_TOOL_NAMES must match what create_mcp_server actually registers.

    Guards against drift — if a tool is added/removed in tools.py but the
    constant isn't updated, validation starts lying.
    """
    from kiln.tools import create_mcp_server

    _, _, _, mcp_tools = create_mcp_server(
        inbox_root=tmp_path / "inbox",
        skills_path=tmp_path / "skills",
        agent_id="test-agent",
    )
    registered_names = {t.name for t in mcp_tools}
    assert registered_names == set(KILN_TOOL_NAMES)
