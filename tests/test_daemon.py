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
        msg = proto.register("beth", "beth-swift-crane", 12345)
        line = msg.to_line()
        parsed = proto.Message.from_line(line)
        assert parsed.type == proto.REGISTER
        assert parsed.ref == msg.ref
        assert parsed.data["agent"] == "beth"
        assert parsed.data["session"] == "beth-swift-crane"
        assert parsed.data["pid"] == 12345

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
        assert evt.type == proto.EVENT
        assert evt.is_event
        assert evt.event_type == proto.EVT_MESSAGE_CHANNEL
        assert evt.data["channel"] == "test"

    def test_message_classification(self):
        assert proto.ack("ref").is_response
        assert proto.result("ref").is_response
        assert proto.error("ref", "x").is_response
        assert not proto.register("a", "b", 1).is_response

        assert proto.event("test").is_event
        assert not proto.ack("ref").is_event

    def test_request_context(self):
        ctx = proto.RequestContext(agent_name="beth", session_id="beth-swift-crane")
        assert ctx.agent_name == "beth"
        assert ctx.session_id == "beth-swift-crane"


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

    config = DaemonConfig(
        socket_path=sock_path,
        pid_file=tmp_daemon_dir / "daemon" / "kiln.pid",
        log_file=tmp_daemon_dir / "daemon" / "daemon.log",
        lockdown_file=tmp_daemon_dir / "daemon" / "lockdown",
        agents_registry=tmp_daemon_dir / "agents.yml",
        channels_dir=tmp_daemon_dir / "channels",
        state_dir=tmp_daemon_dir / "daemon" / "state",
    )

    yield config

    # Clean up short-path socket dir
    sock_path.unlink(missing_ok=True)
    sock_dir.rmdir()


@pytest_asyncio.fixture
async def running_daemon(daemon_config):
    """Start a daemon server and yield it. Stops on cleanup."""
    daemon = KilnDaemon(daemon_config)
    await daemon.start()
    yield daemon
    await daemon.stop()


@pytest_asyncio.fixture
async def make_client(daemon_config):
    """Factory for clients pointed at the test daemon."""
    clients = []

    def _make():
        client = DaemonClient(socket_path=daemon_config.socket_path)
        clients.append(client)
        return client

    yield _make

    # Cleanup — disconnect all
    for c in clients:
        if c.connected:
            try:
                await c.disconnect()
            except Exception:
                pass


@pytest.mark.asyncio
async def test_register_and_list(running_daemon, make_client):
    client = make_client()
    await client.connect(auto_start=False)
    await client.register("beth", "beth-test-1", 9999)

    sessions = await client.list_sessions()
    assert len(sessions) == 1
    assert sessions[0]["session_id"] == "beth-test-1"
    assert sessions[0]["agent_name"] == "beth"


@pytest.mark.asyncio
async def test_subscribe_and_list(running_daemon, make_client):
    client = make_client()
    await client.connect(auto_start=False)
    await client.register("beth", "beth-test-1", 9999)

    count = await client.subscribe("test-channel")
    assert count == 1

    # Sync cache should reflect subscription
    assert "test-channel" in client.subscriptions

    # Async query should also work
    subs = await client.list_subscriptions()
    assert "test-channel" in subs

    await client.unsubscribe("test-channel")
    assert "test-channel" not in client.subscriptions


@pytest.mark.asyncio
async def test_publish_fanout(running_daemon, make_client, daemon_config):
    """Publish to a channel and verify inbox delivery."""
    c1 = make_client()
    c2 = make_client()

    await c1.connect(auto_start=False)
    await c2.connect(auto_start=False)
    await c1.register("beth", "beth-pub", 9001)
    await c2.register("dalet", "dalet-sub", 9002)

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
    c1 = make_client()
    await c1.connect(auto_start=False)
    await c1.register("beth", "beth-sender", 9001)

    result = await c1.send_direct("dalet-receiver", "hi", "Hello from Beth")
    assert "sent" in result.lower() or "dalet" in result.lower()

    # Check inbox
    inbox = daemon_config.agents_registry.parent / "dalet" / "inbox" / "dalet-receiver"
    msgs = list(inbox.glob("msg-*.md"))
    assert len(msgs) == 1
    assert "Hello from Beth" in msgs[0].read_text()


@pytest.mark.asyncio
async def test_disconnect_cleanup(running_daemon, make_client):
    """Verify that disconnecting cleans up subscriptions and presence."""
    client = make_client()
    await client.connect(auto_start=False)
    await client.register("beth", "beth-cleanup", 9001)
    await client.subscribe("temp-channel")

    # Verify registered
    assert running_daemon.state.presence.get("beth-cleanup") is not None
    assert "beth-cleanup" in running_daemon.state.channels.subscribers("temp-channel")

    await client.deregister()

    # Give server a moment to process the cleanup
    await asyncio.sleep(0.1)

    assert running_daemon.state.presence.get("beth-cleanup") is None
    assert "beth-cleanup" not in running_daemon.state.channels.subscribers("temp-channel")


@pytest.mark.asyncio
async def test_error_on_unregistered_operations(running_daemon, make_client):
    """Operations before register should return errors."""
    client = make_client()
    await client.connect(auto_start=False)

    with pytest.raises(DaemonError):
        await client.subscribe("test")


@pytest.mark.asyncio
async def test_get_status(running_daemon, make_client):
    client = make_client()
    await client.connect(auto_start=False)
    await client.register("beth", "beth-status", 9001)

    status = await client.get_status()
    assert "sessions" in status
    assert status["sessions"] == 1
    assert status["lockdown"] is False


@pytest.mark.asyncio
async def test_multiple_sessions(running_daemon, make_client):
    """Multiple sessions can coexist."""
    c1 = make_client()
    c2 = make_client()

    await c1.connect(auto_start=False)
    await c2.connect(auto_start=False)
    await c1.register("beth", "beth-a", 9001)
    await c2.register("beth", "beth-b", 9002)

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

    Returns (tool_fn, client) — client must be registered before use.
    """
    from kiln.tools import create_mcp_server

    inbox_root = daemon_config.agents_registry.parent / "beth" / "inbox"
    client = make_client()

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
    await client.connect(auto_start=False)
    await client.register("beth", "beth-tool-test", 9001)

    result = await handler({"action": "subscribe", "channel": "test-ch"})
    assert _tool_ok(result)
    assert "Subscribed" in _tool_text(result)
    assert "1 subscriber" in _tool_text(result)

    # Verify daemon knows about the subscription
    assert "test-ch" in client.subscriptions
    subs = running_daemon.state.channels.subscribers("test-ch")
    assert "beth-tool-test" in subs


@pytest.mark.asyncio
async def test_message_tool_unsubscribe_via_daemon(running_daemon, message_tool_fn):
    handler, client = message_tool_fn
    await client.connect(auto_start=False)
    await client.register("beth", "beth-tool-test", 9001)
    await client.subscribe("test-ch")

    result = await handler({"action": "unsubscribe", "channel": "test-ch"})
    assert _tool_ok(result)
    assert "Unsubscribed" in _tool_text(result)
    assert "test-ch" not in client.subscriptions


@pytest.mark.asyncio
async def test_message_tool_channel_broadcast_via_daemon(
    running_daemon, message_tool_fn, make_client, daemon_config
):
    """Channel broadcast goes through daemon, delivers to subscriber inboxes."""
    handler, sender = message_tool_fn
    await sender.connect(auto_start=False)
    await sender.register("beth", "beth-tool-test", 9001)

    # Set up a second subscriber
    receiver = make_client()
    await receiver.connect(auto_start=False)
    await receiver.register("dalet", "dalet-listener", 9002)
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
    await client.connect(auto_start=False)
    await client.register("beth", "beth-tool-test", 9001)

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
    """Minimal harness stub for testing subscription persistence logic."""

    def __init__(self, daemon_client=None):
        from kiln.daemon.client import DaemonClient
        self._daemon_client = daemon_client
        self._desired_subscriptions: list[str] = []

    def _snapshot_channel_subscriptions(self) -> list[str]:
        if self._daemon_client and self._daemon_client.connected:
            live = self._daemon_client.subscriptions
            self._desired_subscriptions = list(live)
            return live
        return list(self._desired_subscriptions)

    async def _restore_channel_subscriptions(self, subscriptions: list[str]) -> None:
        if not subscriptions:
            return
        self._desired_subscriptions = list(subscriptions)
        if self._daemon_client and self._daemon_client.connected:
            await self._daemon_client.restore_subscriptions(subscriptions)


@pytest.mark.asyncio
async def test_desired_subscriptions_survive_daemon_outage(running_daemon, make_client):
    """Desired subscriptions persist through daemon disconnect/unavailability.

    Regression test: without this fix, snapshot would return [] when
    disconnected, silently erasing reconnect intent on session save.
    """
    client = make_client()
    await client.connect(auto_start=False)
    await client.register("beth", "beth-persist", 9001)

    harness = FakeHarness(daemon_client=client)

    # Subscribe through daemon — desired state tracks live state
    await harness._restore_channel_subscriptions(["alpha", "beta"])
    assert harness._desired_subscriptions == ["alpha", "beta"]
    assert set(client.subscriptions) == {"alpha", "beta"}

    # Snapshot while connected — returns live truth
    snap = harness._snapshot_channel_subscriptions()
    assert set(snap) == {"alpha", "beta"}

    # Disconnect from daemon (simulates outage)
    await client.deregister()
    await client.disconnect()
    assert not client.connected

    # Snapshot while disconnected — must preserve desired, NOT return []
    snap = harness._snapshot_channel_subscriptions()
    assert set(snap) == {"alpha", "beta"}, \
        "Desired subscriptions must survive daemon disconnect"

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
    c1 = make_client()
    c2 = make_client()
    await c1.connect(auto_start=False)
    await c2.connect(auto_start=False)
    await c1.register("beth", "beth-pub-a", 100)
    await c2.register("beth", "beth-pub-b", 101)
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
    c1 = make_client()
    await c1.connect(auto_start=False)
    await c1.register("beth", "beth-ex-1", 100)
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

    c1 = make_client()
    await c1.connect(auto_start=False)
    await c1.register("beth", "beth-src-1", 100)
    await c1.subscribe("src-ch")

    await running_daemon.publish_to_channel(
        "src-ch", "discord-kira", "s", "body", source="discord",
    )
    await asyncio.sleep(0.05)

    channel_events = [e for e in events if e.data.get("event_type") == "message.channel"]
    assert len(channel_events) == 1
    assert channel_events[0].data["source"] == "discord"


@pytest.mark.asyncio
async def test_deliver_platform_message(running_daemon, make_client, daemon_config):
    """Test daemon platform message delivery with structured payload."""
    c1 = make_client()
    await c1.connect(auto_start=False)
    await c1.register("beth", "beth-plat-1", 100)

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

    # D3 ops still raise NotImplementedError
    with pytest.raises(NotImplementedError):
        await adapter.platform_op("voice_send", {})

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
    def test_subscribe_surface_round_trip(self):
        msg = proto.subscribe_surface("discord:user:116377")
        line = msg.to_line()
        parsed = proto.Message.from_line(line)
        assert parsed.type == proto.SUBSCRIBE_SURFACE
        assert parsed.data["surface_ref"] == "discord:user:116377"

    def test_unsubscribe_surface_round_trip(self):
        msg = proto.unsubscribe_surface("discord:user:116377")
        line = msg.to_line()
        parsed = proto.Message.from_line(line)
        assert parsed.type == proto.UNSUBSCRIBE_SURFACE
        assert parsed.data["surface_ref"] == "discord:user:116377"

    def test_list_surface_subscriptions_round_trip(self):
        msg = proto.list_surface_subscriptions()
        parsed = proto.Message.from_line(msg.to_line())
        assert parsed.type == proto.LIST_SURFACE_SUBSCRIPTIONS
        assert "adapter_id" not in parsed.data

    def test_list_surface_subscriptions_with_adapter(self):
        msg = proto.list_surface_subscriptions(adapter_id="discord")
        parsed = proto.Message.from_line(msg.to_line())
        assert parsed.data["adapter_id"] == "discord"

    def test_surface_event_types(self):
        evt_sub = proto.event(proto.EVT_SURFACE_SUBSCRIBED,
                              surface_ref="discord:user:123", session_id="beth-a")
        assert evt_sub.event_type == proto.EVT_SURFACE_SUBSCRIBED
        assert evt_sub.data["surface_ref"] == "discord:user:123"

        evt_unsub = proto.event(proto.EVT_SURFACE_UNSUBSCRIBED,
                                surface_ref="discord:user:123", session_id="beth-a")
        assert evt_unsub.event_type == proto.EVT_SURFACE_UNSUBSCRIBED


# ---------------------------------------------------------------------------
# Surface subscription client/server integration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_surface_subscribe_and_list(running_daemon, make_client):
    """Surface subscribe via client, verify daemon state and list query."""
    client = make_client()
    await client.connect(auto_start=False)
    await client.register("beth", "beth-surf-1", 9001)

    count = await client.subscribe_surface("discord:user:116377")
    assert count == 1

    # Local cache
    assert "discord:user:116377" in client.surface_subscriptions

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
    client = make_client()
    await client.connect(auto_start=False)
    await client.register("beth", "beth-surf-2", 9002)

    await client.subscribe_surface("discord:user:123")
    await client.unsubscribe_surface("discord:user:123")

    assert "discord:user:123" not in client.surface_subscriptions
    assert "beth-surf-2" not in running_daemon.state.surfaces.subscribers("discord:user:123")


@pytest.mark.asyncio
async def test_surface_list_filtered_by_adapter(running_daemon, make_client):
    """list_surface_subscriptions with adapter_id filters by prefix."""
    client = make_client()
    await client.connect(auto_start=False)
    await client.register("beth", "beth-surf-3", 9003)

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
async def test_surface_cleanup_on_disconnect(running_daemon, make_client):
    """Surface subscriptions cleaned up when session disconnects."""
    client = make_client()
    await client.connect(auto_start=False)
    await client.register("beth", "beth-surf-cleanup", 9004)

    await client.subscribe_surface("discord:user:999")
    assert "beth-surf-cleanup" in running_daemon.state.surfaces.subscribers("discord:user:999")

    await client.deregister()
    await asyncio.sleep(0.1)

    assert "beth-surf-cleanup" not in running_daemon.state.surfaces.subscribers("discord:user:999")
    assert "discord:user:999" not in running_daemon.state.surfaces.all_surfaces()


@pytest.mark.asyncio
async def test_surface_multiple_subscribers(running_daemon, make_client):
    """Multiple sessions can subscribe to the same surface."""
    c1 = make_client()
    c2 = make_client()
    await c1.connect(auto_start=False)
    await c2.connect(auto_start=False)
    await c1.register("beth", "beth-multi-1", 9010)
    await c2.register("beth", "beth-multi-2", 9011)

    count1 = await c1.subscribe_surface("discord:user:116377")
    assert count1 == 1
    count2 = await c2.subscribe_surface("discord:user:116377")
    assert count2 == 2

    subs = running_daemon.state.surfaces.subscribers("discord:user:116377")
    assert subs == {"beth-multi-1", "beth-multi-2"}


@pytest.mark.asyncio
async def test_surface_subscribe_requires_registration(running_daemon, make_client):
    """Surface operations before register return errors."""
    client = make_client()
    await client.connect(auto_start=False)

    with pytest.raises(DaemonError):
        await client.subscribe_surface("discord:user:123")


@pytest.mark.asyncio
async def test_deliver_to_surface_subscribers(running_daemon, make_client, daemon_config):
    """deliver_to_surface_subscribers delivers to all subscribed sessions."""
    c1 = make_client()
    c2 = make_client()
    await c1.connect(auto_start=False)
    await c2.connect(auto_start=False)
    await c1.register("beth", "beth-deliv-1", 9020)
    await c2.register("beth", "beth-deliv-2", 9021)

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
    client = make_client()
    await client.connect(auto_start=False)
    await client.register("beth", "beth-stat-surf", 9030)
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

    client = make_client()
    await client.connect(auto_start=False)
    await client.register("beth", "beth-val-1", 9040)

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

    client = make_client()
    await client.connect(auto_start=False)
    await client.register("beth", "beth-val-2", 9041)

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
    client = make_client()
    await client.connect(auto_start=False)
    await client.register("beth", "beth-val-3", 9042)

    # No "slack" adapter registered — should succeed without validation
    count = await client.subscribe_surface("slack:channel:12345")
    assert count == 1
    assert "slack:channel:12345" in client.surface_subscriptions


@pytest.mark.asyncio
async def test_surface_client_caches_canonical_ref(running_daemon, make_client):
    """Client caches the daemon-confirmed canonical ref, not caller input.

    This test verifies the echo-back path: subscribe_surface sends a ref,
    the daemon acks with a (potentially canonicalized) surface_ref, and
    the client stores THAT in its local cache. Currently identity, but
    this test ensures the plumbing works when canonicalization becomes
    non-trivial.
    """
    client = make_client()
    await client.connect(auto_start=False)
    await client.register("beth", "beth-canon-1", 9050)

    await client.subscribe_surface("discord:user:116377")
    # Client should have the ref from the daemon ack, not its own input
    assert "discord:user:116377" in client.surface_subscriptions

    await client.unsubscribe_surface("discord:user:116377")
    assert "discord:user:116377" not in client.surface_subscriptions


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
            calls.append((name, event.data.get("event_type")))
        return handler

    # Patch each handler to record calls
    with patch.object(adapter, "_on_channel_message", side_effect=lambda e: calls.append(("channel_message", e.data.get("event_type")))), \
         patch.object(adapter, "_on_session_connected", side_effect=lambda e: calls.append(("session_connected", e.data.get("event_type")))), \
         patch.object(adapter, "_on_session_disconnected", side_effect=lambda e: calls.append(("session_disconnected", e.data.get("event_type")))):

        # Emit events through the daemon event bus
        await running_daemon.events.emit(proto.event(
            proto.EVT_MESSAGE_CHANNEL,
            channel="test", sender="beth-a", summary="hi", body="hello",
        ))
        await running_daemon.events.emit(proto.event(
            proto.EVT_SESSION_CONNECTED,
            session_id="beth-test-1", agent_name="beth",
        ))
        await running_daemon.events.emit(proto.event(
            proto.EVT_SESSION_DISCONNECTED,
            session_id="beth-test-1", agent_name="beth",
        ))

        # Give event bus tasks time to complete
        await asyncio.sleep(0.1)

    assert ("channel_message", proto.EVT_MESSAGE_CHANNEL) in calls
    assert ("session_connected", proto.EVT_SESSION_CONNECTED) in calls
    assert ("session_disconnected", proto.EVT_SESSION_DISCONNECTED) in calls

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
        proto.EVT_SESSION_CONNECTED,
        proto.EVT_SESSION_DISCONNECTED,
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

    await adapter._on_session_connected(proto.event(
        proto.EVT_SESSION_CONNECTED,
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

    await adapter._on_session_connected(proto.event(
        proto.EVT_SESSION_CONNECTED,
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

    await adapter._on_session_disconnected(proto.event(
        proto.EVT_SESSION_DISCONNECTED,
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

    await adapter._on_session_connected(proto.event(
        proto.EVT_SESSION_CONNECTED,
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

class _MockDaemon:
    """Minimal daemon mock for adapter routing tests."""

    def __init__(self):
        from kiln.daemon.state import DaemonState
        self.state = DaemonState()
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
    attachment_paths=None,
):
    from kiln.daemon.adapters.discord import InboundMessage
    return InboundMessage(
        sender_id=sender_id,
        sender_display_name=sender_display_name,
        channel_id=channel_id,
        content=content,
        is_dm=is_dm,
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

    @pytest.mark.asyncio
    async def test_d3_ops_still_raise(self):
        """D3 ops should still raise NotImplementedError."""
        adapter = _make_adapter(channel_access="open")
        for op in ["voice_send", "security_challenge",
                    "permission_request", "permission_resolve"]:
            with pytest.raises(NotImplementedError):
                await adapter.platform_op(op, {})


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
        "author": author,
        "channel": channel,
        "content": content,
        "attachments": attachments or [],
        "flags": type("Flags", (), {"voice": False})(),
    })()
    return msg
