# Customization

How to bend Kiln to a specific agent's needs — from one-line config tweaks all the way to a fully custom harness. Kiln ships useful defaults and exposes a layered set of override points; pick the lightest one that does the job.

## Overview

An agent built on Kiln has six levers available, roughly ordered from lightest to heaviest:

| Lever | What it changes | Where it lives |
|-------|-----------------|----------------|
| `agent.yml` fields | Model, effort, hooks, context injection, startup commands, tool/skill filters | `<home>/agent.yml` |
| Session templates | Partial config overlays for repeated session shapes | `<home>/templates/<name>.yml` |
| Tools & skills | Agent-authored shell tools and skill packages | `<home>/tools/`, `<home>/skills/` |
| Memory & identity | Persistent context, identity doc, context injection manifest | `<home>/memory/`, `<home>/<AGENT>.md` |
| Kiln reference overrides | Per-agent reshape of the `kiln.md` reference chunk | `<home>/kiln-doc/` |
| Custom harness | Subclass `KilnHarness` with bespoke session behavior | `<home>/harness/` (scaffolded by `kiln init --harness`) |

The first four are covered in their own reference docs (`agent.yml` fields are documented inline in the shipped `examples/full-agent.yml`; see `tools.md`, `skills.md`, and `memory.md` for the others). This doc focuses on the three customization layers that don't have a home elsewhere: **scaffolding**, **session templates**, **Kiln reference overrides**, and **custom harnesses**.

## Scaffolding a new agent

`kiln init <name>` creates a new agent home at `~/.kiln/agents/<name>/` (or at `--dir <path>` if supplied) and registers the agent in `~/.kiln/agents.yml` so other Kiln commands can resolve the name to a path.

### What you get

```
<home>/
  agent.yml              # minimal spec: name, identity_doc, model
  <NAME>.md              # empty identity stub
  inbox/
  logs/
  memory/
  plans/
  scratch/
  state/
  tools/                 # copied from kiln's defaults/tools/
  skills/                # copied from kiln's defaults/skills/
```

After `kiln init`, the copied tools and skills are the agent's. Kiln doesn't re-sync `defaults/` on upgrade — edits, additions, and deletions belong to the agent.

### First edits

Most new agents want to:

1. Fill in `<NAME>.md` with identity content — voice, role, preferences. This doc is injected as the first chunk of the system prompt.
2. Expand `agent.yml` with `context_injection` entries for memory files, any `hooks:` or `startup_commands:`, and anything else beyond the three-field default. See `examples/full-agent.yml` for the full field set.
3. Prune `tools/` and `skills/` — the defaults are a starting kit, not a contract. Delete anything that doesn't fit the agent's purpose.

### Flags

- `--model <id>` — set the model in the generated `agent.yml`. Default is the current Kiln-recommended model.
- `--dir <path>` — put the home somewhere other than `~/.kiln/agents/<name>/`. Useful for agents with custom harnesses, which often live at `~/.<name>/`.
- `--harness` — also scaffold a custom harness project at `<home>/harness/` (see [Custom harness](#custom-harness) below).

## Session templates

Templates are partial `agent.yml` overlays applied on top of the base config at session start. They let a single agent run in multiple shapes — a long-lived PA session, a short-lived dev worker, a conclave facilitator — without branching the agent or duplicating the whole config.

### Where they live

```
<home>/templates/
  <name>.yml
```

Each file is an arbitrary subset of the fields in `agent.yml`. Example:

```yaml
# templates/worker.yml — focused-task sessions
identity_doc: WORKER.md
model: claude-opus-4-7
effort: xhigh

context_injection:
  - path: memory/core.md
    label: Core Memory
```

### How they apply

`kiln run <agent> --template <name>` loads `<home>/agent.yml`, then applies the fields from `<home>/templates/<name>.yml` over top. Any field the template doesn't set falls through to the base config. The template name is recorded in `config.template` for observability.

Templates are a runtime concept — they're applied once at session start. A session can't switch templates mid-flight.

### When to use them

- The agent has genuinely distinct session modes (PA vs. worker, interactive vs. batch).
- You want a lightweight way to pass a different identity doc or context injection set for particular spawns.
- A custom harness wants to fork behavior on `config.template` without adding a new agent.

If you find yourself reaching for a template to tweak a single field, consider whether a `--var` or CLI flag would be cleaner.

## Kiln reference overrides

The `kiln.md` reference chunk of the system prompt — the part this doc lives in — is per-agent customizable without modifying Kiln itself. An agent can reshape which sections appear, replace specific section content, and supply custom template values by dropping files into `<home>/kiln-doc/`.

### Three layers

| Layer | File | Purpose |
|-------|------|---------|
| Skeleton | `skeleton.md` | Controls which `##` headings appear in the Kiln reference and in what order |
| Content | `<Heading>.md` | Replaces the body of a specific section with agent-supplied content |
| Placeholders | `placeholders.yml` | Supplies additional `{placeholder}` values for substitution within the Kiln reference chunk |

All three files are optional. If `kiln-doc/` doesn't exist (or is empty), Kiln's shipped defaults are used unchanged. Each layer can be used independently.

### Skeleton (`skeleton.md`)

A list of `##` headings, one per line, defining the order and inclusion of sections in the Kiln reference chunk:

```markdown
## Principles
## Built-In Tools
## Shell Tools
## Skills
## Memory
## Collaboration
```

- Each line must match `^## (.+)$` to be picked up.
- Listed headings appear in the order given.
- Headings **not** listed are omitted from the rendered Kiln reference.
- Headings listed but not present in Kiln's shipped content (and not overridden by a heading-content file) are skipped silently.
- Duplicate headings log a warning; first occurrence wins.

If `skeleton.md` is absent, Kiln uses its default order unchanged.

### Content overrides (`<Heading>.md`)

Any `*.md` file in `kiln-doc/` other than `skeleton.md` is treated as a per-heading content override. The filename stem (case-sensitive) matches the heading text — `Collaboration.md` replaces the body of the `## Collaboration` section.

File contents replace the default content for that section **wholesale** — there's no append/prepend semantics. If you want Kiln's default content plus your additions, copy Kiln's text into your override and extend it.

### Placeholders (`placeholders.yml`)

A YAML mapping of placeholder names to substitution values:

```yaml
owner: Alice
preferred_shell: zsh
```

These merge into Kiln's built-in placeholder dict (`{home_dir}`, `{kiln_path}`, `{builtins}`, `{tool_index}`, `{skill_index}`, `{agent_id}`, `{cwd}`, `{platform}`, `{today}`, `{now}`). Agent-supplied values override built-ins on name collision.

Substitution scope: **Kiln reference chunk only.** Placeholders in agent identity docs or memory files are left literal.

Malformed YAML raises a clear error at prompt-assembly time (loud fail, not silent drift).

### How the layers compose

At system-prompt assembly time, the Kiln reference chunk is rendered by walking the effective skeleton and, for each heading:

1. If the agent has a content override (`<Heading>.md`), use it.
2. Else, use Kiln's shipped default content for that heading.
3. Substitute placeholders (Kiln defaults merged with agent overrides).

The skeleton is either the agent's `skeleton.md` or Kiln's default ordering.

### When to use overrides

- Reorder or hide sections that don't apply (e.g., an agent with no gateway integration can drop `## Gateway`).
- Replace a section whose shape doesn't fit — most commonly `## Collaboration`, where agent-specific spawn and coordination patterns matter.
- Inject custom template variables used by other overrides or by the agent's identity doc (via the identity doc's own `{placeholder}` use).

### What to avoid

- **Don't use overrides for agent-identity content.** Identity, voice, and agent-specific conventions belong in the agent's identity doc (`<AGENT>.md`), not in `kiln-doc/`. The override is for reshaping Kiln's *reference* presentation.
- **Don't replace sections you intend to keep mostly intact.** Override is wholesale replacement. If you want Kiln's defaults plus additions, duplicate the defaults into your override file and extend them — and accept that you now own keeping that content in sync.
- **Don't edit `kiln-doc/` mid-session.** The loader runs once at system-prompt assembly time (session start); edits take effect on the next session, not the current one.

## Custom harness

`KilnHarness` handles prompt assembly, hook wiring, MCP server setup, and session lifecycle. For simple agents it's enough unchanged. When an agent needs bespoke session behavior — conditional orientation, platform bridges, custom session control, guardrails that close over agent state — subclass it.

From `KilnHarness`'s own docstring:

> Complex agents should write their own harness class that imports kiln's building blocks (`kiln.tools`, `kiln.hooks`, `kiln.prompt`, etc.) and composes them however they want.

### Scaffolding

`kiln init <name> --harness` adds a `harness/` directory inside the agent home:

```
<home>/
  harness/
    pyproject.toml             # standalone project; depends on kiln (editable)
    src/<name>/
      __init__.py
      cli.py                   # parses args, dispatches to cmd_run with harness_class
      harness.py               # <Name>Harness(KilnHarness) subclass
```

After init:

```bash
uv tool install --editable <home>/harness
<name>                         # launches a session using the custom CLI
```

The generated `cli.py` imports `cmd_run` from `kiln.cli` and passes `harness_class=<Name>Harness`, so the custom harness gets wired in without forking argument parsing. Kiln's top-level `kiln` command still works for the agent too — but the custom CLI is the canonical entry point.

### Anatomy of the subclass

The generated `harness.py` is a stub:

```python
from kiln.harness import KilnHarness


class NameHarness(KilnHarness):
    """Extend KilnHarness with name-specific behavior."""
    pass
```

Common extension points (see `src/kiln/harness.py` for the full surface):

- **`_build_orientation(self) -> str | None`** — override to inject an agent-specific orientation prompt at session start. Return `None` to skip.
- **`_agent_hooks(self) -> dict[str, list[HookMatcher]]`** — override to register agent-authored hooks beyond what `agent.yml` declares. Useful when hooks need to close over harness state.
- **`_template_vars(self) -> dict[str, str]`** — override to add placeholders available to orientation/cleanup prompt formatting.
- **`_run_startup_commands(self)`** — override to run extra setup before the session starts (daemon checks, environment priming).
- **`_get_cleanup_prompts(self) -> list[str]`** — override to add end-of-session prompts (memory updates, summary formats).
- **`register_guardrail(...)`** (from `kiln.guardrails`) — register guardrails from `__init__` for content filtering or policy enforcement.

A subclass can also add entirely new methods and call them from overridden lifecycle hooks. The harness is a class, not a protocol — composition over configuration.

### When you actually need one

You need a custom harness when:

- Session behavior depends on state the harness owns (e.g., "if this is the canonical session, inject orientation X; otherwise skip").
- You want to bridge external platforms at the harness level rather than via shell tools (e.g., routing inbound Discord messages to the session's inbox).
- Guardrails or hooks need to close over agent-specific state that isn't clean to express in `agent.yml`.

You **don't** need a custom harness for:

- Different session shapes — use templates.
- Agent-specific prompt content — use the identity doc or Kiln reference overrides.
- Static startup work — use `startup_commands` in `agent.yml`.
- Extra tools or skills — add them to `tools/` or `skills/`.

If none of the bullets in the "need one" list apply, stay on the default `KilnHarness`. The subclass is a real maintenance commitment — Kiln's internal signatures can evolve, and subclass overrides that reach into private methods (`_build_orientation`, `_agent_hooks`) are tracking those internals.

## Hooks and guardrails

Two extension points that sit across `agent.yml` and custom harnesses, used often enough to call out explicitly.

### Hooks

Claude Agent SDK hooks (`PreToolUse`, `PostToolUse`, `UserPromptSubmit`, `Stop`, etc.) can be declared in `agent.yml`:

```yaml
hooks:
  PostToolUse:
    - matcher: Edit|Write
      command: <home>/tools/library/format-check
```

Each hook entry is dispatched by the harness when the matching event fires. For hooks that need to close over harness state rather than run as shell commands, register them from a custom harness via `_agent_hooks()` using `HookMatcher` objects directly.

See `lifecycle.md` for when each hook event fires.

### Guardrails

Guardrails are per-session filters registered via `kiln.guardrails.register_guardrail(...)`. They inspect assistant output (or specific content blocks) and can rewrite, block, or annotate. Typical uses: catching stale model references, filtering out forbidden strings, enforcing platform-specific content policies.

Guardrails are best registered from a custom harness's `__init__` where they can close over config. They run on every assistant turn, so keep them cheap.

## Cross-references

- `home.md` — full layout of an agent home, including `kiln-doc/` location.
- `tools.md` — writing custom shell tools (`tools/core/` + `tools/library/`).
- `skills.md` — packaging and discovering skills.
- `memory.md` — `context_injection` manifest, session summary convention.
- `lifecycle.md` — hook event ordering, session start/stop phases.
- `examples/full-agent.yml` — every `agent.yml` field with inline documentation.

## Conventions

- **Start light.** Try `agent.yml` fields and templates before reaching for overrides. Reach for a custom harness only when the bullets in "When you actually need one" apply.
- **Keep Kiln and agent concerns separated.** Kiln reference overrides are for reshaping Kiln's mechanism explanations; agent identity, voice, and project-specific conventions live in the agent's identity doc.
- **Own what you override.** Any layer you customize becomes yours to maintain — Kiln won't re-sync it on upgrade. This is usually fine; just know it going in.
