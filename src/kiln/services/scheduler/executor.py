"""Daemon-backed scheduler executor — concrete action implementations.

This module bridges the abstract scheduler executor interface to real
daemon infrastructure: spawning sessions via management, delivering
messages to session inboxes resolved via tag matching.

Phase 4 of the scheduler implementation. Depends on:
- daemon management (spawn, tag resolution)
- agent registry (home directory lookup)
- inbox message format conventions
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from .engine import ActionResult, SchedulerExecutor
from .models import DeliverAction, SpawnAction

if TYPE_CHECKING:
    from kiln.daemon.management import ManagementActions

log = logging.getLogger(__name__)

# Well-known subdirectory for scheduler-delivered messages when no
# live session matches and fallback is "inbox".
SCHEDULED_INBOX_DIR = "_scheduled"


def _write_inbox_message(
    inbox_dir: Path,
    summary: str,
    body: str,
    priority: str = "normal",
) -> Path:
    """Write a scheduler message to an inbox directory."""
    inbox_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    uid = uuid.uuid4().hex[:6]
    msg_path = inbox_dir / f"msg-{ts}-scheduler-{uid}.md"
    msg_path.write_text(
        f"---\n"
        f"from: scheduler\n"
        f'summary: "{summary}"\n'
        f"priority: {priority}\n"
        f"timestamp: {datetime.now(timezone.utc).isoformat()}\n"
        f"---\n\n"
        f"{body}\n"
    )
    return msg_path


class DaemonExecutor(SchedulerExecutor):
    """Executor backed by the Kiln daemon's management layer.

    Resolves delivery targets via tag-aware presence queries and
    spawns sessions through the daemon's launch infrastructure.
    """

    def __init__(self, management: ManagementActions):
        self._mgmt = management

    async def execute_spawn(self, action: SpawnAction) -> ActionResult:
        result = await self._mgmt.spawn_session(
            agent=action.agent,
            prompt=action.prompt,
            mode=action.mode,
            template=action.template,
            requested_by="scheduler",
        )
        return ActionResult(success=result.success, message=result.message)

    async def execute_deliver(self, action: DeliverAction) -> ActionResult:
        target = action.target

        # Resolve matching sessions
        matched = self._mgmt.resolve_by_tags(
            agent=target.agent,
            tags=list(target.tags),
            match=target.match,
        )

        if matched:
            delivered_to = []
            for session in matched:
                inbox_dir = Path(session.agent_home) / "inbox" / session.session_id
                msg_path = _write_inbox_message(
                    inbox_dir, action.summary, action.body, action.priority,
                )
                delivered_to.append(session.session_id)
                log.info(
                    "Delivered to %s: %s", session.session_id, msg_path.name,
                )
            return ActionResult(
                True,
                f"delivered to {len(delivered_to)} session(s): {', '.join(delivered_to)}",
            )

        # No matching sessions — apply fallback
        if target.fallback == "drop":
            log.info("No matching sessions for %s, dropping (fallback=drop)", target.agent)
            return ActionResult(True, "no matching sessions, dropped per fallback policy")

        if target.fallback == "error":
            log.warning("No matching sessions for %s (fallback=error)", target.agent)
            return ActionResult(False, f"no matching sessions for agent {target.agent!r}")

        # fallback == "inbox" — durable drop to agent-level scheduled inbox
        agent_home = self._mgmt._resolve_agent_home(target.agent)
        if not agent_home:
            return ActionResult(False, f"cannot resolve home for agent {target.agent!r}")

        inbox_dir = agent_home / "inbox" / SCHEDULED_INBOX_DIR
        msg_path = _write_inbox_message(
            inbox_dir, action.summary, action.body, action.priority,
        )
        log.info(
            "No matching sessions for %s, fell back to durable inbox: %s",
            target.agent, msg_path,
        )
        return ActionResult(True, f"no matching sessions, delivered to durable inbox: {msg_path.name}")
