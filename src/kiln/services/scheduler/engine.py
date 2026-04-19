"""Scheduler engine — due-check logic, executor interface, and core loop.

The engine is the scheduler's brain. It checks which entries are due,
dispatches them through an abstract executor, and manages state transitions.
It has zero knowledge of daemon internals, platform concepts, or how
actions are concretely executed.
"""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from croniter import croniter

from .models import (
    Action,
    AtTrigger,
    CronTrigger,
    DeliverAction,
    EntryState,
    ScheduleEntry,
    SchedulerState,
    SpawnAction,
    load_schedule,
    load_state,
    save_state,
)

log = logging.getLogger(__name__)

# Default lookback when no last_check is available (first run)
_FIRST_RUN_LOOKBACK = timedelta(minutes=5)

# Default check interval
DEFAULT_CHECK_INTERVAL = 60  # seconds


# ---------------------------------------------------------------------------
# Due-check logic
# ---------------------------------------------------------------------------

def is_due(entry: ScheduleEntry, state: SchedulerState, now: datetime) -> bool:
    """Determine whether a schedule entry should fire at the given time.

    Policy-free: this only answers "is this entry due?" based on trigger
    type, timing, catchup window, and prior fire state. It does not know
    what firing means or what the action does.
    """
    if not entry.enabled:
        return False

    entry_state = state.get_entry(entry.id)

    if isinstance(entry.trigger, CronTrigger):
        return _is_due_cron(entry, entry_state, state, now)
    elif isinstance(entry.trigger, AtTrigger):
        return _is_due_at(entry, entry_state, state, now)
    return False


def _effective_lookback(
    entry: ScheduleEntry,
    state: SchedulerState,
    now: datetime,
) -> datetime:
    """Compute the effective lookback start for due-checking.

    Uses last_check if available, otherwise a short first-run window.
    Capped by the entry's catchup window if set.
    """
    if state.last_check:
        lookback_start = state.last_check
    else:
        lookback_start = now - _FIRST_RUN_LOOKBACK

    if entry.catchup is not None:
        earliest_allowed = now - entry.catchup
        if lookback_start < earliest_allowed:
            lookback_start = earliest_allowed
    else:
        # catchup disabled — only look back a short window
        lookback_start = max(lookback_start, now - timedelta(seconds=DEFAULT_CHECK_INTERVAL * 2))

    return lookback_start


def _is_due_cron(
    entry: ScheduleEntry,
    entry_state: EntryState,
    state: SchedulerState,
    now: datetime,
) -> bool:
    trigger = entry.trigger
    assert isinstance(trigger, CronTrigger)

    tz = ZoneInfo(trigger.timezone) if trigger.timezone else None
    if tz:
        now_local = now.astimezone(tz)
    else:
        now_local = now

    lookback_start = _effective_lookback(entry, state, now)
    if tz:
        lookback_start = lookback_start.astimezone(tz)

    # Find the most recent cron tick in our lookback window.
    # croniter.get_prev() from `now` gives us the most recent tick <= now.
    try:
        cron = croniter(trigger.expr, now_local)
        most_recent_tick = cron.get_prev(datetime)
    except (ValueError, KeyError) as e:
        log.error("Bad cron expression for %r: %s", entry.id, e)
        return False

    # Tick must be within our lookback window
    if most_recent_tick < lookback_start:
        return False

    # Dedupe: don't fire if already fired for a tick at or after this one
    if entry_state.last_fired:
        last_fired = entry_state.last_fired
        if tz and last_fired.tzinfo != tz:
            last_fired = last_fired.astimezone(tz)
        if last_fired >= most_recent_tick:
            return False

    return True


def _is_due_at(
    entry: ScheduleEntry,
    entry_state: EntryState,
    state: SchedulerState,
    now: datetime,
) -> bool:
    trigger = entry.trigger
    assert isinstance(trigger, AtTrigger)

    if entry_state.completed:
        return False

    target_time = trigger.parsed_time
    if target_time > now:
        return False

    # Check catchup window
    if entry.catchup is not None:
        if now - target_time > entry.catchup:
            return False
    else:
        # No catchup — only fire if within a short window of the target time
        if now - target_time > timedelta(seconds=DEFAULT_CHECK_INTERVAL * 2):
            return False

    return True


# ---------------------------------------------------------------------------
# Executor interface
# ---------------------------------------------------------------------------

@dataclass
class ActionResult:
    success: bool
    message: str


class SchedulerExecutor(ABC):
    """Abstract interface for action execution.

    The scheduler loop calls these methods without knowing how actions
    are concretely performed. Daemon-backed implementations go in a
    separate module (Phase 4).
    """

    @abstractmethod
    async def execute_spawn(self, action: SpawnAction) -> ActionResult:
        ...

    @abstractmethod
    async def execute_deliver(self, action: DeliverAction) -> ActionResult:
        ...

    async def execute(self, action: Action) -> ActionResult:
        """Dispatch an action by kind."""
        if isinstance(action, SpawnAction):
            return await self.execute_spawn(action)
        elif isinstance(action, DeliverAction):
            return await self.execute_deliver(action)
        return ActionResult(False, f"unsupported action type: {type(action).__name__}")


# ---------------------------------------------------------------------------
# Core scheduler loop
# ---------------------------------------------------------------------------

class SchedulerLoop:
    """The scheduler's main loop. Checks entries, fires due ones, manages state.

    Completely decoupled from daemon hosting. Takes a schedule file path,
    state file path, and an executor. Can be tested against mock executors
    with zero daemon knowledge.
    """

    def __init__(
        self,
        schedule_path: Path,
        state_path: Path,
        executor: SchedulerExecutor,
        check_interval: int = DEFAULT_CHECK_INTERVAL,
    ):
        self._schedule_path = schedule_path
        self._state_path = state_path
        self._executor = executor
        self._check_interval = check_interval
        self._running = False
        self._task: asyncio.Task | None = None

    async def run_once(self, now: datetime | None = None) -> list[tuple[str, ActionResult]]:
        """Run a single check cycle. Returns list of (entry_id, result) for entries that fired."""
        if now is None:
            now = datetime.now(timezone.utc)

        entries = load_schedule(self._schedule_path)
        state = load_state(self._state_path)
        results: list[tuple[str, ActionResult]] = []

        for entry in entries:
            if not is_due(entry, state, now):
                continue

            log.info("Firing %r (action: %s)", entry.id, entry.action.kind)
            try:
                result = await self._executor.execute(entry.action)
            except Exception as e:
                log.error("Executor error for %r: %s", entry.id, e)
                result = ActionResult(False, f"executor error: {e}")

            results.append((entry.id, result))

            if result.success:
                if isinstance(entry.trigger, AtTrigger):
                    state.record_completion(entry.id, now)
                else:
                    state.record_fire(entry.id, now)
                log.info("Fired %r successfully: %s", entry.id, result.message)
            else:
                log.warning("Failed to fire %r: %s", entry.id, result.message)

        state.last_check = now
        save_state(state, self._state_path)
        return results

    async def run(self) -> None:
        """Run the scheduler loop until stopped."""
        self._running = True
        log.info("Scheduler loop starting (interval: %ds)", self._check_interval)
        while self._running:
            try:
                await self.run_once()
            except Exception as e:
                log.error("Scheduler loop error: %s", e)
            try:
                await asyncio.sleep(self._check_interval)
            except asyncio.CancelledError:
                break
        log.info("Scheduler loop stopped")

    def stop(self) -> None:
        """Signal the loop to stop."""
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()

    async def start(self) -> None:
        """Start the loop as a background task."""
        self._running = True
        self._task = asyncio.create_task(self.run())

    async def shutdown(self, timeout: float = 5.0) -> None:
        """Stop the loop and wait for it to finish."""
        self.stop()
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=timeout)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass
