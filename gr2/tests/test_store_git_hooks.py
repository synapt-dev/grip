"""Plain `git commit` and `git push` in a native store run the store's own checks.

THE DEFECT, measured on gr2 2.0.0a8: in a native store, a plain `git add <member> && git commit`
at the root records the member's gitlink (mode 160000), but nothing refuses a pin that is not on
the member's origin (`store commit` refuses it, exit 3, "push it first"), and nothing keeps the
`pin` in `grip.toml` level with the gitlink, so the commit leaves two records of one fact
disagreeing and `store check` exits 4. `store init` installs no hooks.

THE SPECIFIED SHAPE: `store init` installs a `pre-commit` and a `pre-push` hook that call the SAME
engine the verbs use (`_member_coverage`, `_write_native_members`). A commit of an unpushed pin is
refused and names the safe next step; after a plain commit the pins in `grip.toml` equal the
gitlinks. The operator types no gr2 command after setup.

EVERY REFUSAL ROW HAS ITS CONTROL AND ITS MUTATION. The control commits the same change once the pin is
pushed (a hook that refuses everything would pass the refusal rows); the mutation removes the hook
and requires the old behaviour back (a row that stays green without the hook was never about it).

A HOOK NEVER OVERWRITES ANOTHER OWNER'S: `test_a_foreign_hook_is_kept_byte_for_byte` installs over a
pre-existing `pre-commit` and compares bytes. Three further cases are rows here: members left
DETACHED after a recursive clone, `core.hooksPath` hiding the installed hooks (status says so), and
the fold under `git commit <pathspec>`, measured on git 2.x as running the hook against a temporary
`next-index-<pid>.lock` index.

Fixtures are real git with bare local origins and no network.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from tests.test_store_git_native_smoke import configure_identity, git, gr2, make_member, run

HOOKS = ("pre-commit", "pre-push")


def _store(tmp_path: Path) -> Path:
    """Two members cloned side by side, a store init, and one recorded root commit."""
    root = tmp_path / "workspace"
    root.mkdir()
    for name in ("alpha", "beta"):
        remote, _ = make_member(tmp_path, name)
        run(root, "git", "clone", str(remote), name)
        configure_identity(root / name)
    assert gr2(root, "init").returncode == 0
    configure_identity(root)
    first = gr2(root, "commit", "-m", "first")
    assert first.returncode == 0, f"{first.stdout}\n{first.stderr}"
    return root


def _advance(root: Path, member: str, *, push: bool) -> str:
    work = root / member
    marker = work / f"{member}-{len(list(work.glob('*.txt')))}.txt"
    marker.write_text("moved\n")
    git(work, "add", marker.name)
    git(work, "commit", "-m", f"{member} moved")
    if push:
        git(work, "push", "origin", "main")
    return git(work, "rev-parse", "HEAD").stdout.strip()


def _gitlink(root: Path, member: str, rev: str = "HEAD") -> str:
    return git(root, "ls-tree", rev, "--", member).stdout.split()[2]


def _toml_pin(root: Path, member: str, rev: str = "HEAD") -> str:
    import tomllib

    document = tomllib.loads(git(root, "show", f"{rev}:grip.toml").stdout)
    return next(m["pin"] for m in document["members"] if m["name"] == member)


def _head(root: Path) -> str:
    return git(root, "rev-parse", "HEAD").stdout.strip()


def _hook(root: Path, name: str) -> Path:
    return root / ".git" / "hooks" / name


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ── A plain commit of an unpushed pin is refused ──────────────────────────────────────────


def test_a_plain_commit_of_an_unpushed_pin_is_refused_and_names_the_next_step(tmp_path: Path) -> None:
    root = _store(tmp_path)
    before = _head(root)
    _advance(root, "alpha", push=False)

    git(root, "add", "alpha")
    result = git(root, "commit", "-m", "pins an unpushed alpha", check=False)
    out = result.stdout + result.stderr

    assert result.returncode != 0, f"the commit must be refused\n{out}"
    assert "alpha" in out and "is not on origin/main; push it first" in out, out
    # THE FRUIT: the root did not move.
    assert _head(root) == before


def test_the_same_commit_succeeds_once_the_pin_is_pushed(tmp_path: Path) -> None:
    """THE CONTROL: a hook that refuses every commit would pass the row above."""
    root = _store(tmp_path)
    new = _advance(root, "alpha", push=True)

    git(root, "add", "alpha")
    result = git(root, "commit", "-m", "pins a pushed alpha", check=False)

    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    assert _gitlink(root, "alpha") == new


def test_without_the_hook_the_unpushed_pin_commits(tmp_path: Path) -> None:
    """THE MUTATION: remove the hook and the refusal row's behaviour must flip, proving the hook did it."""
    root = _store(tmp_path)
    assert _hook(root, "pre-commit").exists(), "init must have installed the hook"
    _hook(root, "pre-commit").unlink()
    new = _advance(root, "alpha", push=False)

    git(root, "add", "alpha")
    result = git(root, "commit", "-m", "pins an unpushed alpha", check=False)

    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    assert _gitlink(root, "alpha") == new


# ── One record of the pin ─────────────────────────────────────────────────────────────────


def test_a_plain_commit_folds_the_toml_pins_to_the_gitlinks(tmp_path: Path) -> None:
    root = _store(tmp_path)
    new = _advance(root, "alpha", push=True)
    old_beta = _toml_pin(root, "beta")

    git(root, "add", "alpha")
    git(root, "commit", "-m", "alpha moved", check=True)

    assert _gitlink(root, "alpha") == new
    assert _toml_pin(root, "alpha") == new, "grip.toml must be level with the gitlink in the SAME commit"
    assert _toml_pin(root, "beta") == old_beta, "an unmoved member's pin is untouched"
    # And the store's own verb agrees the root is consistent.
    assert gr2(root, "check", check=False).returncode == 0


def test_without_the_hook_the_pins_disagree(tmp_path: Path) -> None:
    """THE MUTATION for row D: the disagreement `store check` exits 4 on is what the fold removes."""
    root = _store(tmp_path)
    _hook(root, "pre-commit").unlink()
    new = _advance(root, "alpha", push=True)

    git(root, "add", "alpha")
    git(root, "commit", "-m", "alpha moved")

    assert _gitlink(root, "alpha") == new
    assert _toml_pin(root, "alpha") != new
    assert gr2(root, "check", check=False).returncode == 4


def test_a_member_detached_after_a_recursive_clone_folds_the_same_way(tmp_path: Path) -> None:
    """A recursive clone leaves members on a DETACHED head; the fold must not assume a branch."""
    root = _store(tmp_path)
    new = _advance(root, "alpha", push=True)
    git(root / "alpha", "checkout", "--detach", new)

    git(root, "add", "alpha")
    result = git(root, "commit", "-m", "alpha moved, detached", check=False)

    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    assert _toml_pin(root, "alpha") == new


# ── The hook never overwrites another owner's ────────────────────────────────────────────────────


def test_a_foreign_hook_is_kept_byte_for_byte(tmp_path: Path) -> None:
    """A pre-existing hook is present when `store init` runs: it must come out byte for byte."""
    root = tmp_path / "workspace"
    root.mkdir()
    for name in ("alpha", "beta"):
        remote, _ = make_member(tmp_path, name)
        run(root, "git", "clone", str(remote), name)
    run(root, "git", "init", "-q", "-b", "main")
    foreign = b"#!/bin/sh\n# somebody else's guard\nexit 0\n"
    _hook(root, "pre-commit").write_bytes(foreign)
    _hook(root, "pre-commit").chmod(0o755)
    before = _digest(_hook(root, "pre-commit"))

    init = gr2(root, "init", check=False)

    assert init.returncode == 0, f"{init.stdout}\n{init.stderr}"
    assert _hook(root, "pre-commit").read_bytes() == foreign, "the foreign hook was changed"
    assert _digest(_hook(root, "pre-commit")) == before
    # The hook that had no owner IS installed, and the foreign one is NAMED, not silently skipped,
    # first by init itself and again by status once the root has a commit to report on.
    assert _hook(root, "pre-push").exists()
    assert "pre-commit" in init.stderr and "another owner" in init.stderr.lower(), init.stderr
    configure_identity(root)
    assert gr2(root, "commit", "-m", "first").returncode == 0
    status = gr2(root, "status", check=False)
    assert "pre-commit" in status.stderr and "another owner" in status.stderr.lower(), status.stderr
    assert _hook(root, "pre-commit").read_bytes() == foreign, "still the foreign hook after commit and status"


def test_init_twice_changes_no_hook_bytes(tmp_path: Path) -> None:
    root = _store(tmp_path)
    before = {name: _digest(_hook(root, name)) for name in HOOKS}

    assert gr2(root, "init", check=False).returncode == 0

    assert {name: _digest(_hook(root, name)) for name in HOOKS} == before


def test_core_hookspath_hides_the_hooks_and_status_says_so(tmp_path: Path) -> None:
    root = _store(tmp_path)
    elsewhere = tmp_path / "other-hooks"
    elsewhere.mkdir()
    git(root, "config", "core.hooksPath", str(elsewhere))

    status = gr2(root, "status", check=False)
    out = status.stdout + status.stderr

    assert "core.hooksPath" in out, f"status must name what hides the hooks\n{out}"
    # And the commit is, truthfully, NOT refused: the hooks are not running. The row pins the report, not a pretence.
    _advance(root, "alpha", push=False)
    git(root, "add", "alpha")
    assert git(root, "commit", "-m", "unchecked", check=False).returncode == 0


def test_a_missing_interpreter_fails_closed_and_names_the_remedy(tmp_path: Path) -> None:
    root = _store(tmp_path)
    hook = _hook(root, "pre-commit")
    text = hook.read_text()
    marker = "PY="
    assert marker in text
    hook.write_text("\n".join(f"PY='/nonexistent/python'" if line.startswith(marker) else line for line in text.splitlines()) + "\n")

    new = _advance(root, "alpha", push=True)
    git(root, "add", "alpha")
    result = git(root, "commit", "-m", "x", check=False)
    out = result.stdout + result.stderr

    assert result.returncode != 0
    assert "/nonexistent/python" in out and "store init" in out, out
    assert new != _head(root)


# ── Partial commits (git commit <pathspec>) ──────────────────────────────────────────────────────


def test_a_pathspec_commit_that_needs_the_fold_is_refused_with_the_remedy(tmp_path: Path) -> None:
    """Measured: a hook's `git add` during `git commit <paths>` lands in a temporary index, so a fold
    there would leave the real index disagreeing with HEAD. The hook refuses and says what to run."""
    root = _store(tmp_path)
    _advance(root, "alpha", push=True)
    git(root, "add", "alpha")
    before = _head(root)

    result = git(root, "commit", "-m", "partial", "alpha", check=False)
    out = result.stdout + result.stderr

    assert result.returncode != 0, out
    assert "without paths" in out, f"the refusal must name the remedy\n{out}"
    assert _head(root) == before


def test_a_pathspec_commit_of_an_unrelated_file_still_commits(tmp_path: Path) -> None:
    """THE CONTROL: refusing every pathspec commit would pass the row above."""
    root = _store(tmp_path)
    (root / "NOTES.md").write_text("not a member\n")
    # The generated allow-list ignores files that are not declared, so the fixture forces this one in.
    git(root, "add", "-f", "NOTES.md")
    before = _head(root)

    result = git(root, "commit", "-m", "notes only", "NOTES.md", check=False)

    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    assert _head(root) != before


def test_a_pathspec_commit_of_an_unpushed_pin_is_still_refused_by_coverage(tmp_path: Path) -> None:
    root = _store(tmp_path)
    _advance(root, "alpha", push=False)
    git(root, "add", "alpha")

    result = git(root, "commit", "-m", "partial unpushed", "alpha", check=False)

    assert result.returncode != 0
    assert "is not on origin/main; push it first" in result.stdout + result.stderr


def test_commit_dash_a_is_checked_like_a_plain_commit(tmp_path: Path) -> None:
    root = _store(tmp_path)
    _advance(root, "alpha", push=False)

    dash_a = git(root, "commit", "-a", "-m", "dash a", check=False)

    assert dash_a.returncode != 0
    assert "push it first" in dash_a.stdout + dash_a.stderr


def test_amend_is_checked_like_a_plain_commit(tmp_path: Path) -> None:
    root = _store(tmp_path)
    before = _head(root)
    _advance(root, "alpha", push=False)
    git(root, "add", "alpha")

    amended = git(root, "commit", "--amend", "--no-edit", check=False)

    assert amended.returncode != 0
    assert "push it first" in amended.stdout + amended.stderr
    assert _head(root) == before


# ── pre-push, the backstop for a bypassed commit hook ────────────────────────────────────────────


def _with_root_origin(tmp_path: Path, root: Path) -> Path:
    bare = tmp_path / "root.git"
    run(tmp_path, "git", "init", "--bare", "-q", "-b", "main", str(bare))
    git(root, "remote", "add", "origin", str(bare))
    return bare


def _root_pushed_nothing(tmp_path: Path, bare: Path) -> bool:
    return run(tmp_path, "git", "--git-dir", str(bare), "rev-parse", "--verify", "-q", "main", check=False).returncode != 0


def test_pre_push_refuses_a_bypassed_commit_whose_records_disagree(tmp_path: Path) -> None:
    """`--no-verify` skips pre-commit AND its fold (a named limit), so the commit carries a gitlink the
    document does not match. Pre-push reads the pushed commit the way `store check` does (consistency first)
    and refuses it, which is the second chance the hook pair exists to give."""
    root = _store(tmp_path)
    bare = _with_root_origin(tmp_path, root)
    _advance(root, "alpha", push=True)
    git(root, "add", "alpha")
    git(root, "commit", "--no-verify", "-m", "bypassed")

    result = git(root, "push", "origin", "main", check=False)
    out = result.stdout + result.stderr

    assert result.returncode != 0, out
    assert "disagrees with its gitlink" in out, out
    assert _root_pushed_nothing(tmp_path, bare)


def test_pre_push_refuses_a_bypassed_consistent_commit_that_is_not_covered(tmp_path: Path) -> None:
    """The coverage half: the records agree (the fold was done by hand) but the pin is on no origin."""
    root = _store(tmp_path)
    bare = _with_root_origin(tmp_path, root)
    old = _toml_pin(root, "alpha")
    new = _advance(root, "alpha", push=False)
    toml = root / "grip.toml"
    toml.write_text(toml.read_text().replace(old, new))
    git(root, "add", "alpha", "grip.toml")
    git(root, "commit", "--no-verify", "-m", "bypassed but consistent")
    assert _gitlink(root, "alpha") == _toml_pin(root, "alpha") == new

    result = git(root, "push", "origin", "main", check=False)
    out = result.stdout + result.stderr

    assert result.returncode != 0, out
    assert "is not on origin/main; push it first" in out, out
    assert _root_pushed_nothing(tmp_path, bare)


def test_pre_push_allows_a_covered_root(tmp_path: Path) -> None:
    """THE CONTROL for the row above."""
    root = _store(tmp_path)
    bare = _with_root_origin(tmp_path, root)

    result = git(root, "push", "origin", "main", check=False)

    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    assert run(tmp_path, "git", "--git-dir", str(bare), "rev-parse", "main").stdout.strip() == _head(root)


# ── The verbs keep their own refusals ─────────────────────────────────────────────


def test_store_commit_still_works_and_keeps_its_own_refusal(tmp_path: Path) -> None:
    root = _store(tmp_path)
    _advance(root, "alpha", push=False)

    refused = gr2(root, "commit", "-m", "x", check=False)
    assert refused.returncode == 3
    assert "push it first" in refused.stdout + refused.stderr

    git(root / "alpha", "push", "origin", "main")
    ok = gr2(root, "commit", "-m", "y", "--json")
    assert json.loads(ok.stdout)["status"] == "committed"
    assert gr2(root, "check", check=False).returncode == 0
