"""`store status`'s five member states, each driven deliberately and each with a control.

The design's section 5 table gives status one row per member: `upstream` (the pin is
reachable from upstream and is what upstream has), `stale` (upstream moved past the pin,
which is informational), `missing` (the pin is no longer reachable: a force-push),
`unpinned` (HEAD differs from the pin), and `cannot-measure` (the origin is unreachable).
Status never refuses on a working-state observation; an unreachable origin reads
`cannot-measure`, never `upstream`, and that one is exit 5.

WHY EACH ROW CARRIES A CONTROL: a single-member fixture cannot tell "the verb computed this
state" from "the verb prints one word for every member". Every row here reads BOTH members
out of ONE run and asserts they differ, so the instrument has to discriminate to pass.
The root's own `status`/`porcelain` block rides along and is asserted where it matters.

Written by Sentinel 2026-09-28 for builder step 3 (the verbs of section 5, each with --json
and the exit table).

Premium boundary: OSS (grip). Local workspace orchestration over git; no identity, org, or
entitlement semantics.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from gr2.python_cli.app import app

from tests.conftest import make_cli_runner


def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, text=True, capture_output=True, check=check)


def _git_out(cwd: Path, *args: str) -> str:
    return _git(cwd, *args).stdout.strip()


def _bare_remote(tmp_path: Path, name: str) -> Path:
    """A bare remote with one commit on `main`, returned as a PATH (cloneable as-is)."""
    src = tmp_path / f"{name}-src"
    src.mkdir(parents=True, exist_ok=True)
    _git(src, "init", "-q", "-b", "main")
    _git(src, "config", "user.email", "t@e.invalid")
    _git(src, "config", "user.name", "t")
    (src / "README.md").write_text(f"# {name}\n")
    _git(src, "add", ".")
    _git(src, "commit", "-q", "-m", "initial")
    remote = tmp_path / f"{name}.git"
    subprocess.run(["git", "clone", "--bare", str(src), str(remote)], capture_output=True, check=True)
    _git(remote, "symbolic-ref", "HEAD", "refs/heads/main")
    return remote


def _scratch_clone(tmp_path: Path, remote: Path, name: str) -> Path:
    """A second clone of `remote`, for pushing a commit the workspace members never see."""
    dest = tmp_path / f"scratch-{name}"
    subprocess.run(["git", "clone", "-q", str(remote), str(dest)], capture_output=True, check=True)
    _git(dest, "config", "user.email", "t@e.invalid")
    _git(dest, "config", "user.name", "t")
    return dest


def _cli(*args: str) -> tuple[int, str]:
    result = make_cli_runner().invoke(app, list(args))
    return result.exit_code, (result.stdout or "") + (result.stderr or "")


@pytest.fixture
def ws(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Two pushed members side by side, the root initialised and committed."""
    root = tmp_path / "ws"
    root.mkdir()
    for name in ("alpha", "beta"):
        remote = _bare_remote(tmp_path, name)
        dest = root / name
        subprocess.run(["git", "clone", "-q", str(remote), str(dest)], capture_output=True, check=True)
        _git(dest, "config", "user.email", "t@e.invalid")
        _git(dest, "config", "user.name", "t")
    monkeypatch.chdir(root)
    assert _cli("store", "init", str(root))[0] == 0
    assert _cli("store", "commit", "-m", "first")[0] == 0
    return root


def _rows(rc: int, out: str) -> dict[str, dict]:
    payload = json.loads(out)
    assert payload["status"] == "status", out
    return {row["name"]: row for row in payload["members"]}


def test_status_upstream_is_the_quiet_state(ws: Path) -> None:
    """Both members fully in step: `upstream` for each, exit 0, nothing on stderr."""
    rc, out = _cli("store", "status", "--json")
    assert rc == 0, out
    rows = _rows(rc, out)
    assert {name: row["state"] for name, row in rows.items()} == {"alpha": "upstream", "beta": "upstream"}, out
    # the row carries what it compared, so a reader can check the claim without a second verb
    for name, row in rows.items():
        assert row["pin"] == row["gitlink"] == _git_out(ws / name, "rev-parse", "HEAD"), (name, row)
        assert row["head"] == row["pin"], (name, row)


def test_status_stale_when_upstream_moves_past_the_pin(ws: Path) -> None:
    """The pin is still reachable -- upstream merely moved on. Informational, exit 0.

    The commit is made in a SCRATCH clone so the member's own HEAD stays on the pin; a
    commit in the member would make it `unpinned` and the row would prove the wrong state.
    """
    pin = _git_out(ws / "alpha", "rev-parse", "HEAD")
    scratch = _scratch_clone(ws.parent, ws.parent / "alpha.git", "alpha")
    (scratch / "moved.txt").write_text("upstream moved\n")
    _git(scratch, "add", ".")
    _git(scratch, "commit", "-q", "-m", "moved on")
    _git(scratch, "push", "-q", "origin", "main")

    rc, out = _cli("store", "status", "--json")
    assert rc == 0, out
    rows = _rows(rc, out)
    assert rows["alpha"]["state"] == "stale", out
    assert rows["alpha"]["head"] == pin, "the member's own HEAD must not have moved"
    assert rows["beta"]["state"] == "upstream", f"the control member must read differently: {out}"


def test_status_missing_when_upstream_force_pushes_over_the_pin(ws: Path) -> None:
    """The pin is unreachable: a force-push rewrote the member's history. Exit 0, named pin."""
    pin = _git_out(ws / "alpha", "rev-parse", "HEAD")
    orphan = ws.parent / "orphan"
    orphan.mkdir()
    _git(orphan, "init", "-q", "-b", "main")
    _git(orphan, "config", "user.email", "t@e.invalid")
    _git(orphan, "config", "user.name", "t")
    (orphan / "README.md").write_text("# rewritten\n")
    _git(orphan, "add", ".")
    _git(orphan, "commit", "-q", "-m", "rewrite")
    _git(orphan, "push", "-q", "--force", str(ws.parent / "alpha.git"), "main")
    # ASSERT THE MUTATION LANDED: a force-push that did not move the remote would leave the
    # pin covered and the row would read `upstream`, proving nothing.
    assert _git_out(orphan, "ls-remote", str(ws.parent / "alpha.git"), "refs/heads/main").split()[0] != pin

    rc, out = _cli("store", "status", "--json")
    assert rc == 0, out
    rows = _rows(rc, out)
    assert rows["alpha"]["state"] == "missing", out
    assert rows["beta"]["state"] == "upstream", f"the control member must read differently: {out}"
    assert rows["alpha"]["pin"] == pin, "the missing state must name the pin it could not find"


def test_status_unpinned_when_head_differs_from_the_pin(ws: Path) -> None:
    """A local commit the root has not recorded. The pin stays covered, so this is not missing."""
    pin = _git_out(ws / "alpha", "rev-parse", "HEAD")
    (ws / "alpha" / "local.txt").write_text("unrecorded\n")
    _git(ws / "alpha", "add", ".")
    _git(ws / "alpha", "commit", "-q", "-m", "local only")

    rc, out = _cli("store", "status", "--json")
    assert rc == 0, out
    rows = _rows(rc, out)
    assert rows["alpha"]["state"] == "unpinned", out
    assert rows["alpha"]["pin"] == pin, (rows["alpha"], pin)
    assert rows["alpha"]["head"] != pin, "unpinned means HEAD moved off the pin"
    assert rows["alpha"]["gitlink"] == pin, "the committed gitlink still matches the pin"
    assert rows["beta"]["state"] == "upstream", f"the control member must read differently: {out}"


def test_status_cannot_measure_when_a_member_is_absent(ws: Path) -> None:
    """An unreadable member reads `cannot-measure`, never `upstream`, and the run exits 5.

    The table still prints: the whole point of a status that never refuses is that one
    unreadable member does not hide the others.
    """
    shutil.rmtree(ws / "alpha")

    rc, out = _cli("store", "status", "--json")
    assert rc == 5, f"an unmeasurable member with no inconsistency must exit 5, got {rc}: {out}"
    rows = _rows(rc, out)
    assert rows["alpha"]["state"] == "cannot-measure", out
    assert rows["alpha"]["head"] is None, "an unmeasurable member has no HEAD to report"
    assert rows["beta"]["state"] == "upstream", f"the readable member must still be reported: {out}"
