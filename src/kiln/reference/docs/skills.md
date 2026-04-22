# Skills

How Kiln discovers, lists, and loads skills — packaged domain knowledge that an agent opts into for a session.

## Overview

A skill is a folder containing a `SKILL.md` file (plus optional supporting files). Its YAML frontmatter declares `name` and `description`; the body is prose instructions injected into the model's context when the skill is activated.

Skills sit between always-present prompt content (your identity doc, memory, tool listings) and on-demand knowledge (files you `Read` when you need them). The listing is always in context — a one-line summary per skill — but the instructions themselves only load when the agent calls `ActivateSkill`. That way a long domain guide can exist without burning tokens every session.

The format is [Anthropic's open Skill spec](https://www.anthropic.com/news/skills) — the same shape works in Claude Code and other Skill-aware runtimes. Kiln's contribution is discovery (scan the agent's `skills/` dir), context-listing (render into the system prompt), and activation (the `ActivateSkill` tool + its PostToolUse hook that injects the body).

## Architecture

```
<home>/skills/
  core/                           # tier 1 — always-listed in context
    programming/
      SKILL.md
      references/                 # (optional) deeper docs
      scripts/                    # (optional) supporting code
  library/                        # tier 2 — one-liner listing
    registry.yml                  # (optional) name → brief map
    <skill-name>/
      SKILL.md
```

Flat layout (no `core/` or `library/`) is also supported — all skills render as full listings. Tiered layout is the recommended shape once a home accumulates more than a handful of skills.

### Discovery

`prompt.py:discover_skill_layout()` scans `skills/`, reads each `SKILL.md`'s YAML frontmatter, and returns a list or tiered dict. Directories named `__pycache__` and `archived` are skipped. Duplicate skill names are deduped (first occurrence wins).

Library skill briefs come from `skills/library/registry.yml`. If missing, the library fallback truncates each skill's own frontmatter `description` to 80 characters.

### Rendering

The session context contains:

```
Available skills:
- **<name>** (<path>): <description>

Skill library:
- **<name>** — <one-liner>

Use `activate_skill` to load a skill before using it.
```

The description is passed through as-is (stripped of surrounding whitespace), so authors control their own line breaks.

### Activation

When the agent calls `ActivateSkill(name="programming")`:

1. The MCP tool returns a short confirmation string.
2. A PostToolUse hook (`create_skill_context_hook`) fires, reads `skills/<name>/SKILL.md` (searching the top level first, then immediate subdirectories so tiered layouts work), strips the YAML frontmatter, and injects the body as `additionalContext`.
3. The injection appears as a system-level `[Skill: <name>]` block — not a tool result — so it persists as context for the remainder of the session.

Once activated, the skill cannot be "deactivated" mid-session. Calling `ActivateSkill` a second time for the same skill re-injects the content.

## Reference

### SKILL.md frontmatter

```yaml
---
name: programming
description: >
  Core programming skill for software engineering tasks. Activate when
  working on code — writing, modifying, debugging, refactoring, reviewing.
---

# Programming

<body — instructions, patterns, examples>
```

| Field         | Required | Notes                                                                 |
|---------------|----------|-----------------------------------------------------------------------|
| `name`        | yes      | Invocation name — case-sensitive. Missing `name` → skill is skipped.  |
| `description` | no*      | Shown in the context listing. Missing → empty description.            |

\* Technically optional, but skills without a description provide no discovery signal to the agent. Always include one.

The frontmatter must open on the first line (`---`) and close with a second `---` line. Anything before the first `---` or malformed YAML causes the skill to be skipped.

### Library registry

`skills/library/registry.yml`:

```yaml
# name: one-liner description
autonomy:         Self-continuation and independent operation
conclave:         Multi-agent swarm coordination
```

Keys are skill names, values are the rendered one-liner. Keys must match the skill folder name.

### Supporting files

Everything under a skill's folder is conventionally addressable by the skill body — reference files with absolute paths or paths relative to the skill's directory. Kiln doesn't auto-load them; the skill's own instructions tell the agent what to read.

Common conventions (not enforced):

- `references/` — additional markdown docs the agent should `Read` on demand
- `scripts/` — helper scripts or templates
- `assets/` — static files the skill refers to

## Examples

A minimal skill:

```
skills/core/docs-style/
  SKILL.md
```

```markdown
---
name: docs-style
description: House style for reference documentation — tone, structure, conventions.
---

# Docs Style

## Tone
- Mechanism-first, not opinionated.
- Assume the reader is competent.
...
```

Activating it:

```python
# Via the MCP tool
ActivateSkill(name="docs-style")
# -> "Skill 'docs-style' activated."
# Full body now in context as [Skill: docs-style] block
```

A skill with deeper references:

```
skills/library/mysterium/
  SKILL.md
  references/
    propositions.md
    reconstruction.md
  scripts/
    build_case.py
```

```markdown
---
name: mysterium
description: Investigation-graph engine — propositions, NPC dialogue, reconstruction scoring.
---

# Mysterium

## Quick start
See `references/propositions.md` for the proposition model and
`references/reconstruction.md` for scoring. Code in `scripts/`.

...
```

Discovering skills programmatically:

```python
from kiln.prompt import discover_skill_layout

skills = discover_skill_layout(Path("~/.<agent>/skills").expanduser())
# -> {"core": [{name, description, path}, ...],
#     "library": {name: one_liner, ...}}
```

## Conventions

- **One SKILL.md per skill directory.** Don't nest skills inside other skills — discovery only scans top-level + one-deep subdirectories, and duplicate names get deduped.
- **Description is for discoverability.** Write it so the agent can decide whether to activate without reading the body. "When to use this" framing beats "what this is" framing.
- **Body starts with how to use the skill, not what it is.** The agent activated it on purpose — skip the preamble.
- **Promote to `core/` only for skills that are always-relevant** for this agent. Everything else belongs in `library/` with a registry entry.
- **Don't inline reference material the agent doesn't always need.** Put deep detail in `references/` and have the SKILL.md body point at specific files.

## Gotchas

- **Malformed or missing frontmatter = silent skip.** A skill with a missing opening `---`, a missing close, or a YAML parse error is dropped from discovery with no error. If a skill doesn't appear in the listing, check its frontmatter first.
- **Name must match folder name for library registry lookups.** Rendering uses the registry key as the display name; activation resolves via folder. A mismatch breaks activation.
- **Activation re-injects every time.** Calling `ActivateSkill` repeatedly for the same skill adds the body to context each call. No caching, no dedup — usually fine, just don't loop on it.
- **Skill frontmatter is stripped at injection time.** Don't rely on the frontmatter being visible to the model; only the body below the closing `---` lands in context.
- **Discovery is one-deep.** Skills nested inside `skills/foo/bar/baz/SKILL.md` aren't found. Use `core/<name>/` or `library/<name>/` — not deeper.
