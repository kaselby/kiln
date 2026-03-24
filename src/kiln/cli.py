"""Kiln CLI — launch and manage agent sessions."""

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

from .config import AgentConfig, load_agent_spec
from .names import generate_agent_name
from .registry import lookup_session

# Env var set inside the tmux session to prevent the inner process
# from trying to create another tmux session (infinite recursion).
_TMUX_GUARD = "KILN_IN_TMUX"


def _find_agent_spec(spec_arg: str | None) -> Path:
    """Resolve the agent spec path.

    Searches: explicit path > ./agent.yml > ~/.<name>/agent.yml > error.
    """
    if spec_arg:
        p = Path(spec_arg)
        if p.is_dir():
            p = p / "agent.yml"
        if p.exists():
            return p

        # Try ~/.<name>/agent.yml as a shorthand (e.g. "kiln run beth")
        home_spec = Path.home() / f".{spec_arg}" / "agent.yml"
        if home_spec.exists():
            return home_spec

        raise FileNotFoundError(f"Agent spec not found: {spec_arg}")

    # Default: look in current directory
    cwd_spec = Path("agent.yml")
    if cwd_spec.exists():
        return cwd_spec

    raise FileNotFoundError(
        "No agent spec found. Pass a path or run from a directory with agent.yml."
    )


def _parse_run_args(parser: argparse.ArgumentParser) -> None:
    """Add arguments for the 'run' subcommand."""
    parser.add_argument(
        "spec",
        nargs="?",
        help="Path to agent.yml or directory containing one (default: ./agent.yml)",
    )
    parser.add_argument("--id", help="Agent identifier (auto-generated if not provided)")
    parser.add_argument("--project", help="Project directory (sets working directory)")
    parser.add_argument("--model", help="Model override")
    parser.add_argument("--parent", help="Parent agent ID (for spawned subagents)")
    parser.add_argument("--prompt", help="Initial prompt sent on session start")
    parser.add_argument(
        "--prompt-file",
        help="Read initial prompt from a file (alternative to --prompt)",
    )
    parser.add_argument("--depth", type=int, default=0, help="Spawning depth")
    parser.add_argument(
        "--persistent", action="store_true",
        help="Persistent peer instance: self-continues, coordinates with parent",
    )
    parser.add_argument(
        "--continue", dest="continue_session", action="store_true",
        help="Continue the most recent session instead of starting fresh",
    )
    parser.add_argument(
        "--resume", metavar="AGENT_ID",
        help="Resume a specific session by agent ID",
    )
    parser.add_argument(
        "--mode", choices=["safe", "supervised", "yolo"], default=None,
        help="Initial permission mode (trusted mode is TUI-only)",
    )
    parser.add_argument(
        "--detach", action="store_true",
        help="Don't attach to the tmux session after launch",
    )
    parser.add_argument(
        "--heartbeat", nargs="?", const="10", default=None, metavar="MINUTES",
        help="Enable heartbeat (nudge agent after idle). Default 10 min.",
    )
    parser.add_argument(
        "--idle-nudge", dest="idle_nudge", default=None, metavar="MINUTES",
        help="Send idle nudge after N minutes of inactivity. 0 to disable.",
    )
    parser.add_argument(
        "--continuation", action="store_true",
        help=argparse.SUPPRESS,  # internal: set by self-continuation exec
    )


def _parse_init_args(parser: argparse.ArgumentParser) -> None:
    """Add arguments for the 'init' subcommand."""
    parser.add_argument("name", help="Agent name")
    parser.add_argument(
        "--dir", default=None,
        help="Directory to create (default: ./<name>)",
    )
    parser.add_argument(
        "--model", default="claude-sonnet-4-6",
        help="Model (default: claude-sonnet-4-6)",
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="kiln",
        description="Kiln — agent runtime for Claude Code",
    )
    sub = parser.add_subparsers(dest="command")

    run_parser = sub.add_parser("run", help="Launch an agent session")
    _parse_run_args(run_parser)

    init_parser = sub.add_parser("init", help="Scaffold a new agent home")
    _parse_init_args(init_parser)

    sub.add_parser("list", help="List known sessions")

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)
    return args


# ---------------------------------------------------------------------------
# kiln run
# ---------------------------------------------------------------------------

def _build_inner_command(args: argparse.Namespace, agent_id: str, spec_path: Path) -> str:
    """Build the shell command that runs inside the tmux session."""
    cmd_parts = [shutil.which("kiln") or "kiln", "run", str(spec_path), "--id", agent_id]
    if args.project:
        cmd_parts += ["--project", args.project]
    if args.model:
        cmd_parts += ["--model", args.model]
    if args.parent:
        cmd_parts += ["--parent", args.parent]
    if args.prompt:
        cmd_parts += ["--prompt", args.prompt]
    if args.prompt_file:
        cmd_parts += ["--prompt-file", args.prompt_file]
    if args.depth:
        cmd_parts += ["--depth", str(args.depth)]
    if args.persistent:
        cmd_parts.append("--persistent")
    if args.mode:
        cmd_parts += ["--mode", args.mode]
    if args.continue_session:
        cmd_parts.append("--continue")
    if args.resume:
        cmd_parts += ["--resume", args.resume]
    if args.heartbeat is not None:
        cmd_parts += ["--heartbeat", args.heartbeat]
    if args.idle_nudge is not None:
        cmd_parts += ["--idle-nudge", args.idle_nudge]
    return shlex.join(cmd_parts)


def _launch_in_tmux(args: argparse.Namespace, config: AgentConfig, spec_path: Path) -> None:
    """Launch the agent in a tmux session."""
    if not shutil.which("tmux"):
        import platform
        if platform.system() == "Darwin":
            hint = "brew install tmux"
        else:
            hint = "apt install tmux  (or your distro's package manager)"
        print(f"Error: tmux is not installed. Install it with: {hint}")
        sys.exit(1)

    base_prefix = config.session_prefix.rstrip("-")
    agent_id = config.agent_id or config.resume_session or generate_agent_name(
        prefix=base_prefix,
        worklogs_dir=config.worklogs_path,
    )
    inner_cmd = _build_inner_command(args, agent_id, spec_path.resolve())

    if args.detach:
        shell_script = (
            f'{inner_cmd}\n'
            'EXIT_CODE=$?\n'
            'if [ $EXIT_CODE -ne 0 ]; then\n'
            '    echo ""\n'
            '    echo "Agent exited with status $EXIT_CODE. Press Enter to close."\n'
            '    read\n'
            'fi'
        )
    else:
        shell_script = (
            f'{inner_cmd}\n'
            'echo ""\n'
            'echo "Agent exited with status $?. Press Enter to close."\n'
            'read'
        )

    result = subprocess.run(
        ["tmux", "new-session", "-d", "-s", agent_id, "-e", f"{_TMUX_GUARD}=1",
         "bash", "-c", shell_script],
        capture_output=True, text=True,
    )

    if result.returncode != 0:
        print(f"Error launching tmux session: {result.stderr.strip()}")
        sys.exit(1)

    if args.detach:
        print(f"Agent session started: {agent_id}")
        print(f"  tmux attach -t {agent_id}")
    else:
        if os.environ.get("TMUX"):
            os.execvp("tmux", ["tmux", "switch-client", "-t", agent_id])
        else:
            os.execvp("tmux", ["tmux", "attach", "-t", agent_id])


def _start_caffeinate() -> int | None:
    """Start caffeinate to prevent system sleep on macOS. Returns PID or None."""
    if not shutil.which("caffeinate"):
        return None
    try:
        proc = subprocess.Popen(
            ["caffeinate", "-i"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return proc.pid
    except OSError:
        return None


def _stop_caffeinate(pid: int | None) -> None:
    if pid is None:
        return
    try:
        os.kill(pid, 15)
    except (ProcessLookupError, OSError):
        pass


def _most_recent_agent_id(config: AgentConfig) -> str | None:
    """Find the most recent agent ID from the session registry."""
    from .registry import most_recent_agent_id
    registry_path = config.home / "logs" / "session-registry.json"
    return most_recent_agent_id(registry_path)


def cmd_run(args: argparse.Namespace) -> None:
    """Handle 'kiln run'."""
    spec_path = _find_agent_spec(args.spec)
    config = load_agent_spec(spec_path)

    # Apply CLI overrides
    if args.id:
        config.agent_id = args.id
    if args.model:
        config.model = args.model
    if args.project:
        config.project = args.project
    if args.parent:
        config.parent = args.parent
    if args.depth:
        config.depth = args.depth
    if args.persistent:
        config.persistent = True
    if args.continuation:
        config.continuation = True
    if args.continue_session:
        config.continue_session = True
    if args.resume:
        config.resume_session = args.resume
    if args.mode:
        config.initial_mode = args.mode
    if args.heartbeat is not None:
        config.heartbeat = True
        config.heartbeat_max = float(args.heartbeat) * 60
    if args.idle_nudge is not None:
        config.idle_nudge_timeout = float(args.idle_nudge) * 60

    # Resolve prompt
    if args.prompt and getattr(args, "prompt_file", None):
        print("Error: --prompt and --prompt-file are mutually exclusive")
        sys.exit(1)
    prompt = args.prompt
    if getattr(args, "prompt_file", None):
        try:
            prompt = Path(args.prompt_file).read_text()
        except FileNotFoundError:
            print(f"Error: prompt file not found: {args.prompt_file}")
            sys.exit(1)
    if prompt:
        config.prompt = prompt

    # Resolve --continue agent ID before launching tmux so the session name is correct
    if args.continue_session and not args.id:
        resolved_id = _most_recent_agent_id(config)
        if resolved_id:
            config.agent_id = resolved_id

    # If not inside our tmux guard, launch through tmux
    if not os.environ.get(_TMUX_GUARD) or args.detach:
        _launch_in_tmux(args, config, spec_path)
        return

    # --- Inner execution (inside tmux) ---

    from .harness import KilnHarness
    harness = KilnHarness(config)

    # Rename tmux session on self-continuation
    if args.continuation and args.parent and os.environ.get("TMUX"):
        subprocess.run(
            ["tmux", "rename-session", "-t", args.parent, harness.agent_id],
            capture_output=True,
        )

    _caffeinate_pid = _start_caffeinate()

    # Import TUI here — it has heavy dependencies
    from .tui import KilnApp

    app = KilnApp(harness)
    try:
        app.run()
    finally:
        _stop_caffeinate(_caffeinate_pid)

    if harness.continue_requested:
        kiln_bin = shutil.which("kiln") or sys.argv[0]
        exec_args = [kiln_bin, "run", str(spec_path.resolve()),
                     "--mode", "yolo",
                     "--heartbeat", str(int(config.heartbeat_max / 60)),
                     "--parent", harness.agent_id, "--continuation"]
        if args.model:
            exec_args += ["--model", args.model]
        if args.persistent:
            exec_args.append("--persistent")
        if config.idle_nudge_timeout > 0:
            exec_args += ["--idle-nudge", str(int(config.idle_nudge_timeout / 60))]
        if harness.handoff_text:
            import tempfile
            fd, path = tempfile.mkstemp(prefix="kiln-handoff-", suffix=".md")
            os.write(fd, harness.handoff_text.encode())
            os.close(fd)
            exec_args += ["--prompt-file", path]
        os.execvp(kiln_bin, exec_args)

    if harness.restart_requested:
        kiln_bin = shutil.which("kiln") or sys.argv[0]
        os.execvp(kiln_bin, [kiln_bin, "run", str(spec_path.resolve())])


# ---------------------------------------------------------------------------
# kiln init
# ---------------------------------------------------------------------------

def cmd_init(args: argparse.Namespace) -> None:
    """Scaffold a new agent home directory."""
    target = Path(args.dir or args.name)
    if target.exists():
        print(f"Error: {target} already exists")
        sys.exit(1)

    target.mkdir(parents=True)

    # agent.yml
    spec = (
        f"name: {args.name}\n"
        f"identity_doc: identity.md\n"
        f"model: {args.model}\n"
    )
    (target / "agent.yml").write_text(spec)

    # identity.md
    (target / "identity.md").write_text(
        f"# {args.name}\n\nYou are {args.name}, an AI agent.\n"
    )

    # Standard directories
    for d in ["inbox", "plans", "logs"]:
        (target / d).mkdir()

    # Copy standard library (tools + skills) from kiln defaults.
    # After init, the agent owns these — edits, additions, deletions are theirs.
    defaults_dir = Path(__file__).resolve().parent.parent.parent / "defaults"
    for subdir in ["tools", "skills"]:
        src = defaults_dir / subdir
        dst = target / subdir
        if src.is_dir():
            shutil.copytree(src, dst)
        else:
            dst.mkdir()

    print(f"Agent scaffolded at {target}/")
    print(f"  Edit identity.md and agent.yml, then: kiln run {target}")


# ---------------------------------------------------------------------------
# kiln list
# ---------------------------------------------------------------------------

def cmd_list(args: argparse.Namespace) -> None:
    """List known sessions from registries."""
    # Search common locations for session registries
    candidates = [
        Path("logs") / "session-registry.json",
    ]
    # Also check ~/.{name}/logs/ for any agents registered in ~/.kiln/agents.yml
    agents_yml = Path.home() / ".kiln" / "agents.yml"
    if agents_yml.exists():
        try:
            import yaml as _yaml
            registry = _yaml.safe_load(agents_yml.read_text()) or {}
            for name, home_path in registry.items():
                p = Path(os.path.expanduser(str(home_path))) / "logs" / "session-registry.json"
                if p not in candidates:
                    candidates.insert(0, p)
        except Exception:
            pass

    registry_path = None
    for c in candidates:
        if c.exists():
            registry_path = c
            break

    if not registry_path:
        print("No session registry found.")
        return

    try:
        registry = json.loads(registry_path.read_text())
    except (json.JSONDecodeError, OSError) as e:
        print(f"Error reading registry: {e}")
        return

    if not registry:
        print("No sessions in registry.")
        return

    # Get running tmux sessions
    running = set()
    result = subprocess.run(
        ["tmux", "list-sessions", "-F", "#{session_name}"],
        capture_output=True, text=True,
    )
    if result.returncode == 0:
        running = set(result.stdout.strip().splitlines())

    entries = sorted(
        registry.items(),
        key=lambda kv: kv[1].get("started_at", ""),
        reverse=True,
    )

    for agent_id, info in entries:
        status = "\033[32mrunning\033[0m" if agent_id in running else "\033[90mdead\033[0m"
        started = info.get("started_at", "?")[:19]
        model = info.get("model") or "default"
        print(f"  {agent_id:<24} {status:<20} {started}  {model}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    args = parse_args()

    if args.command == "run":
        cmd_run(args)
    elif args.command == "init":
        cmd_init(args)
    elif args.command == "list":
        cmd_list(args)


if __name__ == "__main__":
    main()
