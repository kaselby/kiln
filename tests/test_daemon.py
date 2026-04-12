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

    with pytest.raises(NotImplementedError):
        await adapter.platform_op("send", {})

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
