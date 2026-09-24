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
    return subprocess.run(
        ["git", *args], cwd=cwd, check=False, capture_output=True, text=True
    )


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
    result = make_cli_runner().invoke(
        app, ["workspace", "init", str(root), "--from-superproject"]
    )
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
