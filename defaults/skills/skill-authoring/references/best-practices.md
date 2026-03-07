# Best Practices Reference

Detailed authoring guidance from the official Agent Skills documentation. Consult this
when writing or reviewing skills — the main SKILL.md covers the spec, this covers craft.

## Contents

- Progressive disclosure patterns
- Template and examples patterns
- Conditional workflows
- Feedback loops
- Scripts and executable code
- Anti-patterns
- Evaluation and iteration
- Checklist

## Progressive Disclosure Patterns

### Pattern 1: High-level guide with references

SKILL.md provides quick-start instructions and points to reference files for depth.

````markdown
# PDF Processing

## Quick start

Extract text with pdfplumber:
```python
import pdfplumber
with pdfplumber.open("file.pdf") as pdf:
    text = pdf.pages[0].extract_text()
```

## Advanced features

**Form filling**: See [FORMS.md](references/FORMS.md) for complete guide
**API reference**: See [REFERENCE.md](references/REFERENCE.md) for all methods
````

### Pattern 2: Domain-specific organization

For skills covering multiple domains, organize by domain so agents only load what's relevant.

```
bigquery-skill/
├── SKILL.md (overview and navigation)
└── references/
    ├── finance.md (revenue, billing metrics)
    ├── sales.md (opportunities, pipeline)
    └── product.md (usage analytics)
```

SKILL.md acts as a routing table — describes what's in each file so the agent reads only
what the task requires.

### Pattern 3: Conditional details

Show basic content inline, link to advanced content:

```markdown
## Editing documents

For simple edits, modify the XML directly.

**For tracked changes**: See [REDLINING.md](references/REDLINING.md)
**For OOXML details**: See [OOXML.md](references/OOXML.md)
```

## Template Pattern

Provide output templates. Match strictness to the task:

**Strict** (API responses, data formats): "ALWAYS use this exact template structure"
with a complete example.

**Flexible** (reports, analysis): "Here is a sensible default format, but use your best
judgment" with an adaptable example.

## Examples Pattern

When output quality depends on seeing examples, provide input/output pairs:

````markdown
## Commit message format

**Example 1:**
Input: Added user authentication with JWT tokens
Output:
```
feat(auth): implement JWT-based authentication

Add login endpoint and token validation middleware
```
````

Examples communicate style and expectations more clearly than descriptions alone.

## Conditional Workflow Pattern

Guide agents through decision points:

```markdown
1. Determine the modification type:

   **Creating new content?** → Follow "Creation workflow" below
   **Editing existing content?** → Follow "Editing workflow" below
```

If workflows become large, push them into separate files and route from SKILL.md.

## Feedback Loops

For quality-critical operations, build in validation cycles:

```markdown
1. Make edits
2. Validate: `python scripts/validate.py`
3. If validation fails → fix issues → validate again
4. Only proceed when validation passes
```

The plan-validate-execute pattern is particularly valuable: have the agent create an
intermediate plan file (e.g. `changes.json`), validate it with a script, then execute.
This catches errors before they hit the actual target.

## Scripts and Executable Code

### When to provide scripts

Even though agents can write code, pre-made scripts are more reliable, save tokens,
save time, and ensure consistency. Provide them for:

- Deterministic operations (validation, data transformation)
- Fragile multi-step processes
- Anything where generated code frequently gets edge cases wrong

### Script guidelines

- **Handle errors explicitly.** Don't punt to the agent — catch exceptions, provide
  helpful messages, create defaults when sensible.
- **Document magic numbers.** Every constant should have a comment explaining why
  that value. If you don't know the right value, the agent won't either.
- **Make execution intent clear** in SKILL.md:
  - "Run `scripts/analyze.py` to extract fields" (execute it)
  - "See `scripts/analyze.py` for the extraction algorithm" (read as reference)
- **List dependencies** explicitly. Don't assume packages are installed.

### Visual analysis

When inputs can be rendered as images (PDFs, layouts, diagrams), convert them and let
the agent analyze visually. Claude's vision capabilities help understand spatial layouts.

## Anti-patterns

- **Windows-style paths.** Always use forward slashes: `references/guide.md`, not
  `references\guide.md`.
- **Too many options.** Provide a default approach, not a menu. Mention alternatives
  only when they cover genuinely different use cases (e.g. OCR for scanned PDFs).
- **Deeply nested references.** Keep one level deep from SKILL.md. Chains of files
  referencing other files cause agents to partially read or miss content.
- **Over-explaining basics.** Agents are already competent. Don't explain what a PDF
  is or how pip works.
- **Time-sensitive content.** Avoid date-gated instructions. Use "old patterns" sections
  with `<details>` tags for historical context.
- **Vague descriptions.** The frontmatter `description` is critical for discovery.
  "Helps with files" will never match correctly against 100+ skills.
- **Inconsistent terminology.** Pick one term per concept and stick with it throughout.

## Evaluation and Iteration

### Build evaluations first

Create evaluations BEFORE writing extensive documentation:

1. Run the agent on representative tasks without a skill — document failures
2. Build 3+ test scenarios targeting those failures
3. Measure baseline performance
4. Write minimal instructions to address the gaps
5. Iterate: run evaluations, compare against baseline, refine

### Iterative development with two agents

Work with "Claude A" to author/refine the skill, test with "Claude B" (fresh instance
with the skill loaded) on real tasks. Observe what Claude B does wrong, bring insights
back to Claude A.

Watch for:
- Unexpected file navigation patterns
- Missed references to important files
- Overreliance on certain sections (maybe that content should be in SKILL.md)
- Ignored files (maybe unnecessary or poorly signaled)

## Checklist

### Core quality
- [ ] `name` matches directory, follows naming rules
- [ ] `description` is third-person, specific, includes keywords and trigger conditions
- [ ] SKILL.md body under 500 lines
- [ ] Detailed content in separate reference files
- [ ] No time-sensitive information
- [ ] Consistent terminology throughout
- [ ] Concrete examples, not abstract descriptions
- [ ] File references one level deep
- [ ] Progressive disclosure used appropriately
- [ ] Workflows have clear steps

### Scripts (if applicable)
- [ ] Scripts handle errors explicitly
- [ ] No magic numbers without justification
- [ ] Required packages documented
- [ ] Clear execution vs. reference intent
- [ ] Validation/verification steps for critical operations
- [ ] Feedback loops for quality-critical tasks
