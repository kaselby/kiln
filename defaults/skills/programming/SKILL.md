---
name: programming
description: >
  Core programming skill for software engineering tasks. This skill should be
  used when working on code — writing, modifying, debugging, refactoring, or
  reviewing. Provides guidelines for code changes, file management, and git
  workflows. Activate this skill when the task involves reading or writing code,
  working with repositories, running tests, or any software development activity.
---

# Programming

## Code Changes

Read and understand existing code before modifying it. If asked to change a file, read it first. Do not propose changes to code that hasn't been read.

Keep changes minimal and focused on what was requested:
- Don't add features, refactor code, or make improvements beyond what was asked.
- Don't add docstrings, comments, or type annotations to unchanged code. Only add comments where the logic isn't self-evident.
- Don't add error handling or validation for scenarios that can't happen. Trust internal code and framework guarantees. Only validate at system boundaries (user input, external APIs).
- Don't create helpers, utilities, or abstractions for one-time operations. Three similar lines of code is better than a premature abstraction.
- Don't design for hypothetical future requirements.
- Don't create files unless they're necessary to achieve the goal. Prefer editing existing files.

Avoid introducing security vulnerabilities — command injection, XSS, SQL injection, and other common attack vectors. If insecure code is noticed, fix it.

Avoid backwards-compatibility hacks like renaming unused variables with underscores, re-exporting removed types, or adding "removed" comments. If something is unused, delete it.

When referencing specific code locations, use the `file_path:line_number` format so the user can navigate directly to the source.

## Git Workflow

For detailed git procedures (committing, creating PRs, branch management), see `references/git-workflow.md` in this skill directory.

Key safety rules:
- Never force push to main/master.
- Never run destructive git commands (push --force, reset --hard, clean -f, branch -D) without explicit user request.
- Always create new commits rather than amending, unless explicitly asked. After a pre-commit hook failure, the commit didn't happen — amending would modify the previous commit.
- Never skip hooks (--no-verify) unless explicitly asked.
- Never use interactive flags (-i) as they require terminal input that isn't supported.
- Stage specific files rather than using `git add -A` or `git add .` to avoid accidentally including sensitive files.
- Do not push unless explicitly asked.
- Do not commit unless explicitly asked.
