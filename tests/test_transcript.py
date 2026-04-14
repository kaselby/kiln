"""Tests for Claude-compatible transcript writer and loader.

Covers:
- TranscriptWriter record structure against Claude golden fixtures
- UUID/parent chaining correctness
- Mixed assistant records (text + tool_use in one record)
- Tool result records with sourceToolAssistantUUID
- Hook attachment records
- TranscriptLoader round-trip (write → load → verify history)
- Interrupted session with orphaned tool calls
- Resume integration: loader output works with CustomBackend._build_input()
"""

import json
from pathlib import Path

import pytest

from kiln.transcript import TranscriptWriter, load_transcript, transcript_session_id
from kiln.types import ThinkingEvent, ToolCallEvent, Usage
from kiln.backends.custom import (
    AssistantTurn,
    ContextInjection,
    CustomBackend,
    ToolResultTurn,
    UserTurn,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def read_records(path: Path) -> list[dict]:
    records = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if line:
            records.append(json.loads(line))
    return records


@pytest.fixture
def transcript_path(tmp_path):
    return tmp_path / "test-session.jsonl"


@pytest.fixture
def writer(transcript_path):
    w = TranscriptWriter(transcript_path, cwd="/test/cwd", model="test-model")
    yield w
    w.close()


# ---------------------------------------------------------------------------
# TranscriptWriter: record structure
# ---------------------------------------------------------------------------

class TestWriterRecordStructure:

    def test_user_plain_text(self, writer, transcript_path):
        uid = writer.write_user("Hello, world")
        records = read_records(transcript_path)
        assert len(records) == 1

        rec = records[0]
        assert rec["type"] == "user"
        assert rec["uuid"] == uid
        assert rec["parentUuid"] is None  # first record
        assert rec["message"]["role"] == "user"
        assert rec["message"]["content"] == "Hello, world"
        assert rec["sessionId"] == writer.session_id
        assert rec["cwd"] == "/test/cwd"
        assert rec["isSidechain"] is False

    def test_user_rich_content(self, writer, transcript_path):
        blocks = [
            {"type": "text", "text": "Look at this"},
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "abc"}},
        ]
        writer.write_user(blocks)
        rec = read_records(transcript_path)[0]
        assert isinstance(rec["message"]["content"], list)
        assert len(rec["message"]["content"]) == 2
        assert rec["message"]["content"][0]["type"] == "text"
        assert rec["message"]["content"][1]["type"] == "image"

    def test_assistant_text_only(self, writer, transcript_path):
        writer.write_user("hi")
        uid = writer.write_assistant(text="Hello back")
        records = read_records(transcript_path)
        assert len(records) == 2

        rec = records[1]
        assert rec["type"] == "assistant"
        assert rec["uuid"] == uid
        assert rec["parentUuid"] == records[0]["uuid"]  # chains to user
        assert rec["message"]["role"] == "assistant"
        assert rec["message"]["model"] == "test-model"
        assert rec["message"]["stop_reason"] == "end_turn"

        content = rec["message"]["content"]
        assert len(content) == 1
        assert content[0]["type"] == "text"
        assert content[0]["text"] == "Hello back"

    def test_assistant_mixed_text_and_tool_use(self, writer, transcript_path):
        """Golden 2: mixed assistant content — text + tool_use in one record."""
        writer.write_user("do something")
        tool_calls = [
            ToolCallEvent(id="toolu_abc", name="Bash", input={"command": "ls"}),
            ToolCallEvent(id="toolu_def", name="Read", input={"file_path": "/x"}),
        ]
        writer.write_assistant(
            text="Let me check that.",
            tool_calls=tool_calls,
            stop_reason="tool_use",
        )
        rec = read_records(transcript_path)[1]
        content = rec["message"]["content"]
        assert len(content) == 3
        assert content[0] == {"type": "text", "text": "Let me check that."}
        assert content[1]["type"] == "tool_use"
        assert content[1]["id"] == "toolu_abc"
        assert content[1]["name"] == "Bash"
        assert content[1]["input"] == {"command": "ls"}
        assert content[2]["type"] == "tool_use"
        assert content[2]["id"] == "toolu_def"
        assert rec["message"]["stop_reason"] == "tool_use"

    def test_assistant_with_thinking(self, writer, transcript_path):
        writer.write_user("think about this")
        thinking = [
            ThinkingEvent(text="Let me consider...", signature="sig123"),
        ]
        writer.write_assistant(
            text="Here's my answer.",
            thinking_events=thinking,
        )
        rec = read_records(transcript_path)[1]
        content = rec["message"]["content"]
        assert len(content) == 2
        assert content[0]["type"] == "thinking"
        assert content[0]["thinking"] == "Let me consider..."
        assert content[0]["signature"] == "sig123"
        assert content[1]["type"] == "text"
        assert content[1]["text"] == "Here's my answer."

    def test_assistant_thinking_without_signature(self, writer, transcript_path):
        writer.write_user("think")
        thinking = [ThinkingEvent(text="hmm...")]
        writer.write_assistant(text="done", thinking_events=thinking)
        rec = read_records(transcript_path)[1]
        block = rec["message"]["content"][0]
        assert block["type"] == "thinking"
        assert "signature" not in block  # omitted, not synthetic

    def test_tool_result(self, writer, transcript_path):
        """Golden 3: tool result with sourceToolAssistantUUID."""
        writer.write_user("go")
        asst_uuid = writer.write_assistant(
            text="Running.",
            tool_calls=[ToolCallEvent(id="toolu_x", name="Bash", input={})],
            stop_reason="tool_use",
        )
        tr_uuid = writer.write_tool_result("toolu_x", "file1.txt\nfile2.txt")
        records = read_records(transcript_path)
        rec = records[2]
        assert rec["type"] == "user"
        assert rec["uuid"] == tr_uuid
        assert rec["parentUuid"] == asst_uuid
        assert rec["sourceToolAssistantUUID"] == asst_uuid

        content = rec["message"]["content"]
        assert len(content) == 1
        assert content[0]["type"] == "tool_result"
        assert content[0]["tool_use_id"] == "toolu_x"
        assert content[0]["content"] == "file1.txt\nfile2.txt"
        assert "is_error" not in content[0]  # omitted when false

    def test_tool_result_error(self, writer, transcript_path):
        writer.write_user("go")
        writer.write_assistant(
            tool_calls=[ToolCallEvent(id="toolu_err", name="Bash", input={})],
            stop_reason="tool_use",
        )
        writer.write_tool_result("toolu_err", "Permission denied", is_error=True)
        rec = read_records(transcript_path)[2]
        content = rec["message"]["content"][0]
        assert content["is_error"] is True
        assert rec["toolUseResult"].startswith("Error: ")

    def test_tool_result_rich_content(self, writer, transcript_path):
        """Rich tool results preserve block structure in transcript."""
        writer.write_user("go")
        writer.write_assistant(
            tool_calls=[ToolCallEvent(id="toolu_rich", name="Read", input={})],
            stop_reason="tool_use",
        )
        rich_blocks = [
            {"type": "text", "text": "File: /img.png"},
            {"type": "image", "data": "abc123", "mimeType": "image/png"},
        ]
        writer.write_tool_result(
            "toolu_rich", "File: /img.png\n[Image: image/png]",
            rich_content=rich_blocks,
        )
        rec = read_records(transcript_path)[2]
        tr_content = rec["message"]["content"][0]["content"]
        # Content should be a block list, not a plain string
        assert isinstance(tr_content, list)
        assert tr_content[0] == {"type": "text", "text": "File: /img.png"}
        assert tr_content[1]["type"] == "image"
        assert tr_content[1]["source"]["data"] == "abc123"
        # toolUseResult mirror is still text
        assert "File: /img.png" in rec["toolUseResult"]

    def test_hook_additional_context(self, writer, transcript_path):
        """Golden 4: hook additional context attachment."""
        writer.write_user("go")
        writer.write_assistant(
            tool_calls=[ToolCallEvent(id="toolu_h", name="Bash", input={})],
            stop_reason="tool_use",
        )
        writer.write_tool_result("toolu_h", "ok")
        uid = writer.write_hook_additional_context(
            "[Session state] context: 50k/200k",
            hook_name="PostToolUse:Bash",
            tool_use_id="toolu_h",
        )
        records = read_records(transcript_path)
        rec = records[3]
        assert rec["type"] == "attachment"
        assert rec["uuid"] == uid
        att = rec["attachment"]
        assert att["type"] == "hook_additional_context"
        assert att["content"] == ["[Session state] context: 50k/200k"]
        assert att["hookName"] == "PostToolUse:Bash"
        assert att["toolUseID"] == "toolu_h"
        assert att["hookEvent"] == "PostToolUse"

    def test_hook_stopped_continuation(self, writer, transcript_path):
        """Golden 5: hook stopped continuation attachment."""
        writer.write_user("go")
        writer.write_assistant(
            tool_calls=[ToolCallEvent(id="toolu_s", name="Read", input={})],
            stop_reason="tool_use",
        )
        writer.write_tool_result("toolu_s", "content")
        uid = writer.write_hook_stopped_continuation(
            hook_name="PostToolUse:Read",
            tool_use_id="toolu_s",
        )
        rec = read_records(transcript_path)[3]
        assert rec["type"] == "attachment"
        att = rec["attachment"]
        assert att["type"] == "hook_stopped_continuation"
        assert att["hookName"] == "PostToolUse:Read"
        assert att["toolUseID"] == "toolu_s"

    def test_system_error(self, writer, transcript_path):
        writer.write_user("go")
        writer.write_system_error("Overloaded", status=529)
        rec = read_records(transcript_path)[1]
        assert rec["type"] == "system"
        assert rec["subtype"] == "api_error"
        assert rec["error"]["message"] == "Overloaded"
        assert rec["error"]["status"] == 529

    def test_last_prompt(self, writer, transcript_path):
        writer.write_last_prompt("Hello, world")
        rec = read_records(transcript_path)[0]
        assert rec["type"] == "last-prompt"
        assert rec["lastPrompt"] == "Hello, world"
        assert rec["sessionId"] == writer.session_id

    def test_usage_formatting(self, writer, transcript_path):
        writer.write_user("hi")
        usage = Usage(
            input_tokens=1000, output_tokens=50,
            cache_read_tokens=500, cache_write_tokens=200,
        )
        writer.write_assistant(text="ok", usage=usage)
        rec = read_records(transcript_path)[1]
        u = rec["message"]["usage"]
        assert u["input_tokens"] == 1000
        assert u["output_tokens"] == 50
        assert u["cache_read_input_tokens"] == 500
        assert u["cache_creation_input_tokens"] == 200


# ---------------------------------------------------------------------------
# TranscriptWriter: UUID chaining
# ---------------------------------------------------------------------------

class TestWriterChaining:

    def test_linear_chain(self, writer, transcript_path):
        """Every record's parentUuid points to the preceding record."""
        writer.write_user("hi")
        writer.write_assistant(text="hello")
        writer.write_user("more")
        writer.write_assistant(
            text="tools",
            tool_calls=[ToolCallEvent(id="t1", name="X", input={})],
            stop_reason="tool_use",
        )
        writer.write_tool_result("t1", "done")

        records = read_records(transcript_path)
        assert records[0]["parentUuid"] is None
        for i in range(1, len(records)):
            assert records[i]["parentUuid"] == records[i - 1]["uuid"]

    def test_source_tool_assistant_uuid(self, writer, transcript_path):
        """Tool result records carry sourceToolAssistantUUID of the assistant."""
        writer.write_user("go")
        asst_uuid = writer.write_assistant(
            tool_calls=[
                ToolCallEvent(id="t1", name="A", input={}),
                ToolCallEvent(id="t2", name="B", input={}),
            ],
            stop_reason="tool_use",
        )
        writer.write_tool_result("t1", "r1")
        writer.write_tool_result("t2", "r2")

        records = read_records(transcript_path)
        assert records[2]["sourceToolAssistantUUID"] == asst_uuid
        assert records[3]["sourceToolAssistantUUID"] == asst_uuid

    def test_all_uuids_unique(self, writer, transcript_path):
        writer.write_user("a")
        writer.write_assistant(text="b")
        writer.write_user("c")
        writer.write_assistant(
            tool_calls=[ToolCallEvent(id="t1", name="X", input={})],
            stop_reason="tool_use",
        )
        writer.write_tool_result("t1", "d")
        writer.write_hook_additional_context("ctx")
        writer.write_hook_stopped_continuation()

        records = read_records(transcript_path)
        uuids = [r["uuid"] for r in records if "uuid" in r]
        assert len(uuids) == len(set(uuids))

    def test_resume_chain_continuity(self, transcript_path):
        """Resumed writer chains from the last record and reuses sessionId."""
        # Write initial session
        w1 = TranscriptWriter(transcript_path, cwd="/test", model="m")
        original_sid = w1.session_id
        w1.write_user("hello")
        asst_uuid = w1.write_assistant(
            text="hi",
            tool_calls=[ToolCallEvent(id="t1", name="X", input={})],
            stop_reason="tool_use",
        )
        w1.write_tool_result("t1", "result")
        w1.close()

        records_before = read_records(transcript_path)
        last_uuid = records_before[-1]["uuid"]

        # Resume with existing session_id
        w2 = TranscriptWriter(
            transcript_path, cwd="/test", model="m",
            session_id=original_sid,
        )
        assert w2.session_id == original_sid
        w2.write_user("resumed question")
        w2.close()

        records_after = read_records(transcript_path)
        resumed_rec = records_after[len(records_before)]
        # New record chains from the last record of the original session
        assert resumed_rec["parentUuid"] == last_uuid
        assert resumed_rec["sessionId"] == original_sid


# ---------------------------------------------------------------------------
# TranscriptLoader: round-trip correctness
# ---------------------------------------------------------------------------

class TestLoaderRoundTrip:

    def test_plain_text_round_trip(self, writer, transcript_path):
        writer.write_user("Hello")
        writer.write_assistant(text="Hi there")
        writer.close()

        history = load_transcript(transcript_path)
        assert len(history) == 2
        assert isinstance(history[0], UserTurn)
        assert history[0].text == "Hello"
        assert isinstance(history[1], AssistantTurn)
        assert history[1].text == "Hi there"

    def test_mixed_assistant_tool_round_trip(self, writer, transcript_path):
        """Key golden: mixed assistant content reconstructs correctly."""
        writer.write_user("do stuff")
        writer.write_assistant(
            text="Running tools.",
            tool_calls=[
                ToolCallEvent(id="t1", name="Bash", input={"command": "ls"}),
            ],
            stop_reason="tool_use",
        )
        writer.write_tool_result("t1", "file.txt")
        writer.write_assistant(text="Done.")
        writer.close()

        history = load_transcript(transcript_path)
        assert len(history) == 4
        # user
        assert isinstance(history[0], UserTurn)
        # assistant with text + tool call
        asst = history[1]
        assert isinstance(asst, AssistantTurn)
        assert asst.text == "Running tools."
        assert len(asst.tool_calls) == 1
        assert asst.tool_calls[0].id == "t1"
        assert asst.tool_calls[0].name == "Bash"
        assert asst.tool_calls[0].input == {"command": "ls"}
        # tool result
        tr = history[2]
        assert isinstance(tr, ToolResultTurn)
        assert tr.call_id == "t1"
        assert tr.output == "file.txt"
        assert tr.is_error is False
        # final assistant
        assert isinstance(history[3], AssistantTurn)
        assert history[3].text == "Done."

    def test_tool_result_error_round_trip(self, writer, transcript_path):
        writer.write_user("go")
        writer.write_assistant(
            tool_calls=[ToolCallEvent(id="te", name="X", input={})],
            stop_reason="tool_use",
        )
        writer.write_tool_result("te", "Permission denied", is_error=True)
        writer.close()

        history = load_transcript(transcript_path)
        tr = history[2]
        assert isinstance(tr, ToolResultTurn)
        assert tr.is_error is True
        assert tr.output == "Permission denied"

    def test_hook_context_injection_round_trip(self, writer, transcript_path):
        """Hook additional_context reconstructs as ContextInjection."""
        writer.write_user("go")
        writer.write_assistant(
            tool_calls=[ToolCallEvent(id="th", name="Bash", input={})],
            stop_reason="tool_use",
        )
        writer.write_tool_result("th", "ok")
        writer.write_hook_additional_context(
            "[Session state] ctx: 40k",
            hook_name="PostToolUse:Bash",
            tool_use_id="th",
        )
        writer.close()

        history = load_transcript(transcript_path)
        assert len(history) == 4
        ci = history[3]
        assert isinstance(ci, ContextInjection)
        assert ci.text == "[Session state] ctx: 40k"

    def test_hook_stopped_continuation_not_in_history(self, writer, transcript_path):
        """hook_stopped_continuation is a flow marker, not history."""
        writer.write_user("go")
        writer.write_assistant(
            tool_calls=[ToolCallEvent(id="ts", name="Read", input={})],
            stop_reason="tool_use",
        )
        writer.write_tool_result("ts", "content")
        writer.write_hook_stopped_continuation(tool_use_id="ts")
        writer.close()

        history = load_transcript(transcript_path)
        # Should have user, assistant, tool_result — no entry for hook_stopped
        assert len(history) == 3
        assert not any(isinstance(t, ContextInjection) for t in history)

    def test_rich_tool_result_round_trip(self, writer, transcript_path):
        """Rich tool results (block lists) are flattened to text on load.

        The transcript stores rich tool content as a block list for auditability,
        but images/docs can't round-trip through resume.  The loader must extract
        text parts and produce a string for ToolResultTurn.output.
        """
        writer.write_user("go")
        writer.write_assistant(
            tool_calls=[ToolCallEvent(id="tr_rich", name="Read", input={})],
            stop_reason="tool_use",
        )
        rich_blocks = [
            {"type": "text", "text": "File: /img.png"},
            {"type": "image", "data": "abc123", "mimeType": "image/png"},
        ]
        writer.write_tool_result(
            "tr_rich", "File: /img.png\n[Image: image/png]",
            rich_content=rich_blocks,
        )
        writer.close()

        history = load_transcript(transcript_path)
        tr = history[2]
        assert isinstance(tr, ToolResultTurn)
        assert tr.call_id == "tr_rich"
        # Output must be a string, not a list — images replaced with placeholder
        assert isinstance(tr.output, str)
        assert "File: /img.png" in tr.output
        assert "[image]" in tr.output

    def test_thinking_round_trip(self, writer, transcript_path):
        writer.write_user("think")
        writer.write_assistant(
            text="Answer",
            thinking_events=[ThinkingEvent(text="reasoning...", signature="sig")],
        )
        writer.close()

        history = load_transcript(transcript_path)
        asst = history[1]
        assert isinstance(asst, AssistantTurn)
        assert asst.thinking_text == "reasoning..."
        assert asst.text == "Answer"

    def test_system_and_lastprompt_skipped(self, writer, transcript_path):
        """System and last-prompt records don't create history entries."""
        writer.write_user("hi")
        writer.write_system_error("oops")
        writer.write_last_prompt("hi")
        writer.close()

        history = load_transcript(transcript_path)
        assert len(history) == 1
        assert isinstance(history[0], UserTurn)


# ---------------------------------------------------------------------------
# Interrupted session: orphaned tool calls
# ---------------------------------------------------------------------------

class TestInterruptedSession:

    def test_orphaned_tool_call_survives_round_trip(self, writer, transcript_path):
        """Transcript with assistant tool_use but no matching result.

        The loader reconstructs the assistant turn with tool_calls.
        CustomBackend._insert_orphaned_results() handles repair at replay time.
        """
        writer.write_user("go")
        writer.write_assistant(
            text="Let me run that.",
            tool_calls=[
                ToolCallEvent(id="t_orphan", name="Bash", input={"command": "sleep 999"}),
            ],
            stop_reason="tool_use",
        )
        # No tool_result written — session crashed/killed
        writer.close()

        history = load_transcript(transcript_path)
        assert len(history) == 2
        asst = history[1]
        assert isinstance(asst, AssistantTurn)
        assert len(asst.tool_calls) == 1
        assert asst.tool_calls[0].id == "t_orphan"

    def test_orphan_repair_after_loader(self, transcript_path):
        """End-to-end: write interrupted → load → _build_input → verify orphan repair."""
        # Write a transcript with an orphaned tool call
        w = TranscriptWriter(transcript_path, cwd="/test", model="m")
        w.write_user("go")
        w.write_assistant(
            text="Running.",
            tool_calls=[ToolCallEvent(id="t_orph", name="Bash", input={"command": "ls"})],
            stop_reason="tool_use",
        )
        w.close()

        # Load into a fresh backend
        history = load_transcript(transcript_path)

        # Simulate what CustomBackend does: assign history, build input
        from kiln.backends.custom import CustomBackend, _strip_output_only_fields

        class DummyProvider:
            context_injection_role = "developer"

            def build_assistant_input(self, *, text, tool_calls):
                items = []
                if text:
                    items.append({
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": text, "annotations": []}],
                        "status": "completed",
                    })
                for tc in tool_calls:
                    items.append({
                        "type": "function_call",
                        "call_id": tc.id,
                        "name": tc.name,
                        "arguments": json.dumps(tc.input),
                        "status": "completed",
                    })
                return items



        backend = CustomBackend(DummyProvider())
        backend._history = history

        items = backend._build_input()
        items = backend._apply_transforms(items)

        # The orphaned tool call should get a synthetic error result
        func_outputs = [i for i in items if i.get("type") == "function_call_output"]
        assert len(func_outputs) == 1
        assert func_outputs[0]["call_id"] == "t_orph"
        assert "interrupted" in func_outputs[0]["output"].lower()


# ---------------------------------------------------------------------------
# transcript_session_id helper
# ---------------------------------------------------------------------------

class TestTranscriptSessionId:

    def test_extracts_session_id(self, writer, transcript_path):
        writer.write_user("hi")
        writer.close()
        sid = transcript_session_id(transcript_path)
        assert sid == writer.session_id

    def test_returns_none_for_empty(self, tmp_path):
        p = tmp_path / "empty.jsonl"
        p.write_text("")
        assert transcript_session_id(p) is None

    def test_returns_none_for_missing(self, tmp_path):
        p = tmp_path / "nope.jsonl"
        assert transcript_session_id(p) is None
