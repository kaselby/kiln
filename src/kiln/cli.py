"""Kiln CLI — launch and manage agent sessions."""

import argparse
import json
import os
import signal
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

    With a name/path argument:
      1. Explicit path (file or directory containing agent.yml)
      2. ~/.kiln/agents/<name>/agent.yml
      3. ~/.<name>/agent.yml  (legacy — Beth, Aleph, etc.)

    With no argument:
      1. ./agent.yml in the current directory
    """
    if spec_arg:
        p = Path(spec_arg)
        if p.is_dir():
            p = p / "agent.yml"
        if p.exists():
            return p

        # Standard location: ~/.kiln/agents/<name>/
        kiln_spec = Path.home() / ".kiln" / "agents" / spec_arg / "agent.yml"
        if kiln_spec.exists():
            return kiln_spec

        # Legacy: ~/.<name>/ (for agents with custom harnesses)
        legacy_spec = Path.home() / f".{spec_arg}" / "agent.yml"
        if legacy_spec.exists():
            return legacy_spec

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
    parser.add_argument(
        "--var",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="Extra template variable for orientation/cleanup (repeatable)",
    )


def _parse_init_args(parser: argparse.ArgumentParser) -> None:
    """Add arguments for the 'init' subcommand."""
    parser.add_argument("name", help="Agent name")
    parser.add_argument(
        "--dir", default=None,
        help="Directory to create (default: ~/.kiln/agents/<name>)",
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

    daemon_parser = sub.add_parser("daemon", help="Manage the Kiln daemon")
    daemon_sub = daemon_parser.add_subparsers(dest="daemon_command")
    daemon_start = daemon_sub.add_parser("start", help="Start the daemon")
    daemon_start.add_argument("--foreground", action="store_true",
                              help="Run in foreground (default: background)")
    daemon_sub.add_parser("stop", help="Stop the daemon")
    daemon_sub.add_parser("status", help="Show daemon status")
    daemon_logs = daemon_sub.add_parser("logs", help="View daemon logs")
    daemon_logs.add_argument("--follow", "-f", action="store_true",
                             help="Follow log output")

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)
    return args


# ---------------------------------------------------------------------------
# kiln run
# ---------------------------------------------------------------------------

def _build_inner_command(args: argparse.Namespace, agent_id: str, spec_path: Path) -> str:
    """Build the shell command that runs inside the tmux session.

    Uses sys.executable to re-enter via the same Python interpreter, preserving
    the active venv and import path. This makes worktree testing work (the inner
    tmux process uses the same code as the outer) and avoids fragile PATH-based
    binary resolution.
    """
    cmd_parts = [sys.executable, "-m", "kiln.cli", "run", str(spec_path), "--id", agent_id]
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
    for var_str in getattr(args, "var", []):
        cmd_parts += ["--var", var_str]
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


def _start_caffeinate() -> subprocess.Popen | None:
    """Start caffeinate to prevent system sleep on macOS. Returns Popen or None."""
    if not shutil.which("caffeinate"):
        return None
    try:
        return subprocess.Popen(
            ["caffeinate", "-i"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        return None


def _stop_caffeinate(proc: subprocess.Popen | None) -> None:
    if proc is None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except (ProcessLookupError, OSError, subprocess.TimeoutExpired):
        try:
            proc.kill()
        except (ProcessLookupError, OSError):
            pass


def _most_recent_agent_id(config: AgentConfig) -> str | None:
    """Find the most recent agent ID from the session registry."""
    from .registry import most_recent_agent_id
    registry_path = config.home / "logs" / "session-registry.json"
    return most_recent_agent_id(registry_path)


def _rebuild_run_args(args: argparse.Namespace, *, omit_spec: bool = False) -> list[str]:
    """Reconstruct CLI run args from a parsed namespace.

    Used by dispatch (to forward args to a custom CLI binary) and could
    later replace the manual reconstruction in _build_inner_command.
    """
    parts: list[str] = []
    if not omit_spec and getattr(args, "spec", None):
        parts.append(str(args.spec))
    if getattr(args, "id", None):
        parts += ["--id", args.id]
    if getattr(args, "model", None):
        parts += ["--model", args.model]
    if getattr(args, "parent", None):
        parts += ["--parent", args.parent]
    if getattr(args, "prompt", None):
        parts += ["--prompt", args.prompt]
    if getattr(args, "prompt_file", None):
        parts += ["--prompt-file", args.prompt_file]
    if getattr(args, "depth", None):
        parts += ["--depth", str(args.depth)]
    if getattr(args, "persistent", False):
        parts.append("--persistent")
    if getattr(args, "last_session", False):
        parts.append("--last")
    if getattr(args, "resume", None):
        parts += ["--resume", args.resume]
    if getattr(args, "mode", None):
        parts += ["--mode", args.mode]
    if getattr(args, "detach", False):
        parts.append("--detach")
    if getattr(args, "heartbeat", None) is not None:
        parts += ["--heartbeat", args.heartbeat]
    if getattr(args, "idle_nudge", None) is not None:
        parts += ["--idle-nudge", args.idle_nudge]
    if getattr(args, "continuation", False):
        parts.append("--continuation")
    if getattr(args, "effort", None):
        parts += ["--effort", args.effort]
    if getattr(args, "template", None):
        parts += ["--template", args.template]
    for var_str in getattr(args, "var", []):
        parts += ["--var", var_str]
    return parts


def cmd_run(args: argparse.Namespace, *, harness_class=None) -> None:
    """Handle 'kiln run'.

    Custom agent CLIs can pass their own harness_class to override
    KilnHarness. The binary name for self-continuation is derived
    from sys.argv[0].
    """
    spec_path = _find_agent_spec(args.spec)
    config = load_agent_spec(spec_path)

    # Dispatch to custom CLI if configured.
    # The harness_class guard prevents recursion: when a custom CLI (e.g. beth)
    # calls cmd_run(args, harness_class=BethHarness), dispatch is skipped.
    if config.cli and not harness_class:
        cli_bin = shutil.which(config.cli)
        if not cli_bin:
            print(f"Error: CLI binary '{config.cli}' not found.")
            print(f"  Install it:  uv tool install -e {config.home / 'harness'}")
            sys.exit(1)
        exec_args = [config.cli, "run"] + _rebuild_run_args(args, omit_spec=True)
        os.execvp(cli_bin, exec_args)

    # Apply session template (before CLI overrides, so flags always win)
    if getattr(args, "template", None):
        apply_template(config, args.template)

    # Apply CLI overrides
    if args.id:
        config.agent_id = args.id
    if args.model:
        config.model = args.model
    if args.parent:
        config.parent = args.parent
    if args.depth:
        config.depth = args.depth
    if args.persistent:
        config.persistent = True
    if args.continuation:
        config.continuation = True
    if args.resume:
        resume_id = args.resume
        prefix = config.session_prefix
        if prefix and not resume_id.startswith(prefix):
            resume_id = prefix + resume_id
        config.resume_session = resume_id
    if args.mode:
        config.initial_mode = args.mode
    if args.heartbeat is not None:
        config.heartbeat = True
        config.heartbeat_max = float(args.heartbeat) * 60
    if args.idle_nudge is not None:
        config.idle_nudge_timeout = float(args.idle_nudge) * 60
    if getattr(args, "effort", None):
        config.effort = args.effort
    # Parse --var KEY=VALUE pairs into config.template_vars
    for var_str in getattr(args, "var", []):
        if "=" not in var_str:
            print(f"Error: --var must be KEY=VALUE, got: {var_str}")
            sys.exit(1)
        key, _, value = var_str.partition("=")
        config.template_vars[key] = value

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

    _caffeinate_proc = _start_caffeinate()

    def _sighup_handler(signum, frame):
        """Clean up on SIGHUP (tmux kill-session). Default SIGHUP terminates
        without running finally blocks, so we handle cleanup explicitly."""
        _stop_caffeinate(_caffeinate_proc)
        if harness.session_config:
            harness.session_config.cleanup()
        sys.exit(1)

    signal.signal(signal.SIGHUP, _sighup_handler)

    # Import TUI here — it has heavy dependencies
    from .tui import KilnApp

    app = KilnApp(harness)
    try:
        app.run()
    finally:
        _stop_caffeinate(_caffeinate_proc)

    if harness.continue_requested:
        cont = getattr(harness, '_continuation_state', {})
        exec_args = [sys.executable, "-m", "kiln.cli",
                     "run", str(spec_path.resolve()),
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
        # Prefer config.template (set by apply_template, survives resume) over args
        template = harness.config.template or getattr(args, "template", None)
        if template:
            exec_args += ["--template", template]
        for var_str in getattr(args, "var", []):
            exec_args += ["--var", var_str]
        for key, val in harness.config.template_vars.items():
            if f"{key}=" not in " ".join(getattr(args, "var", [])):
                exec_args += ["--var", f"{key}={val}"]
        if config.idle_nudge_timeout > 0:
            exec_args += ["--idle-nudge", str(int(config.idle_nudge_timeout / 60))]
        if harness.handoff_text:
            import tempfile
            fd, path = tempfile.mkstemp(prefix="kiln-handoff-", suffix=".md")
            os.write(fd, harness.handoff_text.encode())
            os.close(fd)
            exec_args += ["--prompt-file", path]
        os.execvp(sys.executable, exec_args)

    if harness.restart_requested:
        os.execvp(sys.executable, [
            sys.executable, "-m", "kiln.cli",
            "run", str(spec_path.resolve()),
        ])


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


def _register_agent_home(name: str, target: Path) -> None:
    """Register an agent namespace → home path mapping in ~/.kiln/agents.yml."""
    agents_yml = Path.home() / ".kiln" / "agents.yml"
    agents_yml.parent.mkdir(parents=True, exist_ok=True)

    try:
        import yaml as _yaml
        registry = _yaml.safe_load(agents_yml.read_text()) or {} if agents_yml.exists() else {}
    except Exception:
        registry = {}

    registry[name] = str(target.expanduser())
    try:
        import yaml as _yaml
        agents_yml.write_text(_yaml.safe_dump(registry, sort_keys=True))
    except Exception as e:
        print(f"Warning: failed to update {agents_yml}: {e}")



def cmd_init(args: argparse.Namespace) -> None:
    """Scaffold a new agent home directory."""
    if args.dir:
        target = Path(args.dir)
    else:
        target = Path.home() / ".kiln" / "agents" / args.name

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

    _register_agent_home(args.name, target)

    print(f"Agent scaffolded at {target}/")

    if args.harness:
        print(f"  Install the harness:  uv tool install --editable {target}/harness")
        print(f"  Then launch with:     {args.name}")
    else:
        print(f"  Edit {doc_name} and agent.yml, then: kiln run {args.name}")


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

def cmd_daemon(args: argparse.Namespace) -> None:
    """Manage the Kiln daemon."""
    from .daemon.config import DAEMON_DIR, SOCKET_PATH, PID_FILE, LOG_FILE

    subcmd = args.daemon_command
    if not subcmd:
        print("Usage: kiln daemon {start|stop|status|logs}")
        sys.exit(1)

    if subcmd == "start":
        if SOCKET_PATH.exists():
            # Check if actually running
            if PID_FILE.exists():
                try:
                    pid = int(PID_FILE.read_text().strip())
                    os.kill(pid, 0)
                    print(f"Daemon already running (PID {pid})")
                    return
                except (ValueError, ProcessLookupError, PermissionError):
                    # Stale — clean up and start fresh
                    SOCKET_PATH.unlink(missing_ok=True)
                    PID_FILE.unlink(missing_ok=True)

        DAEMON_DIR.mkdir(parents=True, exist_ok=True)

        if args.foreground:
            import asyncio
            from .daemon.server import KilnDaemon, _setup_logging
            _setup_logging()
            asyncio.run(KilnDaemon().serve_forever())
        else:
            proc = subprocess.Popen(
                [sys.executable, "-m", "kiln.daemon.server", "--background"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            # Wait briefly for socket
            import time
            for _ in range(50):  # 5 seconds
                if SOCKET_PATH.exists():
                    break
                time.sleep(0.1)

            if SOCKET_PATH.exists():
                pid = PID_FILE.read_text().strip() if PID_FILE.exists() else "?"
                print(f"Daemon started (PID {pid})")
            else:
                print("Daemon failed to start — check logs at", LOG_FILE)
                sys.exit(1)

    elif subcmd == "stop":
        if not PID_FILE.exists():
            print("Daemon not running")
            return

        try:
            pid = int(PID_FILE.read_text().strip())
            os.kill(pid, signal.SIGTERM)
            print(f"Sent SIGTERM to daemon (PID {pid})")
        except (ValueError, ProcessLookupError):
            print("Daemon not running (stale PID file)")
            PID_FILE.unlink(missing_ok=True)
            SOCKET_PATH.unlink(missing_ok=True)

    elif subcmd == "status":
        if not SOCKET_PATH.exists():
            print("Daemon: not running")
            return

        if PID_FILE.exists():
            try:
                pid = int(PID_FILE.read_text().strip())
                os.kill(pid, 0)
                print(f"Daemon: running (PID {pid})")
                print(f"Socket: {SOCKET_PATH}")
                print(f"Log: {LOG_FILE}")

                # Query daemon for status
                import asyncio
                from .daemon.client import DaemonClient, DaemonUnavailableError
                async def _query():
                    client = DaemonClient(
                        agent="cli", session="cli-status",
                        auto_start=False,
                    )
                    try:
                        status = await client.get_status()
                        print(f"Sessions: {status.get('sessions', '?')}")
                        print(f"Channels: {status.get('channels', '?')}")
                        print(f"Bridges: {status.get('bridges', '?')}")
                        print(f"Adapters: {', '.join(status.get('adapters', [])) or 'none'}")
                        if status.get("lockdown"):
                            print("** LOCKDOWN ACTIVE **")
                    except DaemonUnavailableError:
                        print("(could not query daemon)")

                asyncio.run(_query())
                return
            except (ValueError, ProcessLookupError, PermissionError):
                pass

        print("Daemon: not running (stale socket)")

    elif subcmd == "logs":
        if not LOG_FILE.exists():
            print(f"No log file at {LOG_FILE}")
            return

        if args.follow:
            os.execvp("tail", ["tail", "-f", str(LOG_FILE)])
        else:
            os.execvp("tail", ["tail", "-50", str(LOG_FILE)])


def main():
    args = parse_args()

    if args.command == "run":
        cmd_run(args)
    elif args.command == "init":
        cmd_init(args)
    elif args.command == "list":
        cmd_list(args)
    elif args.command == "daemon":
        cmd_daemon(args)


if __name__ == "__main__":
    main()
