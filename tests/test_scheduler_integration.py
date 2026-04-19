"""Integration tests for scheduler daemon executor — tag matching, delivery, spawn, fallback."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml

from kiln.daemon.management import ActionResult as MgmtActionResult, ManagementActions
from kiln.daemon.state import DaemonState, PresenceRegistry, SessionRecord
from kiln.services.scheduler.engine import ActionResult
from kiln.services.scheduler.executor import (
    SCHEDULED_INBOX_DIR,
    DaemonExecutor,
    _write_inbox_message,
)
from kiln.services.scheduler.models import (
    DeliverAction,
    DeliverTarget,
    SpawnAction,
)

ET = timezone(timedelta(hours=-4))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_session(
    session_id: str,
    agent: str = "beth",
    agent_home: str = "/tmp/beth",
    tags: list[str] | None = None,
    mode: str = "yolo",
) -> SessionRecord:
    return SessionRecord(
        session_id=session_id,
        agent_name=agent,
        agent_home=agent_home,
        tags=tags or [],
        mode=mode,
    )


class FakeManagement:
    """Minimal fake of ManagementActions for executor testing.

    Avoids needing real DaemonState/config. Only implements the methods
    the executor actually calls.
    """

    def __init__(self, sessions: list[SessionRecord] | None = None, agent_homes: dict[str, Path] | None = None):
        self._sessions = sessions or []
        self._agent_homes = agent_homes or {}
        self.spawn_calls: list[dict] = []

    def resolve_by_tags(
        self, agent: str, tags: list[str] | tuple[str, ...], match: str = "any",
    ) -> list[SessionRecord]:
        agent_sessions = [s for s in self._sessions if s.agent_name == agent]
        if not tags:
            return agent_sessions
        tag_set = set(tags)
        matched = []
        for s in agent_sessions:
            session_tags = set(s.tags)
            if match == "all":
                if tag_set <= session_tags:
                    matched.append(s)
            else:
                if tag_set & session_tags:
                    matched.append(s)
        return matched

    def _resolve_agent_home(self, agent: str) -> Path | None:
        return self._agent_homes.get(agent)

    async def spawn_session(
        self, agent: str, prompt: str | None = None, mode: str | None = None,
        template: str | None = None, requested_by: str | None = None,
    ) -> MgmtActionResult:
        self.spawn_calls.append({
            "agent": agent, "prompt": prompt, "mode": mode,
            "template": template, "requested_by": requested_by,
        })
        return MgmtActionResult(True, "Session launched")


# ---------------------------------------------------------------------------
# Inbox message writing
# ---------------------------------------------------------------------------

class TestWriteInboxMessage:
    def test_creates_message_file(self, tmp_path):
        msg_path = _write_inbox_message(tmp_path / "inbox", "Test", "Hello world")
        assert msg_path.exists()
        content = msg_path.read_text()
        assert "from: scheduler" in content
        assert 'summary: "Test"' in content
        assert "Hello world" in content

    def test_creates_directory(self, tmp_path):
        inbox = tmp_path / "deep" / "nested" / "inbox"
        msg_path = _write_inbox_message(inbox, "Test", "Body")
        assert inbox.exists()
        assert msg_path.exists()


# ---------------------------------------------------------------------------
# Tag matching via resolve_by_tags
# ---------------------------------------------------------------------------

class TestResolveByTags:
    def test_match_any(self):
        mgmt = FakeManagement(sessions=[
            _make_session("beth-s1", tags=["canonical"]),
            _make_session("beth-s2", tags=["worker"]),
            _make_session("beth-s3", tags=[]),
        ])
        matched = mgmt.resolve_by_tags("beth", ["canonical"])
        assert len(matched) == 1
        assert matched[0].session_id == "beth-s1"

    def test_match_all(self):
        mgmt = FakeManagement(sessions=[
            _make_session("beth-s1", tags=["canonical", "pa"]),
            _make_session("beth-s2", tags=["canonical"]),
        ])
        matched = mgmt.resolve_by_tags("beth", ["canonical", "pa"], match="all")
        assert len(matched) == 1
        assert matched[0].session_id == "beth-s1"

    def test_no_tags_returns_all(self):
        mgmt = FakeManagement(sessions=[
            _make_session("beth-s1", tags=["canonical"]),
            _make_session("beth-s2", tags=[]),
        ])
        matched = mgmt.resolve_by_tags("beth", [])
        assert len(matched) == 2

    def test_no_match(self):
        mgmt = FakeManagement(sessions=[
            _make_session("beth-s1", tags=["worker"]),
        ])
        matched = mgmt.resolve_by_tags("beth", ["canonical"])
        assert len(matched) == 0

    def test_filters_by_agent(self):
        mgmt = FakeManagement(sessions=[
            _make_session("beth-s1", agent="beth", tags=["canonical"]),
            _make_session("dalet-s1", agent="dalet", tags=["canonical"]),
        ])
        matched = mgmt.resolve_by_tags("beth", ["canonical"])
        assert len(matched) == 1
        assert matched[0].agent_name == "beth"


# ---------------------------------------------------------------------------
# Executor: deliver
# ---------------------------------------------------------------------------

class TestDaemonExecutorDeliver:
    def _deliver_action(self, tags=("canonical",), match="any", fallback="inbox"):
        return DeliverAction(
            target=DeliverTarget(agent="beth", tags=tags, match=match, fallback=fallback),
            summary="Test delivery",
            body="Hello from scheduler",
            priority="normal",
        )

    @pytest.mark.asyncio
    async def test_deliver_to_matched_session(self, tmp_path):
        home = tmp_path / "beth"
        home.mkdir()
        mgmt = FakeManagement(
            sessions=[_make_session("beth-s1", agent_home=str(home), tags=["canonical"])],
        )
        executor = DaemonExecutor(mgmt)
        result = await executor.execute_deliver(self._deliver_action())

        assert result.success is True
        assert "beth-s1" in result.message
        inbox = home / "inbox" / "beth-s1"
        msgs = list(inbox.glob("msg-*-scheduler-*.md"))
        assert len(msgs) == 1
        assert "Hello from scheduler" in msgs[0].read_text()

    @pytest.mark.asyncio
    async def test_deliver_to_multiple_matched(self, tmp_path):
        home = tmp_path / "beth"
        home.mkdir()
        mgmt = FakeManagement(sessions=[
            _make_session("beth-s1", agent_home=str(home), tags=["worker"]),
            _make_session("beth-s2", agent_home=str(home), tags=["worker"]),
        ])
        executor = DaemonExecutor(mgmt)
        result = await executor.execute_deliver(self._deliver_action(tags=("worker",)))

        assert result.success is True
        assert "2 session(s)" in result.message
        for sid in ["beth-s1", "beth-s2"]:
            inbox = home / "inbox" / sid
            assert len(list(inbox.glob("msg-*.md"))) == 1

    @pytest.mark.asyncio
    async def test_fallback_inbox(self, tmp_path):
        home = tmp_path / "beth"
        home.mkdir()
        mgmt = FakeManagement(
            sessions=[],  # no live sessions
            agent_homes={"beth": home},
        )
        executor = DaemonExecutor(mgmt)
        result = await executor.execute_deliver(self._deliver_action(fallback="inbox"))

        assert result.success is True
        assert "durable inbox" in result.message
        scheduled_inbox = home / "inbox" / SCHEDULED_INBOX_DIR
        msgs = list(scheduled_inbox.glob("msg-*.md"))
        assert len(msgs) == 1

    @pytest.mark.asyncio
    async def test_fallback_drop(self, tmp_path):
        mgmt = FakeManagement(sessions=[])
        executor = DaemonExecutor(mgmt)
        result = await executor.execute_deliver(self._deliver_action(fallback="drop"))

        assert result.success is True
        assert "dropped" in result.message

    @pytest.mark.asyncio
    async def test_fallback_error(self, tmp_path):
        mgmt = FakeManagement(sessions=[])
        executor = DaemonExecutor(mgmt)
        result = await executor.execute_deliver(self._deliver_action(fallback="error"))

        assert result.success is False
        assert "no matching sessions" in result.message

    @pytest.mark.asyncio
    async def test_fallback_inbox_unknown_agent(self):
        mgmt = FakeManagement(sessions=[], agent_homes={})
        executor = DaemonExecutor(mgmt)
        result = await executor.execute_deliver(self._deliver_action(fallback="inbox"))

        assert result.success is False
        assert "cannot resolve home" in result.message


# ---------------------------------------------------------------------------
# Executor: spawn
# ---------------------------------------------------------------------------

class TestDaemonExecutorSpawn:
    @pytest.mark.asyncio
    async def test_spawn_with_template(self):
        mgmt = FakeManagement()
        executor = DaemonExecutor(mgmt)
        action = SpawnAction(agent="beth", template="briefing", mode="yolo")
        result = await executor.execute_spawn(action)

        assert result.success is True
        assert len(mgmt.spawn_calls) == 1
        call = mgmt.spawn_calls[0]
        assert call["agent"] == "beth"
        assert call["template"] == "briefing"
        assert call["mode"] == "yolo"
        assert call["requested_by"] == "scheduler"

    @pytest.mark.asyncio
    async def test_spawn_minimal(self):
        mgmt = FakeManagement()
        executor = DaemonExecutor(mgmt)
        action = SpawnAction(agent="beth")
        result = await executor.execute_spawn(action)

        assert result.success is True
        call = mgmt.spawn_calls[0]
        assert call["template"] is None
        assert call["prompt"] is None

    @pytest.mark.asyncio
    async def test_spawn_with_prompt(self):
        mgmt = FakeManagement()
        executor = DaemonExecutor(mgmt)
        action = SpawnAction(agent="beth", prompt="Do the thing")
        result = await executor.execute_spawn(action)

        assert result.success is True
        assert mgmt.spawn_calls[0]["prompt"] == "Do the thing"
