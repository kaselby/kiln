"""Tests for the scheduler service — models, parsing, validation, due-check, loop."""

from __future__ import annotations

import asyncio
import json
import textwrap
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
import yaml

from kiln.services.scheduler.models import (
    AtTrigger,
    CronTrigger,
    DeliverAction,
    DeliverTarget,
    EntryState,
    ScheduleEntry,
    SchedulerState,
    SpawnAction,
    load_schedule,
    load_state,
    parse_duration,
    parse_entry,
    save_state,
)

ET = timezone(timedelta(hours=-4))


# =========================================================================
# Phase 1 — Models, parsing, validation
# =========================================================================


class TestParseDuration:
    def test_hours(self):
        assert parse_duration("4h") == timedelta(hours=4)

    def test_minutes(self):
        assert parse_duration("30m") == timedelta(minutes=30)

    def test_seconds(self):
        assert parse_duration("90s") == timedelta(seconds=90)

    def test_days(self):
        assert parse_duration("2d") == timedelta(days=2)

    def test_false_disables(self):
        assert parse_duration(False) is None

    def test_none_gives_default(self):
        assert parse_duration(None) == timedelta(hours=1)

    def test_true_gives_default(self):
        assert parse_duration(True) == timedelta(hours=1)

    def test_invalid_raises(self):
        with pytest.raises(ValueError):
            parse_duration("4x")

    def test_whitespace_stripped(self):
        assert parse_duration(" 2h ") == timedelta(hours=2)


class TestTriggerValidation:
    def test_cron_valid(self):
        t = CronTrigger(expr="0 8 * * 1-5", timezone="America/Toronto")
        assert t.validate() == []

    def test_cron_missing_expr(self):
        t = CronTrigger(expr="", timezone="America/Toronto")
        errors = t.validate()
        assert any("missing" in e for e in errors)

    def test_cron_invalid_expr(self):
        t = CronTrigger(expr="not a cron", timezone="America/Toronto")
        errors = t.validate()
        assert any("invalid cron" in e for e in errors)

    def test_at_valid(self):
        t = AtTrigger(time="2026-04-15T10:00:00-04:00")
        assert t.validate() == []

    def test_at_missing_time(self):
        t = AtTrigger(time="")
        errors = t.validate()
        assert any("missing" in e for e in errors)

    def test_at_invalid_time(self):
        t = AtTrigger(time="not-a-date")
        errors = t.validate()
        assert any("invalid" in e for e in errors)

    def test_at_parsed_time(self):
        t = AtTrigger(time="2026-04-15T10:00:00-04:00")
        assert t.parsed_time == datetime(2026, 4, 15, 10, 0, tzinfo=ET)

    def test_at_parsed_time_naive_gets_utc(self):
        t = AtTrigger(time="2026-04-15T10:00:00")
        assert t.parsed_time.tzinfo == timezone.utc


class TestActionValidation:
    def test_spawn_valid(self):
        a = SpawnAction(agent="beth", template="briefing", mode="yolo")
        assert a.validate() == []

    def test_spawn_missing_agent(self):
        a = SpawnAction(agent="")
        assert any("missing" in e for e in a.validate())

    def test_deliver_valid(self):
        a = DeliverAction(
            target=DeliverTarget(agent="beth"),
            summary="Test",
            body="Hello",
        )
        assert a.validate() == []

    def test_deliver_missing_summary(self):
        a = DeliverAction(target=DeliverTarget(agent="beth"), summary="", body="Hello")
        assert any("summary" in e for e in a.validate())

    def test_deliver_missing_body(self):
        a = DeliverAction(target=DeliverTarget(agent="beth"), summary="Test", body="")
        assert any("body" in e for e in a.validate())

    def test_deliver_missing_target_agent(self):
        a = DeliverAction(target=DeliverTarget(agent=""), summary="Test", body="Hello")
        assert any("agent" in e for e in a.validate())

    def test_deliver_unsupported_target_kind(self):
        a = DeliverAction(
            target=DeliverTarget(kind="channel", agent="beth"),
            summary="Test",
            body="Hello",
        )
        assert any("unsupported" in e for e in a.validate())


class TestParseEntry:
    def _spawn_entry(self, **overrides):
        base = {
            "id": "test-spawn",
            "enabled": True,
            "trigger": {"kind": "cron", "expr": "0 8 * * *", "timezone": "America/Toronto"},
            "action": {"kind": "spawn", "agent": "beth", "template": "briefing", "mode": "yolo"},
            "catchup": "4h",
        }
        base.update(overrides)
        return base

    def _deliver_entry(self, **overrides):
        base = {
            "id": "test-deliver",
            "enabled": True,
            "trigger": {"kind": "at", "time": "2026-04-15T10:00:00-04:00"},
            "action": {
                "kind": "deliver",
                "target": {"kind": "agent", "agent": "beth"},
                "summary": "Test",
                "body": "Hello",
                "priority": "normal",
            },
            "catchup": "2h",
        }
        base.update(overrides)
        return base

    def test_valid_spawn(self):
        entry, errors = parse_entry(self._spawn_entry())
        assert errors == []
        assert entry is not None
        assert entry.id == "test-spawn"
        assert isinstance(entry.trigger, CronTrigger)
        assert isinstance(entry.action, SpawnAction)
        assert entry.action.template == "briefing"
        assert entry.catchup == timedelta(hours=4)

    def test_valid_deliver(self):
        entry, errors = parse_entry(self._deliver_entry())
        assert errors == []
        assert entry is not None
        assert isinstance(entry.trigger, AtTrigger)
        assert isinstance(entry.action, DeliverAction)
        assert entry.action.summary == "Test"

    def test_disabled_entry(self):
        entry, errors = parse_entry(self._spawn_entry(enabled=False))
        assert errors == []
        assert entry is not None
        assert entry.enabled is False

    def test_default_enabled(self):
        data = self._spawn_entry()
        del data["enabled"]
        entry, errors = parse_entry(data)
        assert errors == []
        assert entry.enabled is True

    def test_default_catchup(self):
        data = self._spawn_entry()
        del data["catchup"]
        entry, errors = parse_entry(data)
        assert errors == []
        assert entry.catchup == timedelta(hours=1)

    def test_catchup_false(self):
        entry, errors = parse_entry(self._spawn_entry(catchup=False))
        assert errors == []
        assert entry.catchup is None

    def test_missing_trigger(self):
        data = self._spawn_entry()
        del data["trigger"]
        entry, errors = parse_entry(data)
        assert entry is None
        assert any("trigger" in e for e in errors)

    def test_missing_action(self):
        data = self._spawn_entry()
        del data["action"]
        entry, errors = parse_entry(data)
        assert entry is None
        assert any("action" in e for e in errors)

    def test_unknown_top_level_field(self):
        data = self._spawn_entry(bogus="value")
        entry, errors = parse_entry(data)
        assert any("unknown" in e for e in errors)

    def test_unknown_trigger_field(self):
        data = self._spawn_entry()
        data["trigger"]["bogus"] = "value"
        entry, errors = parse_entry(data)
        assert any("unknown" in e and "trigger" in e for e in errors)

    def test_unknown_spawn_field(self):
        data = self._spawn_entry()
        data["action"]["summary"] = "should not be here"
        entry, errors = parse_entry(data)
        assert any("unknown" in e and "spawn" in e for e in errors)

    def test_unknown_deliver_field(self):
        data = self._deliver_entry()
        data["action"]["template"] = "should not be here"
        entry, errors = parse_entry(data)
        assert any("unknown" in e and "deliver" in e for e in errors)

    def test_unsupported_trigger_kind(self):
        data = self._spawn_entry()
        data["trigger"]["kind"] = "interval"
        entry, errors = parse_entry(data)
        assert any("unsupported trigger" in e for e in errors)

    def test_unsupported_action_kind(self):
        data = self._spawn_entry()
        data["action"]["kind"] = "exec"
        entry, errors = parse_entry(data)
        assert any("unsupported action" in e for e in errors)

    def test_not_a_mapping(self):
        entry, errors = parse_entry("not a dict")
        assert entry is None
        assert errors

    def test_deliver_target_preserves_extra(self):
        """Extra fields on target (e.g. future tag spec) are preserved, not rejected."""
        data = self._deliver_entry()
        data["action"]["target"]["tags"] = ["canonical"]
        data["action"]["target"]["match"] = "any"
        entry, errors = parse_entry(data)
        assert errors == []
        assert entry.action.target.extra == {"tags": ["canonical"], "match": "any"}


class TestLoadSchedule:
    def test_load_valid(self, tmp_path):
        schedule = {
            "schedules": [
                {
                    "id": "morning",
                    "enabled": True,
                    "trigger": {"kind": "cron", "expr": "0 8 * * *"},
                    "action": {"kind": "spawn", "agent": "beth"},
                },
                {
                    "id": "reminder",
                    "enabled": True,
                    "trigger": {"kind": "at", "time": "2026-04-15T10:00:00-04:00"},
                    "action": {
                        "kind": "deliver",
                        "target": {"kind": "agent", "agent": "beth"},
                        "summary": "Test",
                        "body": "Hello",
                    },
                },
            ]
        }
        p = tmp_path / "schedule.yml"
        p.write_text(yaml.dump(schedule))
        entries = load_schedule(p)
        assert len(entries) == 2
        assert entries[0].id == "morning"
        assert entries[1].id == "reminder"

    def test_skips_invalid_entries(self, tmp_path):
        schedule = {
            "schedules": [
                {"id": "good", "trigger": {"kind": "cron", "expr": "0 8 * * *"},
                 "action": {"kind": "spawn", "agent": "beth"}},
                {"id": "bad", "trigger": {"kind": "cron"}},  # missing expr
            ]
        }
        p = tmp_path / "schedule.yml"
        p.write_text(yaml.dump(schedule))
        entries = load_schedule(p)
        assert len(entries) == 1
        assert entries[0].id == "good"

    def test_duplicate_ids_skipped(self, tmp_path):
        schedule = {
            "schedules": [
                {"id": "dup", "trigger": {"kind": "cron", "expr": "0 8 * * *"},
                 "action": {"kind": "spawn", "agent": "beth"}},
                {"id": "dup", "trigger": {"kind": "cron", "expr": "0 9 * * *"},
                 "action": {"kind": "spawn", "agent": "beth"}},
            ]
        }
        p = tmp_path / "schedule.yml"
        p.write_text(yaml.dump(schedule))
        entries = load_schedule(p)
        assert len(entries) == 1

    def test_missing_file(self, tmp_path):
        entries = load_schedule(tmp_path / "nonexistent.yml")
        assert entries == []

    def test_empty_file(self, tmp_path):
        p = tmp_path / "schedule.yml"
        p.write_text("")
        entries = load_schedule(p)
        assert entries == []

    def test_no_schedules_key(self, tmp_path):
        p = tmp_path / "schedule.yml"
        p.write_text(yaml.dump({"other": "data"}))
        entries = load_schedule(p)
        assert entries == []


class TestRuntimeState:
    def test_roundtrip(self, tmp_path):
        state = SchedulerState(
            last_check=datetime(2026, 4, 14, 8, 0, tzinfo=ET),
        )
        state.record_fire("morning", datetime(2026, 4, 14, 8, 0, 12, tzinfo=ET))
        state.record_completion("oneshot", datetime(2026, 4, 15, 10, 0, tzinfo=ET))

        p = tmp_path / "state.json"
        save_state(state, p)
        loaded = load_state(p)

        assert loaded.last_check == state.last_check
        assert loaded.entries["morning"].fire_count == 1
        assert loaded.entries["morning"].last_fired == state.entries["morning"].last_fired
        assert loaded.entries["oneshot"].completed is True
        assert loaded.entries["oneshot"].fire_count == 1

    def test_load_missing(self, tmp_path):
        state = load_state(tmp_path / "nonexistent.json")
        assert state.last_check is None
        assert state.entries == {}

    def test_load_corrupt(self, tmp_path):
        p = tmp_path / "state.json"
        p.write_text("not json")
        state = load_state(p)
        assert state.entries == {}

    def test_atomic_write(self, tmp_path):
        """Save should not leave a .tmp file behind."""
        p = tmp_path / "state.json"
        save_state(SchedulerState(), p)
        assert p.exists()
        assert not p.with_suffix(".tmp").exists()

    def test_get_entry_creates_default(self):
        state = SchedulerState()
        es = state.get_entry("new")
        assert es.last_fired is None
        assert es.completed is False
        assert es.fire_count == 0


# =========================================================================
# Phase 2 — Due-check logic
# =========================================================================

from kiln.services.scheduler.engine import is_due


class TestIsDueCron:
    def _entry(self, expr="0 8 * * *", catchup="4h", enabled=True):
        return ScheduleEntry(
            id="test",
            enabled=enabled,
            trigger=CronTrigger(expr=expr, timezone="America/Toronto"),
            action=SpawnAction(agent="beth"),
            catchup=parse_duration(catchup),
        )

    def test_normal_fire(self):
        """Entry fires when a cron tick fell since last check."""
        now = datetime(2026, 4, 14, 8, 0, 30, tzinfo=ET)
        state = SchedulerState(last_check=datetime(2026, 4, 14, 7, 59, 0, tzinfo=ET))
        assert is_due(self._entry(), state, now) is True

    def test_already_fired(self):
        """Entry does not fire if already fired for this tick."""
        now = datetime(2026, 4, 14, 8, 1, 0, tzinfo=ET)
        state = SchedulerState(last_check=datetime(2026, 4, 14, 7, 59, 0, tzinfo=ET))
        state.record_fire("test", datetime(2026, 4, 14, 8, 0, 5, tzinfo=ET))
        assert is_due(self._entry(), state, now) is False

    def test_not_yet_due(self):
        """Entry does not fire when no tick has passed."""
        now = datetime(2026, 4, 14, 7, 30, 0, tzinfo=ET)
        state = SchedulerState(last_check=datetime(2026, 4, 14, 7, 0, 0, tzinfo=ET))
        assert is_due(self._entry(), state, now) is False

    def test_disabled(self):
        now = datetime(2026, 4, 14, 8, 0, 30, tzinfo=ET)
        state = SchedulerState(last_check=datetime(2026, 4, 14, 7, 59, 0, tzinfo=ET))
        assert is_due(self._entry(enabled=False), state, now) is False

    def test_catchup_within_window(self):
        """Missed tick within catchup window still fires."""
        now = datetime(2026, 4, 14, 10, 0, 0, tzinfo=ET)  # 2h after 8am
        state = SchedulerState(last_check=datetime(2026, 4, 14, 6, 0, 0, tzinfo=ET))
        assert is_due(self._entry(catchup="4h"), state, now) is True

    def test_catchup_outside_window(self):
        """Missed tick outside catchup window does not fire."""
        now = datetime(2026, 4, 14, 14, 0, 0, tzinfo=ET)  # 6h after 8am
        state = SchedulerState(last_check=datetime(2026, 4, 14, 6, 0, 0, tzinfo=ET))
        assert is_due(self._entry(catchup="4h"), state, now) is False

    def test_catchup_disabled(self):
        """With catchup=false, only fires if tick is within normal check interval."""
        now = datetime(2026, 4, 14, 10, 0, 0, tzinfo=ET)  # 2h late
        state = SchedulerState(last_check=datetime(2026, 4, 14, 6, 0, 0, tzinfo=ET))
        assert is_due(self._entry(catchup=False), state, now) is False

    def test_no_last_check_uses_catchup(self):
        """First run with no last_check — uses catchup window from now."""
        now = datetime(2026, 4, 14, 8, 5, 0, tzinfo=ET)  # 5 min after 8am tick
        state = SchedulerState()  # no last_check
        assert is_due(self._entry(catchup="1h"), state, now) is True

    def test_coalesces_multiple_missed(self):
        """Multiple missed ticks within window fire at most once."""
        # Hourly cron, 5h downtime — should fire once, not 5 times
        entry = self._entry(expr="0 * * * *", catchup="4h")
        now = datetime(2026, 4, 14, 13, 5, 0, tzinfo=ET)
        state = SchedulerState(last_check=datetime(2026, 4, 14, 8, 0, 0, tzinfo=ET))
        assert is_due(entry, state, now) is True
        # Simulate fire, then check again — should not fire again
        state.record_fire("test", now)
        assert is_due(entry, state, now) is False


class TestIsDueOneShot:
    def _entry(self, time="2026-04-15T10:00:00-04:00", catchup="2h", enabled=True):
        return ScheduleEntry(
            id="test",
            enabled=enabled,
            trigger=AtTrigger(time=time),
            action=DeliverAction(
                target=DeliverTarget(agent="beth"),
                summary="Test",
                body="Hello",
            ),
            catchup=parse_duration(catchup),
        )

    def test_normal_fire(self):
        now = datetime(2026, 4, 15, 10, 0, 30, tzinfo=ET)
        state = SchedulerState(last_check=datetime(2026, 4, 15, 9, 59, 0, tzinfo=ET))
        assert is_due(self._entry(), state, now) is True

    def test_already_completed(self):
        now = datetime(2026, 4, 15, 10, 1, 0, tzinfo=ET)
        state = SchedulerState()
        state.record_completion("test", datetime(2026, 4, 15, 10, 0, 5, tzinfo=ET))
        assert is_due(self._entry(), state, now) is False

    def test_not_yet_due(self):
        now = datetime(2026, 4, 15, 9, 30, 0, tzinfo=ET)
        state = SchedulerState(last_check=datetime(2026, 4, 15, 9, 0, 0, tzinfo=ET))
        assert is_due(self._entry(), state, now) is False

    def test_catchup_within_window(self):
        now = datetime(2026, 4, 15, 11, 30, 0, tzinfo=ET)  # 1.5h late
        state = SchedulerState(last_check=datetime(2026, 4, 15, 8, 0, 0, tzinfo=ET))
        assert is_due(self._entry(catchup="2h"), state, now) is True

    def test_catchup_outside_window(self):
        now = datetime(2026, 4, 15, 14, 0, 0, tzinfo=ET)  # 4h late
        state = SchedulerState(last_check=datetime(2026, 4, 15, 8, 0, 0, tzinfo=ET))
        assert is_due(self._entry(catchup="2h"), state, now) is False

    def test_disabled(self):
        now = datetime(2026, 4, 15, 10, 0, 30, tzinfo=ET)
        state = SchedulerState()
        assert is_due(self._entry(enabled=False), state, now) is False


# =========================================================================
# Phase 3 — Executor interface + scheduler core loop
# =========================================================================

from kiln.services.scheduler.engine import (
    ActionResult,
    SchedulerExecutor,
    SchedulerLoop,
)


class MockExecutor(SchedulerExecutor):
    """Mock executor that records calls and returns configurable results."""

    def __init__(self, spawn_result=None, deliver_result=None):
        self.spawn_calls: list[SpawnAction] = []
        self.deliver_calls: list[DeliverAction] = []
        self._spawn_result = spawn_result or ActionResult(True, "spawned")
        self._deliver_result = deliver_result or ActionResult(True, "delivered")

    async def execute_spawn(self, action: SpawnAction) -> ActionResult:
        self.spawn_calls.append(action)
        return self._spawn_result

    async def execute_deliver(self, action: DeliverAction) -> ActionResult:
        self.deliver_calls.append(action)
        return self._deliver_result


class FailingExecutor(SchedulerExecutor):
    """Executor that raises exceptions."""

    async def execute_spawn(self, action: SpawnAction) -> ActionResult:
        raise RuntimeError("spawn exploded")

    async def execute_deliver(self, action: DeliverAction) -> ActionResult:
        raise RuntimeError("deliver exploded")


def _write_schedule(path, entries):
    path.write_text(yaml.dump({"schedules": entries}))


class TestSchedulerLoop:
    def _spawn_entry_data(self, id="morning", expr="0 8 * * *"):
        return {
            "id": id,
            "enabled": True,
            "trigger": {"kind": "cron", "expr": expr, "timezone": "America/Toronto"},
            "action": {"kind": "spawn", "agent": "beth", "template": "briefing", "mode": "yolo"},
            "catchup": "4h",
        }

    def _deliver_entry_data(self, id="reminder", time="2026-04-15T10:00:00-04:00"):
        return {
            "id": id,
            "enabled": True,
            "trigger": {"kind": "at", "time": time},
            "action": {
                "kind": "deliver",
                "target": {"kind": "agent", "agent": "beth"},
                "summary": "Reminder",
                "body": "Do the thing.",
            },
            "catchup": "2h",
        }

    @pytest.mark.asyncio
    async def test_fires_due_spawn(self, tmp_path):
        schedule_path = tmp_path / "schedule.yml"
        state_path = tmp_path / "state.json"
        executor = MockExecutor()

        _write_schedule(schedule_path, [self._spawn_entry_data()])
        loop = SchedulerLoop(schedule_path, state_path, executor)

        now = datetime(2026, 4, 14, 8, 0, 30, tzinfo=ET)
        results = await loop.run_once(now)

        assert len(results) == 1
        assert results[0][0] == "morning"
        assert results[0][1].success is True
        assert len(executor.spawn_calls) == 1
        assert executor.spawn_calls[0].agent == "beth"
        assert executor.spawn_calls[0].template == "briefing"

    @pytest.mark.asyncio
    async def test_fires_due_deliver(self, tmp_path):
        schedule_path = tmp_path / "schedule.yml"
        state_path = tmp_path / "state.json"
        executor = MockExecutor()

        _write_schedule(schedule_path, [self._deliver_entry_data()])
        loop = SchedulerLoop(schedule_path, state_path, executor)

        now = datetime(2026, 4, 15, 10, 0, 30, tzinfo=ET)
        results = await loop.run_once(now)

        assert len(results) == 1
        assert results[0][0] == "reminder"
        assert len(executor.deliver_calls) == 1
        assert executor.deliver_calls[0].summary == "Reminder"

    @pytest.mark.asyncio
    async def test_skips_not_due(self, tmp_path):
        schedule_path = tmp_path / "schedule.yml"
        state_path = tmp_path / "state.json"
        executor = MockExecutor()

        _write_schedule(schedule_path, [self._spawn_entry_data()])
        loop = SchedulerLoop(schedule_path, state_path, executor)

        now = datetime(2026, 4, 14, 7, 30, 0, tzinfo=ET)
        results = await loop.run_once(now)

        assert len(results) == 0
        assert len(executor.spawn_calls) == 0

    @pytest.mark.asyncio
    async def test_state_persisted_after_fire(self, tmp_path):
        schedule_path = tmp_path / "schedule.yml"
        state_path = tmp_path / "state.json"
        executor = MockExecutor()

        _write_schedule(schedule_path, [self._spawn_entry_data()])
        loop = SchedulerLoop(schedule_path, state_path, executor)

        now = datetime(2026, 4, 14, 8, 0, 30, tzinfo=ET)
        await loop.run_once(now)

        state = load_state(state_path)
        assert state.last_check == now
        assert state.entries["morning"].fire_count == 1
        assert state.entries["morning"].last_fired == now

    @pytest.mark.asyncio
    async def test_oneshot_marked_completed(self, tmp_path):
        schedule_path = tmp_path / "schedule.yml"
        state_path = tmp_path / "state.json"
        executor = MockExecutor()

        _write_schedule(schedule_path, [self._deliver_entry_data()])
        loop = SchedulerLoop(schedule_path, state_path, executor)

        now = datetime(2026, 4, 15, 10, 0, 30, tzinfo=ET)
        await loop.run_once(now)

        state = load_state(state_path)
        assert state.entries["reminder"].completed is True

    @pytest.mark.asyncio
    async def test_does_not_advance_state_on_failure(self, tmp_path):
        schedule_path = tmp_path / "schedule.yml"
        state_path = tmp_path / "state.json"
        executor = MockExecutor(spawn_result=ActionResult(False, "failed"))

        _write_schedule(schedule_path, [self._spawn_entry_data()])
        loop = SchedulerLoop(schedule_path, state_path, executor)

        now = datetime(2026, 4, 14, 8, 0, 30, tzinfo=ET)
        results = await loop.run_once(now)

        assert results[0][1].success is False
        state = load_state(state_path)
        # last_check is updated (we did check), but entry state is not
        assert state.last_check == now
        assert "morning" not in state.entries or state.entries["morning"].fire_count == 0

    @pytest.mark.asyncio
    async def test_executor_exception_handled(self, tmp_path):
        """Executor exceptions are caught and treated as failures."""
        schedule_path = tmp_path / "schedule.yml"
        state_path = tmp_path / "state.json"
        executor = FailingExecutor()

        _write_schedule(schedule_path, [self._spawn_entry_data()])
        loop = SchedulerLoop(schedule_path, state_path, executor)

        now = datetime(2026, 4, 14, 8, 0, 30, tzinfo=ET)
        results = await loop.run_once(now)

        assert len(results) == 1
        assert results[0][1].success is False
        assert "exploded" in results[0][1].message

    @pytest.mark.asyncio
    async def test_error_isolation_between_entries(self, tmp_path):
        """One entry's failure doesn't prevent other entries from firing."""
        schedule_path = tmp_path / "schedule.yml"
        state_path = tmp_path / "state.json"

        # Use a mixed executor — spawn fails, deliver succeeds
        class MixedExecutor(SchedulerExecutor):
            def __init__(self):
                self.deliver_calls = []

            async def execute_spawn(self, action: SpawnAction) -> ActionResult:
                raise RuntimeError("boom")

            async def execute_deliver(self, action: DeliverAction) -> ActionResult:
                self.deliver_calls.append(action)
                return ActionResult(True, "ok")

        executor = MixedExecutor()
        _write_schedule(schedule_path, [
            self._spawn_entry_data(expr="0 10 * * *"),
            self._deliver_entry_data(time="2026-04-14T10:00:00-04:00"),
        ])
        loop = SchedulerLoop(schedule_path, state_path, executor)

        now = datetime(2026, 4, 14, 10, 0, 30, tzinfo=ET)
        results = await loop.run_once(now)

        assert len(results) == 2
        assert results[0][1].success is False  # spawn failed
        assert results[1][1].success is True   # deliver still ran
        assert len(executor.deliver_calls) == 1

    @pytest.mark.asyncio
    async def test_deduplicates_across_runs(self, tmp_path):
        """Entry fires once, then is not due on subsequent run_once."""
        schedule_path = tmp_path / "schedule.yml"
        state_path = tmp_path / "state.json"
        executor = MockExecutor()

        _write_schedule(schedule_path, [self._spawn_entry_data()])
        loop = SchedulerLoop(schedule_path, state_path, executor)

        now = datetime(2026, 4, 14, 8, 0, 30, tzinfo=ET)
        results1 = await loop.run_once(now)
        assert len(results1) == 1

        now2 = datetime(2026, 4, 14, 8, 1, 0, tzinfo=ET)
        results2 = await loop.run_once(now2)
        assert len(results2) == 0
        assert len(executor.spawn_calls) == 1  # still only 1

    @pytest.mark.asyncio
    async def test_empty_schedule(self, tmp_path):
        schedule_path = tmp_path / "schedule.yml"
        state_path = tmp_path / "state.json"
        executor = MockExecutor()

        _write_schedule(schedule_path, [])
        loop = SchedulerLoop(schedule_path, state_path, executor)

        results = await loop.run_once(datetime(2026, 4, 14, 8, 0, tzinfo=ET))
        assert results == []

    @pytest.mark.asyncio
    async def test_missing_schedule_file(self, tmp_path):
        schedule_path = tmp_path / "nonexistent.yml"
        state_path = tmp_path / "state.json"
        executor = MockExecutor()

        loop = SchedulerLoop(schedule_path, state_path, executor)
        results = await loop.run_once(datetime(2026, 4, 14, 8, 0, tzinfo=ET))
        assert results == []

    @pytest.mark.asyncio
    async def test_start_and_shutdown(self, tmp_path):
        schedule_path = tmp_path / "schedule.yml"
        state_path = tmp_path / "state.json"
        executor = MockExecutor()

        _write_schedule(schedule_path, [])
        loop = SchedulerLoop(schedule_path, state_path, executor, check_interval=1)

        await loop.start()
        assert loop._running is True
        await asyncio.sleep(0.1)
        await loop.shutdown(timeout=2.0)
        assert loop._running is False
