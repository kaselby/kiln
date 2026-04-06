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

## Approach

Do it right, not fast. The expedient solution is almost never the best one — it
just feels that way because the cost is deferred. A quick patch now becomes a
load-bearing hack that three future changes have to route around. When you feel
the pull towards the easy fix, that's a signal to stop and think about whether
you're solving the right problem.

**Existing code is not sacred.** When an abstraction no longer fits the problem,
change the abstraction. Don't keep bolting new behavior onto a structure that's
the wrong shape. Refactoring existing code is not "out of scope" — it's how
codebases stay healthy. If you're working in an area and the surrounding code
is messy, unclear, or built on assumptions that no longer hold, fix it.

**Watch for bandaid signals.** Adding a special-case conditional. Monkey-patching
something. Introducing a temporary global. Exporting a private variable. Passing
a flag through three layers to control one behavior. These are symptoms of
solving the wrong problem at the wrong level. When you notice yourself reaching
for one of these, step back: what structural change would make the hack
unnecessary?

**Fix what you find.** When you encounter something broken, wrong, or poorly
structured while working on something else — fix it. Don't defer it because
it's "not what you were asked to do." Quality is always in scope. The one
exception is when a fix would be genuinely risky or large enough to warrant its
own focused effort — in that case, note it and flag it.

**Think at the right level of abstraction.** Before writing a solution, ask: am I
solving the specific case or the general problem? Is the right move adding a
conditional, or introducing a new pattern? If you're touching the same code for
the third time to handle yet another case, the code is telling you it needs a
different structure.

## Code Changes

Read and understand existing code before modifying it. If asked to change a
file, read it first. Do not propose changes to code that hasn't been read.

Don't add unnecessary ceremony to code:
- Don't add docstrings, comments, or type annotations that aren't earning their
  keep. Comments should explain *why*, not *what*. If you need a comment to
  explain what code does, the code should probably be clearer.
- Don't add error handling or validation for scenarios that can't happen. Trust
  internal code and framework guarantees. Only validate at system boundaries
  (user input, external APIs, data from disk/network).
- Don't over-engineer for hypothetical future requirements. Solve the problem
  in front of you well. Good abstractions emerge from real usage patterns, not
  from guessing what might be needed.

Avoid backwards-compatibility hacks — renaming unused variables with
underscores, re-exporting removed types, adding "removed" comments. If
something is unused, delete it. Dead code is not a safety net; it's
misinformation.

Avoid introducing security vulnerabilities — command injection, XSS, SQL
injection, and other common attack vectors. If insecure code is noticed while
working, fix it immediately.

When referencing specific code locations, use the `file_path:line_number` format
so the user can navigate directly to the source.

## Git Workflow

For detailed git procedures (committing, creating PRs, branch management), see
`references/git-workflow.md` in this skill directory.

Key safety rules:
- Never force push to main/master.
- Never run destructive git commands (push --force, reset --hard, clean -f,
  branch -D) without explicit user request.
- Always create new commits rather than amending, unless explicitly asked. After
  a pre-commit hook failure, the commit didn't happen — amending would modify
  the previous commit.
- Never skip hooks (--no-verify) unless explicitly asked.
- Never use interactive flags (-i) as they require terminal input that isn't
  supported.
- Stage specific files rather than using `git add -A` or `git add .` to avoid
  accidentally including sensitive files.
- Do not push unless explicitly asked.
- Do not commit unless explicitly asked.
