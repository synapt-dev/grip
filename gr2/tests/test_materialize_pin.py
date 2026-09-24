"""A materialized member lands on the commit the ROOT declares, not on whatever
its default branch happens to point at.

The defect this pins, measured on ``git-training-open/submodule-example``
before the fix: after ``workspace init --from-superproject`` and ``materialize``,
``agents/default/home/example1`` sat on ``master`` at ``ceed35b970e2`` while the
root pinned ``065be099e95a`` — and ``jabberwocky`` READ CORRECT only because that
repository's master tip happens to equal its pin.

**That is why every fixture here is a PAIR.** A test built only on a member whose
default tip equals its pin passes while the code does the wrong thing, so the
first member of each pair is built to DIVERGE: its default branch moves past the
commit the root pins, and the fixture asserts that divergence before asserting
the behaviour. `test_the_pair_is_a_discriminating_instrument` is that guard.

The two edges are pinned separately: a pin the clone cannot
reach REFUSES loudly rather than falling back to the tip, and a
member with no pin is left on its default branch -- its materialize half is
pinned here, and the half that makes status NAME it unpinned rather than let it
read as at-pin belongs with the status change, not in this range.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from gr2.python_cli import gitops
from gr2.python_cli.app import app

from tests.conftest import make_cli_runner


def _run(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=cwd, check=False, capture_output=True, text=True)


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "dev@layne.pro"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Layne Penney"], cwd=path, check=True)


def _commit(path: Path, name: str, body: str) -> str:
    (path / name).write_text(body)
    subprocess.run(["git", "add", name], cwd=path, check=True)
    subprocess.run(["git", "commit", "-qm", f"add {name}"], cwd=path, check=True)
    return _run("rev-parse", "HEAD", cwd=path).stdout.strip()


def _superproject_with_a_divergent_pair(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    """A root pinning two members: one whose default tip has moved past its pin,
    and one whose default tip equals its pin.

    ``diverging`` carries the defect and ``converging`` masks it, so any test
    asserting on both can tell a real fix from a no-op.
    """
    root = tmp_path / "root"
    _init_repo(root)

    pins: dict[str, str] = {}
    for name, diverge in (("diverging", True), ("converging", False)):
        src = tmp_path / f"{name}-src"
        _init_repo(src)
        pinned = _commit(src, "a.txt", "one\n")
        if diverge:
            # move the default branch PAST the commit that will be pinned
            _commit(src, "b.txt", "two\n")
        added = subprocess.run(
            ["git", "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(src), name],
            cwd=root,
            check=False,
            capture_output=True,
            text=True,
        )
        assert added.returncode == 0, added.stderr
        if diverge:
            # wind the gitlink back so the root pins a commit the tip has left
            checkout = _run("checkout", "-q", pinned, cwd=root / name)
            assert checkout.returncode == 0, checkout.stderr
            subprocess.run(["git", "add", name], cwd=root, check=True)
        pins[name] = _run("rev-parse", "HEAD", cwd=root / name).stdout.strip()

    subprocess.run(["git", "commit", "-qm", "add members"], cwd=root, check=True)
    # the gitlink is the declaration, so read the pin from the ROOT's tree
    pins = {
        name: _run("ls-tree", "HEAD", name, cwd=root).stdout.split()[2]
        for name in ("diverging", "converging")
    }
    return root, pins


def test_the_pair_is_a_discriminating_instrument(tmp_path: Path) -> None:
    """FIXTURE GUARD. If both members had their default tip equal to their pin,
    every assertion below would pass on the broken code as well as the fixed
    one, so the divergence is asserted rather than assumed."""
    root, pins = _superproject_with_a_divergent_pair(tmp_path)
    for name in ("diverging", "converging"):
        src = tmp_path / f"{name}-src"
        tip = _run("rev-parse", "HEAD", cwd=src).stdout.strip()
        equal = tip == pins[name]
        if name == "diverging":
            assert not equal, "the diverging member must NOT have its tip at its pin"
        else:
            assert equal, "the converging member's tip IS its pin — that is its job"


def test_materialize_puts_every_member_at_its_declared_pin(tmp_path: Path) -> None:
    """THE WITNESS. The diverging member is the one that can fail; the converging
    member is present so a reader can see that only the first one discriminates."""
    root, pins = _superproject_with_a_divergent_pair(tmp_path)
    result = make_cli_runner().invoke(app, ["workspace", "init", str(root), "--from-superproject"])
    assert result.exit_code == 0, result.stdout
    result = make_cli_runner().invoke(app, ["workspace", "materialize", str(root), "--yes"])
    assert result.exit_code == 0, result.stdout

    unit = root / "agents" / "default" / "home"
    for name, want in pins.items():
        got = _run("rev-parse", "HEAD", cwd=unit / name).stdout.strip()
        assert got == want, f"{name} landed on {got[:12]}, the root pins {want[:12]}"
        branch = _run("rev-parse", "--abbrev-ref", "HEAD", cwd=unit / name).stdout.strip()
        assert branch == "HEAD", f"{name} should be detached at its pin, is on {branch}"
    # and the materialize SAYS which pin it used, so a reader can check it
    assert "diverging@" in result.stdout and pins["diverging"][:12] in result.stdout


def test_an_unreachable_pin_refuses_instead_of_falling_back(tmp_path: Path) -> None:
    """EDGE 1: a pin the clone cannot reach is fetched by sha, and if the server
    will not provide it the member is REFUSED with the pin, the member and the
    command to try. It is never left on the default tip, because a silent
    fallback is the defect itself.

    Built by handing the function a pin no repository has: the honest way to
    reach the refusal without a server configured to refuse sha-fetches.
    """
    repo = tmp_path / "member"
    _init_repo(repo)
    _commit(repo, "a.txt", "one\n")
    missing = "0" * 40

    with pytest.raises(SystemExit) as excinfo:
        gitops.checkout_declared_pin(repo, missing, member="member1")

    message = str(excinfo.value)
    assert missing in message, "the refusal names the pin"
    assert "member1" in message, "and the member"
    assert "fetch --depth 1 origin" in message, "and the command to try by hand"
    # the fallback that must NOT have happened
    head = _run("rev-parse", "HEAD", cwd=repo).stdout.strip()
    assert head != missing
    assert _run("rev-parse", "--abbrev-ref", "HEAD", cwd=repo).stdout.strip() == "main", (
        "the refusal left the member where it was"
    )


def test_a_member_with_no_pin_is_left_on_its_default_branch(tmp_path: Path) -> None:
    """EDGE 2: an unpinned repo is not this function's business. The caller skips
    it, so the member stays on its default branch — and the status is what must
    name it UNPINNED rather than letting it read as at-pin."""
    repo = tmp_path / "plain"
    _init_repo(repo)
    _commit(repo, "a.txt", "one\n")
    before = _run("rev-parse", "HEAD", cwd=repo).stdout.strip()

    gitops.checkout_declared_pin(repo, "", member="plain")  # no pin: no-op by contract

    assert _run("rev-parse", "HEAD", cwd=repo).stdout.strip() == before


# --------------------------------------------------------------------------
# The lanes AROUND materialize, from the reviewer's probes on this range.
#
# The tests above witness the clone. These witness what a caller does with the
# clone afterwards -- sync, lane create, a second materialize after a refusal --
# because a member can sit correctly at its pin and still be moved off it by the
# next command the user runs. Each names the property, and each keeps its
# precondition assert, because a fixture that cannot fail proves nothing.
# --------------------------------------------------------------------------


def _cli(*args: str) -> tuple[int, str]:
    result = make_cli_runner().invoke(app, list(args))
    return result.exit_code, result.stdout


def _materialized_root(tmp_path: Path) -> tuple[Path, dict[str, str], str, Path]:
    """The pair, taken all the way through init and materialize, with the pin
    asserted BEFORE the caller's own command runs."""
    root, pins = _superproject_with_a_divergent_pair(tmp_path)
    tip = _run("rev-parse", "HEAD", cwd=tmp_path / "diverging-src").stdout.strip()
    assert tip != pins["diverging"], "precondition: diverging tip is past its pin"
    rc, out = _cli("workspace", "init", str(root), "--from-superproject")
    assert rc == 0, out
    rc, out = _cli("workspace", "materialize", str(root), "--yes")
    assert rc == 0, out
    unit = root / "agents" / "default" / "home"
    assert _run("rev-parse", "HEAD", cwd=unit / "diverging").stdout.strip() == pins["diverging"], (
        "precondition: materialize itself put the member at its pin"
    )
    return root, pins, tip, unit


def test_PA_sync_after_materialize_keeps_the_pin(tmp_path: Path) -> None:
    """A sync must not quietly walk a pinned member off its commit. If it does,
    the pin survives only until the user's next command."""
    root, pins, _tip, unit = _materialized_root(tmp_path)
    rc, out = _cli("sync", "run", str(root))
    assert rc == 0, out
    got = _run("rev-parse", "HEAD", cwd=unit / "diverging").stdout.strip()
    assert got == pins["diverging"], f"sync moved the member to {got[:12]}"


def test_PB_lane_create_bases_on_the_pin(tmp_path: Path) -> None:
    """A lane is a copy of the declared state. If it is cut from the remote tip
    instead, every lane silently starts from a commit the root never declared."""
    root, pins, _tip, _unit = _materialized_root(tmp_path)
    rc, out = _cli(
        "lane", "create", str(root), "default", "feat", "--repos", "diverging", "--branch", "feat/x"
    )
    assert rc == 0, out
    checkouts = [p for p in (root / ".grip").rglob("diverging") if (p / ".git").exists()]
    assert checkouts, "precondition: the lane created a checkout to assert on"
    for path in checkouts:
        got = _run("rev-parse", "HEAD", cwd=path).stdout.strip()
        assert got == pins["diverging"], f"{path} was based on {got[:12]}, not the pin"


def test_PC_missing_root_member_is_cloned_at_its_pin(tmp_path: Path) -> None:
    """EDGE at the ROOT clone_repo op: a member deleted from the workspace and
    re-materialized lands at its pin, not at the default tip. Before this the op
    cloned and left it at the tip, which also dirtied the superproject."""
    root, pins = _superproject_with_a_divergent_pair(tmp_path)
    rc, out = _cli("workspace", "init", str(root), "--from-superproject")
    assert rc == 0, out
    subprocess.run(["rm", "-rf", str(root / "diverging")], check=True)
    assert not (root / "diverging").exists(), "precondition: the root member path is absent"

    rc, out = _cli("workspace", "materialize", str(root), "--yes")
    assert rc == 0, out
    assert (root / "diverging").exists(), "the op re-cloned the member"
    got = _run("rev-parse", "HEAD", cwd=root / "diverging").stdout.strip()
    assert got == pins["diverging"], f"the root member landed on {got[:12]}"


def _remote_with_a_gcd_pin(tmp_path: Path) -> tuple[Path, str]:
    """A bare remote whose pinned commit is GONE: pushed, the branch deleted, the
    remote reflog expired and gc'd, so no ref and no object carry it.

    The shape matters. A merely-unreachable object is still fetchable by sha and
    a file:// remote serves it; only a gc'd remote refuses the fetch, and only
    that refusal witnesses the no-fallback path.
    """
    work = tmp_path / "w"
    _init_repo(work)
    _commit(work, "a.txt", "1\n")
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(work), str(bare)], check=True)
    subprocess.run(["git", "-C", str(work), "remote", "add", "o", str(bare)], check=True)
    subprocess.run(["git", "-C", str(work), "checkout", "-q", "-b", "side"], check=True)
    pin = _commit(work, "p.txt", "pin\n")
    subprocess.run(["git", "-C", str(work), "push", "-q", "o", "side"], check=True)
    subprocess.run(
        ["git", "-C", str(bare), "branch", "-D", "side"], check=True, capture_output=True
    )
    subprocess.run(
        ["git", "-C", str(bare), "reflog", "expire", "--expire=now", "--all"], check=True
    )
    subprocess.run(["git", "-C", str(bare), "gc", "-q", "--prune=now"], check=True)
    assert (
        subprocess.run(["git", "-C", str(bare), "cat-file", "-e", f"{pin}^{{commit}}"]).returncode
        != 0
    ), "precondition: the remote no longer has the pin"
    return bare, pin


def test_PE_refusal_then_rerun_does_not_converge_on_the_tip(tmp_path: Path) -> None:
    """A half-built member must not be mistaken for a converged one. If a refused
    first run left a clone lying at the tip, the second run finds it present,
    calls the unit converged, and exits 0 over a member at the wrong commit.

    Staging is what makes this decidable: nothing is renamed into place until the
    pin is checked out, so a refusal leaves the destination untouched.
    """
    bare, pin = _remote_with_a_gcd_pin(tmp_path)
    root = tmp_path / "root"
    _init_repo(root)
    _commit(root, "r.txt", "r\n")
    remote_tip = _run("rev-parse", "main", cwd=bare).stdout.strip()
    subprocess.run(
        ["git", "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(bare), "m"],
        cwd=root,
        check=True,
    )
    subprocess.run(["git", "update-index", "--cacheinfo", f"160000,{pin},m"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "pin m"], cwd=root, check=True)
    assert _run("ls-tree", "HEAD", "m", cwd=root).stdout.split()[2] == pin, (
        "precondition: the root pins the gone commit"
    )

    rc, out = _cli("workspace", "init", str(root), "--from-superproject")
    assert rc == 0, out
    rc1, out1 = _cli("workspace", "materialize", str(root), "--yes")
    assert rc1 != 0, f"the first run refuses rather than falling back: {out1}"

    rc2, out2 = _cli("workspace", "materialize", str(root), "--yes")
    unit_member = root / "agents" / "default" / "home" / "m"
    after = (
        _run("rev-parse", "HEAD", cwd=unit_member).stdout.strip()
        if unit_member.exists()
        else "absent"
    )
    assert not (rc2 == 0 and after == remote_tip), (
        f"the re-run called the remote tip {remote_tip[:12]} converged"
    )


def test_PD_a_sha_fetch_does_not_make_a_full_clone_shallow(tmp_path: Path) -> None:
    """The pin is fetched by sha, and `--depth 1` on that fetch belongs ONLY on a
    clone that is already shallow. On a full clone it silently truncates history
    the user never asked to lose -- and nothing in the output says so."""
    work = tmp_path / "w"
    _init_repo(work)
    _commit(work, "a.txt", "1\n")
    _commit(work, "b.txt", "2\n")
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(work), str(bare)], check=True)
    subprocess.run(["git", "-C", str(work), "checkout", "-q", "-b", "side"], check=True)
    pin = _commit(work, "p.txt", "pin\n")
    subprocess.run(["git", "-C", str(work), "push", "-q", str(bare), "side"], check=True)
    # delete the REF but leave the OBJECT: the clone below cannot reach the pin by
    # ref, and the remote can still serve it by sha
    subprocess.run(["git", "-C", str(bare), "update-ref", "-d", "refs/heads/side"], check=True)
    assert subprocess.run(["git", "-C", str(bare), "cat-file", "-e", pin]).returncode == 0, (
        "precondition: the remote holds the object"
    )

    clone = tmp_path / "c"
    subprocess.run(
        ["git", "-c", "protocol.file.allow=always", "clone", "-q", f"file://{bare}", str(clone)],
        check=True,
    )
    assert _run("cat-file", "-e", f"{pin}^{{commit}}", cwd=clone).returncode != 0, (
        "precondition: the clone lacks the pin, so the fetch path runs"
    )
    before = _run("rev-parse", "--is-shallow-repository", cwd=clone).stdout.strip()
    assert before == "false", "precondition: the clone starts full"

    gitops.checkout_declared_pin(clone, pin, member="m")

    assert _run("rev-parse", "HEAD", cwd=clone).stdout.strip() == pin
    after = _run("rev-parse", "--is-shallow-repository", cwd=clone).stdout.strip()
    assert after == "false", "the sha fetch must not truncate a full clone"


def test_is_repo_root_answers_its_own_question(tmp_path: Path) -> None:
    """GUARD WITNESS for clone_repo's pre-existence check. `is_git_repo` reads
    through to the enclosing repository, so the two helpers disagree on a plain
    directory inside a checkout -- which is exactly the shape of a staging
    directory and of a unit member path. If they ever stop disagreeing here, the
    guard has silently gone back to asking the wrong question."""
    outer = tmp_path / "outer"
    _init_repo(outer)
    _commit(outer, "a.txt", "1\n")
    inside = outer / "plain"
    inside.mkdir()

    assert gitops.is_repo_root(outer) is True, "a work tree's own top IS a repo root"
    assert gitops.is_git_repo(inside) is True, (
        "the read-through helper speaks for the ENCLOSING repo"
    )
    assert gitops.is_repo_root(inside) is False, "a directory inside one is not itself a repo root"
    assert gitops.is_repo_root(tmp_path / "absent") is False, "a path that does not exist is not"
