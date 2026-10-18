"""The `tracking_stale` command a merge prints runs as printed, whatever the target branch is called.

THE DEFECT, measured on gitgrip dev f8cb2d16: the command was built by f-string with every value bare. `;` is legal in
a refname, so a merge into `x;touch${IFS}<file>` printed a line that, run with bash as printed, created `<file>`.

THE RULE: every value in the command (clone path, remote, refspec) is shell-quoted whole with `next_steps._shown`,
the rule every other printed line uses, so the line is one git command and nothing else.
"""
from __future__ import annotations

import json
import shlex
import subprocess
from types import SimpleNamespace

import pytest

from typer.testing import CliRunner

from gr2.python_cli import app as app_mod, approvals, merge_gate, read_review
from gr2.python_cli.app import app
from tests.test_review_merge_gate import _git_identity, check, git, world  # noqa: F401
from tests.test_review_merge_updates_tracking_ref import _respell_origin

runner = CliRunner()


def _bash(command):
    return subprocess.run(["bash", "-c", command], capture_output=True, text=True)


# The marker's absolute path goes into a refname through this variable, never as text: a temporary path can hold a
# component that starts with `.` (~/.synapt/...), and git refuses such a refname, so the fixture itself failed there.
# `$`, `{` and `}` are legal in a refname, and bash expands the variable when a pasted line runs it unquoted.
MARK_VAR = "${GR2_TEST_MARK}"


def _mark(tmp_path, monkeypatch):
    """The marker file, its path in GR2_TEST_MARK for the whole test, so gr2's own calls see it too."""
    mark = tmp_path / "MARK"
    monkeypatch.setenv("GR2_TEST_MARK", str(mark))
    return mark


def _prove_armed(text, mark, cwd=None):
    """Run the armed text once UNQUOTED and see MARK appear, then remove it: without this, a fixture that lost its
    GR2_TEST_MARK would run a bare `touch`, create nothing, and every "no marker" assertion would pass vacuously."""
    subprocess.run(["bash", "-c", text], capture_output=True, text=True, cwd=cwd)
    assert mark.exists(), f"the armed text did not create the marker: {text}"
    mark.unlink()


def _armed(tmp_path, world, monkeypatch):
    """A target branch whose name is a shell command that would create MARK, made on both remotes."""
    mark = _mark(tmp_path, monkeypatch)
    into = "x;touch${IFS}" + MARK_VAR
    _prove_armed(into, mark)
    base = git(world["remote"], "rev-parse", "main")
    git(world["remote"], "update-ref", f"refs/heads/{into}", base)
    return mark, into, base


def test_a_semicolon_target_prints_a_command_that_runs_with_no_marker(world, tmp_path, monkeypatch):
    mark, into, base = _armed(tmp_path, world, monkeypatch)
    other = tmp_path / "other.git"
    git(tmp_path, "clone", "--bare", "-q", world["remote"], other)
    _respell_origin(world, str(other))  # no remote of the clone names the member url: the path at _refresh_tracking
    check(world)

    result = runner.invoke(app, ["review", "merge", "--approvals", "0", str(world["author"]), world["review"],
                                 "--into", into, "--from", "feat"])

    assert result.exit_code == 0, result.output
    stale = json.loads(result.stdout.splitlines()[0])["tracking_stale"]
    command = stale[0]["command"]
    assert command in result.stderr, result.stderr
    assert not mark.exists(), "precondition: gr2 itself ran nothing"
    ran = _bash(command)
    assert not mark.exists(), command
    assert ran.returncode == 0, ran.stderr
    assert git(world["member"], "rev-parse", f"refs/remotes/origin/{into}") == git(world["remote"], "rev-parse", into)


def _clone_with_metachars(tmp_path, monkeypatch):
    """A clone, its remote and a member url whose paths each hold `;`, so every value in the command is armed."""
    mark = _mark(tmp_path, monkeypatch)
    arm = "a;touch${IFS}" + MARK_VAR + ";b"
    _prove_armed(arm, mark)
    remote = tmp_path / f"remote{arm}.git"
    seed = tmp_path / "seed"
    git(tmp_path, "init", "--bare", "-q", "-b", "main", remote)
    seed.mkdir()
    git(seed, "init", "-q", "-b", "main")
    git(seed, "commit", "-q", "--allow-empty", "-m", "base")
    into = "x" + arm
    git(seed, "push", "-q", remote, f"main:refs/heads/{into}")
    repo = tmp_path / f"clone{arm}"
    git(tmp_path, "clone", "-q", remote, repo)
    git(seed, "commit", "-q", "--allow-empty", "-m", "merged")  # the remote moves on, as a merge moves it
    git(seed, "push", "-q", remote, f"main:refs/heads/{into}")
    return mark, into, remote, repo, arm


def test_every_value_of_a_no_matching_remote_command_is_quoted(tmp_path, _git_identity, monkeypatch):
    mark, into, remote, repo, arm = _clone_with_metachars(tmp_path, monkeypatch)
    elsewhere = tmp_path / f"elsewhere{arm}.git"

    failed = merge_gate._refresh_tracking(repo, str(elsewhere), into)

    assert failed["reason"] == f"no remote of this clone names {elsewhere}", failed
    assert shlex.split(failed["command"]) == ["git", "-C", str(repo), "fetch", "--no-tags", "--refmap=", "--", str(elsewhere),
                                              f"+refs/heads/{into}:refs/remotes/origin/{into}"]
    _bash(failed["command"])
    assert not mark.exists(), failed["command"]


def test_every_value_of_a_failed_fetch_command_is_quoted(tmp_path, _git_identity, monkeypatch):
    mark, into, remote, repo, _ = _clone_with_metachars(tmp_path, monkeypatch)
    _prove_armed("o;touch${IFS}" + MARK_VAR, mark)
    git(repo, "remote", "rename", "origin", "o;touch${IFS}" + MARK_VAR)
    name = git(repo, "remote").strip()
    lock = repo / ".git" / "refs" / "remotes" / name / f"{into}.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text("")
    try:
        failed = merge_gate._refresh_tracking(repo, str(remote), into)
    finally:
        lock.unlink()

    assert failed["reason"] == "the fetch failed", failed
    assert shlex.split(failed["command"]) == ["git", "-C", str(repo), "fetch", "--no-tags", "--refmap=", "--", name,
                                              f"+refs/heads/{into}:refs/remotes/{name}/{into}"]
    ran = _bash(failed["command"])
    assert not mark.exists(), failed["command"]
    assert ran.returncode == 0, ran.stderr


def test_an_unreadable_clone_names_a_quoted_spec(world, tmp_path, monkeypatch):
    mark, into, _ = _armed(tmp_path, world, monkeypatch)
    check(world)

    def unreadable(*_args):
        raise OSError("probe: unreadable")
    monkeypatch.setattr(merge_gate, "_refresh_tracking", unreadable)
    code, receipt = merge_gate.review_merge(world["author"], world["review"], into=into, feature="feat",
                                            required_approvals=0)

    assert code == merge_gate.EXIT_MERGED, receipt
    stale = receipt["tracking_stale"]
    assert stale[0]["reason"] == "the clone could not be read: probe: unreadable", stale
    command = stale[0]["command"].replace("<remote>", "origin", 1)
    assert shlex.split(command) == ["git", "-C", str(world["member"].resolve()), "fetch", "--no-tags", "origin",
                                    f"+refs/heads/{into}:refs/remotes/<remote>/{into}"]
    _bash(command)
    assert not mark.exists(), command


def test_control_a_plain_target_prints_the_same_git_command_as_before(world, tmp_path):
    other = tmp_path / "other.git"
    git(tmp_path, "clone", "--bare", "-q", world["remote"], other)
    _respell_origin(world, str(other))
    check(world)

    code, receipt = merge_gate.review_merge(world["author"], world["review"], into="main", feature="feat",
                                            required_approvals=0)

    assert code == merge_gate.EXIT_MERGED, receipt
    command = receipt["tracking_stale"][0]["command"]
    assert shlex.split(command) == ["git", "-C", str(world["member"].resolve()), "fetch", "--no-tags", "--refmap=", "--",
                                    str(world["remote"]), "+refs/heads/main:refs/remotes/origin/main"]
    assert _bash(command).returncode == 0
    assert git(world["member"], "rev-parse", "origin/main") == git(world["remote"], "rev-parse", "main")


def test_an_option_shaped_url_is_never_read_as_an_option(tmp_path, _git_identity, monkeypatch):
    mark, into, remote, repo, _ = _clone_with_metachars(tmp_path, monkeypatch)
    url = f"--upload-pack=touch {mark}"
    subprocess.run(["git", "-C", str(repo), "fetch", url, str(remote)], capture_output=True, text=True)
    assert mark.exists(), f"git did not read the url as an option: {url}"
    mark.unlink()

    failed = merge_gate._refresh_tracking(repo, url, into)

    argv = shlex.split(failed["command"])
    assert argv[argv.index(url) - 1] == "--", failed["command"]
    _bash(failed["command"])
    assert not mark.exists(), failed["command"]


class TestAClonePathHoldingAShellCommand:
    """The world is built under a directory whose name is a shell command, so the clone path at the unreadable-clone
    line holds it too."""

    @pytest.fixture
    def tmp_path(self, tmp_path_factory):
        armed = tmp_path_factory.mktemp("w") / "a;touch${IFS}MARK;b"
        armed.mkdir()
        _prove_armed(f"true {armed}", armed.parent / "MARK", cwd=armed.parent)
        return armed

    def test_the_unreadable_clone_line_names_that_exact_path_and_runs_with_no_marker(self, world, tmp_path,
                                                                                    monkeypatch):
        mark = tmp_path.parent / "MARK"  # the line is run from beside the armed directory
        check(world)

        def unreadable(*_args):
            raise OSError("probe: unreadable")
        monkeypatch.setattr(merge_gate, "_refresh_tracking", unreadable)
        code, receipt = merge_gate.review_merge(world["author"], world["review"], into="main", feature="feat",
                                                required_approvals=0)

        assert code == merge_gate.EXIT_MERGED, receipt
        command = receipt["tracking_stale"][0]["command"].replace("<remote>", "origin", 1)
        member = str(world["member"].resolve())
        assert ";touch${IFS}" in member
        assert shlex.split(command)[:3] == ["git", "-C", member], command
        ran = subprocess.run(["bash", "-c", command], capture_output=True, text=True, cwd=tmp_path.parent)
        assert not mark.exists(), command
        assert ran.returncode == 0, ran.stderr


def _armed_value(tmp_path, name):
    mark = tmp_path / f"MARK{name}"
    armed = f"{name};touch " + str(mark)
    _prove_armed(armed, mark)
    return mark, armed


def test_a_partial_lane_names_the_record_workspace_and_review_quoted(tmp_path, monkeypatch):
    mark, armed = _armed_value(tmp_path, "ws")
    workspace = tmp_path / armed
    workspace.mkdir(parents=True)  # the name holds the marker path, so its slashes nest
    rid = "abc;touch " + str(mark)
    _prove_armed(rid, mark)
    monkeypatch.setattr(merge_gate, "_toplevel", lambda p: p)
    monkeypatch.setattr(merge_gate, "_store_inside", lambda *_a: True)
    monkeypatch.setattr(approvals.grip, "verify_review_commit", lambda *_a: {"tree_matches": True, "rows": [{"key": "k"}]})
    monkeypatch.setattr(approvals.grip, "show_review_commit", lambda *_a: {"author": "a", "members": [{"key": "k"}]})

    with pytest.raises(approvals.ApprovalRefused) as refused:
        approvals._context(workspace, rid, member_dirs={})

    line = refused.value.next_step.split("stamp that lane: ", 1)[1].split(" --lane-dir", 1)[0]
    assert shlex.split(line) == ["gr2", "review", "open", str(workspace.resolve()), f"gr:{rid}"]
    _bash(line)
    assert not mark.exists(), line


@pytest.mark.parametrize("state", ["changed", "unbound"])
def test_a_hooks_trust_line_names_the_spec_member_quoted(tmp_path, monkeypatch, state):
    mark, name = _armed_value(tmp_path, "m")
    workspace = tmp_path / "ws"
    (workspace / ".grip").mkdir(parents=True)
    (workspace / ".grip" / "workspace_spec.toml").write_text("")
    target = SimpleNamespace(name=name, path=tmp_path / "member")
    monkeypatch.setattr(app_mod, "repo_proto", SimpleNamespace(
        read_workspace_spec=lambda *_a: {}, read_policy=lambda *_a: {}, derive_targets=lambda *_a: [target],
        inspect_repo=lambda *_a: None, policy_for=lambda *_a: None, classify=lambda *_a: None,
        render_table=lambda *_a: ""))
    monkeypatch.setattr(app_mod, "load_repo_hooks", lambda *_a: {"hook": 1})
    monkeypatch.setattr(app_mod, "consent_member_key", lambda *_a: "k")
    monkeypatch.setattr(app_mod, "consent_state", lambda *_a: (state, None))
    monkeypatch.setattr(app_mod, "hooks_sha", lambda *_a: "0" * 40)

    result = runner.invoke(app, ["repo", "status", str(workspace)])

    assert result.exit_code == 0, result.output
    line = result.output.split("bind: ", 1)[1].splitlines()[0]
    assert shlex.split(line) == ["gr2", "hooks", "trust", name], line
    _bash(line)
    assert not mark.exists(), line


def test_a_read_pr_diff_line_names_the_pr_values_quoted(tmp_path, monkeypatch):
    mark, armed = _armed_value(tmp_path, "pr")
    path = tmp_path / armed
    opened = read_review.ReadReview(path=path, url="u", head="h;touch " + str(mark), base="b$(touch " + str(mark) + ")",
                                    stat="")
    _prove_armed(opened.head, mark)
    _prove_armed(opened.base, mark)
    monkeypatch.setattr(read_review, "open_read_review", lambda *_a, **_k: opened)

    result = runner.invoke(app, ["review", "read-pr", "o/r", "1", "--dir", str(path)])

    assert result.exit_code == 0, result.output
    line = result.output.split("Read the full diff: ", 1)[1].splitlines()[0]
    assert shlex.split(line) == ["git", "-C", str(path), "diff", f"{opened.base}...{opened.head}"], line
    _bash(line)
    assert not mark.exists(), line
