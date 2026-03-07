---
name: skill-authoring
description: >
  Creates, edits, and audits Agent Skills following the official agentskills.io
  specification. Activate when writing a new skill, modifying an existing skill's
  SKILL.md or structure, or reviewing skills for spec compliance. Covers frontmatter
  requirements, naming conventions, directory layout, progressive disclosure, and
  content best practices.
---

# Skill Authoring

Agent Skills follow an open standard defined at [agentskills.io](https://agentskills.io).
This skill contains the spec and best practices for authoring them.

## Skill Structure

A skill is a directory containing at minimum a `SKILL.md` file:

```
skill-name/
├── SKILL.md          # Required — metadata + instructions
├── references/       # Optional — docs loaded into context on demand
├── scripts/          # Optional — executable code, run without loading into context
└── assets/           # Optional — files used in output (templates, images, fonts), never loaded into context
```

The key distinction between the optional directories: **references** are read into context to inform the agent's thinking; **scripts** are executed (output enters context, not the code itself); **assets** are used in the agent's output without ever entering context (e.g. a PowerPoint template that gets copied and modified).

Skills live at `<agent_home>/skills/<name>/`.

## SKILL.md Format

YAML frontmatter followed by Markdown body.

### Required Frontmatter

```yaml
---
name: skill-name
description: >
  What the skill does and when to use it. Written in third person.
  Include specific keywords that help agents identify relevant tasks.
---
```

### Frontmatter Fields

| Field           | Required | Constraints |
|-----------------|----------|-------------|
| `name`          | Yes | Max 64 chars. Lowercase alphanumeric + hyphens. No consecutive hyphens. Can't start/end with hyphen. Must match parent directory name. No reserved words ("anthropic", "claude"). |
| `description`   | Yes | Max 1024 chars. Non-empty. Third person. Describe what it does AND when to use it. |
| `license`       | No  | License name or reference to bundled license file. |
| `compatibility` | No  | Max 500 chars. Environment requirements (intended product, packages, network). |
| `metadata`      | No  | Arbitrary string key-value map for additional properties. |
| `allowed-tools` | No  | Space-delimited list of pre-approved tools. Experimental. |

### Name Rules

Valid: `pdf-processing`, `data-analysis`, `code-review`
Invalid: `PDF-Processing` (uppercase), `-pdf` (leading hyphen), `pdf--processing` (consecutive hyphens)

Prefer gerund form for clarity: `processing-pdfs`, `analyzing-data`, `writing-documentation`.

### Description Rules

- Write in **third person** ("Processes files..." not "I can help you..." or "You can use this to...")
- Include both what it does and when/why to activate it
- Include specific keywords that help with task matching
- Be specific, not vague — "Helps with PDFs" is bad; "Extracts text and tables from PDF files, fills forms, merges documents" is good

### Body Content

The Markdown body after frontmatter contains the skill instructions. No format restrictions — write whatever helps agents perform the task.

**Key constraint: keep SKILL.md body under 500 lines.** Move detailed reference material to separate files.

**Writing style:** Use imperative/infinitive form ("To extract text, run..." not "You should run..." or "I would run..."). Skills are written for *another agent instance* to consume — objective, instructional language works best.

**Key mindset:** Only include information the consuming agent doesn't already have. Challenge each paragraph: does this justify its token cost? Claude already knows what PDFs are and how pip works.

For large reference files (>10k words), include grep search patterns in SKILL.md so the agent can search rather than loading the entire file into context.

## Progressive Disclosure

Skills are designed for efficient context use across three tiers:

1. **Metadata** (~100 tokens): `name` and `description` loaded at startup for all skills
2. **Instructions** (<5000 tokens recommended): Full SKILL.md body loaded on activation
3. **Resources** (as needed): Files in `references/`, `scripts/`, `assets/` loaded only when required

This means you can bundle extensive reference material without paying a context cost until it's actually needed.

## File References

Reference other files using relative paths from the skill root:

```markdown
See [the API reference](references/api.md) for method details.
Run the extraction script: `python scripts/extract.py`
```

**Keep references one level deep from SKILL.md.** Don't chain references — SKILL.md → file.md is fine, but SKILL.md → file.md → other-file.md causes agents to partially read or miss content.

For reference files over 100 lines, include a table of contents at the top.

## Content Principles

- **Assume competence.** Only add context the agent doesn't already have. Don't explain what PDFs are or how libraries work.
- **Be opinionated.** Provide a default approach rather than listing options. "Use pdfplumber" is better than "you can use pypdf, pdfplumber, PyMuPDF, or..." Mention alternatives only when they cover genuinely different use cases.
- **Match specificity to fragility.** High freedom (prose instructions) for tasks with many valid approaches; low freedom (exact scripts, specific commands) for fragile or error-prone operations. Think of it as bridge width — narrow bridge with cliffs needs exact guardrails, open field just needs a direction.
- **No time-sensitive information.** Don't write "if before August 2025, use the old API." Use an "old patterns" section with `<details>` if historical context is needed.
- **Consistent terminology.** Pick one term and stick with it — don't alternate between "field", "box", "element", "control".
- **Write for another agent.** The skill will be consumed by a different instance that has no memory of the authoring process. Include non-obvious procedural knowledge, domain-specific details, and reusable context — not things any capable model already knows.

## Writing Workflows

For complex multi-step tasks, provide a checklist the agent can track:

```markdown
## Workflow

1. Analyze the input
2. Create a plan file
3. Validate the plan (run `scripts/validate.py`)
4. Execute the plan
5. Verify the output
```

Include feedback loops for quality-critical operations: run validator → fix errors → repeat.

## Our Harness Integration

Skills in Aleph are activated via the `activate_skill` MCP tool. A PostToolUse hook strips the frontmatter and injects the body as `additionalContext` (system-level message), so it persists better than a tool result during context compression.

At startup, the harness scans `<agent_home>/skills/`, extracts frontmatter, and lists available skills in the session context.

## Detailed Reference

For the full best practices guide (patterns, anti-patterns, evaluation strategies, script guidelines), see [references/best-practices.md](references/best-practices.md).
