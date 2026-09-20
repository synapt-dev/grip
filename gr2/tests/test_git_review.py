"""`git review` single-repo entry point: open records {repo, base, head} under
`.git/grip/review.json`, status reads it, close removes it (and the empty store dir).
base defaults to the merge-base of HEAD and the default branch. Outside a git repo it
refuses. The console script is registered as `git-review` so git resolves `git review`."""
import json
import os
import shlex
import shutil
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


def _pkg_repo(
    tmp_path: Path,
    *,
    test_body: str,
    name: str = "r2",
    neutralize_excludes: bool = True,
) -> Path:
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
    # Belt for the OTHER tests: neutralize the host's global gitignore repo-locally so
    # a developer machine that ignores `__pycache__/` globally (this one does) cannot
    # blind the fixture. It is NOT what makes the cleanup work -- the product
    # neutralizes excludes itself on every status call, which is the thing
    # `test_the_run_cleans_up_on_a_host_whose_global_gitignore_hides_the_artifact`
    # witnesses with this switched off. Left on elsewhere so an unrelated failure
    # cannot be caused by whatever the running machine happens to ignore.
    if neutralize_excludes:
        _run(repo, "config", "core.excludesFile", "/dev/null")
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


def _fake_junit_runner(repo: Path, name: str, xml: str, report: str) -> str:
    """Write a fresh JUnit report while standing in for Gradle or Maven.

    The script lives beside the repository, as the existing cargo/jest stand-ins do,
    but writes its report inside the reviewed clone. That makes this an end-to-end
    witness of the ``git review`` entry point's report-file contract, rather than a
    parser-only test.
    """
    script = repo.parent / name
    script.write_text(
        "#!/bin/sh\n"
        f"mkdir -p {shlex.quote(str(Path(report).parent))}\n"
        f"cat > {shlex.quote(report)} <<'XML'\n{xml}\nXML\n"
        "printf 'BUILD SUCCESSFUL\\n'\n"
    )
    script.chmod(0o755)
    return str(script)


def _git_review_main_in(repo: Path, args: list[str]) -> int:
    """Run the installed-command entry function from a reviewer's clone."""
    cwd = Path.cwd()
    try:
        os.chdir(repo)
        return git_review.main(args)
    finally:
        os.chdir(cwd)


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


def test_git_review_junit_xml_runner_reads_a_fresh_report_via_reports_flag(tmp_path):
    """The stranger-facing entry point, not just ``gr2 review run``, shares the
    JUnit report seam. Replacing it with the old output parser makes this refuse
    ``unparseable_summary`` because the command prints no JUnit counts."""
    r = _pkg_repo(tmp_path, test_body=PASS_BODY)
    git_review.open_review(r)
    command = _fake_junit_runner(
        r,
        "fake-gradle-junit",
        '<testsuite tests="3" failures="0" errors="0" skipped="0" />',
        "reports/TEST-demo.xml",
    )
    assert _git_review_main_in(
        r,
        ["run", "--runner", "junit-xml", "--test", command, "--reports", "reports/*.xml"],
    ) == 0
    receipt = git_review.read_run_receipt(r)
    assert receipt["reports"] == "reports/*.xml"
    assert receipt["report_files"] == ["reports/TEST-demo.xml"]
    assert (receipt["selected"], receipt["passed"], receipt["failed"], receipt["skipped"]) == (3, 3, 0, 0)


def test_git_review_junit_xml_runner_refuses_stale_reports_via_entry_point(tmp_path):
    r = _pkg_repo(tmp_path, test_body=PASS_BODY)
    stale = r / "build" / "test-results" / "TEST-stale.xml"
    stale.parent.mkdir(parents=True)
    stale.write_text('<testsuite tests="1" />')
    os.utime(stale, (1, 1))
    git_review.open_review(r)
    assert _git_review_main_in(r, ["run", "--runner", "junit-xml", "--test", "true"]) == 2
    receipt = git_review.read_run_receipt(r)
    assert receipt["refusal_code"] == "no_fresh_reports"
    assert "stale reports are not trusted" in receipt["refusal_detail"]


def test_git_review_junit_xml_runner_refuses_malformed_fresh_report_via_entry_point(tmp_path):
    r = _pkg_repo(tmp_path, test_body=PASS_BODY)
    git_review.open_review(r)
    command = _fake_junit_runner(r, "fake-malformed-junit", "<testsuite>", "build/test-results/TEST-bad.xml")
    assert _git_review_main_in(r, ["run", "--runner", "junit-xml", "--test", command]) == 2
    receipt = git_review.read_run_receipt(r)
    assert receipt["refusal_code"] == "malformed_junit_xml"
    assert "TEST-bad.xml" in receipt["refusal_detail"]


# Like _offline_install, but ALSO writes a console-script SHIM into the
# review venv's own bin/ -- an offline stand-in for what a real editable install's
# `entry_points.txt` -> console_scripts machinery would write there (this venv has
# neither network nor host-bundled setuptools). Exactly the shape grip's own
# `test_gr2_console_script_resolves` shells out to, and exactly the test Fathom's
# stranger dogfood found RED on a fresh outer venv + ordinary clone.
def _offline_install_with_console_script(repo: Path) -> str:
    venv_python = repo / ".git" / "grip" / git_review.VENV_DIRNAME / "bin" / "python"
    paths = [str(repo / "src"), *[p for p in sys.path if p]]
    script = (
        "import site,stat,sys,pathlib;"
        "sp=pathlib.Path(site.getsitepackages()[0]);"
        "sp.mkdir(parents=True,exist_ok=True);"
        "(sp/'zz_review.pth').write_text('\\n'.join(sys.argv[1:])+'\\n');"
        "binf=pathlib.Path(sys.executable).parent/'demo_pkg_cli';"
        "binf.write_text('#!'+sys.executable+'\\nprint(\"demo_pkg_cli ok\")\\n');"
        "binf.chmod(binf.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)"
    )
    return shlex.join([str(venv_python), "-c", script, *paths])


CONSOLE_SCRIPT_BODY = (
    "import shutil, subprocess\n\n"
    "def test_console_script_resolves_on_path():\n"
    "    found = shutil.which('demo_pkg_cli')\n"
    "    assert found is not None, 'demo_pkg_cli not found on PATH'\n"
    "    result = subprocess.run(['demo_pkg_cli'], capture_output=True, text=True)\n"
    "    assert result.returncode == 0 and 'demo_pkg_cli ok' in result.stdout, result\n"
)


def test_the_run_puts_the_review_venvs_bin_on_the_pytest_subprocess_path(tmp_path):
    """On the ACTUAL path Fathom's stranger dogfood exercised: `git
    review run` against an ordinary clone. A repository's own tests can shell out
    to its OWN installed console script -- grip's own packaging test does exactly
    this -- and without an activation-shaped subprocess env the lookup fails
    `FileNotFoundError`, RED, in a repo whose own developers would never see it,
    because their shell has that venv's `bin/` on PATH before they ever run pytest
    by hand. Measured: a fresh outer venv, an ordinary clone of grip at merge
    commit 0b8ef1278fb5be3d139ce55ba27853223cb431bb, the four documented verbs,
    RED on `gr2/tests/test_gr2_packaging.py::test_gr2_console_script_resolves`.

    The review venv is created fresh under `.git/grip/venv` every run, so it is
    never on this TEST process's own PATH to begin with -- "the parent PATH does
    not contain the venv" is the ambient default, not something this test
    constructs."""
    r = _pkg_repo(tmp_path, test_body=CONSOLE_SCRIPT_BODY)
    git_review.open_review(r)
    receipt = git_review.run_review(r, install=_offline_install_with_console_script(r))
    assert receipt["result"] == "green", receipt


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


def _artifact_install(repo: Path) -> str:
    """An install that drops BOTH artifact kinds a real run leaves in the source tree:
    an `<pkg>.egg-info/` (every editable install) and a `__pycache__/` (importing the
    package, on the platforms that write bytecode there). Deterministic on every OS,
    so the cleanup witness does not depend on which platform the suite runs on."""
    venv_python = repo / ".git" / "grip" / git_review.VENV_DIRNAME / "bin" / "python"
    paths = [str(repo / "src"), *[p for p in sys.path if p]]
    script = (
        "import site,sys,pathlib;"
        "sp=pathlib.Path(site.getsitepackages()[0]);"
        "sp.mkdir(parents=True,exist_ok=True);"
        "(sp/'zz_review.pth').write_text('\\n'.join(sys.argv[2:])+'\\n');"
        "root=pathlib.Path(sys.argv[1]);"
        "egg=root/'src'/'demo_pkg.egg-info';"
        "egg.mkdir(parents=True,exist_ok=True);"
        "(egg/'PKG-INFO').write_text('Name: demo_pkg\\n');"
        "pyc=root/'src'/'demo_pkg'/'__pycache__';"
        "pyc.mkdir(parents=True,exist_ok=True);"
        "(pyc/'__init__.cpython-000.pyc').write_bytes(b'\\x00')"
    )
    return shlex.join([str(venv_python), "-c", script, str(repo), *paths])


def test_the_run_removes_the_build_dir_its_install_created(tmp_path):
    r = _pkg_repo(tmp_path, test_body=PASS_BODY)
    git_review.open_review(r)
    receipt = git_review.run_review(r, install=_egg_info_install(r))
    assert receipt["result"] == "green"
    # Membership, not list equality: what else the run cleans up is platform-dependent
    # (a `__pycache__` appears where the runtime writes bytecode into the source tree
    # and not where it does not). An exact-list assertion here passed on one platform
    # and failed on another for a reason that has nothing to do with what it is
    # testing, which is this install's egg-info being removed.
    assert "src/demo_pkg.egg-info/" in receipt["removed_build_dirs"]
    assert not (r / "src" / "demo_pkg.egg-info").exists()
    porcelain = subprocess.run(
        ["git", "-C", str(r), "status", "--porcelain"],
        capture_output=True, text=True, check=True,
    ).stdout
    assert porcelain == ""


def test_the_run_removes_every_artifact_kind_its_own_run_created(tmp_path):
    """`__pycache__` as well as `*.egg-info`.

    CI on Linux failed here while the same assertion passed on macOS: importing the
    package under test writes bytecode into the SOURCE tree on one platform and not
    the other, so a witness that WAITS for the runtime to drop a `__pycache__` asserts
    nothing wherever the runtime does not. The fixture therefore CREATES both artifact
    kinds itself, exactly as the platform-dependent install would, so the cleanup is
    exercised identically everywhere. Same correction as the egg-info fixture one
    commit earlier: do not let the fixture decide whether the case under test occurs."""
    r = _pkg_repo(tmp_path, test_body=PASS_BODY)
    git_review.open_review(r)
    receipt = git_review.run_review(r, install=_artifact_install(r))
    assert receipt["result"] == "green"
    removed = set(receipt["removed_build_dirs"])
    assert any(d.rstrip("/").endswith(".egg-info") for d in removed), removed
    assert any(d.rstrip("/").endswith("__pycache__") for d in removed), removed
    porcelain = subprocess.run(
        ["git", "-C", str(r), "status", "--porcelain"],
        capture_output=True, text=True, check=True,
    ).stdout
    assert porcelain == "", f"clone not left as found: {porcelain!r}"


@pytest.mark.parametrize("host_mechanism", ["core.excludesFile", "xdg-ignore"])
def test_the_run_cleans_up_on_a_host_whose_global_gitignore_hides_the_artifact(
    tmp_path, monkeypatch, host_mechanism
):
    """The one a reviewer's own machine is most likely to be: a global gitignore
    listing `__pycache__/`.

    `git status --porcelain` honours `core.excludesFile`, so on such a host git never
    REPORTS the `__pycache__` the run just created, a cleanup that removes only what
    it sees removes nothing, and the clone is left dirtier than it was found while
    every assertion in the suite stays green. That is the condition CI hit, and
    ignoring `__pycache__` globally is common enough advice that a stranger is likely
    to be in it. So the product neutralizes excludes on its own status calls, and this
    is the witness for that -- `neutralize_excludes=False` keeps the repo-local
    `/dev/null` OFF, leaving the host config as the only thing in play.

    The assertion is on the FILESYSTEM, deliberately. A `git status` check here would
    be blinded by the very config under test and could not fail -- the same shape of
    mistake this test exists to close. The porcelain check that follows re-neutralizes
    excludes for the same reason.
    """
    # Both of the ways a host hides paths from every repo it owns, because a stranger
    # may be in either and they are configured in different places: an explicit
    # `core.excludesFile` in the global config, and git's own XDG fallback at
    # `$XDG_CONFIG_HOME/git/ignore`, which applies when `core.excludesFile` is UNSET.
    # Measured: an empty `core.excludesFile` on the command line suppresses both.
    home = tmp_path / "fakehome"
    (home / ".config" / "git").mkdir(parents=True)
    gitconfig = home / ".gitconfig"
    if host_mechanism == "core.excludesFile":
        excludes = home / "global_gitignore"
        excludes.write_text("__pycache__/\n")
        gitconfig.write_text(f"[core]\n\texcludesfile = {excludes}\n")
    else:
        (home / ".config" / "git" / "ignore").write_text("__pycache__/\n")
        gitconfig.write_text("")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(gitconfig))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))

    r = _pkg_repo(tmp_path, test_body=PASS_BODY, neutralize_excludes=False)

    # Control: the host config really is in force and really does hide the artifact,
    # so a pass below is the product working rather than the setup failing to bite.
    decoy = r / "src" / "demo_pkg" / "__pycache__"
    decoy.mkdir(parents=True)
    (decoy / "probe.pyc").write_bytes(b"\x00")
    blinded = subprocess.run(
        ["git", "-C", str(r), "status", "--porcelain"],
        capture_output=True, text=True, check=True,
    ).stdout
    assert "__pycache__" not in blinded, (
        "setup failed to bite: the host excludes are not hiding the artifact, so this "
        f"test cannot witness anything. porcelain={blinded!r}"
    )
    shutil.rmtree(decoy)

    # The adversarial case for the fix itself. Neutralizing excludes WIDENS what the
    # cleanup can see, so the paths newly in view must still be protected by the
    # "absent before the install" scoping rather than by having been invisible. A
    # host-ignored artifact-named directory the reviewer already had is exactly the
    # thing that was safe by accident before this change and must be safe on purpose
    # after it.
    reviewers_own = r / "tests" / "__pycache__"
    reviewers_own.mkdir(parents=True)
    (reviewers_own / "mine.txt").write_text("not yours\n")

    git_review.open_review(r)
    receipt = git_review.run_review(r, install=_artifact_install(r))
    assert receipt["result"] == "green"

    assert reviewers_own.is_dir(), "a pre-existing host-ignored __pycache__ was deleted"
    assert (reviewers_own / "mine.txt").is_file()
    assert "tests/__pycache__/" not in set(receipt["removed_build_dirs"])

    left_behind = r / "src" / "demo_pkg" / "__pycache__"
    assert not left_behind.exists(), (
        "the run left a __pycache__ behind on a host whose global gitignore hides it"
    )
    removed = set(receipt["removed_build_dirs"])
    assert any(d.rstrip("/").endswith("__pycache__") for d in removed), removed

    porcelain = subprocess.run(
        ["git", "-C", str(r), "-c", "core.excludesFile=", "status", "--porcelain"],
        capture_output=True, text=True, check=True,
    ).stdout
    # "as found" includes the reviewer's own pre-existing directory, which was there
    # before and must still be: it is the only thing allowed in this output.
    assert porcelain == "?? tests/__pycache__/\n", (
        f"clone not left as found: {porcelain!r}"
    )


def test_a_pycache_the_reviewer_already_had_is_never_removed(tmp_path):
    """The cleanup is scoped to what THIS run created, for every artifact kind — not
    just the egg-info. A `__pycache__` present before the run must survive it."""
    r = _pkg_repo(tmp_path, test_body=PASS_BODY)
    pre_existing = r / "src" / "demo_pkg" / "__pycache__"
    pre_existing.mkdir(parents=True)
    (pre_existing / "marker.txt").write_text("mine\n")
    git_review.open_review(r)
    git_review.run_review(r, install=_artifact_install(r))
    assert pre_existing.is_dir(), "a pre-existing __pycache__ was deleted"
    assert (pre_existing / "marker.txt").is_file()


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
    assert "src/demo_pkg.egg-info/" in receipt["removed_build_dirs"]
    # The property under test is the NEGATIVE one: the pre-existing path is absent
    # from what was removed, and still on disk.
    assert "src/preexisting.egg-info/" not in receipt["removed_build_dirs"]
    assert pre_existing.is_dir(), "a pre-existing untracked path was deleted"


def _egg_info_install_without_pytest(repo: Path) -> str:
    """Like `_egg_info_install`, but the `.pth` names only `repo/src` -- never the
    host's own `sys.path` the way `_egg_info_install` and `_offline_install` do -- so
    `import demo_pkg` resolves under `-I` (the import-under-lane check passes) while
    `import pytest` does not (nothing on the venv's site puts it there). This is the
    ordinary shape of a package that only declares pytest in an optional extra the
    reviewer did not ask to install: a ten-minutes-in stranger, not a contrived one.
    The egg-info is written exactly like every real editable install, and the run
    reaches `pytest_not_installed` AFTER it exists on disk."""
    venv_python = repo / ".git" / "grip" / git_review.VENV_DIRNAME / "bin" / "python"
    script = (
        "import site,sys,pathlib;"
        "sp=pathlib.Path(site.getsitepackages()[0]);"
        "sp.mkdir(parents=True,exist_ok=True);"
        "(sp/'zz_review.pth').write_text(sys.argv[2]+'\\n');"
        "egg=pathlib.Path(sys.argv[1])/'src'/'demo_pkg.egg-info';"
        "egg.mkdir(parents=True,exist_ok=True);"
        "(egg/'PKG-INFO').write_text('Name: demo_pkg\\n')"
    )
    return shlex.join([str(venv_python), "-c", script, str(repo), str(repo / "src")])


def test_a_refusal_after_the_install_still_cleans_up_the_egg_info(tmp_path):
    """The install writes `<pkg>.egg-info/` into the reviewer's own working tree (this
    is NOT a throwaway clone) as its very first act, unconditionally. Before the
    `finally` fix, `_remove_new_run_artifacts` ran only as the last line of the green
    path, so any refusal AFTER the install (pytest missing, unparseable summary, zero
    collected) skipped it and left the egg-info behind as untracked drift.

    Reproduced directly, in sequence, exactly as the report described it: a refused
    run first (`pytest_not_installed`, the most common ten-minutes-in stranger --
    pytest declared only in an optional extra the reviewer did not install), then a
    real green run with a real install. Both must leave the clone exactly as found.
    """
    r = _pkg_repo(tmp_path, test_body=PASS_BODY)
    git_review.open_review(r)

    with pytest.raises(rr.ReviewRunRefused) as exc:
        git_review.run_review(r, install=_egg_info_install_without_pytest(r))
    assert exc.value.code == "pytest_not_installed"
    porcelain = subprocess.run(
        ["git", "-C", str(r), "status", "--porcelain"],
        capture_output=True, text=True, check=True,
    ).stdout
    assert porcelain == "", f"a refused run left the egg-info behind: {porcelain!r}"

    receipt = git_review.run_review(r, install=_egg_info_install(r))
    assert receipt["result"] == "green"
    assert "src/demo_pkg.egg-info/" in receipt["removed_build_dirs"]
    porcelain = subprocess.run(
        ["git", "-C", str(r), "status", "--porcelain"],
        capture_output=True, text=True, check=True,
    ).stdout
    assert porcelain == "", f"clone not left as found: {porcelain!r}"


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
