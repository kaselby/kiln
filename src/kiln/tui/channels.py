"""Channel viewer — tail channel messages and send from the terminal.

Uses prompt_toolkit's PromptSession with patch_stdout for a clean chat UX:
messages print to scrollback above the input prompt, native terminal scrolling
and text selection work naturally.

Usage:
    python -m kiln.tui.channels <channel-name> [--as <user>] [--home <path>]
"""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

from prompt_toolkit import PromptSession
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.patch_stdout import patch_stdout
from prompt_toolkit.shortcuts import print_formatted_text
from prompt_toolkit.styles import Style

STYLE = Style.from_dict({
    "ts": "#666666",
    "sender-agent": "ansimagenta bold",
    "sender-user": "ansicyan bold",
    "summary": "#cccccc",
    "body": "#aaaaaa",
    "priority-high": "ansired bold",
    "dim": "#888888",
    "channel-name": "ansimagenta bold",
    "prompt-user": "ansicyan bold",
    "prompt-arrow": "#888888",
})


def _esc(text: str) -> str:
    """Escape HTML special characters for prompt_toolkit HTML."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def format_message(msg: dict, show_body: bool = False) -> str:
    """Format a channel message as an HTML string for print_formatted_text."""
    ts = msg.get("ts", "")
    try:
        dt = datetime.fromisoformat(ts)
        time_str = dt.astimezone().strftime("%H:%M")
    except (ValueError, TypeError):
        time_str = "??:??"

    sender = msg.get("from", "unknown")
    summary = msg.get("summary", "")
    body = msg.get("body", "")
    priority = msg.get("priority", "normal")

    sender_class = "sender-agent" if "-" in sender else "sender-user"

    priority_prefix = ""
    if priority == "high":
        priority_prefix = "<priority-high>[!] </priority-high>"

    parts = [f"<ts>{time_str}</ts> <{sender_class}>{_esc(sender)}</{sender_class}>: "]
    parts.append(f"{priority_prefix}{_esc(summary)}")

    if show_body and body and body != summary:
        for line in body.split("\n"):
            parts.append(f"\n       <body>{_esc(line)}</body>")

    return "".join(parts)


class ChannelViewer:
    def __init__(self, channel: str, user: str = "user", home: Path | None = None):
        self.channel = channel
        self.user = user
        self.home = home or Path(os.environ.get("KILN_AGENT_HOME", Path.cwd()))
        self.channels_path = self.home / "channels.json"
        self.history_file = self.home / "channels" / channel / "history.jsonl"
        self.inbox_root = self.home / "inbox"
        self.show_body = False
        self._file_pos = 0
        self._running = True

    @property
    def _viewer_id(self) -> str:
        return f"viewer-{self.user}"

    def read_subscribers(self) -> list[str]:
        if not self.channels_path.exists():
            return []
        try:
            channels = json.loads(self.channels_path.read_text())
            return channels.get(self.channel, [])
        except (json.JSONDecodeError, OSError):
            return []

    def _subscribe(self) -> None:
        self.channels_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.channels_path, "a+") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            f.seek(0)
            try:
                channels = json.loads(f.read() or "{}")
            except json.JSONDecodeError:
                channels = {}
            subs = channels.get(self.channel, [])
            if self._viewer_id not in subs:
                subs.append(self._viewer_id)
                channels[self.channel] = subs
            f.seek(0)
            f.truncate()
            f.write(json.dumps(channels, indent=2) + "\n")

    def _unsubscribe(self) -> None:
        if not self.channels_path.exists():
            return
        try:
            with open(self.channels_path, "a+") as f:
                fcntl.flock(f, fcntl.LOCK_EX)
                f.seek(0)
                try:
                    channels = json.loads(f.read() or "{}")
                except json.JSONDecodeError:
                    return
                subs = channels.get(self.channel, [])
                if self._viewer_id in subs:
                    subs.remove(self._viewer_id)
                    if subs:
                        channels[self.channel] = subs
                    else:
                        del channels[self.channel]
                f.seek(0)
                f.truncate()
                f.write(json.dumps(channels, indent=2) + "\n")
        except OSError:
            pass

    def send_message(self, text: str) -> None:
        now = datetime.now(timezone.utc)
        if len(text) <= 200:
            summary, body = text, ""
        else:
            summary, body = text[:200] + "...", text
        entry = {
            "ts": now.isoformat(),
            "from": self.user,
            "summary": summary,
            "body": body,
            "priority": "normal",
        }

        self.history_file.parent.mkdir(parents=True, exist_ok=True)
        with open(self.history_file, "a") as f:
            f.write(json.dumps(entry) + "\n")

        for sub in self.read_subscribers():
            if sub == self._viewer_id or sub.startswith("viewer-"):
                continue
            self._deliver_to_inbox(sub, summary, body, now)

    def _deliver_to_inbox(self, recipient: str, summary: str, body: str, now: datetime) -> None:
        inbox = self.inbox_root / recipient
        inbox.mkdir(parents=True, exist_ok=True)

        timestamp = now.strftime("%Y%m%d-%H%M%S")
        msg_id = f"msg-{timestamp}-{uuid.uuid4().hex[:6]}"
        msg_path = inbox / f"{msg_id}.md"

        content = (
            f"---\n"
            f"from: {self.user}\n"
            f"summary: {json.dumps(summary)}\n"
            f"priority: normal\n"
            f"channel: {self.channel}\n"
            f"timestamp: {now.isoformat()}\n"
            f"---\n"
        )
        if body:
            content += f"\n{body}\n"
        msg_path.write_text(content)

    def _read_new_lines(self) -> list[dict]:
        if not self.history_file.exists():
            return []

        try:
            size = self.history_file.stat().st_size
        except OSError:
            return []

        if size <= self._file_pos:
            return []

        messages = []
        with open(self.history_file, "r") as f:
            f.seek(self._file_pos)
            new_data = f.read()
            self._file_pos = f.tell()

        for line in new_data.splitlines():
            line = line.strip()
            if line:
                try:
                    messages.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return messages

    def _print_msg(self, msg: dict) -> None:
        print_formatted_text(
            HTML(format_message(msg, show_body=self.show_body)),
            style=STYLE,
        )

    def _print(self, html: str) -> None:
        print_formatted_text(HTML(html), style=STYLE)

    async def _tail(self) -> None:
        while self._running:
            try:
                for msg in self._read_new_lines():
                    if msg.get("from") == self.user:
                        continue
                    self._print_msg(msg)
            except Exception:
                pass
            await asyncio.sleep(0.5)

    def _handle_command(self, text: str) -> bool:
        cmd = text.strip().lower()

        if cmd == "/body":
            self.show_body = not self.show_body
            state = "on" if self.show_body else "off"
            self._print(f"<dim>Message bodies: {state}</dim>")
            return True

        if cmd == "/subs":
            subs = self.read_subscribers()
            names = ", ".join(subs) if subs else "none"
            self._print(f"<dim>Subscribers: {names}</dim>")
            return True

        if cmd == "/replay":
            if self.history_file.exists():
                for line in self.history_file.read_text().splitlines():
                    line = line.strip()
                    if line:
                        try:
                            self._print_msg(json.loads(line))
                        except json.JSONDecodeError:
                            continue
            return True

        if cmd in ("/help", "/h", "/?"):
            self._print(
                "<dim>/msg &lt;id&gt; &lt;text&gt;  send a direct message to an agent\n"
                "/body            toggle full message bodies\n"
                "/subs            show channel subscribers\n"
                "/replay          redisplay all messages\n"
                "/quit            exit (or Ctrl-D)</dim>"
            )
            return True

        if text.startswith("/msg ") or text.startswith("/dm "):
            parts = text.split(None, 2)
            if len(parts) < 3:
                self._print("<dim>Usage: /msg &lt;agent-id&gt; &lt;message&gt;</dim>")
                return True
            _, recipient, msg_text = parts
            now = datetime.now(timezone.utc)
            self._deliver_to_inbox(recipient, msg_text, "", now)
            self._print(f"<dim>Sent to {_esc(recipient)}</dim>")
            return True

        if cmd in ("/quit", "/q"):
            self._running = False
            return True

        self._print(f"<dim>Unknown command: {_esc(cmd)}. Try /help</dim>")
        return True

    async def run(self) -> None:
        self._subscribe()

        subs = self.read_subscribers()
        sub_names = ", ".join(subs) if subs else "none"
        self._print(
            f"<channel-name>#{_esc(self.channel)}</channel-name> "
            f"<dim>({len(subs)} subscriber(s): {sub_names})</dim>"
        )
        self._print("<dim>Type to send. /help for commands. Ctrl-D to quit.</dim>")
        self._print("<dim>" + "\u2500" * 60 + "</dim>")

        if self.history_file.exists():
            for line in self.history_file.read_text().splitlines():
                line = line.strip()
                if line:
                    try:
                        self._print_msg(json.loads(line))
                    except json.JSONDecodeError:
                        continue
            self._file_pos = self.history_file.stat().st_size

        tail_task = asyncio.create_task(self._tail())

        session = PromptSession(history=InMemoryHistory())
        try:
            with patch_stdout():
                while self._running:
                    try:
                        text = await session.prompt_async(
                            HTML(
                                f"<prompt-user>{_esc(self.user)}</prompt-user>"
                                f"<prompt-arrow>&gt;</prompt-arrow> "
                            ),
                            style=STYLE,
                        )
                        text = text.strip()
                        if not text:
                            continue
                        if text.startswith("/"):
                            if self._handle_command(text):
                                continue
                        self.send_message(text)
                    except (EOFError, KeyboardInterrupt):
                        break
        finally:
            self._running = False
            tail_task.cancel()
            try:
                await tail_task
            except asyncio.CancelledError:
                pass
            self._unsubscribe()
            self._print("<dim>Disconnected.</dim>")


def main():
    parser = argparse.ArgumentParser(
        description="Kiln channel viewer",
        usage="python -m kiln.tui.channels CHANNEL [--as USER] [--home PATH]",
    )
    parser.add_argument("channel", help="Channel name to view")
    parser.add_argument(
        "--as", dest="user", default="user",
        help="User identity for sending messages (default: user)",
    )
    parser.add_argument(
        "--home", type=Path, default=None,
        help="Agent home directory",
    )
    args = parser.parse_args()

    viewer = ChannelViewer(channel=args.channel, user=args.user, home=args.home)
    asyncio.run(viewer.run())


if __name__ == "__main__":
    main()
