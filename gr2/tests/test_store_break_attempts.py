"""The thin-slice adversarial pass: break attempts 01-21, each a named test that must FAIL CLOSED.

Source of record: the thin-slice design note, tracked privately and deliberately
not linked here (OSS surfaces describe their own behaviour, not the private record).
Writers: Sentinel (this file), built in parallel with the verbs it tests.

WHAT "FAIL CLOSED" MEANS HERE, and it is asserted per attempt rather than assumed:
a refusal with the contract's exit code, the root HEAD unmoved, and no partial tree.
Section 5's exit table for the whole `store` group is: 0 ok, 2 usage,
3 refused on coverage or cleanliness, 4 refused as inconsistent or beta, 5 cannot measure.
Every refusal names the member and both values it compared.

⚠ THE ATTEMPT LIST, DERIVED AND DISCLOSED. Section 8's table numbers only 01-13; the rest
live in two places the doc mentions in passing, and this file was written from the doc, not
from the request summary:

  * section 8's table   01 03 04 05 07 08 10 11 12 13, and 02 06 09 marked beta below
  * section 11 (root-side changes)   14 (credentials in a remote URL), 15 (symlink member path)
  * section 6 ("break attempts, added to section 8")   17 18 19 20
  * section 12 ("for section 8")   21 (a tracked, non-member root folder, written below)

⚠ ATTEMPT 16 IS DEFINED NOWHERE. a full-text search of the design records returns 8, 14, 15
and 18; `grep` for `attempt 16`, `break_16`, `test_break_16` across the design records and `gr2/`
returns nothing. The numbering has a hole between the symlink refusal (15) and the unit-path
escapes (17). NO TEST IS WRITTEN FOR 16, deliberately: a test named for a defect nobody
specified asserts the writer's guess, which is the opposite of what this file is for. If a
reader can name it, it goes in; until then the gap is stated rather than filled.

⚠ ATTEMPT 20 IS NOW IN THIS FILE, at the bottom above the beta rows, and the paragraph that
used to say it was not is superseded here rather than deleted. Section 6 puts 17-20 in one
block and this file read 20 as "the same lane as 19, assigned elsewhere"; what changed the
call is that 19's re-aim made the pair legible: **19 asserts the verb prefers the PATH, and 20
asserts it has anywhere else to look.** MEASURED on the name-placed shape, no site looks at
the NAME coordinate, so all three verbs refuse with exit 5 naming the declared path only --
which is a gap a reader meets by moving a checkout, and it belongs beside 19 where the two
coordinates are pinned from both sides. It is `xfail(strict=True)` and genuinely red.

BETA ROWS ARE `xfail(strict=True)`, per the doc: "so the day beta lands they are forced to
turn green, and not only to exist." `strict=True` is the load-bearing half — without it the
day beta lands the row silently passes and nothing forces anyone to look.

EVERY ROW HERE FAILS UNTIL THE VERBS LAND. That is the point of a spec-first file, and it is
why the harness has its own control (`test_harness_control_...` below) that PASSES on real
git today: a file whose every row is red cannot distinguish "the verb is unbuilt" from "the
harness is broken", and those need different responses.

Premium boundary: OSS (grip). Local workspace orchestration over git; no identity, org, or
entitlement semantics.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest

from gr2.python_cli.app import app
from gr2.python_cli import grip as grip_mod

from tests.conftest import make_cli_runner

# The workspace spec's schema, named by design section 4 as the DELIVERABLE that defines the
# unit grammar, with `gr2 spec validate` and `store check` validating against it. Rows 17 and
# 19 cite this path rather than inventing a unit shape: guessing the format is the error both
# readers caught in this file, and the design names the file precisely so a
# row can wait for it honestly.
SPEC_SCHEMA_REL = "gr2/gr2/schemas/gr2-workspace-spec-v1.schema.json"
SPEC_SCHEMA_PATH = Path(__file__).resolve().parents[1] / "gr2" / "schemas" / (
    "gr2-workspace-spec-v1.schema.json"
)


# ---------------------------------------------------------------------------
# Harness. Real git, bare remotes in tmp_path, no network.
# ---------------------------------------------------------------------------


def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=cwd, text=True, capture_output=True, check=check
    )


def _git_out(cwd: Path, *args: str) -> str:
    return _git(cwd, *args).stdout.strip()


def _bare_remote(tmp_path: Path, name: str) -> str:
    """A bare remote with one commit on `main`, as a file:// URI.

    Lifted in shape from `test_cli_adapter_error_boundary.py::_bare_remote`, so the
    slice's suite uses the harness the repo already trusts rather than a new one.
    """
    src = tmp_path / f"{name}-src"
    src.mkdir(parents=True, exist_ok=True)
    _git(src, "init", "-q", "-b", "main")
    _git(src, "config", "user.email", "t@e.invalid")
    _git(src, "config", "user.name", "t")
    (src / "README.md").write_text(f"# {name}\n")
    _git(src, "add", ".")
    _git(src, "commit", "-q", "-m", "initial")
    remote = tmp_path / f"{name}.git"
    subprocess.run(
        ["git", "clone", "--bare", str(src), str(remote)],
        capture_output=True,
        check=True,
    )
    return remote.as_uri()


def _set_root_origin(root: Path, url: str) -> None:
    """Give the root an `origin`, whether or not one already exists.

    `git remote add` fails with exit 128 when the name is taken OR when the directory is
    not a repo yet, and either way the row dies inside its fixture rather than at a
    contract assertion. Section 3 makes the root a git repo; this only sets the URL.
    """
    if _git(root, "remote", "get-url", "origin", check=False).returncode == 0:
        _git(root, "remote", "set-url", "origin", url)
    else:
        _git(root, "remote", "add", "origin", url)


def _clone_member(tmp_path: Path, name: str) -> Path:
    """An ordinary working clone of `name`'s remote, sibling-shaped (the gr1 layout)."""
    url = _bare_remote(tmp_path, name)
    dest = tmp_path / "ws" / name
    dest.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "clone", "-q", url, str(dest)], capture_output=True, check=True)
    _git(dest, "config", "user.email", "t@e.invalid")
    _git(dest, "config", "user.name", "t")
    return dest


def _cli(*args: str) -> tuple[int, str]:
    result = make_cli_runner().invoke(app, list(args))
    return result.exit_code, (result.stdout or "") + (result.stderr or "")


def _head(repo: Path) -> str:
    return _git_out(repo, "rev-parse", "HEAD")


def _ls_remote(url: str, ref: str) -> str:
    """The sha a remote advertises for `ref`, or '' when it advertises nothing."""
    out = subprocess.run(
        ["git", "ls-remote", url, ref], text=True, capture_output=True, check=False
    ).stdout.strip()
    return out.split("\t")[0] if out else ""


def _set_member_upstream(text: str, member_path: str, upstream: str) -> str:
    """Set ONE member's `upstream` field, located by its `path`, leaving the others alone.

    Section 4 makes `upstream` a grip.toml field (default "origin/main"), so a row that
    wants to test coverage-against-upstream must set the FIELD. A row that instead adds a
    git remote named `upstream` exercises nothing the contract describes.
    """
    blocks = text.split("[[members]]")
    hit = False
    for i, block in enumerate(blocks[1:], start=1):
        if f'path = "{member_path}"' in block:
            new, n = re.subn(r"(?m)^upstream\s*=.*$", f'upstream = "{upstream}"', block)
            assert n == 1, f"expected one upstream line in the {member_path!r} block, found {n}"
            blocks[i] = new
            hit = True
            break
    assert hit, f"no [[members]] block with path = {member_path!r}"
    return "[[members]]".join(blocks)


def _set_member_name(text: str, member_path: str, name: str) -> str:
    """Rename ONE member, located by its `path`, leaving every other block alone.

    A block-scoped rename, and the primitive break 19 was missing. That row moved a member's
    two coordinates with `text.replace('path = "alpha"', 'name = "renamed-member"\\npath = ...', 1)`,
    which INSERTS a second `name` key into the block it matched instead of renaming the first
    -- so the document fails `tomllib` with `Cannot overwrite a value`, every verb below exits
    non-zero with an EMPTY payload, and the row never reaches a single assertion. Locating the
    member first is what makes the rename well-formed; `_set_member_upstream` and
    `_set_member_remote` above already work this way.
    """
    blocks = text.split("[[members]]")
    hit = False
    for i, block in enumerate(blocks[1:], start=1):
        if f'path = "{member_path}"' in block:
            new, n = re.subn(r"(?m)^name\s*=.*$", f'name = "{name}"', block)
            assert n == 1, f"expected one name line in the {member_path!r} block, found {n}"
            blocks[i] = new
            hit = True
            break
    assert hit, f"no [[members]] block with path = {member_path!r}"
    return "[[members]]".join(blocks)


def _set_member_remote(text: str, member_path: str, remote: str, url: str) -> str:
    """Set ONE member's named remote URL under `[members.remotes]`, by member path.

    Deterministic on purpose. The credential row used to rewrite the first `file://` it
    found and then assert on the member name `alpha`, which are not necessarily the same
    member.
    """
    blocks = text.split("[[members]]")
    hit = False
    for i, block in enumerate(blocks[1:], start=1):
        if f'path = "{member_path}"' in block:
            new, n = re.subn(
                rf"(?m)^(\s*){re.escape(remote)}\s*=.*$", rf'\g<1>{remote} = "{url}"', block
            )
            assert n == 1, f"expected one {remote!r} line in the {member_path!r} block, found {n}"
            blocks[i] = new
            hit = True
            break
    assert hit, f"no [[members]] block with path = {member_path!r}"
    return "[[members]]".join(blocks)


@pytest.fixture(autouse=True)
def _commit_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every commit this file makes carries an identity that does not come from the host.

    MODULE-WIDE RATHER THAN INSIDE `two_member_ws`, deliberately: `_clone_member` makes a
    commit, is called by BOTH workspace fixtures, and `gr1_sibling_ws` is a second entry
    point. A fix scoped to one fixture would leave the other path green here and red on a
    CI runner, which is the worse outcome -- a green that means "this machine".

    WHY IT IS NEEDED: the gr2 conftest points `GIT_CONFIG_GLOBAL` at a BLANK config and sets
    `GIT_CONFIG_NOSYSTEM` (see `_isolated_git_config`), so a commit with no `user.name` set
    anywhere falls back to git's host auto-detect -- the OS gecos. Measured on this host: the
    same call with the env absent resolves to the developer's own global identity, and with
    no global config and an empty gecos it fails as `empty ident name`, exit 128. The rows
    that make a bare `git commit` in a workspace root set no local identity, so without this
    those rows are green on a Mac and red on a CI runner for a reason that has nothing to do
    with any contract they pin.

    ENV, NOT `git config`: `GIT_AUTHOR_*`/`GIT_COMMITTER_*` take precedence over config, so
    this holds for a command run in any directory without needing a repo to exist first.
    """
    monkeypatch.setenv("GIT_AUTHOR_NAME", "gr2 suite")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "gr2-suite@example.invalid")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "gr2 suite")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "gr2-suite@example.invalid")
    # ASSERTED, not assumed. A fixture that silently failed to set these would restore the
    # exact host-dependence this closes, and nothing would say so. `git var` is the witness
    # rather than `os.environ`: it proves git RESOLVES the env, not merely that it is set.
    probe = subprocess.run(
        ["git", "var", "GIT_AUTHOR_IDENT"], capture_output=True, text=True, check=False
    )
    assert probe.returncode == 0 and "gr2-suite@example.invalid" in probe.stdout, (
        f"the fixture must pin commit identity; `git var GIT_AUTHOR_IDENT` said "
        f"rc={probe.returncode} {probe.stdout.strip()!r}"
    )


@pytest.fixture
def two_member_ws(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The section 2 shape: a workspace root holding two member clones side by side,
    NOT itself a git repo. Both members are pushed, so coverage is satisfied and a
    break attempt has to break something.

    The cwd IS SET here and it is load-bearing. The store verbs default `workspace_root`
    to `Path.cwd()` (app.py:137/149, 918, 980); only `store init` is handed a path. Without
    this chdir, every row after init runs `commit`/`check`/`status`/`push` against pytest's
    cwd -- in CI, `gr2/` inside the grip checkout -- and then asserts on a `root` the verb
    never touched. That means a row can be red for a reason that
    has nothing to do with the contract it claims to pin.
    """
    ws = tmp_path / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    for name in ("alpha", "beta"):
        clone = _clone_member(tmp_path, name)
        _git(clone, "push", "-q", "origin", "main")
    monkeypatch.chdir(ws)
    # ASSERTED, not assumed: a fixture that silently failed to chdir would restore the
    # exact defect this exists to close, and every row below would go back to measuring
    # the wrong tree while still reporting a verdict.
    assert Path.cwd() == ws, f"the fixture must run the verbs inside {ws}, cwd is {Path.cwd()}"
    return ws


def _assert_init_ran(root: Path) -> None:
    """Section 3: `store init` makes the root a git repo AND writes `grip.toml`.

    Asserted BEFORE any row reads the manifest. Without it an incomplete `store init`
    surfaces as a FileNotFoundError inside a fixture read -- which
    `xfail(strict=True, raises=AssertionError)` would absorb as though the row had reached
    its contract assertion. A precondition that fails loudly is the difference between
    "the verb is unbuilt" and "this row could not build its fixture", and the two need
    different responses.
    """
    assert (root / ".git").is_dir(), f"store init must make {root} a git repo (section 3)"
    assert (root / "grip.toml").is_file(), f"store init must write grip.toml under {root} (section 3)"


def _assert_flow_ran(root: Path, member: str | None = None) -> None:
    """The store verbs acted on `root`. Row 13's lesson, generalised: a row that measures
    a delta must first assert that the thing producing it actually happened, or a red row
    and a broken harness are indistinguishable.

    `member` names a gitlink the CALLING row is about to read with
    `git rev-parse HEAD:<member>`. Asserting it here is what keeps that row's death at a
    NAMED assertion rather than a foreign exception type: the read is made through
    `_git_out`, which defaults to `check=True`, so on a root whose gitlink the verb never
    wrote it raises `CalledProcessError` -- the exact class the file's `raises=AssertionError`
    discipline exists to exclude.

    THE CHECK IS THE EXIT STATUS, NOT TRUTHINESS, and that is load-bearing. Measured on this
    host: `git rev-parse HEAD:<absent>` does NOT print nothing for an unresolvable path. It
    ECHOES THE ARGUMENT to stdout and exits 128 --
    `rc=128, stdout='HEAD:<member>', stderr="fatal: path '<member>' does not exist in 'HEAD'"`.
    A truthiness test on `.stdout.strip()` is therefore TRUE on precisely the broken tree this
    asserts against, and cannot fail. The first version of this assertion was written that way
    and was inert; the probe in the commit that follows it caught that.

    AND THE SAME RULE ONE LINE UP, which was the last place it had not been applied. `git
    rev-parse HEAD` echoes its argument too: on a repo with NO commit it is `rc=128,
    stdout='HEAD'`, so asserting the bare stdout passed on exactly the tree its own message
    names -- `store init` writing grip.toml and creating the repo while
    `store commit` never commits is the realistic half-built state, and it is reachable. The head
    assertion now checks the exit status for the same reason the member assertion below does. A
    sweep of every `check=False` site and every truthiness assertion in this file found this to
    be the single remaining instance; the file already carried the correct idiom twice (the
    section-8 control's `--verify -q HEAD`, and the rc check below), so this was a missed
    application rather than an unknown technique.
    """
    assert (root / "grip.toml").is_file(), f"store init must have written grip.toml under {root}"
    head = _git(root, "rev-parse", "HEAD", check=False)
    assert head.returncode == 0, (
        f"store commit must have made a root commit in {root}; "
        f"`git rev-parse HEAD` exited {head.returncode}: {head.stderr.strip()}"
    )
    if member is not None:
        link = _git(root, "rev-parse", f"HEAD:{member}", check=False)
        assert link.returncode == 0, (
            f"store commit must have written a gitlink for {member!r} in {root}; "
            f"`git rev-parse HEAD:{member}` exited {link.returncode}: {link.stderr.strip()}"
        )


# ---------------------------------------------------------------------------
# THE HARNESS CONTROL. Passes on real git today; it is the control that makes a red
# break row below mean "the verb is unbuilt" rather than "this file cannot run".
# ---------------------------------------------------------------------------


def test_harness_control_bare_remote_round_trip(two_member_ws: Path, tmp_path: Path) -> None:
    """NOT a break attempt: the harness proving itself.

    A bare remote, a clone, an unpushed commit, and `ls-remote` able to distinguish
    present from absent — which is the instrument attempt 03 depends on. If this row
    ever goes red, every red below it is suspect.
    """
    clone = two_member_ws / "alpha"
    remote_url = _bare_remote(tmp_path, "probe")
    assert _ls_remote(remote_url, "refs/heads/main") != "", "a pushed ref must be visible"
    assert _ls_remote(remote_url, "refs/heads/nope") == "", "an absent ref must be absent"
    (clone / "README.md").write_text("# alpha\nwork\n")
    _git(clone, "add", ".")
    _git(clone, "commit", "-q", "-m", "unpushed")
    assert _head(clone) != _git_out(Path(clone), "rev-parse", "origin/main")


# ---------------------------------------------------------------------------
# 01 — pin a sha the member's upstream lacks
# ---------------------------------------------------------------------------


def test_break_01_pin_a_sha_upstream_lacks(two_member_ws: Path) -> None:
    """`store commit` exit 3; the root HEAD does not move."""
    root = two_member_ws
    assert _cli("store", "init", str(root))[0] == 0
    _assert_init_ran(root)
    rc, out = _cli("store", "commit", "-m", "first", "--json")
    assert rc == 0, out
    root_head_after_first = _head(root)

    alpha = root / "alpha"
    (alpha / "README.md").write_text("# alpha\nlocal only\n")
    _git(alpha, "add", ".")
    _git(alpha, "commit", "-q", "-m", "never pushed")

    rc, out = _cli("store", "commit", "-m", "second", "--json")
    assert rc == 3, f"a pin the upstream lacks must be refused with exit 3, got {rc}: {out}"
    assert "alpha" in out, "the refusal must name the member"
    assert "push it first" in out, "the refusal must say what to do"
    assert _head(root) == root_head_after_first, "the root HEAD must not move on a refusal"


# ---------------------------------------------------------------------------
# 03 — push the root while a member branch is unpushed
# ---------------------------------------------------------------------------


def test_break_03_push_root_with_unpushed_member(two_member_ws: Path, tmp_path: Path) -> None:
    """`store push` exit 3, and the root ref is ABSENT on the root remote.

    The absent check needs a PRESENT CONTROL on the same instrument, or an `ls-remote`
    that cannot see refs at all reports the safe-looking answer.

    Section 2 (line 17): "a coverage check refuses to commit OR PUSH a pin that the
    member's upstream does not have." Section 5: push refuses "when any member is dirty or
    any HEAD snapshot pin is uncovered (the same 5a check)". Section 5a: the check is
    `git merge-base --is-ancestor <pin> <upstream>`, whose input is the PIN. A member whose
    HEAD has moved is `unpinned` -- a status observation beside `stale` and `missing`, per
    section 5's status table, NOT a refusal. So the row drives an uncovered PIN.
    """
    root = two_member_ws
    root_remote = tmp_path / "root.git"
    # -b main is load-bearing: the suite isolates git config (conftest._isolated_git_config),
    # so a bare `git init -q --bare` takes its initial branch from nothing and lands on
    # `master`, while `store push` publishes the ROOT branch. The clone then gets an unborn
    # HEAD and `git rev-list --count HEAD` fails at 128. `_bare_remote` already pins it.
    subprocess.run(
        ["git", "init", "-q", "-b", "main", "--bare", str(root_remote)],
        capture_output=True,
        check=True,
    )
    assert _cli("store", "init", str(root))[0] == 0
    _assert_init_ran(root)
    _set_root_origin(root, root_remote.as_uri())
    assert _cli("store", "commit", "-m", "first")[0] == 0
    _assert_flow_ran(root, member="alpha")

    # THE UNCOVERED PIN, INSIDE THE ROOT SNAPSHOT ITSELF. This is the input section 5a
    # defines: `git merge-base --is-ancestor <pin> <upstream>`, where the input is the PIN.
    # The first version moved alpha's working HEAD and expected `store push` to refuse --
    # but section 5 runs `store check` over the pins already in `grip.toml`, and a member
    # whose HEAD moved is `unpinned`, which section 5 lists as a STATUS OBSERVATION beside
    # `stale` and `missing`, not a refusal. Moving HEAD tested a rule the note does not
    # define; a test written to an undefined rule makes the test the spec.
    #
    # So the pin itself is made uncoverable: a real commit in alpha that is NEVER pushed,
    # written as the pin AND as the gitlink (they AGREE, so this drives COVERAGE and not
    # the 4-consistency refusal), committed into the root, and then pushed.
    alpha = root / "alpha"
    covered_pin = _git_out(root, "rev-parse", "HEAD:alpha")
    (alpha / "uncovered.txt").write_text("local only, never pushed\n")
    _git(alpha, "add", ".")
    _git(alpha, "commit", "-q", "-m", "a commit its upstream will never have")
    uncovered = _head(alpha)
    upstream_main = _ls_remote(_git_out(alpha, "remote", "get-url", "origin"), "refs/heads/main")
    # ASSERT BOTH HALVES OF THE MUTATION: the pin moved, AND it is not reachable.
    assert uncovered != covered_pin, "the fixture must actually create a new pin value"
    assert upstream_main != uncovered, "the uncovered pin must NOT be on the upstream"
    assert upstream_main == covered_pin, "the upstream should still be at the covered pin"

    text = (root / "grip.toml").read_text()
    assert covered_pin in text, "the fixture must start from the pin store commit wrote"
    (root / "grip.toml").write_text(text.replace(covered_pin, uncovered))
    _git(root, "update-index", "--add", "--cacheinfo", f"160000,{uncovered},alpha")
    # v5, same class as the witnesses file: the root repo carries no identity by design, so
    # the production -c identity is supplied explicitly rather than borrowed from the desk.
    _git(root, "-c", "user.name=gr2", "-c", "user.email=gr2@example.invalid",
         "commit", "-q", "-m", "malformed root: a pin its upstream does not have")

    rc, out = _cli("store", "push", "--json")
    assert rc == 3, (
        f"a root snapshot carrying an uncovered pin must refuse the push with exit 3, "
        f"got {rc}: {out}"
    )
    assert "alpha" in out, f"the refusal must name the member; got: {out}"

    url = root_remote.as_uri()
    # READ THE REF NAME FROM THE ROOT, never a literal: the root's branch after init follows
    # init.defaultBranch, so `refs/heads/main` can be absent vacuously.
    branch = _git_out(root, "symbolic-ref", "--short", "HEAD")
    assert _ls_remote(url, f"refs/heads/{branch}") == "", f"the root ref ({branch}) must be ABSENT"
    # CONTROL: the same instrument on a ref that IS there
    _git(root, "branch", "-f", "controlbranch", "HEAD")
    _git(root, "push", "-q", "origin", "controlbranch")
    assert _ls_remote(url, "refs/heads/controlbranch") != "", (
        "the absent check is only meaningful if ls-remote can see a present ref"
    )


# ---------------------------------------------------------------------------
# 04 — upstream force-pushes over the pinned branch
# ---------------------------------------------------------------------------


def test_break_04_upstream_force_push_over_the_pin(
    two_member_ws: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`store status` reports `missing`; `materialize` in a fresh clone of the root exits 3
    naming the pin. In the slice nothing covers it, and saying so IS the fail-closed
    outcome (section 11.4: one behaviour, not two).

    The first version built `orphan` and never used it -- it force-pushed
    alpha's OWN main, which re-pushes the same sha and leaves the pin reachable -- and it
    was missing the materialize half entirely. The force-push now comes FROM the orphan,
    and it is aimed at alpha's remote.
    """
    root = two_member_ws
    assert _cli("store", "init", str(root))[0] == 0
    _assert_init_ran(root)
    assert _cli("store", "commit", "-m", "first")[0] == 0
    _assert_flow_ran(root)

    alpha = root / "alpha"
    pinned = _head(alpha)
    alpha_remote = _git_out(alpha, "remote", "get-url", "origin")

    # a fresh clone of the ROOT, made while the pin is still covered, for the materialize half
    fresh = tmp_path / "fresh-root"
    subprocess.run(["git", "clone", "-q", str(root), str(fresh)], capture_output=True, check=True)

    # rewrite the member's upstream to an unrelated history: the pin becomes unreachable.
    # The push is FROM the orphan TO alpha's remote -- pushing alpha's own main would be a
    # no-op with a force flag and would leave the pin covered.
    orphan = tmp_path / "orphan"
    orphan.mkdir()
    _git(orphan, "init", "-q", "-b", "main")
    _git(orphan, "config", "user.email", "t@e.invalid")
    _git(orphan, "config", "user.name", "t")
    (orphan / "README.md").write_text("# rewritten\n")
    _git(orphan, "add", ".")
    _git(orphan, "commit", "-q", "-m", "rewrite")
    _git(orphan, "push", "-q", "--force", alpha_remote, "main")
    # ASSERT THE MUTATION LANDED: a force-push that did not change the remote would leave
    # the pin covered and every assertion below would test nothing.
    assert _ls_remote(alpha_remote, "refs/heads/main") != pinned, (
        "the force-push must have moved the member's main off the pin"
    )

    rc, out = _cli("store", "status", "--json")
    # `missing` is a reported STATE, and section 8 names no exit for it. The informational
    # states are upstream/stale/unpinned; `missing` is
    # not named there, so this asserts the state and the non-failing exit together.
    assert rc == 0, f"`missing` is a reported state, not a refusal, got {rc}: {out}"
    assert "missing" in out, f"a force-pushed pin must read `missing`, got: {out}"
    assert pinned[:8] in out, "the missing pin must be named"

    # the materialize half, from the fresh clone. Section 5 gives the verb `[<commit>]`, so
    # it acts on the CLONE'S cwd -- passing a path here would exercise nothing.
    monkeypatch.chdir(fresh)
    rc, out = _cli("store", "materialize", "--json")
    assert rc == 3, f"materialize must refuse an unreachable pin with exit 3, got {rc}: {out}"
    assert pinned[:8] in out, f"the refusal must name the pin, got: {out}"


# ---------------------------------------------------------------------------
# 05 — shallow clone of the root
# ---------------------------------------------------------------------------


def test_break_05_shallow_clone_of_the_root(
    two_member_ws: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`materialize` either succeeds from member origins or refuses with the reason.
    Never a hole: a tree that is silently partial is the failure this row exists for.

    Section 5 gives the verb `materialize [<commit>]` -- it acts on the cwd, which is the
    shallow clone here. The first version passed the clone's PATH, which is not a commit
    and is not what the verb reads.
    """
    root = two_member_ws
    root_remote = tmp_path / "root.git"
    # -b main is load-bearing: the suite isolates git config (conftest._isolated_git_config),
    # so a bare `git init -q --bare` takes its initial branch from nothing and lands on
    # `master`, while `store push` publishes the ROOT branch. The clone then gets an unborn
    # HEAD and `git rev-list --count HEAD` fails at 128. `_bare_remote` already pins it.
    subprocess.run(
        ["git", "init", "-q", "-b", "main", "--bare", str(root_remote)],
        capture_output=True,
        check=True,
    )
    assert _cli("store", "init", str(root))[0] == 0
    _assert_init_ran(root)
    _set_root_origin(root, root_remote.as_uri())
    assert _cli("store", "commit", "-m", "first")[0] == 0
    assert _cli("store", "push")[0] == 0
    _assert_flow_ran(root)

    shallow = tmp_path / "shallow"
    subprocess.run(
        ["git", "clone", "-q", "--depth", "1", root_remote.as_uri(), str(shallow)],
        capture_output=True, check=True,
    )
    # ASSERT THE FIXTURE: a shallow clone of depth 1 is the whole point of this row, and a
    # clone that quietly got full history would make every assertion below vacuous.
    assert _git_out(shallow, "rev-list", "--count", "HEAD") == "1", (
        "the fixture must be a depth-1 clone"
    )
    monkeypatch.chdir(shallow)
    rc, out = _cli("store", "materialize", "--json")
    if rc == 0:
        for name in ("alpha", "beta"):
            assert (shallow / name).exists(), f"{name} missing from a successful materialize"
    else:
        assert rc == 3, f"a refusal here must be exit 3 with a reason, got {rc}: {out}"
        assert out.strip(), "a refusal must carry the reason, not be silent"


# ---------------------------------------------------------------------------
# 07 — hand-edit the gitlink or `pin` so they disagree
# ---------------------------------------------------------------------------


def test_break_07_gitlink_and_pin_disagree(two_member_ws: Path) -> None:
    """Every verb that reads the pin exits 4 and names BOTH values.

    `store status` additionally prints its FULL TABLE in every case (2026-09-27): "never refuses" means status always RUNS and reports every member rather
    than aborting early -- it does not mean it always exits 0, because a diagnostic that
    exits 0 on an inconsistency is the silent-success class. Status's codes are 0 (every
    member readable and consistent), 4 (any member whose gitlink and pin disagree, both
    values named in that member's row), 5 (any member that cannot be measured, when no
    member is 4 -- 4 outranks 5).
    """
    root = two_member_ws
    assert _cli("store", "init", str(root))[0] == 0
    _assert_init_ran(root)
    assert _cli("store", "commit", "-m", "first")[0] == 0
    _assert_flow_ran(root, member="alpha")

    real_pin = _git_out(root, "rev-parse", "HEAD:alpha")
    # ⚠ WHICH SIDE TAKES THE ZEROS IS LOAD-BEARING, and this row never ran without knowing it.
    # git 2.50.1 REFUSES to write a null (all-zero) sha into the index -- `update-index
    # --cacheinfo 160000,000...0,alpha` dies with "cache entry has null sha1" and writes nothing,
    # and `--index-info` refuses it the same way. All-ones IS accepted. So a gitlink of forty
    # zeros cannot be built at all: this row was failing in its own SETUP, before `_cli` was ever
    # called, and had never once exercised the verb it exists to test. The roles are swapped
    # here instead -- the GITLINK takes the ones, and the PIN takes the zeros, which is safe
    # because `pin` is a grip.toml field that git never validates. The disagreement is identical
    # and the later assertion, which requires BOTH values in the output, still holds.
    _git(root, "update-index", "--add", "--cacheinfo", f"160000,{'1' * 40},alpha")
    text = (root / "grip.toml").read_text()
    assert real_pin in text, "the fixture must start from a recorded pin"
    (root / "grip.toml").write_text(text.replace(real_pin, "0" * 40))
    # ⚠ THE DISAGREEMENT MUST LAND IN THE SNAPSHOT, NOT ONLY IN THE WORKING INDEX
    # (measured 2026-09-28). Reading HEAD's tree is the correct surface for a check whose
    # failure mode is "a clone materializes the wrong sha": the root snapshot is what gets
    # pushed and cloned, so a check reading the INDEX would pass a snapshot that breaks every
    # clone -- the silent-success class. The row wrote its sentinel with `update-index` and
    # stopped there, so `store check` correctly read the COMMITTED gitlink (6b6066e0 at the
    # probe) against the pin and never printed the fixture's own two values; the assertion
    # below could not hold however correct the verb was. Commit the edit so the tree carries
    # it. Measured surfaces at the probe: `ls-files -s alpha` -> 1111... , `ls-tree HEAD
    # alpha` -> 6b6066e0, verb output -> gitlink 6b6066e0 vs pin 0000....
    _git(root, "add", "grip.toml")
    _git(root, "commit", "-q", "-m", "malformed root: a gitlink its pin disagrees with")
    assert _git_out(root, "rev-parse", "HEAD:alpha") == "1" * 40, (
        "the fixture must put the sentinel gitlink in the COMMITTED tree, or the row "
        "measures the index surface the verb does not read"
    )

    for verb in (["store", "check"], ["store", "status"], ["store", "push"]):
        rc, out = _cli(*verb, "--json")
        assert rc == 4, f"{' '.join(verb)} must refuse a gitlink/pin disagreement with 4, got {rc}: {out}"
        # NAME BOTH COMPARED VALUES. The first version asked `real_pin[:8] or "1"*8`, and
        # after the edit the two compared values are 0000... (the gitlink) and 1111... (the
        # pin) -- real_pin is NEITHER, so the disjunction could not detect the failure it
        # names.
        assert "0" * 8 in out and "1" * 8 in out, (
            f"{' '.join(verb)} must name BOTH compared values (the gitlink and the pin); got: {out}"
        )

    # the full table still prints: status reports every member, whatever the exit code
    rc, out = _cli("store", "status", "--json")
    assert rc == 4, out
    for member in ("alpha", "beta"):
        assert member in out, (
            f"status must print its full table -- {member} is missing from a run that "
            f"exited 4 (the full table prints in every case)"
        )


# ---------------------------------------------------------------------------
# 08 — two remotes; the pin is reachable only from the fork
# ---------------------------------------------------------------------------


def test_break_08_check_runs_against_upstream_not_origin(
    two_member_ws: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """exit 3: the check ran against the member's declared `upstream`, not the literal
    remote name `origin`. Section 4 makes `upstream` a grip.toml FIELD (default
    "origin/main") and section 5a says coverage is checked against it, "never against the
    literal name origin".

    The first version added a GIT remote named `upstream` and never set the
    field, so it exercised nothing the contract describes -- and it cloned `fork` and never
    used it. The field is set here, and the pin is reachable from origin but NOT from the
    declared upstream.
    """
    root = two_member_ws
    alpha = root / "alpha"
    # Native store verbs operate on cwd. Passing root as init's positional argument
    # selects the legacy .grip initializer and returns a misleading success.
    monkeypatch.chdir(root)

    # a commit that IS on alpha's origin ...
    (alpha / "README.md").write_text("# on origin\n")
    _git(alpha, "add", ".")
    _git(alpha, "commit", "-q", "-m", "on origin")
    _git(alpha, "push", "-q", "origin", "main")
    landed = _head(alpha)

    # ... and a second remote that has NOT got it
    _git(alpha, "remote", "add", "other", _bare_remote(tmp_path, "alpha-other"))
    _git(alpha, "fetch", "-q", "other")

    assert _cli("store", "init")[0] == 0
    _assert_init_ran(root)
    toml_path = root / "grip.toml"
    toml_path.write_text(_set_member_upstream(toml_path.read_text(), "alpha", "other/main"))

    rc, out = _cli("store", "commit", "-m", "first", "--json")
    assert rc == 3, (
        "a pin reachable from origin but not from the DECLARED upstream must be refused "
        f"with exit 3, got {rc}: {out}"
    )
    # EVERY ONE OF THE THREE, not either of two: the message claims the refusal names the
    # member, the pin AND the upstream. The first version asked `landed[:8] in out or
    # "other/main" in out`, which passes when only the upstream string is present and never
    # checks the member name at all -- a disjunction that cannot detect the failure it names.
    for token in ("alpha", landed[:8], "other/main"):
        assert token in out, (
            f"the refusal must name the member, the pin and the upstream it compared; "
            f"{token!r} is missing from: {out}"
        )
    # section 8: every row fails CLOSED, so the root must still have no commit
    assert _git(root, "rev-parse", "--verify", "-q", "HEAD", check=False).returncode != 0, (
        "a refused first commit must leave the root without a commit"
    )


# ---------------------------------------------------------------------------
# 10 — two root branches (lanes) pinning different pushed shas of one member
# ---------------------------------------------------------------------------


def test_break_10_two_root_branches_two_pins(
    two_member_ws: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both commit; switching branches plus `materialize` gives each its tree.

    The first version never materialized, so the half of section 8's
    sentence that reads "gives each its tree" was unasserted -- it proved two root commits
    existed, not that each lane materializes to its OWN member tree.
    """
    root = two_member_ws
    assert _cli("store", "init", str(root))[0] == 0
    _assert_init_ran(root)
    alpha = root / "alpha"

    assert _cli("store", "commit", "-m", "lane one")[0] == 0
    _assert_flow_ran(root)
    one = _head(root)
    (alpha / "README.md").write_text("# alpha\nsecond\n")
    _git(alpha, "add", ".")
    _git(alpha, "commit", "-q", "-m", "second")
    _git(alpha, "push", "-q", "origin", "main")

    _git(root, "checkout", "-q", "-b", "lane-two")
    assert _cli("store", "commit", "-m", "lane two")[0] == 0
    two = _head(root)
    assert two != one

    _git(root, "checkout", "-q", "main")
    assert _head(root) == one
    _git(root, "checkout", "-q", "lane-two")
    assert _head(root) == two

    # EACH LANE MATERIALIZES TO ITS OWN MEMBER TREE. The member HEAD after materializing must
    # be the pin that lane's root commit recorded, and the two pins must differ -- otherwise
    # "gives each its tree" is satisfied by any single tree.
    pins = {}
    for branch, commit in (("main", one), ("lane-two", two)):
        pin = _git_out(root, "rev-parse", f"{commit}:alpha")
        pins[branch] = pin
        fresh = tmp_path / f"fresh-{branch}"
        subprocess.run(
            ["git", "clone", "-q", "-b", branch, str(root), str(fresh)],
            capture_output=True, check=True,
        )
        monkeypatch.chdir(fresh)
        rc, out = _cli("store", "materialize", "--json")
        assert rc == 0, f"materialize for {branch} must succeed, got {rc}: {out}"
        got = _head(fresh / "alpha")
        assert got == pin, (
            f"{branch} must materialize alpha at that lane's own pin {pin[:8]}, got {got[:8]}"
        )
    assert pins["main"] != pins["lane-two"], "the two lanes must pin different member shas"


# ---------------------------------------------------------------------------
# 11 — a beta field present in grip.toml
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("field", ["overlay", "staged", "nested"])
def test_break_11_beta_field_is_refused(two_member_ws: Path, field: str) -> None:
    """exit 4 naming the field; never ignored. A beta key silently accepted is a
    contract the reader cannot see."""
    root = two_member_ws
    assert _cli("store", "init", str(root))[0] == 0
    _assert_init_ran(root)
    text = (root / "grip.toml").read_text()
    (root / "grip.toml").write_text(text + f'\n{field} = "beta-value"\n')

    rc, out = _cli("store", "commit", "-m", "x", "--json")
    assert rc == 4, f"a beta field must be refused with 4, got {rc}: {out}"
    assert field in out, f"the refusal must name the field `{field}`, got: {out}"


# ---------------------------------------------------------------------------
# 12 — root adoption: an existing .gitignore ignoring the member dirs
# ---------------------------------------------------------------------------


def test_break_12_adoption_tracks_gitlinks_and_leaves_gitignore_alone(two_member_ws: Path) -> None:
    """`store commit` tracks the gitlinks; `.gitignore` byte-identical before and after.
    The slice ADOPTS a root that already exists, and an adoption that rewrites canon
    is not an adoption.

    The first version ran `store init` FIRST and created the root repo
    afterwards, so it exercised a fresh init and not adoption at all. Section 8's witness
    shape is an EXISTING root repo, and section 5 says an adopted root "gets no history
    rewrite and no `.gitignore` edit" -- so the pre-existing history is asserted here too.
    """
    root = two_member_ws
    # the root ALREADY EXISTS as a git repo, with the ignore list in place
    gitignore = root / ".gitignore"
    gitignore.write_text("alpha/\nbeta/\n")
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@e.invalid")
    _git(root, "config", "user.name", "t")
    _git(root, "add", ".gitignore")
    _git(root, "commit", "-q", "-m", "root with an ignore list")
    before = gitignore.read_bytes()
    pre_head = _head(root)

    assert _cli("store", "init")[0] == 0
    _assert_init_ran(root)

    # ADOPTION, not init: the pre-existing commit is still the root of the history
    assert _git_out(root, "rev-list", "--count", "HEAD") == "1", (
        "store init must ADOPT the existing root repo, not rewrite or re-create its history"
    )
    assert _head(root) == pre_head, "adoption must leave the existing root commit in place"
    assert gitignore.read_bytes() == before, "adoption must not edit .gitignore"

    rc, out = _cli("store", "commit", "-m", "pinned", "--json")
    assert rc == 0, out
    assert gitignore.read_bytes() == before, ".gitignore must be byte-identical after adoption"
    tree = _git_out(root, "ls-tree", "HEAD")
    assert "160000 commit" in tree, "the gitlinks must be tracked despite .gitignore"


# ---------------------------------------------------------------------------
# 13 — gr2 writes nothing under any `.git/`
# ---------------------------------------------------------------------------


def _listing(path: Path) -> set[str]:
    return {
        str(p.relative_to(path))
        for p in path.rglob("*")
        if p.is_file()
    }


def _git_fetch_metadata_delta(members: list[Path]) -> dict[Path, set[str]]:
    """What GIT ITSELF writes under a member's `.git/` for the fetch section 5a requires.

    Section 5a mandates `git fetch <upstream-remote>` in the MEMBER repo, and Git writes
    its own fetch bookkeeping when that runs (FETCH_HEAD, remote-tracking refs). A row
    that prohibits EVERY new file under a member's `.git/` therefore fails a CORRECT
    implementation for Git's required side effects -- it forbids the fetch the contract
    mandates.

    So the same fetch is run here as a CONTROL and its measured delta is returned to be
    SUBTRACTED. That is deliberately a measurement and not a whitelist of gr2-owned names:
    a name list would be the writer's guess at a naming convention, which is the same
    defect this fix is repairing. Returns {member_path: {new files under its .git/}}.
    """
    before = {m: _listing(m / ".git") for m in members}
    for m in members:
        _git(m, "fetch", "-q", "origin", check=False)
    return {m: _listing(m / ".git") - before[m] for m in members}


def test_break_13_nothing_written_under_any_git_dir(two_member_ws: Path) -> None:
    """After the suite's flow, a scan of every member's `.git/` and the root's `.git/`
    finds no file gr2 created BEYOND what Git writes for the fetch section 5a requires.
    The scan carries a PLANTED CONTROL FILE, so a scan that cannot see files cannot
    report the safe-looking answer.

    MUTATION (proves the row still has teeth after the subtraction): have gr2 write any
    file of its own under a member's `.git/` (e.g. `.git/gr2-state.json`) and this row
    must fail. The subtraction covers Git's fetch metadata only, so a gr2-owned name is
    not in it and cannot be masked by it.
    """
    root = two_member_ws
    members = [root / "alpha", root / "beta"]
    gitdirs = [root / ".git"] + [m / ".git" for m in members]

    control = root / "alpha" / ".git" / "SENTINEL-PLANTED-CONTROL"
    control.write_text("control\n")
    try:
        seen = _listing(gitdirs[1])
        assert "SENTINEL-PLANTED-CONTROL" in seen, (
            "the scan must be able to see a planted file, or its clean result means nothing"
        )
    finally:
        control.unlink()

    # THE CONTROL FETCH RUNS BEFORE THE SNAPSHOT, so the files Git rewrites for the
    # check's and commit's own fetches are already present in `before` and cannot be read
    # as gr2's artifacts. `git_meta` is subtracted as well, for any name Git writes only
    # on a later fetch (a ref it did not create the first time).
    git_meta = _git_fetch_metadata_delta(members)

    assert _cli("store", "init", str(root))[0] == 0
    _assert_init_ran(root)
    assert _cli("store", "commit", "-m", "first")[0] == 0

    # THE SNAPSHOT IS TAKEN AFTER init AND commit, and the ordering IS the fix. Taken
    # before init, the root's `.git` does not exist yet, so `if d.exists()` DROPPED it out
    # of `before` -- and `before.items()` is what the loop below scans. The row claimed to
    # scan the root's own `.git` and structurally could never do it: no measurement of it
    # could fail. ASSERTED here so the claim cannot silently come undone again.
    before = {d: _listing(d) for d in gitdirs if d.exists()}
    assert (root / ".git") in before, (
        "the root's .git must exist AND be in the snapshot, or this row cannot scan it"
    )

    # `store check` is READ-ONLY by contract (design section 5), so the root's own `.git`
    # needs no subtraction: after the snapshot, nothing may appear under it. The member
    # `.git` axis keeps the fetch subtraction, because section 5a mandates a fetch whose
    # metadata Git itself writes. BOUNDARY, named rather than implied: commit's own
    # git-machinery writes under the root `.git` happen BEFORE this snapshot, so what is
    # witnessed here is the read-only verb, plus any gr2-private file any verb writes after
    # it -- not a gr2 write made during commit.
    assert _cli("store", "check")[0] == 0

    for d, files in before.items():
        if not d.exists():
            continue
        allowed = git_meta.get(d.parent, set()) if d.parent in members else set()
        created = (_listing(d) - files) - allowed
        assert not created, (
            f"gr2 created files under {d}: {sorted(created)[:5]} "
            f"(Git's own fetch metadata, {len(allowed)} file(s), is already subtracted)"
        )


# ---------------------------------------------------------------------------
# 14 — credentials in a remote URL (the hard gate, section 11.1)
# ---------------------------------------------------------------------------


def test_break_14_credentials_in_a_remote_url_are_refused(two_member_ws: Path) -> None:
    """exit 4, naming the member and the remote NAME — and NEVER printing the URL.
    The refusal must not leak the secret it is refusing.

    The first version rewrote the first `file://` it found and, when there
    was none, appended a comment instead — so no credential existed, a correct verb exited
    0, and the row tested nothing while looking like it covered the case. The member whose
    remote carries the credential is now chosen deterministically, and the fixture asserts
    the credential is really in the file before the verb is run.
    """
    root = two_member_ws
    assert _cli("store", "init", str(root))[0] == 0
    _assert_init_ran(root)
    secret = "DUMMY-USERINFO-NOT-A-TOKEN"
    toml_path = root / "grip.toml"
    text = toml_path.read_text()
    assert "file://" in text, "the generated grip.toml must carry a real remote URL to rewrite"
    toml_path.write_text(
        _set_member_remote(
            text, "alpha", "origin", f"https://user:{secret}@github.com/synapt-dev/alpha.git"
        )
    )
    assert secret in toml_path.read_text(), "the rewritten remote must really carry the credential"

    rc, out = _cli("store", "commit", "-m", "x", "--json")
    assert rc == 4, f"a credentialed remote URL must be refused with 4, got {rc}: {out}"
    assert secret not in out, "the refusal must NEVER print the credential"
    assert "alpha" in out and "origin" in out, (
        f"the refusal must name the MEMBER and the remote NAME; got: {out}"
    )


# ---------------------------------------------------------------------------
# 15 — a member path that is a symlink (section 11.2)
# ---------------------------------------------------------------------------


def test_break_15_symlink_member_path_is_refused(two_member_ws: Path, tmp_path: Path) -> None:
    """exit 4, "link mode is beta". A gitlink needs a real checkout at its path;
    `update-index --cacheinfo 160000` over a symlink leaves the root reporting a type
    change forever. The first witness is shaped exactly this way."""
    root = two_member_ws
    link = root / "gamma"
    link.symlink_to(root / "alpha")
    assert _cli("store", "init", str(root))[0] == 0
    _assert_init_ran(root)

    rc, out = _cli("store", "commit", "-m", "first", "--json")
    assert rc == 4, f"a symlinked member path must be refused with 4, got {rc}: {out}"
    # Section 4: a beta refusal is exit 4 NAMING the field and saying "beta". The first
    # version's `"link mode is beta" in out.lower() or "beta" in out.lower()` collapses to
    # "beta" alone, so it could not tell this refusal from any other beta refusal, or from
    # an unrelated message that happened to contain the word.
    assert "beta" in out.lower(), f"the refusal must say beta, got: {out}"
    assert "link" in out.lower(), f"the refusal must name the link mode it refused, got: {out}"


# ---------------------------------------------------------------------------
# 17 — a unit path that escapes the root
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad", ["absolute", "../../outside", "../sibling/inside", "symlinked"]
)
# THE MARK CAME OFF HERE, and the reason it carried was stale when it did. It said
# the schema file "does not exist yet"; gr2/gr2/schemas/gr2-workspace-spec-v1.schema.json
# has been present and load-bearing since section 4 landed. What actually kept these
# rows red was narrower: `gr2 spec validate` read `grip.toml` OR the workspace spec and
# never both, and it exited 1 where section 5's table has no 1. With both documents
# validated and 4 returned, all four cases below go green, and a strict xfail on a
# green row is a failure -- which is how the mark was forced off rather than removed
# on a hunch.
def test_break_17_unit_path_outside_the_root(two_member_ws: Path, tmp_path: Path, bad: str) -> None:
    """exit 4 naming the unit; nothing created or written outside the root.

    THE UNIT GRAMMAR IS NOT GUESSED HERE. Design section 4 names the deliverable and the file:
    `gr2/gr2/schemas/gr2-workspace-spec-v1.schema.json`, beside
    `gr2-materialization-plan-v1.schema.json`, with `gr2 spec validate` and `store check`
    validating against it. Section 6c's refusal is on `units[].path` IN THAT SPEC.

    The first version wrote `[[units]]` into `grip.toml` -- a section 4 BETA
    field -- so the exit 4 came from the beta refusal rather than the path escape, and it
    fingerprinted a directory none of its bad paths resolved into, so the fingerprint could
    not fail. Section 6c's fourth case (a path through a symlinked directory) was missing.

    Until the schema exists this row asserts its own precondition and fails there, which is
    what makes it honest: it cites the source of the grammar instead of inventing one. The
    mark comes off in the change that delivers the file, and that change writes the fixture
    from it.
    """
    root = two_member_ws
    schema = SPEC_SCHEMA_PATH
    assert schema.is_file(), (
        f"the unit grammar is defined by {SPEC_SCHEMA_REL} (design section 4); until that file "
        f"exists this row cannot build its fixture without guessing the format"
    )
    # The four cases section 6c names, driven through `gr2 spec validate` against the schema.
    paths = {
        "absolute": str(tmp_path / "outside" / "abs-unit"),
        "../../outside": "../../outside",
        "../sibling/inside": "../sibling/inside",
        "symlinked": "escape-link/inside",   # a path through a symlinked directory
    }
    bad_path = paths[bad]

    # the gr1 sibling area: where a `../x` unit would land. Section 6c: a refusal must keep
    # the gripspace as the root and must NOT suggest making a parent directory the root.
    parent = root.parent
    control = parent / "SENTINEL-PLANTED-CONTROL"
    control.write_text("control\n")
    (tmp_path / "outside").mkdir(exist_ok=True)
    link = root / "escape-link"
    if not link.exists():
        link.symlink_to(tmp_path, target_is_directory=True)

    assert _cli("store", "init", str(root))[0] == 0
    _assert_init_ran(root)
    before_parent = {p.name for p in parent.iterdir()}
    assert "SENTINEL-PLANTED-CONTROL" in before_parent, "the fingerprint must see a planted file"

    spec_dir = root / ".grip"
    spec_dir.mkdir(exist_ok=True)
    (spec_dir / "workspace_spec.toml").write_text(
        f'[[units]]\nname = "bad"\npath = "{bad_path}"\n'
    )

    rc, out = _cli("spec", "validate", "--json")
    assert rc == 4, f"a unit path outside the root must be refused with 4, got {rc}: {out}"
    # WHICH PATH, not just the unit's name. This read `bad in out or "bad" in out`,
    # and the second half was near-vacuous: every fixture here names its unit "bad",
    # so the assertion held for ANY output mentioning the unit at all -- including one
    # that never said which path was refused.
    #
    # MEASURED on all four cases, and they are not all the same shape: the three
    # lexical escapes name the path as written, and the symlinked case names the
    # SYMLINK (`.../ws/escape-link`) instead, because the link is the thing that makes
    # the path land outside. Asserting the written path for all four was my first
    # version and it went red on exactly that case, which is the measurement that
    # produced this form.
    assert "unit 'bad'" in out, f"the refusal must name the unit; got: {out}"
    assert bad_path in out or "symlink" in out, (
        "the refusal must say WHICH path it refused -- the path as written, or the "
        f"symlink it passes through; got: {out}"
    )
    after_parent = {p.name for p in parent.iterdir()}
    assert after_parent == before_parent, (
        f"nothing may be created beside the root; got {sorted(after_parent - before_parent)}"
    )


@pytest.mark.parametrize(
    "bad", ["absolute", "../outside", "dot-segment", "symlinked"]
)
def test_break_17_root_repo_path_outside_the_root(
    two_member_ws: Path, tmp_path: Path, bad: str
) -> None:
    """exit 4 naming the repo AND the path; nothing created outside the root.

    BREAK ATTEMPT 17 AT THE ROOT COORDINATE, which is not the coordinate the row above
    drives. That one escapes through `units[].path`; this one through `repos[].path`,
    the workspace spec's own top-level repo list, and nothing contained it. The
    mechanism is pathlib's: `Path("/ws") / "/tmp/outside/x"` IS `"/tmp/outside/x"`,
    because an absolute right operand REPLACES the base -- so an absolute declared path
    named a directory outside the root, and every probe downstream of it (`exists`,
    `repo_path_state`, the hook read) ran against that outside directory. Found by
    Apollo while reviewing the unit-path fix, measured on base: an absolute path there
    plans and APPLIES a clone outside the workspace root.

    THE SAME PREDICATE CONTAINS IT HERE, `canonicalize_workspace_path`, which the unit
    coordinate already delegates to. That is deliberate rather than convenient: a second
    hand-rolled containment check is a second thing that can disagree with the first,
    and this file's whole subject is what happens when two spellings of one rule drift.

    AND IT CLOSES A REAL ASYMMETRY rather than only the absolute case. The unit grammar
    refuses `./m`, `m/`, `m/.` and `a//m` as spellings the same helper's segment rule
    rejects, while the ROOT level accepted them, because the root level had no segment
    rule at all. `dot-segment` below is that case, and it is asserted here so the
    stricter root grammar is a deliberate, visible choice rather than a side effect a
    reader discovers later. No spec any compiler writes carries those spellings, so the
    direction that costs anything -- something legal now refused -- is empty.
    """
    root = two_member_ws
    paths = {
        "absolute": str(tmp_path / "outside" / "abs-repo"),
        "../outside": "../outside",
        "dot-segment": "alpha/./beta",
        "symlinked": "escape-link/alpha",
    }
    bad_path = paths[bad]

    parent = root.parent
    control = parent / "SENTINEL-PLANTED-CONTROL"
    control.write_text("control\n")
    (tmp_path / "outside").mkdir(exist_ok=True)
    link = root / "escape-link"
    if not link.exists():
        link.symlink_to(tmp_path, target_is_directory=True)

    assert _cli("store", "init", str(root))[0] == 0
    _assert_init_ran(root)
    before_parent = {p.name for p in parent.iterdir()}
    assert "SENTINEL-PLANTED-CONTROL" in before_parent, "the fingerprint must see a planted file"

    spec_dir = root / ".grip"
    spec_dir.mkdir(exist_ok=True)
    (spec_dir / "workspace_spec.toml").write_text(
        f'workspace_name = "ws"\n\n[[repos]]\nname = "bad"\npath = "{bad_path}"\n'
        f'url = "https://example.invalid/bad.git"\n'
    )

    rc, out = _cli("spec", "validate", "--json")
    assert rc == 4, f"a repo path outside the root must be refused with 4, got {rc}: {out}"
    assert "repo 'bad'" in out, f"the refusal must name the repo; got: {out}"
    assert bad_path in out or "symlink" in out, (
        "the refusal must say WHICH path it refused -- the path as written, or the "
        f"symlink it passes through; got: {out}"
    )
    after_parent = {p.name for p in parent.iterdir()}
    assert after_parent == before_parent, (
        f"nothing may be created beside the root; got {sorted(after_parent - before_parent)}"
    )


# ---------------------------------------------------------------------------
# Store migration — alpha .grip/.git becomes one root commit and remains readable
# ---------------------------------------------------------------------------


def test_store_migrate_moves_alpha_head_to_native_root(two_member_ws: Path) -> None:
    """Section 6a: migration preserves the alpha HEAD pins without replaying history."""
    root = two_member_ws
    grip_mod.grip_init(root)
    alpha_head = grip_mod.grip_snapshot(
        root,
        {"alpha": root / "alpha", "beta": root / "beta"},
        message="alpha snapshot",
    )
    pins = {name: _head(root / name) for name in ("alpha", "beta")}

    rc, out = _cli("store", "migrate", "--json")
    assert rc == 0, f"store migrate must succeed: {out}"
    receipt = json.loads(out)
    assert receipt["alpha_head"] == alpha_head
    assert (root / ".grip" / "legacy-store.git").is_dir()
    assert not (root / ".grip" / ".git").exists()
    assert _git_out(root / ".grip" / "legacy-store.git", "log", "-1", "--format=%H") == alpha_head
    assert "migrated from alpha store" in _git_out(root, "log", "-1", "--format=%s")
    tree = _git_out(root, "ls-tree", "HEAD")
    for name, pin in pins.items():
        assert f"160000 commit {pin}\t{name}" in tree


# ---------------------------------------------------------------------------
# 18 — the multi-desk fixture: five agents with gr1 worktrees
# ---------------------------------------------------------------------------


def _checkout_at(path: Path, tmp_path: Path, name: str) -> Path:
    """A real clone of a fresh bare remote of `name`, placed AT `path`.

    Placed at the member's manifest PATH, not at its name: section 6c gap 2 measures 13 of
    25 repos whose name differs from their path, and "adopts by PATH" is only load-bearing
    when the two differ.
    """
    url = _bare_remote(tmp_path, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "clone", "-q", url, str(path)], capture_output=True, check=True)
    _git(path, "config", "user.email", "t@e.invalid")
    _git(path, "config", "user.name", "t")
    _git(path, "push", "-q", "origin", "main")
    return path


@pytest.fixture
def gr1_sibling_ws(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The section 6b/6c gr1 shape as the live manifests have it: a workspace root that is
    NOT a git repo, five units in `agents.toml` (one `worktree = "main"`, four worktrees as
    SIBLINGS beside the root), and existing checkouts at the members' manifest PATHS.

    The agents.toml shape is the live one: `[agents.<unit>]` with `worktree = "main"` for
    the root agent and one sibling directory per other desk.
    """
    root = tmp_path / "ws"
    root.mkdir(parents=True, exist_ok=True)
    units = {
        "opus": "main",
        "apollo": "synapt-dev",
        "sentinel": "synapt-global",
        "atlas": "synapt-codex",
        "fathom": "synapt-fathom",
    }
    gr1 = root / ".gitgrip"
    (gr1 / "spaces" / "main").mkdir(parents=True)
    (gr1 / "agents.toml").write_text(
        "".join(f'[agents.{u}]\nworktree = "{w}"\n\n' for u, w in units.items())
    )
    _checkout_at(root / "config", tmp_path, "root-config")
    for desk in ("synapt-dev", "synapt-global", "synapt-codex", "synapt-fathom"):
        _checkout_at(tmp_path / desk / "config", tmp_path, f"{desk}-config")
    # migrate-gr1 reads gr1's canonical manifest and its agents file from
    # .gitgrip.  Keeping the fixture's authority files in those locations is
    # essential: a root-level agents.toml would exercise a layout gr1 never
    # accepted, then mislabel its own refusal as a sibling-layout failure.
    (gr1 / "spaces" / "main" / "gripspace.yml").write_text(
        "repos:\n"
        "  config:\n"
        "    path: config\n"
        f"    url: {_git_out(root / 'config', 'remote', 'get-url', 'origin')}\n"
    )
    monkeypatch.chdir(root)
    assert Path.cwd() == root, f"the gr1 fixture must be the cwd, it is {Path.cwd()}"
    return root


def test_break_18_migrate_gr1_adopts_existing_checkouts_by_path(gr1_sibling_ws: Path) -> None:
    """Section 6b: `workspace migrate-gr1` then `store init` then `store commit` is the
    whole path. migrate-gr1 emits `../synapt-dev`, `../synapt-codex`, `../synapt-fathom`,
    `../synapt-global` and `"."`; apply ADOPTS the existing checkouts by PATH; zero clones;
    desk contents byte-identical before and after.

    Two readers landed on the same defect here: the first version called
    `store migrate --gr1`, a verb section 6b does not define, and its desks held a marker
    file each, so "adopts by path" and "zero clones" were untestable; `"." in out` matched
    almost any output. The desks now hold real checkouts at manifest paths whose NAMES
    differ, and the emitted paths are asserted one by one.

    MUTATION (per the design): restoring the hard-coded `agents/<unit>/home` turns this red.
    """
    root = gr1_sibling_ws
    # Only the SIBLING desks are asserted byte-identical: the root legitimately gains
    # .git, .grip and grip.toml, and section 6c's claim is about the desks.
    desks = [root.parent / d for d in
             ("synapt-dev", "synapt-global", "synapt-codex", "synapt-fathom")]
    before = {d: _listing(d) for d in desks}
    assert all(before[d] for d in desks), "each desk must hold a real checkout to start from"

    rc, out = _cli("workspace", "migrate-gr1")
    assert rc == 0, f"workspace migrate-gr1 must succeed here, got {rc}: {out[:300]}"

    spec = root / ".grip" / "workspace_spec.toml"
    assert spec.is_file(), "migrate-gr1 must emit .grip/workspace_spec.toml (section 5)"
    emitted = out + spec.read_text()
    for expected in (
        "../synapt-dev", "../synapt-codex", "../synapt-fathom", "../synapt-global"
    ):
        assert expected in emitted, (
            f"migrate-gr1 must emit {expected} for that unit's worktree rather than "
            f"agents/<unit>/home; got: {out[:300]}"
        )

    assert _cli("store", "init")[0] == 0
    _assert_init_ran(root)
    assert _cli("store", "commit", "-m", "gr1 adoption")[0] == 0
    _assert_flow_ran(root)

    for d, files in before.items():
        assert _listing(d) == files, (
            f"{d} must be byte-identical: adoption reads the existing checkout at its PATH "
            f"and never clones over it (section 6c item 6, zero clones)"
        )


# ---------------------------------------------------------------------------
# 19 — a member whose name differs from its path
#
# THE UNIT-HOME VARIANT CANNOT BE WRITTEN IN THIS SLICE, said here rather than left in a
# mark's reason: the row drives `store status` over a manifest carrying `[[units]]`, and
# `[[units]]` is beta, so the status verb is refused as beta before the name-versus-path
# resolution it tests is ever reached. The coordinate this row CAN measure today is the
# ROOT-level one, and the rule it pins -- that a member is resolved by PATH and its NAME is
# carried for reporting and never used as a lookup -- is the same rule at either coordinate.
# When `[[units]]` leaves beta this is the row to extend, and the extension is a fixture
# placement, not a new subject.
# ---------------------------------------------------------------------------


def test_break_19_member_found_at_its_path_not_its_name(two_member_ws: Path, tmp_path: Path) -> None:
    """Found at its PATH by `status`, `commit` and store resolution; a lookup by NAME finds
    nothing. Name and path are two coordinates and the resolution sites must use the path one.

    THE MARK CAME OFF BECAUSE THE ROW NOW RUNS, and it runs because two defects above the
    subject were removed rather than worked around. Its fixture moved the member's two
    coordinates with `text.replace('path = "alpha"', 'name = ...\\npath = ...')`, which INSERTS
    a second `name` key instead of renaming the first, so grip.toml failed `tomllib` with
    `Cannot overwrite a value` and `store status` exited 1 with an EMPTY payload -- the row
    died on its own setup and never reached an assertion. Renaming through `_set_member_name`
    (block-scoped, by path, like the two helpers above it) fixes that, and the row now asserts
    its own document parses before any verb runs. Second: `store status` measures a COMMITTED
    spec and exits 5 without one, so the fixture commits before asking.

    WHAT THE ROW CAN AND CANNOT SEE, named so neither half reads as the other. `store
    status --json` emits `name`, `pin`, `gitlink`, `head` and `state` and carries NO `path`,
    so the row asserts the observable CONSEQUENCE of path resolution -- the member reports
    `upstream`, meaning its checkout was found and its HEAD matched its pin -- rather than a
    `path` field the verb does not emit. A resolution that used the NAME would find no
    `renamed-member` checkout and could not report a state at all.
    """
    root = two_member_ws
    assert SPEC_SCHEMA_PATH.is_file(), (
        f"the member shape is defined by {SPEC_SCHEMA_REL} (design section 4); "
        f"until that file exists this row cannot build its fixture without guessing the format"
    )
    assert _cli("store", "init", str(root))[0] == 0
    _assert_init_ran(root)

    # NAME AND PATH ARE TWO COORDINATES, and the fixture makes them DISAGREE while leaving the
    # checkout exactly where the manifest says it is: the member is RENAMED and its path is
    # untouched, so a lookup by NAME must not satisfy a path lookup and the resolution sites
    # must use the path one.
    manifest = root / "grip.toml"
    manifest.write_text(_set_member_name(manifest.read_text(), "alpha", "renamed-member"))
    # THE FIXTURE'S OWN DOCUMENT MUST PARSE, asserted BEFORE the verb runs. The previous
    # version moved both coordinates with `text.replace('path = "alpha"', 'name = ...\\npath = ...')`,
    # which INSERTS a second `name` key into the block instead of renaming the first: grip.toml
    # then fails tomllib with `Cannot overwrite a value`, `store status` exits 1 with an EMPTY
    # payload, and the row dies on its own setup without reaching a single assertion above.
    parsed = tomllib.loads(manifest.read_text())
    assert [m["name"] for m in parsed["members"]] == ["renamed-member", "beta"], (
        f"the fixture's own document must parse AND carry the rename; got {parsed['members']}"
    )

    # A ROOT COMMIT IS A PRECONDITION OF THE VERB, not decoration: `store status` measures a
    # committed spec, and with none it exits 5 naming "no root commit yet; run store commit".
    # The invalid grip.toml masked this -- the row died on its own setup one step earlier.
    assert _cli("store", "commit", "-m", "store the renamed fixture")[0] == 0

    rc, out = _cli("store", "status", "--json")
    assert rc == 0, out
    # PARSED, not a substring heuristic: the row asserts on the members list, and
    # separately that a lookup by NAME resolves to nothing.
    try:
        data = json.loads(out)
    except json.JSONDecodeError as exc:
        # ASSERTED FIRST. A JSONDecodeError is not an AssertionError, so it would
        # surface as a harness defect rather than as "the verb is unbuilt" -- and the
        # row's own subject is a PATH lookup, which it can only measure if the payload
        # parsed. Failing here names the precondition instead of the parser.
        raise AssertionError(
            f"`store status --json` must emit JSON; got {out[:200]!r}"
        ) from exc
    members = data.get("members") or data.get("member") or []
    names = {m.get("name") for m in members if isinstance(m, dict)}

    # THE NAME IS CARRIED FOR REPORTING. It is what a reader sees, and it is NOT what the
    # member is found by.
    assert names == {"renamed-member", "beta"}, f"the NAME must be carried: got {names}"

    # RESOLUTION USED THE PATH, and this is its observable consequence rather than a claim
    # about a field. `alpha` is the only place this member's checkout exists, so a member
    # reported `upstream` was resolved from there: its HEAD was read and matched its pin. A
    # resolution that used the NAME would find no `renamed-member` checkout and could not
    # report a state at all.
    #
    # WHAT THIS ROW CANNOT ASSERT, named so its absence does not read as an oversight:
    # `store status --json` emits `name`, `pin`, `gitlink`, `head` and `state` and carries
    # NO `path`, so there is no path field for a consumer to look up. The path is real --
    # members are derived from the root's gitlink tree entries, which ARE paths -- it is
    # simply not reported. Asserting on a `path` key would be asserting on a field the verb
    # does not emit.
    renamed = next(
        (m for m in members if isinstance(m, dict) and m.get("name") == "renamed-member"), None
    )
    assert renamed is not None, f"the renamed member must be listed; got {members}"
    assert renamed.get("state") == "upstream", (
        f"the member must resolve from the checkout at its PATH, not its name: got {renamed}"
    )
    assert renamed.get("head") == renamed.get("pin"), (
        f"resolved from the checkout at its path, its HEAD matches its pin: got {renamed}"
    )

    # CONTROL -- the falsifiable half, and the reason the assertion above is evidence rather
    # than a description. Give the NAME a directory of its own. Nothing resolves by name
    # today, so the members list must be byte-identical; a site that resolved by name would
    # change it. Without this, the row would also pass if the state field were a constant.
    (root / "renamed-member").mkdir(exist_ok=True)
    rc2, out2 = _cli("store", "status", "--json")
    assert rc2 == 0, out2
    after = json.loads(out2).get("members")
    assert after == members, (
        "a directory named after the member must not change resolution: "
        f"{after} != {members}"
    )
    # THE PREMISE, pinned falsifiably. The line that stood here asked whether
    # `"renamed-member"` was absent from `{m.get("path") for m in members}` -- and that set
    # is `{None}`, because the verb emits no `path`, so the assertion could not fail and a
    # reader found a check where nothing was checked (measured: PATHS SET {None}, member
    # keys ['gitlink','head','name','pin','state']).
    #
    # What is worth pinning is the premise itself: there is no `path` field, which is why
    # the consequence above is read off `state` and `head`. This reddens the day the
    # payload gains one -- the day this row could assert directly -- and it cannot pass
    # vacuously, because the name set is pinned exactly two lines up.
    assert members and all(isinstance(m, dict) and "path" not in m for m in members), (
        "`store status --json` carries no path today; if it gains one, read this row's "
        f"consequence off it directly: got {members}"
    )


# ---------------------------------------------------------------------------
# 21 — a tracked, non-member root folder (section 12, "for section 8")
# ---------------------------------------------------------------------------


def test_break_21_tracked_non_member_root_folder_is_untouched(two_member_ws: Path) -> None:
    """An explicitly included, tracked non-member root folder stays byte-identical.

    Root inclusion is declared in .gitinclude. Tracking content with force-add does
    not grant native commit authority over an undeclared path.
    """
    root = two_member_ws
    tracked = root / "config"
    (tracked / "nested").mkdir(parents=True)
    (tracked / "nested" / "notes.md").write_text("notes, not a workspace spec\n")

    assert _cli("store", "init")[0] == 0
    _assert_init_ran(root)
    # AFTER init, never before: section 3 is what makes the root a git repo, so a git
    # config against it earlier fails with exit 128 and the row dies in its own setup.
    _git(root, "config", "user.email", "t@e.invalid")
    _git(root, "config", "user.name", "t")
    # Declare root content through the owning policy, then prove it is tracked
    # with ordinary Git staging rather than bypassing that policy with force-add.
    from gr2.python_cli import grip_cli

    with (root / ".gitinclude").open("a") as declaration:
        declaration.write("config/\n")
    grip_cli._regenerate_workspace_gitignore(root)
    _git(root, "add", "config")
    _git(root, "commit", "-q", "-m", "track config/ so the claim is real")
    assert _git_out(root, "ls-files", "config"), (
        "the fixture must actually track config/, or byte-identity proves nothing"
    )
    assert _cli("store", "commit", "-m", "first")[0] == 0
    before = _listing(tracked)
    assert before, "the fixture must plant real files, or byte-identity proves nothing"

    for verb in (["store", "status"], ["store", "check"], ["store", "commit", "-m", "again"]):
        rc, out = _cli(*verb, "--json")
        named = rc in (3, 4) and "config" in out
        assert not named, (
            f"{' '.join(verb)} refused (rc={rc}) and named the tracked non-member folder: {out}"
        )
    assert _listing(tracked) == before, (
        "a tracked non-member root folder must be byte-identical after every store verb"
    )


# ---------------------------------------------------------------------------
# 20 — a checkout at the member's NAME, with nothing at its declared PATH
#
# THE SHAPE THE DESIGN NAMES AND THE VERB DOES NOT YET SERVE. Section 6's contract for
# attempt 20 is that the member is FOUND at the name, with "exactly one line printed naming
# both locations; zero clones; bytes unchanged". MEASURED on that shape (a two-member store,
# the member renamed, its checkout moved from `alpha/` to `renamed-member/` so the declared
# path is empty) -- all three verbs refuse with exit 5 and name the PATH only:
#
#     store status   rc=5   state "cannot-measure", head null
#     store check    rc=5   "renamed-member cannot fetch origin"
#     store commit   rc=5   "renamed-member path alpha is not a checkout; materialize it first"
#
# So the gap is not that the verb is wrong ABOUT the member -- it identifies `renamed-member`
# correctly by name in all three. It is that no site looks at the NAME coordinate at all, so
# a reader who has moved a checkout is told to materialize a path instead of being told where
# the checkout already is. That is the same pair of coordinates row 19 pins from the other
# side, and it is why row 19's re-aim could land green while this one cannot: 19 asks the verb
# to prefer the PATH, and 20 asks it to have a second place to look.
#
# LANDED -- the marker came off in the same change that made the row pass, which is what
# `strict=True` forced: the day the verbs land the row XPASSes, and a strict xpass is a
# FAILURE, so the marker could not be left on. `_native_member_working_root` in
# `grip_cli` resolves a member by PATH first and falls back to the NAME only when nothing
# is at the path, so every root whose two coordinates agree resolves exactly as before and
# no verb can read a different member than it did. The PATH keeps the gitlink read: that
# key is the root snapshot's, not the working copy's.
# ---------------------------------------------------------------------------


def test_break_20_a_checkout_at_the_name_is_found_and_both_locations_are_named(
    two_member_ws: Path,
) -> None:
    """Section 6: a home holding a checkout at `<home>/<name>`, with nothing at the path.

    The contract is a FINDING and a SENTENCE rather than a refusal -- "found at the name;
    exactly one line printed naming both locations; zero clones; bytes unchanged" -- so the
    row asserts the finding, both names in the message, and that nothing was written.
    """
    root = two_member_ws
    assert _cli("store", "init", str(root))[0] == 0
    _assert_init_ran(root)
    manifest = root / "grip.toml"
    manifest.write_text(_set_member_name(manifest.read_text(), "alpha", "renamed-member"))
    assert _cli("store", "commit", "-m", "store the renamed fixture")[0] == 0

    # THE NAME-PLACED SHAPE: the checkout moves to the NAME and the declared path goes empty.
    (root / "alpha").rename(root / "renamed-member")
    assert not (root / "alpha").exists(), "the declared path must be EMPTY for this shape"
    placed = _listing(root / "renamed-member")
    assert placed, "the fixture must plant a real checkout at the name, or byte-identity is empty"

    rc, out = _cli("store", "status", "--json")
    assert rc == 0, f"a checkout at the name must be FOUND, not refused: rc={rc} {out[:200]}"

    # THE SENTENCE, and both coordinates in it. A message naming only the path is the
    # current refusal; a message naming only the name leaves the reader unable to find the
    # declaration that disagrees with it.
    assert "renamed-member" in out, f"the message must name where the checkout IS: {out[:200]}"
    assert "alpha" in out, f"the message must name the declaration that disagrees: {out[:200]}"

    # ZERO CLONES and bytes unchanged: the verb found the checkout, so it must not write one.
    assert not (root / "alpha").exists(), "a checkout must not be materialized at the empty path"
    assert _listing(root / "renamed-member") == placed, (
        "the checkout found at the name must be byte-identical afterwards"
    )


def test_break_20b_a_checkout_at_both_coordinates_reads_the_PATH(
    two_member_ws: Path,
) -> None:
    """The PRECEDENCE half of attempt 20, and the only shape that can see it.

    Row 20 plants a checkout at the NAME with nothing at the path; row 19 plants one at the
    PATH with nothing at the name. In BOTH, exactly one coordinate holds a checkout, so a
    resolver that preferred either one would pass both rows. `_native_member_working_root`
    claims PATH first with NAME only as a fallback, and that claim is observable only when
    both coordinates hold a checkout at DIFFERENT commits. MEASURED, which is why this row
    exists: removing the path preference entirely (`if _is_member_checkout(root,
    member["path"]):` -> `if False:`) leaves rows 19 and 20 GREEN, so nothing in the file
    could see the precedence at all.

    The discriminator is the member's reported `head`. The two checkouts are at different
    commits, so which one was read is visible in the payload, and the row asserts the FIXTURE
    disagrees before it asserts the verb does -- a fixture whose two coordinates share a
    commit would satisfy either answer and could not fail.
    """
    root = two_member_ws
    assert _cli("store", "init", str(root))[0] == 0
    _assert_init_ran(root)
    manifest = root / "grip.toml"
    manifest.write_text(_set_member_name(manifest.read_text(), "alpha", "renamed-member"))
    assert _cli("store", "commit", "-m", "store the renamed fixture")[0] == 0

    # THE BOTH-PRESENT SHAPE: a second real checkout at the NAME, at a DIFFERENT commit.
    # `alpha` stays exactly where the manifest declares it, so the path is the live one and
    # the name-placed copy is the thing a NAME-first resolver would read by mistake.
    at_name = root / "renamed-member"
    shutil.copytree(root / "alpha", at_name)
    _git(at_name, "config", "user.email", "t@e.invalid")
    _git(at_name, "config", "user.name", "t")
    _git(at_name, "commit", "-q", "--allow-empty", "-m", "the name-placed copy moved on")

    path_head = _head(root / "alpha")
    name_head = _head(at_name)
    # THE ROW'S OWN CONTROL, asserted FIRST: if both coordinates were at one commit then a
    # report of either HEAD would satisfy the assertion below and this row could not fail.
    # A fixture that cannot disagree is the defect here, not the verb.
    assert path_head != name_head, (
        f"the fixture must plant the two checkouts at DIFFERENT commits; both read {path_head}"
    )

    # STDOUT AND STDERR KEPT APART, and this is load-bearing rather than tidy. `_cli`
    # concatenates the two channels, and the name-placed NOTE is written to stderr -- so
    # under a NAME-first resolver the concatenated form raises JSONDecodeError and the row
    # reddens on a parse error instead of on the precedence it exists to pin. MEASURED on
    # the mutant: `JSONDecodeError: Extra data: line 2 column 1`, never reaching the assert
    # below. A row that reddens for the wrong reason is a false red with a true-looking
    # colour, so the assertion that is supposed to fail is the one that has to fail first.
    result = make_cli_runner().invoke(app, ["store", "status", "--json"])
    assert result.exit_code == 0, (result.stdout or "") + (result.stderr or "")
    data = json.loads(result.stdout or "")
    members = data.get("members") or data.get("member") or []
    renamed = next(
        (m for m in members if isinstance(m, dict) and m.get("name") == "renamed-member"), None
    )
    assert renamed is not None, f"the renamed member must be listed; got {members}"
    assert renamed.get("head") == path_head, (
        "the PATH holds a checkout, so the PATH is the one read: the NAME is a fallback, never "
        f"a preference -- reported head {renamed.get('head')}, path {path_head}, name {name_head}"
    )

    # AND THE BRANCH TAKEN, pinned directly rather than only through its consequence: the
    # note is emitted only where the NAME is read as a fallback, so its ABSENCE on this
    # shape is the precedence restated in one line. Independent of `head`, which means a
    # resolver that read the path but still announced a fallback cannot pass both.
    assert "is checked out at" not in (result.stderr or ""), (
        "the NAME fallback must not fire while the PATH holds a checkout; "
        f"stderr was {result.stderr!r}"
    )


@pytest.mark.parametrize(
    "escaping_name",
    ["ABSOLUTE", "../outside"],
    ids=["absolute", "dotdot"],
)
def test_break_20c_a_name_that_escapes_the_root_is_never_read(
    two_member_ws: Path, tmp_path: Path, escaping_name: str
) -> None:
    """The NAME is a PATH COORDINATE, so it takes the SAME containment check the declared path takes.

    THE ESCAPE THIS CLOSES, and the parent refuses it for `path`: with the declared path empty, a
    member whose NAME is absolute or carries `..` made the fallback read a checkout OUTSIDE the
    root -- its HEAD in `store status`, and `store commit` running git inside it. A fallback that
    joined the name raw reintroduced, through the other coordinate, exactly the shape
    `_normalise_member_path` exists to refuse.

    TWO ARMS BECAUSE THEY FAIL DIFFERENTLY: the absolute form is caught by the `is_absolute` test,
    the `..` form by the `parts` test. One arm would leave the other branch unwitnessed.
    """
    root = two_member_ws
    assert _cli("store", "init", str(root))[0] == 0
    _assert_init_ran(root)

    # A REAL member-shaped checkout OUTSIDE the root, at a HEAD of its own, so that a read of it
    # shows up in the payload rather than being merely plausible.
    outside = tmp_path / "outside"
    shutil.copytree(root / "alpha", outside)
    _git(outside, "config", "user.email", "t@e.invalid")
    _git(outside, "config", "user.name", "t")
    _git(outside, "commit", "-q", "--allow-empty", "-m", "the outside checkout moved on")
    outside_head = _head(outside)
    assert outside_head != _head(root / "alpha"), (
        "the fixture's outside checkout must differ from the in-root one, or a wrong read is invisible"
    )

    name_value = str(outside) if escaping_name == "ABSOLUTE" else "../outside"
    manifest = root / "grip.toml"
    manifest.write_text(_set_member_name(manifest.read_text(), "alpha", name_value))
    assert _cli("store", "commit", "-m", "store the escaping-name fixture")[0] == 0

    # EMPTY THE DECLARED PATH, so the NAME fallback is the only route that could be taken at all.
    (root / "alpha").rename(root / "alpha-moved-away")
    assert not (root / "alpha").exists(), "the declared path must be empty for this shape"

    rc, out = _cli("store", "status", "--json")
    assert outside_head not in out, (
        "the NAME was joined raw, so the verb read a checkout OUTSIDE the root "
        f"({name_value!r} -> {outside_head}): rc={rc} {out[:300]}"
    )
    # AND IT MUST NOT HAVE SUCCEEDED BY READING IT. A refusal is the expected outcome; a success
    # that names the outside HEAD is the defect, and this catches the second route to it.
    if rc == 0:
        data = json.loads(out)
        for row in data.get("members") or []:
            assert row.get("head") != outside_head, row


def test_break_20d_a_name_colliding_with_another_member_is_never_read_or_pinned(
    two_member_ws: Path,
) -> None:
    """A NAME that resolves to ANOTHER member's checkout must not be read, and must not be PINNED.

    THE SHAPE, from Apollo's r2 on this lane: member `alpha` is RENAMED to `beta` while a member
    named `beta` exists, and alpha's own declared path is empty. The NAME fallback then resolves
    alpha's name coordinate to `beta`'s checkout -- a directory belonging to a DIFFERENT member --
    so alpha is measured, and pinned, from beta.

    THE PARENT REFUSES THIS SHAPE, so the fallback INTRODUCED it, and the two consequences are not
    equally bad. `store status` reported beta's HEAD on alpha's row (a wrong ANSWER), and
    `store commit` rewrote alpha's gitlink pin to beta's sha at rc 0 (a wrong BYTE in the root
    snapshot). That is why this row asserts the pin is byte-identical rather than asserting an exit
    code: which code the verbs choose is the author's call, which bytes land is not.
    """
    root = two_member_ws
    assert _cli("store", "init", str(root))[0] == 0
    _assert_init_ran(root)

    # MAKE THE TWO MEMBERS' HEADS DISAGREE ON PURPOSE, and push so coverage still holds. If they
    # agreed, reading the wrong checkout would be invisible and this row would pass for exactly
    # the reason it exists. Asserted rather than assumed.
    _git(root / "beta", "commit", "-q", "--allow-empty", "-m", "beta moves on")
    _git(root / "beta", "push", "-q", "origin", "main")
    beta_head = _head(root / "beta")
    assert beta_head != _head(root / "alpha"), (
        "the fixture must make the two members' HEADs differ, or a wrong read is invisible"
    )

    manifest = root / "grip.toml"
    manifest.write_text(_set_member_name(manifest.read_text(), "alpha", "beta"))
    parsed = tomllib.loads(manifest.read_text())
    assert [m["name"] for m in parsed["members"]] == ["beta", "beta"], (
        f"the fixture's own document must carry the collision; got {parsed['members']}"
    )
    # This commit is taken BEFORE the declared path is emptied, so the PATH still wins and the
    # pin recorded here is alpha's own. Only the second commit can reach the fallback.
    assert _cli("store", "commit", "-m", "record the colliding name")[0] == 0

    def alpha_pin() -> str | None:
        fields = _git_out(root, "ls-tree", "HEAD", "--", "alpha").split()
        return fields[2] if len(fields) >= 3 else None

    (root / "alpha").rename(root / "alpha-moved-away")
    assert not (root / "alpha").exists(), "the declared path must be empty for this shape"

    pin_before = alpha_pin()
    assert pin_before is not None, (
        "alpha's gitlink must be in the root snapshot, or the byte comparison below is vacuous"
    )

    # STDOUT ONLY. `_cli` concatenates stdout and stderr, and the name-placed note goes to stderr,
    # so the payload arrives followed by prose and `json.loads` on the concatenation raises. Row
    # 20b hit this same trap; the runner is used directly so the two channels stay apart.
    result = make_cli_runner().invoke(app, ["store", "status", "--json"])
    rc = result.exit_code
    try:
        payload = json.loads(result.stdout or "")
    except json.JSONDecodeError as exc:
        # NOT a silent skip. My first version wrote `except JSONDecodeError: rows = []`, which let
        # this whole arm become vacuous while the row still reached the PIN assertion below -- so
        # the row reported a defect and its read-side arm had contributed nothing. A guard whose
        # subject is absent must say so, not pass.
        raise AssertionError(
            f"`store status --json` must emit a payload on stdout for this shape: "
            f"rc={rc} stdout={(result.stdout or '')[:300]!r} stderr={(result.stderr or '')[:200]!r}"
        ) from exc
    rows = payload.get("members") or []
    assert rows, f"`store status --json` emitted no member rows: {(result.stdout or '')[:300]!r}"
    heads = [row.get("head") for row in rows]
    # SCOPED TO A COUNT, not `beta_head not in out`: beta's OWN row must report beta_head, so a
    # whole-payload membership test would fail on the correct behaviour. Alpha's checkout is gone,
    # so exactly one row may carry any head and exactly one must be unmeasurable.
    assert heads.count(beta_head) == 1, (
        f"beta's HEAD ({beta_head}) appears on {heads.count(beta_head)} row(s); alpha's "
        f"checkout is gone, so only beta may report it: {heads}"
    )
    assert heads.count(None) == 1, (
        f"alpha's checkout is gone, so exactly one row must be unmeasurable: {heads}"
    )

    rc, out = _cli("store", "commit", "-m", "must not rewrite alpha's pin")
    pin_after = alpha_pin()
    assert pin_after == pin_before, (
        f"the fallback PINNED another member's sha onto alpha: {pin_before} -> {pin_after} "
        f"(rc={rc} {out[:200]})"
    )


def test_break_20e_another_members_NAME_coordinate_is_claimed_without_any_rename(
    two_member_ws: Path,
) -> None:
    """A member whose checkout sits at its NAME claims THAT directory, not merely its declared path.

    THE HOLE THIS CLOSES, found by a third reader attacking the containment predicate itself, and
    it is NOT the rename case: a member may sit at its NAME coordinate -- the alpha-era placement
    this whole fallback exists for -- while its DECLARED path holds nothing. A claim set built from
    declared paths alone then misses the directory that member actually occupies, so another member
    whose name lands there finds it UNCLAIMED and reads it, with no rename of a declared path
    anywhere in the picture. Measured before the fix: `beta` declaring `beta-declared` while its
    checkout sits at `beta`, and `alpha` renamed to `beta`, put beta's HEAD on BOTH rows.

    **THE OUTCOME IS A REFUSAL FOR BOTH MEMBERS, and that is the correct answer rather than a
    harsh one.** With two members named `beta`, the directory `beta` cannot be attributed to
    either; the fallback must not guess, so both fall back to a declared path that holds nothing
    and both are unmeasurable. The row asserts the REFUSAL and the BYTES, because those are the two
    things a containment fix owes: a wrong read cannot happen, and a pin cannot be rewritten.
    """
    root = two_member_ws
    assert _cli("store", "init", str(root))[0] == 0
    _assert_init_ran(root)

    # The disagreement is forced, so a wrong read could not hide behind two members that agree.
    _git(root / "beta", "commit", "-q", "--allow-empty", "-m", "beta moves on")
    _git(root / "beta", "push", "-q", "origin", "main")
    beta_head = _head(root / "beta")
    assert beta_head != _head(root / "alpha"), (
        "the fixture must make the two members' HEADs differ, or a wrong read is invisible"
    )

    # beta DECLARES a path with nothing at it and keeps its checkout at its NAME coordinate.
    manifest = root / "grip.toml"
    blocks = manifest.read_text().split("[[members]]")
    for i, block in enumerate(blocks):
        if i and 'path = "beta"' in block:
            blocks[i] = block.replace('path = "beta"', 'path = "beta-declared"', 1)
    manifest.write_text("[[members]]".join(blocks))
    assert 'path = "beta-declared"' in manifest.read_text(), "fixture: beta's declared path must move"
    assert (root / "beta").is_dir(), "fixture: beta's checkout must STAY at its NAME coordinate"

    # The new declared gitlink coordinate also needs root inclusion authority.
    from gr2.python_cli import grip_cli

    with (root / ".gitinclude").open("a") as declaration:
        declaration.write("beta-declared/\n")
    grip_cli._regenerate_workspace_gitignore(root)
    # Committed BEFORE the collision exists, so this step proves beta resolves through its NAME.
    rc, out = _cli("store", "commit", "-m", "record the name-placed fixture")
    assert rc == 0, out

    def pin_of(declared_path: str) -> str | None:
        fields = _git_out(root, "ls-tree", "HEAD", "--", declared_path).split()
        return fields[2] if len(fields) >= 3 else None

    alpha_pin, beta_pin = pin_of("alpha"), pin_of("beta-declared")
    assert alpha_pin is not None and beta_pin is not None, (
        f"both gitlinks must be in the snapshot or the byte comparison is vacuous: "
        f"alpha={alpha_pin} beta={beta_pin}"
    )

    # NOW the collision, and alpha's own declared path is emptied.
    manifest.write_text(_set_member_name(manifest.read_text(), "alpha", "beta"))
    (root / "alpha").rename(root / "alpha-moved-away")
    assert not (root / "alpha").exists(), "alpha's declared path must be empty for this shape"

    rc, out = _cli("store", "commit", "-m", "must refuse, not attribute")
    assert rc != 0, (
        f"the collision was ATTRIBUTED rather than refused: rc={rc} {out[:250]}"
    )
    assert pin_of("alpha") == alpha_pin, "alpha's pin moved under a refused commit"
    assert pin_of("beta-declared") == beta_pin, "beta's pin moved under a refused commit"


def test_break_20f_a_name_landing_INSIDE_another_members_tree_is_refused(
    two_member_ws: Path, tmp_path: Path
) -> None:
    """A name landing INSIDE another member's tree is refused, not only one landing ON a coordinate.

    THE SHAPE, from a reader's BLOCK on the two-coordinate fix: `alpha.name = "beta/nested"` with
    `<root>/beta/nested` a real checkout. Exact membership closes "lands ON another member's
    coordinate" and **cannot** close "lands INSIDE one", because `beta/nested` equals no member's
    coordinate -- it is BELOW one. Measured before the fix: alpha's gitlink pin was rewritten to
    the NESTED checkout's HEAD at **rc 0**, the same wrong-pin severity as the direct collision.

    **⚠ THE NESTED CHECKOUT GETS A REAL ORIGIN, AND THAT IS THE LOAD-BEARING PART OF THE FIXTURE.**
    With NO remote on it, the wrong read is stopped anyway -- by §5a's coverage fetch
    (`cannot fetch origin`), **not** by containment. A refusal arriving from a DIFFERENT guard looks
    exactly like this predicate working, so a fixture without an origin proves nothing about this
    guard. A vendored clone has an origin by construction, and its own branch trivially covers its
    own HEAD.
    """
    root = two_member_ws
    assert _cli("store", "init", str(root))[0] == 0
    _assert_init_ran(root)

    url = _bare_remote(tmp_path, "nested")
    nested = root / "beta" / "nested"
    subprocess.run(["git", "clone", "-q", url, str(nested)], capture_output=True, check=True)
    _git(nested, "config", "user.email", "t@e.invalid")
    _git(nested, "config", "user.name", "t")
    _git(nested, "commit", "-q", "--allow-empty", "-m", "nested moves on")
    _git(nested, "push", "-q", "origin", "main")
    nested_head = _head(nested)

    # The vendored clone is UNTRACKED inside beta, and `store commit` refuses a dirty member (rc 3)
    # before any resolver runs. Ignore it -- AND PUSH IT, or §5a refuses beta's own pin as
    # uncovered (rc 3, "not on origin/main"), which is a third refusal that has nothing to do with
    # the guard under test. Both were hit while building this row.
    (root / "beta" / ".gitignore").write_text("nested/\n")
    _git(root / "beta", "add", ".gitignore")
    _git(root / "beta", "commit", "-q", "-m", "ignore the vendored clone")
    _git(root / "beta", "push", "-q", "origin", "main")

    manifest = root / "grip.toml"
    manifest.write_text(_set_member_name(manifest.read_text(), "alpha", "beta/nested"))
    assert _cli("store", "commit", "-m", "record the nested-name fixture")[0] == 0

    def alpha_pin() -> str | None:
        fields = _git_out(root, "ls-tree", "HEAD", "--", "alpha").split()
        return fields[2] if len(fields) >= 3 else None

    pin_before = alpha_pin()
    assert pin_before is not None, (
        "alpha's gitlink must be in the snapshot, or the byte comparison below is vacuous"
    )
    assert nested_head != pin_before, (
        "the fixture must make the nested checkout's HEAD differ from alpha's pin, or adopting it "
        "would be invisible"
    )

    (root / "alpha").rename(root / "alpha-moved-away")
    assert not (root / "alpha").exists(), "alpha's declared path must be empty for this shape"

    rc, out = _cli("store", "commit", "-m", "must not adopt the nested checkout")
    assert alpha_pin() == pin_before, (
        f"alpha's pin was rewritten to the NESTED checkout's HEAD ({nested_head}): "
        f"{pin_before} -> {alpha_pin()} (rc={rc} {out[:200]})"
    )


# ---------------------------------------------------------------------------
# BETA ROWS — 02, 06, 09. `xfail(strict=True)` per section 8: "so the day beta lands
# they are forced to turn green, and not only to exist." strict=True is the load-bearing
# half — without it, the day beta lands the row silently passes and nothing looks.
# ---------------------------------------------------------------------------


@pytest.mark.xfail(strict=True, raises=AssertionError, reason="beta: gc of covering refs")
def test_break_02_gc_of_covering_refs(two_member_ws: Path) -> None:
    """A `git gc` that prunes a ref which was covering the pin. Beta: the slice does not
    implement the gc-refusal, so the outcome is unspecified today and the row is forced
    green when it lands."""
    raise AssertionError("beta: gc of covering refs is not implemented in the slice")


@pytest.mark.xfail(strict=True, raises=AssertionError, reason="beta: overlays")
def test_break_06_overlays(two_member_ws: Path) -> None:
    """`overlay` in `grip.toml` is a beta field (see attempt 11, which asserts the
    REFUSAL). This row is about the behaviour once overlays exist."""
    raise AssertionError("beta: overlays are not implemented in the slice")


@pytest.mark.xfail(strict=True, raises=AssertionError, reason="beta: nested coverage")
def test_break_09_nested_coverage(two_member_ws: Path) -> None:
    """A member whose coverage is satisfied only through another member's history.
    Beta: the slice checks coverage per member against its own upstream."""
    raise AssertionError("beta: nested coverage is not implemented in the slice")
