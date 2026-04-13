"""Security challenge flow — core state machine.

Owns the challenge logic (password verification, strike counting, retry
policy) while delegating all platform I/O to a transport object supplied
by the adapter.  This keeps the security-critical decision logic testable
and platform-independent.

The helper returns an outcome dict — it never triggers lockdown or other
side effects.  The caller decides what to do with failure.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Protocol, runtime_checkable

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Transport protocol — implemented by each platform adapter
# ---------------------------------------------------------------------------

@runtime_checkable
class ChallengeTransport(Protocol):
    """Platform-specific I/O for security challenges.

    Implementations handle posting messages, waiting for responses, and
    cleaning up artifacts on the target platform.  Message tracking for
    cleanup is the transport's responsibility.
    """

    async def post_challenge(self, text: str) -> None:
        """Post the initial challenge prompt (may prepend platform mentions)."""
        ...

    async def wait_for_response(self, timeout: float) -> tuple[str, str, str] | None:
        """Block until a candidate response arrives or *timeout* expires.

        Returns ``(content, author_id, message_id)`` on response,
        or ``None`` on timeout.  The transport is responsible for
        tracking the response message_id for later cleanup.
        """
        ...

    async def post_message(self, text: str) -> None:
        """Post a follow-up message (strike warnings, confirmations)."""
        ...

    async def delete_message(self, message_id: str) -> None:
        """Delete a single message by platform ID."""
        ...

    async def cleanup(self) -> None:
        """Delete all messages tracked during this challenge."""
        ...


# ---------------------------------------------------------------------------
# Password verification
# ---------------------------------------------------------------------------

def check_password(answer: str, passwords: list[dict]) -> str:
    """Check *answer* against the password list.

    Returns ``"valid"``, ``"used"``, or ``"invalid"``.
    """
    normalised = answer.strip().lower()
    for entry in passwords:
        if entry["word"].lower() == normalised:
            if entry.get("status") == "used":
                return "used"
            return "valid"
    return "invalid"


# ---------------------------------------------------------------------------
# Challenge state machine
# ---------------------------------------------------------------------------

async def run_security_challenge(
    transport: ChallengeTransport,
    *,
    reason: str,
    passwords: list[dict],
    timeout: float = 60,
    max_attempts: int = 2,
) -> dict:
    """Run an interactive security challenge via *transport*.

    The full flow: post a challenge prompt, loop waiting for responses,
    verify passwords, track strikes, and return an outcome.

    Returns one of::

        {"result": "verified", "password": str, "author_id": str}
        {"result": "failed",   "strikes": int}

    Transport or setup errors should be caught by the caller — this
    function assumes the transport is ready and raises on transport
    failures.
    """
    unused = sum(1 for p in passwords if p.get("status") != "used")
    strikes = 0

    # --- Post initial challenge ---
    challenge_text = (
        "\U0001f512 **Security verification required**\n"
        f"Reason: {reason}\n\n"
        f"Send a password from your OTP list. "
        f"You have {int(timeout)} seconds.\n"
        f"({unused} unused password{'s' if unused != 1 else ''} remaining)"
    )
    await transport.post_challenge(challenge_text)
    log.info("Security challenge started (reason: %s)", reason)

    # --- Response loop ---
    while strikes < max_attempts:
        result = await transport.wait_for_response(timeout)

        if result is None:
            # Timeout — counts as a strike
            strikes += 1
            log.info("Security challenge: timeout — strike %d/%d",
                     strikes, max_attempts)
            if strikes < max_attempts:
                await transport.post_message(
                    f"\u23f0 No response — strike {strikes}/{max_attempts}. "
                    f"You have {int(timeout)} more seconds."
                )
            continue

        content, author_id, response_msg_id = result
        pw_result = check_password(content, passwords)

        if pw_result == "valid":
            log.info("Security challenge: password accepted")
            await transport.post_message("\u2705 Verified. Password accepted.")
            await transport.cleanup()
            return {
                "result": "verified",
                "password": content.strip().lower(),
                "author_id": author_id,
            }

        elif pw_result == "used":
            # Used password — free retry, no strike
            log.info("Security challenge: used password — free retry")
            await transport.post_message(
                "\u26a0\ufe0f That password has already been used "
                "— try another."
            )
            await transport.delete_message(response_msg_id)
            continue

        else:
            # Wrong password — strike
            strikes += 1
            log.info("Security challenge: wrong password — strike %d/%d",
                     strikes, max_attempts)
            await transport.delete_message(response_msg_id)
            if strikes < max_attempts:
                await transport.post_message(
                    f"\u274c Incorrect — strike {strikes}/{max_attempts}. "
                    f"One more try."
                )
            continue

    # --- Max strikes reached ---
    log.info("Security challenge: %d strikes — failed", strikes)
    await transport.post_message(
        "\U0001f6a8 **LOCKDOWN** — verification failed. "
        "All remote access is being shut down."
    )
    # Brief pause so the lockdown message is visible before cleanup
    await asyncio.sleep(2)
    await transport.cleanup()
    return {"result": "failed", "strikes": strikes}
