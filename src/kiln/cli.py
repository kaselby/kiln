"""Kiln CLI — launch and manage agent sessions."""

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

from .config import AgentConfig, apply_template, load_agent_spec
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
        "--last", dest="last_session", action="store_true",
        help="Resume the most recent session",
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
    parser.add_argument(
        "--effort",
        choices=["low", "medium", "high"],
        default=None,
        help="Thinking effort level (low, medium, high). Default: high.",
    )
    parser.add_argument(
        "--template",
        default=None,
        help="Session template — partial config override from <home>/templates/<name>.yml",
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
    parser.add_argument(
        "--harness", action="store_true",
        help="Scaffold a custom harness project (for agents with their own CLI)",
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

def _cli_bin() -> str:
    """Resolve the current CLI binary for use in subprocesses and continuations."""
    name = Path(sys.argv[0]).name
    return shutil.which(name) or sys.argv[0]


def _build_inner_command(args: argparse.Namespace, agent_id: str, spec_path: Path) -> str:
    """Build the shell command that runs inside the tmux session."""
    cmd_parts = [_cli_bin(), "run", str(spec_path), "--id", agent_id]
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
    if args.last_session:
        cmd_parts.append("--last")
    if args.resume:
        cmd_parts += ["--resume", args.resume]
    if args.heartbeat is not None:
        cmd_parts += ["--heartbeat", args.heartbeat]
    if args.idle_nudge is not None:
        cmd_parts += ["--idle-nudge", args.idle_nudge]
    if getattr(args, "effort", None):
        cmd_parts += ["--effort", args.effort]
    if getattr(args, "template", None):
        cmd_parts += ["--template", args.template]
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
            '    echo "Agent exited with status $EXIT_CODE. Closing in 30s (Enter to close now)."\n'
            '    read -t 30\n'
            'fi'
        )
    else:
        shell_script = (
            f'{inner_cmd}\n'
            'echo ""\n'
            'echo "Agent exited with status $?. Closing in 30s (Enter to close now)."\n'
            'read -t 30'
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


def cmd_run(args: argparse.Namespace, *, harness_class=None) -> None:
    """Handle 'kiln run'.

    Custom agent CLIs can pass their own harness_class to override
    KilnHarness. The binary name for self-continuation is derived
    from sys.argv[0].
    """
    spec_path = _find_agent_spec(args.spec)
    config = load_agent_spec(spec_path)

    # Apply session template (before CLI overrides, so flags always win)
    if getattr(args, "template", None):
        apply_template(config, args.template)

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
    if args.resume:
        config.resume_session = args.resume
    if args.mode:
        config.initial_mode = args.mode
    if args.heartbeat is not None:
        config.heartbeat = True
        config.heartbeat_max = float(args.heartbeat) * 60
    if args.idle_nudge is not None:
        config.idle_nudge_timeout = float(args.idle_nudge) * 60
    if getattr(args, "effort", None):
        config.effort = args.effort

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

    # --last resolves to --resume <most-recent-agent-id>
    if args.last_session and not config.resume_session:
        resolved_id = _most_recent_agent_id(config)
        if resolved_id:
            config.resume_session = resolved_id

    # If not inside our tmux guard, launch through tmux
    if not os.environ.get(_TMUX_GUARD) or args.detach:
        _launch_in_tmux(args, config, spec_path)
        return

    # --- Inner execution (inside tmux) ---

    if harness_class is None:
        from .harness import KilnHarness
        harness_class = KilnHarness
    harness = harness_class(config)

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
        cli_bin = _cli_bin()
        cont = getattr(harness, '_continuation_state', {})
        exec_args = [cli_bin, "run", str(spec_path.resolve()),
                     "--mode", "yolo",
                     "--parent", harness.agent_id, "--continuation"]
        if cont.get('heartbeat_enabled'):
            exec_args += ["--heartbeat", str(int(cont.get('heartbeat_max', 600) / 60))]
        if args.model:
            exec_args += ["--model", args.model]
        if getattr(args, "effort", None):
            exec_args += ["--effort", args.effort]
        if args.persistent:
            exec_args.append("--persistent")
        if getattr(args, "template", None):
            exec_args += ["--template", args.template]
        if config.idle_nudge_timeout > 0:
            exec_args += ["--idle-nudge", str(int(config.idle_nudge_timeout / 60))]
        if harness.handoff_text:
            import tempfile
            fd, path = tempfile.mkstemp(prefix="kiln-handoff-", suffix=".md")
            os.write(fd, harness.handoff_text.encode())
            os.close(fd)
            exec_args += ["--prompt-file", path]
        os.execvp(cli_bin, exec_args)

    if harness.restart_requested:
        cli_bin = _cli_bin()
        os.execvp(cli_bin, [cli_bin, "run", str(spec_path.resolve())])


# ---------------------------------------------------------------------------
# kiln init
# ---------------------------------------------------------------------------

def _scaffold_harness(target: Path, name: str) -> None:
    """Create a custom harness project inside the agent home."""
    harness_dir = target / "harness"
    pkg_dir = harness_dir / "src" / name
    pkg_dir.mkdir(parents=True)

    # pyproject.toml
    (harness_dir / "pyproject.toml").write_text(
        f'[build-system]\n'
        f'requires = ["hatchling"]\n'
        f'build-backend = "hatchling.build"\n'
        f'\n'
        f'[project]\n'
        f'name = "{name}"\n'
        f'version = "0.1.0"\n'
        f'requires-python = ">=3.12"\n'
        f'dependencies = [\n'
        f'    "kiln",\n'
        f']\n'
        f'\n'
        f'[project.scripts]\n'
        f'{name} = "{name}.cli:main"\n'
        f'\n'
        f'[tool.hatch.build.targets.wheel]\n'
        f'packages = ["src/{name}"]\n'
        f'\n'
        f'[tool.uv.sources]\n'
        f'kiln = {{ path = "{Path(__file__).resolve().parent.parent.parent}", editable = true }}\n'
    )

    # __init__.py
    (pkg_dir / "__init__.py").write_text("")

    # cli.py
    (pkg_dir / "cli.py").write_text(
        f'"""CLI for {name} — wraps kiln with a custom harness."""\n'
        f'\n'
        f'import argparse\n'
        f'import sys\n'
        f'from pathlib import Path\n'
        f'\n'
        f'from kiln.cli import _parse_run_args, cmd_list, cmd_run\n'
        f'\n'
        f'from {name}.harness import {name.title()}Harness\n'
        f'\n'
        f'{name.upper()}_HOME = Path.home() / ".{name}"\n'
        f'\n'
        f'\n'
        f'def parse_args() -> argparse.Namespace:\n'
        f'    parser = argparse.ArgumentParser(prog="{name}")\n'
        f'    sub = parser.add_subparsers(dest="command")\n'
        f'    run_parser = sub.add_parser("run", help="Launch a session")\n'
        f'    _parse_run_args(run_parser)\n'
        f'    sub.add_parser("list", help="List known sessions")\n'
        f'    if not sys.argv[1:] or sys.argv[1] not in ("run", "list", "-h", "--help"):\n'
        f'        run_ns = argparse.Namespace(command="run")\n'
        f'        run_parser.parse_args(sys.argv[1:], namespace=run_ns)\n'
        f'        return run_ns\n'
        f'    return parser.parse_args()\n'
        f'\n'
        f'\n'
        f'def main():\n'
        f'    args = parse_args()\n'
        f'    if args.command == "run":\n'
        f'        args.spec = args.spec or str({name.upper()}_HOME)\n'
        f'        cmd_run(args, harness_class={name.title()}Harness)\n'
        f'    elif args.command == "list":\n'
        f'        cmd_list(args)\n'
    )

    # harness.py
    (pkg_dir / "harness.py").write_text(
        f'"""Custom harness for {name}."""\n'
        f'\n'
        f'from kiln.harness import KilnHarness\n'
        f'\n'
        f'\n'
        f'class {name.title()}Harness(KilnHarness):\n'
        f'    """Extend KilnHarness with {name}-specific behavior."""\n'
        f'    pass\n'
    )


def cmd_init(args: argparse.Namespace) -> None:
    """Scaffold a new agent home directory."""
    target = Path(args.dir or args.name)
    if target.exists():
        print(f"Error: {target} already exists")
        sys.exit(1)

    target.mkdir(parents=True)

    # agent.yml
    doc_name = f"{args.name.upper()}.md"
    spec = (
        f"name: {args.name}\n"
        f"identity_doc: {doc_name}\n"
        f"model: {args.model}\n"
    )
    (target / "agent.yml").write_text(spec)

    # Identity doc
    (target / doc_name).write_text(
        f"# {args.name}\n\nYou are {args.name}, an AI agent.\n"
    )

    # Standard directories
    for d in ["inbox", "logs", "memory", "plans", "scratch", "state"]:
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

    # Optional: custom harness project
    if args.harness:
        _scaffold_harness(target, args.name)

    print(f"Agent scaffolded at {target}/")
    if args.harness:
        print(f"  Install the harness:  uv tool install --editable {target}/harness")
        print(f"  Then launch with:     {args.name}")
    else:
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
