"""Scheduler service — daemon-hosted timed trigger substrate.

Wraps the scheduler loop and daemon executor into a Service that
plugs into the daemon's service lifecycle. Implements the
``kiln.services.base.Service`` protocol structurally — no inheritance
needed.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .engine import SchedulerLoop
from .executor import DaemonExecutor

if TYPE_CHECKING:
    from kiln.services.base import DaemonHost

log = logging.getLogger(__name__)

# Default paths relative to kiln home
DEFAULT_SCHEDULE_PATH = "daemon/state/schedule.yml"
DEFAULT_STATE_PATH = "daemon/state/scheduler-state.json"
DEFAULT_CHECK_INTERVAL = 60


class SchedulerService:
    """Timed trigger service — cron and one-shot scheduling.

    Config (from ``services.scheduler`` in daemon config):
        schedule_path: optional override for the schedule YAML location.
        state_path:    optional override for the persistent state JSON.
        check_interval: how often (seconds) the loop checks for due entries.
    """

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        config = config or {}
        sp = config.get("schedule_path")
        st = config.get("state_path")
        self._schedule_path = Path(sp).expanduser() if sp else None
        self._state_path = Path(st).expanduser() if st else None
        self._check_interval = int(config.get("check_interval", DEFAULT_CHECK_INTERVAL))
        self._loop: SchedulerLoop | None = None
        self._schedule_path_used: Path | None = None

    @property
    def name(self) -> str:
        return "scheduler"

    async def start(self, daemon: DaemonHost) -> None:
        kiln_home = Path(daemon.config.kiln_home)

        schedule_path = self._schedule_path or kiln_home / DEFAULT_SCHEDULE_PATH
        state_path = self._state_path or kiln_home / DEFAULT_STATE_PATH
        self._schedule_path_used = schedule_path

        executor = DaemonExecutor(daemon.management)
        self._loop = SchedulerLoop(
            schedule_path=schedule_path,
            state_path=state_path,
            executor=executor,
            check_interval=self._check_interval,
        )

        await self._loop.start()
        log.info("Scheduler service started (schedule: %s, interval: %ds)",
                 schedule_path, self._check_interval)

    async def stop(self) -> None:
        if self._loop:
            await self._loop.shutdown()
            self._loop = None
            log.info("Scheduler service stopped")

    def status(self) -> dict[str, Any]:
        return {
            "running": self._loop is not None,
            "schedule_path": str(self._schedule_path_used) if self._schedule_path_used else None,
            "check_interval": self._check_interval,
        }
