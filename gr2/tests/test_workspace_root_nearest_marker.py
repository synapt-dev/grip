"""The implied workspace root is the NEAREST workspace, of either kind, not the nearest spec file.

THE DEFECT: `_resolve_workspace_root` walked up from the current directory for the nearest
`.grip/workspace_spec.toml` and asked for nothing else. A native store root (what `store init`
makes: a `grip.toml` beside a root `.git`) has no such file, so any OUTER initialised workspace
above it won. `review bind` with no positional, run from inside the store root, then bound in the
OUTER workspace: exit 0, a `gr:` id, and every read of that id against the root the caller was
standing in said `no_rows` or `not a gr2 review bind commit`. A confident success on a bind that
went somewhere else.

THE CONTRACT these rows pin:
  * the root is the nearest ancestor (the current directory included) that is a workspace of
    EITHER kind: `.grip/workspace_spec.toml`, or a native store root;
  * so a store root nested under an initialised workspace is its own root, from the root and from
    any subdirectory of it, while a directory under the outer workspace only still finds the outer;
  * `workspace status` asks the same resolver instead of keeping its own copy of the walk;
  * `review bind` with no positional binds in the root the caller is standing in.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from gr2.python_cli import app as app_mod

from tests.test_review_bind_native_store import _unpushed_head
from tests.test_store_break_attempts import _cli, _git, _git_out, two_member_ws  # noqa: F401


def _outer_workspace(path: Path) -> Path:
    """An INITIALISED outer workspace: `.grip` is a git repo holding a committed workspace_spec.toml."""
    grip = path / ".grip"
    grip.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(grip)], check=True)
    (grip / "workspace_spec.toml").write_text('schema_version = 1\nworkspace_name = "outer"\n')
    _git(grip, "add", "-A")
    _git(grip, "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "-m", "outer")
    return path


@pytest.fixture
def nested(two_member_ws: Path) -> tuple[Path, Path]:
    """(outer, inner): inner is a native store root, outer (its parent) an initialised workspace."""
    inner = two_member_ws
    assert _cli("store", "init", str(inner))[0] == 0
    outer = _outer_workspace(inner.parent)
    assert (inner / "grip.toml").is_file() and not (inner / ".grip" / "workspace_spec.toml").exists()
    return outer, inner


def test_a_store_root_under_an_initialised_workspace_is_its_own_root(nested, monkeypatch) -> None:
    """The discriminating case: cwd IS the store root. Before, the outer workspace was returned."""
    outer, inner = nested
    monkeypatch.chdir(inner)
    assert app_mod._resolve_workspace_root() == inner.resolve()


def test_a_subdirectory_of_the_store_root_finds_the_store_root(nested, monkeypatch) -> None:
    outer, inner = nested
    monkeypatch.chdir(inner / "alpha")
    assert app_mod._resolve_workspace_root() == inner.resolve()


def test_a_directory_under_the_outer_workspace_only_still_finds_the_outer(nested, monkeypatch) -> None:
    """The spec-file rule is unchanged where no store root is nearer."""
    outer, inner = nested
    other = outer / "elsewhere" / "deep"
    other.mkdir(parents=True)
    monkeypatch.chdir(other)
    assert app_mod._resolve_workspace_root() == outer.resolve()


def test_an_explicit_argument_still_wins(nested, monkeypatch) -> None:
    outer, inner = nested
    monkeypatch.chdir(inner)
    assert app_mod._resolve_workspace_root(outer) == outer.resolve()


def test_a_git_repo_with_a_grip_toml_but_no_root_git_is_not_a_store_root(tmp_path, monkeypatch) -> None:
    """A store root is `grip.toml` BESIDE a root `.git`: a stray grip.toml in a plain directory
    must not capture the walk. Goes red if the marker is `grip.toml` alone."""
    outer = _outer_workspace(tmp_path / "outer")
    stray = outer / "stray"
    stray.mkdir()
    (stray / "grip.toml").write_text("schema_version = 1\n")
    monkeypatch.chdir(stray)
    assert app_mod._resolve_workspace_root() == outer.resolve()


def test_review_bind_with_no_positional_binds_in_the_root_the_caller_stands_in(nested, monkeypatch) -> None:
    """The reproduction: the handle must verify against the INNER root, and the outer store must
    not have received it. Before, bind exited 0 with an id that the inner root called corrupt."""
    outer, inner = nested
    monkeypatch.chdir(inner)
    remote, base, head = _unpushed_head(inner)
    outer_head_before = _git_out(outer / ".grip", "rev-parse", "HEAD")

    code, out = _cli(
        "review", "bind", "--repo", "alpha", "--remote", remote, "--base", base, "--head", head,
        "--ref", "refs/heads/main", "--source", str(inner / "alpha"),
    )
    assert code == 0, out
    gr_id = out.strip().splitlines()[-1]
    assert gr_id.startswith("gr:") and len(gr_id) == 3 + 40, out

    code, out = _cli("review", "verify", str(inner), gr_id)
    assert code == 0 and "tree_matches: True" in out, out
    sha = gr_id[3:]
    in_outer = subprocess.run(
        ["git", "-C", str(outer / ".grip"), "cat-file", "-e", f"{sha}^{{commit}}"], capture_output=True
    )
    assert in_outer.returncode != 0, "the bind commit landed in the OUTER workspace's store"
    assert _git_out(outer / ".grip", "rev-parse", "HEAD") == outer_head_before


def test_workspace_status_uses_the_shared_resolver(nested, monkeypatch) -> None:
    """`workspace status` kept its own copy of the ancestor walk. It must hand `repo status` the root
    the resolver names, so the two cannot drift apart again."""
    outer, inner = nested
    monkeypatch.chdir(inner)
    seen: list[Path] = []
    monkeypatch.setattr(app_mod, "repo_status", lambda root, **kw: seen.append(root))
    app_mod.status_cmd()
    assert seen == [inner.resolve()]
