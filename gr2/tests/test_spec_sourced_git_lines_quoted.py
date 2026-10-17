"""A `git` line gr2 tells the person to run, built from a value the spec, a record or the directory scan supplies
(not one typed in the same command), runs as printed: a path or pin holding shell text is quoted, so running the
line does not run that text."""

from __future__ import annotations

import re
import shlex
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from gr2.python_cli import clone_exec, gitops, grip_cli
from gr2.python_cli.app import app


def _run_printed(line: str, cwd: Path) -> None:
    subprocess.run(["bash", "-c", line], cwd=cwd, capture_output=True, text=True, timeout=60)


def test_workspace_init_no_origin_line_quotes_a_scanned_directory_holding_shell_text(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    member = root / "r;touch MARK #"
    member.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(member)], check=True)

    done = CliRunner().invoke(app, ["workspace", "init", str(root)])

    assert done.exit_code == 0, done.output
    lines = [line for line in done.output.splitlines() if "remote add origin <url>" in line]
    assert len(lines) == 1, done.output
    command = lines[0].split(": ", 1)[1].replace("<url>", "https://example.invalid/x.git")
    _run_printed(command, tmp_path)
    assert not (tmp_path / "MARK").exists(), command
    assert subprocess.run(["git", "-C", str(member), "remote", "get-url", "origin"], capture_output=True,
                          text=True).stdout.strip() == "https://example.invalid/x.git", command


def _unreachable(tmp_path: Path, name: str) -> Path:
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    repo = tmp_path / name
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", str(origin)], check=True)
    return repo


def _hand_line(exc: pytest.ExceptionInfo[SystemExit]) -> str:
    found = re.search(r"Try `(.*?)` by hand", str(exc.value))
    assert found, str(exc.value)
    return found.group(1)


def test_the_by_hand_fetch_line_quotes_a_clone_path_holding_shell_text(tmp_path: Path) -> None:
    repo = _unreachable(tmp_path, "s;touch MARK #")
    with pytest.raises(SystemExit) as exc:
        gitops.checkout_declared_pin(repo, "a" * 40, member="alpha")
    _run_printed(_hand_line(exc), tmp_path)
    assert not (tmp_path / "MARK").exists(), _hand_line(exc)


def test_the_by_hand_fetch_line_quotes_a_pin_holding_shell_text(tmp_path: Path) -> None:
    """The spec load refuses this pin shape (spec_apply._PIN_SHAPE); the line is quoted for any other caller."""
    repo = _unreachable(tmp_path, "plain")
    with pytest.raises(SystemExit) as exc:
        gitops.checkout_declared_pin(repo, "v1;touch${IFS}MARK", member="alpha")
    _run_printed(_hand_line(exc), tmp_path)
    assert not (tmp_path / "MARK").exists(), _hand_line(exc)


def _lane_on_main(tmp_path: Path, name: str) -> tuple[Path, str]:
    seed = tmp_path / "seed"
    subprocess.run(["git", "init", "-q", "-b", "main", str(seed)], check=True)
    subprocess.run(["git", "-C", str(seed), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q",
                    "--allow-empty", "-m", "c"], check=True)
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(seed), str(origin)], check=True)
    lane = tmp_path / name
    subprocess.run(["git", "clone", "-q", str(origin), str(lane)], check=True)
    return lane, str(origin)


def _move_back_line(exc: pytest.ExceptionInfo[clone_exec.CloneExecutionError]) -> str:
    found = re.search(r"move it back with `(.*?)`", str(exc.value))
    assert found, str(exc.value)
    return found.group(1)


@pytest.mark.parametrize(("lane_name", "branch"), [("l;touch MARK #", "feature"), ("plain", "x;touch${IFS}MARK")],
                         ids=["lane-path", "lane-branch"])
def test_the_move_back_line_for_a_lane_on_another_branch_quotes_shell_text(tmp_path: Path, lane_name: str,
                                                                          branch: str) -> None:
    lane, origin = _lane_on_main(tmp_path, lane_name)
    with pytest.raises(clone_exec.CloneExecutionError) as exc:
        clone_exec._reuse_existing_lane(lane, workspace_root=tmp_path, repo_url=origin, reference_base=None,
                                        expected_branch=branch)
    _run_printed(_move_back_line(exc), tmp_path)
    assert not (tmp_path / "MARK").exists(), _move_back_line(exc)


def test_the_switch_line_for_a_member_left_detached_quotes_a_declared_path_holding_shell_text(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """The verb's own steps are stubbed: the row reads only the line it prints for a member left detached."""
    member = tmp_path / "m;touch MARK #"
    subprocess.run(["git", "init", "-q", str(member)], check=True)
    monkeypatch.setattr(grip_cli, "_store_root", lambda root: tmp_path)
    monkeypatch.setattr(grip_cli, "_native_store_materialize", lambda root: [
        {"name": "alpha", "branch": None, "pin": "a" * 40, "detached": "no remote holds main", "path": str(member)}])
    monkeypatch.setattr(grip_cli.store_hooks, "install_store_hooks", lambda root: [])
    monkeypatch.setattr(grip_cli.store_hooks, "hook_problems", lambda root, installed: [])

    grip_cli.grip_materialize_cmd(root=tmp_path, json_output=False)

    found = re.search(r"to work on a branch: (.*)", capsys.readouterr().err)
    assert found
    command = found.group(1).replace("<branch>", "feat")
    _run_printed(command, tmp_path)
    assert not (tmp_path / "MARK").exists(), command
    assert subprocess.run(["git", "-C", str(member), "symbolic-ref", "--short", "HEAD"], capture_output=True,
                          text=True).stdout.strip() == "feat", command


def test_submodule_init_advice_quotes_a_path_from_the_store_holding_shell_text(tmp_path: Path, capsys) -> None:
    root = tmp_path / "store"
    subprocess.run(["git", "init", "-q", str(root)], check=True)

    grip_cli._register_submodules(root, ["s;touch MARK #"])

    lines = re.findall(r"need `(git submodule init [^`]*)` first", capsys.readouterr().err)
    assert len(lines) == 1, lines
    _run_printed(lines[0], root)
    assert not (root / "MARK").exists(), lines[0]


def test_hooks_bind_line_quotes_a_repo_name_from_the_spec_holding_shell_text(tmp_path: Path, capsys) -> None:
    from gr2.python_cli import hooks

    name = "r;touch MARK #"
    ctx = hooks.HookContext(tmp_path, tmp_path, tmp_path, tmp_path, name, "o", "s", "l")
    hooks._report_unbound(ctx, "r", "unbound", None, [], [])

    bind = [line for line in capsys.readouterr().out.splitlines() if line.startswith("bind: ")]
    assert len(bind) == 1, bind
    assert shlex.split(bind[0].removeprefix("bind: ")) == ["gr2", "hooks", "trust", name]


def test_workspace_init_advice_quotes_the_root_it_names() -> None:
    from gr2.python_cli.workspace_guidance import workspace_init_command

    root = "/w/x;touch MARK #"
    assert shlex.split(workspace_init_command(root).strip("`")) == ["gr2", "workspace", "init", root]
