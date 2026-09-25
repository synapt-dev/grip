"""A refused `lane create` must leave no lane that any verb accepts.

Measured before the fix: `lane create` refused at materialization, exited 1, and STILL
left `.grip/state/lanes/<unit>/<lane>/lane.toml` on disk. `lane enter` then returned 0 on
that leftover, `lane exit` returned 0, and `exec run` returned 1 with the repos missing --
two steps away from the create that refused, with nothing in between pointing back at it.
`lane enter` returning 0 is what makes this worse than a stray file: the failure surfaces
somewhere else entirely.

The control matters as much as the witness here. A witness that only asserts "the create
refused and the lane is absent" is satisfied by a build in which lane creation is broken
outright, so the second test creates a lane whose source DOES exist and requires it to
return 0 and be enterable. The witness must be a statement about the refusal path, not
about the verb being dead.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from typer.testing import CliRunner

from gr2.python_cli.app import app

runner = CliRunner()

SPEC = (
    'schema_version = 1\nworkspace_name = "m"\n\n'
    '[[repos]]\nname = "repo-a"\npath = "repos/repo-a"\nurl = "{url}"\n\n'
    '[[units]]\nname = "atlas"\npath = "agents/atlas"\nrepos = ["repo-a"]\n'
)


def _git(cwd: Path, *a: str) -> str:
    return subprocess.run(
        ["git", *a], cwd=cwd, text=True, capture_output=True, check=True
    ).stdout.strip()


def _unmaterialized_workspace(tmp_path: Path) -> Path:
    """A workspace whose spec names a repo that was never materialized.

    `lane create` writes the lane document and THEN materializes, so this is the state
    in which the command refuses with an artifact already on disk. The url is unreachable
    as well as unmaterialized: the refusal must not depend on network reachability.
    """
    ws = tmp_path / "ws-unmaterialized"
    (ws / ".grip").mkdir(parents=True)
    (ws / ".grip" / "workspace_spec.toml").write_text(
        SPEC.format(url="https://example.invalid/repo-a.git")
    )
    return ws


def _materialized_workspace(tmp_path: Path) -> Path:
    """The control's fixture: the same spec, with the source actually materialized."""
    origin = tmp_path / "repo-a.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    ws = tmp_path / "ws-materialized"
    src = ws / "repos" / "repo-a"
    src.parent.mkdir(parents=True)
    _git(tmp_path, "clone", "-q", str(origin), str(src))
    _git(src, "config", "user.email", "t@e.invalid")
    _git(src, "config", "user.name", "t")
    (src / "f.txt").write_text("base\n")
    _git(src, "add", ".")
    _git(src, "commit", "-q", "-m", "base")
    _git(src, "push", "-q", "origin", "HEAD:refs/heads/main")
    (ws / ".grip").mkdir(parents=True)
    (ws / ".grip" / "workspace_spec.toml").write_text(SPEC.format(url=str(origin)))
    return ws


def _create(ws: Path, lane: str) -> "subprocess.CompletedProcess[str]":
    return runner.invoke(
        app,
        ["lane", "create", str(ws), "atlas", lane, "--repos", "repo-a", "--branch", "feat/x"],
    )


def _enter(ws: Path, lane: str):
    return runner.invoke(
        app,
        ["lane", "enter", str(ws), "atlas", lane, "--actor", "agent:apollo"],
    )


def test_a_refused_lane_create_leaves_no_lane_any_verb_accepts(tmp_path: Path) -> None:
    """THE WITNESS. A refusal must leave nothing behind, and no verb may accept it."""
    ws = _unmaterialized_workspace(tmp_path)
    lane_root = ws / ".grip" / "state" / "lanes" / "atlas" / "orph-lane"

    create = _create(ws, "orph-lane")
    assert create.exit_code != 0, (
        f"the create must refuse on an unmaterialized source; got {create.exit_code}: {create.output}"
    )
    assert not lane_root.exists(), (
        f"a refused create must leave no lane on disk, but {lane_root} exists"
    )

    enter = _enter(ws, "orph-lane")
    assert enter.exit_code != 0, (
        f"no verb may accept a lane that was never created; enter returned {enter.exit_code}: {enter.output}"
    )
    assert "not found" in enter.output.lower(), (
        f"the refusal must say the lane is absent; got {enter.output!r}"
    )


def _blocked_hook_workspace(tmp_path: Path) -> Path:
    """A workspace whose repo carries a projection hook that BLOCKS after materialization.

    The checkout IS materialized -- so the fork base is recorded -- and then a
    `files.link` whose source does not exist in that checkout refuses. This is the
    second refusal through the same call, and the one whose lane must be KEPT.
    """
    ws = tmp_path / "ws-blocked-hook"
    (ws / ".grip").mkdir(parents=True)
    _git(ws / ".grip", "init", "-q", "-b", "main")
    _git(ws / ".grip", "config", "user.email", "g@e.invalid")
    _git(ws / ".grip", "config", "user.name", "g")
    _git(ws / ".grip", "commit", "-q", "--allow-empty", "-m", "init grip")

    origin = tmp_path / "app.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    src = ws / "repos" / "app"
    src.parent.mkdir(parents=True, exist_ok=True)
    _git(tmp_path, "clone", "-q", str(origin), str(src))
    _git(src, "config", "user.email", "t@e.invalid")
    _git(src, "config", "user.name", "t")
    (src / "f.txt").write_text("base\n")
    (src / ".gr2").mkdir()
    (src / ".gr2" / "hooks.toml").write_text(
        '[[files.link]]\nsrc = "does/not/exist.md"\ndest = "PROJECTED.md"\n'
    )
    _git(src, "add", ".")
    _git(src, "commit", "-q", "-m", "base with a blocked projection hook")
    _git(src, "push", "-q", "origin", "main")

    # The consent gate: bind the member so the projection block is what refuses, rather
    # than a consent skip that would refuse for a different reason and pass this witness
    # by accident.
    from gr2.python_cli.consent import write_consent

    write_consent(ws, "repos/app", src)
    (ws / ".grip" / "workspace_spec.toml").write_text(
        'schema_version = 1\nworkspace_name = "m"\n\n'
        f'[[repos]]\nname = "app"\npath = "repos/app"\nurl = "{origin}"\n\n'
        '[[units]]\nname = "atlas"\npath = "agents/atlas"\nrepos = ["app"]\n'
    )
    return ws


def test_a_kept_lane_says_so_and_names_both_ways_forward(tmp_path: Path) -> None:
    """LEGIBILITY on the kept path.

    A refusal that keeps the lane must SAY it kept it. A user who reads "create failed"
    and later finds a lane on disk would otherwise read it as the orphan this change
    exists to prevent, arriving from the other side.

    The message must carry both ways forward: the verb that continues with the recovered
    lane, and what removes it. It names no removal VERB because none exists (`lane` has
    create/enter/resolve/exit/current/bind), so it names the exact path instead and says
    so. A message naming a command the user cannot run is the defect this test would
    otherwise protect.
    """
    ws = _blocked_hook_workspace(tmp_path)
    lane_root = ws / ".grip" / "state" / "lanes" / "atlas" / "feature"

    result = runner.invoke(
        app,
        ["lane", "create", str(ws), "atlas", "feature", "--repos", "app", "--branch", "feat/lane"],
    )

    assert result.exit_code == 1, result.output
    combined = result.output + str(result.stderr)
    assert "KEPT" in combined, f"the kept path must say the lane was kept; got {combined!r}"
    assert "fork base is recorded" in combined, (
        f"the message must say WHY it is recoverable; got {combined!r}"
    )
    assert "review create-project" in combined, (
        f"the message must name the way to CONTINUE with the lane; got {combined!r}"
    )
    assert str(lane_root) in combined, (
        f"the message must name the exact path that removes it; got {combined!r}"
    )
    # The message must describe a state that EXISTS. The text assertions above can all
    # pass while the behaviour is wrong, so the lane's presence is asserted on the disk.
    assert lane_root.is_dir(), "the message says KEPT, so the lane must really be kept"


def test_the_path_the_kept_message_names_is_safe_to_remove(tmp_path: Path) -> None:
    """The kept-path advice must be SAFE, because it tells the user to remove a path by hand.

    Advice is a claim. The message names an exact path and says "delete it", so the two
    things a user does next must both work: `lane enter` must refuse the lane as ABSENT
    rather than trip on state the removal left behind, and a fresh `lane create` with the
    same name must then SUCCEED. If manual removal leaves anything under .grip that either
    verb trips over, the advice must say what else to remove -- or not be given at all.

    The re-create is made possible by repairing the blocking hook row, which is what a
    user following the refusal would do anyway; without that repair the second create
    would refuse for the same unrelated reason and would prove nothing about stale state.
    """
    ws = _blocked_hook_workspace(tmp_path)
    lane_root = ws / ".grip" / "state" / "lanes" / "atlas" / "feature"

    refused = runner.invoke(
        app,
        ["lane", "create", str(ws), "atlas", "feature", "--repos", "app", "--branch", "feat/lane"],
    )
    assert refused.exit_code == 1, refused.output
    assert lane_root.is_dir(), "precondition: the blocked-hook refusal KEEPS the lane"
    assert str(lane_root) in refused.output + str(refused.stderr), (
        "precondition: the message names exactly this path"
    )

    # --- the advice, followed literally ---
    shutil.rmtree(lane_root)
    assert not lane_root.exists()

    enter = runner.invoke(
        app, ["lane", "enter", str(ws), "atlas", "feature", "--actor", "agent:apollo"]
    )
    assert enter.exit_code != 0, (
        f"a manually removed lane must not be enterable; got {enter.exit_code}: {enter.output}"
    )
    assert "not found" in enter.output.lower(), (
        f"the refusal must say the lane is absent, not something else; got {enter.output!r}"
    )

    # --- repair the hook row, so a re-create can actually get through ---
    src = ws / "repos" / "app"
    (src / ".gr2" / "hooks.toml").write_text("")
    subprocess.run(["git", "-C", str(src), "add", "."], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(src), "commit", "-q", "-m", "remove the blocking row"],
        check=True,
        capture_output=True,
    )
    from gr2.python_cli.consent import write_consent

    write_consent(ws, "repos/app", src)

    again = runner.invoke(
        app,
        ["lane", "create", str(ws), "atlas", "feature", "--repos", "app", "--branch", "feat/lane"],
    )
    assert again.exit_code == 0, (
        f"a fresh create with the same name must succeed after the manual removal; "
        f"got {again.exit_code}: {again.output}"
    )
    assert lane_root.is_dir(), "the fresh create must own its lane directory again"


def _partial_lane_workspace(tmp_path: Path) -> Path:
    """A TWO-repo workspace whose FIRST source materializes and whose SECOND refuses.

    The fork base is recorded per repo as the materialization loop runs, so this state
    leaves a lane carrying a fork base for repo-a and none for repo-b. That is the
    partial-fork-base shape: existence of a fork base is not the discriminator, because
    this lane is exactly as unusable as one with none -- `lane enter` accepts it while
    `review create-project` refuses it as not materialized.

    repo-b's declared path is a directory holding a file, which is neither a repository
    nor an unfilled placeholder, so the loop refuses it with `run gr2 workspace
    materialize first`.
    """
    ws = tmp_path / "ws-partial"
    (ws / ".grip").mkdir(parents=True)
    origin_a = tmp_path / "repa.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin_a))
    src_a = ws / "repos" / "repo-a"
    src_a.parent.mkdir(parents=True)
    _git(tmp_path, "clone", "-q", str(origin_a), str(src_a))
    _git(src_a, "config", "user.email", "t@e.invalid")
    _git(src_a, "config", "user.name", "t")
    (src_a / "f.txt").write_text("base\n")
    _git(src_a, "add", ".")
    _git(src_a, "commit", "-q", "-m", "base")
    _git(src_a, "push", "-q", "origin", "HEAD:refs/heads/main")
    src_b = ws / "repos" / "repo-b"
    src_b.mkdir(parents=True)
    (src_b / "stray.txt").write_text("not a repository\n")
    (ws / ".grip" / "workspace_spec.toml").write_text(
        'schema_version = 1\nworkspace_name = "m"\n\n'
        f'[[repos]]\nname = "repo-a"\npath = "repos/repo-a"\nurl = "{origin_a}"\n\n'
        '[[repos]]\nname = "repo-b"\npath = "repos/repo-b"\nurl = "https://example.invalid/repo-b.git"\n\n'
        '[[units]]\nname = "atlas"\npath = "agents/atlas"\nrepos = ["repo-a", "repo-b"]\n'
    )
    return ws


def test_a_partial_fork_base_is_not_recoverable_and_the_lane_is_removed(tmp_path: Path) -> None:
    """A PARTIAL fork base must not buy the KEPT path.

    The fork base is recorded per repo, so a two-repo lane whose second source never
    materialized carries one for the first repo and none for the second. Keying on the
    EXISTENCE of a fork base keeps that lane and promises it is recoverable, while the
    verb the promise names refuses it: `review create-project` exits 2 with "repo repo-b
    is not materialized". The message's advice is false in exactly that state, which is
    the orphan class this change exists to remove.

    COVERAGE is the condition: every repo the lane document names must have a fork base
    entry. The lane must then be removed like any other orphan, and no message may
    promise a recovery that a verb will refuse.
    """
    ws = _partial_lane_workspace(tmp_path)
    lane_root = ws / ".grip" / "state" / "lanes" / "atlas" / "partial"

    create = runner.invoke(
        app,
        ["lane", "create", str(ws), "atlas", "partial", "--repos", "repo-a,repo-b", "--branch", "feat/x"],
    )

    assert create.exit_code != 0, (
        f"the create must refuse on the second, unmaterialized source; got {create.exit_code}: {create.output}"
    )
    combined = create.output + str(create.stderr)
    assert "KEPT" not in combined, (
        f"a lane whose fork base does not cover every repo is not recoverable and must not "
        f"promise that it is; got {combined!r}"
    )
    assert not lane_root.exists(), (
        f"a refused create must leave no lane on disk, and a partial fork base is not a "
        f"reason to keep one; but {lane_root} exists"
    )
    enter = _enter(ws, "partial")
    assert enter.exit_code != 0, (
        f"no verb may accept a lane that was never created; enter returned {enter.exit_code}: {enter.output}"
    )
    assert "not found" in enter.output.lower(), (
        f"the refusal must say the lane is absent; got {enter.output!r}"
    )


def test_a_create_whose_source_exists_still_creates_and_is_enterable(tmp_path: Path) -> None:
    """THE CONTROL. Without this, a build in which creation is simply broken satisfies
    the witness, and the witness would then be measuring the verb rather than the
    refusal path."""
    ws = _materialized_workspace(tmp_path)
    lane_root = ws / ".grip" / "state" / "lanes" / "atlas" / "real-lane"

    create = _create(ws, "real-lane")
    assert create.exit_code == 0, (
        f"an ordinary create must still succeed; got {create.exit_code}: {create.output}"
    )
    assert lane_root.is_dir(), f"the successful create must own its lane directory: {lane_root}"

    enter = _enter(ws, "real-lane")
    assert enter.exit_code == 0, (
        f"a created lane must be enterable; got {enter.exit_code}: {enter.output}"
    )
