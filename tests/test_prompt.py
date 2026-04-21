import logging
from pathlib import Path

import pytest

from kiln.config import AgentConfig
from kiln.prompt import (
    BUILTIN_TOOL_SUMMARIES,
    KilnOverrides,
    KilnSections,
    PromptBuilder,
    load_kiln_overrides,
    load_tool_docs,
    parse_kiln_sections,
    render_builtins_summary,
    render_kiln_reference,
    substitute,
)


# ---------------------------------------------------------------------------
# load_tool_docs — legacy (will be deleted in commit #5)
# ---------------------------------------------------------------------------


def test_load_tool_docs_returns_empty_string_when_no_docs_exist(tmp_path: Path):
    extra_dir = tmp_path / "tool_docs"
    extra_dir.mkdir()

    assert load_tool_docs(["MadeUpTool"], extra_dirs=[extra_dir]) == ""



def test_load_tool_docs_renders_built_in_tool_guide_with_sections():
    guide = load_tool_docs(["Read", "Bash", "Message", "Plan", "WebSearch"])

    assert guide.startswith("## Built-In Tool Guide\n\n")
    assert "### Shell and Execution" in guide
    assert "### File Tools" in guide
    assert "### Workflow Tools" in guide
    assert "### Coordination and Session Tools" in guide
    assert "### Research Tools" in guide
    assert "#### Bash" in guide
    assert "#### Read" in guide
    assert "#### Message" in guide
    assert "#### Plan" in guide
    assert "#### WebSearch" in guide



def test_load_tool_docs_allows_extra_dir_overrides(tmp_path: Path):
    extra_dir = tmp_path / "tool_docs"
    extra_dir.mkdir()
    (extra_dir / "read.md").write_text("### Read\n\nOverride doc.")

    guide = load_tool_docs(["Read"], extra_dirs=[extra_dir])

    assert "#### Read\n\nOverride doc." in guide
    assert "The `file_path` parameter must be an absolute path." not in guide


# ---------------------------------------------------------------------------
# substitute — placeholder rendering primitive
# ---------------------------------------------------------------------------


def test_substitute_replaces_known_placeholders():
    assert substitute("hello {name}", {"name": "world"}) == "hello world"


def test_substitute_handles_multiple_occurrences():
    out = substitute("{a}/{b}/{a}", {"a": "x", "b": "y"})
    assert out == "x/y/x"


def test_substitute_leaves_unknown_placeholders_literal(caplog):
    with caplog.at_level(logging.WARNING, logger="kiln.prompt"):
        out = substitute("hello {missing}", {})
    assert out == "hello {missing}"
    assert any("missing" in rec.message for rec in caplog.records)


def test_substitute_warns_once_per_call_listing_all_missing(caplog):
    with caplog.at_level(logging.WARNING, logger="kiln.prompt"):
        substitute("{a} and {b}", {})
    assert len([r for r in caplog.records if "Unknown placeholder" in r.message]) == 1


def test_substitute_double_braces_escape_to_literal():
    # {{name}} should render as literal {name}, not be substituted.
    assert substitute("{{name}}", {"name": "world"}) == "{name}"


def test_substitute_does_not_recurse():
    # Single-pass: {b}'s value {a} is not re-expanded.
    out = substitute("{b}", {"a": "real", "b": "{a}"})
    assert out == "{a}"


def test_substitute_ignores_non_identifier_braces():
    # Braces around arbitrary text (JSON, prose) aren't touched.
    text = 'result: {"foo": 1}'
    assert substitute(text, {}) == text


def test_substitute_empty_text_returns_empty():
    assert substitute("", {"a": "b"}) == ""


# ---------------------------------------------------------------------------
# parse_kiln_sections
# ---------------------------------------------------------------------------


def _write(path: Path, text: str) -> Path:
    path.write_text(text)
    return path


def test_parse_kiln_sections_extracts_preamble_and_sections(tmp_path: Path):
    text = (
        "# Kiln Reference\n\n"
        "Intro paragraph.\n\n"
        "## First\n\n"
        "First body.\n\n"
        "## Second\n\n"
        "Second body.\n"
    )
    path = _write(tmp_path / "kiln.md", text)

    parsed = parse_kiln_sections(path)
    assert isinstance(parsed, KilnSections)
    assert "# Kiln Reference" in parsed.preamble
    assert "Intro paragraph." in parsed.preamble
    assert list(parsed.sections.keys()) == ["First", "Second"]
    assert parsed.sections["First"].strip() == "First body."
    assert parsed.sections["Second"].strip() == "Second body."


def test_parse_kiln_sections_handles_no_preamble(tmp_path: Path):
    text = "## Only\n\nBody.\n"
    parsed = parse_kiln_sections(_write(tmp_path / "k.md", text))
    assert parsed.preamble == ""
    assert list(parsed.sections.keys()) == ["Only"]


def test_parse_kiln_sections_ignores_non_h2_headings(tmp_path: Path):
    text = (
        "# H1 preamble\n\n"
        "## Section\n\n"
        "### Sub heading\n\n"
        "Body with **markdown**.\n"
    )
    parsed = parse_kiln_sections(_write(tmp_path / "k.md", text))
    assert list(parsed.sections.keys()) == ["Section"]
    assert "### Sub heading" in parsed.sections["Section"]


def test_parse_kiln_sections_warns_on_duplicate_heading(tmp_path: Path, caplog):
    text = "## Dup\n\nFirst.\n\n## Dup\n\nSecond.\n"
    with caplog.at_level(logging.WARNING, logger="kiln.prompt"):
        parsed = parse_kiln_sections(_write(tmp_path / "k.md", text))
    assert parsed.sections["Dup"].strip() == "First."
    assert any("Duplicate section" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# load_kiln_overrides
# ---------------------------------------------------------------------------


def test_load_kiln_overrides_empty_when_no_dir(tmp_path: Path):
    overrides = load_kiln_overrides(tmp_path)
    assert overrides == KilnOverrides()


def test_load_kiln_overrides_reads_skeleton(tmp_path: Path):
    doc = tmp_path / "kiln-doc"
    doc.mkdir()
    (doc / "skeleton.md").write_text("## Principles\n## Memory\n## Custom\n")

    overrides = load_kiln_overrides(tmp_path)
    assert overrides.skeleton == ["Principles", "Memory", "Custom"]


def test_load_kiln_overrides_skeleton_warns_on_duplicate(tmp_path: Path, caplog):
    doc = tmp_path / "kiln-doc"
    doc.mkdir()
    (doc / "skeleton.md").write_text("## A\n## A\n## B\n")

    with caplog.at_level(logging.WARNING, logger="kiln.prompt"):
        overrides = load_kiln_overrides(tmp_path)
    assert overrides.skeleton == ["A", "B"]
    assert any("Duplicate heading" in r.message for r in caplog.records)


def test_load_kiln_overrides_reads_content_files(tmp_path: Path):
    doc = tmp_path / "kiln-doc"
    doc.mkdir()
    (doc / "Memory.md").write_text("Custom memory section body.\n")
    (doc / "Principles.md").write_text("Agent-specific principles.\n")

    overrides = load_kiln_overrides(tmp_path)
    assert overrides.content == {
        "Memory": "Custom memory section body.",
        "Principles": "Agent-specific principles.",
    }


def test_load_kiln_overrides_reads_placeholders(tmp_path: Path):
    doc = tmp_path / "kiln-doc"
    doc.mkdir()
    (doc / "placeholders.yml").write_text("foo: bar\nchannels: '#general, #channels'\n")

    overrides = load_kiln_overrides(tmp_path)
    assert overrides.placeholders == {"foo": "bar", "channels": "#general, #channels"}


def test_load_kiln_overrides_raises_on_malformed_yaml(tmp_path: Path):
    doc = tmp_path / "kiln-doc"
    doc.mkdir()
    (doc / "placeholders.yml").write_text("this: is:\n   not valid: [\n")

    with pytest.raises(ValueError, match="Malformed YAML"):
        load_kiln_overrides(tmp_path)


def test_load_kiln_overrides_rejects_non_mapping_yaml(tmp_path: Path):
    doc = tmp_path / "kiln-doc"
    doc.mkdir()
    (doc / "placeholders.yml").write_text("- just\n- a\n- list\n")

    with pytest.raises(ValueError, match="mapping"):
        load_kiln_overrides(tmp_path)


# ---------------------------------------------------------------------------
# render_builtins_summary
# ---------------------------------------------------------------------------


def test_render_builtins_summary_renders_all_by_default():
    out = render_builtins_summary()
    for name in BUILTIN_TOOL_SUMMARIES:
        assert f"- **{name}** — " in out


def test_render_builtins_summary_filters_to_provided_tools():
    out = render_builtins_summary(["Kiln::Bash", "Kiln::Plan"])
    assert "- **Bash**" in out
    assert "- **Plan**" in out
    assert "- **Write**" not in out


def test_render_builtins_summary_strips_namespaces():
    out = render_builtins_summary(["MyAgent::Bash"])
    assert "- **Bash**" in out


def test_render_builtins_summary_skips_unknown_tools():
    out = render_builtins_summary(["Kiln::Bash", "Kiln::UnknownTool"])
    assert "- **Bash**" in out
    assert "UnknownTool" not in out


def test_render_builtins_summary_dedupes():
    out = render_builtins_summary(["Kiln::Bash", "Base::Bash", "Kiln::Bash"])
    assert out.count("- **Bash** — ") == 1


# ---------------------------------------------------------------------------
# render_kiln_reference — end-to-end integration of the above
# ---------------------------------------------------------------------------


def _make_default_reference(reference_dir: Path) -> None:
    reference_dir.mkdir(parents=True, exist_ok=True)
    (reference_dir / "kiln.md").write_text(
        "# Kiln Reference\n\n"
        "Shared intro about Kiln. Home is {home_dir}.\n\n"
        "## Principles\n\n"
        "Default principles.\n\n"
        "## Memory\n\n"
        "Default memory (should be rare).\n"
    )


def test_render_kiln_reference_default_only(tmp_path: Path):
    ref_dir = tmp_path / "reference"
    _make_default_reference(ref_dir)
    home = tmp_path / "home"
    home.mkdir()

    out = render_kiln_reference(home, ref_dir, {"home_dir": "/my/home"})
    assert out.startswith("# Kiln Reference\n\n")
    assert "Home is /my/home." in out
    assert "## Principles" in out
    assert "Default principles." in out
    assert "## Memory" in out


def test_render_kiln_reference_skeleton_override_reorders_sections(tmp_path: Path):
    ref_dir = tmp_path / "reference"
    _make_default_reference(ref_dir)
    home = tmp_path / "home"
    (home / "kiln-doc").mkdir(parents=True)
    (home / "kiln-doc" / "skeleton.md").write_text("## Memory\n## Principles\n")

    out = render_kiln_reference(home, ref_dir, {"home_dir": "/my/home"})
    memory_idx = out.index("## Memory")
    principles_idx = out.index("## Principles")
    assert memory_idx < principles_idx


def test_render_kiln_reference_content_override(tmp_path: Path):
    ref_dir = tmp_path / "reference"
    _make_default_reference(ref_dir)
    home = tmp_path / "home"
    (home / "kiln-doc").mkdir(parents=True)
    (home / "kiln-doc" / "Memory.md").write_text(
        "Agent-specific memory: stored at {home_dir}/memory/.\n"
    )

    out = render_kiln_reference(home, ref_dir, {"home_dir": "/my/home"})
    assert "Agent-specific memory: stored at /my/home/memory/." in out
    assert "Default memory (should be rare)." not in out


def test_render_kiln_reference_agent_placeholder_wins_on_conflict(tmp_path: Path):
    ref_dir = tmp_path / "reference"
    _make_default_reference(ref_dir)
    home = tmp_path / "home"
    (home / "kiln-doc").mkdir(parents=True)
    (home / "kiln-doc" / "placeholders.yml").write_text("home_dir: /agent/override\n")

    out = render_kiln_reference(home, ref_dir, {"home_dir": "/kiln/auto"})
    assert "Home is /agent/override." in out


def test_render_kiln_reference_skeleton_heading_without_content_is_skipped(
    tmp_path: Path, caplog
):
    ref_dir = tmp_path / "reference"
    _make_default_reference(ref_dir)
    home = tmp_path / "home"
    (home / "kiln-doc").mkdir(parents=True)
    (home / "kiln-doc" / "skeleton.md").write_text("## Principles\n## Nonexistent\n")

    with caplog.at_level(logging.WARNING, logger="kiln.prompt"):
        out = render_kiln_reference(home, ref_dir, {"home_dir": "/x"})
    assert "## Principles" in out
    assert "Nonexistent" not in out
    assert any("Nonexistent" in r.message for r in caplog.records)


def test_render_kiln_reference_missing_default_returns_empty(tmp_path: Path, caplog):
    ref_dir = tmp_path / "reference"  # not created
    home = tmp_path / "home"
    home.mkdir()

    with caplog.at_level(logging.WARNING, logger="kiln.prompt"):
        out = render_kiln_reference(home, ref_dir, {})
    assert out == ""
    assert any("not found" in r.message for r in caplog.records)


def test_render_kiln_reference_round_trip_no_overrides(tmp_path: Path):
    """With no overrides and trivial placeholders, the rendered output
    should contain every default section body verbatim."""
    ref_dir = tmp_path / "reference"
    _make_default_reference(ref_dir)
    home = tmp_path / "home"
    home.mkdir()

    out = render_kiln_reference(home, ref_dir, {"home_dir": "/x"})
    assert "Default principles." in out
    assert "Default memory (should be rare)." in out
    assert "## Principles\n\nDefault principles." in out


# ---------------------------------------------------------------------------
# PromptBuilder — orchestrator
# ---------------------------------------------------------------------------


def _make_agent_home(tmp_path: Path, *, identity: str = "", memory: dict[str, str] | None = None) -> AgentConfig:
    home = tmp_path / "agent_home"
    home.mkdir()
    (home / "identity.md").write_text(identity)
    (home / "tools").mkdir()
    (home / "skills").mkdir()

    context_injection: list = []
    for label, body in (memory or {}).items():
        rel = f"memory/{label.lower().replace(' ', '-')}.md"
        path = home / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
        context_injection.append({"path": rel, "label": label})

    return AgentConfig(
        name="test",
        home=home,
        identity_doc="identity.md",
        model="claude-opus-4-6",
        context_injection=context_injection,
        tools=["Kiln::Bash", "Kiln::Plan"],
    )


def test_prompt_builder_assembles_four_chunks(tmp_path: Path):
    ref_dir = tmp_path / "reference"
    _make_default_reference(ref_dir)
    config = _make_agent_home(
        tmp_path,
        identity="# Test Agent\n\nI am a test.",
        memory={"Core Memory": "Durable facts.", "Volatile": "Working state."},
    )

    builder = PromptBuilder(
        config, "test-agent-001", kiln_reference_dir=ref_dir
    )
    out = builder.build()

    # Identity first
    assert out.startswith("# Test Agent\n\nI am a test.")
    # Separator appears between chunks
    assert "\n\n---\n\n" in out
    # Kiln reference appears
    assert "# Kiln Reference" in out
    # Session context
    assert "## Session Context" in out
    assert "Agent ID: test-agent-001" in out
    assert "Model: claude-opus-4-6" in out
    # Memory labels
    assert "## Core Memory" in out
    assert "Durable facts." in out
    assert "## Volatile" in out
    assert "Working state." in out


def test_prompt_builder_session_context_drops_tool_listings(tmp_path: Path):
    """Tool/skill listings move to {tool_index}/{skill_index} — they should
    NOT appear in the session-context chunk."""
    ref_dir = tmp_path / "reference"
    _make_default_reference(ref_dir)
    config = _make_agent_home(tmp_path)

    builder = PromptBuilder(config, "aid", kiln_reference_dir=ref_dir)
    sc = builder._session_context()
    assert "Custom tools (invoke via Bash)" not in sc
    assert "Available skills:" not in sc


def test_prompt_builder_automatic_placeholders_wired(tmp_path: Path):
    ref_dir = tmp_path / "reference"
    ref_dir.mkdir()
    (ref_dir / "kiln.md").write_text(
        "# Kiln Reference\n\nIntro.\n\n"
        "## Built-In Tools\n\n{builtins}\n\n"
        "## Session Info\n\n"
        "Agent: {agent_id}. Home: {home_dir}. Today: {today}. Docs: {kiln_path}/docs/.\n"
    )
    config = _make_agent_home(tmp_path)

    builder = PromptBuilder(config, "aid-42", kiln_reference_dir=ref_dir)
    out = builder.build()

    assert "- **Bash**" in out  # {builtins} expanded
    assert "Agent: aid-42." in out
    assert f"Home: {config.home}." in out
    # {kiln_path} must resolve to the reference dir itself so that
    # {kiln_path}/docs/tools.md (as used in the real kiln.md) resolves
    # to an actual file path. .parent.parent would point at src/kiln
    # and break all doc links.
    assert f"Docs: {ref_dir}/docs/." in out


def test_prompt_builder_extra_lines_appended_to_session_context(tmp_path: Path):
    ref_dir = tmp_path / "reference"
    _make_default_reference(ref_dir)
    config = _make_agent_home(tmp_path)

    builder = PromptBuilder(
        config,
        "aid",
        extra_lines=["Inbox: /tmp/inbox"],
        kiln_reference_dir=ref_dir,
    )
    sc = builder._session_context()
    assert "Inbox: /tmp/inbox" in sc


def test_prompt_builder_identity_and_memory_are_not_substituted(tmp_path: Path):
    """Substitution scope is the Kiln reference chunk only. A {placeholder}
    written in identity or memory text should pass through verbatim."""
    ref_dir = tmp_path / "reference"
    _make_default_reference(ref_dir)
    config = _make_agent_home(
        tmp_path,
        identity="Identity says {home_dir}.",
        memory={"Volatile": "Volatile says {agent_id}."},
    )

    builder = PromptBuilder(config, "aid", kiln_reference_dir=ref_dir)
    out = builder.build()
    assert "Identity says {home_dir}." in out
    assert "Volatile says {agent_id}." in out
