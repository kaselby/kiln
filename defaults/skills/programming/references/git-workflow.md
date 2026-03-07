# Git Workflow Reference

## Creating Commits

When asked to commit:

1. Run `git status` (never use `-uall` flag) and `git diff` in parallel to understand staged and unstaged changes.
2. Run `git log --oneline -5` to see recent commit message style.
3. Analyze all changes and draft a commit message:
   - Summarize the nature of the changes (new feature, enhancement, bug fix, refactoring, etc.).
   - Use "add" for new features, "update" for enhancements, "fix" for bug fixes.
   - Keep it concise (1-2 sentences), focusing on "why" not "what."
   - Do not commit files that likely contain secrets (.env, credentials, etc.).
4. Stage specific files and create the commit.

Pass commit messages via HEREDOC for clean formatting:
```bash
git commit -m "$(cat <<'EOF'
Commit message here.

Co-Authored-By: Aleph <noreply@anthropic.com>
EOF
)"
```

If a pre-commit hook fails: fix the issue, re-stage, and create a **new** commit. Do not amend — the failed commit never happened, so amending would modify the previous commit.

## Creating Pull Requests

When asked to create a PR:

1. Run `git status`, `git diff`, and `git log` / `git diff <base>...HEAD` in parallel to understand the full set of changes from all commits on the branch.
2. Draft a PR title (under 70 chars) and description based on **all commits**, not just the latest.
3. Push with `-u` if needed, then create the PR:

```bash
gh pr create --title "the pr title" --body "$(cat <<'EOF'
## Summary
<1-3 bullet points>

## Test plan
- [ ] Testing checklist items...
EOF
)"
```

Return the PR URL when done.

## Branch Management

- Use `gh` CLI for all GitHub operations (issues, PRs, checks, releases).
- If given a GitHub URL, use `gh` to get the information.
- To view PR comments: `gh api repos/owner/repo/pulls/123/comments`
