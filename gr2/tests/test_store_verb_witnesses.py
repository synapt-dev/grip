"""Per-verb witnesses for the `store` group: JSON shape, exit code, and the mutation.

Builder step 3 asks for "the `store` verbs of section 5, each with `--json` and the exit
table". This file is the witness half of that: one row per verb, each asserting
(a) the JSON the verb prints, (b) its exit code, and (c) where the verb makes a claim about
work it did, the mutation that claim rests on -- asserted to have landed, with a control on
the same instrument.

⚠ THREE VERBS ARE NOT THE SECTION-5 VERBS YET, AND THE ROWS SAY SO RATHER THAN PASSING.
`snapshot`, `diff` and `checkout` are still the alpha-store verbs: each takes a positional
`workspace_root` and calls into the `.grip` store (`grip_mod.grip_init` / `grip_snapshot`).
The design's section 5 row for them is explicit -- "`log` is the root's `git log` with pins
per commit; `diff` is pin changes between two root commits; `checkout` is `materialize` at a
commit", and "`store snapshot` [is a] hidden alias of `store commit`, removed at beta".
`store log` IS ported (its JSON is the native shape). The three rows below are
`xfail(strict=True)`, so the day each verb is ported its row turns XPASS(strict) -- a
FAILURE -- and forces the row to be rewritten rather than silently satisfied.

Measured 2026-09-28, this is what each returns today when called the way section 5 says:
  store diff HEAD~1 HEAD --json   -> exit 2, "Missing argument 'ref_b'" (it wants
                                     {workspace_root} {ref_a} {ref_b})
  store checkout HEAD~1 --json    -> exit 2, "Missing argument 'ref'"
  store snapshot -m x --json      -> exit 2, "Missing argument 'workspace_root'"

Premium boundary: OSS (grip). Local workspace orchestration over git; no identity, org, or
entitlement semantics.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from gr2.python_cli.app import app

from tests.conftest import make_cli_runner


def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, text=True, capture_output=True, check=check)


def _git_out(cwd: Path, *args: str, check: bool = True) -> str:
    return _git(cwd, *args, check=check).stdout.strip()


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


def _cli(*args: str) -> tuple[int, str]:
    result = make_cli_runner().invoke(app, list(args))
    return result.exit_code, (result.stdout or "") + (result.stderr or "")


def _ls_remote(url: str, ref: str) -> str:
    out = subprocess.run(["git", "ls-remote", url, ref], text=True, capture_output=True).stdout.strip()
    return out.split("\t")[0] if out else ""


@pytest.fixture
def ws(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "ws"
    root.mkdir()
    for name in ("alpha", "beta"):
        remote = _bare_remote(tmp_path, name)
        dest = root / name
        subprocess.run(["git", "clone", "-q", str(remote), str(dest)], capture_output=True, check=True)
        _git(dest, "config", "user.email", "t@e.invalid")
        _git(dest, "config", "user.name", "t")
    monkeypatch.chdir(root)
    return root


# ---------------------------------------------------------------------------
# init
# ---------------------------------------------------------------------------


def test_init_json_shape_idempotence_and_the_no_path_form(
    ws: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--json` names its path, the second call is a no-op, and the cwd form works.

    The no-path form is the one a user types inside the workspace, and it is the form every
    OTHER verb defaults to. If it silently initialised somewhere else, the root a user is
    standing in would stay uninitialised while the verb reported success.
    """
    rc, out = _cli("store", "init", str(ws), "--json")
    assert rc == 0, out
    payload = json.loads(out)
    assert payload == {"status": "initialized", "path": str(ws.resolve()), "store": "native"}, out

    repo_head = _git_out(ws, "rev-parse", "HEAD", check=False)
    rc, out = _cli("store", "init", str(ws), "--json")
    assert rc == 0, out
    assert json.loads(out)["status"] == "initialized", out

    # the cwd form: a fresh root, no positional path at all
    other = ws.parent / "other"
    other.mkdir()
    subprocess.run(
        ["git", "clone", "-q", str(ws.parent / "alpha.git"), str(other / "alpha")],
        capture_output=True,
        check=True,
    )
    monkeypatch.chdir(other)
    rc, out = _cli("store", "init", "--json")
    assert rc == 0, out
    assert json.loads(out)["path"] == str(other.resolve()), out
    assert (other / "grip.toml").is_file()
    assert repo_head == _git_out(ws, "rev-parse", "HEAD", check=False), "the first root must not move"


def test_init_pins_the_root_branch_against_the_hosts_config(
    ws: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The root lands on `main` even where a bare `git init` would give `master`.

    THE MUTATION IS ASSERTED, NOT ASSUMED: the control below runs a bare `git init` under
    the same ambient config and requires it to come out on `master`. Without that, a host
    that already defaults to `main` would make this row pass while proving nothing -- the
    shape of the defect it exists to catch (break_10 could never run for exactly this
    reason).
    """
    ambient = {
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": str(tmp_path / "empty-global"),
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "init.defaultBranch",
        "GIT_CONFIG_VALUE_0": "master",
    }
    (tmp_path / "empty-global").write_text("")
    for key, value in ambient.items():
        monkeypatch.setenv(key, value)

    control = tmp_path / "control"
    control.mkdir()
    _git(control, "init", "-q")
    assert _git_out(control, "symbolic-ref", "--short", "HEAD") == "master", (
        "the ambient config must actually be in force, or this row measures nothing"
    )

    root = ws.parent / "root-under-master-ambient"
    root.mkdir()
    subprocess.run(
        ["git", "clone", "-q", str(ws.parent / "alpha.git"), str(root / "alpha")],
        capture_output=True,
        check=True,
    )
    monkeypatch.chdir(root)
    rc, out = _cli("store", "init", "--json")
    assert rc == 0, out
    assert _git_out(root, "symbolic-ref", "--short", "HEAD") == "main", (
        "the root branch must be a property of the verb, never of the host's config"
    )


# ---------------------------------------------------------------------------
# check / push
# ---------------------------------------------------------------------------


def test_check_json_names_every_member_and_its_exit_is_zero(ws: Path) -> None:
    assert _cli("store", "init", str(ws))[0] == 0
    assert _cli("store", "commit", "-m", "first")[0] == 0

    rc, out = _cli("store", "check", "--json")
    assert rc == 0, out
    payload = json.loads(out)
    assert payload["status"] == "checked", out
    assert {member["name"] for member in payload["members"]} == {"alpha", "beta"}, out
    for member in payload["members"]:
        assert member["state"] == "upstream", member
        assert member["pin"] == _git_out(ws / member["name"], "rev-parse", "HEAD"), member
        assert member["upstream"] == "origin/main", member


def test_push_publishes_the_root_ref_and_refuses_on_coverage(ws: Path, tmp_path: Path) -> None:
    """The push half, with the remote ref as the fruit and a control on the same instrument.

    An `ls-remote` that cannot see refs at all reports the safe-looking answer, so the
    covered case is asserted PRESENT before the refusal case asserts ABSENT.
    """
    root_remote = tmp_path / "root.git"
    subprocess.run(["git", "init", "-q", "-b", "main", "--bare", str(root_remote)], check=True)
    # ORDER: the root is not a repo until the verb makes it one, so the origin is set after.
    assert _cli("store", "init", str(ws))[0] == 0
    _git(ws, "remote", "add", "origin", str(root_remote.as_uri()))
    assert _cli("store", "commit", "-m", "first")[0] == 0

    rc, out = _cli("store", "push", "--json")
    assert rc == 0, out
    payload = json.loads(out)
    assert payload["status"] == "pushed", out
    assert payload["root_branch"] == "main", out
    published = _ls_remote(root_remote.as_uri(), "refs/heads/main")
    assert published == _git_out(ws, "rev-parse", "HEAD"), (
        "the remote must hold the root commit the verb claimed to publish"
    )

    # THE FIRST VERSION OF THIS ROW WAS WRONG, and the port is what exposed it. It made a
    # local commit in a member and expected push to refuse; that passes only while
    # `store check` measures the member's HEAD. Section 5a's input is the PIN, and section
    # 5's status table makes a member whose HEAD has moved `unpinned` -- a status
    # OBSERVATION, not a refusal (break_03's row draws the same distinction). So the two
    # halves below are separate on purpose: a moved HEAD must NOT refuse, and an uncovered
    # PIN must.
    (ws / "alpha" / "unpushed.txt").write_text("unpushed\n")
    _git(ws / "alpha", "add", ".")
    _git(ws / "alpha", "commit", "-q", "-m", "unpushed")
    moved = _git_out(ws / "alpha", "rev-parse", "HEAD")
    rc, out = _cli("store", "push", "--json")
    assert rc == 0, (
        f"an unpinned member -- HEAD moved, recorded pin still covered -- is a status "
        f"observation, not a push refusal; got {rc}: {out}"
    )

    # Now make the PIN itself uncoverable, in both grip.toml and the root's gitlink (they
    # AGREE, so this drives coverage and not the 4-consistency refusal).
    text = (ws / "grip.toml").read_text()
    pin = _git_out(ws, "rev-parse", "HEAD:alpha")
    assert pin in text, f"the fixture must start from the pin store commit wrote: {pin}"
    assert pin != moved, "the fixture must move the pin to a different value"
    (ws / "grip.toml").write_text(text.replace(pin, moved))
    _git(ws, "add", "grip.toml")
    _git(ws, "update-index", "--add", "--cacheinfo", f"160000,{moved},alpha")
    _git(ws, "commit", "-q", "-m", "malformed root: a pin its upstream does not have")
    root_before = _git_out(ws, "rev-parse", "HEAD")
    rc, out = _cli("store", "push", "--json")
    assert rc == 3, f"an uncovered pin must refuse the push with 3, got {rc}: {out}"
    assert "alpha" in out, out
    assert _git_out(ws, "rev-parse", "HEAD") == root_before, "a refused push must not move the root"
    assert _ls_remote(root_remote.as_uri(), "refs/heads/main") == published, (
        "a refused push must leave the remote ref exactly as it was"
    )


# ---------------------------------------------------------------------------
# materialize: no partial tree
# ---------------------------------------------------------------------------


def test_materialize_reports_exactly_which_members_are_done(
    ws: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A member its origin cannot serve: exit 3, and the done-list is ACCURATE.

    Section 5 allows either rollback or an exact report of which members are done. The verb
    reports. So the row checks the report rather than trusting it: every member named done
    must be at its pin, and every member not named done must have no tree at all -- the
    "silently partial" shape is the failure this row exists for.
    """
    root_remote = tmp_path / "root.git"
    subprocess.run(["git", "init", "-q", "-b", "main", "--bare", str(root_remote)], check=True)
    assert _cli("store", "init", str(ws))[0] == 0
    _git(ws, "remote", "add", "origin", str(root_remote.as_uri()))
    assert _cli("store", "commit", "-m", "first")[0] == 0
    assert _cli("store", "push", "--json")[0] == 0
    pins = {name: _git_out(ws / name, "rev-parse", "HEAD") for name in ("alpha", "beta")}

    fresh = tmp_path / "fresh"
    subprocess.run(
        ["git", "clone", "-q", "--no-checkout", root_remote.as_uri(), str(fresh)],
        capture_output=True,
        check=True,
    )
    # make beta unservable: its remote is gone, so materialize can never reach its pin
    beta_remote = ws.parent / "beta.git"
    assert beta_remote.exists()
    shutil.rmtree(beta_remote)
    assert not beta_remote.exists(), "the fixture must actually remove beta's origin"

    monkeypatch.chdir(fresh)
    rc, out = _cli("store", "materialize", "--json")
    assert rc == 3, f"a pin its origin cannot serve must refuse with 3, got {rc}: {out}"
    assert "beta" in out, out
    assert pins["beta"][:8] in out, "the refusal must name the pin it could not serve"
    assert "done:" in out, f"the refusal must report which members are done; got: {out}"

    done_part = out.split("done:", 1)[1]
    done = {name for name in ("alpha", "beta") if name in done_part}
    assert done == {"alpha"}, f"alpha is the only member that could complete; got {done}: {out}"
    assert _git_out(fresh / "alpha", "rev-parse", "HEAD") == pins["alpha"], (
        "a member reported done must be at its pin"
    )
    assert not (fresh / "beta").exists(), "a member not reported done must leave no tree behind"


# ---------------------------------------------------------------------------
# log (ported) and the three verbs that are still alpha-shaped
# ---------------------------------------------------------------------------


def test_fresh_init_generates_the_allow_list_and_the_root_ends_clean(ws: Path) -> None:
    """Section 3a's fruit: a root whose `git status` is not noise.

    Without the allow-list a gr1 root reports its whole member checkout as untracked, and
    someone eventually commits a venv. The assertion is the FRUIT (`git status --porcelain`
    empty after init+commit), not the file's existence: a `.gitignore` that exists but does
    not silence the member would pass a weaker row and fail the design's reason.
    """
    assert _cli("store", "init", str(ws))[0] == 0
    allow_list = ws / ".gitignore"
    assert allow_list.is_file(), "a fresh init writes the 3a allow-list"
    text = allow_list.read_text()
    for line in ("/*", "!/.gitignore", "!/grip.toml", "!/alpha", "!/beta"):
        assert line in text.splitlines(), f"{line!r} missing from the allow-list:\n{text}"

    assert _cli("store", "commit", "-m", "first")[0] == 0
    tracked = _git_out(ws, "ls-tree", "HEAD", "--name-only").splitlines()
    assert sorted(tracked) == [".gitignore", "alpha", "beta", "grip.toml"], tracked
    assert _git_out(ws, "status", "--porcelain") == "", (
        "the whole point of the allow-list: a committed root is clean, not venv-noise"
    )


def test_an_adopted_root_keeps_its_own_gitignore_and_never_commits_it(
    ws: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Adoption is an adoption: the owner's rule survives byte-for-byte and is not staged.

    The second half is the control the byte-identity check does not give: a verb that
    rewrote the file and restored it, or that staged the owner's rule into OUR root commit,
    would pass a bytes-only row. The marker line is what lets commit tell the two apart
    without guessing.
    """
    root = ws.parent / "adopted"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    owner_rule = "# the owner's own rule\nalpha/\nbeta/\n"
    (root / ".gitignore").write_text(owner_rule)
    subprocess.run(
        ["git", "clone", "-q", str(ws.parent / "alpha.git"), str(root / "alpha")],
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ["git", "clone", "-q", str(ws.parent / "beta.git"), str(root / "beta")],
        capture_output=True,
        check=True,
    )
    monkeypatch.chdir(root)
    # the root repo already exists, so this is the ADOPTION path
    assert _cli("store", "init", "--json")[0] == 0
    assert (root / ".gitignore").read_text() == owner_rule, "adoption must not edit the owner's rule"
    assert _cli("store", "commit", "-m", "first")[0] == 0
    assert (root / ".gitignore").read_text() == owner_rule, "and not after the commit either"
    tracked = _git_out(root, "ls-tree", "HEAD", "--name-only").splitlines()
    assert ".gitignore" not in tracked, f"the owner's rule is not ours to commit: {tracked}"
    assert "alpha" in tracked and "beta" in tracked, tracked


def test_write_gitignore_unignores_a_nested_member_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The nested shape, driven directly: a sibling-only discovery cannot reach it yet.

    `store init` derives members from sibling clones, so a nested member path
    (`reference/mem0`, the witness shape) needs the spec-driven form, which is not built.
    The generator is the part section 3a specifies precisely, so it is driven directly here
    rather than left unasserted behind a shape that does not exist yet.
    """
    from gr2.python_cli.grip_cli import _write_gitignore

    root = tmp_path / "nested"
    root.mkdir()
    (root / "reference" / "mem0").mkdir(parents=True)
    _write_gitignore(root, [{"name": "mem0", "path": "reference/mem0"}])
    lines = (root / ".gitignore").read_text().splitlines()
    assert lines[1:] == [
        "/*",
        "!/.gitignore",
        "!/grip.toml",
        "!/reference/",
        "/reference/*",
        "!/reference/mem0",
    ], lines
    monkeypatch.chdir(root)
    _git(root, "init", "-q", "-b", "main")  # check-ignore needs a repo to read the rule
    # `check-ignore` exits 1 and prints nothing when the path is NOT ignored, so both calls
    # are read with check=False: the output is the instrument, not the exit code.
    assert _git_out(root, "check-ignore", "reference/mem0/marker.txt", check=False) == "", (
        "the nested member must not be ignored"
    )
    assert _git_out(root, "check-ignore", "reference/other.txt", check=False) != "", (
        "control: a sibling of the nested member IS ignored, or the un-ignore proves nothing"
    )


def test_status_and_check_refuse_a_root_that_is_not_a_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No grip.toml and no HEAD: exit 5 with a named refusal, never an internal exit 1.

    Measured 2026-09-28 before this row: both verbs printed git's own "fatal: invalid object
    name 'HEAD'" and exited 1 -- a named refusal on an internal code, which the store group's
    table has no room for. The root is simply unmeasurable, and 5 says so.
    """
    root = tmp_path / "bare-root"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    monkeypatch.chdir(root)
    for verb in ("status", "check"):
        rc, out = _cli("store", verb, "--json")
        assert rc == 5, f"store {verb} on a root with no spec must exit 5, got {rc}: {out}"
        assert "grip.toml" in out, f"the refusal must name what is missing; got: {out}"
        assert "fatal:" not in out, f"git's raw error is not a refusal; got: {out}"


def test_commit_and_check_read_the_members_upstream_not_the_literal_origin(
    ws: Path, tmp_path: Path
) -> None:
    """F1'S WITNESS, and it only works because the field is set to a NON-DEFAULT value.

    §5a: "The check is against `upstream`, never against the literal name `origin`". The
    divergence was invisible by construction: `_write_native_members` writes
    `upstream = member.get("upstream", "origin/main")`, so on any member that leaves the
    field alone the literal and the field are the SAME STRING and a verb that hardcoded
    `origin/main` would pass every other row in this file (Atlas, pre-read m_d416e648 F1).

    So this row separates them: alpha's `upstream` is pointed at a SECOND remote that carries
    the pin, and alpha's origin is force-pushed away from it. A verb reading the literal
    refuses; a verb reading the member's field commits. The control is the second half, which
    flips the field back and requires the same verb to refuse, naming the ref it compared.
    """
    assert _cli("store", "init", str(ws))[0] == 0
    alpha = ws / "alpha"
    pin = _git_out(alpha, "rev-parse", "HEAD")

    fork = tmp_path / "fork.git"
    subprocess.run(["git", "init", "-q", "-b", "main", "--bare", str(fork)], check=True)
    _git(alpha, "remote", "add", "fork", str(fork))
    _git(alpha, "push", "-q", "fork", "main")
    assert _ls_remote(str(fork), "refs/heads/main") == pin, "the fork must carry the pin"

    orphan = tmp_path / "orphan"
    orphan.mkdir()
    _git(orphan, "init", "-q", "-b", "main")
    _git(orphan, "config", "user.email", "t@e.invalid")
    _git(orphan, "config", "user.name", "t")
    (orphan / "README.md").write_text("# rewritten\n")
    _git(orphan, "add", ".")
    _git(orphan, "commit", "-q", "-m", "rewrite")
    _git(orphan, "push", "-q", "--force", str(ws.parent / "alpha.git"), "main")
    assert _ls_remote(str(ws.parent / "alpha.git"), "refs/heads/main") != pin, (
        "the fixture must move ORIGIN off the pin, or both refs would agree"
    )

    # alpha's upstream -> the fork. The partition keeps the edit on ALPHA's block: replacing
    # the first occurrence in the file would silently hit whichever member is written first.
    head, sep, tail = (ws / "grip.toml").read_text().partition('name = "alpha"')
    assert sep, "the fixture must find alpha's block"
    tail = tail.replace('upstream = "origin/main"', 'upstream = "fork/main"', 1)
    (ws / "grip.toml").write_text(head + sep + tail)
    assert 'upstream = "fork/main"' in (ws / "grip.toml").read_text()

    rc, out = _cli("store", "commit", "-m", "fork covered", "--json")
    assert rc == 0, (
        f"the pin IS on fork/main, which is the member's DECLARED upstream; a refusal here "
        f"means the verb read the literal origin/main instead: {out}"
    )

    # CONTROL: the same verb, the same pin, the field flipped back -- now it must refuse and
    # name the ref it compared.
    text = (ws / "grip.toml").read_text()
    (ws / "grip.toml").write_text(
        text.replace('upstream = "fork/main"', 'upstream = "origin/main"', 1)
    )
    rc, out = _cli("store", "check", "--json")
    assert rc == 3, f"with the field back at origin/main the pin is uncovered; got {rc}: {out}"
    assert "origin/main" in out, f"the refusal must name the upstream it compared: {out}"


def test_check_and_push_refuse_an_init_root_that_has_no_commit_yet(
    ws: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Finding B's class in CHECK and PUSH (Apollo, v2 r1).

    The guard was lifted out of status into `_require_root_commit` because the same
    unborn-HEAD read sat in check (`ls-tree HEAD`), in log (`rev-list HEAD`) and in push,
    which inherits check. This row drives check and push; the log row is its sibling, so
    Apollo's mutation -- drop the guard in check ONLY -- turns this one red and leaves the log
    row green, which is what localizes the defect to the verb rather than to the helper.
    """
    assert _cli("store", "init", str(ws))[0] == 0

    rc, out = _cli("store", "check", "--json")
    assert rc == 5, f"an uncommitted root cannot be measured; expected 5, got {rc}: {out}"
    assert "store commit" in out, f"the refusal must name the verb to run: {out}"
    assert "fatal:" not in out, f"git's raw error is not a refusal: {out}"

    # push inherits check's gate, so it must refuse the same way rather than reaching its own
    # git call first
    root_remote = tmp_path / "root.git"
    subprocess.run(["git", "init", "-q", "-b", "main", "--bare", str(root_remote)], check=True)
    _git(ws, "remote", "add", "origin", str(root_remote.as_uri()))
    rc, out = _cli("store", "push", "--json")
    assert rc == 5, f"push inherits check's refusal; expected 5, got {rc}: {out}"
    assert "store commit" in out, out
    assert _ls_remote(root_remote.as_uri(), "refs/heads/main") == "", (
        "and it published nothing: the remote must still have no root ref"
    )

    # CONTROL: the same two verbs on the same root after a commit
    assert _cli("store", "commit", "-m", "first")[0] == 0
    rc, out = _cli("store", "check", "--json")
    assert rc == 0, f"control: check after a commit must exit 0, got {rc}: {out}"
    rc, out = _cli("store", "push", "--json")
    assert rc == 0, f"control: push after a commit must exit 0, got {rc}: {out}"


def test_log_refuses_an_init_root_that_has_no_commit_yet(
    ws: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The third verb carrying finding B's class (Apollo, v2 r1).

    `log` ran `rev-list HEAD`, which on an unborn HEAD is git's "ambiguous argument" wrapped
    in a code. Its own row, so a mutation aimed at check leaves THIS one green and the reader
    can see which verb was actually broken.
    """
    assert _cli("store", "init", str(ws))[0] == 0

    rc, out = _cli("store", "log", "--json")
    assert rc == 5, f"an uncommitted root has no history to report; expected 5, got {rc}: {out}"
    assert "store commit" in out, f"the refusal must name the verb to run: {out}"
    assert "fatal:" not in out, f"git's raw error is not a refusal: {out}"

    assert _cli("store", "commit", "-m", "first")[0] == 0
    rc, out = _cli("store", "log", "--json")
    assert rc == 0, f"control: log after a commit must exit 0, got {rc}: {out}"
    assert json.loads(out)["entries"], out


def test_status_refuses_an_init_root_that_has_no_commit_yet(
    ws: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A named state, not a git error (Apollo, r1 m_227344fd finding B).

    `ls-tree HEAD` raises on an unborn HEAD, which surfaced as a 5 carrying git's own
    "fatal: invalid object name 'HEAD'". The row asserts the NAME and the ABSENCE of the raw
    error, and carries its own control: the same verb on the same root after a commit exits 0,
    so this cannot pass by refusing everything.
    """
    assert _cli("store", "init", str(ws))[0] == 0

    rc, out = _cli("store", "status", "--json")
    assert rc == 5, f"an uncommitted root cannot be measured; expected 5, got {rc}: {out}"
    assert "store commit" in out, f"the refusal must name the verb to run: {out}"
    assert "fatal:" not in out, f"git's raw error is not a refusal: {out}"

    assert _cli("store", "commit", "-m", "first")[0] == 0
    rc, out = _cli("store", "status", "--json")
    assert rc == 0, f"control: the same root after a commit must exit 0, got {rc}: {out}"


def test_diff_refuses_a_ref_it_cannot_resolve(ws: Path) -> None:
    """Exit 5 naming the ref, never a wrapped git message (Apollo, r1 ruling)."""
    assert _cli("store", "init", str(ws))[0] == 0
    assert _cli("store", "commit", "-m", "first")[0] == 0

    rc, out = _cli("store", "diff", "HEAD~5", "HEAD", "--json")
    assert rc == 5, f"an unresolvable ref cannot be measured; expected 5, got {rc}: {out}"
    assert "HEAD~5" in out, f"the refusal must name the ref it could not resolve: {out}"
    # OUR refusal leads and git's detail FOLLOWS it. This row used to assert `"fatal:" not in
    # out`, which was right while the diagnostic was discarded and is wrong now that it is
    # deliberately carried: git's detail is the half that names candidates for an ambiguous
    # prefix. The property that matters is that the message is OURS first, not that git's
    # words are absent.
    first_line = out.strip().splitlines()[0]
    assert first_line.startswith("HEAD~5 is not a root commit"), (
        f"the named refusal must lead, not git's text: {first_line}"
    )
    assert "git says:" in out, f"git's diagnostic must be carried, not discarded: {out}"

    rc, out = _cli("store", "diff", "HEAD", "HEAD", "--json")
    assert rc == 0, f"control: two resolvable refs must succeed, got {rc}: {out}"


def test_checkout_json_reports_the_commit_it_resolved_not_the_ref_typed(ws: Path) -> None:
    """The reported commit must be a COMMIT (Apollo, r1 m_227344fd finding A).

    `--json` used to echo `root_commit: "HEAD~1"`, which names a commit only relative to a
    HEAD the call itself just moved: a caller could not tell which commit it got, and the
    value is meaningless to any later read.
    """
    assert _cli("store", "init", str(ws))[0] == 0
    assert _cli("store", "commit", "-m", "first")[0] == 0
    first = _git_out(ws, "rev-parse", "HEAD")
    (ws / "alpha" / "next.txt").write_text("next\n")
    _git(ws / "alpha", "add", ".")
    _git(ws / "alpha", "commit", "-q", "-m", "next")
    _git(ws / "alpha", "push", "-q", "origin", "main")
    assert _cli("store", "commit", "-m", "second")[0] == 0

    rc, out = _cli("store", "checkout", "HEAD~1", "--json")
    assert rc == 0, out
    reported = json.loads(out)["root_commit"]
    assert reported == first, f"the resolved commit must be reported: {reported} vs {first}"
    assert reported != "HEAD~1", "the typed ref is not a commit"
    assert _git_out(ws, "rev-parse", "HEAD") == first, "and it is where the root actually is"


def test_log_reports_the_root_shape_not_the_alpha_snapshot_index(ws: Path) -> None:
    """The SHAPE, because both branches printed something.

    Atlas's r2 BLOCK (m_ce3627a7) found `store log <root>` still reading
    `.grip/snapshots/index.json` while the no-argument branch was already native. A row that
    asserted "log printed entries" would have passed on EITHER branch, which is why this
    asserts the entry KEYS: a native entry carries `commit`, `message` and `pins`; an alpha
    snapshot-index row carried `id` and `repos`. And the positional root is gone, so the
    alpha branch is not reachable by spelling it differently.
    """
    assert _cli("store", "init", str(ws))[0] == 0
    assert _cli("store", "commit", "-m", "first")[0] == 0

    rc, out = _cli("store", "log", "--json")
    assert rc == 0, out
    entries = json.loads(out)["entries"]
    assert entries, "the root has one commit, so there is one entry"
    entry = entries[0]
    assert set(entry) >= {"commit", "message", "pins"}, f"native entry keys: {sorted(entry)}"
    assert "id" not in entry and "repos" not in entry, (
        f"an alpha snapshot-index row must not be what log prints: {sorted(entry)}"
    )
    assert entry["message"] == "first", entry

    rc, out = _cli("store", "log", str(ws), "--json")
    assert rc == 2, (
        f"log takes no positional root any more, so this is a usage error; got {rc}: {out}"
    )


def test_log_reports_pin_changes_per_root_commit(ws: Path) -> None:
    assert _cli("store", "init", str(ws))[0] == 0
    assert _cli("store", "commit", "-m", "first")[0] == 0
    first_pins = {name: _git_out(ws / name, "rev-parse", "HEAD") for name in ("alpha", "beta")}

    (ws / "alpha" / "next.txt").write_text("next\n")
    _git(ws / "alpha", "add", ".")
    _git(ws / "alpha", "commit", "-q", "-m", "next")
    _git(ws / "alpha", "push", "-q", "origin", "main")
    assert _cli("store", "commit", "-m", "second")[0] == 0

    rc, out = _cli("store", "log", "--json")
    assert rc == 0, out
    entries = json.loads(out)["entries"]
    assert [entry["message"] for entry in entries] == ["first", "second"], out
    first, second = entries
    assert {change["name"]: change["after"] for change in first["pins"]} == first_pins, first
    moved = {change["name"]: change for change in second["pins"]}
    assert set(moved) == {"alpha"}, f"only alpha's pin moved: {second}"
    assert moved["alpha"]["before"] == first_pins["alpha"], moved["alpha"]
    assert moved["alpha"]["after"] == _git_out(ws / "alpha", "rev-parse", "HEAD"), moved["alpha"]


def test_snapshot_is_a_hidden_alias_of_commit(ws: Path) -> None:
    assert _cli("store", "init", str(ws))[0] == 0
    rc, out = _cli("store", "snapshot", "-m", "first", "--json")
    assert rc == 0, out
    assert json.loads(out)["status"] == "committed", out
    assert _git_out(ws, "ls-tree", "HEAD", "alpha").split()[2] == _git_out(
        ws / "alpha", "rev-parse", "HEAD"
    )


def test_diff_reports_pin_changes_between_two_root_commits(ws: Path) -> None:
    assert _cli("store", "init", str(ws))[0] == 0
    assert _cli("store", "commit", "-m", "first")[0] == 0
    (ws / "alpha" / "next.txt").write_text("next\n")
    _git(ws / "alpha", "add", ".")
    _git(ws / "alpha", "commit", "-q", "-m", "next")
    _git(ws / "alpha", "push", "-q", "origin", "main")
    assert _cli("store", "commit", "-m", "second")[0] == 0

    rc, out = _cli("store", "diff", "HEAD~1", "HEAD", "--json")
    assert rc == 0, out
    changes = {row["name"]: row for row in json.loads(out)["members"]}
    assert changes["alpha"]["changed"] is True, out
    assert changes["beta"]["changed"] is False, out


def test_checkout_materializes_the_members_at_a_root_commit(ws: Path) -> None:
    assert _cli("store", "init", str(ws))[0] == 0
    assert _cli("store", "commit", "-m", "first")[0] == 0
    first = {name: _git_out(ws / name, "rev-parse", "HEAD") for name in ("alpha", "beta")}
    (ws / "alpha" / "next.txt").write_text("next\n")
    _git(ws / "alpha", "add", ".")
    _git(ws / "alpha", "commit", "-q", "-m", "next")
    _git(ws / "alpha", "push", "-q", "origin", "main")
    assert _cli("store", "commit", "-m", "second")[0] == 0

    rc, out = _cli("store", "checkout", "HEAD~1", "--json")
    assert rc == 0, out
    assert _git_out(ws / "alpha", "rev-parse", "HEAD") == first["alpha"], out
    assert _git_out(ws / "beta", "rev-parse", "HEAD") == first["beta"], out


# ---------------------------------------------------------------------------
# The CLASS guard: no test may pass a WORKSPACE POSITIONAL to a ported verb
# ---------------------------------------------------------------------------


def test_no_test_passes_a_workspace_positional_to_a_ported_verb() -> None:
    """The sweep step 3 v3 was BLOCKed with, kept as a row so it cannot rot.

    Eleven test_grip_cli rows went red on v3's head for a reason that had nothing to do with
    what they asserted: the signature port removed the workspace positional (the verbs act on
    cwd), so typer refused the call at the PARSER -- exit 2 before any store code ran. The class
    is "a test that passes a workspace positional to a ported verb", and it is detectable
    statically, which is why it is a row rather than a note in a PR body.

    WHITESPACE IS NORMALISED BEFORE MATCHING, and that is the instrument's whole point: most
    alpha invocations were written across five lines, with the verb and its path argument on
    separate lines of the list, so a per-line scan sees only the single-line minority. Measured
    on the base: the per-line scan finds 3, the normalised scan finds 20 -- and 20 is what an
    independent sweep counted (snapshot 12, log 4, diff 2, checkout 2). A query that undercounts
    returns 0 and reads as a pass, which is exactly the failure this row exists to prevent.

    SELF-MATCH IS THE REAL HAZARD HERE, and it bit this row on its first run: the docstring above
    originally quoted the five-line invocation verbatim, whitespace-normalisation folded it into
    the pattern, and the sweep matched ITS OWN FILE. The example is therefore described rather
    than quoted, and the control below is BUILT FROM PARTS so that no contiguous match exists in
    this file's source. The control assertion is what keeps a pattern that stopped matching from
    reporting 0.
    """
    ported = {
        "snapshot", "log", "diff", "checkout", "materialize",
        "status", "check", "push", "commit", "migrate",
    }
    pattern = re.compile(r'\[\s*"grip"\s*,\s*"([a-z-]+)"\s*,\s*str\(')

    control = 'runner.invoke(app, ["grip", ' + '"snapshot", str(' + 'workspace), "--json"])'
    control_hits = [m.group(1) for m in pattern.finditer(control) if m.group(1) in ported]
    assert control_hits == ["snapshot"], (
        "the pattern can no longer match the class it guards, so a 0 below would mean nothing"
    )

    tests_dir = Path(__file__).resolve().parent
    hits: list[str] = []
    for path in sorted(tests_dir.rglob("*.py")):
        for m in pattern.finditer(path.read_text()):
            if m.group(1) in ported:
                hits.append(f"{path.name}: {m.group(0)[:60]}")
    assert hits == [], (
        "a test still passes a workspace positional to a ported verb (the ported verbs act on "
        "cwd and take no positional; typer refuses the call at the parser):\n  " + "\n  ".join(hits)
    )

