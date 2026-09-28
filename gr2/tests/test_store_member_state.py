"""The member-state coverage, ported to the native store verbs (2026-09-28).

WHAT THIS FILE USED TO WITNESS. Measured 2026-09-24 on the adopted-superproject flow: after a
plain clone, adoption and materialization, `store snapshot` recorded the ROOT's HEAD as every
member's head (rc 0), and with the root carrying its own untracked `.grip/` and `agents/` it
refused with "Dirty repos detected" for members that were CLEAN. Both answers came from git
resolving the EMPTY placeholder at the declared path through to the enclosing root. The dirty
check and the head record were the two checks; the same map fed `store checkout`.

WHY IT IS PORTED. Design section 5 makes `store snapshot` a hidden alias of `store commit`,
and reimplements `checkout` as materialize-at-a-commit over the root repo, so the alpha
spellings these rows drove (`store snapshot <workspace_root>`) are gone. Apollo's condition on
the port: the rows must still drive the superproject shape and still assert that each member
lands at the commit the root declares. Only the verb spellings change.

⚠ ONE ROW'S SHAPE HAD TO CHANGE, AND IT IS NAMED RATHER THAN QUIETLY DROPPED. The alpha model
keeps the real checkout at the unit copy (`agents/default/home/<member>`) while the DECLARED
path at the root is an empty placeholder that git resolves through to the root -- which is
what produced the defect this file exists for. Native section 3's layout has no such
indirection: `<root>/<member.path>` IS the member checkout. So the fixture adopts the
superproject exactly as before, then puts a real clone at each declared path, and the rows
assert the same things about that shape. The fifth row (a member path that is missing) cannot
keep its old assertion -- the alpha verb "recorded it empty", and a native commit has no
"empty" to record -- so it now asserts what the contract requires instead: the verb REFUSES,
naming the member, rather than silently recording nothing.

Premium boundary: OSS (grip). Local workspace orchestration over git; no identity, org, or
entitlement semantics.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest

from gr2.python_cli.app import app

from tests.conftest import make_cli_runner
from tests.test_repo_path_read_through import _adopted_superproject


def _cli(*args: str) -> tuple[int, str]:
    result = make_cli_runner().invoke(app, list(args))
    return result.exit_code, result.stdout + (result.stderr or "")


def _run_head(repo: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()


def _superproject(
    tmp_path: Path, *, root_clean: bool, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, dict[str, str]]:
    """An adopted superproject with a REAL checkout at each declared member path.

    The adoption flow is the original one (`workspace init --from-superproject`, then
    `workspace materialize`) so this still exercises the placeholders-and-units shape; the
    clones at the declared paths are what section 3's layout calls the member checkout.
    """
    plain, pins = _adopted_superproject(tmp_path)
    rc, out = _cli("workspace", "init", str(plain), "--from-superproject")
    assert rc == 0, out
    rc, out = _cli("workspace", "materialize", str(plain), "--yes")
    assert rc == 0, out

    spec = tomllib.loads((plain / ".grip" / "workspace_spec.toml").read_text())
    for repo in spec["repos"]:
        dest = plain / repo["path"]
        assert dest.is_dir() and not any(dest.iterdir()), (
            f"precondition: {repo['path']} is the empty placeholder after a plain clone"
        )
        shutil.rmtree(dest)
        subprocess.run(["git", "clone", "-q", repo["url"], str(dest)], check=True)
        subprocess.run(["git", "-C", str(dest), "config", "user.email", "t@e.invalid"], check=True)
        subprocess.run(["git", "-C", str(dest), "config", "user.name", "t"], check=True)
    if root_clean:
        # the measured clean-root shape: the root's own .grip/ and agents/ excluded, so
        # `git status` AT THE ROOT is clean
        (plain / ".git" / "info" / "exclude").write_text(".grip/\nagents/\n")
    # the verbs act on the cwd, so the fixture leaves the caller standing in the root
    monkeypatch.chdir(plain)
    heads = {repo["name"]: _run_head(plain / repo["path"]) for repo in spec["repos"]}
    assert set(heads) == set(pins), f"the fixture's members must be the spec's: {sorted(heads)}"
    return plain, heads


def _commit(root: Path, message: str = "a commit") -> tuple[int, str]:
    """`store init` then `store commit`, from inside the root (the verbs act on the cwd)."""
    rc, out = _cli("store", "init", str(root))
    assert rc == 0, out
    return _cli("store", "commit", "-m", message, "--json")


def test_commit_records_the_members_pins_not_the_root_head(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """THE WITNESS. On the old code every member's head was the ROOT's.

    The control is inside the row: the two members must record DIFFERENT shas. A verb that
    answered "the workspace root's HEAD" for every member would give one value for both, so
    a single-member fixture could not tell the two apart.
    """
    plain, heads = _superproject(tmp_path, root_clean=True, monkeypatch=monkeypatch)
    rc, out = _commit(plain, "clean-root")
    assert rc == 0, out
    root_commit = json.loads(out)["root_commit"]

    recorded = {
        name: subprocess.run(
            ["git", "-C", str(plain), "ls-tree", "HEAD", name],
            capture_output=True, text=True, check=True,
        ).stdout.split()[2]  # "160000 commit <sha>\t<path>": index 2 is the sha, not the path
        for name in heads
    }
    assert recorded == heads, f"each member's gitlink must be its own HEAD: {recorded} vs {heads}"
    assert len(set(recorded.values())) == len(heads), (
        f"control: the members' shas must differ from each other, or one answer served both: {recorded}"
    )
    assert root_commit not in recorded.values(), (
        "the root's own commit must never be recorded as a member's head"
    )


def test_commit_succeeds_when_the_root_carries_untracked_workspace_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """THE SECOND WITNESS, plus its opposite as a control.

    With the root NOT clean (its own .grip/ and agents/ untracked, the default after
    adoption), the old code refused with "Dirty repos detected" for members that are clean.
    And a dirty MEMBER must still refuse -- otherwise this row would pass on a verb that
    simply stopped checking cleanliness, which is the defect it replaced.
    """
    plain, heads = _superproject(tmp_path, root_clean=False, monkeypatch=monkeypatch)
    rc, out = _commit(plain, "dirty-root")
    assert rc == 0, f"the members are clean; the root's own state is not theirs: {out}"

    member = plain / sorted(heads)[0]
    (member / "scratch.txt").write_text("uncommitted\n")
    rc, out = _cli("store", "commit", "-m", "dirty member")
    assert rc == 3, f"a dirty MEMBER must still refuse at 3, got {rc}: {out}"
    assert sorted(heads)[0] in out, out
    assert "dirty" in out.lower(), out


def test_checkout_restores_member_heads_and_leaves_the_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """THE COVERAGE WITNESS. `store checkout` must act on the members and never move the ROOT."""
    plain, heads = _superproject(tmp_path, root_clean=False, monkeypatch=monkeypatch)
    rc, out = _commit(plain, "before")
    assert rc == 0, out
    root_commit = json.loads(out)["root_commit"]
    root_before = _run_head(plain)

    name = sorted(heads)[0]
    member = plain / name
    subprocess.run(
        ["git", "-C", str(member), "-c", "user.name=test", "-c", "user.email=t@e.invalid",
         "commit", "-q", "--allow-empty", "-m", "moved"],
        check=True,
    )
    moved = _run_head(member)
    assert moved != heads[name], "precondition: the member moved off its recorded pin"

    rc, out = _cli("store", "checkout", root_commit, "--json")
    assert rc == 0, out
    assert _run_head(member) == heads[name], "the member is restored to the commit the root declares"
    assert _run_head(plain) == root_before, "the ROOT repository must not move"


def test_commit_refuses_a_member_path_that_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """THE MISSING-MEMBER ROW, and its assertion CHANGED with the model (see the docstring).

    The alpha verb recorded a missing member as "empty" and succeeded. A native commit has no
    empty to record: the pin IS the member's HEAD, so a member that cannot be read is a
    refusal that names it, never a success that records nothing.
    """
    plain, heads = _superproject(tmp_path, root_clean=False, monkeypatch=monkeypatch)
    # INIT FIRST, so the store records BOTH members; only then remove one. Removing it before
    # init would simply discover one fewer member and the row would never reach the refusal.
    rc, out = _cli("store", "init", str(plain))
    assert rc == 0, out
    name = sorted(heads)[0]
    gone = plain / name
    shutil.rmtree(gone)
    assert not gone.exists(), "the fixture must actually remove the member"

    root_before = root_head(plain)
    assert root_before is not None, (
        "precondition: this root is a CLONE, so it already carries the superproject's own "
        "commits -- the assertion below is that a refusal does not MOVE it"
    )
    rc, out = _cli("store", "commit", "-m", "one-member-missing", "--json")
    assert rc == 5, f"an unreadable member cannot be measured; expected 5, got {rc}: {out}"
    assert name in out, f"the refusal must name the member it could not read: {out}"
    assert root_head(plain) == root_before, "a refused commit must not move the root HEAD"


def root_head(repo: Path) -> str | None:
    result = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--verify", "HEAD"], capture_output=True, text=True
    )
    return result.stdout.strip() or None
