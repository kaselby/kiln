"""Prompt assembly utilities — tool/skill discovery, session context building, model resolution."""

import os
import platform
from datetime import date
from pathlib import Path

import yaml


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


def _tool_doc_key(tool_name: str) -> str:
    """Extract the base tool name from a possibly namespaced name.

    'Kiln::Edit' → 'edit', 'Base::WebSearch' → 'websearch', 'Read' → 'read'
    """
    # Strip namespace prefix
    if "::" in tool_name:
        tool_name = tool_name.split("::", 1)[1]
    return tool_name.lower()


def load_tool_docs(
    tool_names: list[str],
    *,
    extra_dirs: list[Path] | None = None,
) -> str:
    """Load tool documentation for the given tool names.

    Searches kiln's built-in tool_docs/ first, then any extra directories
    (e.g. an agent's own tool_docs/ for agent-namespaced tools).

    Args:
        tool_names: List of tool names, possibly namespaced
            (e.g. ["Kiln::Edit", "MyAgent::Bash", "Base::Read"]).
        extra_dirs: Additional directories to search for tool doc files.
            Searched after kiln's built-in docs, so agent docs can override.

    Returns:
        Concatenated tool documentation as a string, suitable for injection
        into the system prompt. Empty string if no docs found.
    """
    search_dirs = [_KILN_TOOL_DOCS_DIR]
    if extra_dirs:
        search_dirs.extend(extra_dirs)

    seen = set()
    docs = []

    for name in tool_names:
        key = _tool_doc_key(name)
        if key in seen:
            continue
        seen.add(key)

        # Search directories in order — last match wins (agent overrides kiln)
        doc_content = None
        for d in search_dirs:
            doc_file = d / f"{key}.md"
            if doc_file.exists():
                doc_content = doc_file.read_text().strip()

        if doc_content:
            docs.append(doc_content)

    if not docs:
        return ""
    return "## Tools\n\n" + "\n\n".join(docs) + "\n"


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
    scan_dirs = [skills_path]
    for child in sorted(skills_path.iterdir()):
        if child.is_dir() and not child.name.startswith(".") and child.name != "__pycache__":
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
        ctx += "\nUse `activate_skill` to load a skill before using it.\n"

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
