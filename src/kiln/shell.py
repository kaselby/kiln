"""Persistent bash subprocess with sentinel-based output capture.

Provides a long-lived bash process that maintains state (env vars, cwd,
aliases) across commands. Uses a shell stack to support nested contexts
(SSH, docker exec, etc.) — the sentinel protocol works identically through
any transport that provides a bash subprocess on the other end of stdin/stdout.
"""

import asyncio
import os
import re
import shlex
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from time import monotonic

# SSH options that take an argument (from man ssh).
# Used to parse ssh commands and distinguish host from remote command.
_SSH_ARG_FLAGS = set("bcDEeFIiJLlmOopQRSWw")


def safe_getcwd() -> str:
    """Return the current working directory, falling back to ~ if it's been deleted."""
    try:
        return os.getcwd()
    except OSError:
        home = os.path.expanduser("~")
        try:
            os.chdir(home)
        except OSError:
            pass
        return home


@dataclass
class ShellEntry:
    """One level in the shell stack."""
    process: asyncio.subprocess.Process
    cwd: str
    label: str  # "local", "ssh:user@host", "docker:container", etc.
    spawn_args: list[str] = field(default_factory=list)


def _parse_ssh_command(command: str) -> tuple[list[str], str] | None:
    """Detect interactive SSH commands and return (spawn_args, label).

    Returns None if the command isn't SSH or has a remote command
    (making it non-interactive and suitable for normal execution).
    """
    stripped = command.strip()
    if not re.match(r"^ssh\s", stripped):
        return None

    try:
        args = shlex.split(stripped)
    except ValueError:
        return None

    # Walk args (skip 'ssh'), consuming flags and their arguments.
    # Positional args are the hostname and optional remote command.
    positional = []
    i = 1
    while i < len(args):
        arg = args[i]
        if arg == "--":
            # Everything after -- is the remote command
            positional.extend(args[i + 1 :])
            break
        elif arg.startswith("-") and len(arg) >= 2 and not arg.startswith("--"):
            # Short flags: could be combined like -vvi keyfile
            flags = arg[1:]
            for j, ch in enumerate(flags):
                if ch in _SSH_ARG_FLAGS:
                    if j + 1 < len(flags):
                        pass  # arg value is inline (e.g., -ikey)
                    else:
                        i += 1  # arg value is next token
                    break
            i += 1
        else:
            positional.append(arg)
            i += 1

    if len(positional) != 1:
        # Either no host (weird) or has a remote command — don't intercept
        return None

    host = positional[0]
    # Build spawn args from original command + bash on the remote end
    ssh_args = args[:]
    if ssh_args and ssh_args[-1] == "--":
        ssh_args.pop()  # remove trailing bare --
    # Auto-accept new host keys to avoid interactive prompts.
    # accept-new still rejects *changed* keys (MITM protection).
    ssh_args.insert(1, "-o")
    ssh_args.insert(2, "StrictHostKeyChecking=accept-new")
    ssh_args.extend(["--", "bash", "--norc", "--noprofile"])
    return (ssh_args, f"ssh:{host}")


class PersistentShell:
    """A persistent bash subprocess that maintains state across commands.

    Supports a stack of shell contexts for nested sessions (SSH, docker,
    etc.). The sentinel protocol works identically at every level since
    it just needs a bash process on the other end of stdin/stdout.
    """

    def __init__(self, cwd: str | None = None, env: dict[str, str] | None = None):
        self._initial_cwd = cwd or safe_getcwd()
        self._env = self._build_env(env)
        self._stack: list[ShellEntry] = []
        self._lock = asyncio.Lock()
        self._background_jobs: dict[str, dict] = {}

    @property
    def cwd(self) -> str:
        """The current working directory (local or remote)."""
        if self._stack:
            return self._stack[-1].cwd
        return self._initial_cwd

    @property
    def label(self) -> str:
        """Label for the current shell context."""
        if self._stack:
            return self._stack[-1].label
        return "local"

    @staticmethod
    def _build_env(overrides: dict[str, str] | None) -> dict[str, str]:
        """Build a clean environment, stripping CLAUDE* vars."""
        base = dict(os.environ)
        for key in list(base):
            if key.startswith("CLAUDE"):
                del base[key]
        if overrides:
            base.update(overrides)
        return base

    async def _spawn_process(
        self, spawn_args: list[str] | None = None, cwd: str | None = None
    ) -> asyncio.subprocess.Process:
        """Spawn a subprocess. Defaults to local bash.

        Falls back to ~ if the requested cwd has been deleted.
        """
        if spawn_args is None:
            spawn_args = ["bash", "--norc", "--noprofile"]
        # Only set cwd for local processes — remote shells ignore it
        effective_cwd = cwd if spawn_args[0] != "ssh" else None
        # Validate cwd exists before spawning — deleted dirs cause FileNotFoundError
        if effective_cwd and not os.path.isdir(effective_cwd):
            effective_cwd = os.path.expanduser("~")
        return await asyncio.create_subprocess_exec(
            *spawn_args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=effective_cwd,
            env=self._env,
        )

    async def _ensure_alive(self) -> list[str]:
        """Ensure there's a running shell, spawning one if needed.

        Returns a list of messages about shells that were popped
        (e.g. dead SSH connections discovered between commands).
        """
        messages = []
        while self._stack and self._stack[-1].process.returncode is not None:
            dead = self._stack.pop()
            if dead.label != "local":
                messages.append(f"[Connection to {dead.label} lost]")

        if not self._stack:
            if not os.path.isdir(self._initial_cwd):
                old = self._initial_cwd
                self._initial_cwd = os.path.expanduser("~")
                messages.append(
                    f"[Working directory {old} no longer exists — "
                    f"fell back to {self._initial_cwd}]"
                )
            proc = await self._spawn_process(cwd=self._initial_cwd)
            self._stack.append(
                ShellEntry(process=proc, cwd=self._initial_cwd, label="local")
            )

        return messages

    async def push_shell(self, spawn_args: list[str], label: str) -> str:
        """Push a new shell context onto the stack.

        Verifies the connection by running a test command. Raises
        ConnectionError if the shell doesn't respond.
        """
        proc = await self._spawn_process(spawn_args)

        # Verify the shell is alive and get initial cwd
        sentinel = f"___KILN_{uuid.uuid4().hex}___"
        test = f"printf '{sentinel}%s %s\\n' 0 \"$(pwd)\"\n"
        proc.stdin.write(test.encode())
        await proc.stdin.drain()

        cwd = "~"

        async def _read_test():
            nonlocal cwd
            while True:
                line = await proc.stdout.readline()
                if not line:
                    raise ConnectionError("Shell process died during startup")
                decoded = line.decode("utf-8", errors="replace")
                if sentinel in decoded:
                    after = decoded.split(sentinel)[1].strip()
                    parts = after.split(" ", 1)
                    cwd = parts[1] if len(parts) > 1 else "~"
                    return

        try:
            await asyncio.wait_for(_read_test(), timeout=30.0)
        except asyncio.TimeoutError:
            proc.kill()
            raise ConnectionError(f"Connection to {label} timed out")
        except ConnectionError:
            proc.kill()
            raise

        self._stack.append(
            ShellEntry(process=proc, cwd=cwd, label=label, spawn_args=spawn_args)
        )
        return f"Connected to {label} (cwd: {cwd})"

    async def pop_shell(self) -> str:
        """Pop the current shell and return to the previous one."""
        if len(self._stack) <= 1:
            return "Already on local shell"

        entry = self._stack.pop()
        if entry.process.returncode is None:
            entry.process.kill()
            try:
                await asyncio.wait_for(entry.process.wait(), timeout=2)
            except asyncio.TimeoutError:
                pass

        current = self._stack[-1].label if self._stack else "local"
        return f"Disconnected from {entry.label} (back to {current})"

    def _check_shell_command(self, command: str) -> tuple[list[str], str] | None:
        """Check if a command should trigger a shell push.

        Returns (spawn_args, label) or None. Add new handlers here
        for docker exec, kubectl exec, etc.
        """
        return _parse_ssh_command(command)

    async def run(self, command: str, timeout_ms: int = 120_000) -> dict:
        """Run a command and return its output, exit code, and metadata.

        Returns:
            {
                "output": str,       # stdout+stderr combined
                "exit_code": int,
                "cwd": str,          # working directory after command
                "label": str,        # current shell context label
                "timestamp": str,    # ISO timestamp when command started
                "elapsed_ms": int,   # wall-clock milliseconds
                "timed_out": bool,
            }
        """
        async with self._lock:
            pop_messages = await self._ensure_alive()

            stripped = command.strip()

            # 'exit' or 'logout' on a non-root shell → clean pop
            if stripped in ("exit", "logout") and len(self._stack) > 1:
                old_label = self._stack[-1].label
                msg = await self.pop_shell()
                return {
                    "output": msg + "\n",
                    "exit_code": 0,
                    "cwd": self.cwd,
                    "label": self.label,
                    "timestamp": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
                    "elapsed_ms": 0,
                    "timed_out": False,
                }

            # Check for interactive shell commands (ssh, etc.)
            shell_cmd = self._check_shell_command(stripped)
            if shell_cmd:
                spawn_args, label = shell_cmd
                timestamp = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
                start = monotonic()
                try:
                    msg = await self.push_shell(spawn_args, label)
                    return {
                        "output": msg + "\n",
                        "exit_code": 0,
                        "cwd": self.cwd,
                        "label": self.label,
                        "timestamp": timestamp,
                        "elapsed_ms": int((monotonic() - start) * 1000),
                        "timed_out": False,
                    }
                except ConnectionError as e:
                    return {
                        "output": str(e) + "\n",
                        "exit_code": 1,
                        "cwd": self.cwd,
                        "label": self.label,
                        "timestamp": timestamp,
                        "elapsed_ms": int((monotonic() - start) * 1000),
                        "timed_out": False,
                    }

            # --- Normal command execution ---
            entry = self._stack[-1]
            proc = entry.process
            assert proc.stdin and proc.stdout

            sentinel = f"___KILN_{uuid.uuid4().hex}___"
            timestamp = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
            start = monotonic()

            wrapped = (
                f"{command}\n"
                f"__kiln_ec=$?\n"
                f"printf '\\n{sentinel}%s %s\\n' \"$__kiln_ec\" \"$(pwd)\"\n"
            )
            proc.stdin.write(wrapped.encode())
            await proc.stdin.drain()

            timeout_s = timeout_ms / 1000.0
            output_lines = []
            timed_out = False
            exit_code = -1
            cwd = entry.cwd
            auto_popped = False

            async def _read_until_sentinel():
                nonlocal exit_code, cwd, auto_popped
                while True:
                    line = await proc.stdout.readline()
                    if not line:
                        # EOF — process died
                        if len(self._stack) > 1:
                            old_label = entry.label
                            self._stack.pop()
                            auto_popped = True
                            output_lines.append(
                                f"\n[Connection to {old_label} lost — "
                                f"returned to {self.label}]\n"
                            )
                        break
                    decoded = line.decode("utf-8", errors="replace")
                    if sentinel in decoded:
                        after = decoded.split(sentinel)[1].strip()
                        parts = after.split(" ", 1)
                        try:
                            exit_code = int(parts[0]) if parts else -1
                        except ValueError:
                            exit_code = -1
                        reported_cwd = parts[1] if len(parts) > 1 else None
                        # pwd can return stale/error output if cwd was deleted
                        if reported_cwd and os.path.isdir(reported_cwd):
                            cwd = reported_cwd
                            entry.cwd = cwd
                        else:
                            cwd = entry.cwd  # keep last known good
                        break
                    output_lines.append(decoded)

            try:
                await asyncio.wait_for(_read_until_sentinel(), timeout=timeout_s)
            except asyncio.TimeoutError:
                timed_out = True
                exit_code = -1
                cwd = entry.cwd
                try:
                    proc.send_signal(2)  # SIGINT
                    await asyncio.sleep(0.5)
                    if proc.returncode is None:
                        proc.kill()
                except ProcessLookupError:
                    pass
                # Remove dead entry, will respawn/pop on next call
                if self._stack and self._stack[-1] is entry:
                    self._stack.pop()

            elapsed_ms = int((monotonic() - start) * 1000)
            output = "".join(output_lines)
            if len(output) > 30_000:
                output = output[:30_000] + "\n... [output truncated at 30000 chars]"

            # Prepend notifications about shells that died between commands
            if pop_messages:
                prefix = "\n".join(pop_messages) + "\n"
                output = prefix + output

            return {
                "output": output,
                "exit_code": 0 if auto_popped else exit_code,
                "cwd": self.cwd if auto_popped else cwd,
                "label": self.label,
                "timestamp": timestamp,
                "elapsed_ms": elapsed_ms,
                "timed_out": timed_out,
            }

    async def run_background(self, command: str) -> dict:
        """Start a command in the background and return immediately.

        Spawns the command via nohup in a subshell, writing output to a
        temp file. Returns {"job_id": str, "pid": int}.

        Works on both local and remote shells — all file paths and PIDs
        are on whichever host the current shell is running on.
        """
        job_id = uuid.uuid4().hex[:10]
        output_file = f"/tmp/kiln-bg-{job_id}.out"
        pid_file = f"/tmp/kiln-bg-{job_id}.pid"
        exitcode_file = f"/tmp/kiln-bg-{job_id}.exit"

        inner = f"{command}; echo $? > {exitcode_file}"
        wrapper = (
            f"nohup bash -c {shlex.quote(inner)} "
            f"> {output_file} 2>&1 & "
            f"echo $! > {pid_file}; echo $!"
        )
        result = await self.run(wrapper, timeout_ms=10_000)
        pid_str = (
            result["output"].strip().splitlines()[-1]
            if result["output"].strip()
            else "0"
        )
        try:
            pid = int(pid_str)
        except ValueError:
            pid = 0

        self._background_jobs[job_id] = {
            "pid": pid,
            "output_file": output_file,
            "pid_file": pid_file,
            "exitcode_file": exitcode_file,
        }

        return {"job_id": job_id, "pid": pid}

    async def check_background(self, job_id: str) -> dict:
        """Check status of a background job.

        Runs status checks through the shell so they work on both local
        and remote hosts — whichever shell is currently active.

        Returns {"running": bool, "output": str (tail of output), "exit_code": int|None}.
        """
        jobs = self._background_jobs
        job = jobs.get(job_id)
        if not job:
            return {
                "running": False,
                "output": f"Unknown job_id: {job_id}",
                "exit_code": None,
            }

        pid = job["pid"]
        output_file = job["output_file"]
        exitcode_file = job["exitcode_file"]

        # Check if process is still running
        running = False
        if pid:
            r = await self.run(
                f"kill -0 {pid} 2>/dev/null", timeout_ms=10_000
            )
            running = r["exit_code"] == 0

        # Read tail of output
        r = await self.run(
            f"tail -c 5000 {shlex.quote(output_file)} 2>/dev/null",
            timeout_ms=10_000,
        )
        output = r["output"] if r["exit_code"] == 0 else "(no output yet)"

        # Get exit code if process finished
        exit_code = None
        if not running and pid:
            r = await self.run(
                f"cat {shlex.quote(exitcode_file)} 2>/dev/null",
                timeout_ms=10_000,
            )
            if r["exit_code"] == 0:
                try:
                    exit_code = int(r["output"].strip())
                except ValueError:
                    pass

        return {"running": running, "output": output, "exit_code": exit_code}

    async def cleanup_background(self, job_id: str) -> None:
        """Remove temp files for a background job and forget it.

        Safe to call on unknown or already-cleaned job IDs — no-ops silently.
        Runs the deletions through the shell so it works for remote jobs too.
        """
        job = self._background_jobs.pop(job_id, None)
        if not job:
            return

        files = [
            job["output_file"],
            job.get("pid_file", f"/tmp/kiln-bg-{job_id}.pid"),
            job["exitcode_file"],
        ]
        rm_cmd = "rm -f " + " ".join(shlex.quote(f) for f in files)
        try:
            await self.run(rm_cmd, timeout_ms=5_000)
        except Exception:
            pass  # best-effort cleanup

    async def restart(self):
        """Kill all shells and start fresh."""
        async with self._lock:
            for entry in reversed(self._stack):
                if entry.process.returncode is None:
                    entry.process.kill()
                    try:
                        await asyncio.wait_for(entry.process.wait(), timeout=2)
                    except asyncio.TimeoutError:
                        pass
            self._stack.clear()
            if not os.path.isdir(self._initial_cwd):
                self._initial_cwd = os.path.expanduser("~")

    async def close(self):
        """Terminate all shells gracefully."""
        for entry in reversed(self._stack):
            if entry.process.returncode is None:
                entry.process.terminate()
                try:
                    await asyncio.wait_for(entry.process.wait(), timeout=5)
                except asyncio.TimeoutError:
                    entry.process.kill()
        self._stack.clear()

    def __del__(self):
        """Last-resort cleanup: kill all subprocesses via OS signal."""
        for entry in self._stack:
            if entry.process.returncode is None:
                pid = entry.process.pid
                if pid:
                    import os as _os
                    import signal

                    try:
                        _os.kill(pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
