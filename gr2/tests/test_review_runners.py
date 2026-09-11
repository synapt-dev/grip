"""The runner contract: counts always from the runner's OWN summary line, never the
exit code; an unreadable summary is a refusal with the raw tail, not a zero-green.
Parsers are pinned to REAL runner output shapes; the end-to-end path runs cargo in a
bound lane and keeps the language-agnostic tree checks."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from python_cli import review_run as rr
from python_cli import review_runners as R


# ---- parser unit tests (real output shapes) ---------------------------------

def test_cargo_sums_across_every_result_line():
    # cargo prints one `test result:` line per binary; the doc-test line is 0 passed
    # and must NOT zero out the unit line (taking only the last line would).
    out = (
        "running 1 test\n"
        "test result: ok. 1 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.00s\n"
        "   Doc-tests onetest\n"
        "running 0 tests\n"
        "test result: ok. 0 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.00s\n"
    )
    assert R.parse_cargo_summary(out) == {
        "passed": 1, "failed": 0, "errors": 0, "skipped": 0, "selected": 1,
    }


def test_cargo_counts_failures_and_ignored():
    out = "test result: FAILED. 2 passed; 1 failed; 3 ignored; 0 measured; 0 filtered out; finished in 0.01s\n"
    got = R.parse_cargo_summary(out)
    assert got["passed"] == 2 and got["failed"] == 1 and got["skipped"] == 3 and got["selected"] == 6


def test_cargo_compile_error_is_none_not_zero():
    # no `test result:` line -> None -> the caller refuses (never a false zero-green)
    assert R.parse_cargo_summary("error[E0425]: cannot find value\nerror: could not compile") is None


def test_cargo_unreadable_result_line_refuses_not_partial_sum():
    # Two `test result:` lines but only one the regex can read: a silent sum would report
    # passed=3 (dropping the second binary's 2), a confidently wrong green. The count check
    # forces None -> the caller refuses as unparseable.
    out = (
        "test result: ok. 3 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.00s\n"
        "test result: ok. 2 passed; 0 failed\n"  # truncated form the regex cannot read
    )
    assert R.parse_cargo_summary(out) is None


def test_jest_tests_line_with_failures():
    out = "Tests:       1 failed, 2 passed, 3 total\nSnapshots:   0 total\n"
    got = R.parse_jest_summary(out)
    assert got["passed"] == 2 and got["failed"] == 1 and got["selected"] == 3


def test_jest_no_tally_is_none():
    assert R.parse_jest_summary("Cannot find module 'x'") is None


def test_jest_unknown_outcome_dropped_refuses_not_partial_green():
    # 2 passed + an unknown "2 flaky" != 4 total: a silent parse would land green with two
    # tests unaccounted for. The sum check forces None -> the caller refuses.
    assert R.parse_jest_summary("Tests:       2 flaky, 2 passed, 4 total\n") is None


def test_jest_bare_total_refuses():
    # a total with no counted outcomes summing to it -> None (not a red about nothing)
    assert R.parse_jest_summary("Tests:       4 total\n") is None


def test_jest_outcomes_summing_to_total_parse():
    got = R.parse_jest_summary("Tests:       1 skipped, 2 passed, 3 total\n")
    assert got == {"passed": 2, "failed": 0, "errors": 0, "skipped": 1, "selected": 3}


def test_dispatch_unknown_runner_raises():
    with pytest.raises(KeyError):
        R.parse_runner_summary("nosuch", "whatever")


# ---- end-to-end: cargo in a bound lane --------------------------------------

def _git(r: Path, *a: str) -> str:
    return subprocess.run(["git", "-C", str(r), *a], text=True, capture_output=True, check=True).stdout.strip()


def _cargo_lane(tmp_path: Path, review_install: str | None = None, gitignore: bool = True) -> Path:
    lane = tmp_path / "lane"
    lane.mkdir()
    (lane / "Cargo.toml").write_text(
        '[package]\nname = "onetest"\nversion = "0.0.0"\nedition = "2021"\n'
    )
    (lane / "src").mkdir()
    (lane / "src" / "lib.rs").write_text(
        "pub fn add(a:i32,b:i32)->i32{a+b}\n"
        "#[cfg(test)] mod t{use super::*; #[test] fn ok(){assert_eq!(add(2,2),4);}}\n"
    )
    _git(lane, "init", "-q")
    _git(lane, "config", "user.email", "a@b")
    _git(lane, "config", "user.name", "a")
    if gitignore:
        (lane / ".gitignore").write_text("target/\n")
    if review_install is not None:
        # .review-install is committed in a real repo (the repo declares itself), so it
        # is part of the bound tree and does not read as untracked drift.
        (lane / ".review-install").write_text(review_install)
    _git(lane, "add", "-A")
    _git(lane, "commit", "-q", "-m", "c1", "--no-gpg-sign")
    tree = rr.compute_working_tree(lane)
    marker = {
        "kind": "open-gr-reconstruct",
        "gr_commit": "gr:test",
        "repos": [{"key": "r", "bound_head": _git(lane, "rev-parse", "HEAD"), "bound_head_tree": tree}],
    }
    (lane / rr._MARKER_NAME).write_text(json.dumps(marker) + "\n")
    return lane


@pytest.mark.skipif(shutil.which("cargo") is None, reason="cargo not on this host")
def test_cargo_lane_runs_green_with_counts_from_summary(tmp_path):
    lane = _cargo_lane(tmp_path)
    rec = rr.run_test_command_in_lane(lane, runner="cargo", test_command=["cargo", "test"])
    assert rec["result"] == "green"
    assert rec["passed"] == 1 and rec["failed"] == 0 and rec["selected"] == 1
    assert rec["runner"] == "cargo"


@pytest.mark.skipif(shutil.which("cargo") is None, reason="cargo not on this host")
def test_cargo_compile_error_refuses_with_raw_tail(tmp_path):
    lane = _cargo_lane(tmp_path)
    # Break the crate and COMMIT it, then bind the marker to the committed tree so the
    # tree-drift check passes and the refusal is the runner's own (unparseable summary),
    # not tree_drift.
    (lane / "src" / "lib.rs").write_text("fn broken( { }\n")
    _git(lane, "commit", "-aq", "-m", "break", "--no-gpg-sign")
    tree = rr.compute_working_tree(lane)
    marker = json.loads((lane / rr._MARKER_NAME).read_text())
    marker["repos"][0]["bound_head_tree"] = tree
    (lane / rr._MARKER_NAME).write_text(json.dumps(marker) + "\n")
    with pytest.raises(rr.ReviewRunRefused) as ei:
        rr.run_test_command_in_lane(lane, runner="cargo", test_command=["cargo", "test"])
    assert ei.value.code == "unparseable_summary"
    assert "raw tail:" in ei.value.detail


def test_unknown_runner_refuses(tmp_path):
    lane = _cargo_lane(tmp_path)
    with pytest.raises(rr.ReviewRunRefused) as ei:
        rr.run_test_command_in_lane(lane, runner="nosuch", test_command=["true"])
    assert ei.value.code == "unknown_runner"


# ---- CLI wiring: `gr2 review run --runner cargo --test`, and the hint form --------

@pytest.mark.skipif(shutil.which("cargo") is None, reason="cargo not on this host")
def test_cli_review_run_cargo_flags(tmp_path):
    from typer.testing import CliRunner
    from python_cli.app import app

    lane = _cargo_lane(tmp_path)
    r = CliRunner().invoke(app, ["review", "run", str(lane), "--runner", "cargo", "--test", "cargo test"])
    assert r.exit_code == 0
    assert "green (cargo)" in r.stdout and "passed=1" in r.stdout


@pytest.mark.skipif(shutil.which("cargo") is None, reason="cargo not on this host")
def test_cli_review_run_cargo_from_review_install_hint(tmp_path):
    """A stranger types nothing: .review-install declares the runner and test line."""
    from typer.testing import CliRunner
    from python_cli.app import app

    lane = _cargo_lane(tmp_path, review_install="runner = cargo\ntest = cargo test\n")
    r = CliRunner().invoke(app, ["review", "run", str(lane)])
    assert r.exit_code == 0
    assert "green (cargo)" in r.stdout


def test_cli_non_pytest_runner_without_test_command_refuses(tmp_path):
    from typer.testing import CliRunner
    from python_cli.app import app

    lane = _cargo_lane(tmp_path)
    r = CliRunner().invoke(app, ["review", "run", str(lane), "--runner", "cargo"])
    assert r.exit_code == 2
    assert "no_test_command" in r.stdout or "needs a test command" in (r.stdout + (r.stderr or ""))


@pytest.mark.skipif(shutil.which("cargo") is None, reason="cargo not on this host")
def test_two_consecutive_runs_both_green_on_un_gitignored_lane(tmp_path):
    """A library crate with no .gitignore: `cargo test` creates target/ and Cargo.lock.
    The FIRST run is green; without exempting the runner's own outputs the SECOND run
    refuses untracked_drift on them. Both runs must be green."""
    lane = _cargo_lane(tmp_path, gitignore=False)  # no .gitignore: target/ + Cargo.lock untracked after run 1
    rec1 = rr.run_test_command_in_lane(lane, runner="cargo", test_command=["cargo", "test"])
    assert rec1["result"] == "green"
    # the run created its own artifacts, untracked
    porcelain = _git(lane, "status", "--porcelain")
    assert "Cargo.lock" in porcelain and "target/" in porcelain
    rec2 = rr.run_test_command_in_lane(lane, runner="cargo", test_command=["cargo", "test"])
    assert rec2["result"] == "green"  # not a untracked_drift refusal on the run's own outputs


def test_pytest_runner_with_test_flag_refuses(tmp_path):
    """Fix-forward: --test with the pytest runner refuses instead of silently ignoring
    the command (which would run pytest and call the result green about the wrong thing)."""
    from typer.testing import CliRunner
    from python_cli.app import app

    lane = _cargo_lane(tmp_path)
    r = CliRunner().invoke(app, ["review", "run", str(lane), "--test", "cargo test"])
    assert r.exit_code == 2
    assert "test_with_pytest" in (r.stdout + (r.stderr or "")) or "for a non-pytest runner" in (r.stdout + (r.stderr or ""))
