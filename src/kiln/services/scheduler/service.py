"""Scheduler service — daemon-hosted timed trigger substrate.

Wraps the scheduler loop and daemon executor into a Service that
plugs into the daemon's service lifecycle.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from ..base import Service
from .engine import SchedulerLoop
from .executor import DaemonExecutor

if TYPE_CHECKING:
    from kiln.daemon.server import KilnDaemon

log = logging.getLogger(__name__)

# Default paths relative to kiln home
DEFAULT_SCHEDULE_PATH = "daemon/state/schedule.yml"
DEFAULT_STATE_PATH = "daemon/state/scheduler-state.json"


class SchedulerService(Service):
    """Timed trigger service — cron and one-shot scheduling."""

    def __init__(
        self,
        schedule_path: Path | None = None,
        state_path: Path | None = None,
        check_interval: int = 60,
    ):
        self._schedule_path = schedule_path
        self._state_path = state_path
        self._check_interval = check_interval
        self._loop: SchedulerLoop | None = None

    @property
    def name(self) -> str:
        return "scheduler"

    async def start(self, daemon: KilnDaemon) -> None:
        kiln_home = Path(daemon.config.kiln_home)

        schedule_path = self._schedule_path or kiln_home / DEFAULT_SCHEDULE_PATH
        state_path = self._state_path or kiln_home / DEFAULT_STATE_PATH

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
            log.info("Scheduler service stopped")
