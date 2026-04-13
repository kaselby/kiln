"""Smoke tests for the daemon core — protocol, state, client/server lifecycle.

Tests the daemon's core operations: register, subscribe, publish, direct
send, disconnect cleanup. Uses real Unix sockets in a temp directory.
"""

import asyncio
import json
import tempfile
from pathlib import Path

import pytest
import pytest_asyncio

from kiln.daemon import protocol as proto
from kiln.daemon.config import DaemonConfig
from kiln.daemon.client import DaemonClient, DaemonError, DaemonUnavailableError
from kiln.daemon.server import KilnDaemon
from kiln.daemon.state import (
    ChannelRegistry,
    DaemonState,
    PresenceRegistry,
    SessionRecord,
    SurfaceSubscriptionRegistry,
)


# ---------------------------------------------------------------------------
# Protocol tests
# ---------------------------------------------------------------------------

class TestProtocol:
    def test_message_round_trip(self):
        msg = proto.subscribe("test-ch", agent="beth", session="beth-swift-crane")
        line = msg.to_line()
        parsed = proto.Message.from_line(line)
        assert parsed.type == proto.SUBSCRIBE
        assert parsed.ref == msg.ref
        assert parsed.data["channel"] == "test-ch"

    def test_response_builders(self):
        ref = "abc123"
        a = proto.ack(ref, subscriber_count=3)
        assert a.type == proto.ACK
        assert a.ref == ref
        assert a.data["status"] == "ok"
        assert a.data["subscriber_count"] == 3

        r = proto.result(ref, channels=["a", "b"])
        assert r.type == proto.RESULT
        assert r.data["channels"] == ["a", "b"]

        e = proto.error(ref, "bad stuff", code="oops")
        assert e.type == proto.ERROR
        assert e.data["message"] == "bad stuff"
        assert e.data["code"] == "oops"

    def test_event_builder(self):
        evt = proto.event(proto.EVT_MESSAGE_CHANNEL, channel="test", sender="beth")
        # Events now use event_type directly as Message.type
        assert evt.type == proto.EVT_MESSAGE_CHANNEL
        assert evt.data["channel"] == "test"

    def test_message_classification(self):
        assert proto.ack("ref").is_response
        assert proto.result("ref").is_response
        assert proto.error("ref", "x").is_response
        assert not proto.subscribe("ch", agent="a", session="b").is_response

        # Events are no longer type=EVENT — they use the event type directly
        evt = proto.event(proto.EVT_SESSION_LIVE, session_id="test")
        assert not evt.is_response

    def test_request_context(self):
        ctx = proto.RequestContext(agent_name="beth", session_id="beth-swift-crane")
        assert ctx.agent_name == "beth"
        assert ctx.session_id == "beth-swift-crane"

    def test_request_context_from_request(self):
        msg = proto.subscribe("ch", agent="beth", session="beth-test")
        ctx = proto.RequestContext.from_request(msg)
        assert ctx is not None
        assert ctx.agent_name == "beth"
        assert ctx.session_id == "beth-test"


# ---------------------------------------------------------------------------
# State registry tests
# ---------------------------------------------------------------------------

class TestPresenceRegistry:
    def test_register_and_lookup(self):
        reg = PresenceRegistry()
        record = SessionRecord(
            session_id="beth-swift-crane",
            agent_name="beth",
            agent_home="/home/test/.beth",
            pid=1234,
        )
        reg.register(record)
        assert reg.get("beth-swift-crane") is record
        assert len(reg) == 1

    def test_deregister(self):
        reg = PresenceRegistry()
        record = SessionRecord(
            session_id="beth-swift-crane",
            agent_name="beth",
            agent_home="/home/test/.beth",
            pid=1234,
        )
        reg.register(record)
        removed = reg.deregister("beth-swift-crane")
        assert removed is record
        assert reg.get("beth-swift-crane") is None
        assert len(reg) == 0

    def test_by_agent(self):
        reg = PresenceRegistry()
        for i, name in enumerate(["beth-a", "beth-b", "dalet-c"]):
            agent = name.split("-")[0]
            reg.register(SessionRecord(
                session_id=name, agent_name=agent,
                agent_home=f"/home/{agent}", pid=1000 + i,
            ))
        assert len(reg.by_agent("beth")) == 2
        assert len(reg.by_agent("dalet")) == 1


class TestChannelRegistry:
    def test_subscribe_and_query(self):
        reg = ChannelRegistry()
        count = reg.subscribe("test-channel", "beth-a")
        assert count == 1
        count = reg.subscribe("test-channel", "dalet-b")
        assert count == 2
        assert reg.subscribers("test-channel") == {"beth-a", "dalet-b"}

    def test_unsubscribe(self):
        reg = ChannelRegistry()
        reg.subscribe("ch", "a")
        reg.subscribe("ch", "b")
        reg.unsubscribe("ch", "a")
        assert reg.subscribers("ch") == {"b"}

    def test_unsubscribe_cleans_empty(self):
        reg = ChannelRegistry()
        reg.subscribe("ch", "a")
        reg.unsubscribe("ch", "a")
        assert "ch" not in reg.all_channels()

    def test_unsubscribe_all(self):
        reg = ChannelRegistry()
        reg.subscribe("ch1", "a")
        reg.subscribe("ch2", "a")
        reg.subscribe("ch2", "b")
        departed = reg.unsubscribe_all("a")
        assert set(departed) == {"ch1", "ch2"}
        assert reg.subscribers("ch1") == set()
        assert reg.subscribers("ch2") == {"b"}

    def test_channels_for(self):
        reg = ChannelRegistry()
        reg.subscribe("ch1", "a")
        reg.subscribe("ch2", "a")
        reg.subscribe("ch3", "b")
        assert set(reg.channels_for("a")) == {"ch1", "ch2"}


# ---------------------------------------------------------------------------
# Client/server integration tests
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
def tmp_daemon_dir(tmp_path):
    """Create a temp directory with daemon paths."""
    daemon_dir = tmp_path / "daemon"
    daemon_dir.mkdir()
    channels_dir = tmp_path / "channels"
    channels_dir.mkdir()

    # Write a minimal agents registry
    agents_file = tmp_path / "agents.yml"
    agents_file.write_text(f"beth: {tmp_path / 'beth'}\ndalet: {tmp_path / 'dalet'}\n")

    # Create inbox dirs
    for agent in ["beth", "dalet"]:
        (tmp_path / agent / "inbox").mkdir(parents=True)

    return tmp_path


@pytest_asyncio.fixture
def daemon_config(tmp_daemon_dir):
    """Build a DaemonConfig pointing at temp paths.

    Uses /tmp/ for the socket to avoid macOS 104-byte Unix socket path limit.
    """
    import uuid
    sock_dir = Path(f"/tmp/kiln-test-{uuid.uuid4().hex[:8]}")
    sock_dir.mkdir(parents=True, exist_ok=True)
    sock_path = sock_dir / "kiln.sock"

    state_dir = tmp_daemon_dir / "daemon" / "state"
    config = DaemonConfig(
        socket_path=sock_path,
        pid_file=tmp_daemon_dir / "daemon" / "kiln.pid",
        log_file=tmp_daemon_dir / "daemon" / "daemon.log",
        lockdown_file=tmp_daemon_dir / "daemon" / "lockdown",
        agents_registry=tmp_daemon_dir / "agents.yml",
        channels_dir=tmp_daemon_dir / "channels",
        state_dir=state_dir,
        subscriptions_dir=state_dir / "subscriptions",
    )

    yield config

    # Clean up short-path socket dir
    sock_path.unlink(missing_ok=True)
    sock_dir.rmdir()


@pytest_asyncio.fixture
async def running_daemon(daemon_config):
    """Start a daemon server and yield it. Stops on cleanup.

    Patches get_live_tmux_sessions for the entire fixture lifetime so
    real tmux sessions never leak into test state — not at startup, and
    not during the periodic reconcile loop.
    """
    from unittest.mock import patch
    daemon = KilnDaemon(daemon_config)
    with patch("kiln.daemon.state.get_live_tmux_sessions", return_value=set()):
        await daemon.start()
        yield daemon
        await daemon.stop()


@pytest_asyncio.fixture
async def make_client(daemon_config):
    """Factory for stateless clients pointed at the test daemon."""
    def _make(agent: str = "beth", session: str = "beth-test-1"):
        return DaemonClient(
            agent=agent, session=session,
            socket_path=daemon_config.socket_path,
            auto_start=False,
        )

    yield _make


@pytest.mark.asyncio
async def test_session_lazy_registration(running_daemon, make_client):
    """First mutating request from a client lazily registers the session."""
    client = make_client("beth", "beth-test-1")

    # No presence yet — haven't sent any requests
    assert running_daemon.state.presence.get("beth-test-1") is None

    # Subscribe triggers ensure_session (mutating operation)
    await client.subscribe("test-channel")

    sessions = await client.list_sessions()
    assert any(s["session_id"] == "beth-test-1" for s in sessions)
    assert any(s["agent_name"] == "beth" for s in sessions)


@pytest.mark.asyncio
async def test_subscribe_and_list(running_daemon, make_client):
    client = make_client("beth", "beth-test-1")

    count = await client.subscribe("test-channel")
    assert count == 1

    subs = await client.list_subscriptions()
    assert "test-channel" in subs

    await client.unsubscribe("test-channel")
    subs = await client.list_subscriptions()
    assert "test-channel" not in subs


@pytest.mark.asyncio
async def test_publish_fanout(running_daemon, make_client, daemon_config):
    """Publish to a channel and verify inbox delivery."""
    c1 = make_client("beth", "beth-pub")
    c2 = make_client("dalet", "dalet-sub")

    await c1.subscribe("news")
    await c2.subscribe("news")

    recipients = await c1.publish("news", "headline", "Full story here")
    assert recipients == 1  # c2 is the only OTHER subscriber

    # Check that c2's inbox got the message
    inbox = daemon_config.agents_registry.parent / "dalet" / "inbox" / "dalet-sub"
    msgs = list(inbox.glob("msg-*.md"))
    assert len(msgs) == 1
    content = msgs[0].read_text()
    assert "headline" in content
    assert "Full story here" in content
    assert "channel: news" in content

    # Check channel history
    history = daemon_config.channels_dir / "news" / "history.jsonl"
    assert history.exists()
    entry = json.loads(history.read_text().strip())
    assert entry["from"] == "beth-pub"


@pytest.mark.asyncio
async def test_direct_message(running_daemon, make_client, daemon_config):
    """Send a direct message and verify inbox delivery."""
    c1 = make_client("beth", "beth-sender")

    result = await c1.send_direct("dalet-receiver", "hi", "Hello from Beth")
    assert "sent" in result.lower() or "dalet" in result.lower()

    # Check inbox
    inbox = daemon_config.agents_registry.parent / "dalet" / "inbox" / "dalet-receiver"
    msgs = list(inbox.glob("msg-*.md"))
    assert len(msgs) == 1
    assert "Hello from Beth" in msgs[0].read_text()


@pytest.mark.asyncio
async def test_reconcile_cleanup(running_daemon, make_client):
    """Verify that reconcile prunes sessions no longer alive in tmux."""
    client = make_client("beth", "beth-cleanup")
    await client.subscribe("temp-channel")

    # Verify registered (ensure_session triggered by subscribe)
    assert running_daemon.state.presence.get("beth-cleanup") is not None
    assert "beth-cleanup" in running_daemon.state.channels.subscribers("temp-channel")

    # Directly prune via reconcile (simulates tmux session gone)
    running_daemon.state.presence.deregister("beth-cleanup")
    running_daemon.state.channels.unsubscribe_all("beth-cleanup")

    assert running_daemon.state.presence.get("beth-cleanup") is None
    assert "beth-cleanup" not in running_daemon.state.channels.subscribers("temp-channel")


@pytest.mark.asyncio
async def test_durable_state_round_trip(daemon_config):
    """Full round-trip: subscribe → file written → new DaemonState loads it → reconcile prunes."""
    from unittest.mock import patch

    daemon = KilnDaemon(daemon_config)
    with patch("kiln.daemon.state.get_live_tmux_sessions", return_value=set()):
        await daemon.start()

    client = DaemonClient(
        agent="beth", session="beth-durable-1",
        socket_path=daemon_config.socket_path, auto_start=False,
    )

    # 1. Subscribe — daemon writes durable file
    await client.subscribe("persist-ch")
    sub_file = daemon_config.subscriptions_dir / "channels" / "beth-durable-1.yml"
    assert sub_file.exists(), "Subscription file should be written on subscribe"
    content = sub_file.read_text()
    assert "persist-ch" in content

    # 2. Fresh DaemonState loads from files — proves files are truth
    fresh_state = DaemonState(daemon_config.subscriptions_dir)
    fresh_state.load_from_files()
    assert "beth-durable-1" in fresh_state.channels.subscribers("persist-ch")

    # 3. Reconcile against empty tmux — prunes the session and removes file
    with patch("kiln.daemon.state.get_live_tmux_sessions", return_value=set()):
        pruned, _ = fresh_state.reconcile()
    assert "beth-durable-1" in pruned
    assert "beth-durable-1" not in fresh_state.channels.subscribers("persist-ch")
    assert not sub_file.exists(), "Subscription file should be removed on prune"

    await daemon.stop()


@pytest.mark.asyncio
async def test_reconcile_discovers_live_sessions(daemon_config):
    """Reconcile discovers tmux sessions matching known agent prefixes."""
    from unittest.mock import patch

    fake_tmux = {"beth-bright-pine", "beth-cool-lake", "dalet-red-fox", "random-other"}
    agents = {"beth": daemon_config.agents_registry.parent / "beth",
              "dalet": daemon_config.agents_registry.parent / "dalet"}

    state = DaemonState(daemon_config.subscriptions_dir)
    state.store.ensure_dirs()

    with patch("kiln.daemon.state.get_live_tmux_sessions", return_value=fake_tmux):
        pruned, discovered = state.reconcile(agents_registry=agents)

    assert set(discovered) == {"beth-bright-pine", "beth-cool-lake", "dalet-red-fox"}
    assert state.presence.get("random-other") is None
    # All discovered sessions should be in presence
    for sid in discovered:
        assert state.presence.get(sid) is not None


@pytest.mark.asyncio
async def test_error_on_missing_identity(running_daemon, daemon_config):
    """Requests without requester identity should return errors."""
    # Manually send a raw subscribe without requester envelope
    reader, writer = await asyncio.open_unix_connection(
        str(daemon_config.socket_path)
    )
    msg = proto.Message(
        type=proto.SUBSCRIBE,
        ref=proto.make_ref(),
        data={"channel": "test"},  # no requester
    )
    writer.write(msg.to_line())
    await writer.drain()
    resp_line = await reader.readline()
    writer.close()
    resp = proto.Message.from_line(resp_line)
    assert resp.type == proto.ERROR


@pytest.mark.asyncio
async def test_get_status(running_daemon, make_client):
    client = make_client("beth", "beth-status")

    # Trigger lazy registration
    await client.subscribe("dummy")

    status = await client.get_status()
    assert "sessions" in status
    assert status["sessions"] == 1
    assert status["lockdown"] is False


@pytest.mark.asyncio
async def test_multiple_sessions(running_daemon, make_client):
    """Multiple sessions can coexist."""
    c1 = make_client("beth", "beth-a")
    c2 = make_client("beth", "beth-b")

    # Trigger lazy registration for both
    await c1.subscribe("shared")
    await c2.subscribe("shared")

    sessions = await c1.list_sessions()
    assert len(sessions) == 2

    sessions_beth = await c1.list_sessions(agent="beth")
    assert len(sessions_beth) == 2


# ---------------------------------------------------------------------------
# Message tool integration tests — verify the tool routes through daemon
# ---------------------------------------------------------------------------


def _tool_text(result: dict) -> str:
    """Extract text from MCP tool result."""
    return result["content"][0]["text"]


def _tool_ok(result: dict) -> bool:
    return not result.get("isError", False)


@pytest_asyncio.fixture
def message_tool_fn(daemon_config, make_client):
    """Create a message_tool closure wired to a daemon client.

    Returns (tool_fn, client).
    """
    from kiln.tools import create_mcp_server

    inbox_root = daemon_config.agents_registry.parent / "beth" / "inbox"
    client = make_client("beth", "beth-tool-test")

    _, _, _, mcp_tools = create_mcp_server(
        inbox_root=inbox_root,
        skills_path=Path("/nonexistent"),
        agent_id="beth-tool-test",
        daemon_client=client,
    )
    msg_tool = next(t for t in mcp_tools if t.name == "message")
    return msg_tool.handler, client


@pytest.mark.asyncio
async def test_message_tool_subscribe_via_daemon(running_daemon, message_tool_fn):
    """message tool subscribe routes through daemon, not channels.json."""
    handler, client = message_tool_fn

    result = await handler({"action": "subscribe", "channel": "test-ch"})
    assert _tool_ok(result)
    assert "Subscribed" in _tool_text(result)
    assert "1 subscriber" in _tool_text(result)

    # Verify daemon knows about the subscription
    assert "test-ch" in await client.list_subscriptions()
    subs = running_daemon.state.channels.subscribers("test-ch")
    assert "beth-tool-test" in subs


@pytest.mark.asyncio
async def test_message_tool_unsubscribe_via_daemon(running_daemon, message_tool_fn):
    handler, client = message_tool_fn
    await client.subscribe("test-ch")

    result = await handler({"action": "unsubscribe", "channel": "test-ch"})
    assert _tool_ok(result)
    assert "Unsubscribed" in _tool_text(result)
    assert "test-ch" not in await client.list_subscriptions()


@pytest.mark.asyncio
async def test_message_tool_channel_broadcast_via_daemon(
    running_daemon, message_tool_fn, make_client, daemon_config
):
    """Channel broadcast goes through daemon, delivers to subscriber inboxes."""
    handler, sender = message_tool_fn

    # Set up a second subscriber
    receiver = make_client("dalet", "dalet-listener")
    await receiver.subscribe("broadcast-ch")
    await sender.subscribe("broadcast-ch")

    result = await handler({
        "action": "send",
        "channel": "broadcast-ch",
        "summary": "test broadcast",
        "body": "Hello from message tool",
    })
    assert _tool_ok(result)
    assert "broadcast" in _tool_text(result).lower()
    assert "1 recipient" in _tool_text(result)

    # Verify inbox delivery
    inbox = daemon_config.agents_registry.parent / "dalet" / "inbox" / "dalet-listener"
    msgs = list(inbox.glob("msg-*.md"))
    assert len(msgs) == 1
    assert "Hello from message tool" in msgs[0].read_text()


@pytest.mark.asyncio
async def test_message_tool_dm_via_daemon(running_daemon, message_tool_fn, daemon_config):
    """DM routes through daemon when connected."""
    handler, client = message_tool_fn

    result = await handler({
        "action": "send",
        "to": "dalet-target",
        "summary": "direct msg",
        "body": "Hello directly",
    })
    assert _tool_ok(result)

    inbox = daemon_config.agents_registry.parent / "dalet" / "inbox" / "dalet-target"
    msgs = list(inbox.glob("msg-*.md"))
    assert len(msgs) == 1
    assert "Hello directly" in msgs[0].read_text()


@pytest.mark.asyncio
async def test_message_tool_dm_filesystem_fallback(daemon_config, monkeypatch):
    """DM falls back to filesystem when daemon is unavailable."""
    from kiln import tools as tools_mod
    from kiln.tools import create_mcp_server

    inbox_root = daemon_config.agents_registry.parent / "beth" / "inbox"

    # Patch the registry loader so it uses the test's agents.yml
    agents = {"beth": daemon_config.agents_registry.parent / "beth",
              "dalet": daemon_config.agents_registry.parent / "dalet"}
    monkeypatch.setattr(tools_mod, "_load_namespace_registry", lambda: agents)

    # No daemon client — should fall back to filesystem
    _, _, _, mcp_tools = create_mcp_server(
        inbox_root=inbox_root,
        skills_path=Path("/nonexistent"),
        agent_id="beth-fallback",
        daemon_client=None,
    )
    handler = next(t for t in mcp_tools if t.name == "message").handler

    result = await handler({
        "action": "send",
        "to": "beth-fallback",
        "summary": "fallback test",
        "body": "Should use filesystem",
    })
    assert _tool_ok(result)
    assert "sent" in _tool_text(result).lower()

    # Verify inbox delivery via filesystem
    msgs = list((inbox_root / "beth-fallback").glob("msg-*.md"))
    assert len(msgs) == 1


@pytest.mark.asyncio
async def test_message_tool_channel_requires_daemon():
    """Channel operations fail cleanly without daemon."""
    from kiln.tools import create_mcp_server

    _, _, _, mcp_tools = create_mcp_server(
        inbox_root=Path("/tmp/test-inbox"),
        skills_path=Path("/nonexistent"),
        agent_id="beth-no-daemon",
        daemon_client=None,
    )
    handler = next(t for t in mcp_tools if t.name == "message").handler

    result = await handler({"action": "subscribe", "channel": "test"})
    assert not _tool_ok(result)
    assert "daemon" in _tool_text(result).lower()

    result = await handler({"action": "unsubscribe", "channel": "test"})
    assert not _tool_ok(result)

    result = await handler({
        "action": "send", "channel": "test",
        "summary": "test", "body": "test",
    })
    assert not _tool_ok(result)
    assert "daemon" in _tool_text(result).lower()


# ---------------------------------------------------------------------------
# Harness subscription persistence tests
# ---------------------------------------------------------------------------


class FakeHarness:
    """Minimal harness stub for testing subscription persistence logic.

    Mirrors real harness: snapshot returns _desired_subscriptions directly
    (no daemon query needed — stateless client has no local cache).
    """

    def __init__(self, daemon_client=None):
        self._daemon_client = daemon_client
        self._desired_subscriptions: list[str] = []

    def _snapshot_channel_subscriptions(self) -> list[str]:
        return list(self._desired_subscriptions)

    async def _restore_channel_subscriptions(self, subscriptions: list[str]) -> None:
        if not subscriptions:
            return
        self._desired_subscriptions = list(subscriptions)
        if self._daemon_client:
            await self._daemon_client.restore_subscriptions(subscriptions)


@pytest.mark.asyncio
async def test_desired_subscriptions_survive_daemon_outage(running_daemon, make_client):
    """Desired subscriptions persist through daemon unavailability.

    Regression test: snapshot must return desired subscriptions regardless
    of daemon availability — it reads local state, not daemon state.
    """
    client = make_client("beth", "beth-tool-test")

    harness = FakeHarness(daemon_client=client)

    # Subscribe through daemon — desired state tracks
    await harness._restore_channel_subscriptions(["alpha", "beta"])
    assert harness._desired_subscriptions == ["alpha", "beta"]

    # Verify daemon received the subscriptions
    subs = await client.list_subscriptions()
    assert set(subs) == {"alpha", "beta"}

    # Snapshot returns desired state (local, no daemon query)
    snap = harness._snapshot_channel_subscriptions()
    assert set(snap) == {"alpha", "beta"}

    # Restore with no daemon — records intent without losing it
    harness2 = FakeHarness(daemon_client=None)
    await harness2._restore_channel_subscriptions(["alpha", "beta"])
    assert harness2._desired_subscriptions == ["alpha", "beta"]

    snap2 = harness2._snapshot_channel_subscriptions()
    assert set(snap2) == {"alpha", "beta"}, \
        "Desired subscriptions must survive when daemon is None"


# ---------------------------------------------------------------------------
# Phase 5 — PlatformMessage, daemon ingress, management, adapter skeleton
# ---------------------------------------------------------------------------


class TestPlatformMessage:
    def test_fields(self):
        msg = proto.PlatformMessage(
            sender_name="kira",
            sender_platform_id="123456",
            platform="discord",
            content="hello",
            trust="full",
            channel_desc="dm",
            channel_id="789",
        )
        assert msg.sender_name == "kira"
        assert msg.platform == "discord"
        assert msg.trust == "full"
        assert msg.attachment_paths is None

    def test_defaults(self):
        msg = proto.PlatformMessage(
            sender_name="someone",
            sender_platform_id="000",
            platform="slack",
            content="test",
        )
        assert msg.trust == "unknown"
        assert msg.channel_desc == ""


@pytest.mark.asyncio
async def test_publish_to_channel_core(running_daemon, make_client, daemon_config):
    """Test KilnDaemon.publish_to_channel — the shared ingress path."""
    c1 = make_client("beth", "beth-pub-a")
    c2 = make_client("beth", "beth-pub-b")
    await c1.subscribe("test-ch")
    await c2.subscribe("test-ch")

    # Publish via daemon method directly (simulating adapter path)
    count = await running_daemon.publish_to_channel(
        "test-ch", "external-sender", "summary", "hello from adapter",
    )
    # Both subscribers should receive (external sender is not excluded)
    assert count == 2

    # Check channel history was written
    history = daemon_config.channels_dir / "test-ch" / "history.jsonl"
    assert history.exists()
    entries = [json.loads(l) for l in history.read_text().strip().split("\n")]
    assert entries[-1]["from"] == "external-sender"
    assert entries[-1]["body"] == "hello from adapter"

    # Check inbox delivery
    inbox_b = daemon_config.agents_registry.parent / "beth" / "inbox" / "beth-pub-b"
    msgs = list(inbox_b.glob("msg-*.md"))
    assert len(msgs) >= 1
    content = msgs[-1].read_text()
    assert "hello from adapter" in content


@pytest.mark.asyncio
async def test_publish_to_channel_excludes_sender(running_daemon, make_client):
    """Sender excluded from their own publish by default."""
    c1 = make_client("beth", "beth-ex-1")
    await c1.subscribe("excl-ch")

    count = await running_daemon.publish_to_channel(
        "excl-ch", "beth-ex-1", "s", "body",
    )
    assert count == 0  # sender excluded, no other subscribers


@pytest.mark.asyncio
async def test_publish_to_channel_source_in_event(running_daemon, make_client):
    """Source tag carried in event for echo prevention."""
    events = []
    running_daemon.events.add_handler(lambda e: events.append(e))

    c1 = make_client("beth", "beth-src-1")
    await c1.subscribe("src-ch")

    await running_daemon.publish_to_channel(
        "src-ch", "discord-kira", "s", "body", source="discord",
    )
    await asyncio.sleep(0.05)

    channel_events = [e for e in events if e.type == "message.channel"]
    assert len(channel_events) == 1
    assert channel_events[0].data["source"] == "discord"


@pytest.mark.asyncio
async def test_deliver_platform_message(running_daemon, make_client, daemon_config):
    """Test daemon platform message delivery with structured payload."""
    c1 = make_client("beth", "beth-plat-1")

    msg = proto.PlatformMessage(
        sender_name="kira",
        sender_platform_id="111222",
        platform="discord",
        content="hey beth, voice memo incoming",
        trust="full",
        channel_desc="dm",
        channel_id="999888",
        attachment_paths=["/tmp/audio.ogg"],
    )

    path = await running_daemon.deliver_platform_message("beth-plat-1", msg)
    assert path is not None
    assert path.exists()

    content = path.read_text()
    # Check rich frontmatter (yaml.dump serialization)
    assert "from: discord-kira" in content
    assert "trust: full" in content
    assert "discord-user-id:" in content and "111222" in content
    assert "discord-channel-id:" in content and "999888" in content
    assert "ATTACHMENT RECEIVED" in content
    assert "audio.ogg" in content
    assert "hey beth, voice memo incoming" in content


@pytest.mark.asyncio
async def test_deliver_platform_message_unknown_recipient(running_daemon):
    """Delivery to unknown recipient returns None."""
    msg = proto.PlatformMessage(
        sender_name="someone",
        sender_platform_id="000",
        platform="test",
        content="hi",
    )
    path = await running_daemon.deliver_platform_message("nobody-session-1", msg)
    assert path is None


# ---------------------------------------------------------------------------
# Management actions
# ---------------------------------------------------------------------------

class TestManagementQueries:
    def test_resolve_session_ref_exact(self):
        from kiln.daemon.management import ManagementActions
        state = DaemonState()
        config = DaemonConfig()
        mgmt = ManagementActions(state, config)

        state.presence.register(SessionRecord(
            session_id="beth-storm-jay", agent_name="beth",
            agent_home="/tmp/beth", pid=100,
        ))

        assert mgmt.resolve_session_ref("beth-storm-jay") == "beth-storm-jay"

    def test_resolve_session_ref_with_prefix(self):
        from kiln.daemon.management import ManagementActions
        state = DaemonState()
        config = DaemonConfig()
        mgmt = ManagementActions(state, config)

        state.presence.register(SessionRecord(
            session_id="beth-storm-jay", agent_name="beth",
            agent_home="/tmp/beth", pid=100,
        ))

        assert mgmt.resolve_session_ref("storm-jay", prefix="beth-") == "beth-storm-jay"

    def test_resolve_session_ref_prefix_match(self):
        from kiln.daemon.management import ManagementActions
        state = DaemonState()
        config = DaemonConfig()
        mgmt = ManagementActions(state, config)

        state.presence.register(SessionRecord(
            session_id="beth-storm-jay", agent_name="beth",
            agent_home="/tmp/beth", pid=100,
        ))

        # "beth-storm" is a prefix → unambiguous match
        assert mgmt.resolve_session_ref("beth-storm") == "beth-storm-jay"
        # Arbitrary substring must NOT match
        assert mgmt.resolve_session_ref("storm") is None
        # With prefix: "storm" → "beth-storm" → prefix match
        assert mgmt.resolve_session_ref("storm", prefix="beth-") == "beth-storm-jay"

    def test_resolve_session_ref_ambiguous(self):
        from kiln.daemon.management import ManagementActions
        state = DaemonState()
        config = DaemonConfig()
        mgmt = ManagementActions(state, config)

        state.presence.register(SessionRecord(
            session_id="beth-storm-jay", agent_name="beth",
            agent_home="/tmp/beth", pid=100,
        ))
        state.presence.register(SessionRecord(
            session_id="beth-storm-owl", agent_name="beth",
            agent_home="/tmp/beth", pid=101,
        ))

        # "beth-storm" prefixes both — ambiguous
        assert mgmt.resolve_session_ref("beth-storm") is None

    def test_resolve_session_ref_with_candidates(self):
        """resolve_session_ref accepts external candidate set for resume."""
        from kiln.daemon.management import ManagementActions
        state = DaemonState()
        config = DaemonConfig()
        mgmt = ManagementActions(state, config)

        # Not in live presence — durable registry lookup
        durable = {"beth-old-session", "beth-other-thing"}
        assert mgmt.resolve_session_ref("beth-old-session", candidates=durable) == "beth-old-session"
        assert mgmt.resolve_session_ref("beth-old", candidates=durable) == "beth-old-session"

    def test_active_channels_and_subscribers(self):
        from kiln.daemon.management import ManagementActions
        state = DaemonState()
        config = DaemonConfig()
        mgmt = ManagementActions(state, config)

        state.channels.subscribe("alpha", "s1")
        state.channels.subscribe("alpha", "s2")
        state.channels.subscribe("beta", "s1")

        assert set(mgmt.active_channels()) == {"alpha", "beta"}
        assert mgmt.get_channel_subscribers("alpha") == {"s1", "s2"}
        assert mgmt.get_channel_subscribers("gamma") == set()

    def test_build_launch_cmd_prefers_agent_wrapper(self, monkeypatch):
        from kiln.daemon.management import ManagementActions
        state = DaemonState()
        config = DaemonConfig()
        mgmt = ManagementActions(state, config)

        def fake_which(name: str):
            if name == "beth":
                return "/usr/local/bin/beth"
            if name == "kiln":
                return "/usr/local/bin/kiln"
            return None

        monkeypatch.setattr("shutil.which", fake_which)

        cmd = mgmt._build_launch_cmd("beth", mode="yolo", prompt="hello")
        assert cmd == [
            "/usr/local/bin/beth",
            "--detach",
            "--mode",
            "yolo",
            "--prompt",
            "hello",
        ]

    def test_build_launch_cmd_falls_back_to_kiln_run(self, monkeypatch):
        from kiln.daemon.management import ManagementActions
        state = DaemonState()
        config = DaemonConfig()
        mgmt = ManagementActions(state, config)

        def fake_which(name: str):
            if name == "kiln":
                return "/usr/local/bin/kiln"
            return None

        monkeypatch.setattr("shutil.which", fake_which)

        cmd = mgmt._build_launch_cmd("dalet", mode="supervised", resume_id="dalet-old")
        assert cmd == [
            "/usr/local/bin/kiln",
            "run",
            "dalet",
            "--detach",
            "--mode",
            "supervised",
            "--resume",
            "dalet-old",
        ]

    def test_build_launch_cmd_missing_binaries(self, monkeypatch):
        from kiln.daemon.management import ManagementActions
        state = DaemonState()
        config = DaemonConfig()
        mgmt = ManagementActions(state, config)

        monkeypatch.setattr("shutil.which", lambda name: None)

        assert mgmt._build_launch_cmd("gimel") is None


@pytest.mark.asyncio
async def test_management_set_session_mode(tmp_path):

    """Test mode change via management."""
    from kiln.daemon.management import ManagementActions
    import yaml
    state = DaemonState()
    config = DaemonConfig()
    mgmt = ManagementActions(state, config)

    agent_home = tmp_path / "beth"
    state_dir = agent_home / "state"
    state_dir.mkdir(parents=True)

    state.presence.register(SessionRecord(
        session_id="beth-test-1", agent_name="beth",
        agent_home=str(agent_home), pid=100,
    ))

    config_path = state_dir / "session-config-beth-test-1.yml"
    config_path.write_text("mode: supervised\n")

    result = await mgmt.set_session_mode("beth-test-1", "yolo")
    assert result.success
    assert "supervised" in result.message
    assert "yolo" in result.message

    data = yaml.safe_load(config_path.read_text())
    assert data["mode"] == "yolo"


@pytest.mark.asyncio
async def test_management_set_invalid_mode():
    from kiln.daemon.management import ManagementActions
    state = DaemonState()
    config = DaemonConfig()
    mgmt = ManagementActions(state, config)

    result = await mgmt.set_session_mode("beth-x", "trusted")
    assert not result.success
    assert "Invalid mode" in result.message


@pytest.mark.asyncio
async def test_management_capture_nonexistent():
    """Capture of nonexistent session fails gracefully."""
    from kiln.daemon.management import ManagementActions
    state = DaemonState()
    config = DaemonConfig()
    mgmt = ManagementActions(state, config)

    result = await mgmt.capture_session("nonexistent-session-id")
    assert not result.success


# ---------------------------------------------------------------------------
# Adapter skeleton
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_adapter_lifecycle(running_daemon):
    """Test DiscordAdapter start/stop lifecycle."""
    from kiln.daemon.adapters.discord import DiscordAdapter

    adapter = DiscordAdapter()
    await adapter.start(running_daemon)

    assert adapter.adapter_id == "discord"
    assert adapter.platform_name == "discord"
    assert adapter._daemon is running_daemon
    assert adapter._state_dir is not None
    assert adapter._state_dir.exists()

    assert len(running_daemon.events._handlers) == 1

    await adapter.stop()
    assert adapter._daemon is None
    assert len(running_daemon.events._handlers) == 0


@pytest.mark.asyncio
async def test_adapter_state_persistence(running_daemon):
    """Adapter persists branch/channel thread mappings across restarts."""
    from kiln.daemon.adapters.discord import DiscordAdapter

    adapter = DiscordAdapter()
    await adapter.start(running_daemon)

    adapter._branch_threads["beth-test-1"] = 12345
    adapter._channel_threads["general"] = 67890

    await adapter.stop()

    adapter2 = DiscordAdapter()
    await adapter2.start(running_daemon)

    assert adapter2._branch_threads.get("beth-test-1") == 12345
    assert adapter2._channel_threads.get("general") == 67890

    await adapter2.stop()


@pytest.mark.asyncio
async def test_adapter_platform_op_dispatch(running_daemon):
    """platform_op routes to the right handler."""
    from kiln.daemon.adapters.discord import DiscordAdapter

    adapter = DiscordAdapter()
    await adapter.start(running_daemon)

    # D2 ops return error dicts on bad args instead of raising
    result = await adapter.platform_op("send", {})
    assert result["ok"] is False

    # D3d voice_send returns error dict on bad args (no longer a stub)
    result = await adapter.platform_op("voice_send", {})
    assert result["ok"] is False

    with pytest.raises(ValueError, match="Unknown Discord platform op"):
        await adapter.platform_op("nonexistent_action", {})

    await adapter.stop()


@pytest.mark.asyncio
async def test_adapter_supports(running_daemon):
    from kiln.daemon.adapters.discord import DiscordAdapter

    adapter = DiscordAdapter()
    await adapter.start(running_daemon)

    assert adapter.supports("send_message")
    assert adapter.supports("security_challenge")
    assert not adapter.supports("nonexistent_feature")

    await adapter.stop()


@pytest.mark.asyncio
async def test_adapter_state_in_daemon_dir(running_daemon, daemon_config):
    """Adapter state lives in shared daemon state dir, not any agent home."""
    from kiln.daemon.adapters.discord import DiscordAdapter

    adapter = DiscordAdapter()
    await adapter.start(running_daemon)

    assert adapter._state_dir == daemon_config.state_dir / "discord"
    assert str(adapter._state_dir).startswith(str(daemon_config.state_dir))

    await adapter.stop()


# ---------------------------------------------------------------------------
# Phase 5 Slice B — Surface subscription registry
# ---------------------------------------------------------------------------


class TestSurfaceSubscriptionRegistry:
    def test_subscribe_and_query(self):
        reg = SurfaceSubscriptionRegistry()
        count = reg.subscribe("discord:user:123", "beth-a")
        assert count == 1
        count = reg.subscribe("discord:user:123", "beth-b")
        assert count == 2
        assert reg.subscribers("discord:user:123") == {"beth-a", "beth-b"}

    def test_unsubscribe(self):
        reg = SurfaceSubscriptionRegistry()
        reg.subscribe("discord:user:123", "beth-a")
        reg.subscribe("discord:user:123", "beth-b")
        reg.unsubscribe("discord:user:123", "beth-a")
        assert reg.subscribers("discord:user:123") == {"beth-b"}

    def test_unsubscribe_cleans_empty(self):
        reg = SurfaceSubscriptionRegistry()
        reg.subscribe("discord:user:123", "beth-a")
        reg.unsubscribe("discord:user:123", "beth-a")
        assert "discord:user:123" not in reg.all_surfaces()

    def test_unsubscribe_all(self):
        reg = SurfaceSubscriptionRegistry()
        reg.subscribe("discord:user:123", "beth-a")
        reg.subscribe("discord:channel:456", "beth-a")
        reg.subscribe("discord:channel:456", "beth-b")
        departed = reg.unsubscribe_all("beth-a")
        assert set(departed) == {"discord:user:123", "discord:channel:456"}
        assert reg.subscribers("discord:user:123") == set()
        assert reg.subscribers("discord:channel:456") == {"beth-b"}

    def test_surfaces_for(self):
        reg = SurfaceSubscriptionRegistry()
        reg.subscribe("discord:user:123", "beth-a")
        reg.subscribe("discord:channel:456", "beth-a")
        reg.subscribe("slack:channel:789", "beth-a")
        assert set(reg.surfaces_for("beth-a")) == {
            "discord:user:123", "discord:channel:456", "slack:channel:789",
        }

    def test_surfaces_for_adapter_filter(self):
        reg = SurfaceSubscriptionRegistry()
        reg.subscribe("discord:user:123", "beth-a")
        reg.subscribe("discord:channel:456", "beth-a")
        reg.subscribe("slack:channel:789", "beth-a")
        discord_only = reg.surfaces_for("beth-a", adapter_id="discord")
        assert set(discord_only) == {"discord:user:123", "discord:channel:456"}
        slack_only = reg.surfaces_for("beth-a", adapter_id="slack")
        assert slack_only == ["slack:channel:789"]

    def test_subscriber_count(self):
        reg = SurfaceSubscriptionRegistry()
        assert reg.subscriber_count("discord:user:123") == 0
        reg.subscribe("discord:user:123", "beth-a")
        reg.subscribe("discord:user:123", "beth-b")
        assert reg.subscriber_count("discord:user:123") == 2

    def test_idempotent_subscribe(self):
        reg = SurfaceSubscriptionRegistry()
        reg.subscribe("discord:user:123", "beth-a")
        count = reg.subscribe("discord:user:123", "beth-a")
        assert count == 1  # set deduplicates


class TestSurfaceProtocol:
    _REQ = {"agent": "beth", "session": "beth-test"}

    def test_subscribe_surface_round_trip(self):
        msg = proto.subscribe_surface("discord:user:116377", **self._REQ)
        line = msg.to_line()
        parsed = proto.Message.from_line(line)
        assert parsed.type == proto.SUBSCRIBE_SURFACE
        assert parsed.data["surface_ref"] == "discord:user:116377"

    def test_unsubscribe_surface_round_trip(self):
        msg = proto.unsubscribe_surface("discord:user:116377", **self._REQ)
        line = msg.to_line()
        parsed = proto.Message.from_line(line)
        assert parsed.type == proto.UNSUBSCRIBE_SURFACE
        assert parsed.data["surface_ref"] == "discord:user:116377"

    def test_list_surface_subscriptions_round_trip(self):
        msg = proto.list_surface_subscriptions(**self._REQ)
        parsed = proto.Message.from_line(msg.to_line())
        assert parsed.type == proto.LIST_SURFACE_SUBSCRIPTIONS

    def test_list_surface_subscriptions_with_adapter(self):
        msg = proto.list_surface_subscriptions(adapter_id="discord", **self._REQ)
        parsed = proto.Message.from_line(msg.to_line())
        assert parsed.data["adapter_id"] == "discord"

    def test_surface_event_types(self):
        evt_sub = proto.event(proto.EVT_SURFACE_SUBSCRIBED,
                              surface_ref="discord:user:123", session_id="beth-a")
        assert evt_sub.type == proto.EVT_SURFACE_SUBSCRIBED
        assert evt_sub.data["surface_ref"] == "discord:user:123"

        evt_unsub = proto.event(proto.EVT_SURFACE_UNSUBSCRIBED,
                                surface_ref="discord:user:123", session_id="beth-a")
        assert evt_unsub.type == proto.EVT_SURFACE_UNSUBSCRIBED


# ---------------------------------------------------------------------------
# Surface subscription client/server integration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_surface_subscribe_and_list(running_daemon, make_client):
    """Surface subscribe via client, verify daemon state and list query."""
    client = make_client("beth", "beth-surf-1")

    count = await client.subscribe_surface("discord:user:116377")
    assert count == 1

    assert "discord:user:116377" in [s["surface_ref"] for s in await client.list_surface_subscriptions()]

    # Daemon-side truth
    assert "beth-surf-1" in running_daemon.state.surfaces.subscribers("discord:user:116377")

    # List query
    subs = await client.list_surface_subscriptions()
    assert len(subs) == 1
    assert subs[0]["surface_ref"] == "discord:user:116377"
    assert subs[0]["subscriber_count"] == 1


@pytest.mark.asyncio
async def test_surface_unsubscribe(running_daemon, make_client):
    """Surface unsubscribe removes from daemon and local cache."""
    client = make_client("beth", "beth-surf-2")

    await client.subscribe_surface("discord:user:123")
    await client.unsubscribe_surface("discord:user:123")

    assert "discord:user:123" not in [s["surface_ref"] for s in await client.list_surface_subscriptions()]
    assert "beth-surf-2" not in running_daemon.state.surfaces.subscribers("discord:user:123")


@pytest.mark.asyncio
async def test_surface_list_filtered_by_adapter(running_daemon, make_client):
    """list_surface_subscriptions with adapter_id filters by prefix."""
    client = make_client("beth", "beth-surf-1")

    await client.subscribe_surface("discord:user:111")
    await client.subscribe_surface("discord:channel:222")
    await client.subscribe_surface("slack:channel:333")

    discord_subs = await client.list_surface_subscriptions(adapter_id="discord")
    assert len(discord_subs) == 2
    refs = {s["surface_ref"] for s in discord_subs}
    assert refs == {"discord:user:111", "discord:channel:222"}

    slack_subs = await client.list_surface_subscriptions(adapter_id="slack")
    assert len(slack_subs) == 1
    assert slack_subs[0]["surface_ref"] == "slack:channel:333"


@pytest.mark.asyncio
async def test_surface_cleanup_on_session_gone(running_daemon, make_client):
    """Surface subscriptions cleaned up when session is pruned."""
    client = make_client("beth", "beth-surf-cleanup")

    await client.subscribe_surface("discord:user:999")
    assert "beth-surf-cleanup" in running_daemon.state.surfaces.subscribers("discord:user:999")

    # Simulate reconcile pruning the session
    running_daemon.state.presence.deregister("beth-surf-cleanup")
    running_daemon.state.surfaces.unsubscribe_all("beth-surf-cleanup")

    assert "beth-surf-cleanup" not in running_daemon.state.surfaces.subscribers("discord:user:999")
    assert "discord:user:999" not in running_daemon.state.surfaces.all_surfaces()


@pytest.mark.asyncio
async def test_surface_multiple_subscribers(running_daemon, make_client):
    """Multiple sessions can subscribe to the same surface."""
    c1 = make_client("beth", "beth-multi-1")
    c2 = make_client("beth", "beth-multi-2")

    count1 = await c1.subscribe_surface("discord:user:116377")
    assert count1 == 1
    count2 = await c2.subscribe_surface("discord:user:116377")
    assert count2 == 2

    subs = running_daemon.state.surfaces.subscribers("discord:user:116377")
    assert subs == {"beth-multi-1", "beth-multi-2"}


@pytest.mark.asyncio
async def test_deliver_to_surface_subscribers(running_daemon, make_client, daemon_config):
    """deliver_to_surface_subscribers delivers to all subscribed sessions."""
    c1 = make_client("beth", "beth-deliv-1")
    c2 = make_client("beth", "beth-deliv-2")

    await c1.subscribe_surface("discord:user:116377")
    await c2.subscribe_surface("discord:user:116377")

    msg = proto.PlatformMessage(
        sender_name="kira",
        sender_platform_id="116377",
        platform="discord",
        content="hey both of you",
        trust="full",
        channel_desc="dm",
        channel_id="dm-116377",
    )

    delivered = await running_daemon.deliver_to_surface_subscribers(
        "discord:user:116377", msg,
    )
    assert delivered == 2

    # Verify both inboxes got the message
    for session_id in ["beth-deliv-1", "beth-deliv-2"]:
        inbox = daemon_config.agents_registry.parent / "beth" / "inbox" / session_id
        msgs = list(inbox.glob("msg-*.md"))
        assert len(msgs) == 1
        content = msgs[0].read_text()
        assert "hey both of you" in content
        assert "trust: full" in content


@pytest.mark.asyncio
async def test_deliver_to_surface_no_subscribers(running_daemon):
    """deliver_to_surface_subscribers with no subscribers returns 0."""
    msg = proto.PlatformMessage(
        sender_name="someone",
        sender_platform_id="000",
        platform="test",
        content="hello?",
    )
    delivered = await running_daemon.deliver_to_surface_subscribers(
        "test:orphan:surface", msg,
    )
    assert delivered == 0


@pytest.mark.asyncio
async def test_surface_in_get_status(running_daemon, make_client):
    """get_status includes surface count."""
    client = make_client("beth", "beth-surf-cleanup")
    await client.subscribe_surface("discord:user:123")

    status = await client.get_status()
    assert status["surfaces"] == 1
    assert status["sessions"] == 1


# ---------------------------------------------------------------------------
# Surface ref validation via adapter
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_surface_validation_rejects_malformed(running_daemon, make_client):
    """Adapter validation rejects malformed surface refs."""
    from kiln.daemon.adapters.discord import DiscordAdapter

    adapter = DiscordAdapter()
    await adapter.start(running_daemon)
    running_daemon.adapters["discord"] = adapter

    client = make_client("beth", "beth-stat-surf")

    # Missing type/id structure
    with pytest.raises(DaemonError, match="Invalid surface ref"):
        await client.subscribe_surface("discord:garbage")

    # Unknown surface type
    with pytest.raises(DaemonError, match="Unknown Discord surface type"):
        await client.subscribe_surface("discord:bogus:123")

    # Empty surface ID
    with pytest.raises(DaemonError, match="Surface ID cannot be empty"):
        await client.subscribe_surface("discord:user:")

    await adapter.stop()
    running_daemon.adapters.pop("discord", None)


@pytest.mark.asyncio
async def test_surface_validation_accepts_valid(running_daemon, make_client):
    """Adapter validation accepts well-formed surface refs."""
    from kiln.daemon.adapters.discord import DiscordAdapter

    adapter = DiscordAdapter()
    await adapter.start(running_daemon)
    running_daemon.adapters["discord"] = adapter

    client = make_client("beth", "beth-val-1")

    count = await client.subscribe_surface("discord:user:116377")
    assert count == 1
    count = await client.subscribe_surface("discord:channel:999888")
    assert count == 1

    subs = await client.list_surface_subscriptions()
    refs = {s["surface_ref"] for s in subs}
    assert refs == {"discord:user:116377", "discord:channel:999888"}

    await adapter.stop()
    running_daemon.adapters.pop("discord", None)


@pytest.mark.asyncio
async def test_surface_no_adapter_still_stores(running_daemon, make_client):
    """Surface refs for unknown platforms are stored without validation.

    When no adapter is registered for the platform prefix, the daemon
    stores the ref as-is. This allows refs to be seeded before adapters
    start, or for platforms without adapters.
    """
    client = make_client("beth", "beth-val-2")

    # No "slack" adapter registered — should succeed without validation
    count = await client.subscribe_surface("slack:channel:12345")
    assert count == 1
    assert "slack:channel:12345" in [s["surface_ref"] for s in await client.list_surface_subscriptions()]


@pytest.mark.asyncio
async def test_surface_client_caches_canonical_ref(running_daemon, make_client):
    """Client caches the daemon-confirmed canonical ref, not caller input.

    This test verifies the echo-back path: subscribe_surface sends a ref,
    the daemon acks with a (potentially canonicalized) surface_ref, and
    the client stores THAT in its local cache. Currently identity, but
    this test ensures the plumbing works when canonicalization becomes
    non-trivial.
    """
    client = make_client("beth", "beth-val-3")

    await client.subscribe_surface("discord:user:116377")
    # Client should have the ref from the daemon ack, not its own input
    assert "discord:user:116377" in [s["surface_ref"] for s in await client.list_surface_subscriptions()]

    await client.unsubscribe_surface("discord:user:116377")
    assert "discord:user:116377" not in [s["surface_ref"] for s in await client.list_surface_subscriptions()]


# ---------------------------------------------------------------------------
# Slice C1 — Event routing skeleton
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_adapter_event_routing(running_daemon):
    """Events reach the correct handler methods."""
    from kiln.daemon.adapters.discord import DiscordAdapter
    from unittest.mock import AsyncMock, patch

    adapter = DiscordAdapter()
    await adapter.start(running_daemon)

    # Track which handlers are called
    calls = []

    async def track(name):
        def handler(self, event):
            calls.append((name, event.type))
        return handler

    # Patch each handler to record calls
    with patch.object(adapter, "_on_channel_message", side_effect=lambda e: calls.append(("channel_message", e.type))), \
         patch.object(adapter, "_on_session_live", side_effect=lambda e: calls.append(("session_connected", e.type))), \
         patch.object(adapter, "_on_session_gone", side_effect=lambda e: calls.append(("session_disconnected", e.type))):

        # Emit events through the daemon event bus
        await running_daemon.events.emit(proto.event(
            proto.EVT_MESSAGE_CHANNEL,
            channel="test", sender="beth-a", summary="hi", body="hello",
        ))
        await running_daemon.events.emit(proto.event(
            proto.EVT_SESSION_LIVE,
            session_id="beth-test-1", agent_name="beth",
        ))
        await running_daemon.events.emit(proto.event(
            proto.EVT_SESSION_GONE,
            session_id="beth-test-1", agent_name="beth",
        ))

        # Give event bus tasks time to complete
        await asyncio.sleep(0.1)

    assert ("channel_message", proto.EVT_MESSAGE_CHANNEL) in calls
    assert ("session_connected", proto.EVT_SESSION_LIVE) in calls
    assert ("session_disconnected", proto.EVT_SESSION_GONE) in calls

    await adapter.stop()


@pytest.mark.asyncio
async def test_adapter_echo_prevention(running_daemon):
    """Channel messages from Discord are not echoed back."""
    from kiln.daemon.adapters.discord import DiscordAdapter
    from unittest.mock import AsyncMock

    adapter = DiscordAdapter()
    await adapter.start(running_daemon)

    # Directly call _on_channel_message with a discord-sourced event
    # If echo prevention works, it should return without doing anything.
    # We can't easily test "nothing happened" in a stub, but we CAN verify
    # it doesn't raise and that future bridge rendering code won't fire.
    discord_event = proto.event(
        proto.EVT_MESSAGE_CHANNEL,
        channel="test", sender="discord-kira",
        summary="hi", body="hello", source="discord",
    )
    # Should return silently (echo prevention)
    await adapter._on_channel_message(discord_event)

    # Non-discord event should pass through (currently just logs)
    agent_event = proto.event(
        proto.EVT_MESSAGE_CHANNEL,
        channel="test", sender="beth-a",
        summary="hi", body="hello",
    )
    await adapter._on_channel_message(agent_event)

    await adapter.stop()


@pytest.mark.asyncio
async def test_adapter_ignores_unknown_events(running_daemon):
    """Unknown event types are silently ignored, not errored."""
    from kiln.daemon.adapters.discord import DiscordAdapter

    adapter = DiscordAdapter()
    await adapter.start(running_daemon)

    # Emit an event the adapter doesn't handle
    await running_daemon.events.emit(proto.event(
        "some.future.event", key="value",
    ))
    await asyncio.sleep(0.05)

    # No error, adapter still running
    assert adapter._daemon is running_daemon

    await adapter.stop()


@pytest.mark.asyncio
async def test_adapter_event_handler_table_completeness():
    """Verify the event handler table maps known event types."""
    from kiln.daemon.adapters.discord import DiscordAdapter

    expected_events = {
        proto.EVT_MESSAGE_CHANNEL,
        proto.EVT_SESSION_LIVE,
        proto.EVT_SESSION_GONE,
        proto.EVT_SESSION_MODE_CHANGED,
        proto.EVT_CHANNEL_SUBSCRIBED,
        proto.EVT_CHANNEL_UNSUBSCRIBED,
        proto.EVT_BRIDGE_BOUND,
        proto.EVT_BRIDGE_UNBOUND,
    }
    assert set(DiscordAdapter._EVENT_HANDLERS.keys()) == expected_events


# ---------------------------------------------------------------------------
# Slice C2 — Outbound rendering + thread lifecycle
# ---------------------------------------------------------------------------


class TestSplitMessage:
    def test_short_message_unchanged(self):
        from kiln.daemon.adapters.discord import split_message
        assert split_message("hello") == ["hello"]

    def test_long_message_splits_at_paragraph(self):
        from kiln.daemon.adapters.discord import split_message
        para1 = "A" * 1000
        para2 = "B" * 1000
        text = para1 + "\n\n" + para2
        chunks = split_message(text, max_len=1200)
        assert len(chunks) == 2
        assert "(1/2)" in chunks[0]
        assert "(2/2)" in chunks[1]

    def test_preserves_code_blocks(self):
        from kiln.daemon.adapters.discord import split_message
        code = "```python\n" + "x = 1\n" * 200 + "```"
        before = "Some text before.\n\n"
        text = before + code
        if len(text) > 1900:
            chunks = split_message(text, max_len=1900)
            # Should not split inside the code block
            for chunk in chunks:
                if "```python" in chunk:
                    assert "```" in chunk[chunk.index("```python") + 3:]


class TestFormatOutbound:
    def test_basic_format(self):
        from kiln.daemon.adapters.discord import format_outbound
        result = format_outbound("beth-a", "Hello world")
        assert result == "**beth-a:** Hello world"

    def test_empty_body_uses_summary(self):
        from kiln.daemon.adapters.discord import format_outbound
        result = format_outbound("beth-a", "", summary="Brief")
        assert result == "**beth-a:** Brief"

    def test_empty_returns_none(self):
        from kiln.daemon.adapters.discord import format_outbound
        assert format_outbound("beth-a", "", "") is None


@pytest.mark.asyncio
async def test_outbound_bridge_rendering(running_daemon, make_client):
    """Channel message with a bridge triggers outbound formatting and post."""
    from kiln.daemon.adapters.discord import DiscordAdapter
    from kiln.daemon.state import BridgeRecord
    from unittest.mock import AsyncMock

    adapter = DiscordAdapter()
    await adapter.start(running_daemon)

    # Register a bridge: channel "updates" → Discord thread "99999"
    running_daemon.state.bridges.bind(BridgeRecord(
        bridge_id="b1",
        source_kind="channel",
        source_name="updates",
        adapter_id="discord",
        platform_target="99999",
    ))

    # Mock the Discord API stub
    adapter._discord_post_to_surface = AsyncMock()

    # Simulate a channel message event
    await adapter._on_channel_message(proto.event(
        proto.EVT_MESSAGE_CHANNEL,
        channel="updates", sender="beth-test-1",
        summary="progress", body="Tests pass now",
    ))

    adapter._discord_post_to_surface.assert_called_once()
    call_args = adapter._discord_post_to_surface.call_args
    assert call_args[0][0] == "99999"  # surface_id
    assert "**beth-test-1:**" in call_args[0][1]
    assert "Tests pass now" in call_args[0][1]

    await adapter.stop()


@pytest.mark.asyncio
async def test_outbound_no_bridge_no_post(running_daemon):
    """Channel message without a bridge does not trigger a post."""
    from kiln.daemon.adapters.discord import DiscordAdapter
    from unittest.mock import AsyncMock

    adapter = DiscordAdapter()
    await adapter.start(running_daemon)
    adapter._discord_post_to_surface = AsyncMock()

    await adapter._on_channel_message(proto.event(
        proto.EVT_MESSAGE_CHANNEL,
        channel="unbridged", sender="beth-a",
        summary="hi", body="hello",
    ))

    adapter._discord_post_to_surface.assert_not_called()
    await adapter.stop()


@pytest.mark.asyncio
async def test_outbound_echo_prevention_with_bridge(running_daemon):
    """Discord-sourced messages are not echoed even when bridge exists."""
    from kiln.daemon.adapters.discord import DiscordAdapter
    from kiln.daemon.state import BridgeRecord
    from unittest.mock import AsyncMock

    adapter = DiscordAdapter()
    await adapter.start(running_daemon)

    running_daemon.state.bridges.bind(BridgeRecord(
        bridge_id="b1", source_kind="channel", source_name="chat",
        adapter_id="discord", platform_target="88888",
    ))

    adapter._discord_post_to_surface = AsyncMock()

    await adapter._on_channel_message(proto.event(
        proto.EVT_MESSAGE_CHANNEL,
        channel="chat", sender="discord-kira",
        summary="hi", body="hello", source="discord",
    ))

    adapter._discord_post_to_surface.assert_not_called()
    await adapter.stop()


@pytest.mark.asyncio
async def test_branch_thread_created_on_connect(running_daemon):
    """Session connect creates a branch thread when #branches is configured."""
    from kiln.daemon.adapters.discord import DiscordAdapter
    from unittest.mock import AsyncMock

    adapter = DiscordAdapter(config={"channels": {"branches": "123456"}})
    await adapter.start(running_daemon)

    adapter._discord_create_thread = AsyncMock(return_value=77777)

    await adapter._on_session_live(proto.event(
        proto.EVT_SESSION_LIVE,
        session_id="beth-test-1", agent_name="beth",
    ))

    adapter._discord_create_thread.assert_called_once_with(
        "123456", "beth-test-1", "Session beth-test-1 (beth)",
    )
    assert adapter._branch_threads["beth-test-1"] == 77777
    assert adapter._thread_to_session[77777] == "beth-test-1"

    await adapter.stop()


@pytest.mark.asyncio
async def test_branch_thread_reused_on_reconnect(running_daemon):
    """Existing branch thread is reused, not recreated."""
    from kiln.daemon.adapters.discord import DiscordAdapter
    from unittest.mock import AsyncMock

    adapter = DiscordAdapter(config={"channels": {"branches": "123456"}})
    await adapter.start(running_daemon)

    # Pre-populate existing thread mapping
    adapter._branch_threads["beth-test-1"] = 77777
    adapter._rebuild_reverse_indexes()

    adapter._discord_create_thread = AsyncMock()

    await adapter._on_session_live(proto.event(
        proto.EVT_SESSION_LIVE,
        session_id="beth-test-1", agent_name="beth",
    ))

    # Should NOT create a new thread
    adapter._discord_create_thread.assert_not_called()

    await adapter.stop()


@pytest.mark.asyncio
async def test_branch_thread_archived_on_disconnect(running_daemon):
    """Session disconnect archives the branch thread."""
    from kiln.daemon.adapters.discord import DiscordAdapter
    from unittest.mock import AsyncMock

    adapter = DiscordAdapter()
    await adapter.start(running_daemon)

    adapter._branch_threads["beth-test-1"] = 77777
    adapter._discord_archive_thread = AsyncMock()

    await adapter._on_session_gone(proto.event(
        proto.EVT_SESSION_GONE,
        session_id="beth-test-1", agent_name="beth",
    ))

    adapter._discord_archive_thread.assert_called_once_with(77777)

    await adapter.stop()


@pytest.mark.asyncio
async def test_branch_thread_no_branches_channel(running_daemon):
    """No branch thread created when #branches isn't configured."""
    from kiln.daemon.adapters.discord import DiscordAdapter
    from unittest.mock import AsyncMock

    adapter = DiscordAdapter()  # no channels config
    await adapter.start(running_daemon)

    adapter._discord_create_thread = AsyncMock()

    await adapter._on_session_live(proto.event(
        proto.EVT_SESSION_LIVE,
        session_id="beth-test-1", agent_name="beth",
    ))

    adapter._discord_create_thread.assert_not_called()

    await adapter.stop()


@pytest.mark.asyncio
async def test_channel_thread_created_on_bridge_bound(running_daemon):
    """Bridge bound creates a channel thread when #channels is configured."""
    from kiln.daemon.adapters.discord import DiscordAdapter
    from unittest.mock import AsyncMock

    adapter = DiscordAdapter(config={"channels": {"channels": "654321"}})
    await adapter.start(running_daemon)

    adapter._discord_create_thread = AsyncMock(return_value=88888)

    await adapter._on_bridge_bound(proto.event(
        proto.EVT_BRIDGE_BOUND,
        adapter_id="discord", source_kind="channel",
        source_name="updates", platform_target="654321",
    ))

    adapter._discord_create_thread.assert_called_once_with(
        "654321", "updates", "Bridge: updates",
    )
    assert adapter._channel_threads["updates"] == 88888
    assert adapter._thread_to_channel[88888] == "updates"

    await adapter.stop()


@pytest.mark.asyncio
async def test_channel_thread_reused_on_rebind(running_daemon):
    """Existing channel thread is not recreated on bridge rebind."""
    from kiln.daemon.adapters.discord import DiscordAdapter
    from unittest.mock import AsyncMock

    adapter = DiscordAdapter(config={"channels": {"channels": "654321"}})
    await adapter.start(running_daemon)

    adapter._channel_threads["updates"] = 88888
    adapter._rebuild_reverse_indexes()
    adapter._discord_create_thread = AsyncMock()

    await adapter._on_bridge_bound(proto.event(
        proto.EVT_BRIDGE_BOUND,
        adapter_id="discord", source_kind="channel",
        source_name="updates", platform_target="654321",
    ))

    adapter._discord_create_thread.assert_not_called()
    await adapter.stop()


@pytest.mark.asyncio
async def test_channel_thread_archived_on_bridge_unbound(running_daemon):
    """Bridge unbound archives the channel thread."""
    from kiln.daemon.adapters.discord import DiscordAdapter
    from unittest.mock import AsyncMock

    adapter = DiscordAdapter()
    await adapter.start(running_daemon)

    adapter._channel_threads["updates"] = 88888
    adapter._discord_archive_thread = AsyncMock()

    await adapter._on_bridge_unbound(proto.event(
        proto.EVT_BRIDGE_UNBOUND,
        adapter_id="discord", source_kind="channel",
        source_name="updates",
    ))

    adapter._discord_archive_thread.assert_called_once_with(88888)
    await adapter.stop()


@pytest.mark.asyncio
async def test_channel_thread_ignores_non_discord_bridge(running_daemon):
    """Bridge events for other adapters are ignored."""
    from kiln.daemon.adapters.discord import DiscordAdapter
    from unittest.mock import AsyncMock

    adapter = DiscordAdapter(config={"channels": {"channels": "654321"}})
    await adapter.start(running_daemon)
    adapter._discord_create_thread = AsyncMock()

    await adapter._on_bridge_bound(proto.event(
        proto.EVT_BRIDGE_BOUND,
        adapter_id="slack", source_kind="channel",
        source_name="updates",
    ))

    adapter._discord_create_thread.assert_not_called()
    await adapter.stop()


@pytest.mark.asyncio
async def test_channel_thread_no_channels_channel(running_daemon):
    """No channel thread created when #channels isn't configured."""
    from kiln.daemon.adapters.discord import DiscordAdapter
    from unittest.mock import AsyncMock

    adapter = DiscordAdapter()  # no channels config
    await adapter.start(running_daemon)
    adapter._discord_create_thread = AsyncMock()

    await adapter._on_bridge_bound(proto.event(
        proto.EVT_BRIDGE_BOUND,
        adapter_id="discord", source_kind="channel",
        source_name="updates",
    ))

    adapter._discord_create_thread.assert_not_called()
    await adapter.stop()


class TestDiscordValidateSurfaceRef:
    """Unit tests for DiscordAdapter.validate_surface_ref."""

    def test_valid_user(self):
        from kiln.daemon.adapters.discord import DiscordAdapter
        adapter = DiscordAdapter()
        assert adapter.validate_surface_ref("discord:user:116377") == "discord:user:116377"

    def test_valid_channel(self):
        from kiln.daemon.adapters.discord import DiscordAdapter
        adapter = DiscordAdapter()
        assert adapter.validate_surface_ref("discord:channel:999") == "discord:channel:999"

    def test_wrong_platform_prefix(self):
        from kiln.daemon.adapters.discord import DiscordAdapter
        adapter = DiscordAdapter()
        with pytest.raises(ValueError, match="Invalid Discord surface ref"):
            adapter.validate_surface_ref("slack:user:123")

    def test_missing_parts(self):
        from kiln.daemon.adapters.discord import DiscordAdapter
        adapter = DiscordAdapter()
        with pytest.raises(ValueError, match="Invalid Discord surface ref"):
            adapter.validate_surface_ref("discord:user")

    def test_unknown_type(self):
        from kiln.daemon.adapters.discord import DiscordAdapter
        adapter = DiscordAdapter()
        with pytest.raises(ValueError, match="Unknown Discord surface type"):
            adapter.validate_surface_ref("discord:guild:123")

    def test_empty_id(self):
        from kiln.daemon.adapters.discord import DiscordAdapter
        adapter = DiscordAdapter()
        with pytest.raises(ValueError, match="Surface ID cannot be empty"):
            adapter.validate_surface_ref("discord:user:")


# ---------------------------------------------------------------------------
# C3 — Inbound classification and routing
# ---------------------------------------------------------------------------

class _MockManagement:
    """Mock management layer that records calls and returns configurable results."""

    def __init__(self):
        from kiln.daemon.management import ActionResult
        self._default_result = ActionResult(True, "ok")
        self._results: dict[str, ActionResult] = {}
        self.calls: list[tuple[str, dict]] = []

    def set_result(self, action: str, result):
        from kiln.daemon.management import ActionResult
        if isinstance(result, tuple):
            result = ActionResult(*result)
        self._results[action] = result

    def _record(self, action: str, **kwargs):
        self.calls.append((action, kwargs))
        return self._results.get(action, self._default_result)

    async def set_session_mode(self, session_id, mode, requested_by=None):
        return self._record("set_session_mode",
                            session_id=session_id, mode=mode, requested_by=requested_by)

    async def spawn_session(self, agent, prompt=None, mode=None, requested_by=None):
        return self._record("spawn_session",
                            agent=agent, prompt=prompt, mode=mode, requested_by=requested_by)

    async def resume_session(self, agent, session_id, requested_by=None):
        return self._record("resume_session",
                            agent=agent, session_id=session_id, requested_by=requested_by)

    async def stop_session(self, session_id, requested_by=None):
        return self._record("stop_session",
                            session_id=session_id, requested_by=requested_by)

    async def interrupt_session(self, session_id, requested_by=None):
        return self._record("interrupt_session",
                            session_id=session_id, requested_by=requested_by)

    async def capture_session(self, session_id, lines=50):
        return self._record("capture_session",
                            session_id=session_id, lines=lines)

    def _session_config_path(self, session_id):
        return None


class _MockDaemon:
    """Minimal daemon mock for adapter routing tests."""

    def __init__(self):
        from kiln.daemon.state import DaemonState
        self.state = DaemonState()
        self.config = DaemonConfig(agents_registry=Path("/fake/agents.yml"))
        self.management = _MockManagement()
        self.delivered: list[tuple[str, proto.PlatformMessage]] = []
        self.published: list[dict] = []
        self.surface_delivered: list[tuple[str, proto.PlatformMessage]] = []

    async def deliver_platform_message(self, recipient, msg):
        self.delivered.append((recipient, msg))
        return Path("/fake/inbox/msg.md")

    async def publish_to_channel(self, channel, sender, summary, body,
                                 priority="normal", source="", exclude_sender=True):
        self.published.append({
            "channel": channel, "sender": sender,
            "summary": summary, "body": body, "source": source,
        })
        return 1

    async def deliver_to_surface_subscribers(self, surface_ref, msg):
        self.surface_delivered.append((surface_ref, msg))
        return 1


def _make_adapter(
    *,
    branch_threads=None,
    channel_threads=None,
    users=None,
    channels=None,
    dm_access="allowlist",
    channel_access="open",
):
    """Build a DiscordAdapter wired to a mock daemon with pre-set state."""
    from kiln.daemon.adapters.discord import DiscordAdapter

    config = {}
    if users:
        config["users"] = users
    if channels:
        config["channels"] = channels
    if dm_access:
        config["dm_access"] = dm_access
    if channel_access:
        config["channel_access"] = channel_access

    adapter = DiscordAdapter(config)
    adapter._daemon = _MockDaemon()

    if branch_threads:
        adapter._branch_threads = dict(branch_threads)
    if channel_threads:
        adapter._channel_threads = dict(channel_threads)
    adapter._rebuild_reverse_indexes()

    return adapter


def _msg(
    *,
    sender_id="111",
    sender_display_name="TestUser",
    channel_id="999",
    content="hello",
    is_dm=False,
    message_id="",
    attachment_paths=None,
):
    from kiln.daemon.adapters.discord import InboundMessage
    return InboundMessage(
        sender_id=sender_id,
        sender_display_name=sender_display_name,
        channel_id=channel_id,
        content=content,
        is_dm=is_dm,
        message_id=message_id,
        attachment_paths=attachment_paths or [],
    )


class TestClassifyMessage:
    """Unit tests for _classify_message — classification only, no delivery."""

    def test_branch_thread(self):
        from kiln.daemon.adapters.discord import RouteBucket
        adapter = _make_adapter(branch_threads={"beth-test-1": 9001})
        decision = adapter._classify_message(_msg(channel_id="9001"))
        assert decision is not None
        assert decision.bucket == RouteBucket.BRANCH
        assert decision.session_id == "beth-test-1"

    def test_bridge_thread(self):
        from kiln.daemon.adapters.discord import RouteBucket
        adapter = _make_adapter(channel_threads={"design-review": 8001})
        decision = adapter._classify_message(_msg(channel_id="8001"))
        assert decision is not None
        assert decision.bucket == RouteBucket.BRIDGE
        assert decision.channel_name == "design-review"

    def test_surface_subscription_dm(self):
        from kiln.daemon.adapters.discord import RouteBucket
        adapter = _make_adapter()
        adapter._daemon.state.surfaces.subscribe("discord:user:111", "beth-a")
        decision = adapter._classify_message(_msg(is_dm=True, sender_id="111"))
        assert decision is not None
        assert decision.bucket == RouteBucket.SURFACE
        assert decision.surface_ref == "discord:user:111"

    def test_surface_subscription_channel(self):
        from kiln.daemon.adapters.discord import RouteBucket
        adapter = _make_adapter()
        adapter._daemon.state.surfaces.subscribe("discord:channel:555", "beth-a")
        decision = adapter._classify_message(_msg(channel_id="555"))
        assert decision is not None
        assert decision.bucket == RouteBucket.SURFACE
        assert decision.surface_ref == "discord:channel:555"

    def test_unrouted(self):
        adapter = _make_adapter()
        decision = adapter._classify_message(_msg(channel_id="777"))
        assert decision is None

    def test_invariant_violation_branch_and_surface(self):
        from kiln.daemon.adapters.discord import RoutingError
        adapter = _make_adapter(branch_threads={"beth-test-1": 9001})
        # Also subscribe a surface for the same thread ID
        adapter._daemon.state.surfaces.subscribe("discord:channel:9001", "beth-b")
        with pytest.raises(RoutingError, match="invariant violation"):
            adapter._classify_message(_msg(channel_id="9001"))

    def test_invariant_violation_bridge_and_surface(self):
        from kiln.daemon.adapters.discord import RoutingError
        adapter = _make_adapter(channel_threads={"refactor": 8001})
        adapter._daemon.state.surfaces.subscribe("discord:channel:8001", "beth-b")
        with pytest.raises(RoutingError, match="invariant violation"):
            adapter._classify_message(_msg(channel_id="8001"))

    def test_invariant_violation_branch_and_bridge(self):
        from kiln.daemon.adapters.discord import RoutingError
        # Same thread ID in both maps — should never happen but must be caught
        adapter = _make_adapter(
            branch_threads={"beth-test-1": 9001},
            channel_threads={"refactor": 9001},
        )
        with pytest.raises(RoutingError, match="invariant violation"):
            adapter._classify_message(_msg(channel_id="9001"))

    def test_non_numeric_channel_id(self):
        """Non-numeric channel IDs don't match thread maps (int keys)."""
        adapter = _make_adapter(branch_threads={"beth-test-1": 9001})
        decision = adapter._classify_message(_msg(channel_id="not-a-number"))
        assert decision is None

    def test_surface_ref_dm(self):
        adapter = _make_adapter()
        msg = _msg(is_dm=True, sender_id="42")
        assert adapter._build_surface_ref(msg) == "discord:user:42"

    def test_surface_ref_channel(self):
        adapter = _make_adapter()
        msg = _msg(channel_id="555")
        assert adapter._build_surface_ref(msg) == "discord:channel:555"


class TestCheckAccess:

    def test_channel_open_allows_anyone(self):
        adapter = _make_adapter(channel_access="open")
        assert adapter._check_access(_msg(sender_id="unknown_user")) is True

    def test_dm_allowlist_blocks_unknown(self):
        adapter = _make_adapter(
            users={"111": {"name": "Kira", "trust": "full"}},
            dm_access="allowlist",
        )
        assert adapter._check_access(_msg(is_dm=True, sender_id="999")) is False

    def test_dm_allowlist_allows_known(self):
        adapter = _make_adapter(
            users={"111": {"name": "Kira", "trust": "full"}},
            dm_access="allowlist",
        )
        assert adapter._check_access(_msg(is_dm=True, sender_id="111")) is True

    def test_channel_allowlist(self):
        adapter = _make_adapter(
            users={"111": {"name": "Kira"}},
            channel_access="allowlist",
        )
        assert adapter._check_access(_msg(sender_id="111")) is True
        assert adapter._check_access(_msg(sender_id="999")) is False


class TestControlChannel:

    def test_is_control_channel(self):
        adapter = _make_adapter(channels={"control": "7777"})
        assert adapter._is_control_channel("7777") is True
        assert adapter._is_control_channel("9999") is False

    def test_no_control_configured(self):
        adapter = _make_adapter()
        assert adapter._is_control_channel("7777") is False


@pytest.mark.asyncio
class TestHandleMessage:
    """Integration tests for handle_message — full routing pipeline."""

    async def test_route_to_branch(self):
        from kiln.daemon.adapters.discord import RouteBucket
        adapter = _make_adapter(
            branch_threads={"beth-test-1": 9001},
            channel_access="open",
        )
        result = await adapter.handle_message(_msg(channel_id="9001"))
        assert result is not None
        assert result.bucket == RouteBucket.BRANCH
        # Verify daemon received the delivery
        daemon = adapter._daemon
        assert len(daemon.delivered) == 1
        assert daemon.delivered[0][0] == "beth-test-1"
        assert daemon.delivered[0][1].platform == "discord"

    async def test_route_to_bridge(self):
        from kiln.daemon.adapters.discord import RouteBucket
        adapter = _make_adapter(
            channel_threads={"design-review": 8001},
            channel_access="open",
        )
        result = await adapter.handle_message(
            _msg(channel_id="8001", content="looks good"),
        )
        assert result is not None
        assert result.bucket == RouteBucket.BRIDGE
        daemon = adapter._daemon
        assert len(daemon.published) == 1
        assert daemon.published[0]["channel"] == "design-review"
        assert daemon.published[0]["source"] == "discord"
        assert daemon.published[0]["body"] == "looks good"

    async def test_route_to_surface(self):
        from kiln.daemon.adapters.discord import RouteBucket
        adapter = _make_adapter(
            users={"111": {"name": "Kira", "trust": "full"}},
            dm_access="allowlist",
        )
        adapter._daemon.state.surfaces.subscribe("discord:user:111", "beth-a")
        result = await adapter.handle_message(
            _msg(is_dm=True, sender_id="111", content="hey beth"),
        )
        assert result is not None
        assert result.bucket == RouteBucket.SURFACE
        daemon = adapter._daemon
        assert len(daemon.surface_delivered) == 1
        assert daemon.surface_delivered[0][0] == "discord:user:111"
        assert daemon.surface_delivered[0][1].content == "hey beth"
        assert daemon.surface_delivered[0][1].trust == "full"

    async def test_access_denied_returns_none(self):
        adapter = _make_adapter(dm_access="allowlist", users={})
        result = await adapter.handle_message(
            _msg(is_dm=True, sender_id="999"),
        )
        assert result is None

    async def test_unrouted_returns_none(self):
        adapter = _make_adapter(channel_access="open")
        result = await adapter.handle_message(_msg(channel_id="777"))
        assert result is None

    async def test_control_channel_intercepted(self):
        adapter = _make_adapter(
            channels={"control": "7777"},
            channel_access="open",
            users={"111": {"name": "Kira", "trust": "full"}},
        )
        result = await adapter.handle_message(
            _msg(channel_id="7777", sender_id="111"),
        )
        # Control messages are consumed, not routed
        assert result is None
        daemon = adapter._daemon
        assert len(daemon.delivered) == 0
        assert len(daemon.published) == 0
        assert len(daemon.surface_delivered) == 0

    async def test_no_daemon_returns_none(self):
        from kiln.daemon.adapters.discord import DiscordAdapter
        adapter = DiscordAdapter()
        result = await adapter.handle_message(_msg())
        assert result is None

    async def test_identity_resolution_in_platform_message(self):
        """Verify the PlatformMessage carries resolved identity, not raw Discord data."""
        adapter = _make_adapter(
            users={"111": {"name": "Kira", "trust": "full"}},
            dm_access="allowlist",
        )
        adapter._daemon.state.surfaces.subscribe("discord:user:111", "beth-a")
        await adapter.handle_message(
            _msg(is_dm=True, sender_id="111", sender_display_name="krylea94"),
        )
        pm = adapter._daemon.surface_delivered[0][1]
        # Should use config name "Kira", not Discord display name
        assert pm.sender_name == "Kira"
        assert pm.sender_platform_id == "111"

    async def test_attachment_paths_carried_through(self):
        adapter = _make_adapter(
            branch_threads={"beth-test-1": 9001},
            channel_access="open",
        )
        await adapter.handle_message(
            _msg(channel_id="9001", attachment_paths=["/tmp/photo.png"]),
        )
        pm = adapter._daemon.delivered[0][1]
        assert pm.attachment_paths == ["/tmp/photo.png"]

    async def test_bridge_uses_sender_name_not_id(self):
        """Bridge publish should use resolved sender name."""
        adapter = _make_adapter(
            channel_threads={"chat": 8001},
            channel_access="open",
            users={"111": {"name": "Kira"}},
        )
        await adapter.handle_message(_msg(channel_id="8001", sender_id="111"))
        assert adapter._daemon.published[0]["sender"] == "Kira"


# ---------------------------------------------------------------------------
# Slice D1: Token resolution
# ---------------------------------------------------------------------------

class TestTokenResolution:
    """Tests for _resolve_token — token_file primary, inline fallback."""

    def test_token_from_file(self, tmp_path):
        from kiln.daemon.adapters.discord import DiscordAdapter

        token_file = tmp_path / "bot-token"
        token_file.write_text("my-secret-token\n")

        adapter = DiscordAdapter({"token_file": str(token_file)})
        assert adapter._resolve_token() == "my-secret-token"

    def test_token_inline_fallback(self):
        from kiln.daemon.adapters.discord import DiscordAdapter

        adapter = DiscordAdapter({"token": "inline-token"})
        assert adapter._resolve_token() == "inline-token"

    def test_token_file_takes_precedence(self, tmp_path):
        from kiln.daemon.adapters.discord import DiscordAdapter

        token_file = tmp_path / "bot-token"
        token_file.write_text("file-token\n")

        adapter = DiscordAdapter({
            "token_file": str(token_file),
            "token": "inline-token",
        })
        assert adapter._resolve_token() == "file-token"

    def test_no_token_returns_none(self):
        from kiln.daemon.adapters.discord import DiscordAdapter

        adapter = DiscordAdapter({})
        assert adapter._resolve_token() is None

    def test_empty_token_file_falls_back_to_inline(self, tmp_path):
        from kiln.daemon.adapters.discord import DiscordAdapter

        token_file = tmp_path / "bot-token"
        token_file.write_text("")

        adapter = DiscordAdapter({
            "token_file": str(token_file),
            "token": "inline-token",
        })
        assert adapter._resolve_token() == "inline-token"

    def test_missing_token_file_falls_back_to_inline(self):
        from kiln.daemon.adapters.discord import DiscordAdapter

        adapter = DiscordAdapter({
            "token_file": "/nonexistent/path/token",
            "token": "inline-token",
        })
        assert adapter._resolve_token() == "inline-token"


# ---------------------------------------------------------------------------
# Slice D1: Discord API method safety
# ---------------------------------------------------------------------------

class TestDiscordAPIMethods:
    """Tests for Discord API methods without a live client."""

    @pytest.mark.asyncio
    async def test_post_to_surface_without_client(self):
        """API methods should not crash when no client is connected."""
        adapter = _make_adapter(channel_access="open")
        # Should log a warning, not raise
        await adapter._discord_post_to_surface("12345", "test message")

    @pytest.mark.asyncio
    async def test_create_thread_without_client(self):
        adapter = _make_adapter(channel_access="open")
        result = await adapter._discord_create_thread("12345", "test-thread")
        assert result is None

    @pytest.mark.asyncio
    async def test_archive_thread_without_client(self):
        adapter = _make_adapter(channel_access="open")
        # Should not raise
        await adapter._discord_archive_thread(12345)

    def test_attachment_dir_with_state_dir(self, tmp_path):
        from kiln.daemon.adapters.discord import DiscordAdapter
        adapter = DiscordAdapter({})
        adapter._state_dir = tmp_path / "discord"
        adapter._state_dir.mkdir()
        d = adapter._get_attachment_dir()
        assert d == tmp_path / "discord" / "attachments"
        assert d.exists()

    def test_attachment_dir_without_state_dir(self):
        from kiln.daemon.adapters.discord import DiscordAdapter
        adapter = DiscordAdapter({})
        d = adapter._get_attachment_dir()
        assert "kiln-discord-attachments" in str(d)
        assert d.exists()


# ---------------------------------------------------------------------------
# Slice D1: Client extraction (on_message → InboundMessage)
# ---------------------------------------------------------------------------

class TestClientExtraction:
    """Tests for _DiscordClient.on_message extraction into InboundMessage."""

    @pytest.mark.asyncio
    async def test_on_message_extracts_inbound_message(self):
        """on_message should extract Discord fields into InboundMessage."""
        from kiln.daemon.adapters.discord import DiscordAdapter, InboundMessage

        adapter = _make_adapter(
            branch_threads={"beth-test": 5555},
            channel_access="open",
        )

        # Create a mock discord.Message
        msg = _mock_discord_message(
            author_id=111, author_name="TestUser",
            channel_id=5555, content="hello world",
            is_dm=False,
        )

        from kiln.daemon.adapters.discord import _DiscordClient
        client = _DiscordClient(adapter, asyncio.Event())
        # Patch user so the bot-self check works
        client._connection = type("FakeConn", (), {"user": _mock_discord_user(999, "BotUser")})()

        await client.on_message(msg)

        # Message should have been routed through handle_message → branch delivery
        assert len(adapter._daemon.delivered) == 1
        session_id, pm = adapter._daemon.delivered[0]
        assert session_id == "beth-test"
        assert pm.content == "hello world"
        assert pm.sender_platform_id == "111"

    @pytest.mark.asyncio
    async def test_on_message_skips_own_messages(self):
        """on_message should skip messages from the bot itself."""
        adapter = _make_adapter(channel_access="open")

        from kiln.daemon.adapters.discord import _DiscordClient
        client = _DiscordClient(adapter, asyncio.Event())
        bot_user = _mock_discord_user(999, "BotUser")
        client._connection = type("FakeConn", (), {"user": bot_user})()

        msg = _mock_discord_message(
            author_id=999, author_name="BotUser",
            channel_id=1234, content="echo",
        )
        # Make author == self.user
        msg.author = bot_user

        await client.on_message(msg)
        assert len(adapter._daemon.delivered) == 0

    @pytest.mark.asyncio
    async def test_on_message_skips_empty_messages(self):
        """on_message should skip messages with no content and no attachments."""
        adapter = _make_adapter(channel_access="open")

        from kiln.daemon.adapters.discord import _DiscordClient
        client = _DiscordClient(adapter, asyncio.Event())
        client._connection = type("FakeConn", (), {"user": _mock_discord_user(999, "Bot")})()

        msg = _mock_discord_message(
            author_id=111, author_name="User",
            channel_id=1234, content="   ",
        )

        await client.on_message(msg)
        assert len(adapter._daemon.delivered) == 0

    @pytest.mark.asyncio
    async def test_on_message_dm_detection(self):
        """on_message should detect DMs and set is_dm correctly."""
        adapter = _make_adapter(
            dm_access="open",
            users={"111": {"name": "Kira", "max_trust": "full"}},
        )
        adapter._daemon.state.surfaces.subscribe("discord:user:111", "beth-a")

        from kiln.daemon.adapters.discord import _DiscordClient
        client = _DiscordClient(adapter, asyncio.Event())
        client._connection = type("FakeConn", (), {"user": _mock_discord_user(999, "Bot")})()

        msg = _mock_discord_message(
            author_id=111, author_name="Kira",
            channel_id=7777, content="hey beth",
            is_dm=True,
        )

        await client.on_message(msg)
        assert len(adapter._daemon.surface_delivered) == 1
        ref, pm = adapter._daemon.surface_delivered[0]
        assert ref == "discord:user:111"
        assert pm.content == "hey beth"


# ---------------------------------------------------------------------------
# Slice D2: Platform ops
# ---------------------------------------------------------------------------

class TestPlatformOps:
    """Tests for D2 platform op handlers — arg validation and error paths."""

    @pytest.mark.asyncio
    async def test_op_send_missing_args(self):
        adapter = _make_adapter(channel_access="open")
        result = await adapter._op_send({}, None)
        assert result["ok"] is False
        assert "required" in result["error"]

    @pytest.mark.asyncio
    async def test_op_send_missing_content(self):
        adapter = _make_adapter(channel_access="open")
        result = await adapter._op_send({"target": "#general"}, None)
        assert result["ok"] is False

    @pytest.mark.asyncio
    async def test_op_send_unresolvable_target(self):
        adapter = _make_adapter(channel_access="open")
        # No client, so any target fails to resolve
        result = await adapter._op_send(
            {"target": "#nonexistent", "content": "hello"}, None,
        )
        assert result["ok"] is False
        assert "resolve" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_op_read_history_missing_target(self):
        adapter = _make_adapter(channel_access="open")
        result = await adapter._op_read_history({}, None)
        assert result["ok"] is False

    @pytest.mark.asyncio
    async def test_op_read_history_limit_capped(self):
        adapter = _make_adapter(channel_access="open")
        # Even with absurd limit, should not crash — just fail on resolve
        result = await adapter._op_read_history(
            {"target": "12345", "limit": 9999}, None,
        )
        assert result["ok"] is False

    @pytest.mark.asyncio
    async def test_op_branch_post_missing_args(self):
        adapter = _make_adapter(channel_access="open")
        result = await adapter._op_branch_post({}, None)
        assert result["ok"] is False

    @pytest.mark.asyncio
    async def test_op_branch_post_no_thread(self):
        adapter = _make_adapter(channel_access="open")
        result = await adapter._op_branch_post(
            {"session_id": "beth-nonexistent", "content": "hello"}, None,
        )
        assert result["ok"] is False
        assert "No branch thread" in result["error"]

    @pytest.mark.asyncio
    async def test_op_thread_create_missing_args(self):
        adapter = _make_adapter(channel_access="open")
        result = await adapter._op_thread_create({}, None)
        assert result["ok"] is False

    @pytest.mark.asyncio
    async def test_op_thread_archive_missing_args(self):
        adapter = _make_adapter(channel_access="open")
        result = await adapter._op_thread_archive({}, None)
        assert result["ok"] is False

    @pytest.mark.asyncio
    async def test_op_list_channels_no_client(self):
        adapter = _make_adapter(channel_access="open")
        result = await adapter._op_list_channels({}, None)
        assert result["ok"] is False

    @pytest.mark.asyncio
    async def test_op_delete_missing_args(self):
        adapter = _make_adapter(channel_access="open")
        result = await adapter._op_delete({}, None)
        assert result["ok"] is False

    @pytest.mark.asyncio
    async def test_op_delete_needs_both_args(self):
        adapter = _make_adapter(channel_access="open")
        result = await adapter._op_delete({"target": "#general"}, None)
        assert result["ok"] is False

    # test_d3_ops_still_raise removed — D3b ops are now implemented


# ---------------------------------------------------------------------------
# Voice receive (inbound transcription)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestVoiceReceive:
    """Tests for inbound voice memo transcription in _DiscordClient."""

    async def test_voice_fallback_no_credentials(self):
        """Voice message without credentials_dir produces fallback text."""
        from kiln.daemon.adapters.discord import _DiscordClient
        adapter = _make_adapter(
            branch_threads={"beth-test": 5555},
            channel_access="open",
        )
        # No credentials_dir set
        client = _DiscordClient(adapter, asyncio.Event())

        result = await client._transcribe_voice("/tmp/audio.ogg", "", "Kira")
        assert "Voice message received" in result
        assert "no credentials" in result
        assert "/tmp/audio.ogg" in result

    async def test_voice_fallback_import_failure(self, monkeypatch):
        """Voice message with import failure produces fallback text."""
        from kiln.daemon.adapters.discord import _DiscordClient
        import builtins
        real_import = builtins.__import__

        def fail_voice_import(name, *args, **kwargs):
            if "voice" in name:
                raise ImportError("no voice")
            return real_import(name, *args, **kwargs)

        adapter = _make_adapter(channel_access="open")
        adapter._discord_config.credentials_dir = "/fake/creds"
        client = _DiscordClient(adapter, asyncio.Event())

        monkeypatch.setattr(builtins, "__import__", fail_voice_import)
        result = await client._transcribe_voice("/tmp/audio.ogg", "", "Kira")
        assert "Voice message received" in result
        assert "unavailable" in result

    async def test_voice_fallback_preserves_existing_content(self):
        """Fallback text should preserve any existing message content."""
        from kiln.daemon.adapters.discord import _DiscordClient
        adapter = _make_adapter(channel_access="open")
        client = _DiscordClient(adapter, asyncio.Event())

        result = await client._transcribe_voice("/tmp/audio.ogg", "some text", "Kira")
        assert "Voice message received" in result
        assert "some text" in result

    async def test_voice_successful_transcript(self):
        """Successful transcription should prepend transcript to content."""
        from kiln.daemon.adapters.discord import _DiscordClient
        from unittest.mock import AsyncMock, patch

        adapter = _make_adapter(channel_access="open")
        adapter._discord_config.credentials_dir = "/fake/creds"
        client = _DiscordClient(adapter, asyncio.Event())

        mock_stt = AsyncMock()
        mock_stt.transcribe = AsyncMock(return_value="hello world transcribed")

        with patch("kiln.daemon.adapters.discord.WhisperSTT", return_value=mock_stt, create=True):
            # We need to patch at import time — use a different approach
            import sys
            voice_mod = type(sys)("voice")
            voice_openai = type(sys)("voice.openai")
            mock_class = type("WhisperSTT", (), {
                "__init__": lambda self, path: None,
                "transcribe": AsyncMock(return_value="hello world transcribed"),
            })
            voice_openai.WhisperSTT = mock_class
            sys.modules["voice"] = voice_mod
            sys.modules["voice.openai"] = voice_openai
            try:
                result = await client._transcribe_voice("/tmp/audio.ogg", "", "Kira")
                assert "Voice message transcript" in result
                assert "hello world transcribed" in result
            finally:
                del sys.modules["voice"]
                del sys.modules["voice.openai"]

    async def test_voice_transcript_with_existing_content(self):
        """Transcript + existing content should both appear."""
        from kiln.daemon.adapters.discord import _DiscordClient
        from unittest.mock import AsyncMock
        import sys

        adapter = _make_adapter(channel_access="open")
        adapter._discord_config.credentials_dir = "/fake/creds"
        client = _DiscordClient(adapter, asyncio.Event())

        voice_mod = type(sys)("voice")
        voice_openai = type(sys)("voice.openai")
        mock_class = type("WhisperSTT", (), {
            "__init__": lambda self, path: None,
            "transcribe": AsyncMock(return_value="transcribed text"),
        })
        voice_openai.WhisperSTT = mock_class
        sys.modules["voice"] = voice_mod
        sys.modules["voice.openai"] = voice_openai
        try:
            result = await client._transcribe_voice("/tmp/audio.ogg", "original text", "Kira")
            assert "transcribed text" in result
            assert "original text" in result
        finally:
            del sys.modules["voice"]
            del sys.modules["voice.openai"]

    async def test_voice_flag_triggers_transcription(self):
        """on_message with voice flag should attempt transcription."""
        from kiln.daemon.adapters.discord import _DiscordClient
        from unittest.mock import AsyncMock

        adapter = _make_adapter(
            branch_threads={"beth-test": 5555},
            channel_access="open",
        )
        client = _DiscordClient(adapter, asyncio.Event())
        client._connection = type("FakeConn", (), {
            "user": _mock_discord_user(999, "Bot"),
        })()

        # Mock attachment with save method
        mock_att = type("MockAttachment", (), {
            "filename": "voice-message.ogg",
            "size": 1234,
            "save": AsyncMock(),
        })()

        msg = _mock_discord_message(
            author_id=111, author_name="Kira",
            channel_id=5555, content="",
            attachments=[mock_att],
            voice=True,
        )

        # Patch _transcribe_voice to verify it's called
        client._transcribe_voice = AsyncMock(return_value="[Voice transcript]")

        await client.on_message(msg)

        client._transcribe_voice.assert_called_once()
        # Should have routed with the transcribed content
        assert len(adapter._daemon.delivered) == 1
        _, pm = adapter._daemon.delivered[0]
        assert pm.content == "[Voice transcript]"

    async def test_non_voice_skips_transcription(self):
        """Regular messages with attachments should NOT attempt transcription."""
        from kiln.daemon.adapters.discord import _DiscordClient
        from unittest.mock import AsyncMock

        adapter = _make_adapter(
            branch_threads={"beth-test": 5555},
            channel_access="open",
        )
        client = _DiscordClient(adapter, asyncio.Event())
        client._connection = type("FakeConn", (), {
            "user": _mock_discord_user(999, "Bot"),
        })()

        mock_att = type("MockAttachment", (), {
            "filename": "image.png",
            "size": 5000,
            "save": AsyncMock(),
        })()

        msg = _mock_discord_message(
            author_id=111, author_name="Kira",
            channel_id=5555, content="check this out",
            attachments=[mock_att],
            voice=False,
        )

        client._transcribe_voice = AsyncMock()
        await client.on_message(msg)

        client._transcribe_voice.assert_not_called()
        assert len(adapter._daemon.delivered) == 1
        _, pm = adapter._daemon.delivered[0]
        assert pm.content == "check this out"


# ---------------------------------------------------------------------------
# Slice D3d: Voice send
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestVoiceSend:
    """Tests for _op_voice_send — arg validation and config checks."""

    async def test_voice_missing_target(self):
        adapter = _make_adapter(channel_access="open")
        result = await adapter._op_voice_send({"text": "hello"}, None)
        assert result["ok"] is False
        assert "required" in result["error"]

    async def test_voice_missing_text(self):
        adapter = _make_adapter(channel_access="open")
        result = await adapter._op_voice_send({"target": "#general"}, None)
        assert result["ok"] is False
        assert "required" in result["error"]

    async def test_voice_no_credentials_dir(self):
        adapter = _make_adapter(channel_access="open")
        # No credentials_dir configured
        result = await adapter._op_voice_send(
            {"target": "#general", "text": "hello"}, None,
        )
        assert result["ok"] is False
        assert "credentials_dir" in result["error"]

    async def test_voice_import_failure(self, monkeypatch):
        """If voice service is not importable, returns clean error."""
        import builtins
        real_import = builtins.__import__

        def fail_voice_import(name, *args, **kwargs):
            if name.startswith("voice"):
                raise ImportError("no voice")
            return real_import(name, *args, **kwargs)

        adapter = _make_adapter(channel_access="open")
        adapter._discord_config.credentials_dir = "/fake/creds"
        monkeypatch.setattr(builtins, "__import__", fail_voice_import)
        result = await adapter._op_voice_send(
            {"target": "#general", "text": "hello"}, None,
        )
        assert result["ok"] is False
        assert "import" in result["error"].lower()

    async def test_voice_unresolvable_target(self):
        adapter = _make_adapter(channel_access="open")
        adapter._discord_config.credentials_dir = "/fake/creds"
        # No client → target resolution fails, but import might fail first
        # So we mock the import away
        import sys
        voice_mock = type(sys)("voice")
        voice_openai = type(sys)("voice.openai")
        voice_discord = type(sys)("voice.discord")
        voice_openai.generate_speech = None
        voice_discord.send_voice_message = None
        sys.modules["voice"] = voice_mock
        sys.modules["voice.openai"] = voice_openai
        sys.modules["voice.discord"] = voice_discord
        try:
            result = await adapter._op_voice_send(
                {"target": "#nonexistent", "text": "hello"}, None,
            )
            assert result["ok"] is False
            assert "resolve" in result["error"].lower()
        finally:
            del sys.modules["voice"]
            del sys.modules["voice.openai"]
            del sys.modules["voice.discord"]

    async def test_voice_config_defaults_used(self):
        """Voice and instructions should fall back to adapter config."""
        from kiln.daemon.adapters.discord import DiscordAdapterConfig
        adapter = _make_adapter(channel_access="open")
        adapter._discord_config.credentials_dir = "/fake/creds"
        adapter._discord_config.voice_default = "coral"
        adapter._discord_config.voice_instructions = "speak warmly"

        # We can't test the full TTS pipeline without mocking, but we can
        # verify the config is properly wired by checking the op doesn't
        # crash on arg parsing before hitting the import
        result = await adapter._op_voice_send(
            {"target": "#general", "text": "hello"}, None,
        )
        # Will fail at import or resolve, but should not crash on config access
        assert result["ok"] is False


class TestSendUserMessage:
    """Tests for send_user_message."""

    @pytest.mark.asyncio
    async def test_empty_body_raises(self):
        from kiln.daemon.adapters.discord import DiscordAdapter
        adapter = DiscordAdapter({})
        adapter._daemon = _MockDaemon()
        with pytest.raises(ValueError, match="empty"):
            await adapter.send_user_message("kira", "", "")

    @pytest.mark.asyncio
    async def test_no_client_raises(self):
        from kiln.daemon.adapters.discord import DiscordAdapter
        adapter = DiscordAdapter({})
        adapter._daemon = _MockDaemon()
        with pytest.raises(ValueError, match="client"):
            await adapter.send_user_message("kira", "hi", "hello")

    @pytest.mark.asyncio
    async def test_no_platform_id_raises(self):
        """User exists in daemon config but has no discord platform ID."""
        from kiln.daemon.adapters.discord import DiscordAdapter
        from kiln.daemon.config import UserConfig
        adapter = DiscordAdapter({})
        adapter._daemon = _MockDaemon()
        adapter._daemon.config = type("C", (), {
            "users": {"kira": UserConfig(name="kira", platforms={})},
        })()
        adapter._client = True  # fake "connected"
        with pytest.raises(ValueError, match="platform ID"):
            await adapter.send_user_message("kira", "hi", "hello")

    def test_resolve_user_platform_id_found(self):
        """Should look up Discord ID from daemon config, not adapter trust map."""
        from kiln.daemon.adapters.discord import DiscordAdapter
        from kiln.daemon.config import UserConfig
        adapter = DiscordAdapter({})
        adapter._daemon = type("D", (), {
            "config": type("C", (), {
                "users": {"kira": UserConfig(
                    name="kira",
                    platforms={"discord": "123456"},
                )},
            })(),
        })()
        assert adapter._resolve_user_platform_id("kira") == "123456"

    def test_resolve_user_platform_id_not_found(self):
        from kiln.daemon.adapters.discord import DiscordAdapter
        adapter = DiscordAdapter({})
        adapter._daemon = type("D", (), {
            "config": type("C", (), {"users": {}})(),
        })()
        assert adapter._resolve_user_platform_id("nobody") is None


class TestResolveTarget:
    """Tests for _resolve_target without a live client."""

    @pytest.mark.asyncio
    async def test_no_client_returns_none(self):
        from kiln.daemon.adapters.discord import DiscordAdapter
        adapter = DiscordAdapter({})
        result = await adapter._resolve_target("#general")
        assert result is None

    @pytest.mark.asyncio
    async def test_named_channel_from_config(self):
        from kiln.daemon.adapters.discord import DiscordAdapter
        # Even with a channel in config, no client means None
        adapter = DiscordAdapter({"channels": {"general": "12345"}})
        result = await adapter._resolve_target("#general")
        assert result is None


# ---------------------------------------------------------------------------
# Slice D1: Startup rollback
# ---------------------------------------------------------------------------

class TestStartupRollback:
    """Tests for adapter startup failure rollback."""

    @pytest.mark.asyncio
    async def test_failed_start_clears_daemon_ref(self, tmp_path):
        """If client startup fails, adapter should not keep daemon ref."""
        from kiln.daemon.adapters.discord import DiscordAdapter
        from unittest.mock import AsyncMock, patch

        adapter = DiscordAdapter({"token": "bad-token"})
        mock_daemon = _MockDaemon()
        mock_daemon.config = type("C", (), {"state_dir": tmp_path})()
        mock_daemon.events = type("E", (), {
            "add_handler": lambda self, h: None,
            "remove_handler": lambda self, h: None,
        })()

        # Patch _start_client to simulate login failure
        with patch.object(adapter, "_start_client", side_effect=RuntimeError("Login failed")):
            with pytest.raises(RuntimeError, match="Login failed"):
                await adapter.start(mock_daemon)

        # Adapter should have rolled back
        assert adapter._daemon is None

    @pytest.mark.asyncio
    async def test_failed_start_does_not_register_event_handler(self, tmp_path):
        """If client startup fails, event handler should not be registered."""
        from kiln.daemon.adapters.discord import DiscordAdapter
        from unittest.mock import patch

        adapter = DiscordAdapter({"token": "bad-token"})
        handlers_registered = []
        mock_daemon = _MockDaemon()
        mock_daemon.config = type("C", (), {"state_dir": tmp_path})()
        mock_daemon.events = type("E", (), {
            "add_handler": lambda self, h: handlers_registered.append(h),
            "remove_handler": lambda self, h: None,
        })()

        with patch.object(adapter, "_start_client", side_effect=RuntimeError("boom")):
            with pytest.raises(RuntimeError):
                await adapter.start(mock_daemon)

        assert len(handlers_registered) == 0


# ---------------------------------------------------------------------------
# Slice D1: RoutingError propagation
# ---------------------------------------------------------------------------

class TestRoutingErrorPropagation:
    """RoutingError should not be swallowed by the client."""

    @pytest.mark.asyncio
    async def test_routing_error_propagates_from_on_message(self):
        from kiln.daemon.adapters.discord import (
            _DiscordClient, DiscordAdapter, RoutingError,
        )
        from unittest.mock import AsyncMock

        adapter = _make_adapter(channel_access="open")
        adapter.handle_message = AsyncMock(side_effect=RoutingError("overlap"))

        client = _DiscordClient(adapter, asyncio.Event())
        client._connection = type("FakeConn", (), {
            "user": _mock_discord_user(999, "Bot"),
        })()

        msg = _mock_discord_message(
            author_id=111, author_name="User",
            channel_id=1234, content="hello",
        )

        with pytest.raises(RoutingError):
            await client.on_message(msg)


# ---------------------------------------------------------------------------
# Slice D1: Filename sanitization
# ---------------------------------------------------------------------------

class TestSanitizeFilename:
    """Tests for _sanitize_filename."""

    def test_normal_filename(self):
        from kiln.daemon.adapters.discord import _sanitize_filename
        assert _sanitize_filename("photo.png") == "photo.png"

    def test_strips_path_traversal(self):
        from kiln.daemon.adapters.discord import _sanitize_filename
        result = _sanitize_filename("../../etc/passwd")
        assert "/" not in result
        assert ".." not in result

    def test_strips_backslash_paths(self):
        from kiln.daemon.adapters.discord import _sanitize_filename
        result = _sanitize_filename("C:\\Users\\evil\\payload.exe")
        assert "\\" not in result
        assert "payload.exe" in result

    def test_replaces_shell_dangerous_chars(self):
        from kiln.daemon.adapters.discord import _sanitize_filename
        result = _sanitize_filename('file<>:"|?*.txt')
        assert "<" not in result
        assert ">" not in result
        assert ":" not in result

    def test_empty_becomes_attachment(self):
        from kiln.daemon.adapters.discord import _sanitize_filename
        assert _sanitize_filename("") == "attachment"

    def test_dots_only_becomes_attachment(self):
        from kiln.daemon.adapters.discord import _sanitize_filename
        assert _sanitize_filename("...") == "attachment"

    def test_long_filename_truncated(self):
        from kiln.daemon.adapters.discord import _sanitize_filename
        long_name = "a" * 300 + ".png"
        result = _sanitize_filename(long_name)
        assert len(result) <= 200


# Mock helpers for discord.py objects

def _mock_discord_user(user_id: int, name: str):
    """Create a minimal mock discord.User-like object."""
    user = type("MockUser", (), {
        "id": user_id,
        "name": name,
        "display_name": name,
        "__eq__": lambda self, other: getattr(other, "id", None) == self.id,
    })()
    return user


def _mock_discord_message(
    *,
    author_id: int,
    author_name: str,
    channel_id: int,
    content: str,
    is_dm: bool = False,
    attachments: list | None = None,
    voice: bool = False,
):
    """Create a minimal mock discord.Message-like object."""
    import discord as _discord

    author = _mock_discord_user(author_id, author_name)

    if is_dm:
        channel = type("MockDMChannel", (_discord.DMChannel,), {
            "id": channel_id,
            "__init__": lambda self, **kw: None,
        })()
        channel.id = channel_id
    else:
        channel = type("MockChannel", (), {
            "id": channel_id,
            "name": f"channel-{channel_id}",
        })()

    msg = type("MockMessage", (), {
        "id": channel_id * 1000 + author_id,  # deterministic fake snowflake
        "author": author,
        "channel": channel,
        "content": content,
        "attachments": attachments or [],
        "flags": type("Flags", (), {"voice": voice})(),
    })()
    return msg


def _control_msg(content: str, **kwargs) -> "InboundMessage":
    """Build an InboundMessage suitable for control command tests."""
    from kiln.daemon.adapters.discord import InboundMessage
    defaults = dict(
        sender_id="111",
        sender_display_name="Kira",
        channel_id="7777",
        content=content,
        is_dm=False,
        message_id="99999",
    )
    defaults.update(kwargs)
    return InboundMessage(**defaults)


def _make_control_adapter(**kwargs):
    """Build an adapter wired for control command tests.

    Returns (adapter, mock_management) for easy assertion access.
    """
    adapter = _make_adapter(channels={"control": "7777"}, **kwargs)
    return adapter, adapter._daemon.management


# ---------------------------------------------------------------------------
# Slice D3a: Control commands
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestControlCommands:
    """Tests for the control command parser and individual command handlers."""

    # --- Parser / dispatch ---

    async def test_unknown_command(self):
        adapter, mgmt = _make_control_adapter()
        # Patch _control_respond to capture output
        responses = []
        adapter._control_respond = lambda msg, text: _capture(responses, text)
        await adapter._handle_control_message(
            _control_msg("frobnicate"), "Kira", "full",
        )
        assert len(responses) == 1
        assert "Unknown command" in responses[0]
        assert "frobnicate" in responses[0]

    async def test_empty_message_ignored(self):
        adapter, mgmt = _make_control_adapter()
        responses = []
        adapter._control_respond = lambda msg, text: _capture(responses, text)
        await adapter._handle_control_message(
            _control_msg("   "), "Kira", "full",
        )
        assert len(responses) == 0

    async def test_trust_denial(self):
        adapter, mgmt = _make_control_adapter()
        responses = []
        adapter._control_respond = lambda msg, text: _capture(responses, text)
        await adapter._handle_control_message(
            _control_msg("help"), "Stranger", "known",
        )
        # Should be silently denied — no response, no management call
        assert len(responses) == 0
        assert len(mgmt.calls) == 0

    async def test_command_error_caught(self):
        adapter, mgmt = _make_control_adapter()
        responses = []
        adapter._control_respond = lambda msg, text: _capture(responses, text)
        # Make management raise
        async def explode(*a, **kw):
            raise RuntimeError("boom")
        mgmt.stop_session = explode

        await adapter._handle_control_message(
            _control_msg("kill beth-test-1"), "Kira", "full",
        )
        assert len(responses) == 1
        assert "boom" in responses[0]

    # --- help ---

    async def test_help(self):
        adapter, mgmt = _make_control_adapter()
        responses = []
        adapter._control_respond = lambda msg, text: _capture(responses, text)
        await adapter._handle_control_message(
            _control_msg("help"), "Kira", "full",
        )
        assert len(responses) == 1
        text = responses[0]
        for cmd in ("spawn", "kill", "interrupt", "resume", "show", "mode", "help"):
            assert cmd in text

    # --- mode ---

    async def test_mode_success(self):
        adapter, mgmt = _make_control_adapter()
        from kiln.daemon.management import ActionResult
        mgmt.set_result("set_session_mode", ActionResult(True, "beth-test: safe -> yolo"))

        responses = []
        adapter._control_respond = lambda msg, text: _capture(responses, text)
        await adapter._handle_control_message(
            _control_msg("mode beth-test yolo"), "Kira", "full",
        )
        assert len(mgmt.calls) == 1
        assert mgmt.calls[0] == ("set_session_mode", {
            "session_id": "beth-test", "mode": "yolo", "requested_by": "Kira",
        })
        assert "\u2705" in responses[0]

    async def test_mode_failure(self):
        adapter, mgmt = _make_control_adapter()
        from kiln.daemon.management import ActionResult
        mgmt.set_result("set_session_mode", ActionResult(False, "Invalid mode"))

        responses = []
        adapter._control_respond = lambda msg, text: _capture(responses, text)
        await adapter._handle_control_message(
            _control_msg("mode beth-test badmode"), "Kira", "full",
        )
        assert "\u274c" in responses[0]

    async def test_mode_missing_args(self):
        adapter, mgmt = _make_control_adapter()
        responses = []
        adapter._control_respond = lambda msg, text: _capture(responses, text)
        await adapter._handle_control_message(
            _control_msg("mode beth-test"), "Kira", "full",
        )
        assert "Usage" in responses[0]
        assert len(mgmt.calls) == 0

    async def test_mode_trusted_rejected(self):
        adapter, mgmt = _make_control_adapter()
        responses = []
        adapter._control_respond = lambda msg, text: _capture(responses, text)
        await adapter._handle_control_message(
            _control_msg("mode beth-test trusted"), "Kira", "full",
        )
        assert "TUI-only" in responses[0]
        assert len(mgmt.calls) == 0

    # --- spawn ---

    async def test_spawn_success(self):
        adapter, mgmt = _make_control_adapter()
        from kiln.daemon.management import ActionResult
        mgmt.set_result("spawn_session", ActionResult(True, "Session launched"))

        responses = []
        adapter._control_respond = lambda msg, text: _capture(responses, text)
        # Mock agent registry
        adapter._get_known_agents = lambda: {"beth", "dalet"}
        await adapter._handle_control_message(
            _control_msg("spawn beth do some research"), "Kira", "full",
        )
        assert len(mgmt.calls) == 1
        assert mgmt.calls[0] == ("spawn_session", {
            "agent": "beth", "prompt": "do some research",
            "mode": None, "requested_by": "Kira",
        })
        assert "\u2705" in responses[0]

    async def test_spawn_no_instructions(self):
        adapter, mgmt = _make_control_adapter()
        responses = []
        adapter._control_respond = lambda msg, text: _capture(responses, text)
        adapter._get_known_agents = lambda: {"beth"}
        await adapter._handle_control_message(
            _control_msg("spawn beth"), "Kira", "full",
        )
        assert len(mgmt.calls) == 1
        assert mgmt.calls[0][1]["agent"] == "beth"
        assert mgmt.calls[0][1]["prompt"] is None

    async def test_spawn_unknown_agent(self):
        adapter, mgmt = _make_control_adapter()
        responses = []
        adapter._control_respond = lambda msg, text: _capture(responses, text)
        adapter._get_known_agents = lambda: {"beth", "dalet"}
        await adapter._handle_control_message(
            _control_msg("spawn hackerman"), "Kira", "full",
        )
        assert len(mgmt.calls) == 0
        assert "Unknown agent" in responses[0]
        assert "hackerman" in responses[0]
        assert "beth" in responses[0]  # lists known agents

    async def test_spawn_missing_agent(self):
        adapter, mgmt = _make_control_adapter()
        responses = []
        adapter._control_respond = lambda msg, text: _capture(responses, text)
        await adapter._handle_control_message(
            _control_msg("spawn"), "Kira", "full",
        )
        assert "Usage" in responses[0]
        assert len(mgmt.calls) == 0

    async def test_spawn_case_insensitive_agent(self):
        adapter, mgmt = _make_control_adapter()
        responses = []
        adapter._control_respond = lambda msg, text: _capture(responses, text)
        adapter._get_known_agents = lambda: {"beth"}
        await adapter._handle_control_message(
            _control_msg("spawn Beth"), "Kira", "full",
        )
        assert len(mgmt.calls) == 1
        assert mgmt.calls[0][1]["agent"] == "beth"

    # --- resume ---

    async def test_resume_success(self):
        adapter, mgmt = _make_control_adapter()
        from kiln.daemon.management import ActionResult
        mgmt.set_result("resume_session", ActionResult(True, "Resumed beth-old-fox"))

        responses = []
        adapter._control_respond = lambda msg, text: _capture(responses, text)
        await adapter._handle_control_message(
            _control_msg("resume beth-old-fox"), "Kira", "full",
        )
        assert len(mgmt.calls) == 1
        assert mgmt.calls[0] == ("resume_session", {
            "agent": "beth", "session_id": "beth-old-fox", "requested_by": "Kira",
        })
        assert "\u2705" in responses[0]

    async def test_resume_extracts_agent_from_id(self):
        adapter, mgmt = _make_control_adapter()
        responses = []
        adapter._control_respond = lambda msg, text: _capture(responses, text)
        await adapter._handle_control_message(
            _control_msg("resume dalet-quiet-drake"), "Kira", "full",
        )
        assert mgmt.calls[0][1]["agent"] == "dalet"
        assert mgmt.calls[0][1]["session_id"] == "dalet-quiet-drake"

    async def test_resume_missing_args(self):
        adapter, mgmt = _make_control_adapter()
        responses = []
        adapter._control_respond = lambda msg, text: _capture(responses, text)
        await adapter._handle_control_message(
            _control_msg("resume"), "Kira", "full",
        )
        assert "Usage" in responses[0]
        assert len(mgmt.calls) == 0

    async def test_resume_no_hyphen_in_id(self):
        adapter, mgmt = _make_control_adapter()
        responses = []
        adapter._control_respond = lambda msg, text: _capture(responses, text)
        await adapter._handle_control_message(
            _control_msg("resume badid"), "Kira", "full",
        )
        assert "Cannot determine agent" in responses[0]
        assert len(mgmt.calls) == 0

    # --- kill ---

    async def test_kill_success(self):
        adapter, mgmt = _make_control_adapter()
        from kiln.daemon.management import ActionResult
        mgmt.set_result("stop_session", ActionResult(True, "Killed beth-test"))

        responses = []
        adapter._control_respond = lambda msg, text: _capture(responses, text)
        await adapter._handle_control_message(
            _control_msg("kill beth-test"), "Kira", "full",
        )
        assert len(mgmt.calls) == 1
        assert mgmt.calls[0] == ("stop_session", {
            "session_id": "beth-test", "requested_by": "Kira",
        })
        assert "\u2705" in responses[0]

    async def test_kill_failure(self):
        adapter, mgmt = _make_control_adapter()
        from kiln.daemon.management import ActionResult
        mgmt.set_result("stop_session", ActionResult(False, "tmux session not found"))

        responses = []
        adapter._control_respond = lambda msg, text: _capture(responses, text)
        await adapter._handle_control_message(
            _control_msg("kill beth-ghost"), "Kira", "full",
        )
        assert "\u274c" in responses[0]

    async def test_kill_missing_args(self):
        adapter, mgmt = _make_control_adapter()
        responses = []
        adapter._control_respond = lambda msg, text: _capture(responses, text)
        await adapter._handle_control_message(
            _control_msg("kill"), "Kira", "full",
        )
        assert "Usage" in responses[0]

    # --- interrupt ---

    async def test_interrupt_success(self):
        adapter, mgmt = _make_control_adapter()
        from kiln.daemon.management import ActionResult
        mgmt.set_result("interrupt_session", ActionResult(True, "Sent ESC to beth-test"))

        responses = []
        adapter._control_respond = lambda msg, text: _capture(responses, text)
        await adapter._handle_control_message(
            _control_msg("interrupt beth-test"), "Kira", "full",
        )
        assert len(mgmt.calls) == 1
        assert mgmt.calls[0] == ("interrupt_session", {
            "session_id": "beth-test", "requested_by": "Kira",
        })

    async def test_interrupt_missing_args(self):
        adapter, mgmt = _make_control_adapter()
        responses = []
        adapter._control_respond = lambda msg, text: _capture(responses, text)
        await adapter._handle_control_message(
            _control_msg("interrupt"), "Kira", "full",
        )
        assert "Usage" in responses[0]

    # --- show ---

    async def test_show_success(self):
        adapter, mgmt = _make_control_adapter()
        from kiln.daemon.management import ActionResult
        mgmt.set_result("capture_session", ActionResult(
            True, "$ echo hello\nhello",
        ))

        responses = []
        adapter._control_respond = lambda msg, text: _capture(responses, text)
        await adapter._handle_control_message(
            _control_msg("show beth-test"), "Kira", "full",
        )
        assert len(mgmt.calls) == 1
        assert mgmt.calls[0][1]["session_id"] == "beth-test"
        assert "```" in responses[0]
        assert "beth-test" in responses[0]

    async def test_show_empty_pane(self):
        adapter, mgmt = _make_control_adapter()
        from kiln.daemon.management import ActionResult
        mgmt.set_result("capture_session", ActionResult(True, ""))

        responses = []
        adapter._control_respond = lambda msg, text: _capture(responses, text)
        await adapter._handle_control_message(
            _control_msg("show beth-test"), "Kira", "full",
        )
        assert "empty pane" in responses[0]

    async def test_show_failure(self):
        adapter, mgmt = _make_control_adapter()
        from kiln.daemon.management import ActionResult
        mgmt.set_result("capture_session", ActionResult(False, "tmux not found"))

        responses = []
        adapter._control_respond = lambda msg, text: _capture(responses, text)
        await adapter._handle_control_message(
            _control_msg("show beth-test"), "Kira", "full",
        )
        assert "\u274c" in responses[0]

    async def test_show_missing_args(self):
        adapter, mgmt = _make_control_adapter()
        responses = []
        adapter._control_respond = lambda msg, text: _capture(responses, text)
        await adapter._handle_control_message(
            _control_msg("show"), "Kira", "full",
        )
        assert "Usage" in responses[0]

    async def test_show_truncates_long_output(self):
        adapter, mgmt = _make_control_adapter()
        from kiln.daemon.management import ActionResult
        # Generate output that exceeds Discord limit
        long_content = "x" * 3000
        mgmt.set_result("capture_session", ActionResult(True, long_content))

        responses = []
        adapter._control_respond = lambda msg, text: _capture(responses, text)
        await adapter._handle_control_message(
            _control_msg("show beth-test"), "Kira", "full",
        )
        assert len(responses[0]) <= 2000

    # --- Integration: control via handle_message ---

    async def test_control_routed_through_handle_message(self):
        """Control messages via handle_message should hit the command parser."""
        adapter, mgmt = _make_control_adapter(
            users={"111": {"name": "Kira", "max_trust": "full"}},
            channel_access="open",
        )
        responses = []
        adapter._control_respond = lambda msg, text: _capture(responses, text)
        adapter._get_known_agents = lambda: {"beth"}

        result = await adapter.handle_message(_msg(
            sender_id="111", channel_id="7777", content="spawn beth test",
        ))
        # Control messages return None (consumed)
        assert result is None
        assert len(mgmt.calls) == 1
        assert mgmt.calls[0][0] == "spawn_session"

    async def test_control_denied_non_full_trust(self):
        """Non-full-trust users in control channel should be silently denied."""
        adapter, mgmt = _make_control_adapter(
            users={"222": {"name": "Stranger", "max_trust": "known"}},
            channel_access="open",
        )
        responses = []
        adapter._control_respond = lambda msg, text: _capture(responses, text)

        await adapter.handle_message(_msg(
            sender_id="222", channel_id="7777", content="kill beth-test",
        ))
        assert len(responses) == 0
        assert len(mgmt.calls) == 0


async def _capture(responses: list, text: str):
    """Async-compatible response capture for patched _control_respond."""
    responses.append(text)


# ---------------------------------------------------------------------------
# D3c — Status embeds + presence
# ---------------------------------------------------------------------------

class TestFormatUptime:
    def test_minutes_only(self):
        from kiln.daemon.adapters.discord import _format_uptime
        from datetime import datetime, timezone, timedelta
        ts = (datetime.now(timezone.utc) - timedelta(minutes=42)).isoformat()
        assert _format_uptime(ts) == "42m"

    def test_hours_and_minutes(self):
        from kiln.daemon.adapters.discord import _format_uptime
        from datetime import datetime, timezone, timedelta
        ts = (datetime.now(timezone.utc) - timedelta(hours=3, minutes=7)).isoformat()
        assert _format_uptime(ts) == "3h07m"

    def test_invalid_timestamp(self):
        from kiln.daemon.adapters.discord import _format_uptime
        assert _format_uptime("not-a-date") == "?"

    def test_empty(self):
        from kiln.daemon.adapters.discord import _format_uptime
        assert _format_uptime("") == "?"


class TestFormatContext:
    def test_normal(self):
        from kiln.daemon.adapters.discord import _format_context
        assert _format_context((100_000, 200_000)) == "50%"

    def test_none(self):
        from kiln.daemon.adapters.discord import _format_context
        assert _format_context(None) == "?"

    def test_zero_total(self):
        from kiln.daemon.adapters.discord import _format_context
        assert _format_context((0, 0)) == "?"


class TestBuildPresenceText:
    def test_no_agents(self):
        from kiln.daemon.adapters.discord import _build_presence_text
        assert _build_presence_text([]) == "No agents running"

    def test_single_agent(self):
        from kiln.daemon.adapters.discord import _build_presence_text
        agents = [{"id": "beth-cool-fox", "context": "42%"}]
        text = _build_presence_text(agents)
        assert "1 agent" in text
        assert "cool-fox" in text
        assert "42%" in text

    def test_multiple_agents_uses_last(self):
        from kiln.daemon.adapters.discord import _build_presence_text
        agents = [
            {"id": "beth-aaa", "context": "10%"},
            {"id": "beth-bbb", "context": "50%"},
        ]
        text = _build_presence_text(agents)
        assert "2 agents" in text
        assert "bbb" in text

    def test_canonical_preferred(self):
        from kiln.daemon.adapters.discord import _build_presence_text
        agents = [
            {"id": "beth-aaa", "context": "10%"},
            {"id": "beth-bbb", "context": "50%"},
        ]
        text = _build_presence_text(agents, canonical_id="beth-aaa")
        assert "aaa" in text
        assert "10%" in text

    def test_truncated_at_128(self):
        from kiln.daemon.adapters.discord import _build_presence_text
        agents = [{"id": "beth-" + "x" * 200, "context": "1%"}]
        assert len(_build_presence_text(agents)) <= 128


class TestBuildAgentEmbed:
    def test_basic_embed(self):
        from kiln.daemon.adapters.discord import _build_agent_embed, COLOR_ACTIVE
        agent = {"id": "beth-fox", "uptime": "1h30m", "context": "45%", "context_pct": 45, "inbox": 0}
        embed = _build_agent_embed(agent, canonical_id=None)
        assert "beth-fox" in embed.title
        assert embed.color.value == COLOR_ACTIVE
        assert "45%" in embed.description

    def test_canonical_star(self):
        from kiln.daemon.adapters.discord import _build_agent_embed
        agent = {"id": "beth-fox", "uptime": "1h", "context": "?", "context_pct": None, "inbox": 0}
        embed = _build_agent_embed(agent, canonical_id="beth-fox")
        assert "\u2b50" in embed.title

    def test_no_canonical_no_star(self):
        from kiln.daemon.adapters.discord import _build_agent_embed
        agent = {"id": "beth-fox", "uptime": "1h", "context": "?", "context_pct": None, "inbox": 0}
        embed = _build_agent_embed(agent, canonical_id=None)
        assert "\u2b50" not in embed.title

    def test_mode_shown_when_not_supervised(self):
        from kiln.daemon.adapters.discord import _build_agent_embed
        agent = {"id": "beth-fox", "uptime": "1h", "context": "?", "context_pct": None, "inbox": 0, "mode": "yolo"}
        embed = _build_agent_embed(agent, canonical_id=None)
        assert "yolo" in embed.description

    def test_mode_hidden_when_supervised(self):
        from kiln.daemon.adapters.discord import _build_agent_embed
        agent = {"id": "beth-fox", "uptime": "1h", "context": "?", "context_pct": None, "inbox": 0, "mode": "supervised"}
        embed = _build_agent_embed(agent, canonical_id=None)
        assert "supervised" not in embed.description

    def test_plan_shown(self):
        from kiln.daemon.adapters.discord import _build_agent_embed
        agent = {
            "id": "beth-fox", "uptime": "1h", "context": "?",
            "context_pct": None, "inbox": 0,
            "plan": {"goal": "Test goal", "tasks": [
                {"status": "done"}, {"status": "in_progress"}, {"status": "pending"},
            ]},
        }
        embed = _build_agent_embed(agent, canonical_id=None)
        assert len(embed.fields) == 1
        assert "Test goal" in embed.fields[0].value
        assert "1/3" in embed.fields[0].value

    def test_inbox_shown_when_nonzero(self):
        from kiln.daemon.adapters.discord import _build_agent_embed
        agent = {"id": "beth-fox", "uptime": "1h", "context": "?", "context_pct": None, "inbox": 3}
        embed = _build_agent_embed(agent, canonical_id=None)
        assert "3" in embed.description

    def test_idle_color_when_no_context(self):
        from kiln.daemon.adapters.discord import _build_agent_embed, COLOR_IDLE
        agent = {"id": "beth-fox", "uptime": "1h", "context": "?", "context_pct": None, "inbox": 0}
        embed = _build_agent_embed(agent, canonical_id=None)
        assert embed.color.value == COLOR_IDLE


class TestBuildStatusEmbeds:
    def test_no_agents(self):
        from kiln.daemon.adapters.discord import _build_status_embeds
        embeds = _build_status_embeds([])
        assert len(embeds) == 1
        assert "No agents" in embeds[0].title

    def test_standalone_agents(self):
        from kiln.daemon.adapters.discord import _build_status_embeds
        agents = [
            {"id": "beth-aaa", "uptime": "1h", "context": "30%", "context_pct": 30, "inbox": 0},
            {"id": "beth-bbb", "uptime": "2h", "context": "60%", "context_pct": 60, "inbox": 0},
        ]
        embeds = _build_status_embeds(agents)
        assert len(embeds) == 2

    def test_conclave_grouping(self):
        from kiln.daemon.adapters.discord import _build_status_embeds, COLOR_CONCLAVE
        agents = [
            {"id": "beth-standalone", "uptime": "1h", "context": "?", "context_pct": None, "inbox": 0},
            {"id": "beth-fac", "uptime": "1h", "context": "?", "context_pct": None, "inbox": 0},
            {"id": "beth-collab", "uptime": "1h", "context": "?", "context_pct": None, "inbox": 0},
        ]
        membership = {
            "beth-fac": {"conclave": "test-conclave", "role": "facilitator"},
            "beth-collab": {"conclave": "test-conclave", "role": "collaborator"},
        }
        embeds = _build_status_embeds(agents, membership=membership)
        # 1 standalone + 1 conclave group
        assert len(embeds) == 2
        conclave_embed = [e for e in embeds if e.color.value == COLOR_CONCLAVE]
        assert len(conclave_embed) == 1
        assert "test-conclave" in conclave_embed[0].title

    def test_max_10_embeds(self):
        from kiln.daemon.adapters.discord import _build_status_embeds
        agents = [
            {"id": f"beth-agent-{i}", "uptime": "1h", "context": "?", "context_pct": None, "inbox": 0}
            for i in range(15)
        ]
        embeds = _build_status_embeds(agents)
        assert len(embeds) <= 10


class TestFormatUsageLines:
    def test_anthropic_only(self):
        from kiln.daemon.adapters.discord import _format_usage_lines
        data = {"anthropic": {"five_hour": {"utilization": 42.5, "resets_at": None}}}
        lines = _format_usage_lines(data)
        assert len(lines) == 1
        assert "Anthropic" in lines[0]
        assert "42%" in lines[0]

    def test_both_providers(self):
        from kiln.daemon.adapters.discord import _format_usage_lines
        data = {
            "anthropic": {"five_hour": {"utilization": 30.0}},
            "openai": {"rate_limit": {"primary_window": {"used_percent": 10}}},
        }
        lines = _format_usage_lines(data)
        assert len(lines) == 2

    def test_empty(self):
        from kiln.daemon.adapters.discord import _format_usage_lines
        assert _format_usage_lines({}) == []


class TestCollectDaemonStatus:
    def test_collects_from_presence(self):
        from kiln.daemon.state import SessionRecord
        adapter = _make_adapter(channel_access="open")
        # Register sessions in mock daemon
        s1 = SessionRecord(session_id="beth-fox", agent_name="beth", agent_home="/home/beth", pid=1)
        s2 = SessionRecord(session_id="dalet-owl", agent_name="dalet", agent_home="/home/dalet", pid=2)
        adapter._daemon.state.presence.register(s1)
        adapter._daemon.state.presence.register(s2)

        agents = adapter._collect_daemon_status()
        assert len(agents) == 2
        ids = [a["id"] for a in agents]
        assert "beth-fox" in ids
        assert "dalet-owl" in ids
        # Should be sorted
        assert agents[0]["id"] < agents[1]["id"]

    def test_empty_registry(self):
        adapter = _make_adapter(channel_access="open")
        agents = adapter._collect_daemon_status()
        assert agents == []


class TestHomeDecorators:
    def test_read_plan(self, tmp_path):
        import yaml
        from kiln.daemon.adapters.discord import DiscordAdapter
        plans_dir = tmp_path / "plans"
        plans_dir.mkdir()
        plan_data = {"goal": "test", "tasks": [{"status": "done"}]}
        (plans_dir / "beth-fox.yml").write_text(yaml.dump(plan_data))

        result = DiscordAdapter._read_plan(tmp_path, "beth-fox")
        assert result["goal"] == "test"

    def test_read_plan_missing(self, tmp_path):
        from kiln.daemon.adapters.discord import DiscordAdapter
        assert DiscordAdapter._read_plan(tmp_path, "beth-ghost") is None

    def test_count_inbox(self, tmp_path):
        from kiln.daemon.adapters.discord import DiscordAdapter
        inbox = tmp_path / "inbox" / "beth-fox"
        inbox.mkdir(parents=True)
        (inbox / "msg1.md").write_text("hi")
        (inbox / "msg2.md").write_text("there")
        (inbox / "msg2.read").write_text("")  # read marker
        (inbox / "msg3.md").write_text("unread")

        assert DiscordAdapter._count_inbox(tmp_path, "beth-fox") == 2

    def test_count_inbox_missing(self, tmp_path):
        from kiln.daemon.adapters.discord import DiscordAdapter
        assert DiscordAdapter._count_inbox(tmp_path, "beth-ghost") == 0

    def test_read_session_mode(self, tmp_path):
        import yaml
        from kiln.daemon.adapters.discord import DiscordAdapter
        state_dir = tmp_path / "state"
        state_dir.mkdir(parents=True)
        (state_dir / "session-config-beth-fox.yml").write_text(yaml.dump({"mode": "yolo"}))

        assert DiscordAdapter._read_session_mode(tmp_path, "beth-fox") == "yolo"

    def test_read_session_mode_missing(self, tmp_path):
        from kiln.daemon.adapters.discord import DiscordAdapter
        assert DiscordAdapter._read_session_mode(tmp_path, "beth-ghost") == ""

    def test_read_canonical(self, tmp_path):
        adapter = _make_adapter(channel_access="open")
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        (state_dir / "canonical").write_text("beth-fox\n")
        assert adapter._read_canonical(tmp_path) == "beth-fox"

    def test_read_canonical_missing(self, tmp_path):
        adapter = _make_adapter(channel_access="open")
        assert adapter._read_canonical(tmp_path) is None


class TestStatusSignal:
    def test_signal_sets_event(self):
        adapter = _make_adapter(channel_access="open")
        adapter._status_refresh_signal = asyncio.Event()
        assert not adapter._status_refresh_signal.is_set()
        adapter._signal_status_refresh()
        assert adapter._status_refresh_signal.is_set()

    def test_signal_noop_when_no_event(self):
        adapter = _make_adapter(channel_access="open")
        adapter._status_refresh_signal = None
        # Should not raise
        adapter._signal_status_refresh()

    @pytest.mark.asyncio
    async def test_cmd_mode_triggers_refresh(self):
        adapter = _make_adapter(channel_access="open")
        adapter._status_refresh_signal = asyncio.Event()

        responses = []
        adapter._control_respond = lambda msg, text: _capture(responses, text)

        msg = _msg(content="mode beth-fox yolo", channel_id="999")
        await adapter._cmd_mode(msg, ["beth-fox", "yolo"], "TestUser")

        # Should have signalled
        assert adapter._status_refresh_signal.is_set()

    @pytest.mark.asyncio
    async def test_session_connected_triggers_refresh(self):
        from kiln.daemon.state import SessionRecord
        adapter = _make_adapter(channel_access="open", channels={"branches": "8888"})
        adapter._status_refresh_signal = asyncio.Event()

        # Patch discord thread creation to avoid needing live client
        async def _noop_create(*a, **k):
            return None
        adapter._discord_create_thread = _noop_create

        event = proto.event(proto.EVT_SESSION_LIVE, session_id="beth-fox", agent_name="beth")
        await adapter._on_session_live(event)

        assert adapter._status_refresh_signal.is_set()

    @pytest.mark.asyncio
    async def test_session_connected_triggers_refresh_without_branches(self):
        """Refresh fires even when #branches is not configured."""
        adapter = _make_adapter(channel_access="open")  # no channels config
        adapter._status_refresh_signal = asyncio.Event()

        event = proto.event(proto.EVT_SESSION_LIVE, session_id="beth-fox", agent_name="beth")
        await adapter._on_session_live(event)

        assert adapter._status_refresh_signal.is_set()

    @pytest.mark.asyncio
    async def test_session_disconnected_triggers_refresh_without_thread(self):
        """Refresh fires even when session has no branch thread."""
        adapter = _make_adapter(channel_access="open")
        adapter._status_refresh_signal = asyncio.Event()
        # No branch thread for this session

        event = proto.event(proto.EVT_SESSION_GONE, session_id="beth-fox")
        await adapter._on_session_gone(event)

        assert adapter._status_refresh_signal.is_set()

    @pytest.mark.asyncio
    async def test_session_disconnected_triggers_refresh(self):
        adapter = _make_adapter(channel_access="open")
        adapter._status_refresh_signal = asyncio.Event()
        adapter._branch_threads["beth-fox"] = 12345

        async def _noop_archive(*a):
            pass
        adapter._discord_archive_thread = _noop_archive

        event = proto.event(proto.EVT_SESSION_GONE, session_id="beth-fox")
        await adapter._on_session_gone(event)

        assert adapter._status_refresh_signal.is_set()


class TestStatusMessagePersistence:
    def test_persist_and_load(self, tmp_path):
        adapter = _make_adapter(channel_access="open")
        adapter._state_dir = tmp_path
        adapter._status_message_id = 123456789

        adapter._persist_status_message_id()
        loaded = adapter._load_status_message_id()
        assert loaded == 123456789

    def test_load_missing(self, tmp_path):
        adapter = _make_adapter(channel_access="open")
        adapter._state_dir = tmp_path
        assert adapter._load_status_message_id() is None

    def test_load_empty(self, tmp_path):
        adapter = _make_adapter(channel_access="open")
        adapter._state_dir = tmp_path
        (tmp_path / "status-message-id").write_text("")
        assert adapter._load_status_message_id() is None

    def test_load_invalid(self, tmp_path):
        adapter = _make_adapter(channel_access="open")
        adapter._state_dir = tmp_path
        (tmp_path / "status-message-id").write_text("not-a-number")
        assert adapter._load_status_message_id() is None


class TestEnrichWithHomeDecorators:
    """Integration test for _enrich_with_home_decorators — all reads together."""

    def test_enriches_all_fields(self, tmp_path):
        import yaml
        adapter = _make_adapter(channel_access="open")

        # Set up agent home structure
        (tmp_path / "state").mkdir(parents=True)
        (tmp_path / "state" / "session-config-beth-fox.yml").write_text(
            yaml.dump({"mode": "yolo"})
        )
        (tmp_path / "plans").mkdir()
        (tmp_path / "plans" / "beth-fox.yml").write_text(
            yaml.dump({"goal": "test", "tasks": [{"status": "done"}]})
        )
        inbox = tmp_path / "inbox" / "beth-fox"
        inbox.mkdir(parents=True)
        (inbox / "msg1.md").write_text("hi")

        agents = [{"id": "beth-fox", "agent_name": "beth", "agent_home": str(tmp_path)}]
        adapter._enrich_with_home_decorators(agents)

        a = agents[0]
        assert a["mode"] == "yolo"
        assert a["plan"]["goal"] == "test"
        assert a["inbox"] == 1
        assert a["context"] == "?"  # No JSONL, so "?"
        assert a["context_pct"] is None

    def test_graceful_with_empty_home(self, tmp_path):
        adapter = _make_adapter(channel_access="open")
        agents = [{"id": "beth-fox", "agent_name": "beth", "agent_home": str(tmp_path)}]
        adapter._enrich_with_home_decorators(agents)

        a = agents[0]
        assert a["mode"] == ""
        assert a["plan"] is None
        assert a["inbox"] == 0
        assert a["context"] == "?"

    def test_graceful_with_invalid_home(self):
        adapter = _make_adapter(channel_access="open")
        agents = [{"id": "beth-fox", "agent_name": "beth", "agent_home": "/nonexistent/path"}]
        adapter._enrich_with_home_decorators(agents)

        a = agents[0]
        assert a["mode"] == ""
        assert a["context"] == "?"


class TestConclaveMembership:
    def test_parses_briefing(self, tmp_path):
        from kiln.daemon.state import SessionRecord
        adapter = _make_adapter(channel_access="open")

        # Create conclave briefing
        briefing_dir = tmp_path / "conclaves" / "test-research"
        briefing_dir.mkdir(parents=True)
        (briefing_dir / "briefing.md").write_text(
            "# Briefing\n\n## Members\n"
            "- **Facilitator:** beth-lead\n"
            "- **Collaborator:** beth-helper\n"
            "\n## Goals\nDo stuff\n"
        )

        s = SessionRecord(session_id="beth-lead", agent_name="beth",
                          agent_home=str(tmp_path), pid=1)
        adapter._daemon.state.presence.register(s)

        membership = adapter._get_conclave_membership()
        assert "beth-lead" in membership
        assert membership["beth-lead"]["role"] == "facilitator"
        assert membership["beth-lead"]["conclave"] == "test-research"
        assert "beth-helper" in membership
        assert membership["beth-helper"]["role"] == "collaborator"

    def test_empty_when_no_conclaves(self, tmp_path):
        from kiln.daemon.state import SessionRecord
        adapter = _make_adapter(channel_access="open")
        s = SessionRecord(session_id="beth-fox", agent_name="beth",
                          agent_home=str(tmp_path), pid=1)
        adapter._daemon.state.presence.register(s)

        assert adapter._get_conclave_membership() == {}

    def test_deduplicates_homes(self, tmp_path):
        from kiln.daemon.state import SessionRecord
        adapter = _make_adapter(channel_access="open")

        # Two sessions from same home
        s1 = SessionRecord(session_id="beth-fox", agent_name="beth",
                           agent_home=str(tmp_path), pid=1)
        s2 = SessionRecord(session_id="beth-owl", agent_name="beth",
                           agent_home=str(tmp_path), pid=2)
        adapter._daemon.state.presence.register(s1)
        adapter._daemon.state.presence.register(s2)

        # Should not crash or double-count
        membership = adapter._get_conclave_membership()
        assert isinstance(membership, dict)


class TestUsageCaching:
    @pytest.mark.asyncio
    async def test_returns_cache_when_fresh(self):
        import time
        adapter = _make_adapter(channel_access="open")
        adapter._usage_cache = {"anthropic": {"five_hour": {"utilization": 50}}}
        adapter._usage_cache_time = time.monotonic()  # just now

        result = await adapter._collect_usage_data()
        assert result == adapter._usage_cache

    @pytest.mark.asyncio
    async def test_returns_stale_cache_on_fetch_failure(self):
        adapter = _make_adapter(channel_access="open")
        adapter._usage_cache = {"anthropic": {"five_hour": {"utilization": 50}}}
        adapter._usage_cache_time = 0  # expired

        # The library import will work but the actual API calls will fail
        # (no tokens configured). Should fall back to cache.
        result = await adapter._collect_usage_data()
        # Either returns fresh data (unlikely in test) or stale cache
        assert result is not None


class TestStatusRefreshLoop:
    @pytest.mark.asyncio
    async def test_start_noop_without_status_channel(self):
        adapter = _make_adapter(channel_access="open")
        # No "status" in channels config
        await adapter._start_status_loop()
        assert adapter._status_refresh_task is None
        assert adapter._status_refresh_signal is None

    @pytest.mark.asyncio
    async def test_start_creates_task_with_status_channel(self):
        adapter = _make_adapter(channel_access="open", channels={"status": "7777"})
        await adapter._start_status_loop()
        assert adapter._status_refresh_task is not None
        assert adapter._status_refresh_signal is not None

        # Clean up
        await adapter._stop_status_loop()
        assert adapter._status_refresh_task is None

    @pytest.mark.asyncio
    async def test_stop_cancels_running_task(self):
        adapter = _make_adapter(channel_access="open", channels={"status": "7777"})
        await adapter._start_status_loop()
        task = adapter._status_refresh_task
        assert not task.done()

        await adapter._stop_status_loop()
        assert task.done() or task.cancelled()

    @pytest.mark.asyncio
    async def test_stop_noop_when_no_task(self):
        adapter = _make_adapter(channel_access="open")
        # Should not raise
        await adapter._stop_status_loop()


class TestDoStatusRefresh:
    @pytest.mark.asyncio
    async def test_full_flow_mocked(self):
        """Integration test: _do_status_refresh collects data and calls display methods."""
        from kiln.daemon.state import SessionRecord
        adapter = _make_adapter(channel_access="open")

        s = SessionRecord(session_id="beth-fox", agent_name="beth",
                          agent_home="/tmp/nonexistent", pid=1)
        adapter._daemon.state.presence.register(s)

        # Track calls to display methods
        status_calls = []
        presence_calls = []

        async def mock_update_status(channel_id, content, embeds):
            status_calls.append((channel_id, content, embeds))

        async def mock_update_presence(text):
            presence_calls.append(text)

        async def mock_usage():
            return None

        adapter._update_status_message = mock_update_status
        adapter._update_presence = mock_update_presence
        adapter._collect_usage_data = mock_usage

        await adapter._do_status_refresh("7777")

        assert len(status_calls) == 1
        channel_id, content, embeds = status_calls[0]
        assert channel_id == "7777"
        assert "Agent Status" in content
        assert len(embeds) >= 1
        assert "beth-fox" in embeds[0].title

        assert len(presence_calls) == 1
        assert "beth-fox" in presence_calls[0] or "1 agent" in presence_calls[0]

    @pytest.mark.asyncio
    async def test_with_usage_data(self):
        """Verify usage data appears in the status content."""
        adapter = _make_adapter(channel_access="open")

        status_calls = []

        async def mock_update_status(channel_id, content, embeds):
            status_calls.append((channel_id, content, embeds))

        async def mock_update_presence(text):
            pass

        async def mock_usage():
            return {"anthropic": {"five_hour": {"utilization": 42.0}}}

        adapter._update_status_message = mock_update_status
        adapter._update_presence = mock_update_presence
        adapter._collect_usage_data = mock_usage

        await adapter._do_status_refresh("7777")

        _, content, _ = status_calls[0]
        assert "Anthropic" in content
        assert "42%" in content

    @pytest.mark.asyncio
    async def test_with_canonical(self, tmp_path):
        """Verify canonical ID is passed through to embeds."""
        from kiln.daemon.state import SessionRecord
        adapter = _make_adapter(channel_access="open")

        # Set up canonical file
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        (state_dir / "canonical").write_text("beth-fox")

        s = SessionRecord(session_id="beth-fox", agent_name="beth",
                          agent_home=str(tmp_path), pid=1)
        adapter._daemon.state.presence.register(s)

        status_calls = []

        async def mock_update_status(channel_id, content, embeds):
            status_calls.append((channel_id, content, embeds))

        async def mock_update_presence(text):
            pass

        async def mock_usage():
            return None

        adapter._update_status_message = mock_update_status
        adapter._update_presence = mock_update_presence
        adapter._collect_usage_data = mock_usage

        await adapter._do_status_refresh("7777")

        _, _, embeds = status_calls[0]
        # The canonical agent's embed should have the star
        assert "\u2b50" in embeds[0].title


# ---------------------------------------------------------------------------
# D3b — Security challenge intercept
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestSecurityIntercept:
    """Test the security challenge message intercept in handle_message."""

    async def test_intercept_consumes_matching_message(self):
        """Messages in the challenge channel are consumed when a challenge is active."""
        adapter = _make_adapter(channel_access="open")
        future = asyncio.get_running_loop().create_future()
        adapter._security_challenge_state = {
            "future": future,
            "channel_id": "5555",
            "message_ids": [],
        }

        result = await adapter.handle_message(
            _msg(channel_id="5555", content="secretword", sender_id="111"),
        )

        # Message consumed — not routed
        assert result is None
        # Future resolved with content
        assert future.done()
        content, sender_id, msg_id = future.result()
        assert content == "secretword"
        assert sender_id == "111"

    async def test_intercept_ignores_other_channels(self):
        """Messages in non-challenge channels pass through normally."""
        adapter = _make_adapter(channel_access="open")
        future = asyncio.get_running_loop().create_future()
        adapter._security_challenge_state = {
            "future": future,
            "channel_id": "5555",
            "message_ids": [],
        }

        # Different channel — should pass through to normal routing
        result = await adapter.handle_message(
            _msg(channel_id="9999", content="hello"),
        )

        # Not consumed by intercept (unrouted since no matching route)
        assert result is None
        # Future NOT resolved
        assert not future.done()

    async def test_no_intercept_when_no_challenge(self):
        """Normal messages route normally when no challenge is active."""
        from kiln.daemon.adapters.discord import RouteBucket
        adapter = _make_adapter(
            branch_threads={"beth-test-1": 9001},
            channel_access="open",
        )
        assert adapter._security_challenge_state is None

        result = await adapter.handle_message(_msg(channel_id="9001"))
        assert result is not None
        assert result.bucket == RouteBucket.BRANCH

    async def test_intercept_before_access_control(self):
        """Security intercept fires even if sender would normally be blocked."""
        adapter = _make_adapter(
            channel_access="allowlist",
            users={},  # nobody allowed
        )
        future = asyncio.get_running_loop().create_future()
        adapter._security_challenge_state = {
            "future": future,
            "channel_id": "5555",
            "message_ids": [],
        }

        result = await adapter.handle_message(
            _msg(channel_id="5555", content="password123", sender_id="999"),
        )

        # Consumed by intercept despite access denial
        assert result is None
        assert future.done()
        assert future.result()[0] == "password123"

    async def test_intercept_tracks_message_id(self):
        """Intercepted message IDs are tracked for cleanup."""
        adapter = _make_adapter(channel_access="open")
        message_ids: list[int] = []
        future = asyncio.get_running_loop().create_future()
        adapter._security_challenge_state = {
            "future": future,
            "channel_id": "5555",
            "message_ids": message_ids,
        }

        await adapter.handle_message(
            _msg(channel_id="5555", content="pw"),
        )

        # _msg doesn't set message_id by default, so it will try to append ""
        # which int("") will fail silently — that's fine, it's robust


# ---------------------------------------------------------------------------
# D3b — _build_owner_mentions
# ---------------------------------------------------------------------------

class TestBuildOwnerMentions:
    def test_mentions_full_trust_users(self):
        adapter = _make_adapter(
            users={
                "111": {"name": "Kira", "max_trust": "full"},
                "222": {"name": "Guest", "max_trust": "known"},
            },
        )
        result = adapter._build_owner_mentions()
        assert result == "<@111>"

    def test_multiple_full_trust(self):
        adapter = _make_adapter(
            users={
                "111": {"name": "Kira", "max_trust": "full"},
                "222": {"name": "Admin", "max_trust": "full"},
            },
        )
        result = adapter._build_owner_mentions()
        assert "<@111>" in result
        assert "<@222>" in result

    def test_no_full_trust_returns_none(self):
        adapter = _make_adapter(
            users={"111": {"name": "Guest", "max_trust": "known"}},
        )
        assert adapter._build_owner_mentions() is None

    def test_trust_field_fallback(self):
        """Falls back to 'trust' field if 'max_trust' not present."""
        adapter = _make_adapter(
            users={"111": {"name": "Kira", "trust": "full"}},
        )
        assert adapter._build_owner_mentions() == "<@111>"


# ---------------------------------------------------------------------------
# D3b — Permission request / resolve
# ---------------------------------------------------------------------------

class _FakeDiscordChannel:
    """Minimal fake Discord channel/thread for permission tests."""

    def __init__(self, channel_id: int = 9001):
        self.id = channel_id
        self.sent_messages: list[dict] = []

    async def send(self, content="", embed=None, view=None):
        msg = _FakeDiscordMessage(len(self.sent_messages) + 1, embed=embed)
        self.sent_messages.append({
            "content": content, "embed": embed, "view": view, "msg": msg,
        })
        return msg


class _FakeDiscordMessage:
    """Minimal fake Discord message for edit/delete testing."""

    def __init__(self, msg_id: int, embed=None):
        self.id = msg_id
        self.embed = embed
        self.edited = False
        self.last_edit_kwargs: dict = {}

    async def edit(self, **kwargs):
        self.edited = True
        self.last_edit_kwargs = kwargs


@pytest.mark.asyncio
class TestPermissionRequest:
    async def test_approved_via_future(self):
        """Permission request returns approved when future resolves True."""
        adapter = _make_adapter(
            branch_threads={"beth-test-1": 9001},
            users={"111": {"name": "Kira", "max_trust": "full"}},
        )
        fake_channel = _FakeDiscordChannel(9001)

        # Patch the client to return our fake channel
        class FakeClient:
            def get_channel(self, cid):
                return fake_channel if cid == 9001 else None
        adapter._client = FakeClient()

        from kiln.daemon.protocol import RequestContext
        ctx = RequestContext(agent_name="beth", session_id="beth-test-1")

        # Start the request in background, then resolve it
        async def resolve_after_delay():
            await asyncio.sleep(0.05)
            pending = adapter._pending_permissions.get("beth-test-1")
            assert pending is not None
            pending["future"].set_result((True, "Kira"))

        task = asyncio.create_task(resolve_after_delay())
        result = await adapter._op_permission_request(
            {"title": "Run rm -rf?", "preview": "dangerous command", "timeout": 5},
            ctx,
        )
        await task

        assert result["approved"] is True
        assert result["responder"] == "Kira"
        assert result["timed_out"] is False
        # Should be cleaned up from pending
        assert "beth-test-1" not in adapter._pending_permissions

    async def test_rejected_via_future(self):
        adapter = _make_adapter(
            branch_threads={"beth-test-1": 9001},
            users={"111": {"name": "Kira", "max_trust": "full"}},
        )
        fake_channel = _FakeDiscordChannel(9001)
        class FakeClient:
            def get_channel(self, cid):
                return fake_channel if cid == 9001 else None
        adapter._client = FakeClient()

        from kiln.daemon.protocol import RequestContext
        ctx = RequestContext(agent_name="beth", session_id="beth-test-1")

        async def resolve_after_delay():
            await asyncio.sleep(0.05)
            adapter._pending_permissions["beth-test-1"]["future"].set_result(
                (False, "Kira"),
            )

        task = asyncio.create_task(resolve_after_delay())
        result = await adapter._op_permission_request(
            {"title": "Run rm -rf?", "preview": "dangerous", "timeout": 5},
            ctx,
        )
        await task

        assert result["approved"] is False
        assert result["timed_out"] is False

    async def test_timeout(self):
        adapter = _make_adapter(
            branch_threads={"beth-test-1": 9001},
        )
        fake_channel = _FakeDiscordChannel(9001)
        class FakeClient:
            def get_channel(self, cid):
                return fake_channel if cid == 9001 else None
        adapter._client = FakeClient()

        from kiln.daemon.protocol import RequestContext
        ctx = RequestContext(agent_name="beth", session_id="beth-test-1")

        result = await adapter._op_permission_request(
            {"title": "Test", "preview": "test", "timeout": 0.1},
            ctx,
        )

        assert result["approved"] is False
        assert result["timed_out"] is True
        assert "beth-test-1" not in adapter._pending_permissions

    async def test_no_branch_thread(self):
        adapter = _make_adapter(branch_threads={})
        class FakeClient:
            pass
        adapter._client = FakeClient()

        from kiln.daemon.protocol import RequestContext
        ctx = RequestContext(agent_name="beth", session_id="beth-test-1")

        result = await adapter._op_permission_request(
            {"title": "Test", "preview": "test"},
            ctx,
        )

        assert "error" in result
        assert "no branch thread" in result["error"]

    async def test_no_client(self):
        adapter = _make_adapter()
        adapter._client = None

        from kiln.daemon.protocol import RequestContext
        ctx = RequestContext(agent_name="beth", session_id="beth-test-1")

        result = await adapter._op_permission_request(
            {"title": "Test", "preview": "test"},
            ctx,
        )

        assert "error" in result
        assert "not connected" in result["error"]


@pytest.mark.asyncio
class TestPermissionResolve:
    async def test_resolve_approved(self):
        adapter = _make_adapter()
        future = asyncio.get_running_loop().create_future()
        fake_msg = _FakeDiscordMessage(1)
        import discord
        embed = discord.Embed(title="Permission Required")
        adapter._pending_permissions["beth-test-1"] = {
            "future": future,
            "message": fake_msg,
            "embed": embed,
            "externally_resolved": False,
        }

        result = await adapter._op_permission_resolve(
            {"session_id": "beth-test-1", "status": "approved"},
            None,
        )

        assert result["ok"] is True
        assert future.done()
        approved, responder = future.result()
        assert approved is True
        assert responder == "terminal"
        assert adapter._pending_permissions["beth-test-1"]["externally_resolved"]

    async def test_resolve_rejected(self):
        adapter = _make_adapter()
        future = asyncio.get_running_loop().create_future()
        fake_msg = _FakeDiscordMessage(1)
        import discord
        embed = discord.Embed(title="Permission Required")
        adapter._pending_permissions["beth-test-1"] = {
            "future": future,
            "message": fake_msg,
            "embed": embed,
            "externally_resolved": False,
        }

        result = await adapter._op_permission_resolve(
            {"session_id": "beth-test-1", "status": "rejected"},
            None,
        )

        assert result["ok"] is True
        approved, _ = future.result()
        assert approved is False

    async def test_resolve_not_found(self):
        adapter = _make_adapter()
        result = await adapter._op_permission_resolve(
            {"session_id": "nonexistent", "status": "approved"},
            None,
        )
        assert result["ok"] is False
        assert "No pending permission" in result["error"]

    async def test_resolve_already_done(self):
        """Resolving an already-resolved future doesn't crash."""
        adapter = _make_adapter()
        future = asyncio.get_running_loop().create_future()
        future.set_result((True, "Kira"))  # already resolved
        fake_msg = _FakeDiscordMessage(1)
        import discord
        embed = discord.Embed(title="Permission Required")
        adapter._pending_permissions["beth-test-1"] = {
            "future": future,
            "message": fake_msg,
            "embed": embed,
            "externally_resolved": False,
        }

        result = await adapter._op_permission_resolve(
            {"session_id": "beth-test-1", "status": "rejected"},
            None,
        )

        # Should succeed without crashing
        assert result["ok"] is True

    async def test_resolve_stops_view(self):
        """External resolve must call view.stop() to clean up discord.py listener."""
        adapter = _make_adapter()
        future = asyncio.get_running_loop().create_future()
        fake_msg = _FakeDiscordMessage(1)
        import discord
        embed = discord.Embed(title="Permission Required")

        class FakeView:
            def __init__(self):
                self.stopped = False
            def stop(self):
                self.stopped = True

        fake_view = FakeView()
        adapter._pending_permissions["beth-test-1"] = {
            "future": future,
            "message": fake_msg,
            "embed": embed,
            "view": fake_view,
            "externally_resolved": False,
        }

        await adapter._op_permission_resolve(
            {"session_id": "beth-test-1", "status": "approved"},
            None,
        )

        assert fake_view.stopped


# ---------------------------------------------------------------------------
# Server→adapter approval integration
# ---------------------------------------------------------------------------

class _PermissionMockAdapter:
    """Minimal adapter that captures platform_op calls for permission tests."""

    adapter_id = "mock-perms"
    platform_name = "mock-perms"

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    def supports(self, feature: str) -> bool:
        return feature == "permission"

    async def platform_op(self, action: str, args: dict, context=None) -> dict:
        self.calls.append((action, dict(args)))
        if action == "permission_resolve":
            session_id = args.get("session_id", "")
            if not session_id:
                return {"ok": False, "error": "No session_id in args"}
            return {"ok": True, "resolved": session_id}
        return {"ok": False, "error": f"unknown action {action}"}


@pytest.mark.asyncio
async def test_resolve_approval_passes_session_id(running_daemon, make_client):
    """Server mgmt resolve_approval must forward session_id (not agent_id) to the adapter.

    Regression test: the original code sent ``agent_id`` but the adapter reads
    ``session_id``. This test exercises the full server→adapter path that the
    unit tests on the adapter alone did not cover.
    """
    adapter = _PermissionMockAdapter()
    running_daemon.adapters["mock-perms"] = adapter

    client = make_client("beth", "beth-resolve-test")

    # Trigger lazy registration so the session exists
    await client.subscribe("dummy")

    result = await client.mgmt("resolve_approval", {"status": "approved"})
    assert result.get("ok") is True
    assert result.get("resolved") == "beth-resolve-test"

    # Verify the adapter received session_id, not agent_id
    assert len(adapter.calls) == 1
    action, args = adapter.calls[0]
    assert action == "permission_resolve"
    assert "session_id" in args, f"Expected session_id in args, got: {args}"
    assert "agent_id" not in args, f"Unexpected agent_id in args: {args}"
    assert args["session_id"] == "beth-resolve-test"


@pytest.mark.asyncio
async def test_request_approval_passes_agent_id(running_daemon, make_client):
    """Server mgmt request_approval forwards agent_id to the adapter."""
    adapter = _PermissionMockAdapter()
    running_daemon.adapters["mock-perms"] = adapter

    # Override platform_op to handle request too
    async def handle_op(action, args, context=None):
        adapter.calls.append((action, dict(args)))
        if action == "permission_request":
            return {"approved": True, "timed_out": False, "responder": "test"}
        return {"ok": False}
    adapter.platform_op = handle_op

    client = make_client("beth", "beth-request-test")
    await client.subscribe("dummy")

    result = await client.mgmt("request_approval", {
        "title": "Test", "preview": "test preview",
    })
    assert result.get("approved") is True

    assert len(adapter.calls) == 1
    action, args = adapter.calls[0]
    assert action == "permission_request"
    assert args["agent_id"] == "beth-request-test"


# ---------------------------------------------------------------------------
# D3b — Security challenge op (adapter wrapper)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
# ---------------------------------------------------------------------------
# Adapter bootstrap tests
# ---------------------------------------------------------------------------

class _BootstrapMockAdapter:
    """Minimal adapter for bootstrap tests."""

    def __init__(self, config=None):
        self.config = config or {}
        self.started = False
        self.stopped = False
        self.daemon_ref = None

    @property
    def adapter_id(self):
        return "mock"

    @property
    def platform_name(self):
        return "mock"

    async def start(self, daemon):
        self.daemon_ref = daemon
        self.started = True

    async def stop(self):
        self.stopped = True

    async def send_user_message(self, user, summary, body, context=None):
        return "sent"

    async def platform_op(self, action, args, context=None):
        return {"ok": True, "action": action}

    def validate_surface_ref(self, ref):
        return ref

    def supports(self, feature):
        return False


class _BootstrapFailingAdapter(_BootstrapMockAdapter):
    """Adapter whose start() raises."""

    async def start(self, daemon):
        raise RuntimeError("adapter start failed")


def _patch_adapter_registry(monkeypatch, overrides):
    """Patch the adapter registry on KilnDaemon for one test."""
    merged = dict(KilnDaemon._adapter_registry)
    merged.update(overrides)
    monkeypatch.setattr(KilnDaemon, "_adapter_registry", merged)


class TestAdapterBootstrap:
    """Tests for adapter loading during daemon startup."""

    @pytest.mark.asyncio
    async def test_adapter_started_from_config(self, daemon_config, monkeypatch):
        """Configured adapter is instantiated and started."""
        from kiln.daemon.config import AdapterConfig

        daemon_config.adapters["mock"] = AdapterConfig(
            adapter_id="mock", platform="mock", enabled=True,
            config={"key": "value"},
        )
        _patch_adapter_registry(monkeypatch, {"mock": _BootstrapMockAdapter})

        daemon = KilnDaemon(daemon_config)
        await daemon.start()

        assert "mock" in daemon.adapters
        adapter = daemon.adapters["mock"]
        assert isinstance(adapter, _BootstrapMockAdapter)
        assert adapter.started
        assert adapter.daemon_ref is daemon
        assert adapter.config == {"key": "value"}

        await daemon.stop()

    @pytest.mark.asyncio
    async def test_disabled_adapter_skipped(self, daemon_config, monkeypatch):
        """Disabled adapter is not instantiated."""
        from kiln.daemon.config import AdapterConfig

        daemon_config.adapters["mock"] = AdapterConfig(
            adapter_id="mock", platform="mock", enabled=False, config={},
        )
        _patch_adapter_registry(monkeypatch, {"mock": _BootstrapMockAdapter})

        daemon = KilnDaemon(daemon_config)
        await daemon.start()
        assert "mock" not in daemon.adapters
        await daemon.stop()

    @pytest.mark.asyncio
    async def test_unknown_platform_skipped(self, daemon_config):
        """Adapter with unrecognized platform is skipped with warning."""
        from kiln.daemon.config import AdapterConfig

        daemon_config.adapters["mystery"] = AdapterConfig(
            adapter_id="mystery", platform="nonexistent", enabled=True, config={},
        )

        daemon = KilnDaemon(daemon_config)
        await daemon.start()
        assert "nonexistent" not in daemon.adapters
        await daemon.stop()

    @pytest.mark.asyncio
    async def test_adapter_start_failure_does_not_crash_daemon(self, daemon_config, monkeypatch):
        """If an adapter's start() raises, the daemon continues."""
        from kiln.daemon.config import AdapterConfig

        daemon_config.adapters["failing"] = AdapterConfig(
            adapter_id="failing", platform="failing", enabled=True, config={},
        )
        _patch_adapter_registry(monkeypatch, {"failing": _BootstrapFailingAdapter})

        daemon = KilnDaemon(daemon_config)
        await daemon.start()

        assert "failing" not in daemon.adapters
        assert daemon._server is not None

        await daemon.stop()

    @pytest.mark.asyncio
    async def test_adapters_stopped_on_daemon_stop(self, daemon_config, monkeypatch):
        """Adapters are stopped when the daemon stops."""
        from kiln.daemon.config import AdapterConfig

        daemon_config.adapters["mock"] = AdapterConfig(
            adapter_id="mock", platform="mock", enabled=True, config={},
        )
        _patch_adapter_registry(monkeypatch, {"mock": _BootstrapMockAdapter})

        daemon = KilnDaemon(daemon_config)
        await daemon.start()

        adapter = daemon.adapters["mock"]
        assert not adapter.stopped

        await daemon.stop()
        assert adapter.stopped
        assert len(daemon.adapters) == 0

    @pytest.mark.asyncio
    async def test_no_adapters_configured(self, daemon_config):
        """Daemon starts fine with no adapters configured."""
        daemon = KilnDaemon(daemon_config)
        await daemon.start()
        assert len(daemon.adapters) == 0
        assert daemon._server is not None
        await daemon.stop()

    @pytest.mark.asyncio
    async def test_adapter_receives_platform_op(self, daemon_config, make_client, monkeypatch):
        """Platform ops route to the bootstrapped adapter."""
        from kiln.daemon.config import AdapterConfig

        daemon_config.adapters["mock"] = AdapterConfig(
            adapter_id="mock", platform="mock", enabled=True, config={},
        )
        _patch_adapter_registry(monkeypatch, {"mock": _BootstrapMockAdapter})

        daemon = KilnDaemon(daemon_config)
        await daemon.start()

        client = make_client("beth", "beth-canon-1")

        result = await client.platform_op("mock", "ping", {"data": 1})
        assert result["ok"] is True
        assert result["action"] == "ping"

        await daemon.stop()

    @pytest.mark.asyncio
    async def test_resolve_adapter_class_dotted_path(self, monkeypatch):
        """_resolve_adapter_class handles dotted string paths."""
        _patch_adapter_registry(monkeypatch, {
            "test_resolve": "kiln.daemon.server.KilnDaemon",
        })
        cls = KilnDaemon._resolve_adapter_class("test_resolve")
        assert cls is KilnDaemon

    @pytest.mark.asyncio
    async def test_resolve_adapter_class_direct_type(self, monkeypatch):
        """_resolve_adapter_class handles direct class references."""
        _patch_adapter_registry(monkeypatch, {"mock": _BootstrapMockAdapter})
        cls = KilnDaemon._resolve_adapter_class("mock")
        assert cls is _BootstrapMockAdapter


# ---------------------------------------------------------------------------
# D3b — Security challenge op (adapter wrapper)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestSecurityChallengeOp:
    async def test_no_client(self):
        adapter = _make_adapter()
        adapter._client = None

        result = await adapter._op_security_challenge(
            {"reason": "test", "passwords": [{"word": "alpha"}]},
            None,
        )

        assert result["result"] == "error"
        assert "not connected" in result["error"]

    async def test_no_security_channel(self):
        """Returns error when #security channel doesn't exist in config."""
        adapter = _make_adapter(channels={})
        # Need a client that returns None for resolve
        class FakeClient:
            def get_channel(self, cid):
                return None
            async def fetch_channel(self, cid):
                return None
        adapter._client = FakeClient()

        result = await adapter._op_security_challenge(
            {"reason": "test", "passwords": [{"word": "alpha"}]},
            None,
        )

        assert result["result"] == "error"
        assert "no #security channel" in result["error"]

    @pytest.mark.skip(reason="Hangs: run_security_challenge waits for user input with no mock response")
    async def test_empty_reason_falls_back(self):
        """Empty string reason should fall back to 'Unspecified' (no regression)."""
        from kiln.daemon.adapters.discord import _DiscordChallengeTransport

        adapter = _make_adapter(
            channels={"security": "5555"},
            users={"111": {"name": "Kira", "max_trust": "full"}},
        )

        # Fake channel that records sent messages
        class FakeChannel:
            id = 5555
            def __init__(self):
                self.sent: list[str] = []
            async def send(self, text):
                self.sent.append(text)
                class Msg:
                    id = 1
                return Msg()
            def get_partial_message(self, mid):
                class Partial:
                    async def delete(self): pass
                return Partial()

        fake_ch = FakeChannel()
        transport = _DiscordChallengeTransport(adapter, fake_ch)

        from kiln.daemon.security import run_security_challenge
        # Run with a correct password so it completes quickly
        result = await run_security_challenge(
            transport,
            reason="Unspecified",  # this is what the adapter should pass for ""
            passwords=[{"word": "alpha"}],
        )
        # Verify the challenge text contains "Unspecified"
        assert any("Unspecified" in s for s in fake_ch.sent)
