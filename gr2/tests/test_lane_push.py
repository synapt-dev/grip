"""`gr2 lane push`: one command pushes every repo of a lane, each verified on its remote.

Before this verb the last per-member loop of a multi-repo change was the push: `gr2 push` is
single-repo by design, so a five-repo lane paid five pushes per delivery and five more after every
rework a reviewer asked for. `lane push` runs the SAME per-repo path (`push_current_branch`: remote
choice, detached-HEAD refusal, `ls-remote` proof that the remote ref equals the head), so
single-repo push and lane push cannot drift.

THE RULE, with each edge decided here:
  * every lane repo is pushed; one line per repo says `pushed`, `up to date` or `FAILED`;
  * a repo whose push FAILS (a rejected non-fast-forward, a refused lease) exits 1, and the repos
    that landed are not rolled back: pushes to N remotes cannot be atomic, so the promise is "all
    or a named partial";
  * exit 3 only when every push succeeded and a head is still not on the lane's branch at its
    remote (a repo checked out on another branch pushes that branch fine; the lane's branch never
    gets the head, and the store's root push would later refuse the pin);
  * `--force-with-lease` is a lease on each repo's own last-fetched remote head, never a plain
    force: a remote that moved since the lane last fetched refuses.

Fixtures are real git with bare local origins and no network.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from gr2.prototypes import lane_workspace_prototype as lanes
from gr2.python_cli import app as gr2_app
from tests.test_lane_create_fork_base_cli import _workspace, runner

REPOS = ["alpha", "beta", "gamma"]
BRANCH = "feat/lane"


def _git(cwd: Path, *a: str) -> str:
    done = subprocess.run(["git", *a], cwd=cwd, text=True, capture_output=True, check=True)
    return done.stdout.strip()


def _lane(tmp_path: Path) -> tuple[Path, dict[str, Path]]:
    """A three-repo lane on `feat/lane`, one unpushed commit in each repo."""
    ws, _tips = _workspace(tmp_path, REPOS)
    made = runner.invoke(gr2_app.app, ["lane", "create", str(ws), "atlas", "feature",
                                       "--repos", ",".join(REPOS), "--branch", BRANCH])
    assert made.exit_code == 0, made.output
    repos = {r: lanes.lane_repo_root(ws, "atlas", "feature", r) for r in REPOS}
    for name, repo in repos.items():
        _git(repo, "config", "user.email", "t@e.invalid")
        _git(repo, "config", "user.name", "t")
        _commit(repo, f"{name}.txt", "work\n")
    return ws, repos


def _commit(repo: Path, name: str, text: str) -> str:
    (repo / name).write_text(text)
    _git(repo, "add", name)
    _git(repo, "commit", "-q", "-m", f"add {name}")
    return _git(repo, "rev-parse", "HEAD")


def _remote_sha(tmp_path: Path, repo: str, branch: str = BRANCH) -> str:
    bare = str(tmp_path / f"{repo}.git")
    out = _git(tmp_path, "ls-remote", "--heads", bare, f"refs/heads/{branch}")
    return out.split()[0] if out else ""


def _push(ws: Path, *extra: str):
    return runner.invoke(gr2_app.app, ["lane", "push", str(ws), "atlas", "feature", *extra])


def _move_remote(tmp_path: Path, repo: str) -> str:
    """Someone else pushes a commit to the repo's `feat/lane` from their own clone."""
    other = tmp_path / f"other-{repo}"
    _git(tmp_path, "clone", "-q", "-b", BRANCH, str(tmp_path / f"{repo}.git"), str(other))
    _git(other, "config", "user.email", "o@e.invalid")
    _git(other, "config", "user.name", "o")
    sha = _commit(other, "theirs.txt", "theirs\n")
    _git(other, "push", "-q", "origin", BRANCH)
    return sha


def test_one_command_pushes_every_repo_and_the_remotes_hold_the_heads(tmp_path: Path) -> None:
    ws, repos = _lane(tmp_path)
    result = _push(ws)
    assert result.exit_code == 0, result.output
    for name, repo in repos.items():
        head = _git(repo, "rev-parse", "HEAD")
        # the remote is read here, not taken from the verb's exit
        assert _remote_sha(tmp_path, name) == head, name
        assert f"{name}: pushed {BRANCH} to origin at {head}" in result.output, result.output


def test_a_second_run_reports_up_to_date_and_sends_nothing(tmp_path: Path) -> None:
    ws, repos = _lane(tmp_path)
    assert _push(ws).exit_code == 0
    before = {name: _remote_sha(tmp_path, name) for name in repos}
    again = _push(ws)
    assert again.exit_code == 0, again.output
    for name, repo in repos.items():
        head = _git(repo, "rev-parse", "HEAD")
        assert f"{name}: up to date at {head}" in again.output, again.output
        assert _remote_sha(tmp_path, name) == before[name]
    assert "pushed" not in again.output, again.output


def test_up_to_date_does_not_depend_on_the_language_git_speaks(tmp_path: Path, monkeypatch) -> None:
    """A git that prints its messages in another language still reports a rerun as up to date.

    git translates "Everything up-to-date" under a non-English locale, so the label cannot read
    that sentence. A shim on PATH stands in for a translated git: it runs the real git and
    rewrites the English sentence on stderr, which is what a German git prints instead.
    """
    import os
    import shutil
    import stat

    ws, repos = _lane(tmp_path)
    assert _push(ws).exit_code == 0
    real = shutil.which("git")
    shim = tmp_path / "shim"
    shim.mkdir()
    script = shim / "git"
    script.write_text(
        "#!/bin/sh\n"
        f"{real} \"$@\" 2>{tmp_path}/err; rc=$?\n"
        f"sed 's/Everything up-to-date/Alles ist aktuell/' {tmp_path}/err >&2\n"
        "exit $rc\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{shim}{os.pathsep}{os.environ['PATH']}")
    again = _push(ws)
    assert again.exit_code == 0, again.output
    for name, repo in repos.items():
        head = _git(repo, "rev-parse", "HEAD")
        assert f"{name}: up to date at {head}" in again.output, again.output
    assert "pushed" not in again.output, again.output


def test_a_rejected_push_exits_1_and_names_it_while_the_others_land(tmp_path: Path) -> None:
    ws, repos = _lane(tmp_path)
    assert _push(ws).exit_code == 0
    theirs = _move_remote(tmp_path, "beta")
    for repo in repos.values():
        _commit(repo, "second.txt", "second\n")
    result = _push(ws)
    assert result.exit_code == 1, result.output
    assert "beta: FAILED" in result.output, result.output
    assert "[rejected]" in result.output, result.output  # git's per-ref reason, not only its hints
    assert _remote_sha(tmp_path, "beta") == theirs  # nothing overwritten
    for name in ("alpha", "gamma"):
        head = _git(repos[name], "rev-parse", "HEAD")
        assert _remote_sha(tmp_path, name) == head, name
        assert f"{name}: pushed {BRANCH} to origin at {head}" in result.output, result.output


def test_every_push_succeeds_but_a_head_off_the_lane_branch_exits_3(tmp_path: Path) -> None:
    ws, repos = _lane(tmp_path)
    gamma = repos["gamma"]
    _git(gamma, "checkout", "-q", "-b", "feat/other")
    head = _commit(gamma, "other.txt", "other\n")
    result = _push(ws, "-u")
    assert result.exit_code == 3, result.output
    # the push itself landed (feat/other has the head) ...
    assert _remote_sha(tmp_path, "gamma", "feat/other") == head
    # ... but the lane's branch does not, and the line says so in the store's words
    said = f"gamma head {head} is not on origin/{BRANCH}; push it first"
    assert said in result.output, result.output
    for name in ("alpha", "beta"):
        assert _remote_sha(tmp_path, name) == _git(repos[name], "rev-parse", "HEAD")


def test_a_failed_push_outranks_an_uncovered_head(tmp_path: Path) -> None:
    ws, repos = _lane(tmp_path)
    assert _push(ws).exit_code == 0
    _move_remote(tmp_path, "beta")
    _commit(repos["beta"], "second.txt", "second\n")
    _git(repos["gamma"], "checkout", "-q", "-b", "feat/other")
    _commit(repos["gamma"], "other.txt", "other\n")
    result = _push(ws, "-u")
    assert result.exit_code == 1, result.output
    assert "beta: FAILED" in result.output and "gamma head" in result.output, result.output


def test_force_with_lease_rewrites_its_own_head_but_refuses_a_moved_remote(tmp_path: Path) -> None:
    ws, repos = _lane(tmp_path)
    assert _push(ws).exit_code == 0
    # alpha: amend (a rewrite) with the remote unmoved: the lease holds and the rewrite lands
    _git(repos["alpha"], "commit", "-q", "--amend", "-m", "reworded")
    rewritten = _git(repos["alpha"], "rev-parse", "HEAD")
    # beta: the remote moved since the lane last fetched; amend; the lease must refuse
    theirs = _move_remote(tmp_path, "beta")
    _git(repos["beta"], "commit", "-q", "--amend", "-m", "reworded")
    result = _push(ws, "--force-with-lease")
    assert result.exit_code == 1, result.output
    assert _remote_sha(tmp_path, "alpha") == rewritten
    assert "beta: FAILED" in result.output, result.output
    assert _remote_sha(tmp_path, "beta") == theirs, "a lease is never a plain force"


def test_json_prints_one_row_per_repo(tmp_path: Path) -> None:
    ws, repos = _lane(tmp_path)
    result = _push(ws, "--json")
    assert result.exit_code == 0, result.output
    rows = json.loads(result.output)["repos"]
    assert [r["repo"] for r in rows] == REPOS
    for row in rows:
        assert row["status"] == "pushed"
        assert row["sha"] == _git(repos[row["repo"]], "rev-parse", "HEAD")
        assert row["branch"] == BRANCH and row["remote"] == "origin"


def test_coverage_reuses_the_push_receipt_with_no_extra_round_trip(
    tmp_path: Path, monkeypatch
) -> None:
    """A repo that pushed the lane's own branch is covered by the push's `ls-remote` proof: the
    coverage check asks the remote nothing more (one `ls-remote` per repo, all from the push)."""
    from gr2.python_cli import push as push_ops

    ws, _repos = _lane(tmp_path)
    calls: list[tuple[str, ...]] = []
    real = push_ops.git

    def counting(repo, *args, **kw):
        calls.append(args)
        return real(repo, *args, **kw)

    monkeypatch.setattr(push_ops, "git", counting)
    assert _push(ws).exit_code == 0
    assert sum(1 for a in calls if a[:1] == ("ls-remote",)) == len(REPOS), calls
    assert not any(a[:1] == ("fetch",) for a in calls), calls


def test_a_review_ephemeral_lane_refuses_and_pushes_nothing(tmp_path: Path) -> None:
    """A review lane is read-only and disposable: it never becomes a work lane, so it is never pushed."""
    ws, _repos = _lane(tmp_path)
    doc = lanes.lane_file(ws, "atlas", "feature")
    text = doc.read_text()
    assert text.count('lane_kind = "') == 1, text  # the fixture's precondition: one kind line to rewrite
    head, _, rest = text.partition('lane_kind = "')
    doc.write_text(head + 'lane_kind = "review-ephemeral"' + rest[rest.index('"'):][1:])
    result = _push(ws)
    assert result.exit_code == 1, result.output
    assert "review-ephemeral lane" in result.output and "cannot be pushed" in result.output, result.output
    for name in REPOS:
        assert _remote_sha(tmp_path, name) == "", name


def _gamma_off_branch_behind_the_remote(tmp_path: Path, ws: Path, repos: dict[str, Path]) -> tuple[str, str]:
    """gamma's head is on the remote lane branch, a teammate has pushed past it, and gamma has not fetched.
    gamma then pushes from another branch, so coverage asks about a lane tip this clone does not hold."""
    assert _push(ws).exit_code == 0
    head = _git(repos["gamma"], "rev-parse", "HEAD")
    theirs = _move_remote(tmp_path, "gamma")
    _git(repos["gamma"], "checkout", "-q", "-b", "feat/other")
    probe = subprocess.run(["git", "cat-file", "-e", theirs], cwd=repos["gamma"], capture_output=True)
    assert probe.returncode != 0, "the fixture needs the teammate's tip absent from gamma's clone"
    return head, theirs


def test_a_head_behind_a_lane_tip_this_clone_has_not_fetched_is_covered(tmp_path: Path) -> None:
    """The remote lane branch holds the head under a teammate's newer commit. The coverage question reads
    ancestry against that tip, so the tip is fetched (that ref only) rather than the head called uncovered."""
    ws, repos = _lane(tmp_path)
    head, theirs = _gamma_off_branch_behind_the_remote(tmp_path, ws, repos)
    result = _push(ws, "-u")
    assert result.exit_code == 0, result.output
    assert "gamma head" not in result.output and "cannot tell" not in result.output, result.output
    assert _remote_sha(tmp_path, "gamma", "feat/other") == head
    assert _remote_sha(tmp_path, "gamma") == theirs, "coverage reads the lane branch; it never writes it"


def test_a_lane_tip_that_cannot_be_fetched_says_cannot_tell(tmp_path: Path, monkeypatch) -> None:
    """When the missing tip cannot be fetched, the line says the coverage could not be read, never "push it
    first", and the verb exits 3: success is not proven."""
    from gr2.python_cli import push as push_ops

    ws, repos = _lane(tmp_path)
    head, _theirs = _gamma_off_branch_behind_the_remote(tmp_path, ws, repos)
    real = push_ops.git

    def no_fetch(repo, *args, **kw):
        if args[:1] == ("fetch",):
            return subprocess.CompletedProcess(args, 128, "", "fatal: unable to access remote")
        return real(repo, *args, **kw)

    monkeypatch.setattr(push_ops, "git", no_fetch)
    result = _push(ws, "-u")
    assert result.exit_code == 3, result.output
    said = f"gamma cannot tell whether head {head} is on origin/{BRANCH}: its tip could not be fetched"
    assert said in result.output and "push it first" not in result.output, result.output


# A rejected push prints ONE sentence first, parsed from git's porcelain '!' line; under it, indented,
# git's hint lines, then git's other lines verbatim. Only the bare "Done" line (porcelain's end
# marker) is dropped; the exit code and the --json error text are otherwise unchanged.


def _rejected_beta(tmp_path: Path, *extra: str):
    ws, repos = _lane(tmp_path)
    assert _push(ws).exit_code == 0
    _move_remote(tmp_path, "beta")
    for repo in repos.values():
        _commit(repo, "second.txt", "second\n")
    return _push(ws, *extra)


def _failed_block(output: str, repo: str) -> list[str]:
    """The FAILED line for `repo` and the indented lines under it."""
    lines = output.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith(f"{repo}: FAILED, "))
    block = [lines[start]]
    for line in lines[start + 1:]:
        if not line.startswith("  "):
            break
        block.append(line)
    return block


def test_an_all_pushed_lane_prints_only_the_pushed_lines(tmp_path: Path) -> None:
    ws, repos = _lane(tmp_path)
    result = _push(ws)
    assert result.exit_code == 0, result.output
    assert result.output.splitlines() == [
        f"{name}: pushed {BRANCH} to origin at {_git(repo, 'rev-parse', 'HEAD')} (remote ref verified)"
        for name, repo in repos.items()
    ]


def test_a_rejected_push_says_one_sentence_then_git_hints_indented(tmp_path: Path) -> None:
    result = _rejected_beta(tmp_path)
    assert result.exit_code == 1, result.output
    block = _failed_block(result.output, "beta")
    assert block[0] == f"beta: FAILED, rejected: {BRANCH} on origin (fetch first)", block
    under = block[1:]
    assert "Done" not in [line.strip() for line in under], block
    hints = [i for i, line in enumerate(under) if line.startswith("  hint: ")]
    assert hints and hints == list(range(len(hints))), block  # the hints come first, indented
    assert any(line.startswith("  error: failed to push some refs") for line in under), block
    assert any("\t[rejected] (fetch first)" in line for line in under), block  # git's own line kept


def test_done_inside_the_remote_path_survives(tmp_path: Path) -> None:
    where = tmp_path / "Done"
    where.mkdir()
    result = _rejected_beta(where)
    assert result.exit_code == 1, result.output
    block = _failed_block(result.output, "beta")
    assert block[0] == f"beta: FAILED, rejected: {BRANCH} on origin (fetch first)", block
    assert f"  To {where / 'beta.git'}" in block, block
    assert "  Done" not in block, block


def test_two_rejected_pushes_each_say_their_sentence(tmp_path: Path) -> None:
    ws, repos = _lane(tmp_path)
    assert _push(ws).exit_code == 0
    _move_remote(tmp_path, "beta")
    _move_remote(tmp_path, "gamma")
    for repo in repos.values():
        _commit(repo, "second.txt", "second\n")
    result = _push(ws)
    assert result.exit_code == 1, result.output
    for name in ("beta", "gamma"):
        assert _failed_block(result.output, name)[0] == (
            f"{name}: FAILED, rejected: {BRANCH} on origin (fetch first)"
        ), result.output
    assert "Done" not in [line.strip() for line in result.output.splitlines()], result.output


def test_json_error_keeps_git_text_but_the_done_line(tmp_path: Path) -> None:
    result = _rejected_beta(tmp_path, "--json")
    assert result.exit_code == 1, result.output
    beta = next(r for r in json.loads(result.output)["repos"] if r["repo"] == "beta")
    error = beta["error"].splitlines()
    assert beta["status"] == "failed" and "Done" not in error, error
    assert error[0] == f"rejected: {BRANCH} on origin (fetch first)", error
    assert any(line.startswith("hint: ") for line in error), error
    assert any(line.startswith("To ") and line.endswith("beta.git") for line in error), error
    assert any(line.startswith("error: failed to push some refs") for line in error), error
    assert any(line.startswith("!\t") and line.endswith("[rejected] (fetch first)") for line in error), error


def test_help_says_what_a_failure_prints(tmp_path: Path) -> None:
    result = runner.invoke(gr2_app.app, ["lane", "push", "--help"])
    assert result.exit_code == 0, result.output
    said = " ".join(result.output.replace("│", " ").split())
    assert "Under a FAILED line, indented, git's hint lines" in said, said
