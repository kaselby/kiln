"""Scheduler data models — schedule entries, triggers, actions, runtime state.

The schedule schema is the contract that the loop, CLI, tests, and all future
consumers hang off of. Changes here ripple everywhere, so be deliberate.

Schedule entries are declarative config (loaded from YAML, never mutated at
runtime). Runtime state (last-fired times, one-shot completion) lives in a
separate state file.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

import yaml

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Duration parsing
# ---------------------------------------------------------------------------

_DURATION_RE = re.compile(r"^(\d+)\s*(s|m|h|d)$")
_DURATION_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}

DEFAULT_CATCHUP = timedelta(hours=1)


def parse_duration(value: str | bool | None) -> timedelta | None:
    """Parse a duration string like '4h', '30m', '1d' into a timedelta.

    Returns None if value is False (explicitly disabled).
    Returns DEFAULT_CATCHUP if value is None or True.
    """
    if value is False:
        return None
    if value is None or value is True:
        return DEFAULT_CATCHUP
    if isinstance(value, str):
        m = _DURATION_RE.match(value.strip())
        if not m:
            raise ValueError(f"Invalid duration: {value!r}")
        return timedelta(seconds=int(m.group(1)) * _DURATION_UNITS[m.group(2)])
    raise ValueError(f"Invalid duration type: {type(value)}")


# ---------------------------------------------------------------------------
# Trigger models
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CronTrigger:
    kind: Literal["cron"] = "cron"
    expr: str = ""
    timezone: str = ""

    def validate(self) -> list[str]:
        errors = []
        if not self.expr:
            errors.append("cron trigger missing 'expr'")
        else:
            try:
                from croniter import croniter
                croniter(self.expr)
            except (ValueError, KeyError) as e:
                errors.append(f"invalid cron expression {self.expr!r}: {e}")
        return errors


@dataclass(frozen=True)
class AtTrigger:
    kind: Literal["at"] = "at"
    time: str = ""

    def validate(self) -> list[str]:
        errors = []
        if not self.time:
            errors.append("at trigger missing 'time'")
        else:
            try:
                datetime.fromisoformat(self.time)
            except ValueError as e:
                errors.append(f"invalid 'at' time {self.time!r}: {e}")
        return errors

    @property
    def parsed_time(self) -> datetime:
        dt = datetime.fromisoformat(self.time)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt


Trigger = CronTrigger | AtTrigger


# ---------------------------------------------------------------------------
# Action models
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DeliverTarget:
    kind: str = "agent"
    agent: str = ""
    tags: tuple[str, ...] = ()
    match: Literal["any", "all"] = "any"
    fallback: Literal["inbox", "drop", "error"] = "inbox"

    def validate(self) -> list[str]:
        errors = []
        if self.kind != "agent":
            errors.append(f"unsupported target kind: {self.kind!r}")
        if not self.agent:
            errors.append("deliver target missing 'agent'")
        if self.match not in ("any", "all"):
            errors.append(f"unsupported match mode: {self.match!r}")
        if self.fallback not in ("inbox", "drop", "error"):
            errors.append(f"unsupported fallback mode: {self.fallback!r}")
        return errors


@dataclass(frozen=True)
class SpawnAction:
    kind: Literal["spawn"] = "spawn"
    agent: str = ""
    template: str | None = None
    mode: str | None = None
    prompt: str | None = None

    def validate(self) -> list[str]:
        errors = []
        if not self.agent:
            errors.append("spawn action missing 'agent'")
        return errors


@dataclass(frozen=True)
class DeliverAction:
    kind: Literal["deliver"] = "deliver"
    target: DeliverTarget = field(default_factory=DeliverTarget)
    summary: str = ""
    body: str = ""
    priority: str = "normal"

    def validate(self) -> list[str]:
        errors = []
        if not self.summary:
            errors.append("deliver action missing 'summary'")
        if not self.body:
            errors.append("deliver action missing 'body'")
        errors.extend(self.target.validate())
        return errors


Action = SpawnAction | DeliverAction


# ---------------------------------------------------------------------------
# Schedule entry
# ---------------------------------------------------------------------------

@dataclass
class ScheduleEntry:
    id: str
    enabled: bool
    trigger: Trigger
    action: Action
    catchup: timedelta | None  # None = disabled

    def validate(self) -> list[str]:
        errors = []
        if not self.id:
            errors.append("entry missing 'id'")
        errors.extend(self.trigger.validate())
        errors.extend(self.action.validate())
        return errors


# ---------------------------------------------------------------------------
# Schedule loader
# ---------------------------------------------------------------------------

_KNOWN_TOP_KEYS = {"id", "enabled", "trigger", "action", "catchup"}
_KNOWN_TRIGGER_KEYS = {"kind", "expr", "timezone", "time"}
_KNOWN_SPAWN_KEYS = {"kind", "agent", "template", "mode", "prompt"}
_KNOWN_DELIVER_KEYS = {"kind", "target", "summary", "body", "priority"}
_KNOWN_TARGET_KEYS = {"kind", "agent", "tags", "match", "fallback"}


def _check_unknown_keys(data: dict, known: set[str], context: str) -> list[str]:
    unknown = set(data.keys()) - known
    if unknown:
        return [f"unknown {context} field(s): {', '.join(sorted(unknown))}"]
    return []


def _parse_trigger(data: Any) -> tuple[Trigger | None, list[str]]:
    if not isinstance(data, dict):
        return None, ["trigger must be a mapping"]
    errors = _check_unknown_keys(data, _KNOWN_TRIGGER_KEYS, "trigger")
    kind = data.get("kind")
    if kind == "cron":
        trigger = CronTrigger(expr=data.get("expr", ""), timezone=data.get("timezone", ""))
        return trigger, errors
    elif kind == "at":
        trigger = AtTrigger(time=data.get("time", ""))
        return trigger, errors
    else:
        return None, errors + [f"unsupported trigger kind: {kind!r}"]


def _parse_target(data: Any) -> tuple[DeliverTarget | None, list[str]]:
    if not isinstance(data, dict):
        return None, ["target must be a mapping"]
    errors = _check_unknown_keys(data, _KNOWN_TARGET_KEYS, "target")
    raw_tags = data.get("tags", [])
    if isinstance(raw_tags, list):
        tags = tuple(str(t) for t in raw_tags if t)
    else:
        tags = ()
        errors.append("target 'tags' must be a list")
    target = DeliverTarget(
        kind=data.get("kind", "agent"),
        agent=data.get("agent", ""),
        tags=tags,
        match=data.get("match", "any"),
        fallback=data.get("fallback", "inbox"),
    )
    return target, errors


def _parse_action(data: Any) -> tuple[Action | None, list[str]]:
    if not isinstance(data, dict):
        return None, ["action must be a mapping"]
    kind = data.get("kind")
    if kind == "spawn":
        errors = _check_unknown_keys(data, _KNOWN_SPAWN_KEYS, "spawn action")
        action = SpawnAction(
            agent=data.get("agent", ""),
            template=data.get("template"),
            mode=data.get("mode"),
            prompt=data.get("prompt"),
        )
        return action, errors
    elif kind == "deliver":
        errors = _check_unknown_keys(data, _KNOWN_DELIVER_KEYS, "deliver action")
        target_data = data.get("target")
        if target_data is None:
            return None, errors + ["deliver action missing 'target'"]
        target, target_errors = _parse_target(target_data)
        errors.extend(target_errors)
        action = DeliverAction(
            target=target or DeliverTarget(),
            summary=data.get("summary", ""),
            body=data.get("body", ""),
            priority=data.get("priority", "normal"),
        )
        return action, errors
    else:
        return None, [f"unsupported action kind: {kind!r}"]


def parse_entry(data: Any) -> tuple[ScheduleEntry | None, list[str]]:
    """Parse a single schedule entry from a YAML dict.

    Returns (entry, errors). If errors is non-empty, entry may be None.
    """
    if not isinstance(data, dict):
        return None, ["entry must be a mapping"]

    entry_id = data.get("id", "")
    prefix = f"[{entry_id}] " if entry_id else ""
    errors: list[str] = []

    errors.extend(f"{prefix}{e}" for e in _check_unknown_keys(data, _KNOWN_TOP_KEYS, "entry"))

    trigger_data = data.get("trigger")
    if trigger_data is None:
        errors.append(f"{prefix}entry missing 'trigger'")
        trigger = None
    else:
        trigger, trigger_errors = _parse_trigger(trigger_data)
        errors.extend(f"{prefix}{e}" for e in trigger_errors)

    action_data = data.get("action")
    if action_data is None:
        errors.append(f"{prefix}entry missing 'action'")
        action = None
    else:
        action, action_errors = _parse_action(action_data)
        errors.extend(f"{prefix}{e}" for e in action_errors)

    try:
        catchup = parse_duration(data.get("catchup"))
    except ValueError as e:
        errors.append(f"{prefix}invalid catchup: {e}")
        catchup = DEFAULT_CATCHUP

    if trigger is None or action is None:
        return None, errors

    entry = ScheduleEntry(
        id=entry_id,
        enabled=data.get("enabled", True),
        trigger=trigger,
        action=action,
        catchup=catchup,
    )

    validation_errors = entry.validate()
    errors.extend(f"{prefix}{e}" for e in validation_errors)

    if errors:
        return None, errors
    return entry, []


def load_schedule(path: Path) -> list[ScheduleEntry]:
    """Load schedule entries from a YAML file.

    Invalid entries are skipped with loud log warnings.
    Returns only successfully parsed entries.
    """
    if not path.exists():
        return []
    try:
        data = yaml.safe_load(path.read_text())
    except Exception as e:
        log.error("Failed to read schedule file %s: %s", path, e)
        return []

    if not data or not isinstance(data, dict):
        return []

    raw_entries = data.get("schedules")
    if not raw_entries or not isinstance(raw_entries, list):
        return []

    entries: list[ScheduleEntry] = []
    seen_ids: set[str] = set()
    for raw in raw_entries:
        entry, errors = parse_entry(raw)
        if errors:
            for e in errors:
                log.warning("Schedule validation: %s", e)
            continue
        if entry.id in seen_ids:
            log.warning("Duplicate schedule entry id: %r (skipping)", entry.id)
            continue
        seen_ids.add(entry.id)
        entries.append(entry)

    return entries


# ---------------------------------------------------------------------------
# Runtime state
# ---------------------------------------------------------------------------

@dataclass
class EntryState:
    last_fired: datetime | None = None
    completed: bool = False
    fire_count: int = 0


@dataclass
class SchedulerState:
    last_check: datetime | None = None
    entries: dict[str, EntryState] = field(default_factory=dict)

    def get_entry(self, entry_id: str) -> EntryState:
        if entry_id not in self.entries:
            self.entries[entry_id] = EntryState()
        return self.entries[entry_id]

    def record_fire(self, entry_id: str, fired_at: datetime) -> None:
        state = self.get_entry(entry_id)
        state.last_fired = fired_at
        state.fire_count += 1

    def record_completion(self, entry_id: str, fired_at: datetime) -> None:
        self.record_fire(entry_id, fired_at)
        self.get_entry(entry_id).completed = True


def load_state(path: Path) -> SchedulerState:
    """Load scheduler runtime state from JSON."""
    if not path.exists():
        return SchedulerState()
    try:
        data = json.loads(path.read_text())
    except Exception as e:
        log.error("Failed to read scheduler state %s: %s", path, e)
        return SchedulerState()

    state = SchedulerState()
    if "_last_check" in data:
        try:
            state.last_check = datetime.fromisoformat(data["_last_check"])
        except (ValueError, TypeError):
            pass

    for entry_id, edata in data.get("entries", {}).items():
        es = EntryState()
        if "last_fired" in edata:
            try:
                es.last_fired = datetime.fromisoformat(edata["last_fired"])
            except (ValueError, TypeError):
                pass
        es.completed = edata.get("completed", False)
        es.fire_count = edata.get("fire_count", 0)
        state.entries[entry_id] = es

    return state


def save_state(state: SchedulerState, path: Path) -> None:
    """Persist scheduler state atomically to JSON."""
    path.parent.mkdir(parents=True, exist_ok=True)
    data: dict[str, Any] = {}
    if state.last_check:
        data["_last_check"] = state.last_check.isoformat()
    entries_data: dict[str, Any] = {}
    for entry_id, es in state.entries.items():
        ed: dict[str, Any] = {"fire_count": es.fire_count}
        if es.last_fired:
            ed["last_fired"] = es.last_fired.isoformat()
        if es.completed:
            ed["completed"] = True
        entries_data[entry_id] = ed
    data["entries"] = entries_data

    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.rename(path)
