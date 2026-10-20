"""`lane create` refuses a shallow member by name, with exit 2 and no traceback.

A lane clone needs the member's whole history. Before this refusal a shallow member
reached the clone step and failed there, printing a raw traceback and internal error
text instead of a message a user can act on.
"""
from __future__ import annotations

import re
import shlex
import subprocess
from pathlib import Path

from typer.testing import CliRunner

from gr2.prototypes import lane_workspace_prototype as lane_proto
from gr2.python_cli import gitops
from gr2.python_cli.app import app

runner = CliRunner()


def _git(cwd: Path, *a: str) -> str:
    return subprocess.run(
        ["git", *a], cwd=cwd, text=True, capture_output=True, check=True
    ).stdout.strip()


def _workspace(tmp_path: Path, members: dict[str, bool], members_dir: str = "repos") -> Path:
    """A workspace whose members are clones of their own origin; True marks a shallow one."""
    ws = tmp_path / "ws"
    (ws / members_dir).mkdir(parents=True)
    spec = 'schema_version = 1\nworkspace_name = "m"\n\n'
    for name, shallow in members.items():
        origin = tmp_path / f"{name}.git"
        _git(tmp_path, "init", "-q", "--bare", "-b", "trunk", str(origin))
        seed = tmp_path / f"seed-{name}"
        _git(tmp_path, "clone", "-q", str(origin), str(seed))
        for msg in ("one", "two"):
            _git(seed, "-c", "user.name=t", "-c", "user.email=t@e.invalid", "commit", "-q", "--allow-empty", "-m", msg)
        _git(seed, "push", "-q", "origin", "HEAD:refs/heads/trunk")
        member = ws / members_dir / name
        depth = ["--depth", "1"] if shallow else []
        _git(tmp_path, "clone", "-q", *depth, origin.as_uri(), str(member))
        assert _git(member, "rev-parse", "--is-shallow-repository") == ("true" if shallow else "false")
        spec += f'[[repos]]\nname = "{name}"\npath = "{members_dir}/{name}"\nurl = "{origin}"\n\n'
    names = ", ".join(f'"{n}"' for n in members)
    spec += f'[[units]]\nname = "u1"\npath = "agents/u1"\nrepos = [{names}]\n'
    (ws / ".grip").mkdir()
    (ws / ".grip" / "workspace_spec.toml").write_text(spec)
    return ws


def _shallow_workspace(tmp_path: Path) -> Path:
    return _workspace(tmp_path, {"repo-a": True})


def test_lane_create_refuses_a_shallow_member_by_name(tmp_path: Path) -> None:
    ws = _shallow_workspace(tmp_path)
    result = runner.invoke(app, ["lane", "create", str(ws), "u1", "feat-x", "--repos", "repo-a"])
    out = result.output
    assert result.exit_code == 2, out
    assert "refused: shallow_member: repo-a " in out, out
    assert "fetch --unshallow" in out, out
    assert "Traceback" not in out, out
    assert re.search(r"section [0-9]", out) is None, out
    for internal in ("CloneExecutionError", "reference_base", "plan operation"):
        assert internal not in out, (internal, out)


def test_lane_create_refuses_a_shallow_second_member_before_any_lane_clone(tmp_path: Path, monkeypatch) -> None:
    ws = _workspace(tmp_path, {"repo-a": False, "repo-b": True}, members_dir="my repos")
    # The failed create's rollback removes a first-member clone afterwards, so the directory alone
    # cannot tell; the witness is that no member reached the lane clone step at all.
    cloned: list[Path] = []
    real_cache = gitops.ensure_repo_cache

    def recording_cache(url, cache_root, **kw):
        cloned.append(Path(cache_root))
        return real_cache(url, cache_root, **kw)

    monkeypatch.setattr(gitops, "ensure_repo_cache", recording_cache)
    result = runner.invoke(app, ["lane", "create", str(ws), "u1", "feat-x", "--repos", "repo-a,repo-b"])
    out = result.output
    assert result.exit_code == 2, out
    assert "refused: shallow_member: repo-b " in out, out
    assert "Traceback" not in out, out
    assert cloned == [], (cloned, out)
    # The refusal rolls the lane record back, so the path is built, not read from it.
    first = lane_proto.lane_dir(ws, "u1", "feat-x") / "repos" / "repo-a"
    assert not first.exists(), (first, out)
    leftovers = [p for p in ws.rglob("repo-a") if p != ws / "my repos" / "repo-a"]
    assert not leftovers, (leftovers, out)
    shown = shlex.quote(str((ws / "my repos" / "repo-b").resolve()))
    assert shown.startswith("'"), shown
    assert f"git -C {shown} fetch --unshallow" in out, out
