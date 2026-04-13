"""Tests for kiln.daemon.security ��� challenge state machine with fake transport."""

from __future__ import annotations

import asyncio
import pytest

from kiln.daemon.security import check_password, run_security_challenge


# ---------------------------------------------------------------------------
# Fake transport for testing — no Discord, no I/O
# ---------------------------------------------------------------------------

class FakeTransport:
    """In-memory challenge transport for testing.

    Pre-load ``responses`` with tuples of ``(content, author_id, msg_id)``
    or ``None`` for timeouts.  The transport pops from the front on each
    ``wait_for_response`` call.
    """

    def __init__(self, responses: list[tuple[str, str, str] | None] | None = None):
        self.responses: list[tuple[str, str, str] | None] = list(responses or [])
        self.posted_messages: list[str] = []
        self.deleted_messages: list[str] = []
        self.cleaned_up: bool = False

    async def post_challenge(self, text: str) -> None:
        self.posted_messages.append(text)

    async def wait_for_response(self, timeout: float) -> tuple[str, str, str] | None:
        if not self.responses:
            return None  # timeout
        return self.responses.pop(0)

    async def post_message(self, text: str) -> None:
        self.posted_messages.append(text)

    async def delete_message(self, message_id: str) -> None:
        self.deleted_messages.append(message_id)

    async def cleanup(self) -> None:
        self.cleaned_up = True


# ---------------------------------------------------------------------------
# check_password
# ---------------------------------------------------------------------------

class TestCheckPassword:
    def test_valid_password(self):
        passwords = [{"word": "alpha"}, {"word": "bravo"}]
        assert check_password("alpha", passwords) == "valid"

    def test_valid_case_insensitive(self):
        passwords = [{"word": "Alpha"}]
        assert check_password("ALPHA", passwords) == "valid"

    def test_valid_with_whitespace(self):
        passwords = [{"word": "alpha"}]
        assert check_password("  alpha  ", passwords) == "valid"

    def test_used_password(self):
        passwords = [{"word": "alpha", "status": "used"}]
        assert check_password("alpha", passwords) == "used"

    def test_invalid_password(self):
        passwords = [{"word": "alpha"}]
        assert check_password("wrong", passwords) == "invalid"

    def test_empty_list(self):
        assert check_password("anything", []) == "invalid"


# ---------------------------------------------------------------------------
# run_security_challenge — verified path
# ---------------------------------------------------------------------------

class TestSecurityChallengeVerified:
    @pytest.mark.asyncio
    async def test_immediate_correct_password(self):
        transport = FakeTransport(responses=[
            ("alpha", "user123", "msg1"),
        ])
        passwords = [{"word": "alpha"}, {"word": "bravo"}]

        result = await run_security_challenge(
            transport, reason="test", passwords=passwords,
        )

        assert result["result"] == "verified"
        assert result["password"] == "alpha"
        assert result["author_id"] == "user123"
        assert transport.cleaned_up

    @pytest.mark.asyncio
    async def test_correct_on_second_attempt(self):
        """Wrong password first, correct second — one strike then verified."""
        transport = FakeTransport(responses=[
            ("wrong", "user123", "msg1"),
            ("alpha", "user123", "msg2"),
        ])
        passwords = [{"word": "alpha"}]

        result = await run_security_challenge(
            transport, reason="test", passwords=passwords,
        )

        assert result["result"] == "verified"
        assert "msg1" in transport.deleted_messages  # wrong answer deleted

    @pytest.mark.asyncio
    async def test_used_password_free_retry(self):
        """Used password doesn't count as a strike — gets a free retry."""
        transport = FakeTransport(responses=[
            ("old", "user123", "msg1"),      # used — free retry
            ("alpha", "user123", "msg2"),     # valid
        ])
        passwords = [
            {"word": "old", "status": "used"},
            {"word": "alpha"},
        ]

        result = await run_security_challenge(
            transport, reason="test", passwords=passwords,
        )

        assert result["result"] == "verified"
        # Used-password response should be deleted
        assert "msg1" in transport.deleted_messages


# ---------------------------------------------------------------------------
# run_security_challenge — failed path
# ---------------------------------------------------------------------------

class TestSecurityChallengeFailed:
    @pytest.mark.asyncio
    async def test_two_wrong_passwords(self):
        transport = FakeTransport(responses=[
            ("wrong1", "user123", "msg1"),
            ("wrong2", "user123", "msg2"),
        ])
        passwords = [{"word": "alpha"}]

        result = await run_security_challenge(
            transport, reason="test", passwords=passwords,
        )

        assert result["result"] == "failed"
        assert result["strikes"] == 2
        assert transport.cleaned_up

    @pytest.mark.asyncio
    async def test_two_timeouts(self):
        """Two consecutive timeouts should result in failure."""
        transport = FakeTransport(responses=[
            None,  # timeout
            None,  # timeout
        ])
        passwords = [{"word": "alpha"}]

        result = await run_security_challenge(
            transport, reason="test", passwords=passwords,
        )

        assert result["result"] == "failed"
        assert result["strikes"] == 2

    @pytest.mark.asyncio
    async def test_timeout_then_wrong(self):
        transport = FakeTransport(responses=[
            None,                             # timeout — strike 1
            ("wrong", "user123", "msg1"),      # wrong — strike 2
        ])
        passwords = [{"word": "alpha"}]

        result = await run_security_challenge(
            transport, reason="test", passwords=passwords,
        )

        assert result["result"] == "failed"
        assert result["strikes"] == 2

    @pytest.mark.asyncio
    async def test_custom_max_attempts(self):
        transport = FakeTransport(responses=[
            ("wrong1", "u", "m1"),
            ("wrong2", "u", "m2"),
            ("wrong3", "u", "m3"),
        ])
        passwords = [{"word": "alpha"}]

        result = await run_security_challenge(
            transport, reason="test", passwords=passwords,
            max_attempts=3,
        )

        assert result["result"] == "failed"
        assert result["strikes"] == 3


# ---------------------------------------------------------------------------
# run_security_challenge — message flow
# ---------------------------------------------------------------------------

class TestSecurityChallengeMessages:
    @pytest.mark.asyncio
    async def test_challenge_posts_initial_message(self):
        transport = FakeTransport(responses=[
            ("alpha", "user123", "msg1"),
        ])
        passwords = [{"word": "alpha"}, {"word": "bravo"}]

        await run_security_challenge(
            transport, reason="suspicious activity", passwords=passwords,
        )

        # First posted message is the challenge prompt
        assert len(transport.posted_messages) >= 1
        challenge = transport.posted_messages[0]
        assert "Security verification required" in challenge
        assert "suspicious activity" in challenge
        assert "2 unused passwords" in challenge

    @pytest.mark.asyncio
    async def test_strike_warning_posted(self):
        transport = FakeTransport(responses=[
            ("wrong", "user123", "msg1"),
            ("alpha", "user123", "msg2"),
        ])
        passwords = [{"word": "alpha"}]

        await run_security_challenge(
            transport, reason="test", passwords=passwords,
        )

        # Should have: challenge + strike warning + verified confirmation
        strike_msgs = [m for m in transport.posted_messages if "strike" in m.lower()]
        assert len(strike_msgs) >= 1

    @pytest.mark.asyncio
    async def test_timeout_warning_posted(self):
        transport = FakeTransport(responses=[
            None,                           # timeout — strike 1
            ("alpha", "user123", "msg1"),    # valid
        ])
        passwords = [{"word": "alpha"}]

        await run_security_challenge(
            transport, reason="test", passwords=passwords,
        )

        timeout_msgs = [m for m in transport.posted_messages if "No response" in m]
        assert len(timeout_msgs) >= 1

    @pytest.mark.asyncio
    async def test_lockdown_message_on_failure(self):
        transport = FakeTransport(responses=[
            ("wrong1", "u", "m1"),
            ("wrong2", "u", "m2"),
        ])
        passwords = [{"word": "alpha"}]

        await run_security_challenge(
            transport, reason="test", passwords=passwords,
        )

        lockdown_msgs = [m for m in transport.posted_messages if "LOCKDOWN" in m]
        assert len(lockdown_msgs) == 1

    @pytest.mark.asyncio
    async def test_used_password_warning(self):
        transport = FakeTransport(responses=[
            ("old", "user123", "msg1"),
            ("alpha", "user123", "msg2"),
        ])
        passwords = [
            {"word": "old", "status": "used"},
            {"word": "alpha"},
        ]

        await run_security_challenge(
            transport, reason="test", passwords=passwords,
        )

        used_msgs = [m for m in transport.posted_messages if "already been used" in m]
        assert len(used_msgs) == 1

    @pytest.mark.asyncio
    async def test_singular_password_grammar(self):
        """'1 unused password remaining' not '1 unused passwords remaining'."""
        transport = FakeTransport(responses=[("alpha", "u", "m")])
        passwords = [{"word": "alpha"}]

        await run_security_challenge(
            transport, reason="test", passwords=passwords,
        )

        challenge = transport.posted_messages[0]
        assert "1 unused password remaining" in challenge
