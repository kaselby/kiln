"""Guardrails — regex-based detection of dangerous bash commands.

IMPORTANT: these guardrails are regex-based and operate on the command
string as submitted.  They catch direct invocations but are trivially
bypassable by an agent that writes a script to a file and then executes
it, uses variable indirection, pipes to an interpreter, encodes the
command, or uses shell aliases/functions.  This is by design — the
guardrails are a speed bump for the common case (catching accidental
dangerous commands in normal task flow), NOT a security boundary.

Known bypass categories that are NOT caught:
  - Write-to-file + execute:  Write("rm -rf /") -> bash script.sh
  - Variable indirection:     CMD='git push'; $CMD
  - Pipe to interpreter:      echo 'git push' | bash
  - Encoding:                 echo <base64> | base64 -d | bash
  - Alias/function def:       alias gp='git push'; gp
  - curl | bash:              curl URL | bash

The real safeguard is the user reviewing the agent's intent, not
these regex patterns.  See also: the permission mode system in permissions.py.

This module handles detection only — it answers "is this command
dangerous and how?" without making approval decisions or interacting
with the user.
"""

import os
import re


# ---------------------------------------------------------------------------
# Quoted-string masking
# ---------------------------------------------------------------------------
# Commands whose string arguments are themselves executed (and therefore
# still dangerous).  Patterns are checked against the command word that
# "owns" a quoted string — if it matches, the string content is NOT masked.
_EXEC_WRAPPERS = re.compile(
    r"\b(?:bash|sh|zsh|fish|dash|ksh|csh|tcsh"
    r"|eval|exec|source|\."
    r"|ssh|sudo|su|doas|env|nohup|xargs|watch"
    r"|python[23]?|ruby|perl|node|php"
    r"|screen|tmux\s+(?:send-keys|run-shell))\b"
)


def _mask_quoted_strings(command: str) -> str:
    """Replace content inside quoted strings with placeholder text.

    Preserves string content when the surrounding command is an execution
    wrapper (bash -c, ssh, eval, etc.) so that dangerous patterns inside
    those strings are still caught.

    Falls back to the original command (unmasked) on any parse ambiguity,
    erring on the side of false positives over missed detections.
    """
    # Mask heredoc bodies first — they're almost always data, not commands.
    # Matches: << EOF ... EOF, << 'EOF' ... EOF, << "EOF" ... EOF
    # Also handles <<- (strip tabs) variant.
    command = re.sub(
        r"<<-?\s*['\"]?(\w+)['\"]?.*?\n.*?\n\1\b",
        lambda m: m.group(0).split("\n", 1)[0] + "\n__HEREDOC_MASKED__\n" + m.group(1),
        command,
        flags=re.DOTALL,
    )

    # Split on unescaped single and double quotes using a simple state machine.
    # Single-quoted strings: always literal (no escapes).
    # Double-quoted strings: we mask content unless it contains $( or `
    #   (command substitution makes the content potentially executable).
    result = []
    i = 0
    n = len(command)

    while i < n:
        ch = command[i]

        if ch == "'":
            # Single-quoted string — find closing quote
            end = command.find("'", i + 1)
            if end == -1:
                # Unclosed quote — fall back to original
                return command
            # Check if the command owning this string is an exec wrapper
            prefix = command[:i].rstrip()
            if _EXEC_WRAPPERS.search(prefix):
                result.append(command[i:end + 1])  # preserve
            else:
                result.append("'__MASKED__'")
            i = end + 1

        elif ch == '"':
            # Double-quoted string — find closing unescaped quote
            j = i + 1
            while j < n:
                if command[j] == '\\':
                    j += 2  # skip escaped char
                elif command[j] == '"':
                    break
                else:
                    j += 1
            if j >= n:
                # Unclosed quote — fall back to original
                return command
            inner = command[i + 1:j]
            # If inner contains command substitution, don't mask (content is executable)
            if '$(' in inner or '`' in inner:
                result.append(command[i:j + 1])
            else:
                prefix = command[:i].rstrip()
                if _EXEC_WRAPPERS.search(prefix):
                    result.append(command[i:j + 1])  # preserve
                else:
                    result.append('"__MASKED__"')
            i = j + 1

        elif ch == '\\' and i + 1 < n:
            result.append(command[i:i + 2])
            i += 2

        else:
            result.append(ch)
            i += 1

    return "".join(result)


# ---------------------------------------------------------------------------
# Pattern registry
# ---------------------------------------------------------------------------
# Each entry: (compiled regex, tier, human description)
# "block"   = always denied, no override from the agent
# "confirm" = requires explicit user approval (even in YOLO mode)
#
# Block patterns are checked first and take priority over confirm patterns.

_GUARDRAIL_PATTERNS: list[tuple[re.Pattern, str, str]] = []


def _compile_guardrails():
    raw = [
        # --- Block: catastrophic, almost never intentional from an agent ---
        (r"\brm\s+-\S*r\S*\s+/\s*$", "block", "recursive delete from filesystem root"),
        (r"\brm\s+-\S*r\S*\s+/\*", "block", "recursive delete from filesystem root"),
        (r"\brm\s+-\S*r\S*\s+~/?\s*$", "block", "recursive delete of home directory"),
        (r"\bmkfs\b", "block", "format filesystem"),
        (r"\bdd\b.*\bof\s*=\s*/dev/", "block", "write directly to raw device"),

        # --- Confirm: destructive but sometimes legitimate ---
        (r"\bgit\s+push\b", "confirm", "git push"),
        (r"\bgit\s+filter-branch\b", "confirm", "git filter-branch (rewrites history)"),
        (r"\bgit\s+filter-repo\b", "confirm", "git filter-repo (rewrites history)"),
        (r"\bgit\s+reset\s+--hard\b", "confirm", "git reset --hard (discards changes)"),
        (r"\bgit\s+clean\b.*-\w*f", "confirm", "git clean (deletes untracked files)"),
        (r"\btmux\s+kill-(session|server)\b", "confirm", "kill tmux session/server"),
        (r"\bkillall\s", "confirm", "kill processes by name (killall)"),
        (r"\bpkill\s", "confirm", "kill processes by pattern (pkill)"),
    ]
    for pattern_str, tier, desc in raw:
        _GUARDRAIL_PATTERNS.append((re.compile(pattern_str), tier, desc))


_compile_guardrails()


def register_guardrail(pattern: str, tier: str, description: str) -> None:
    """Register an additional guardrail pattern.

    Args:
        pattern: Regex pattern string to match against bash commands.
        tier: "block" (always denied) or "confirm" (requires user approval).
        description: Human-readable description shown in the prompt/notification.
    """
    if tier not in ("block", "confirm"):
        raise ValueError(f"tier must be 'block' or 'confirm', got {tier!r}")
    _GUARDRAIL_PATTERNS.append((re.compile(pattern), tier, description))


# ---------------------------------------------------------------------------
# Detection helpers
# ---------------------------------------------------------------------------

def _has_rm_rf(command: str) -> bool:
    """Check if a command contains rm with both -r and -f flags in any form."""
    if not re.search(r"\brm\s", command):
        return False
    match = re.search(r"\brm\s(.*)", command)
    if not match:
        return False
    after_rm = match.group(1)
    # Collect only actual flag tokens — must be preceded by whitespace or at
    # start of string. This excludes hyphens embedded in filenames like
    # "aleph-first-bay.yml" where "-first" would be a false positive.
    flags = re.findall(r"(?:^|\s)-(\w+)", after_rm)
    all_flags = "".join(flags)
    return "r" in all_flags and "f" in all_flags


def is_exempt(reason: str, cwd: str, agent_home: str, command: str = "") -> bool:
    """Check if a confirm-tier guardrail should be exempted based on CWD or command.

    Exempts git push when the shell is inside the agent's home directory,
    or when the command explicitly targets it (e.g. `cd ~/.<agent> && git push`
    or `git -C ~/.<agent> push`).
    """
    if reason == "git push":
        try:
            real_cwd = os.path.realpath(cwd)
            if real_cwd == agent_home or real_cwd.startswith(agent_home + os.sep):
                return True
        except (OSError, ValueError):
            pass
        # Check if the command itself targets the agent's home repo
        if command:
            home = os.path.expanduser("~")
            home_patterns = [
                re.escape(agent_home),
                re.escape(agent_home.replace(home, "~")),
                r"\$KILN_AGENT_HOME",
                r"\$\{KILN_AGENT_HOME\}",
            ]
            for pat in home_patterns:
                # cd <agent-home> && git push  /  cd <agent-home>; git push
                if re.search(rf"\bcd\s+[\"']?{pat}[\"']?\s*[;&]", command):
                    return True
                # git -C <agent-home> push
                if re.search(rf"\bgit\s+-C\s+[\"']?{pat}[\"']?\s+push\b", command):
                    return True
    return False


def classify_danger(command: str) -> tuple[str, str] | None:
    """Classify a bash command's danger level.

    Returns (tier, description) where tier is "block" or "confirm",
    or None if the command is not flagged as dangerous.

    Quoted string content is masked before matching to avoid false
    positives on commit messages, echo statements, etc.  Content is
    preserved (not masked) when the quoting command is an execution
    wrapper (bash -c, ssh, eval, ...) or contains command substitution.
    """
    masked = _mask_quoted_strings(command)

    # Block patterns first
    for pattern, tier, desc in _GUARDRAIL_PATTERNS:
        if tier == "block" and pattern.search(masked):
            return ("block", desc)

    # rm -rf detection (confirm tier) — uses helper for flag combinations
    if _has_rm_rf(masked):
        return ("confirm", "recursive force delete (rm -rf)")

    # Other confirm patterns
    for pattern, tier, desc in _GUARDRAIL_PATTERNS:
        if tier == "confirm" and pattern.search(masked):
            return ("confirm", desc)

    return None
