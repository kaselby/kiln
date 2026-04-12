"""Daemon wire protocol — message types and serialization.

JSON-line protocol over Unix domain socket. Each message is a single JSON
object terminated by newline. Every message has a ``type`` field. Requests
include a ``ref`` for response correlation; the daemon echoes it back.

Direction markers in comments:
    C->D  = client (agent) to daemon
    D->C  = daemon to client (response or pushed event)
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from typing import Any


# ---------------------------------------------------------------------------
# Message type constants
# ---------------------------------------------------------------------------

# Requests (C->D)
REGISTER = "register"
DEREGISTER = "deregister"
SUBSCRIBE = "subscribe"
UNSUBSCRIBE = "unsubscribe"
PUBLISH = "publish"
SEND_DIRECT = "send_direct"
SEND_USER = "send_user"
LIST_SUBSCRIPTIONS = "list_subscriptions"
LIST_SESSIONS = "list_sessions"
GET_STATUS = "get_status"
PLATFORM_OP = "platform_op"
MGMT = "mgmt"

# Responses (D->C)
ACK = "ack"
RESULT = "result"
ERROR = "error"

# Pushed events (D->C)
EVENT = "event"


# ---------------------------------------------------------------------------
# Event type constants (carried in the event_type field of EVENT messages)
# ---------------------------------------------------------------------------

EVT_MESSAGE_DIRECT = "message.direct"
EVT_MESSAGE_CHANNEL = "message.channel"
EVT_MESSAGE_INBOUND = "message.inbound"
EVT_CHANNEL_SUBSCRIBED = "channel.subscribed"
EVT_CHANNEL_UNSUBSCRIBED = "channel.unsubscribed"
# Connection/presence events (agent connected/disconnected from daemon)
EVT_SESSION_CONNECTED = "session.connected"
EVT_SESSION_DISCONNECTED = "session.disconnected"
# Management lifecycle events (session spawned/killed via management actions)
EVT_SESSION_STARTED = "session.started"
EVT_SESSION_STOPPED = "session.stopped"
EVT_SESSION_MODE_CHANGED = "session.mode_changed"
EVT_BRIDGE_BOUND = "bridge.bound"
EVT_BRIDGE_UNBOUND = "bridge.unbound"
EVT_STATUS_UPDATED = "status.updated"


# ---------------------------------------------------------------------------
# Wire envelope
# ---------------------------------------------------------------------------

@dataclass
class Message:
    """Wire-level message envelope.

    The ``type`` field identifies the message kind. The ``ref`` field
    correlates requests with responses (client sets it on requests,
    daemon echoes it on responses). The ``data`` dict carries the
    type-specific payload — its keys depend on ``type``.
    """

    type: str
    ref: str | None = None
    data: dict[str, Any] = field(default_factory=dict)

    def to_line(self) -> bytes:
        """Serialize to a newline-terminated JSON bytes line."""
        d: dict[str, Any] = {"type": self.type}
        if self.ref is not None:
            d["ref"] = self.ref
        d.update(self.data)
        return json.dumps(d, separators=(",", ":")).encode() + b"\n"

    @classmethod
    def from_line(cls, line: bytes) -> Message:
        """Parse a JSON bytes line into a Message."""
        return cls.from_dict(json.loads(line))

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Message:
        """Parse a dict into a Message."""
        d = dict(d)
        type_ = d.pop("type")
        ref = d.pop("ref", None)
        return cls(type=type_, ref=ref, data=d)

    @property
    def is_response(self) -> bool:
        return self.type in (ACK, RESULT, ERROR)

    @property
    def is_event(self) -> bool:
        return self.type == EVENT

    @property
    def event_type(self) -> str | None:
        """The event_type field for EVENT messages, None otherwise."""
        if self.type == EVENT:
            return self.data.get("event_type")
        return None


@dataclass
class RequestContext:
    """Identity of the requester, threaded through internal boundaries.

    Carried from ClientConnection into management actions and adapter
    calls so the daemon can enforce permissions and maintain auditability
    without relying on tool availability as the only gate.
    """

    agent_name: str
    session_id: str


@dataclass
class PlatformMessage:
    """Structured payload for platform-originated messages.

    Adapters populate this with authenticated/resolved platform data.
    The daemon owns writing the durable inbox artifact from it.
    """

    sender_name: str
    sender_platform_id: str
    platform: str
    content: str
    trust: str = "unknown"
    channel_desc: str = ""
    channel_id: str = ""
    attachment_paths: list[str] | None = None


def make_ref() -> str:
    """Generate a unique message reference ID."""
    return uuid.uuid4().hex[:12]


# ---------------------------------------------------------------------------
# Request builders (C->D)
# ---------------------------------------------------------------------------

def register(agent: str, session: str, pid: int) -> Message:
    """Register this session with the daemon."""
    return Message(REGISTER, make_ref(), {
        "agent": agent,
        "session": session,
        "pid": pid,
    })


def deregister() -> Message:
    """Deregister and disconnect."""
    return Message(DEREGISTER, make_ref())


def subscribe(channel: str) -> Message:
    """Subscribe to a channel."""
    return Message(SUBSCRIBE, make_ref(), {"channel": channel})


def unsubscribe(channel: str) -> Message:
    """Unsubscribe from a channel."""
    return Message(UNSUBSCRIBE, make_ref(), {"channel": channel})


def publish(channel: str, summary: str, body: str,
            priority: str = "normal") -> Message:
    """Publish a message to a channel."""
    return Message(PUBLISH, make_ref(), {
        "channel": channel,
        "summary": summary,
        "body": body,
        "priority": priority,
    })


def send_direct(to: str, summary: str, body: str,
                priority: str = "normal") -> Message:
    """Send a direct message to another agent session."""
    return Message(SEND_DIRECT, make_ref(), {
        "to": to,
        "summary": summary,
        "body": body,
        "priority": priority,
    })


def send_user(to: str, summary: str, body: str) -> Message:
    """Send a message to an external user (routed through an adapter)."""
    return Message(SEND_USER, make_ref(), {
        "to": to,
        "summary": summary,
        "body": body,
    })


def list_subscriptions() -> Message:
    """Query this session's active channel subscriptions."""
    return Message(LIST_SUBSCRIPTIONS, make_ref())


def list_sessions(agent: str | None = None,
                  status: str | None = None) -> Message:
    """Query registered sessions."""
    data: dict[str, Any] = {}
    if agent is not None:
        data["agent"] = agent
    if status is not None:
        data["status"] = status
    return Message(LIST_SESSIONS, make_ref(), data)


def get_status(scope: str | None = None) -> Message:
    """Query daemon status."""
    data: dict[str, Any] = {}
    if scope is not None:
        data["scope"] = scope
    return Message(GET_STATUS, make_ref(), data)


def platform_op(platform: str, action: str,
                args: dict[str, Any] | None = None) -> Message:
    """Execute a platform-specific operation via an adapter."""
    return Message(PLATFORM_OP, make_ref(), {
        "platform": platform,
        "action": action,
        "args": args or {},
    })


def mgmt(action: str, args: dict[str, Any] | None = None) -> Message:
    """Execute a management action (spawn, stop, interrupt, etc.)."""
    return Message(MGMT, make_ref(), {
        "action": action,
        "args": args or {},
    })


# ---------------------------------------------------------------------------
# Response builders (D->C)
# ---------------------------------------------------------------------------

def ack(ref: str, status: str = "ok", **extra: Any) -> Message:
    """Acknowledge a request."""
    data: dict[str, Any] = {"status": status}
    data.update(extra)
    return Message(ACK, ref=ref, data=data)


def result(ref: str, **data: Any) -> Message:
    """Return structured data in response to a request."""
    return Message(RESULT, ref=ref, data=data)


def error(ref: str, message: str, code: str | None = None) -> Message:
    """Return an error in response to a request."""
    data: dict[str, Any] = {"message": message}
    if code is not None:
        data["code"] = code
    return Message(ERROR, ref=ref, data=data)


# ---------------------------------------------------------------------------
# Event builders (D->C, pushed)
# ---------------------------------------------------------------------------

def event(event_type: str, **data: Any) -> Message:
    """Build a pushed event message."""
    return Message(EVENT, data={"event_type": event_type, **data})
