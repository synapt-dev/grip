"""Advice to init a workspace is never given for a directory already inside one.

Each verb that refuses for a missing spec or `.grip/` used to spell `gr2 workspace init <path>` in its own text, so a
verb pointed at a member clone (or any directory inside a workspace) told the operator to init a second workspace
nested inside the first. The texts now go through `workspace_guidance` (`workspace_init_command` for the command,
`init_unless_inside_workspace` for whether init suits the path): one row per call site, and a structural row that
fails if a hand-written init text appears anywhere else. CLI rows run the real CLI in a subprocess.
"""
from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

import pytest
import typer

GR2_ROOT = Path(__file__).parents[1]
PACKAGE = GR2_ROOT / "gr2" / "python_cli"

_CLI = """
import os, runpy, sys
sys.argv = [os.path.join(os.path.dirname(sys.executable), "gr2")] + sys.argv[1:]
runpy.run_module("gr2.python_cli.app", run_name="__main__")
"""

SPEC_TOML = 'workspace_name = "ws"\n\n[[repos]]\nname = "m1"\npath = "m1"\nurl = ""\n'


def _gr2(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "PYTHONPATH": str(GR2_ROOT), "COLUMNS": "2000"}  # one line per sentence in a Rich error box
    return subprocess.run([sys.executable, "-P", "-c", _CLI, *args], cwd=cwd, text=True, capture_output=True, env=env)


def _member_of_spec_workspace(tmp_path: Path) -> tuple[Path, Path]:
    ws = tmp_path / "ws"
    (ws / ".grip").mkdir(parents=True)
    (ws / ".grip" / "workspace_spec.toml").write_text(SPEC_TOML)
    member = ws / "m1"
    member.mkdir()
    return ws.resolve(), member.resolve()


def _sends_to_root(text: str, member: Path, ws: Path) -> None:
    assert "workspace init" not in text, text
    assert f"{member} is inside the workspace at {ws}" in text, text


def test_sync_status_on_a_member_points_at_the_workspace_root(tmp_path):
    """syncops.build_sync_plan."""
    ws, member = _member_of_spec_workspace(tmp_path)
    result = _gr2(tmp_path, "sync", "status", str(member))
    assert result.returncode != 0
    _sends_to_root(result.stdout + result.stderr, member, ws)


def test_spec_show_on_a_member_points_at_the_workspace_root(tmp_path):
    """spec_apply.show_spec."""
    ws, member = _member_of_spec_workspace(tmp_path)
    result = _gr2(tmp_path, "spec", "show", str(member))
    assert result.returncode != 0
    _sends_to_root(result.stdout + result.stderr, member, ws)


def test_workspace_status_on_a_member_points_at_the_workspace_root(tmp_path):
    """migration.render_status, `No workspace detected`."""
    ws, member = _member_of_spec_workspace(tmp_path)
    result = _gr2(tmp_path, "workspace", "status", str(member))
    assert "No workspace detected" in result.stdout, (result.stdout, result.stderr)
    _sends_to_root(result.stdout, member, ws)


def test_lane_create_on_a_member_points_at_the_workspace_root(tmp_path):
    """app._spec_for_lane_create, no spec and no members."""
    ws, member = _member_of_spec_workspace(tmp_path)
    result = _gr2(tmp_path, "lane", "create", "feat", "u1", "--root", str(member))
    assert result.returncode == 2, (result.returncode, result.stdout, result.stderr)
    assert "Nothing was created." in result.stderr
    _sends_to_root(result.stderr, member, ws)


def test_lane_create_on_a_nested_root_of_its_own_keeps_its_own_advice(tmp_path):
    """A root that is itself a workspace (a grip.toml, no members) inside another is told to init itself, not sent out."""
    ws, member = _member_of_spec_workspace(tmp_path)
    (member / "grip.toml").write_text('schema_version = 1\nworkspace_name = "inner"\n')
    result = _gr2(tmp_path, "lane", "create", "feat", "u1", "--root", str(member))
    assert result.returncode == 2, (result.returncode, result.stdout, result.stderr)
    assert f"`gr2 workspace init {member}`" in result.stderr and "inside the workspace" not in result.stderr


def test_lane_create_with_unreadable_members_names_init_for_that_root(tmp_path):
    """app._spec_for_lane_create, grip.toml members unreadable: the command is built by workspace_init_command."""
    root = (tmp_path / "native").resolve()
    root.mkdir()
    (root / "grip.toml").write_text('schema_version = 1\n[[members]\n')  # not TOML
    result = _gr2(tmp_path, "lane", "create", "feat", "u1", "--root", str(root))
    assert result.returncode != 0
    assert f"write the spec with `gr2 workspace init {root}`" in " ".join((result.stdout + result.stderr).replace("│", " ").split())


def test_validate_grip_dir_on_a_member_points_at_the_workspace_root(tmp_path, capsys):
    """grip_cli._validate_grip_dir (no caller in the package today; called directly)."""
    from gr2.python_cli import grip_cli

    ws, member = _member_of_spec_workspace(tmp_path)
    with pytest.raises(typer.Exit):
        grip_cli._validate_grip_dir(member)
    _sends_to_root(capsys.readouterr().out, member, ws)


def test_validate_grip_dir_on_a_lone_directory_still_says_init(tmp_path, capsys):
    from gr2.python_cli import grip_cli

    with pytest.raises(typer.Exit):
        grip_cli._validate_grip_dir(tmp_path)
    assert "Run `gr2 workspace init .` first." in capsys.readouterr().out


# init's own refusal for --from-superproject: the operator is already running init, and it names the plain form.
ALLOWED = {("app.py", "_require_superproject")}


def _docstring_ids(tree: ast.AST) -> set[int]:
    ids = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                ids.add(id(first.value))
    return ids


def _init_texts(path: Path) -> list[tuple[str, int, str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings = _docstring_ids(tree)
    found = []

    def visit(node: ast.AST, func: str) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            func = node.name
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
            if "workspace init" in node.value:
                found.append((func, node.lineno, node.value))
        for child in ast.iter_child_nodes(node):
            visit(child, func)

    visit(tree, "<module>")
    return found


def test_no_hand_written_init_text_outside_the_shared_helper():
    offenders = []
    for path in sorted(PACKAGE.rglob("*.py")):
        if path.name == "workspace_guidance.py":
            continue
        for func, line, text in _init_texts(path):
            if (path.name, func) not in ALLOWED:
                offenders.append(f"{path.relative_to(PACKAGE)}:{line} in {func}: {text!r}")
    assert not offenders, "route these through workspace_guidance.workspace_init_command:\n" + "\n".join(offenders)


def test_the_structural_scan_sees_an_init_text(tmp_path):
    """The scan is not vacuous: it finds the allowed site, and a planted hand-written text in a scratch module (in an
    f-string part too), while a docstring that mentions init is not a text a user sees."""
    assert any(f == "_require_superproject" for f, _, _ in _init_texts(PACKAGE / "app.py"))
    scratch = tmp_path / "scratch.py"
    scratch.write_text('def f(root):\n    """Mentions workspace init."""\n    return f"run `gr2 workspace init {root}` first"\n')
    assert [(f, line) for f, line, _ in _init_texts(scratch)] == [("f", 3)]


def test_alpha_store_refusal_on_a_member_points_at_the_workspace_root(tmp_path):
    """grip._validate_grip_repo: its `gr2 store init .` advice goes through the same helper."""
    from gr2.python_cli import grip as grip_mod

    ws, member = _member_of_spec_workspace(tmp_path)
    with pytest.raises(grip_mod.GripInitError) as refused:
        grip_mod.grip_log(member)
    assert "store init" not in str(refused.value), refused.value
    _sends_to_root(str(refused.value), member, ws)


def test_control_alpha_store_refusal_outside_any_workspace_keeps_store_init(tmp_path):
    from gr2.python_cli import grip as grip_mod

    lone = tmp_path / "lone"
    lone.mkdir()
    with pytest.raises(grip_mod.GripInitError) as refused:
        grip_mod.grip_log(lone)
    assert "Run `gr2 store init .` first." in str(refused.value), refused.value
