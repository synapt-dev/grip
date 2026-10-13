"""`store commit` refuses to move a member's pin backwards.

Found in the multi-agent review cell: bob pulled the root, so his grip.toml named alice's merge
on demo-web, but his demo-web checkout was still at the commit before it. bob's own feature did
not touch demo-web; `store commit` recorded the checkout's HEAD anyway, rc 0, and `store push`
published a pin that undid alice's recorded merge for everyone.

THE RULE: a member whose checkout is strictly BEHIND its recorded pin, or whose checkout does
not hold the recorded pin at all (it never fetched it), is refused at exit 3, naming the member
and both shas, with `gr2 store materialize` as the next step. `--allow-rewind` records the
older pin on purpose. A checkout that is AHEAD of its pin commits as before (the control), and a
checkout that DIVERGED from it is out of scope here.

Premium boundary: OSS (grip). Local workspace orchestration over git; no identity, org, or
entitlement semantics.
"""

from __future__ import annotations

import subprocess
import tomllib
from pathlib import Path

import pytest

from gr2.python_cli.app import app

from tests.conftest import make_cli_runner


def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, text=True, capture_output=True, check=check)


def _git_out(cwd: Path, *args: str) -> str:
    return _git(cwd, *args).stdout.strip()


def _cli(*args: str) -> tuple[int, str]:
    result = make_cli_runner().invoke(app, list(args))
    return result.exit_code, (result.stdout or "") + (result.stderr or "")


def _bare_remote(tmp_path: Path, name: str) -> Path:
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


def _pin(root: Path, name: str) -> str:
    doc = tomllib.loads((root / "grip.toml").read_text(encoding="utf-8"))
    return next(m["pin"] for m in doc["members"] if m["name"] == name)


def _commit_and_push(repo: Path, name: str) -> str:
    (repo / name).write_text(f"{name}\n")
    _git(repo, "add", name)
    _git(repo, "commit", "-q", "-m", f"add {name}")
    _git(repo, "push", "-q", "origin", "main")
    return _git_out(repo, "rev-parse", "HEAD")


@pytest.fixture
def ws(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Two pushed members; alpha's pin then advanced to a second pushed commit and recorded."""
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
    _commit_and_push(root / "alpha", "merged.txt")
    rc, out = _cli("store", "commit", "-m", "alpha merged")
    assert rc == 0, out
    return root


def test_a_checkout_behind_its_pin_is_refused_and_nothing_is_recorded(ws: Path) -> None:
    alpha = ws / "alpha"
    recorded = _pin(ws, "alpha")
    _git(alpha, "reset", "-q", "--hard", "HEAD~1")   # stale: the checkout is back before the merge
    behind = _git_out(alpha, "rev-parse", "HEAD")
    root_before = _git_out(ws, "rev-parse", "HEAD")

    rc, out = _cli("store", "commit", "-m", "feature 2 merged")

    assert rc == 3, out
    assert "alpha" in out and recorded[:12] in out and behind[:12] in out, out
    assert "gr2 store materialize" in out and "--allow-rewind" in out, out
    assert "beta" not in out, out   # only the member that would move backwards is named
    assert _pin(ws, "alpha") == recorded
    assert _git_out(ws, "rev-parse", "HEAD") == root_before


def test_a_checkout_that_never_fetched_its_pin_is_refused(ws: Path) -> None:
    """bob's exact case: grip.toml names a pin the member checkout does not hold at all."""
    alpha = ws / "alpha"
    recorded = _pin(ws, "alpha")
    _git(alpha, "reset", "-q", "--hard", "HEAD~1")
    stale = _git_out(alpha, "rev-parse", "HEAD")
    _git(alpha, "update-ref", "refs/remotes/origin/main", stale)
    _git(alpha, "reflog", "expire", "--expire=now", "--all")
    _git(alpha, "gc", "-q", "--prune=now")
    assert _git(alpha, "cat-file", "-e", f"{recorded}^{{commit}}", check=False).returncode != 0  # really absent
    root_before = _git_out(ws, "rev-parse", "HEAD")

    rc, out = _cli("store", "commit", "-m", "feature 2 merged")

    assert rc == 3, out
    assert "alpha" in out and recorded[:12] in out and "gr2 store materialize" in out, out
    assert _pin(ws, "alpha") == recorded
    assert _git_out(ws, "rev-parse", "HEAD") == root_before


def test_allow_rewind_records_the_older_pin_on_purpose(ws: Path) -> None:
    alpha = ws / "alpha"
    _git(alpha, "reset", "-q", "--hard", "HEAD~1")
    behind = _git_out(alpha, "rev-parse", "HEAD")

    rc, out = _cli("store", "commit", "-m", "revert alpha's pin", "--allow-rewind")

    assert rc == 0, out
    assert _pin(ws, "alpha") == behind


def test_control_a_checkout_ahead_of_its_pin_commits_as_before(ws: Path) -> None:
    ahead = _commit_and_push(ws / "alpha", "next.txt")

    rc, out = _cli("store", "commit", "-m", "alpha moves on")

    assert rc == 0, out
    assert _pin(ws, "alpha") == ahead


def test_a_shallow_checkout_that_does_not_reach_its_pin_says_it_cannot_tell(ws: Path, tmp_path: Path) -> None:
    """A shallow clone can be AHEAD of a pin older than its depth and still lack the pin's object. A missing pin
    says nothing about direction there, so the refusal says it cannot tell and names `git fetch --unshallow`,
    never `gr2 store materialize`, which would move a checkout that is ahead back to the pin."""
    import shutil

    alpha = ws / "alpha"
    recorded = _pin(ws, "alpha")
    url = _git_out(alpha, "remote", "get-url", "origin")
    other = tmp_path / "other"
    subprocess.run(["git", "clone", "-q", url, str(other)], capture_output=True, check=True)
    _git(other, "config", "user.email", "o@e.invalid")
    _git(other, "config", "user.name", "o")
    for i in range(3):
        _commit_and_push(other, f"later-{i}.txt")
    shutil.rmtree(alpha)
    subprocess.run(["git", "clone", "-q", "--depth", "1", f"file://{url}", str(alpha)], capture_output=True, check=True)
    assert _git(alpha, "cat-file", "-e", f"{recorded}^{{commit}}", check=False).returncode != 0  # past the depth
    root_before = _git_out(ws, "rev-parse", "HEAD")

    rc, out = _cli("store", "commit", "-m", "alpha advanced")

    assert rc == 3, out
    assert "cannot tell" in out and "git fetch --unshallow" in out and recorded[:12] in out, out
    assert "gr2 store materialize" not in out, out
    assert _pin(ws, "alpha") == recorded and _git_out(ws, "rev-parse", "HEAD") == root_before


def test_a_switch_to_a_branch_that_drops_the_pin_is_refused(ws: Path) -> None:
    """The member is switched to another pushed branch that diverged before the recorded pin. That branch's upstream
    covers HEAD, so the switch would be adopted, and the old pin's commits would leave the record with exit 0."""
    alpha = ws / "alpha"
    recorded = _pin(ws, "alpha")
    _git(alpha, "checkout", "-q", "-b", "side", "HEAD~1")
    side = _commit_on(alpha, "side.txt")
    _git(alpha, "push", "-q", "-u", "origin", "side")
    root_before = _git_out(ws, "rev-parse", "HEAD")

    rc, out = _cli("store", "commit", "-m", "alpha switched")

    assert rc == 3, out
    assert "does not contain the recorded pin" in out and recorded[:12] in out and side[:12] in out, out
    assert "--allow-rewind" in out, out
    assert _pin(ws, "alpha") == recorded and _git_out(ws, "rev-parse", "HEAD") == root_before


def test_control_a_switch_to_a_branch_that_contains_the_pin_commits(ws: Path) -> None:
    alpha = ws / "alpha"
    recorded = _pin(ws, "alpha")
    _git(alpha, "checkout", "-q", "-b", "next")
    head = _commit_on(alpha, "next.txt")
    _git(alpha, "push", "-q", "-u", "origin", "next")
    assert _git(alpha, "merge-base", "--is-ancestor", recorded, head, check=False).returncode == 0

    rc, out = _cli("store", "commit", "-m", "alpha on next")

    assert rc == 0, out
    assert _pin(ws, "alpha") == head


def _commit_on(repo: Path, name: str) -> str:
    (repo / name).write_text(f"{name}\n")
    _git(repo, "add", name)
    _git(repo, "commit", "-q", "-m", f"add {name}")
    return _git_out(repo, "rev-parse", "HEAD")


def test_a_plain_git_commit_through_the_hook_refuses_the_same_rewind(ws: Path) -> None:
    """The pre-commit hook runs the verb's rewind check, so `git commit` and `store commit` agree."""
    alpha = ws / "alpha"
    recorded = _pin(ws, "alpha")
    _git(alpha, "reset", "-q", "--hard", "HEAD~1")
    root_before = _git_out(ws, "rev-parse", "HEAD")
    _git(ws, "add", "alpha")

    done = _git(ws, "commit", "-q", "-m", "feature 2 merged", check=False)

    assert done.returncode != 0, done.stdout + done.stderr
    assert "behind the recorded pin" in done.stderr and "gr2 store commit --allow-rewind" in done.stderr, done.stderr
    assert _git_out(ws, "rev-parse", "HEAD") == root_before
    assert _pin(ws, "alpha") == recorded


def test_control_a_plain_git_commit_of_a_checkout_ahead_folds_the_pin(ws: Path) -> None:
    ahead = _commit_and_push(ws / "alpha", "next.txt")
    _git(ws, "add", "alpha")

    done = _git(ws, "commit", "-q", "-m", "alpha moves on", check=False)

    assert done.returncode == 0, done.stdout + done.stderr
    assert _pin(ws, "alpha") == ahead


def _set_pin_line(root: Path, old: str, new: str) -> None:
    text = (root / "grip.toml").read_text(encoding="utf-8")
    assert text.count(old) == 1
    (root / "grip.toml").write_text(text.replace(old, new), encoding="utf-8")


def test_an_edited_pin_line_does_not_hide_a_rewind_from_the_verb(ws: Path) -> None:
    """The recorded pin is the one the root commit holds; grip.toml's pin line is editable, so it is not the record."""
    alpha = ws / "alpha"
    recorded = _pin(ws, "alpha")
    _git(alpha, "reset", "-q", "--hard", "HEAD~1")
    behind = _git_out(alpha, "rev-parse", "HEAD")
    _set_pin_line(ws, recorded, behind)
    root_before = _git_out(ws, "rev-parse", "HEAD")

    rc, out = _cli("store", "commit", "-m", "pin line edited back")

    assert rc == 3, out
    assert recorded[:12] in out and behind[:12] in out, out
    assert _git_out(ws, "rev-parse", "HEAD") == root_before


def test_an_edited_pin_line_does_not_hide_a_rewind_from_the_hook(ws: Path) -> None:
    alpha = ws / "alpha"
    recorded = _pin(ws, "alpha")
    _git(alpha, "reset", "-q", "--hard", "HEAD~1")
    behind = _git_out(alpha, "rev-parse", "HEAD")
    _set_pin_line(ws, recorded, behind)   # the pin line set back by hand, its gitlink staged too
    root_before = _git_out(ws, "rev-parse", "HEAD")
    _git(ws, "add", "alpha", "grip.toml")

    done = _git(ws, "commit", "-q", "-m", "pin line edited back", check=False)

    assert done.returncode != 0, done.stdout + done.stderr
    assert "behind the recorded pin" in done.stderr, done.stderr
    assert _git_out(ws, "rev-parse", "HEAD") == root_before


def _rename_member(root: Path, old: str, new: str) -> None:
    text = (root / "grip.toml").read_text(encoding="utf-8")
    line = f'name = "{old}"'
    assert text.count(line) == 1
    (root / "grip.toml").write_text(text.replace(line, f'name = "{new}"'), encoding="utf-8")


def test_a_renamed_member_with_its_pin_set_back_is_refused_by_the_verb(ws: Path) -> None:
    """The record is found by the member's path, so a new name in the working grip.toml does not hide a rewind."""
    alpha = ws / "alpha"
    recorded = _pin(ws, "alpha")
    _git(alpha, "reset", "-q", "--hard", "HEAD~1")
    behind = _git_out(alpha, "rev-parse", "HEAD")
    _set_pin_line(ws, recorded, behind)
    _rename_member(ws, "alpha", "alpha2")
    root_before = _git_out(ws, "rev-parse", "HEAD")

    rc, out = _cli("store", "commit", "-m", "renamed and set back")

    assert rc == 3, out
    assert recorded[:12] in out and behind[:12] in out, out
    assert _git_out(ws, "rev-parse", "HEAD") == root_before


def test_a_renamed_member_with_its_pin_set_back_is_refused_by_the_hook(ws: Path) -> None:
    alpha = ws / "alpha"
    recorded = _pin(ws, "alpha")
    _git(alpha, "reset", "-q", "--hard", "HEAD~1")
    behind = _git_out(alpha, "rev-parse", "HEAD")
    _set_pin_line(ws, recorded, behind)
    _rename_member(ws, "alpha", "alpha2")
    root_before = _git_out(ws, "rev-parse", "HEAD")
    _git(ws, "add", "alpha", "grip.toml")

    done = _git(ws, "commit", "-q", "-m", "renamed and set back", check=False)

    assert done.returncode != 0, done.stdout + done.stderr
    assert "behind the recorded pin" in done.stderr, done.stderr
    assert _git_out(ws, "rev-parse", "HEAD") == root_before


def test_a_switch_that_drops_the_pin_is_refused_through_the_hook_as_through_the_verb(ws: Path) -> None:
    """The hook and the verb share one resolver, so the switch clause holds for a plain `git commit` too. On dev the
    hook never adopted a moved branch and refused this state with "push it first", advice that does nothing for a
    pushed branch. Once the hook adopts (this change), the clause is what refuses it: a pushed side branch that
    diverged before the recorded pin is not BEHIND it, so without the recorded branch the hook would adopt the switch
    and the pin's commits would leave the record with exit 0."""
    alpha = ws / "alpha"
    recorded = _pin(ws, "alpha")
    _git(alpha, "checkout", "-q", "-b", "side", "HEAD~1")
    side = _commit_on(alpha, "side.txt")
    _git(alpha, "push", "-q", "-u", "origin", "side")
    root_before = _git_out(ws, "rev-parse", "HEAD")
    _git(ws, "add", "alpha")

    done = _git(ws, "commit", "-q", "-m", "alpha switched", check=False)

    assert done.returncode != 0, done.stdout + done.stderr
    assert "does not contain the recorded pin" in done.stderr and side[:12] in done.stderr, done.stderr
    assert "gr2 store commit --allow-rewind" in done.stderr, done.stderr
    assert _git_out(ws, "rev-parse", "HEAD") == root_before
    assert _git_out(ws, "show", "HEAD:grip.toml").count(recorded) == 1


def test_control_a_switch_that_contains_the_pin_commits_through_the_hook(ws: Path) -> None:
    alpha = ws / "alpha"
    _git(alpha, "checkout", "-q", "-b", "next")
    head = _commit_on(alpha, "next.txt")
    _git(alpha, "push", "-q", "-u", "origin", "next")
    _git(ws, "add", "alpha")

    done = _git(ws, "commit", "-q", "-m", "alpha on next", check=False)

    assert done.returncode == 0, done.stdout + done.stderr
    toml = _git_out(ws, "show", "HEAD:grip.toml")
    assert head in toml and 'ref = "next"' in toml, toml


def test_full_mode_refuses_a_checkout_behind_its_pin_and_never_carries_it_as_a_layer(tmp_path: Path) -> None:
    """Full mode reads a coverage refusal (exit 3) as "carry a layer". The rewind check runs inside the same resolver,
    so its exit 3 must not be read that way: an older pin is refused, never recorded as a layer."""
    from tests.test_store_full_mode_carrier import _advance, _full_store
    from tests.test_store_git_native_smoke import gr2

    root = _full_store(tmp_path)
    recorded = _advance(root, "alpha", push=True)
    assert gr2(root, "commit", "-m", "alpha moved").returncode == 0
    _git(root / "alpha", "reset", "-q", "--hard", "HEAD~1")
    root_before = _git_out(root, "rev-parse", "HEAD")

    done = gr2(root, "commit", "-m", "alpha behind", check=False)

    assert done.returncode == 3, done.stdout + done.stderr
    assert "behind the recorded pin" in done.stderr and recorded[:12] in done.stderr, done.stderr
    assert _git_out(root, "rev-parse", "HEAD") == root_before
