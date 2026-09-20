"""`git review` single-repo entry point: open records {repo, base, head} under
`.git/grip/review.json`, status reads it, close removes it (and the empty store dir).
base defaults to the merge-base of HEAD and the default branch. Outside a git repo it
refuses. The console script is registered as `git-review` so git resolves `git review`."""
import json
import os
import shlex
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from python_cli import git_review


def _run(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


def _repo(tmp_path: Path) -> Path:
    r = tmp_path / "r"
    r.mkdir()
    _run(r, "init", "-q")
    _run(r, "config", "user.email", "a@b")
    _run(r, "config", "user.name", "a")
    (r / "f").write_text("one")
    _run(r, "add", "-A")
    _run(r, "commit", "-q", "-m", "c1", "--no-gpg-sign")
    return r


def _sha(repo: Path, rev: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", rev], capture_output=True, text=True
    ).stdout.strip()


def test_open_writes_record_under_dot_git_grip(tmp_path):
    r = _repo(tmp_path)
    assert not (r / ".git" / "grip").exists()
    rec = git_review.open_review(r)
    stored = r / ".git" / "grip" / "review.json"
    assert stored.is_file()
    assert json.loads(stored.read_text()) == rec
    assert rec["repo"] == r.name
    assert rec["head"] == _sha(r, "HEAD")


def test_base_defaults_to_merge_base_with_default_branch(tmp_path):
    r = _repo(tmp_path)
    _run(r, "branch", "-m", "main")  # name the default branch
    base_sha = _sha(r, "HEAD")       # divergence point
    _run(r, "checkout", "-q", "-b", "feature")
    (r / "f").write_text("two")
    _run(r, "add", "-A")
    _run(r, "commit", "-q", "-m", "c2", "--no-gpg-sign")
    rec = git_review.open_review(r)
    assert rec["base"] == base_sha           # merge-base(feature, main) == c1
    assert rec["head"] == _sha(r, "HEAD")     # c2
    assert rec["base"] != rec["head"]


def test_status_and_close_roundtrip(tmp_path):
    r = _repo(tmp_path)
    assert git_review.read_review(r) is None
    git_review.open_review(r)
    assert git_review.read_review(r) is not None
    assert git_review.close_review(r) is True
    assert git_review.read_review(r) is None
    assert not (r / ".git" / "grip").exists()  # empty store dir removed
    assert git_review.close_review(r) is False  # idempotent: nothing to close


def test_outside_a_git_repo_refuses(tmp_path):
    with pytest.raises(git_review.GitReviewError):
        git_review._repo_root(tmp_path)  # tmp_path is not a git repo


def test_console_entry_point_is_registered():
    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    data = tomllib.loads(pyproject.read_text())
    scripts = data["project"]["scripts"]
    assert scripts.get("git-review") == "gr2.python_cli.git_review:main"


def test_base_resolves_via_remote_tracking_ref_when_local_default_deleted(tmp_path):
    """Fix (1): merge-base runs against refs/remotes/origin/<name>, so a clone whose
    LOCAL default branch is deleted still records base != HEAD (not a silent empty diff)."""
    origin = tmp_path / "origin"
    origin.mkdir()
    _run(origin, "init", "-q", "-b", "dev")  # default branch is 'dev', not main/master
    _run(origin, "config", "user.email", "a@b")
    _run(origin, "config", "user.name", "a")
    (origin / "f").write_text("one")
    _run(origin, "add", "-A")
    _run(origin, "commit", "-q", "-m", "c1", "--no-gpg-sign")
    c1 = _sha(origin, "HEAD")

    clone = tmp_path / "clone"
    _run(tmp_path, "clone", "-q", str(origin), str(clone))
    _run(clone, "config", "user.email", "a@b")
    _run(clone, "config", "user.name", "a")
    # a feature branch diverging from dev, then DELETE the local default branch
    _run(clone, "checkout", "-q", "-b", "feature")
    (clone / "f").write_text("two")
    _run(clone, "add", "-A")
    _run(clone, "commit", "-q", "-m", "c2", "--no-gpg-sign")
    _run(clone, "branch", "-D", "dev")  # local default gone; origin/HEAD -> dev remains

    rec = git_review.open_review(clone)
    assert rec["base"] == c1                # merge-base(feature, refs/remotes/origin/dev)
    assert rec["base"] != rec["head"]       # NOT a silent base==HEAD


def test_store_root_is_absolute_git_dir_when_dot_git_is_a_file(tmp_path):
    """Fix (2): a repo whose .git is a FILE (separate git dir) stores under the resolved
    git dir instead of tracebacking on a .git/grip path."""
    gitdir = tmp_path / "gd"
    work = tmp_path / "work"
    work.mkdir()
    _run(work, "init", "-q", f"--separate-git-dir={gitdir}")
    _run(work, "config", "user.email", "a@b")
    _run(work, "config", "user.name", "a")
    (work / "f").write_text("one")
    _run(work, "add", "-A")
    _run(work, "commit", "-q", "-m", "c1", "--no-gpg-sign")
    assert (work / ".git").is_file()  # .git is a file, not a dir

    rec = git_review.open_review(work)  # must not raise NotADirectoryError
    assert (gitdir / "grip" / "review.json").is_file()
    assert rec["repo"] == work.name


def test_base_equals_head_fallback_warns(tmp_path, capsys, monkeypatch):
    """Fix (3): the base == HEAD fallback (no diverging branch) prints a warning line."""
    r = tmp_path / "solo"
    r.mkdir()
    _run(r, "init", "-q", "-b", "trunk")  # default is neither main nor master, no origin
    _run(r, "config", "user.email", "a@b")
    _run(r, "config", "user.name", "a")
    (r / "f").write_text("one")
    _run(r, "add", "-A")
    _run(r, "commit", "-q", "-m", "c1", "--no-gpg-sign")
    monkeypatch.chdir(r)          # main() resolves repo from cwd
    rc = git_review.main(["open"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "base = HEAD" in out and "warning" in out


def test_dash_h_prints_usage_and_exits_zero(capsys):
    """Fix-forward: -h/--help prints usage and exits 0, not 'unknown subcommand' exit 2."""
    rc = git_review.main(["-h"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "usage: git review" in out


# ---------------------------------------------------------------- `git review run`
#
# The run's trust properties, witnessed the same way `gr2 review run`'s are: the
# tracked-tree comparison and the untracked-drift refusal are each mutated on their
# own, the counts come from the runner's summary rather than its exit code, and the
# clone is left exactly as it was found. The end-to-end witnesses build a real venv
# but install OFFLINE (a `.pth` seeds the package and the host's sys.path), so no
# network is needed and host pytest is importable inside the review venv.

# Imported the same way as `git_review` above, deliberately: the root conftest makes
# `python_cli` reachable under BOTH `python_cli` and `gr2.python_cli`, which are two
# distinct module objects — so a refusal raised through one path is not caught by an
# `except` bound to the other. Production has one path (the wheel ships only
# `gr2.python_cli`) and git_review imports review_run relatively, so this is a
# test-harness hazard only; matching the file's existing import keeps it out of reach.
from python_cli import review_run as rr  # noqa: E402  (grouped with the run tests)


def _pkg_repo(tmp_path: Path, *, test_body: str, name: str = "r2") -> Path:
    """A git repo holding a trivial installable package `demo_pkg` and one test file,
    declaring itself through `.review-install` the way a stranger's fixture does."""
    repo = tmp_path / name
    (repo / "src" / "demo_pkg").mkdir(parents=True)
    (repo / "tests").mkdir()
    (repo / "src" / "demo_pkg" / "__init__.py").write_text("VALUE = 1\n")
    (repo / "pyproject.toml").write_text(
        "[build-system]\nrequires=['setuptools']\nbuild-backend='setuptools.build_meta'\n"
        "[project]\nname='demo_pkg'\nversion='0.0.0'\n"
        "[tool.setuptools.packages.find]\nwhere=['src']\n"
    )
    (repo / "tests" / "test_demo.py").write_text(test_body)
    (repo / ".review-install").write_text("package = demo_pkg\n")
    _run(repo, "init", "-q", "-b", "main")
    _run(repo, "config", "user.email", "a@e.invalid")
    _run(repo, "config", "user.name", "a")
    _run(repo, "add", "-A")
    _run(repo, "commit", "-q", "-m", "pkg", "--no-gpg-sign")
    return repo


def _offline_install(repo: Path) -> str:
    """An install command that needs no network: write a `.pth` into the review venv's
    site-packages naming the repo's `src` plus the host sys.path, so `demo_pkg`
    resolves under the clone and host pytest is importable."""
    venv_python = repo / ".git" / "grip" / git_review.VENV_DIRNAME / "bin" / "python"
    paths = [str(repo / "src"), *[p for p in sys.path if p]]
    script = (
        "import site,sys,pathlib;"
        "sp=pathlib.Path(site.getsitepackages()[0]);"
        "sp.mkdir(parents=True,exist_ok=True);"
        "(sp/'zz_review.pth').write_text('\\n'.join(sys.argv[1:])+'\\n')"
    )
    return shlex.join([str(venv_python), "-c", script, *paths])


PASS_BODY = "from demo_pkg import VALUE\n\ndef test_ok():\n    assert VALUE == 1\n"
FAIL_BODY = "from demo_pkg import VALUE\n\ndef test_bad():\n    assert VALUE == 2\n"
EMPTY_BODY = "def helper():\n    return 1\n"


def test_open_records_the_head_tree_so_a_run_can_bind_to_it(tmp_path):
    r = _repo(tmp_path)
    rec = git_review.open_review(r)
    assert rec["head_tree"] == _sha(r, "HEAD^{tree}")


def test_run_without_an_open_review_refuses(tmp_path):
    r = _repo(tmp_path)
    with pytest.raises(git_review.GitReviewError):
        git_review.run_review(r)


def test_run_refuses_a_record_written_before_the_tree_binding(tmp_path):
    """A review.json from an older build has no `head_tree`. Running it would compare
    nothing, so it refuses and names the fix instead of reporting an unbound green."""
    r = _repo(tmp_path)
    rec = git_review.open_review(r)
    rec.pop("head_tree")
    git_review._record_path(r).write_text(json.dumps(rec))
    with pytest.raises(rr.ReviewRunRefused) as exc:
        git_review.run_review(r)
    assert exc.value.code == "no_bound_tree"


def test_run_refuses_when_a_tracked_file_changed_after_open(tmp_path):
    """The tracked-tree comparison. Editing a tracked file leaves HEAD untouched, so
    only the tree hash catches it — without this the run would report a green about
    code that is not the code under review."""
    r = _pkg_repo(tmp_path, test_body=PASS_BODY)
    git_review.open_review(r)
    (r / "src" / "demo_pkg" / "__init__.py").write_text("VALUE = 99\n")
    with pytest.raises(rr.ReviewRunRefused) as exc:
        git_review.run_review(r)
    assert exc.value.code == "tree_drift"


def test_run_refuses_an_untracked_file_it_did_not_create(tmp_path):
    """The drift comparison's other half: an injected conftest.py changes what the
    tests do without touching the tracked tree, so the tree hash cannot see it."""
    r = _pkg_repo(tmp_path, test_body=PASS_BODY)
    git_review.open_review(r)
    (r / "conftest.py").write_text("# injected\n")
    with pytest.raises(rr.ReviewRunRefused) as exc:
        git_review.run_review(r)
    assert exc.value.code == "untracked_drift"


def test_a_refusal_is_recorded_so_status_can_report_it(tmp_path):
    r = _pkg_repo(tmp_path, test_body=PASS_BODY)
    git_review.open_review(r)
    (r / "conftest.py").write_text("# injected\n")
    with pytest.raises(rr.ReviewRunRefused):
        git_review.run_review(r)
    receipt = git_review.read_run_receipt(r)
    assert receipt["result"] == "refused" and receipt["refusal_code"] == "untracked_drift"


def test_test_flag_with_the_pytest_runner_refuses(tmp_path):
    """Silently ignoring --test would run pytest and report a green about a command
    the caller never asked for."""
    r = _pkg_repo(tmp_path, test_body=PASS_BODY)
    git_review.open_review(r)
    with pytest.raises(rr.ReviewRunRefused) as exc:
        git_review.run_review(r, test="cargo test")
    assert exc.value.code == "test_with_pytest"


def test_unknown_runner_refuses(tmp_path):
    r = _pkg_repo(tmp_path, test_body=PASS_BODY)
    git_review.open_review(r)
    with pytest.raises(rr.ReviewRunRefused) as exc:
        git_review.run_review(r, runner="maven", test="mvn test")
    assert exc.value.code == "unknown_runner"


def test_a_declared_runner_with_no_test_command_refuses(tmp_path):
    r = _pkg_repo(tmp_path, test_body=PASS_BODY)
    git_review.open_review(r)
    with pytest.raises(rr.ReviewRunRefused) as exc:
        git_review.run_review(r, runner="cargo")
    assert exc.value.code == "no_test_command"


def _fake_runner(repo: Path, name: str, output: str, exit_code: int = 0) -> str:
    """A script standing in for cargo/jest: it prints a real runner's summary text and
    exits with `exit_code`, so a test can drive the count-from-the-summary property and
    the exit-code-is-not-the-verdict property independently."""
    script = repo.parent / name
    script.write_text(
        "#!/bin/sh\ncat <<'OUT'\n" + output + "\nOUT\nexit " + str(exit_code) + "\n"
    )
    script.chmod(0o755)
    return str(script)


def test_non_pytest_runner_takes_its_counts_from_the_summary_not_the_exit_code(tmp_path):
    """A cargo run that exits 0 while its summary reports a failure is a RED. Reading
    the exit code instead would call it green."""
    r = _pkg_repo(tmp_path, test_body=PASS_BODY)
    git_review.open_review(r)
    cmd = _fake_runner(r, "fake-cargo", "test result: FAILED. 1 passed; 2 failed; 0 ignored", 0)
    receipt = git_review.run_review(r, runner="cargo", test=cmd)
    assert receipt["result"] == "red"
    assert (receipt["passed"], receipt["failed"], receipt["selected"]) == (1, 2, 3)


def test_non_pytest_runner_green(tmp_path):
    r = _pkg_repo(tmp_path, test_body=PASS_BODY)
    git_review.open_review(r)
    cmd = _fake_runner(r, "fake-cargo-ok", "test result: ok. 3 passed; 0 failed; 0 ignored")
    receipt = git_review.run_review(r, runner="cargo", test=cmd)
    assert receipt["result"] == "green" and receipt["selected"] == 3
    assert git_review.read_run_receipt(r)["result"] == "green"


def test_non_pytest_runner_with_no_summary_refuses_rather_than_reporting_zero(tmp_path):
    r = _pkg_repo(tmp_path, test_body=PASS_BODY)
    git_review.open_review(r)
    cmd = _fake_runner(r, "fake-cargo-broken", "error[E0433]: failed to resolve", 101)
    with pytest.raises(rr.ReviewRunRefused) as exc:
        git_review.run_review(r, runner="cargo", test=cmd)
    assert exc.value.code == "unparseable_summary"


def test_the_runner_and_test_command_come_from_review_install(tmp_path):
    """A stranger types nothing: the repo declares its own runner and test command."""
    r = _pkg_repo(tmp_path, test_body=PASS_BODY, name="declaring")
    cmd = _fake_runner(r, "fake-cargo-hint", "test result: ok. 2 passed; 0 failed; 0 ignored")
    (r / ".review-install").write_text(f"runner = cargo\ntest = {cmd}\n")
    _run(r, "add", "-A")
    _run(r, "commit", "-q", "-m", "declare", "--no-gpg-sign")
    git_review.open_review(r)
    receipt = git_review.run_review(r)
    assert receipt["runner"] == "cargo" and receipt["result"] == "green"


# ---- end-to-end: a real venv, an offline install ----------------------------

def test_pytest_run_is_green_and_leaves_the_clone_untouched(tmp_path):
    r = _pkg_repo(tmp_path, test_body=PASS_BODY)
    git_review.open_review(r)
    receipt = git_review.run_review(r, install=_offline_install(r))
    assert receipt["result"] == "green"
    assert receipt["passed"] == 1 and receipt["selected"] == 1
    # the venv, receipt and log live inside the git directory ...
    store = r / ".git" / "grip"
    assert (store / git_review.VENV_DIRNAME).is_dir()
    assert (store / git_review.RECEIPT_FILENAME).is_file()
    assert (store / git_review.OUTPUT_LOG_FILENAME).is_file()
    # ... so the reviewed working tree is exactly as it was found.
    porcelain = subprocess.run(
        ["git", "-C", str(r), "status", "--porcelain"],
        capture_output=True, text=True, check=True,
    ).stdout
    assert porcelain == ""


def test_pytest_run_is_red_and_names_which_test_failed(tmp_path):
    r = _pkg_repo(tmp_path, test_body=FAIL_BODY)
    git_review.open_review(r)
    receipt = git_review.run_review(r, install=_offline_install(r))
    assert receipt["result"] == "red" and receipt["failed"] == 1
    assert any("test_bad" in nid for nid in receipt["failed_ids"])


def test_a_zero_test_run_is_a_refusal_not_a_green(tmp_path):
    r = _pkg_repo(tmp_path, test_body=EMPTY_BODY)
    git_review.open_review(r)
    with pytest.raises(rr.ReviewRunRefused) as exc:
        git_review.run_review(r, install=_offline_install(r))
    assert exc.value.code == "zero_collected"


def test_close_removes_the_venv_and_the_run_artifacts(tmp_path):
    r = _pkg_repo(tmp_path, test_body=PASS_BODY)
    git_review.open_review(r)
    git_review.run_review(r, install=_offline_install(r))
    assert (r / ".git" / "grip" / git_review.VENV_DIRNAME).is_dir()
    assert git_review.close_review(r) is True
    assert not (r / ".git" / "grip").exists()
    assert git_review.read_run_receipt(r) is None


def test_run_through_the_console_entry_point_exits_1_on_red_and_2_on_a_refusal(tmp_path):
    """The exit codes a stranger's shell sees: 0 green, 1 red, 2 refused. A refusal is
    not a red — the run could not be trusted at all — so they are different numbers."""
    r = _pkg_repo(tmp_path, test_body=FAIL_BODY)
    git_review.open_review(r)
    cwd = Path.cwd()
    try:
        os.chdir(r)
        assert git_review.main(["run", "--install", _offline_install(r)]) == 1
        (r / "conftest.py").write_text("# injected\n")
        assert git_review.main(["run", "--install", _offline_install(r)]) == 2
    finally:
        os.chdir(cwd)


def test_status_reports_the_last_run(tmp_path, capsys):
    r = _pkg_repo(tmp_path, test_body=PASS_BODY)
    git_review.open_review(r)
    cwd = Path.cwd()
    try:
        os.chdir(r)
        git_review.main(["status"])
        assert "no run yet" in capsys.readouterr().out
        cmd = _fake_runner(r, "fake-cargo-status", "test result: ok. 1 passed; 0 failed; 0 ignored")
        git_review.run_review(r, runner="cargo", test=cmd)
        git_review.main(["status"])
        out = capsys.readouterr().out
        assert "last run GREEN" in out and "passed=1" in out
    finally:
        os.chdir(cwd)


def _egg_info_install(repo: Path) -> str:
    """An install that behaves like a real editable install in the one way that
    matters here: besides seeding the `.pth`, it drops an `<pkg>.egg-info/` into the
    SOURCE tree. The offline `.pth`-only fixture above cannot produce one, so without
    this the "leaves the clone as it was found" witness was green about a case it could
    not reach — the run through the installed console script left the directory behind."""
    venv_python = repo / ".git" / "grip" / git_review.VENV_DIRNAME / "bin" / "python"
    paths = [str(repo / "src"), *[p for p in sys.path if p]]
    script = (
        "import site,sys,pathlib;"
        "sp=pathlib.Path(site.getsitepackages()[0]);"
        "sp.mkdir(parents=True,exist_ok=True);"
        "(sp/'zz_review.pth').write_text('\\n'.join(sys.argv[2:])+'\\n');"
        "egg=pathlib.Path(sys.argv[1])/'src'/'demo_pkg.egg-info';"
        "egg.mkdir(parents=True,exist_ok=True);"
        "(egg/'PKG-INFO').write_text('Name: demo_pkg\\n')"
    )
    return shlex.join([str(venv_python), "-c", script, str(repo), *paths])


def test_the_run_removes_the_build_dir_its_install_created(tmp_path):
    r = _pkg_repo(tmp_path, test_body=PASS_BODY)
    git_review.open_review(r)
    receipt = git_review.run_review(r, install=_egg_info_install(r))
    assert receipt["result"] == "green"
    assert receipt["removed_build_dirs"] == ["src/demo_pkg.egg-info/"]
    assert not (r / "src" / "demo_pkg.egg-info").exists()
    porcelain = subprocess.run(
        ["git", "-C", str(r), "status", "--porcelain"],
        capture_output=True, text=True, check=True,
    ).stdout
    assert porcelain == ""


def test_an_untracked_path_the_reviewer_already_had_is_never_removed(tmp_path):
    """The cleanup is scoped to what THIS run created. A reviewer's own untracked
    `.egg-info` (present before the run) must survive it — deleting a file the tool
    did not create is the failure mode a cleanup must not have."""
    r = _pkg_repo(tmp_path, test_body=PASS_BODY)
    pre_existing = r / "src" / "preexisting.egg-info"
    pre_existing.mkdir(parents=True)
    (pre_existing / "PKG-INFO").write_text("mine\n")
    git_review.open_review(r)
    receipt = git_review.run_review(r, install=_egg_info_install(r))
    assert receipt["removed_build_dirs"] == ["src/demo_pkg.egg-info/"]
    assert pre_existing.is_dir(), "a pre-existing untracked path was deleted"


def test_run_help_prints_usage_instead_of_an_argparse_error(tmp_path, capsys):
    """`git review run -h` used to fall through to `unrecognized arguments: -h`."""
    r = _pkg_repo(tmp_path, test_body=PASS_BODY)
    git_review.open_review(r)
    cwd = Path.cwd()
    try:
        os.chdir(r)
        assert git_review.main(["run", "-h"]) == 0
        out = capsys.readouterr().out
        assert "usage: git review" in out and "unrecognized" not in out
    finally:
        os.chdir(cwd)
