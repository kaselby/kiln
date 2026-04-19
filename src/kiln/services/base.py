"""Service lifecycle protocol for Kiln daemon-hosted services.

Services are optional extensions that integrate with the daemon but are
conceptually separate from core. Each service follows a simple lifecycle:
start(daemon) / stop(). Services own their own startup behavior and
register RPC handlers explicitly during start().

See: scratch/pa-design/service-topology-spec-2026-04-14.md
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kiln.daemon.server import KilnDaemon


class Service(ABC):
    """Base class for daemon-hosted services."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Service name for logging and status reporting."""
        ...

    @abstractmethod
    async def start(self, daemon: KilnDaemon) -> None:
        """Start the service. Called during daemon startup.

        Services should register their RPC handlers, start background
        tasks, and acquire any resources they need here.
        """
        ...

    @abstractmethod
    async def stop(self) -> None:
        """Stop the service. Called during daemon shutdown.

        Services should cancel background tasks, release resources,
        and deregister any RPC handlers here.
        """
        ...
