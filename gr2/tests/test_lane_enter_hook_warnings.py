"""lane enter must not say "ok" when a bound hook failed.

A trusted lifecycle hook whose command runs and exits non-zero, under the warn
tier that is the on_enter DEFAULT, was absorbed silently: the warn JSON printed,
and then the success JSON printed "status": "ok" with exit 0 — the verb's
success did not depend on the entry work having happened. Ruled 2026-09-24
(ruled 2026-09-24): under the warn tier lane enter exits 0, and the string "ok"
appears nowhere — status reads "warned", hook_failures names the hook and its
returncode, and the same text goes to stderr. The blocking tier exits non-zero
unchanged, and a passing hook still says ok with an empty hook_failures.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from gr2.python_cli import app as gr2_app
from gr2.python_cli.hooks import load_repo_hooks
from tests.conftest import make_cli_runner

runner = make_cli_runner()


def _git(cwd: Path, *a: str) -> str:
    return subprocess.run(["git", *a], cwd=cwd, text=True, capture_output=True, check=True).stdout.strip()


def _workspace_with_enter_hook(
    tmp_path: Path, *, command: str, extra: str = "", marker: str = "", stage_key: str = "on_enter"
) -> tuple[Path, str, str]:
    """A one-repo workspace whose committed hooks table carries a lifecycle
    on_enter hook. Returns (ws, member sha, marker path)."""
    ws = tmp_path / "ws"
    (ws / ".grip").mkdir(parents=True)
    _git(ws / ".grip", "init", "-q", "-b", "main")
    _git(ws / ".grip", "config", "user.email", "g@e.invalid")
    _git(ws / ".grip", "config", "user.name", "g")
    _git(ws / ".grip", "commit", "-q", "--allow-empty", "-m", "init grip")

    origin = tmp_path / "app.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    src = ws / "repos" / "app"
    src.parent.mkdir(parents=True, exist_ok=True)
    _git(tmp_path, "clone", "-q", str(origin), str(src))
    _git(src, "config", "user.email", "t@e.invalid")
    _git(src, "config", "user.name", "t")
    (src / "f.txt").write_text("base\n")
    (src / ".gr2").mkdir()
    body = (
        f"[[lifecycle.{stage_key}]]\nname = \"boom\"\nwhen = \"always\"\n"
        f"command = \"{command}\"\n{extra}"
    )
    (src / ".gr2" / "hooks.toml").write_text(body)
    _git(src, "add", ".")
    _git(src, "commit", "-q", "-m " "hooks table committed")
    _git(src, "push", "-q", "origin", "main")
    from gr2.python_cli.consent import write_consent

    write_consent(ws, "repos/app", src)
    (ws / ".grip" / "workspace_spec.toml").write_text(
        f'schema_version = 1\nworkspace_name = "m"\n'
        f'[[repos]]\nname = "app"\npath = "repos/app"\nurl = "{origin}"\n'
        f'[[units]]\nname = "atlas"\npath = "agents/atlas"\nrepos = ["app"]\n'
    )
    return ws, _git(src, "rev-parse", "HEAD"), marker


def _workspace_with_exit_hook(tmp_path: Path) -> tuple[Path, str, str]:
    """Same shape with the failing hook on the on_exit stage."""
    return _workspace_with_enter_hook(
        tmp_path, command="exit 5", stage_key="on_exit"
    )


def test_lane_enter_with_a_failed_warn_hook_is_not_ok(tmp_path: Path) -> None:
    ws, _, _ = _workspace_with_enter_hook(
        tmp_path, command="exit 3", marker="/tmp/unused"
    )
    created = runner.invoke(
        gr2_app.app,
        ["lane", "create", str(ws), "atlas", "w", "--repos", "app", "--branch", "app=feat/w"],
    )
    assert created.exit_code == 0, created.output
    result = runner.invoke(
        gr2_app.app,
        ["lane", "enter", str(ws), "atlas", "w", "--actor", "agent:s"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    # the string "ok" appears nowhere in the JSON for this verb (as a value:
    # the field NAME hook_failures carries "ok" as a substring, which is not a
    # claim about the outcome)
    assert payload["status"] != "ok", result.output
    assert '"ok"' not in result.output
    # hook_failures names the hook and its returncode
    failures = payload["hook_failures"]
    assert len(failures) == 1, result.output
    assert failures[0]["hook"] == "boom"
    assert failures[0]["returncode"] == 3
    # the same text goes to stderr
    assert "boom" in result.stderr


def test_lane_enter_with_a_passing_hook_says_ok_with_empty_failures(
    tmp_path: Path,
) -> None:
    ws, _, _ = _workspace_with_enter_hook(
        tmp_path, command="exit 0"
    )
    created = runner.invoke(
        gr2_app.app,
        ["lane", "create", str(ws), "atlas", "p", "--repos", "app", "--branch", "app=feat/p"],
    )
    assert created.exit_code == 0, created.output
    result = runner.invoke(
        gr2_app.app,
        ["lane", "enter", str(ws), "atlas", "p", "--actor", "agent:s"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["status"] == "ok", result.output
    assert payload["hook_failures"] == [], result.output


def test_lane_enter_with_a_blocking_hook_exits_nonzero(tmp_path: Path) -> None:
    ws, _, _ = _workspace_with_enter_hook(
        tmp_path, command="exit 4", extra='on_failure = "block"\n'
    )
    created = runner.invoke(
        gr2_app.app,
        ["lane", "create", str(ws), "atlas", "b", "--repos", "app", "--branch", "app=feat/b"],
    )
    assert created.exit_code == 0, created.output
    result = runner.invoke(
        gr2_app.app,
        ["lane", "enter", str(ws), "atlas", "b", "--actor", "agent:s"],
    )
    assert result.exit_code != 0, result.output


def test_lane_exit_with_a_failed_warn_hook_is_not_ok(tmp_path: Path) -> None:
    ws, _, _ = _workspace_with_exit_hook(tmp_path)
    created = runner.invoke(
        gr2_app.app,
        ["lane", "create", str(ws), "atlas", "x", "--repos", "app", "--branch", "app=feat/x"],
    )
    assert created.exit_code == 0, created.output
    entered = runner.invoke(
        gr2_app.app,
        ["lane", "enter", str(ws), "atlas", "x", "--actor", "agent:s"],
    )
    assert entered.exit_code == 0, entered.output
    result = runner.invoke(
        gr2_app.app,
        ["lane", "exit", str(ws), "atlas", "--actor", "agent:s"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["status"] != "ok", result.output
    assert payload["hook_failures"][0]["returncode"] == 5, result.output


def test_a_skip_tier_hook_that_fails_is_also_warned(tmp_path: Path) -> None:
    """The skip tier records a failed hook in its results too, so the failure
    record covers it: the payload says "warned" and hook_failures names the
    hook and its rc. This pins existing behaviour; it is not a red-first
    witness."""
    ws, _, _ = _workspace_with_enter_hook(
        tmp_path, command="exit 6", extra='on_failure = "skip"\n'
    )
    created = runner.invoke(
        gr2_app.app,
        ["lane", "create", str(ws), "atlas", "s", "--repos", "app", "--branch", "app=feat/s"],
    )
    assert created.exit_code == 0, created.output
    result = runner.invoke(
        gr2_app.app,
        ["lane", "enter", str(ws), "atlas", "s", "--actor", "agent:s"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["status"] == "warned", result.output
    assert payload["hook_failures"][0]["hook"] == "boom", result.output
    assert payload["hook_failures"][0]["returncode"] == 6, result.output


def test_load_repo_hooks_refuses_a_top_level_stage_table(tmp_path: Path) -> None:
    """The sibling: a hooks table whose stage keys are top-level ([[on_enter]]
    instead of [[lifecycle.on_enter]]) must never vanish silently."""
    repo = tmp_path / "repo"
    (repo / ".gr2").mkdir(parents=True)
    p = repo / ".gr2" / "hooks.toml"
    p.write_text('[[on_enter]]\nname = "h"\ncommand = "true"\n')
    try:
        load_repo_hooks(repo)
        raise AssertionError("a top-level stage table must refuse")
    except SystemExit as exc:
        assert "lifecycle.on_enter" in str(exc)
    # the control: the correct shape parses
    (repo / ".gr2" / "hooks.toml").write_text(
        '[[lifecycle.on_enter]]\nname = "h"\ncommand = "true"\n'
    )
    hooks = load_repo_hooks(repo)
    assert hooks is not None and len(hooks.on_enter) == 1