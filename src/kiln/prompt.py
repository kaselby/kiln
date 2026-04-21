"""Prompt assembly utilities — tool/skill discovery, session context building, model resolution."""

from __future__ import annotations

import logging
import os
import platform
import re
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

import yaml

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Model resolution
# ---------------------------------------------------------------------------

# Alias → full model ID. Used to resolve shorthand names (including "default")
# to the actual model string before building the system prompt.
# Update when Claude Code changes its default or new model families are released.
MODEL_ALIASES = {
    "default": "claude-opus-4-6",
    "opus": "claude-opus-4-6",
    "sonnet": "claude-sonnet-4-6",
    "haiku": "claude-haiku-4-5-20251001",
}

# Model ID prefix → knowledge cutoff date. Prefixes are matched in order,
# so more specific prefixes should come first.
KNOWLEDGE_CUTOFFS = {
    "claude-opus-4-7": "Jan 2026",
    "claude-opus-4-6": "May 2025",
    "claude-opus-4-5": "May 2025",
    "claude-opus-4": "May 2025",
    "claude-sonnet-4-6": "May 2025",
    "claude-sonnet-4-5": "May 2025",
    "claude-sonnet-4": "May 2025",
    "claude-haiku-4-5": "May 2025",
    "claude-haiku-4": "May 2025",
    "claude-3-5": "Early 2024",
    "claude-3": "Early 2024",
    "gpt-5.4": "August 31, 2025",
    "gpt-5": "unknown",
    "gpt-4.1": "unknown",
    "gpt-4o": "unknown",
    "o4-mini": "unknown",
    "o3": "unknown",
    "o1": "unknown",
}


def resolve_model(model: str | None) -> str:
    """Resolve a model name through aliases, falling back to the default alias."""
    if model is None:
        model = "default"
    return MODEL_ALIASES.get(model, model)


def get_knowledge_cutoff(model: str) -> str:
    """Look up the knowledge cutoff for a model string by prefix match."""
    for prefix, cutoff in KNOWLEDGE_CUTOFFS.items():
        if model.startswith(prefix):
            return cutoff
    return "unknown"


# ---------------------------------------------------------------------------
# Tool documentation
# ---------------------------------------------------------------------------

# Built-in tool docs shipped with kiln (for Kiln:: and Base:: tools).
_KILN_TOOL_DOCS_DIR = Path(__file__).parent / "tool_docs"

# Map tool names to doc filenames. Handles both namespaced ("Kiln::Edit")
# and bare ("Edit") names. Case-insensitive lookup.
_TOOL_DOC_NAMES = {
    "bash": "bash",
    "read": "read",
    "write": "write",
    "edit": "edit",
    "plan": "plan",
    "websearch": "websearch",
    "message": "message",
    "exit_session": "exit_session",
    "activate_skill": "activate_skill",
}

_TOOL_GUIDE_SECTIONS = [
    ("Shell and Execution", {"bash"}),
    ("File Tools", {"read", "write", "edit"}),
    ("Workflow Tools", {"plan", "activate_skill"}),
    ("Coordination and Session Tools", {"message", "exit_session"}),
    ("Research Tools", {"websearch"}),
]


_TOOL_GUIDE_SECTION_BY_KEY = {
    key: title
    for title, keys in _TOOL_GUIDE_SECTIONS
    for key in keys
}


def _tool_doc_key(tool_name: str) -> str:
    """Extract the base tool name from a possibly namespaced name.

    'Kiln::Edit' → 'edit', 'Base::WebSearch' → 'websearch', 'Read' → 'read'
    """
    # Strip namespace prefix
    if "::" in tool_name:
        tool_name = tool_name.split("::", 1)[1]
    return tool_name.lower()


def _bump_markdown_headings(text: str, levels: int = 1) -> str:
    """Increase markdown heading levels so sections can be nested cleanly."""
    bumped_lines = []
    for line in text.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("#"):
            prefix_len = len(line) - len(stripped)
            original_hashes = len(stripped) - len(stripped.lstrip("#"))
            if original_hashes > 0 and len(stripped) > original_hashes and stripped[original_hashes] == " ":
                new_hashes = min(6, original_hashes + levels)
                line = (" " * prefix_len) + ("#" * new_hashes) + stripped[original_hashes:]
        bumped_lines.append(line)
    return "\n".join(bumped_lines)



def load_tool_docs(
    tool_names: list[str],
    *,
    extra_dirs: list[Path] | None = None,
) -> str:
    """Load built-in tool docs and render them as a coherent guide.

    Searches kiln's built-in tool_docs/ first, then any extra directories
    (e.g. an agent's own tool_docs/ for agent-namespaced tools).

    Args:
        tool_names: List of tool names, possibly namespaced
            (e.g. ["Kiln::Edit", "MyAgent::Bash", "Base::Read"]).
        extra_dirs: Additional directories to search for tool doc files.
            Searched after kiln's built-in docs, so agent docs can override.

    Returns:
        Built-in tool documentation as a string, suitable for injection into
        the system prompt. Empty string if no docs found.
    """
    search_dirs = [_KILN_TOOL_DOCS_DIR]
    if extra_dirs:
        search_dirs.extend(extra_dirs)

    seen = set()
    grouped_docs: dict[str, list[str]] = {title: [] for title, _ in _TOOL_GUIDE_SECTIONS}
    other_docs = []

    for name in tool_names:
        key = _tool_doc_key(name)
        if key in seen:
            continue
        seen.add(key)

        # Search directories in order — last match wins (agent overrides kiln)
        doc_content = None
        doc_name = _TOOL_DOC_NAMES.get(key, key)
        for d in search_dirs:
            doc_file = d / f"{doc_name}.md"
            if doc_file.exists():
                doc_content = doc_file.read_text().strip()

        if not doc_content:
            continue

        section_title = _TOOL_GUIDE_SECTION_BY_KEY.get(key)

        rendered_doc = _bump_markdown_headings(doc_content, levels=1)
        if section_title:
            grouped_docs[section_title].append(rendered_doc)
        else:
            other_docs.append(rendered_doc)

    if not any(grouped_docs.values()) and not other_docs:
        return ""

    parts = [
        "## Built-In Tool Guide",
        "",
        "This guide covers Kiln's built-in API tools. Shell and custom tools are listed separately in the session context.",
    ]

    for section_title, _keys in _TOOL_GUIDE_SECTIONS:
        section_docs = grouped_docs[section_title]
        if not section_docs:
            continue
        parts.extend(["", f"### {section_title}", "", "\n\n".join(section_docs)])

    if other_docs:
        parts.extend(["", "### Other built-in tools", "", "\n\n".join(other_docs)])

    return "\n".join(parts) + "\n"



# ---------------------------------------------------------------------------
# Tool and skill discovery
# ---------------------------------------------------------------------------

def discover_tool_layout(tools_path: Path) -> list[dict] | dict:
    """Discover tools with core/library awareness.

    If tools_path contains core/ or library/ subdirectories, returns a tiered
    dict::

        {"core": [tool_dicts], "library": {"name": "one-liner", ...}}

    Core tools are discovered via headers (full specs). Library tools come from
    library/registry.yml (one-liner descriptions only).

    If no core/ or library/ dirs exist, falls back to flat discovery and returns
    a plain list[dict] (same as discover_tools).
    """
    if not tools_path.exists():
        return []

    core_dir = tools_path / "core"
    lib_dir = tools_path / "library"

    if not core_dir.exists() and not lib_dir.exists():
        return discover_tools(tools_path)

    result: dict = {"core": [], "library": {}}

    if core_dir.exists():
        result["core"] = discover_tools(core_dir)

    if lib_dir.exists():
        registry_path = lib_dir / "registry.yml"
        if registry_path.exists():
            try:
                raw = yaml.safe_load(registry_path.read_text()) or {}
                result["library"] = {k: v for k, v in raw.items() if isinstance(v, str)}
            except Exception:
                pass

    return result


def discover_skill_layout(skills_path: Path) -> list[dict] | dict:
    """Discover skills with core/library awareness.

    If skills_path contains core/ or library/ subdirectories, returns a tiered
    dict::

        {"core": [skill_dicts], "library": {"name": "one-liner", ...}}

    Core skills have full description + path. Library skills come from
    library/registry.yml, falling back to truncated SKILL.md descriptions.

    If no core/ or library/ dirs exist, falls back to flat discovery.
    """
    if not skills_path.exists():
        return []

    core_dir = skills_path / "core"
    lib_dir = skills_path / "library"

    if not core_dir.exists() and not lib_dir.exists():
        return discover_skills(skills_path)

    result: dict = {"core": [], "library": {}}

    if core_dir.exists():
        result["core"] = discover_skills(core_dir)

    if lib_dir.exists():
        registry_path = lib_dir / "registry.yml"
        if registry_path.exists():
            try:
                raw = yaml.safe_load(registry_path.read_text()) or {}
                result["library"] = {k: v for k, v in raw.items() if isinstance(v, str)}
            except Exception:
                pass

        # Fall back to SKILL.md frontmatter if no registry
        if not result["library"]:
            lib_skills = discover_skills(lib_dir)
            result["library"] = {
                s["name"]: s["description"][:80] for s in lib_skills
            }

    return result


def discover_tools(tools_path: Path) -> list[dict]:
    """Scan a tools directory for standalone scripts and managed tool definitions.

    Standalone scripts: executable files with a ``# ---`` YAML comment header
    containing ``name`` and ``description`` fields.  Scans tools_path and its
    immediate subdirectories (e.g. core/, library/).

    Managed tools: Python modules in tools_path/definitions/ with a ``meta`` dict
    containing ``name``, ``description``, and optionally ``cost_per_call``.

    Returns list of dicts with keys: name, description, arguments (optional),
    cost (optional).
    """
    tools = []
    if not tools_path.exists():
        return tools

    # Directories to scan for standalone scripts: top-level + immediate subdirs
    SKIP_DIRS = {"__pycache__", "definitions", "lib"}
    scan_dirs = [tools_path]
    for child in sorted(tools_path.iterdir()):
        if child.is_dir() and not child.name.startswith(".") and child.name not in SKIP_DIRS:
            scan_dirs.append(child)

    # --- Standalone scripts (executable files with comment headers) ---
    for scan_dir in scan_dirs:
        for path in sorted(scan_dir.iterdir()):
            if path.is_dir() or path.name.startswith("."):
                continue
            if not os.access(path, os.X_OK) and path.suffix != ".py":
                continue
            try:
                text = path.read_text()
            except Exception:
                continue
            header = _parse_tool_header(text)
            if header and "name" in header:
                entry = {
                    "name": header["name"],
                    "description": header.get("brief", header.get("description", "")),
                    "arguments": header.get("arguments", ""),
                }
                if header.get("cost"):
                    entry["cost"] = header["cost"]
                tools.append(entry)

    # --- Managed tools (definitions/*.py with meta dict) ---
    defs_dir = tools_path / "definitions"
    if defs_dir.exists():
        for path in sorted(defs_dir.glob("*.py")):
            if path.name.startswith("_"):
                continue
            try:
                meta = _parse_meta_from_source(path)
                if meta and "name" in meta:
                    entry = {
                        "name": meta["name"],
                        "description": meta.get("description", ""),
                    }
                    cost = meta.get("cost_per_call", 0)
                    if cost:
                        entry["cost"] = cost
                    tools.append(entry)
            except Exception:
                continue

    return tools


def _parse_tool_header(text: str) -> dict | None:
    """Extract YAML fields from a ``# ---`` comment header block."""
    lines = text.split("\n")
    in_header = False
    header_lines = []
    for line in lines:
        stripped = line.strip()
        if stripped == "# ---":
            if in_header:
                break  # closing delimiter
            in_header = True
            continue
        if in_header:
            if stripped.startswith("# "):
                header_lines.append(stripped[2:])
            else:
                break  # non-comment line inside header = malformed, stop
    if not header_lines:
        return None
    try:
        return yaml.safe_load("\n".join(header_lines))
    except Exception:
        return None


def _parse_meta_from_source(path: Path) -> dict | None:
    """Extract a ``meta = {...}`` dict from a Python source file without importing it."""
    import ast

    try:
        tree = ast.parse(path.read_text())
    except SyntaxError:
        return None

    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "meta":
                    try:
                        return ast.literal_eval(node.value)
                    except (ValueError, TypeError):
                        return None
    return None


def discover_skills(skills_path: Path) -> list[dict]:
    """Scan a skills directory and extract name + description from SKILL.md frontmatter.

    Scans skills_path and its immediate subdirectories (e.g. core/, library/),
    same pattern as discover_tools.

    Returns list of dicts with keys: name, description, path.
    """
    skills = []
    if not skills_path.exists():
        return skills

    # Directories to scan: top-level + immediate subdirs
    SKIP_DIRS = {"__pycache__", "archived"}
    scan_dirs = [skills_path]
    for child in sorted(skills_path.iterdir()):
        if child.is_dir() and not child.name.startswith(".") and child.name not in SKIP_DIRS:
            scan_dirs.append(child)

    seen_names: set[str] = set()
    for scan_dir in scan_dirs:
        for skill_dir in sorted(scan_dir.iterdir()):
            if not skill_dir.is_dir():
                continue
            skill_md = skill_dir / "SKILL.md"
            if not skill_md.exists():
                continue
            text = skill_md.read_text()
            if not text.startswith("---"):
                continue
            try:
                end = text.index("---", 3)
            except ValueError:
                continue
            frontmatter = yaml.safe_load(text[3:end])
            if frontmatter and "name" in frontmatter:
                name = frontmatter["name"]
                if name in seen_names:
                    continue
                seen_names.add(name)
                skills.append({
                    "name": name,
                    "description": frontmatter.get("description", "").strip(),
                    "path": str(skill_dir),
                })
    return skills


# ---------------------------------------------------------------------------
# Session context building
# ---------------------------------------------------------------------------

def _render_tool_listing(tools: list[dict] | dict | None) -> str:
    """Render tool listing from flat list or tiered dict."""
    if not tools:
        return ""

    ctx = ""
    if isinstance(tools, dict):
        # Tiered: core (full specs) + library (one-liners)
        core = tools.get("core", [])
        library = tools.get("library", {})

        if core:
            ctx += "\nCustom tools (invoke via Bash):\n"
            for t in core:
                cost_tag = f" **[${t['cost']}/call]**" if t.get("cost") else ""
                args = f" `{t['arguments']}`" if t.get("arguments") else ""
                desc = " ".join(t.get("description", "").split())
                ctx += f"- **{t['name']}**{args} — {desc}{cost_tag}\n"

        if library:
            ctx += "\nTool library (use `tool-info <name>` for details):\n"
            for name in sorted(library):
                ctx += f"- **{name}** — {library[name]}\n"
    else:
        # Flat: all tools with full descriptions
        ctx += "\nCustom tools (invoke via Bash):\n"
        for t in tools:
            cost_tag = f" **[${t['cost']}/call]**" if t.get("cost") else ""
            args = f" `{t['arguments']}`" if t.get("arguments") else ""
            ctx += f"- **{t['name']}**{args} — {t['description']}{cost_tag}\n"

    return ctx


def _render_skill_listing(skills: list[dict] | dict | None) -> str:
    """Render skill listing from flat list or tiered dict."""
    if not skills:
        return ""

    ctx = ""
    if isinstance(skills, dict):
        core = skills.get("core", [])
        library = skills.get("library", {})

        if core:
            ctx += "\nAvailable skills:\n"
            for s in core:
                ctx += f"- **{s['name']}** ({s['path']}): {s['description']}\n"

        if library:
            ctx += "\nSkill library:\n"
            for name in sorted(library):
                ctx += f"- **{name}** — {library[name]}\n"
    else:
        ctx += "\nAvailable skills:\n"
        for s in skills:
            ctx += f"- **{s['name']}** ({s['path']}): {s['description']}\n"

    if ctx:
        ctx += "\nUse `ActivateSkill` to load a skill before using it.\n"

    return ctx


def build_session_context(
    agent_id: str,
    model: str | None = None,
    *,
    tools: list[dict] | dict | None = None,
    skills: list[dict] | dict | None = None,
    parent: str | None = None,
    depth: int = 0,
    cwd: str | None = None,
    extra_lines: list[str] | None = None,
) -> str:
    """Build the dynamic session context block for the system prompt.

    This is the runtime-dependent part of the prompt: agent ID, model info,
    platform details, tool/skill listings, date. Identity and memory are
    NOT included — those are agent-owned and composed by the harness.

    Args:
        agent_id: The agent's session ID.
        model: Model name or alias (resolved internally).
        tools: Tool data — flat list[dict] from discover_tools(), or tiered
            dict from discover_tool_layout() with "core" and "library" keys.
        skills: Skill data — flat list[dict] from discover_skills(), or tiered
            dict from discover_skill_layout().
        parent: Parent agent ID if spawned.
        depth: Spawn depth.
        cwd: Working directory.
        extra_lines: Additional lines to append (e.g. role info).

    Returns:
        The session context block as a string, starting with a section header.
    """
    resolved = resolve_model(model)
    cutoff = get_knowledge_cutoff(resolved)

    ctx = "\n\n---\n## Session Context\n\n"
    ctx += f"Agent ID: {agent_id}\n"

    if parent:
        ctx += f"Parent: {parent}\n"
        ctx += f"Depth: {depth}\n"

    ctx += f"\nModel: {resolved}\n"
    if cutoff == "unknown":
        ctx += (
            f"Knowledge cutoff: **UNKNOWN — the model '{resolved}' doesn't match any "
            f"prefix in KNOWLEDGE_CUTOFFS. Update prompt.py if a new model generation "
            f"has been released.**\n"
        )
    else:
        ctx += f"Knowledge cutoff: {cutoff}\n"
    ctx += f"Platform: {platform.system()} {platform.release()}\n"
    ctx += f"Shell: {os.environ.get('SHELL', 'unknown')}\n"
    ctx += f"Working directory: {cwd or os.getcwd()}\n"

    ctx += _render_tool_listing(tools)
    ctx += _render_skill_listing(skills)

    ctx += f"\nToday's date is **{date.today().strftime('%B %d, %Y')}**."

    # Extra lines (role info, inbox path, etc.)
    if extra_lines:
        for line in extra_lines:
            ctx += f"\n{line}"

    return ctx


# ---------------------------------------------------------------------------
# Kiln reference doc — rendering, parsing, override resolution
# ---------------------------------------------------------------------------
#
# The Kiln reference is a shared description of Kiln's core mechanisms,
# rendered into every agent's system prompt. The default ships at
# ``src/kiln/reference/kiln.md`` alongside deep-dive docs at
# ``src/kiln/reference/docs/*.md``. Agents can override the default via
# a ``kiln-doc/`` subdirectory in their home — see spec for the override
# mechanism. See ``scratch/kiln-doc-spec.md`` (in beth's scratch) for the
# full design.


# Short one-liners for each built-in tool, rendered as the `{builtins}`
# placeholder value inside the Kiln reference. Hand-written here (not
# parsed from tool_docs/*.md) so the summary text stays tight and is
# independent of the full-doc shape. Long-form detail lives in
# ``reference/docs/builtins.md`` — agents can read it on demand.
BUILTIN_TOOL_SUMMARIES: dict[str, str] = {
    "Bash": "Executes a command in a persistent shell. File ops, tool invocations, git, and most actions flow through Bash.",
    "Read": "Reads a file from the local filesystem. Handles text, images, Jupyter notebooks, and PDFs.",
    "Write": "Writes a file to the filesystem. Overwrites if it already exists.",
    "Edit": "Performs exact string replacements in a file. Requires a prior Read.",
    "Plan": "Externalizes your working plan — breaks down complex tasks and tracks progress.",
    "Message": "Sends point-to-point messages to agents, broadcasts to channels, and manages subscriptions.",
    "ActivateSkill": "Activates a skill by name, loading its instructions as system-level context for the rest of the session.",
    "ExitSession": "Exits the session cleanly. The harness handles summary and memory commits; supports self-continuation.",
}


# Placeholder syntax used by ``substitute``. Matches ``{name}`` where name
# is an identifier-like token. Deliberately strict so prose braces
# (``{"json": true}``) don't get mangled. Double braces (``{{name}}``)
# are treated as a literal ``{name}`` escape.
_PLACEHOLDER_RE = re.compile(r"(?<!\{)\{([A-Za-z_][A-Za-z0-9_]*)\}(?!\})")


def substitute(text: str, placeholders: dict[str, str]) -> str:
    """Single-pass placeholder substitution.

    Replaces ``{name}`` tokens in ``text`` with the corresponding value
    from ``placeholders``. Unknown placeholders are left literal in the
    output and a warning is logged (once per unknown name). Escaped
    placeholders (``{{name}}``) collapse to ``{name}`` literals.

    Substitution is single-pass: placeholder values that themselves
    contain ``{other}`` tokens are not expanded recursively.

    Args:
        text: Source text containing placeholder tokens.
        placeholders: Mapping from placeholder name to replacement string.

    Returns:
        The substituted text.
    """
    if not text:
        return text

    missing: set[str] = set()

    def _sub(match: re.Match[str]) -> str:
        name = match.group(1)
        if name in placeholders:
            value = placeholders[name]
            return value if isinstance(value, str) else str(value)
        missing.add(name)
        return match.group(0)  # literal passthrough

    result = _PLACEHOLDER_RE.sub(_sub, text)
    # Collapse {{name}} escapes → {name}
    result = result.replace("{{", "{").replace("}}", "}")

    if missing:
        log.warning(
            "Unknown placeholder(s) in Kiln reference text: %s — rendered literally.",
            ", ".join(sorted(missing)),
        )

    return result


@dataclass
class KilnSections:
    """Parsed Kiln reference doc — preamble plus ordered H2 sections.

    ``preamble`` holds everything above the first ``##`` heading (the
    ``# Kiln Reference`` H1 + intro paragraph). It is always rendered and
    is not overridable in v1.

    ``sections`` is an ordered mapping from H2 heading text (without the
    ``##`` prefix) to the body that follows it, up to the next ``##``
    heading or end of file. Both preamble and section bodies are stored
    verbatim — no placeholder substitution yet, no trailing normalization.
    """

    preamble: str = ""
    sections: "OrderedDict[str, str]" = field(default_factory=OrderedDict)


_H2_HEADING_RE = re.compile(r"^##\s+(.+?)\s*$")


def parse_kiln_sections(path: Path) -> KilnSections:
    """Parse a Kiln reference markdown file into preamble + H2 sections.

    Recognizes ``##``-level headings as section boundaries. Higher-level
    (``#``) and lower-level (``###`` and beyond) headings are treated as
    body content.

    Args:
        path: Path to the markdown file. Missing file raises FileNotFoundError.

    Returns:
        KilnSections with ``preamble`` and ordered ``sections``.
    """
    text = path.read_text()
    lines = text.splitlines(keepends=True)

    preamble_lines: list[str] = []
    sections: OrderedDict[str, str] = OrderedDict()

    current_heading: str | None = None
    current_body: list[str] = []

    for line in lines:
        match = _H2_HEADING_RE.match(line.rstrip("\r\n"))
        if match:
            # Close previous section or preamble
            if current_heading is None:
                # Finished preamble
                pass
            else:
                sections[current_heading] = "".join(current_body).strip("\n")
            current_heading = match.group(1).strip()
            current_body = []
            if current_heading in sections:
                log.warning(
                    "Duplicate section heading %r in %s — first occurrence wins.",
                    current_heading,
                    path,
                )
                # Discard this duplicate; re-target to a sink so we don't
                # overwrite the first occurrence when we close it.
                current_heading = "__duplicate__"
            continue

        if current_heading is None:
            preamble_lines.append(line)
        else:
            current_body.append(line)

    # Close final open section
    if current_heading is not None and current_heading != "__duplicate__":
        sections[current_heading] = "".join(current_body).strip("\n")

    preamble = "".join(preamble_lines).strip("\n")
    return KilnSections(preamble=preamble, sections=sections)


@dataclass
class KilnOverrides:
    """Loaded agent overrides for the Kiln reference.

    Each field may be empty/None when the corresponding file is absent.
    ``skeleton`` is an ordered list of heading names (no ``##`` prefix) or
    ``None`` if the agent didn't ship a skeleton override. ``content`` maps
    heading → body (override body only, no ``## Heading`` line).
    ``placeholders`` is the merged-in agent-specific placeholder map.
    """

    skeleton: list[str] | None = None
    content: dict[str, str] = field(default_factory=dict)
    placeholders: dict[str, str] = field(default_factory=dict)


def load_kiln_overrides(home: Path) -> KilnOverrides:
    """Load per-agent Kiln reference overrides from ``{home}/kiln-doc/``.

    All three files are optional. If ``kiln-doc/`` doesn't exist, returns
    an empty KilnOverrides (Kiln defaults will be used unchanged).

    Files:
        skeleton.md      — ``##`` headings (one per line) specifying the
                           agent's section order.
        <Heading>.md     — content override for the named section. The
                           filename (minus ``.md``) matches the heading
                           text verbatim. Case-sensitive.
        placeholders.yml — YAML mapping of placeholder names to values.
                           Malformed YAML raises a clear error.
    """
    overrides = KilnOverrides()
    override_dir = home / "kiln-doc"
    if not override_dir.is_dir():
        return overrides

    # Skeleton
    skeleton_path = override_dir / "skeleton.md"
    if skeleton_path.exists():
        headings: list[str] = []
        seen: set[str] = set()
        for raw_line in skeleton_path.read_text().splitlines():
            match = _H2_HEADING_RE.match(raw_line.strip())
            if not match:
                continue
            heading = match.group(1).strip()
            if heading in seen:
                log.warning(
                    "Duplicate heading %r in %s — first occurrence wins.",
                    heading,
                    skeleton_path,
                )
                continue
            seen.add(heading)
            headings.append(heading)
        overrides.skeleton = headings

    # Content overrides — any *.md file other than skeleton.md is a
    # content override keyed by its stem.
    for md_path in sorted(override_dir.glob("*.md")):
        if md_path.name == "skeleton.md":
            continue
        heading = md_path.stem
        overrides.content[heading] = md_path.read_text().strip("\n")

    # Placeholders
    placeholders_path = override_dir / "placeholders.yml"
    if placeholders_path.exists():
        try:
            raw = yaml.safe_load(placeholders_path.read_text())
        except yaml.YAMLError as e:
            raise ValueError(
                f"Malformed YAML in {placeholders_path}: {e}"
            ) from e
        if raw is None:
            raw = {}
        if not isinstance(raw, dict):
            raise ValueError(
                f"{placeholders_path} must contain a YAML mapping at the top level; "
                f"got {type(raw).__name__}."
            )
        overrides.placeholders = {str(k): str(v) for k, v in raw.items()}

    return overrides


def default_kiln_reference_dir() -> Path:
    """Path to Kiln's shipped reference material (``src/kiln/reference``)."""
    return Path(__file__).parent / "reference"


def render_builtins_summary(tool_names: list[str] | None = None) -> str:
    """Render the ``{builtins}`` placeholder value.

    Produces a bullet list with a one-liner per enabled built-in tool,
    drawn from ``BUILTIN_TOOL_SUMMARIES``. ``tool_names`` may contain
    namespaced names (e.g. ``Kiln::Plan``) — the namespace is stripped
    before lookup. Tools without a summary are silently skipped (keeps
    the output readable when an agent ships a custom built-in).

    If ``tool_names`` is None, every summary is rendered (stable order).
    """
    if tool_names is None:
        names = list(BUILTIN_TOOL_SUMMARIES.keys())
    else:
        names = []
        seen: set[str] = set()
        for raw in tool_names:
            bare = raw.split("::", 1)[1] if "::" in raw else raw
            if bare in seen:
                continue
            if bare not in BUILTIN_TOOL_SUMMARIES:
                continue
            seen.add(bare)
            names.append(bare)

    lines = [f"- **{name}** — {BUILTIN_TOOL_SUMMARIES[name]}" for name in names]
    return "\n".join(lines)


def render_kiln_reference(
    home: Path,
    kiln_reference_dir: Path | None = None,
    placeholders: dict[str, str] | None = None,
) -> str:
    """Render the Kiln reference chunk for an agent's system prompt.

    Combines the default ``kiln.md`` with any overrides in
    ``{home}/kiln-doc/`` and substitutes placeholders. The output starts
    with the ``# Kiln Reference`` H1 and intro (preamble — non-overridable
    in v1) followed by the agent's resolved ``##`` sections.

    Args:
        home: Agent home directory. ``{home}/kiln-doc/`` is consulted
            for overrides (all optional).
        kiln_reference_dir: Directory containing the default ``kiln.md``.
            Defaults to the packaged ``src/kiln/reference/``.
        placeholders: Map of placeholder name → value. Automatic
            placeholders are expected to be pre-merged by the caller
            (typically ``PromptBuilder``). Agent values in
            ``placeholders.yml`` are layered on top inside this function.

    Returns:
        The rendered Kiln reference as a single string. Trailing
        whitespace is stripped.
    """
    if kiln_reference_dir is None:
        kiln_reference_dir = default_kiln_reference_dir()
    default_path = kiln_reference_dir / "kiln.md"
    if not default_path.exists():
        log.warning(
            "Kiln reference default not found at %s — rendering empty reference.",
            default_path,
        )
        return ""

    default = parse_kiln_sections(default_path)
    overrides = load_kiln_overrides(home)

    # Skeleton: agent override or fall back to default section order
    skeleton = overrides.skeleton if overrides.skeleton is not None else list(default.sections.keys())

    # Merge placeholder sources. Automatic (caller-supplied) first,
    # agent overrides win on conflict.
    merged_placeholders = dict(placeholders or {})
    merged_placeholders.update(overrides.placeholders)

    parts: list[str] = []
    if default.preamble:
        parts.append(default.preamble)

    for heading in skeleton:
        if heading in overrides.content:
            body = overrides.content[heading]
        elif heading in default.sections:
            body = default.sections[heading]
        else:
            log.warning(
                "Skeleton heading %r has no content (no override and no Kiln default) — skipping.",
                heading,
            )
            continue
        parts.append(f"## {heading}\n\n{body}".rstrip())

    rendered = "\n\n".join(parts)
    return substitute(rendered, merged_placeholders).rstrip()


# ---------------------------------------------------------------------------
# PromptBuilder — orchestrator for the full system prompt
# ---------------------------------------------------------------------------


class PromptBuilder:
    """Assembles an agent's system prompt from four chunks.

    Final shape::

        <identity>

        ---

        # Kiln Reference
        ...rendered kiln.md...

        ---

        ## Session Context
        ...runtime-dynamic fields...

        ---

        ## <Memory Label 1>
        ...

        ---

        ## <Memory Label 2>
        ...

    Two H1s total (identity's own H1 + ``# Kiln Reference``); everything
    else stays at H2. Only the Kiln reference chunk is subject to
    placeholder substitution — identity and memory chunks are rendered
    verbatim (agents hand-write those and can't assume Kiln's template
    language applies).

    Parameters not covered by ``config``:
        agent_id: The session's agent ID (runtime-assigned).
        extra_lines: Session-context trailers injected by the harness
            (e.g. ``["Inbox: /path/to/inbox"]``). Kept open-ended so the
            harness can extend without touching PromptBuilder.
        kiln_reference_dir: Override the packaged reference location
            (tests pass a tmp dir).
    """

    def __init__(
        self,
        config,
        agent_id: str,
        *,
        extra_lines: list[str] | None = None,
        kiln_reference_dir: Path | None = None,
    ) -> None:
        self.config = config
        self.agent_id = agent_id
        self.extra_lines = list(extra_lines) if extra_lines else []
        self.kiln_reference_dir = kiln_reference_dir or default_kiln_reference_dir()

    # --- Placeholder sources -------------------------------------------------

    def _automatic_placeholders(self) -> dict[str, str]:
        """Kiln-computed placeholders, available everywhere in the reference."""
        tools = discover_tool_layout(self.config.tools_path)
        skills = discover_skill_layout(self.config.skills_path)
        now = datetime.now()
        return {
            "home_dir": str(self.config.home),
            "kiln_path": str(self.kiln_reference_dir.parent.parent),
            "builtins": render_builtins_summary(list(self.config.tools)),
            "tool_index": _render_tool_listing(tools).strip("\n"),
            "skill_index": _render_skill_listing(skills).strip("\n"),
            "agent_id": self.agent_id,
            "cwd": str(self.config.home),
            "platform": f"{platform.system()} {platform.release()}",
            "today": now.strftime("%B %d, %Y"),
            "now": now.isoformat(timespec="seconds"),
        }

    # --- Chunks --------------------------------------------------------------

    def _identity(self) -> str:
        return self.config.load_identity().strip("\n")

    def _kiln_reference(self) -> str:
        return render_kiln_reference(
            self.config.home,
            self.kiln_reference_dir,
            self._automatic_placeholders(),
        )

    def _session_context(self) -> str:
        """Runtime-dynamic fields only. No tool/skill listings — those live
        in the Kiln reference via {tool_index} / {skill_index}."""
        resolved = resolve_model(self.config.model)
        cutoff = get_knowledge_cutoff(resolved)

        lines: list[str] = ["## Session Context", ""]
        lines.append(f"Agent ID: {self.agent_id}")
        if self.config.parent:
            lines.append(f"Parent: {self.config.parent}")
            lines.append(f"Depth: {self.config.depth}")
        lines.append("")
        lines.append(f"Model: {resolved}")
        if cutoff == "unknown":
            lines.append(
                f"Knowledge cutoff: **UNKNOWN — the model '{resolved}' doesn't match any "
                f"prefix in KNOWLEDGE_CUTOFFS. Update prompt.py if a new model generation "
                f"has been released.**"
            )
        else:
            lines.append(f"Knowledge cutoff: {cutoff}")
        lines.append(f"Platform: {platform.system()} {platform.release()}")
        lines.append(f"Shell: {os.environ.get('SHELL', 'unknown')}")
        lines.append(f"Working directory: {self.config.home}")
        lines.append("")
        lines.append(f"Today's date is **{date.today().strftime('%B %d, %Y')}**.")
        for extra in self.extra_lines:
            lines.append(extra)
        return "\n".join(lines)

    def _memory_files(self) -> list[str]:
        """One chunk per context-injection file, each with its own ## heading."""
        chunks: list[str] = []
        for label, content in self.config.load_context_files():
            chunks.append(f"## {label}\n\n{content.strip()}")
        return chunks

    # --- Orchestrator --------------------------------------------------------

    def build(self) -> str:
        """Compose the full system prompt."""
        chunks: list[str] = []
        identity = self._identity()
        if identity:
            chunks.append(identity)
        reference = self._kiln_reference()
        if reference:
            chunks.append(reference)
        chunks.append(self._session_context())
        chunks.extend(self._memory_files())
        return "\n\n---\n\n".join(chunks)
