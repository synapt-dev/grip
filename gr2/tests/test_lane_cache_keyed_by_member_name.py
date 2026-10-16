"""`lane create` reads the member cache the way materialize and sync write it: by the
member's spec NAME, never by its directory basename.

Materialize and sync key `.grip/cache/repos/<name>.git` by the spec name
(`spec_apply.repo_cache_path`); lane create and sync's lane materialize keyed it by
`source_repo_root.name`. The two agree only while a member's name equals its
directory. A real root breaks that in 9 of 30 rows (synapt-config -> config,
memU -> reference/memu, five `agents/*/home` rows that share one basename), so a
lane either seeded a second cache beside the materialized one, or two members with
the same basename met in one `home.git`.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from typer.testing import CliRunner

from gr2.python_cli import app as gr2_app
from tests.native_root_helper import native_root
from tests.test_lane_create_fork_base_cli import _commit_native_workspace

runner = CliRunner()


def _git(cwd: Path, *a: str) -> str:
    return subprocess.run(["git", *a], cwd=cwd, text=True, capture_output=True, check=True).stdout.strip()


def _source(tmp_path: Path, ws: Path, name: str, path: str) -> str:
    """A checkout at ws/<path> whose origin is a local bare repo named after the member."""
    origin = tmp_path / "origins" / f"{name}.git"
    origin.parent.mkdir(parents=True, exist_ok=True)
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    src = ws / path
    src.parent.mkdir(parents=True, exist_ok=True)
    _git(tmp_path, "clone", "-q", str(origin), str(src))
    _git(src, "config", "user.email", "t@e.invalid")
    _git(src, "config", "user.name", "t")
    (src / "f.txt").write_text(f"{name}\n")
    _git(src, "add", ".")
    _git(src, "commit", "-q", "-m", f"base of {name}")
    _git(src, "push", "-q", "origin", "main")
    return str(origin)


def _workspace(tmp_path: Path, members: list[tuple[str, str]]) -> Path:
    ws = tmp_path / "ws"
    (ws / ".grip").mkdir(parents=True)
    native_root(ws)
    blocks = ""
    for name, path in members:
        url = _source(tmp_path, ws, name, path)
        blocks += f'\n[[repos]]\nname = "{name}"\npath = "{path}"\nurl = "{url}"\n'
    names = ", ".join(f'"{n}"' for n, _ in members)
    (ws / ".grip" / "workspace_spec.toml").write_text(
        f'schema_version = 1\nworkspace_name = "m"\n{blocks}\n'
        f'[[units]]\nname = "atlas"\npath = "agents/atlas"\nrepos = [{names}]\n'
    )
    _commit_native_workspace(ws)
    # A spec-backed root, the shape migrate-gr1 writes: a native root keys its per-lane cache by
    # member name already, so the basename sites are reached only without the native marker.
    (ws / 'grip.toml').unlink()
    return ws


def _create(root: str, repos: list[str], lane: str = "feature"):
    return runner.invoke(gr2_app.app, ["lane", "create", root, "atlas", lane,
                                       "--repos", ",".join(repos), "--branch", f"work/{lane}"])


def _alternate(ws: Path, lane: str, name: str) -> str:
    repo = ws / "agents" / "atlas" / "lanes" / lane / "repos" / name
    return (repo / ".git" / "objects" / "info" / "alternates").read_text().strip()


def _caches(ws: Path) -> list[str]:
    return sorted(p.name for p in (ws / ".grip" / "cache" / "repos").iterdir())


def test_member_named_unlike_its_directory_uses_the_name_cache(tmp_path: Path) -> None:
    ws = _workspace(tmp_path, [("synapt-config", "config")])
    res = _create(str(ws), ["synapt-config"])
    assert res.exit_code == 0, res.output
    assert _alternate(ws, "feature", "synapt-config") == str(ws / ".grip/cache/repos/synapt-config.git/objects")
    assert _caches(ws) == ["synapt-config.git"]


def test_equal_named_member_is_unchanged(tmp_path: Path) -> None:
    # Control: name == basename, so both keys agree before and after the fix.
    ws = _workspace(tmp_path, [("gitgrip", "gitgrip")])
    res = _create(str(ws), ["gitgrip"])
    assert res.exit_code == 0, res.output
    assert _alternate(ws, "feature", "gitgrip") == str(ws / ".grip/cache/repos/gitgrip.git/objects")
    assert _caches(ws) == ["gitgrip.git"]


def test_nested_member_path_uses_the_name_cache(tmp_path: Path) -> None:
    ws = _workspace(tmp_path, [("memU", "reference/memu")])
    res = _create(str(ws), ["memU"])
    assert res.exit_code == 0, res.output
    assert _alternate(ws, "feature", "memU") == str(ws / ".grip/cache/repos/memU.git/objects")
    assert _caches(ws) == ["memU.git"]


def test_two_members_sharing_a_basename_get_two_caches(tmp_path: Path) -> None:
    # agents/a/home and agents/b/home: different repos, one basename.
    ws = _workspace(tmp_path, [("a-home", "homes/a/home"), ("b-home", "homes/b/home")])
    res = _create(str(ws), ["a-home", "b-home"])
    assert res.exit_code == 0, res.output
    for name in ("a-home", "b-home"):
        assert _alternate(ws, "feature", name) == str(ws / f".grip/cache/repos/{name}.git/objects")
        lane_repo = ws / "agents/atlas/lanes/feature/repos" / name
        assert (lane_repo / "f.txt").read_text() == f"{name}\n"
    assert _caches(ws) == ["a-home.git", "b-home.git"]


def test_lane_create_from_a_subdirectory_uses_the_name_cache(tmp_path: Path, monkeypatch) -> None:
    # Both sites resolve paths, so drive the verb from below the root as well.
    ws = _workspace(tmp_path, [("synapt-config", "config")])
    sub = ws / "config"
    monkeypatch.chdir(sub)
    res = _create("..", ["synapt-config"])
    assert res.exit_code == 0, res.output
    assert _alternate(ws, "feature", "synapt-config") == str(ws.resolve() / ".grip/cache/repos/synapt-config.git/objects")
    assert _caches(ws) == ["synapt-config.git"]


def test_sync_rematerializes_a_lane_repo_from_the_name_cache(tmp_path: Path) -> None:
    # sync's materialize_lane_repo is the second caller that keyed the cache by basename.
    import shutil

    from gr2.python_cli import syncops

    ws = _workspace(tmp_path, [("a-home", "homes/a/home"), ("b-home", "homes/b/home")])
    assert _create(str(ws), ["a-home", "b-home"]).exit_code == 0
    for name in ("a-home", "b-home"):
        shutil.rmtree(ws / "agents/atlas/lanes/feature/repos" / name)
    result = syncops.run_sync(ws)
    assert result.status != "blocked", result
    for name in ("a-home", "b-home"):
        assert _alternate(ws, "feature", name) == str(ws / f".grip/cache/repos/{name}.git/objects")
        assert (ws / "agents/atlas/lanes/feature/repos" / name / "f.txt").read_text() == f"{name}\n"
    assert _caches(ws) == ["a-home.git", "b-home.git"]
