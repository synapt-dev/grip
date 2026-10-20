"""`gr2 change`: one change across members in one command.

c1 is the happy path at N=5; c3 rows are phase-0 refusals, which must leave
every member byte-equal (branch, HEAD, index, worktree) and run no git verb but
status and rev-parse.
Git calls are counted with a PATH shim, per member.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from gr2.python_cli.app import app

N = 5
WRITES = ("checkout", "add", "commit", "update-ref", "reset", "branch")
READS = ("status", "rev-parse")


def _git(path: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=path, check=True, capture_output=True, text=True).stdout


def _default_sigint() -> None:
    """preexec_fn for a gr2 child a row signals: SIGINT back to the default handler.

    A launcher that starts the suite with SIGINT ignored (a detached or non-interactive run) hands that
    ignore down to every child, and gr2 keeps an inherited ignore, so the row's SIGINT would never land.
    """
    signal.signal(signal.SIGINT, signal.SIG_DFL)


def _member(path: Path) -> None:
    path.mkdir(parents=True)
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.name", "Test")
    _git(path, "config", "user.email", "test@example.com")
    (path / "README.md").write_text("# m\n")
    _git(path, "add", "README.md")
    _git(path, "commit", "-q", "-m", "initial")


def _edit(path: Path) -> None:
    (path / "README.md").write_text(f"# {path.name} edited\n")
    (path / "new.txt").write_text("new\n")


def _state(path: Path) -> tuple[str, ...]:
    return (
        _git(path, "rev-parse", "--symbolic-full-name", "HEAD").strip(),
        _git(path, "rev-parse", "HEAD").strip(),
        _git(path, "ls-files", "-s"),
        _git(path, "status", "--porcelain=v1", "-uall"),
        (path / "README.md").read_text(),
        _git(path, "for-each-ref", "refs/heads"),
    )


@pytest.fixture
def members(tmp_path: Path) -> list[Path]:
    paths = [tmp_path / f"m{i}" for i in range(1, N + 1)]
    for path in paths:
        _member(path)
        _edit(path)
    return paths


@pytest.fixture
def git_log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    real = shutil.which("git")
    shim_dir = tmp_path / "shim"
    shim_dir.mkdir()
    log = tmp_path / "git.log"
    shim = shim_dir / "git"
    shim.write_text(f'#!/bin/sh\nprintf "%s\\t%s\\n" "$(basename "$PWD")" "$*" >> "{log}"\nexec "{real}" "$@"\n')
    shim.chmod(0o755)
    monkeypatch.setenv("PATH", f"{shim_dir}{os.pathsep}{os.environ['PATH']}")
    log.write_text("")
    return log


def _calls(log: Path) -> list[tuple[str, str]]:
    return [tuple(line.split("\t", 1)) for line in log.read_text().splitlines()]


def _not_reads(log: Path) -> list[tuple[str, str]]:
    """Every logged call whose verb is not a read: a refusal runs only status and rev-parse, nothing else."""
    return [(m, a) for m, a in _calls(log) if _verb(a) not in READS]


def _verb(argv: str) -> str:
    """The git verb, past the global options gitops puts first (`-c key=value` pairs)."""
    words = argv.split(" ")
    i = 0
    while i < len(words) and words[i].startswith("-"):
        i += 2 if words[i] == "-c" else 1
    return words[i] if i < len(words) else ""


def _run(members: list[Path], *extra: str):
    args = ["change", "feat", "-m", "one change", *extra]
    for path in members:
        args += ["--repo-path", str(path)]
    return CliRunner().invoke(app, args)


def test_c1_one_change_commits_every_member(members: list[Path], git_log: Path) -> None:
    bases = [_git(p, "rev-parse", "HEAD").strip() for p in members]
    git_log.write_text("")
    result = _run(members)
    calls = _calls(git_log)
    assert result.exit_code == 0, result.output
    for path, base in zip(members, bases):
        assert _git(path, "branch", "--show-current").strip() == "feat"
        assert _git(path, "rev-parse", "HEAD~1").strip() == base
        assert _git(path, "show", "HEAD:README.md") == f"# {path.name} edited\n"
        assert _git(path, "show", "HEAD:new.txt") == "new\n"
        assert _git(path, "status", "--porcelain") == ""
        head = _git(path, "rev-parse", "HEAD").strip()
        assert f"{path.name}: feat {head[:7]} " in result.output
    per_member = {p.name: sum(1 for m, _ in calls if m == p.name) for p in members}
    print(f"git calls per member: {per_member} total {sum(per_member.values())}")
    # Three reads and three writes: what plain git needs for the same refusals.
    assert all(n == 6 for n in per_member.values()), per_member
    assert sum(per_member.values()) == 30, per_member


def _detached(path: Path) -> None:
    # The worktree edit carries over; `git switch -c` from here would branch off a commit no branch names.
    _git(path, "checkout", "-q", "--detach")
    assert _git(path, "branch", "--show-current").strip() == ""


def _unresolved(path: Path) -> None:
    # Stages 1-3 for one path with no merge in progress: status lists it as unmerged ("u ").
    blobs = [subprocess.run(["git", "hash-object", "-w", "--stdin"], cwd=path, input=t, capture_output=True,
                            text=True, check=True).stdout.strip() for t in ("base\n", "ours\n", "theirs\n")]
    info = "".join(f"100644 {b} {i}\tconflict.txt\n" for i, b in enumerate(blobs, 1))
    subprocess.run(["git", "update-index", "--index-info"], cwd=path, input=info, text=True, check=True)
    (path / "conflict.txt").write_text("<<<<<<< ours\nours\n=======\ntheirs\n>>>>>>> theirs\n")
    assert _git(path, "ls-files", "-u").count("conflict.txt") == 3


def _merge_in_progress(path: Path) -> None:
    # `merge --no-commit -s ours` leaves MERGE_HEAD with the index still equal to HEAD.
    side = _git(path, "commit-tree", "HEAD^{tree}", "-p", "HEAD", "-m", "side").strip()
    _git(path, "branch", "side", side)
    _git(path, "merge", "-q", "--no-commit", "--no-ff", "-s", "ours", "side")
    assert (Path(_git(path, "rev-parse", "--absolute-git-dir").strip()) / "MERGE_HEAD").exists()


def _linked_merge_in_progress(path: Path) -> None:
    # A linked worktree whose main repo path holds a space: `--git-path` prints an absolute path.
    main = path.parent / f"main {path.name}"
    path.rename(main)
    _git(main, "worktree", "add", "-q", "-b", "linked", str(path))
    _edit(path)
    _merge_in_progress(path)


def _cherry_pick_in_progress(path: Path) -> None:
    # Picking an empty commit stops with CHERRY_PICK_HEAD and the index still equal to HEAD.
    empty = _git(path, "commit-tree", "HEAD^{tree}", "-p", "HEAD", "-m", "empty").strip()
    subprocess.run(["git", "cherry-pick", empty], cwd=path, capture_output=True)
    assert (path / ".git" / "CHERRY_PICK_HEAD").exists()


def _am_in_progress(path: Path) -> None:
    # A patch for a file main never had stops `git am` with rebase-apply and HEAD still on main.
    mbox = path.parent / f"{path.name}.mbox"
    mbox.write_text(
        "From 0000000000000000000000000000000000000000 Mon Sep 17 00:00:00 2001\n"
        "From: Test <test@example.com>\nSubject: absent\n\n---\n"
        "diff --git a/absent.txt b/absent.txt\n--- a/absent.txt\n+++ b/absent.txt\n"
        "@@ -1 +1 @@\n-a\n+b\n"
    )
    assert subprocess.run(["git", "am", str(mbox)], cwd=path, capture_output=True).returncode != 0
    assert _git(path, "branch", "--show-current").strip() == "main"
    assert (path / ".git" / "rebase-apply").is_dir()


def _rebase_in_progress(path: Path) -> None:
    # An interactive rebase stopped at `break`, then `checkout main`: rebase-merge stays with HEAD on main.
    _git(path, "stash", "-q", "-u")
    env = {**os.environ, "GIT_SEQUENCE_EDITOR": "perl -i -pe 'print qq(break\\n) if $.==1'"}
    subprocess.run(["git", "rebase", "-q", "-i", "HEAD"], cwd=path, env=env, check=True, capture_output=True)
    _git(path, "checkout", "-q", "main")
    _git(path, "stash", "pop", "-q")
    assert _git(path, "branch", "--show-current").strip() == "main"
    assert (path / ".git" / "rebase-merge").is_dir()


def _stopped_resolved(path: Path, verb: str, pending: str) -> None:
    # A conflicted pick or revert, resolved back to HEAD: no unmerged path is left, the operation is.
    _git(path, "stash", "-q", "-u")
    (path / "c.txt").write_text("a\n")
    _git(path, "add", "c.txt")
    _git(path, "commit", "-q", "-m", "c a")
    added = _git(path, "rev-parse", "HEAD").strip()
    _git(path, "switch", "-q", "-c", "side", "HEAD~1")
    (path / "c.txt").write_text("b\n")
    _git(path, "add", "c.txt")
    _git(path, "commit", "-q", "-m", "c b")
    side = _git(path, "rev-parse", "HEAD").strip()
    _git(path, "switch", "-q", "main")
    if verb == "revert":
        (path / "c.txt").write_text("z\n")
        _git(path, "commit", "-q", "-am", "c z")
    target = side if verb == "cherry-pick" else added
    assert subprocess.run(["git", verb, target], cwd=path, capture_output=True).returncode != 0
    _git(path, "checkout", "HEAD", "--", "c.txt")
    _git(path, "stash", "pop", "-q")
    assert (path / ".git" / pending).exists()
    assert "u " not in _git(path, "status", "--porcelain=v2")


def _sequence_left(path: Path, verb: str) -> None:
    # Two picks or reverts, the first conflicting, resolved and committed by hand: the *_HEAD file
    # is gone but .git/sequencer stays; a later --continue would land the rest on the new branch.
    _git(path, "stash", "-q", "-u")
    (path / "c.txt").write_text("a\n")
    _git(path, "add", "c.txt")
    _git(path, "commit", "-q", "-m", "c a")
    added = _git(path, "rev-parse", "HEAD").strip()
    _git(path, "switch", "-q", "-c", "side", "HEAD~1")
    (path / "c.txt").write_text("b\n")
    _git(path, "add", "c.txt")
    _git(path, "commit", "-q", "-m", "c b")
    first = _git(path, "rev-parse", "HEAD").strip()
    (path / "d.txt").write_text("d\n")
    _git(path, "add", "d.txt")
    _git(path, "commit", "-q", "-m", "d")
    second = _git(path, "rev-parse", "HEAD").strip()
    _git(path, "switch", "-q", "main")
    if verb == "revert":
        (path / "c.txt").write_text("z\n")
        _git(path, "commit", "-q", "-am", "c z")
        (path / "e.txt").write_text("e\n")
        _git(path, "add", "e.txt")
        _git(path, "commit", "-q", "-m", "e")
        first, second = added, _git(path, "rev-parse", "HEAD").strip()
    proc = subprocess.run(["git", verb, "--no-edit", first, second], cwd=path, capture_output=True)
    assert proc.returncode != 0
    (path / "c.txt").write_text("r\n")
    _git(path, "add", "c.txt")
    _git(path, "-c", "core.editor=true", "commit", "-q", "--no-edit")
    _git(path, "stash", "pop", "-q")
    assert not (path / ".git" / "CHERRY_PICK_HEAD").exists()
    assert not (path / ".git" / "REVERT_HEAD").exists()
    assert (path / ".git" / "sequencer").is_dir()


@pytest.mark.parametrize(
    "reason, spoil",
    [
        ("staged_index", lambda p: _git(p, "add", "README.md")),
        ("branch_exists", lambda p: _git(p, "branch", "feat", "HEAD")),
        ("staged_index", lambda p: (p / "later.txt").write_text("later\n") and _git(p, "add", "-N", "later.txt")),
        ("operation_in_progress", _merge_in_progress),
        ("operation_in_progress", _linked_merge_in_progress),
        ("operation_in_progress", _cherry_pick_in_progress),
        ("operation_in_progress", _am_in_progress),
        ("operation_in_progress", _rebase_in_progress),
        ("operation_in_progress", lambda p: _stopped_resolved(p, "cherry-pick", "CHERRY_PICK_HEAD")),
        ("operation_in_progress", lambda p: _stopped_resolved(p, "revert", "REVERT_HEAD")),
        ("operation_in_progress", lambda p: _sequence_left(p, "cherry-pick")),
        ("operation_in_progress", lambda p: _sequence_left(p, "revert")),
        ("detached_head", _detached),
        ("unresolved", _unresolved),
    ],
    ids=[
        "staged", "branch_exists", "intent_to_add", "merge_in_progress", "linked_spaced_main", "cherry_pick",
        "am", "rebase_merge", "cherry_pick_resolved", "revert_resolved", "cherry_pick_sequence",
        "revert_sequence", "detached_head", "unresolved",
    ],
)
def test_c3_refusal_writes_nothing_in_any_member(members: list[Path], git_log: Path, reason, spoil) -> None:
    spoil(members[2])
    before = [_state(p) for p in members]
    git_log.write_text("")
    result = _run(members)
    assert result.exit_code == 2, result.output
    assert f"refused: {reason}: m3:" in result.output
    assert "nothing was written" in result.output
    assert _not_reads(git_log) == []
    assert [_state(p) for p in members] == before



def test_c3_no_member_changed_refuses_nothing_changed(members: list[Path], git_log: Path) -> None:
    for path in members:
        _git(path, "checkout", "-q", "--", "README.md")
        (path / "new.txt").unlink()
    before = [_state(p) for p in members]
    git_log.write_text("")
    result = _run(members)
    assert result.exit_code == 2, result.output
    assert "refused: nothing_changed: m1,m2,m3,m4,m5: no member has a change" in result.output
    assert "nothing was written" in result.output
    assert _not_reads(git_log) == []
    assert [_state(p) for p in members] == before


def test_c3_repo_path_not_a_repo_refuses_not_a_repo(
    members: list[Path], git_log: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The ceiling keeps git from finding a repo above tmp_path, so m3 reads as no repo wherever the run lives.
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    shutil.rmtree(members[2] / ".git")
    others = [p for p in members if p != members[2]]
    before = [_state(p) for p in others]
    listing = sorted(p.name for p in members[2].iterdir())
    git_log.write_text("")
    result = _run(members)
    assert result.exit_code == 2, result.output
    assert "refused: not_a_repo: m3:" in result.output
    assert "nothing was written" in result.output
    assert _not_reads(git_log) == []
    assert [_state(p) for p in others] == before
    assert sorted(p.name for p in members[2].iterdir()) == listing


def test_c3_member_inside_an_enclosing_repo_refuses_not_a_repo(
    members: list[Path], git_log: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # No ceiling: m3 is a plain directory inside a repo, as a member is inside a gripspace that is itself a repo.
    monkeypatch.delenv("GIT_CEILING_DIRECTORIES", raising=False)
    _git(tmp_path, "init", "-q", "-b", "main")
    shutil.rmtree(members[2] / ".git")
    others = [p for p in members if p != members[2]]
    before = [_state(p) for p in others]
    git_log.write_text("")
    result = _run(members)
    assert result.exit_code == 2, result.output
    assert f"refused: not_a_repo: m3: inside the repo at {tmp_path}" in result.output
    assert "nothing was written" in result.output
    assert _not_reads(git_log) == []
    assert [_state(p) for p in others] == before
    assert _git(tmp_path, "for-each-ref") == ""
    assert _git(tmp_path, "ls-files", "-s") == ""


@pytest.mark.parametrize("mode", ["fails", "empty", "short"])
def test_c3_member_rev_parse_that_fails_refuses_not_a_repo(
    members: list[Path], git_log: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode
) -> None:
    # Only m3's `rev-parse --show-toplevel` misbehaves; every other call is real git, through the logging shim.
    # "fails" keeps git's full stdout and exits 128, so the return code alone has to refuse it.
    act = {
        "fails": '"$logged" "$@"; exit 128',
        "empty": '"$logged" "$@" >/dev/null; exit 0',
        "short": '"$logged" "$@" | head -n 1; exit 0',
    }[mode]
    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / "git").write_text(
        f'#!/bin/sh\nlogged="{git_log.parent / "shim" / "git"}"\n'
        f'case "$(basename "$PWD") $*" in "m3 "*" rev-parse --show-toplevel "*) {act};; esac\nexec "$logged" "$@"\n'
    )
    (bad / "git").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bad}{os.pathsep}{os.environ['PATH']}")
    before = [_state(p) for p in members]
    git_log.write_text("")
    result = _run(members)
    assert result.exit_code == 2, result.output
    assert "refused: not_a_repo: m3:" in result.output
    assert "nothing was written" in result.output
    assert _not_reads(git_log) == []
    assert any(m == "m3" and " rev-parse --show-toplevel " in f" {a}" for m, a in _calls(git_log))
    assert [_state(p) for p in members] == before


def test_c1_clean_member_beside_changed_ones_is_left_alone(members: list[Path], git_log: Path) -> None:
    m1, m2, m3 = members[:3]
    _git(m2, "checkout", "-q", "--", "README.md")
    (m2 / "new.txt").unlink()
    clean = _state(m2)
    bases = [_git(p, "rev-parse", "HEAD").strip() for p in (m1, m3)]
    result = _run([m1, m2, m3])
    assert result.exit_code == 0, result.output
    assert _state(m2) == clean
    assert _git(m2, "for-each-ref", "refs/heads/feat") == ""
    for path, base in zip((m1, m3), bases):
        assert _git(path, "branch", "--show-current").strip() == "feat"
        assert _git(path, "rev-parse", "HEAD~1").strip() == base
        assert _git(path, "status", "--porcelain") == ""


@pytest.mark.parametrize("kind", ["file", "missing"])
def test_c3_member_not_a_directory_refuses_not_a_repo(members: list[Path], git_log: Path, kind) -> None:
    shutil.rmtree(members[2])
    if kind == "file":
        members[2].write_text("not a repo\n")
    others = [p for p in members if p != members[2]]
    before = [_state(p) for p in others]
    git_log.write_text("")
    result = _run(members)
    assert result.exit_code == 2, result.output
    assert "refused: not_a_repo: m3: not a directory" in result.output
    assert "nothing was written" in result.output
    assert _not_reads(git_log) == []
    assert [_state(p) for p in others] == before
    assert members[2].is_file() == (kind == "file")

def test_c2_refused_commit_undoes_every_member_written(members: list[Path], git_log: Path) -> None:
    hook = members[2] / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\necho 'm3 refuses this commit' >&2\nexit 1\n")
    hook.chmod(0o755)
    before = [_state(p) for p in members]
    git_log.write_text("")
    result = _run(members)
    calls = _calls(git_log)
    assert result.exit_code == 2, result.output
    assert "refused: commit_failed: m3:" in result.output
    assert "undone: m3, m2, m1" in result.output
    assert [_state(p) for p in members] == before
    for path in members:
        assert (path / "README.md").read_text() == f"# {path.name} edited\n"
        assert _git(path, "branch", "--list", "feat") == ""
    undo_verbs = {"symbolic-ref", "reset", "branch"}
    undo = {p.name: [a for m, a in calls if m == p.name and set(a.split(" ")) & undo_verbs] for p in members}
    print(f"undo git calls per member: { {m: len(a) for m, a in undo.items()} }")
    assert [len(undo[p.name]) for p in members] == [3, 3, 3, 0, 0], undo
    order = [m for m, a in calls if "symbolic-ref" in a.split(" ")]
    assert order == ["m3", "m2", "m1"], order


def test_c6_refused_add_undoes_the_member_it_failed_in(members: list[Path], git_log: Path) -> None:
    # git lists an untracked "GIT~1" but refuses to index it under protectNTFS: m3's checkout lands, its add fails.
    # (Not ".GIT": on a case-insensitive disk that is the member's own .git.)
    _git(members[2], "config", "core.protectNTFS", "true")
    (members[2] / "GIT~1").write_text("not a repo\n")
    before = [_state(p) for p in members]
    git_log.write_text("")
    result = _run(members)
    assert result.exit_code == 2, result.output
    assert "refused: add_failed: m3:" in result.output
    assert "invalid path 'GIT~1'" in result.output
    assert "undone: m3, m2, m1" in result.output
    assert [_state(p) for p in members] == before
    assert not [m for m, a in _calls(git_log) if m in ("m4", "m5") and set(a.split(" ")) & set(WRITES)]


@pytest.mark.parametrize("sig, code", [("INT", 130), ("TERM", 143)])
def test_c5_signal_mid_write_undoes_every_member_touched(members: list[Path], tmp_path: Path, sig, code) -> None:
    # m2's post-commit hook signals gr2 itself: the hook's parent is git, and git's parent is gr2.
    told = tmp_path / "signalled.pid"
    hook = members[1] / ".git" / "hooks" / "post-commit"
    hook.write_text(f'#!/bin/sh\ngr2=$(ps -o ppid= -p $PPID | tr -d " ")\necho "$gr2" > "{told}"\nkill -{sig} "$gr2"\n')
    hook.chmod(0o755)
    before = [_state(p) for p in members]
    args = [os.sys.executable, "-m", "gr2.python_cli", "change", "feat", "-m", "one change"]
    for path in members:
        args += ["--repo-path", str(path)]
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])}
    proc = subprocess.Popen(
        args, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, preexec_fn=_default_sigint
    )
    out, err = proc.communicate(timeout=60)
    assert told.read_text().strip() == str(proc.pid)
    assert proc.returncode == code, (out, err)
    assert f"interrupted: SIG{sig}" in err
    assert "undone: m2, m1" in err
    assert "NOT undone" not in err
    assert [_state(p) for p in members] == before


@pytest.mark.parametrize("second_signal", ["INT", "TERM"])
def test_c8_second_signal_during_the_undo_does_not_cut_it_short(
    members: list[Path], tmp_path: Path, second_signal: str
) -> None:
    # m2's post-commit hook sends the first SIGINT; a git shim sends the second (SIGINT or SIGTERM) as the
    # undo starts on m2 (gitops puts -c options before the verb, so the shim matches the verb anywhere).
    hook = members[1] / ".git" / "hooks" / "post-commit"
    hook.write_text('#!/bin/sh\nkill -INT "$(ps -o ppid= -p $PPID | tr -d " ")"\n')
    hook.chmod(0o755)
    shim_dir = tmp_path / "shim"
    shim_dir.mkdir()
    second = tmp_path / "second.sent"
    shim = shim_dir / "git"
    shim.write_text(
        f'#!/bin/sh\ncase "$(basename "$PWD") $*" in "m2 "*" symbolic-ref "*)\n'
        f'  kill -{second_signal} $PPID; echo sent > "{second}"; sleep 0.5;;\nesac\nexec "{shutil.which("git")}" "$@"\n'
    )
    shim.chmod(0o755)
    before = [_state(p) for p in members]
    args = [os.sys.executable, "-m", "gr2.python_cli", "change", "feat", "-m", "one change"]
    for path in members:
        args += ["--repo-path", str(path)]
    env = {
        **os.environ,
        "PYTHONPATH": str(Path(__file__).resolve().parents[1]),
        "PATH": f"{shim_dir}{os.pathsep}{os.environ['PATH']}",
    }
    proc = subprocess.run(args, env=env, capture_output=True, text=True, timeout=60, preexec_fn=_default_sigint)
    assert second.exists()
    # The tree first, so a red names the half-restored member rather than the stderr text.
    after = [_state(p) for p in members]
    assert after == before, [(p.name, a) for p, a, b in zip(members, after, before) if a != b]
    assert proc.returncode == 130, (proc.stdout, proc.stderr)
    assert "interrupted: SIGINT" in proc.stderr
    assert "undone: m2, m1" in proc.stderr
    assert "NOT undone" not in proc.stderr


def test_c9_second_sigint_during_the_failure_undo_does_not_cut_it_short(members: list[Path], tmp_path: Path) -> None:
    # m3's pre-commit hook refuses, so the undo runs m3, m2, m1; a git shim sends SIGINT as it reaches m2.
    hook = members[2] / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\necho 'm3 refuses this commit' >&2\nexit 1\n")
    hook.chmod(0o755)
    shim_dir = tmp_path / "shim"
    shim_dir.mkdir()
    sent = tmp_path / "sigint.sent"
    shim = shim_dir / "git"
    shim.write_text(
        f'#!/bin/sh\ncase "$(basename "$PWD") $*" in "m2 "*" symbolic-ref "*)\n'
        f'  kill -INT $PPID; echo sent > "{sent}"; sleep 0.5;;\nesac\nexec "{shutil.which("git")}" "$@"\n'
    )
    shim.chmod(0o755)
    before = [_state(p) for p in members]
    args = [os.sys.executable, "-m", "gr2.python_cli", "change", "feat", "-m", "one change"]
    for path in members:
        args += ["--repo-path", str(path)]
    env = {
        **os.environ,
        "PYTHONPATH": str(Path(__file__).resolve().parents[1]),
        "PATH": f"{shim_dir}{os.pathsep}{os.environ['PATH']}",
    }
    proc = subprocess.run(args, env=env, capture_output=True, text=True, timeout=60, preexec_fn=_default_sigint)
    assert sent.exists()
    after = [_state(p) for p in members]
    assert after == before, [(p.name, a) for p, a, b in zip(members, after, before) if a != b]
    assert not [p.name for p in members if (p / ".git" / "refs" / "heads" / "feat.lock").exists()]
    assert proc.returncode == 2, (proc.stdout, proc.stderr)
    assert "refused: commit_failed: m3:" in proc.stderr
    assert "undone: m3, m2, m1" in proc.stderr
    assert "NOT undone" not in proc.stderr
