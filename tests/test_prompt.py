from pathlib import Path

from kiln.prompt import load_tool_docs


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
