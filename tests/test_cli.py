"""Tests for kiln CLI — dispatch, arg rebuilding, agent spec resolution."""

import argparse
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from kiln.cli import _build_inner_command, _find_agent_spec, _rebuild_run_args, cmd_run


# ---------------------------------------------------------------------------
# _rebuild_run_args
# ---------------------------------------------------------------------------

def _make_run_namespace(**overrides) -> argparse.Namespace:
    """Create a run-subcommand namespace with defaults."""
    defaults = dict(
        command="run", spec=None, id=None, model=None, parent=None,
        prompt=None, prompt_file=None, depth=0, persistent=False,
        last_session=False, resume=None, mode=None, detach=False,
        heartbeat=None, idle_nudge=None, continuation=False,
        effort=None, template=None, var=[], tag=[],
    )

    defaults.update(overrides)
    return argparse.Namespace(**defaults)


class TestRebuildRunArgs:
    def test_empty_args(self):
        args = _make_run_namespace()
        assert _rebuild_run_args(args) == []

    def test_spec_included_by_default(self):
        args = _make_run_namespace(spec="beth")
        assert _rebuild_run_args(args) == ["beth"]

    def test_spec_omitted(self):
        args = _make_run_namespace(spec="beth")
        assert _rebuild_run_args(args, omit_spec=True) == []

    def test_scalar_flags(self):
        args = _make_run_namespace(
            model="claude-opus-4-6", mode="yolo", effort="high",
            id="test-123", parent="parent-1", depth=2,
        )
        result = _rebuild_run_args(args, omit_spec=True)
        assert "--model" in result
        assert result[result.index("--model") + 1] == "claude-opus-4-6"
        assert "--mode" in result
        assert result[result.index("--mode") + 1] == "yolo"
        assert "--effort" in result
        assert result[result.index("--effort") + 1] == "high"
        assert "--id" in result
        assert result[result.index("--id") + 1] == "test-123"
        assert "--parent" in result
        assert "--depth" in result
        assert result[result.index("--depth") + 1] == "2"

    def test_boolean_flags(self):
        args = _make_run_namespace(persistent=True, detach=True, last_session=True)
        result = _rebuild_run_args(args)
        assert "--persistent" in result
        assert "--detach" in result
        assert "--last" in result

    def test_boolean_flags_when_false(self):
        args = _make_run_namespace(persistent=False, detach=False)
        result = _rebuild_run_args(args)
        assert "--persistent" not in result
        assert "--detach" not in result

    def test_template_and_vars(self):
        args = _make_run_namespace(template="conclave-facilitator", var=["ROLE=lead", "N=3"])
        result = _rebuild_run_args(args)
        assert "--template" in result
        assert result[result.index("--template") + 1] == "conclave-facilitator"

    def test_tags_forwarded(self):
        args = _make_run_namespace(tag=["canonical", "manager"])
        result = _rebuild_run_args(args)
        assert result.count("--tag") == 2
        assert result[result.index("--tag") + 1] == "canonical"
        assert result[result.index("--tag", result.index("--tag") + 1) + 1] == "manager"


    def test_heartbeat_and_idle_nudge(self):
        args = _make_run_namespace(heartbeat="10", idle_nudge="5")
        result = _rebuild_run_args(args)
        assert "--heartbeat" in result
        assert result[result.index("--heartbeat") + 1] == "10"
        assert "--idle-nudge" in result
        assert result[result.index("--idle-nudge") + 1] == "5"

    def test_prompt(self):
        args = _make_run_namespace(prompt="hello world")
        result = _rebuild_run_args(args)
        assert "--prompt" in result
        assert result[result.index("--prompt") + 1] == "hello world"

    def test_prompt_file(self):
        args = _make_run_namespace(prompt_file="/tmp/prompt.md")
        result = _rebuild_run_args(args)
        assert "--prompt-file" in result
        assert result[result.index("--prompt-file") + 1] == "/tmp/prompt.md"

    def test_resume(self):
        args = _make_run_namespace(resume="beth-old-session")
        result = _rebuild_run_args(args)
        assert "--resume" in result
        assert result[result.index("--resume") + 1] == "beth-old-session"

    def test_continuation(self):
        args = _make_run_namespace(continuation=True)
        result = _rebuild_run_args(args)
        assert "--continuation" in result

    def test_round_trip_all_flags(self):
        """Every flag set — verify nothing is silently dropped."""
        args = _make_run_namespace(
            spec="/path/to/agent", id="test-1", model="gpt-5.4",
            parent="parent-1", prompt="go", prompt_file=None,
            depth=3, persistent=True, last_session=True,
            resume="old-sess", mode="supervised", detach=True,
            heartbeat="15", idle_nudge="8", continuation=True,
            effort="medium", template="worker", var=["A=1"],
        )
        result = _rebuild_run_args(args)
        expected_flags = [
            "/path/to/agent", "--id", "--model", "--parent", "--prompt",
            "--depth", "--persistent", "--last", "--resume", "--mode",
            "--detach", "--heartbeat", "--idle-nudge", "--continuation",
            "--effort", "--template", "--var",
        ]
        for flag in expected_flags:
            assert flag in result, f"Missing: {flag}"


# ---------------------------------------------------------------------------
# _find_agent_spec
# ---------------------------------------------------------------------------

class TestFindAgentSpec:
    def test_explicit_path(self, tmp_path):
        spec = tmp_path / "agent.yml"
        spec.write_text("name: test\n")
        assert _find_agent_spec(str(spec)) == spec

    def test_directory_path(self, tmp_path):
        spec = tmp_path / "agent.yml"
        spec.write_text("name: test\n")
        assert _find_agent_spec(str(tmp_path)) == spec

    def test_kiln_agents_dir(self, tmp_path):
        agents_dir = tmp_path / ".kiln" / "agents" / "myagent"
        agents_dir.mkdir(parents=True)
        spec = agents_dir / "agent.yml"
        spec.write_text("name: myagent\n")
        with patch.object(Path, "home", return_value=tmp_path):
            assert _find_agent_spec("myagent") == spec

    def test_legacy_dot_dir(self, tmp_path):
        legacy_dir = tmp_path / ".myagent"
        legacy_dir.mkdir()
        spec = legacy_dir / "agent.yml"
        spec.write_text("name: myagent\n")
        with patch.object(Path, "home", return_value=tmp_path):
            assert _find_agent_spec("myagent") == spec

    def test_not_found_raises(self):
        with pytest.raises(FileNotFoundError):
            _find_agent_spec("nonexistent-agent-xyz")

    def test_cwd_default(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        spec = tmp_path / "agent.yml"
        spec.write_text("name: test\n")
        assert _find_agent_spec(None) == Path("agent.yml")


# ---------------------------------------------------------------------------
# cmd_run dispatch
# ---------------------------------------------------------------------------

class TestCmdRunDispatch:
    def _write_agent_spec(self, tmp_path, name="test", cli=None):
        spec = {"name": name}
        if cli:
            spec["cli"] = cli
        agent_yml = tmp_path / "agent.yml"
        agent_yml.write_text(yaml.dump(spec))
        return agent_yml

    def _dispatch_execvp(self, *a, **kw):
        """Side effect for mocked execvp — simulates process replacement."""
        raise SystemExit(0)

    def test_dispatch_execs_to_cli_binary(self, tmp_path):
        self._write_agent_spec(tmp_path, cli="myagent")
        args = _make_run_namespace(spec=str(tmp_path), mode="yolo", detach=True)

        with patch("kiln.cli.os.execvp", side_effect=self._dispatch_execvp) as mock_exec, \
             patch("kiln.cli.shutil.which", return_value="/usr/local/bin/myagent"), \
             pytest.raises(SystemExit):
            cmd_run(args)

        mock_exec.assert_called_once()
        call_args = mock_exec.call_args
        assert call_args[0][0] == "/usr/local/bin/myagent"
        exec_argv = call_args[0][1]
        assert exec_argv[0] == "myagent"
        assert exec_argv[1] == "run"
        assert "--mode" in exec_argv
        assert "--detach" in exec_argv
        # spec should be omitted — custom CLI defaults to its own home
        assert str(tmp_path) not in exec_argv

    def test_dispatch_skipped_when_harness_class_provided(self, tmp_path):
        """Custom CLIs pass harness_class — dispatch must not fire."""
        self._write_agent_spec(tmp_path, cli="myagent")
        args = _make_run_namespace(spec=str(tmp_path))

        # Patch _launch_in_tmux to avoid hitting real tmux infrastructure.
        # We only care that the dispatch execvp was NOT called.
        with patch("kiln.cli._launch_in_tmux"):
            cmd_run(args, harness_class=object)

    def test_dispatch_skipped_when_no_cli(self, tmp_path):
        """Stock agents (no cli field) run in-process."""
        self._write_agent_spec(tmp_path, cli=None)
        args = _make_run_namespace(spec=str(tmp_path))

        with patch("kiln.cli._launch_in_tmux"):
            cmd_run(args)

    def test_dispatch_cli_not_found_exits(self, tmp_path):
        self._write_agent_spec(tmp_path, cli="nonexistent-bin")
        args = _make_run_namespace(spec=str(tmp_path))

        with patch("kiln.cli.shutil.which", return_value=None), \
             pytest.raises(SystemExit):
            cmd_run(args)

    def test_dispatch_forwards_all_flags(self, tmp_path):
        self._write_agent_spec(tmp_path, cli="myagent")
        args = _make_run_namespace(
            spec=str(tmp_path),
            model="claude-opus-4-6", mode="yolo", effort="high",
            template="worker", var=["X=1"], tag=["canonical"],
        )


        with patch("kiln.cli.os.execvp", side_effect=self._dispatch_execvp) as mock_exec, \
             patch("kiln.cli.shutil.which", return_value="/bin/myagent"), \
             pytest.raises(SystemExit):
            cmd_run(args)

        exec_argv = mock_exec.call_args[0][1]
        assert "--model" in exec_argv
        assert "--mode" in exec_argv
        assert "--effort" in exec_argv
        assert "--template" in exec_argv
        assert "--var" in exec_argv
        assert "--tag" in exec_argv



# ---------------------------------------------------------------------------
# Inner command / re-entry
# ---------------------------------------------------------------------------

class TestBuildInnerCommand:
    """Verify the tmux inner command uses interpreter-relative re-entry."""

    def test_uses_sys_executable(self):
        args = _make_run_namespace()
        spec = Path("/tmp/agent.yml")
        cmd = _build_inner_command(args, "test-agent-1", spec)
        parts = cmd.split()
        assert parts[0] == sys.executable
        assert parts[1:4] == ["-m", "kiln.cli", "run"]

    def test_includes_spec_and_id(self):
        args = _make_run_namespace()
        spec = Path("/home/user/.myagent/agent.yml")
        cmd = _build_inner_command(args, "myagent-red-fox", spec)
        assert str(spec) in cmd
        assert "myagent-red-fox" in cmd

    def test_forwards_flags(self):
        args = _make_run_namespace(
            model="gpt-5.4", mode="yolo", effort="high",
            heartbeat="10", template="worker", var=["X=1"], tag=["canonical"],
        )

        cmd = _build_inner_command(args, "test-1", Path("/tmp/agent.yml"))
        assert "--model gpt-5.4" in cmd
        assert "--mode yolo" in cmd
        assert "--effort high" in cmd
        assert "--heartbeat 10" in cmd
        assert "--template worker" in cmd
        assert "--var X=1" in cmd
        assert "--tag canonical" in cmd


    def test_no_path_dependency(self):
        """Inner command must not depend on PATH resolution."""
        args = _make_run_namespace()
        cmd = _build_inner_command(args, "test-1", Path("/tmp/agent.yml"))
        parts = cmd.split()
        # First element is an absolute path (sys.executable), not a bare name
        assert os.path.isabs(parts[0])
