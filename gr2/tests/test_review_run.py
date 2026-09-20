"""`review run <lane-dir>`: the review-owned in-lane test run (venv + install +
pytest folded into one verb). A green is trustworthy only because it is bound two
ways — the lane tree equals the bound head-tree, and the import resolves under the
lane — and because counts come from pytest's summary line, never the exit code.

Fast unit witnesses cover the two bindings, the summary parser, and the refusals;
an integration witness runs a real venv end-to-end (offline: the install command
seeds a `.pth`, and the current sys.path is added so host pytest is importable).
"""
from __future__ import annotations

import json
import shlex
import site
import subprocess
import sys
from pathlib import Path

import pytest

from gr2.python_cli import review_run as rr


def _git(cwd: Path, *a: str) -> str:
    return subprocess.run(["git", *a], cwd=cwd, text=True, capture_output=True, check=True).stdout.strip()


def _pkg_repo(tmp_path: Path, *, test_body: str) -> tuple[Path, str]:
    """A git repo holding a trivial installable package `demo_pkg` and a test file.
    Returns (repo_dir, head_tree)."""
    repo = tmp_path / "lane"
    (repo / "src" / "demo_pkg").mkdir(parents=True)
    (repo / "tests").mkdir()
    (repo / "src" / "demo_pkg" / "__init__.py").write_text("VALUE = 1\n")
    (repo / "pyproject.toml").write_text(
        "[build-system]\nrequires=['setuptools']\nbuild-backend='setuptools.build_meta'\n"
        "[project]\nname='demo_pkg'\nversion='0.0.0'\n"
        "[tool.setuptools.packages.find]\nwhere=['src']\n"
    )
    (repo / "tests" / "test_demo.py").write_text(test_body)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "a@e.invalid")
    _git(repo, "config", "user.name", "a")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "pkg")
    head = _git(repo, "rev-parse", "HEAD")
    head_tree = _git(repo, "rev-parse", "HEAD^{tree}")
    return repo, head_tree


def _write_marker(lane_dir: Path, head: str, head_tree: str, *, key: str = "alpha") -> None:
    marker = {
        "kind": "open-gr-reconstruct",
        "gr_commit": "deadbeef" * 5,
        "repos": [{
            "key": key,
            "reconstructed_head": head,
            "bound_head": head,
            "bound_head_tree": head_tree,
            "reconstructed_tree": head_tree,
            "tree_match": True,
        }],
    }
    (lane_dir / rr._MARKER_NAME).write_text(json.dumps(marker, indent=2) + "\n")


# offline install: seed a .pth so demo_pkg resolves under the lane AND host pytest
# (on the current sys.path) is importable in the lane venv — no network.
def _offline_install(lane: Path) -> list[str]:
    vpy = lane / rr._VENV_DIRNAME / "bin" / "python"
    paths = [str(lane / "src"), *[p for p in sys.path if p]]
    script = (
        "import site,sys,pathlib;"
        "sp=pathlib.Path(site.getsitepackages()[0]);"
        "sp.mkdir(parents=True,exist_ok=True);"
        "(sp/'zz_lane.pth').write_text('\\n'.join(sys.argv[1:])+'\\n')"
    )
    return [str(vpy), "-c", script, *paths]


# Like _offline_install, but ALSO writes a console-script SHIM into the
# lane venv's own bin/ -- an offline stand-in for what a real editable install's
# `entry_points.txt` -> console_scripts machinery would write there, so the witness
# below needs no network and no host-bundled setuptools (this venv has neither).
def _offline_install_with_console_script(lane: Path) -> list[str]:
    vpy = lane / rr._VENV_DIRNAME / "bin" / "python"
    paths = [str(lane / "src"), *[p for p in sys.path if p]]
    script = (
        "import site,stat,sys,pathlib;"
        "sp=pathlib.Path(site.getsitepackages()[0]);"
        "sp.mkdir(parents=True,exist_ok=True);"
        "(sp/'zz_lane.pth').write_text('\\n'.join(sys.argv[1:])+'\\n');"
        "binf=pathlib.Path(sys.executable).parent/'demo_pkg_cli';"
        "binf.write_text('#!'+sys.executable+'\\nprint(\"demo_pkg_cli ok\")\\n');"
        "binf.chmod(binf.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)"
    )
    return [str(vpy), "-c", script, *paths]


PASS_TEST = "from demo_pkg import VALUE\n\ndef test_ok():\n    assert VALUE == 1\n"

CONSOLE_SCRIPT_TEST = (
    "import shutil, subprocess\n\n"
    "def test_console_script_resolves_on_path():\n"
    "    found = shutil.which('demo_pkg_cli')\n"
    "    assert found is not None, 'demo_pkg_cli not found on PATH'\n"
    "    result = subprocess.run(['demo_pkg_cli'], capture_output=True, text=True)\n"
    "    assert result.returncode == 0 and 'demo_pkg_cli ok' in result.stdout, result\n"
)


# ---------------------------------------------------------------- summary parse

def test_parse_summary_normal_green():
    s = rr.parse_pytest_summary("collected 3 items\n\n===== 3 passed in 0.01s =====\n")
    assert s is not None and s["selected"] == 3 and s["passed"] == 3 and s["failed"] == 0


def test_parse_summary_counts_from_the_line_not_the_exit_code():
    s = rr.parse_pytest_summary("collected 3 items\n\n=== 1 failed, 2 passed in 0.1s ===\n")
    assert s["passed"] == 2 and s["failed"] == 1


def test_parse_summary_no_tests_ran_is_zero_selected():
    s = rr.parse_pytest_summary(
        "collected 3800 items / 3800 deselected / 0 selected\n\n"
        "===== no tests ran in 0.20s =====\n"
    )
    assert s is not None and s["selected"] == 0 and s["deselected"] == 3800


def test_parse_summary_records_the_selection_visibly():
    s = rr.parse_pytest_summary(
        "collected 3800 items / 3620 deselected / 180 selected\n\n"
        "===== 180 passed, 3620 deselected in 2.0s =====\n"
    )
    assert s["collected"] == 3800 and s["deselected"] == 3620 and s["selected"] == 180


def test_parse_summary_unparseable_is_none():
    assert rr.parse_pytest_summary("Traceback...\nImportError: boom\n") is None


def test_parse_failed_ids_from_real_pytest_summary():
    # review-run door 1: the node ids come from pytest's `-rfE` short test summary.
    # The fixture is real pytest output shape and includes the two ids a naive parser
    # gets wrong: an ERROR-at-setup node (status word ERROR, not FAILED) and a
    # PARAMETRIZED id whose brackets must be kept intact so the reviewer can re-run
    # exactly that case.
    out = (
        "collected 4 items\n\n"
        "tests/test_math.py .FF                                              [ 75%]\n"
        "tests/test_db.py E                                                  [100%]\n\n"
        "==================================== ERRORS ====================================\n"
        "____________________ ERROR at setup of test_query ____________________\n"
        "...fixture 'conn' not found...\n"
        "=========================== short test summary info ============================\n"
        "FAILED tests/test_math.py::test_add - assert 1 == 2\n"
        "FAILED tests/test_math.py::test_param[case-2 with spaces] - AssertionError\n"
        "ERROR tests/test_db.py::test_query - fixture 'conn' not found\n"
        "======================= 2 failed, 1 error in 0.12s ========================\n"
    )
    ids = rr.parse_failed_ids(out)
    assert ids == [
        "tests/test_db.py::test_query",
        "tests/test_math.py::test_add",
        "tests/test_math.py::test_param[case-2 with spaces]",
    ], ids


def test_parse_failed_ids_keeps_a_parametrized_id_with_spaces_whole():
    # A parametrized id can contain spaces inside its brackets; `\\S+` would truncate
    # at the first space. Pin the whole id so the reviewer can re-run exactly it.
    out = (
        "=========================== short test summary info ============================\n"
        "FAILED tests/t.py::test_param[case 2 with spaces] - AssertionError\n"
    )
    assert rr.parse_failed_ids(out) == ["tests/t.py::test_param[case 2 with spaces]"]


def test_parse_failed_ids_empty_when_all_pass():
    out = "collected 3 items\n\n===== 3 passed in 0.01s =====\n"
    assert rr.parse_failed_ids(out) == []


def test_merge_report_flags_puts_fE_last_and_drops_N():
    # Sentinel R1 (v3, measured): pytest's -r is LAST-WINS across tokens AND its chars
    # are processed IN ORDER, with N (none) CLEARING everything before it. So f/E must
    # (a) win over any later caller -r and (b) come AFTER any N, or a sorted union like
    # -rENf drops ERROR (E added, N clears, f added). Fix: drop N (the run requires
    # output), keep caller chars in order, append f/E LAST.
    def r(args):
        out = rr.merge_report_flags(args)
        assert out[0].startswith("-r"), out
        assert sum(1 for a in out if a == "-r" or (a.startswith("-r") and len(a) > 2)) == 1, out
        return out[0][2:], out[1:]

    chars, rest = r(["-q"]); assert chars == "fE" and rest == ["-q"]
    chars, rest = r(["-rN", "-q"]); assert chars == "fE" and rest == ["-q"]   # N dropped
    chars, _ = r(["-rs"]); assert chars == "sfE"                              # caller kept, fE last
    chars, rest = r(["-r", "sx", "-q"]); assert chars == "sxfE" and rest == ["-q"]
    chars, _ = r(["-rNEf"]); assert chars == "fE"                             # N gone, E/f re-appended last
    chars, _ = r(["-rA"]); assert chars == "AfE"                             # A (all) stays ahead of f/E
    chars, _ = r([]); assert chars == "fE"
    chars, _ = r(["-rN", "-q", "-rsx"]); assert chars == "sxfE"              # collapse, N gone, fE last
    # invariant across shapes: no N survives, and the spec ENDS with f then E
    for a in (["-rN"], ["-rxN"], ["-rA"], ["-q"], ["-r", "Ns"]):
        c = rr.merge_report_flags(a)[0][2:]
        assert "N" not in c and c.endswith("fE"), (a, c)


# ---------------------------------------------------- THE tree comparison + drift

def test_tree_bound_passes_on_a_pristine_reconstruction(tmp_path: Path):
    repo, head_tree = _pkg_repo(tmp_path, test_body=PASS_TEST)
    assert rr.assert_lane_tree_bound(repo, head_tree) == head_tree


def test_tree_bound_refuses_a_lane_drifted_after_open(tmp_path: Path):
    # Drift witness: touch a tracked file after open -> the working tree no longer
    # equals the bound head-tree. THE mutation that drops the tree comparison must
    # red THIS test alone.
    repo, head_tree = _pkg_repo(tmp_path, test_body=PASS_TEST)
    (repo / "src" / "demo_pkg" / "__init__.py").write_text("VALUE = 999\n")  # drift
    with pytest.raises(rr.ReviewRunRefused, match="tree_drift"):
        rr.assert_lane_tree_bound(repo, head_tree)


def test_tree_bound_refuses_when_the_marker_records_no_bound_tree(tmp_path: Path):
    repo, _ = _pkg_repo(tmp_path, test_body=PASS_TEST)
    with pytest.raises(rr.ReviewRunRefused, match="no_bound_tree"):
        rr.assert_lane_tree_bound(repo, "")


# ---------------------------------------------------- untracked drift (the 2nd half)

def test_untracked_drift_passes_with_only_run_created_paths(tmp_path: Path):
    # Pristine control: the marker, the lane .venv, an egg-info, and __pycache__ are
    # all things the open/run create -> not drift.
    repo, head_tree = _pkg_repo(tmp_path, test_body=PASS_TEST)
    _write_marker(repo, _git(repo, "rev-parse", "HEAD"), head_tree)
    (repo / ".venv" / "bin").mkdir(parents=True)
    (repo / ".venv" / "bin" / "python").write_text("")
    (repo / "src" / "demo_pkg.egg-info").mkdir()
    (repo / "src" / "demo_pkg.egg-info" / "PKG-INFO").write_text("")
    (repo / "__pycache__").mkdir()
    (repo / "__pycache__" / "x.pyc").write_text("")
    rr.assert_no_untracked_drift(repo)  # no raise


def test_untracked_drift_refuses_an_injected_conftest(tmp_path: Path):
    # An untracked conftest.py in the lane root is invisible to `add -u`, so the
    # tracked-tree comparison passes; the untracked scan is what catches it. Dropping
    # the untracked scan reds THIS witness alone.
    repo, head_tree = _pkg_repo(tmp_path, test_body=PASS_TEST)
    _write_marker(repo, _git(repo, "rev-parse", "HEAD"), head_tree)
    (repo / "conftest.py").write_text("# injected, not tracked\n")
    assert rr.assert_lane_tree_bound(repo, head_tree) == head_tree  # tracked tree still 'clean'
    with pytest.raises(rr.ReviewRunRefused, match="untracked_drift"):
        rr.assert_no_untracked_drift(repo)


# ----------------------------------------------------------- import under the lane

def test_import_under_the_lane_accepts_a_path_inside(tmp_path: Path):
    lane = tmp_path / "lane"
    (lane / "src" / "demo_pkg").mkdir(parents=True)
    inside = lane / "src" / "demo_pkg" / "__init__.py"
    inside.write_text("")
    rr.assert_import_under_lane(str(inside), lane)  # no raise


def test_import_under_the_lane_refuses_a_path_outside(tmp_path: Path):
    lane = tmp_path / "lane"
    lane.mkdir()
    outside = tmp_path / "other_checkout" / "demo_pkg" / "__init__.py"
    outside.parent.mkdir(parents=True)
    outside.write_text("")
    with pytest.raises(rr.ReviewRunRefused, match="import_escapes_lane"):
        rr.assert_import_under_lane(str(outside), lane)


# --------------------------------------------------------------- verb refusals

def test_run_refuses_a_dir_with_no_marker(tmp_path: Path):
    d = tmp_path / "not-a-lane"
    d.mkdir()
    with pytest.raises(rr.ReviewRunRefused, match="no_marker"):
        rr.run_review_lane(d, package="demo_pkg", pytest_args=[])


def test_run_refuses_a_multi_repo_lane(tmp_path: Path):
    lane = tmp_path / "lane"
    lane.mkdir()
    marker = {
        "kind": "open-gr-reconstruct", "gr_commit": "x",
        "repos": [{"key": "a", "bound_head_tree": "t1"}, {"key": "b", "bound_head_tree": "t2"}],
    }
    (lane / rr._MARKER_NAME).write_text(json.dumps(marker))
    with pytest.raises(rr.ReviewRunRefused, match="multi_repo_lane"):
        rr.run_review_lane(lane, package="demo_pkg", pytest_args=[])


# ------------------------------------------------------------------ integration

def test_run_green_records_a_bound_receipt(tmp_path: Path):
    repo, head_tree = _pkg_repo(tmp_path, test_body=PASS_TEST)
    _write_marker(repo, _git(repo, "rev-parse", "HEAD"), head_tree)
    receipt = rr.run_review_lane(
        repo, package="demo_pkg", pytest_args=["-q"], install=_offline_install(repo),
    )
    assert receipt["result"] == "green"
    assert receipt["selected"] >= 1 and receipt["passed"] >= 1 and receipt["failed"] == 0
    assert receipt["bound_head_tree"] == head_tree
    # the install is bound to the lane
    assert str(repo.resolve()) in receipt["resolved_install_path"]
    # the exact test command is recorded (Stromus addition 3)
    assert receipt["test_command"][-1] == "-q" and "pytest" in receipt["test_command"]
    # receipt persisted in the lane, so close-gr reclaims it too
    assert (repo / rr._RECEIPT_NAME).exists()


def test_run_red_receipt_names_the_failed_ids_and_keeps_the_output(tmp_path: Path):
    # review-run door 1: a red run's receipt records only COUNTS (`failed: 35`) with
    # no way back to WHICH tests failed, and the raw pytest output is never persisted
    # at all -- so a reviewer reading the receipt (or the coordinator reading a pasted
    # one) cannot see the failures. Real fixture: my own real review, 35 env failures
    # with no ids. The receipt must carry the failed node ids AND the run must write
    # the full pytest output to a log file in the lane.
    body = (
        "from demo_pkg import VALUE\n\n"
        "def test_ok():\n    assert VALUE == 1\n\n"
        "def test_bad():\n    assert VALUE == 2\n\n"
        "def test_also_bad():\n    raise RuntimeError('boom')\n"
    )
    repo, head_tree = _pkg_repo(tmp_path, test_body=body)
    _write_marker(repo, _git(repo, "rev-parse", "HEAD"), head_tree)
    receipt = rr.run_review_lane(
        repo, package="demo_pkg", pytest_args=["-q"], install=_offline_install(repo),
    )
    assert receipt["result"] == "red"
    assert receipt["failed"] >= 1
    # the node ids of the failures are recoverable from the receipt, not just a count.
    ids = receipt["failed_ids"]
    assert any(nid.endswith("::test_bad") for nid in ids), ids
    assert any(nid.endswith("::test_also_bad") for nid in ids), ids
    assert not any(nid.endswith("::test_ok") for nid in ids), ids
    # every id names a real node (path::test), so a reader can re-run exactly it.
    assert all("::" in nid for nid in ids), ids
    # and the full pytest output is persisted in the lane (survives to close, below).
    log = repo / rr._OUTPUT_LOG_NAME
    assert log.is_file(), "review run must write the pytest output to a log file"
    assert "test_bad" in log.read_text()
    assert receipt["output_log"] == rr._OUTPUT_LOG_NAME


def test_run_red_ids_survive_a_caller_rN_with_a_setup_error(tmp_path: Path):
    # Sentinel R1 (v3, measured): a caller -rN must not suppress EITHER FAILED or ERROR
    # ids. The v2 witness had only a plain failure, so a sorted union `-rENf` (where N
    # clears the E that precedes it) still PASSED it while silently dropping ERROR ids.
    # This fixture has BOTH a plain assertion failure AND an ERROR-at-setup (a fixture
    # that raises), so the ERROR path is exercised: both ids must come back.
    body = (
        "import pytest\n"
        "from demo_pkg import VALUE\n\n"
        "@pytest.fixture\n"
        "def boom():\n    raise RuntimeError('setup fail')\n\n"
        "def test_ok():\n    assert VALUE == 1\n\n"
        "def test_bad():\n    assert VALUE == 2\n\n"
        "def test_errored(boom):\n    assert True\n"
    )
    repo, head_tree = _pkg_repo(tmp_path, test_body=body)
    _write_marker(repo, _git(repo, "rev-parse", "HEAD"), head_tree)
    receipt = rr.run_review_lane(
        repo, package="demo_pkg", pytest_args=["-q", "-rN"], install=_offline_install(repo),
    )
    assert receipt["result"] == "red"
    ids = receipt["failed_ids"]
    assert any(nid.endswith("::test_bad") for nid in ids), ids       # FAILED survives
    assert any(nid.endswith("::test_errored") for nid in ids), ids   # ERROR survives (the v2 gap)


def test_run_refuses_when_a_k_filter_selects_zero_tests(tmp_path: Path):
    # Stromus addition 2: zero selected is a refusal, not a green — from the summary
    # line, not the exit code (pytest exits 0 having run nothing).
    repo, head_tree = _pkg_repo(tmp_path, test_body=PASS_TEST)
    _write_marker(repo, _git(repo, "rev-parse", "HEAD"), head_tree)
    with pytest.raises(rr.ReviewRunRefused, match="zero_collected"):
        rr.run_review_lane(
            repo, package="demo_pkg",
            pytest_args=["-q", "-k", "no_such_test_name_matches_this"],
            install=_offline_install(repo),
        )


def test_run_refuses_an_untracked_conftest_that_would_fake_a_pass(tmp_path: Path):
    # The central-claim probe (Stromus R2): an untracked conftest.py that patches the
    # package turns a red tree green. The run must REFUSE before it can run — the
    # tracked tree is unchanged, so only the untracked scan stops it.
    repo, head_tree = _pkg_repo(tmp_path, test_body="def test_ok():\n    assert False\n")
    _write_marker(repo, _git(repo, "rev-parse", "HEAD"), head_tree)
    (repo / "conftest.py").write_text(
        "import demo_pkg\n\n"
        "def pytest_configure(config):\n"
        "    demo_pkg.VALUE = 1  # a patch the tracked tree never shows\n"
    )
    with pytest.raises(rr.ReviewRunRefused, match="untracked_drift"):
        rr.run_review_lane(
            repo, package="demo_pkg", pytest_args=["-q"], install=_offline_install(repo),
        )


def test_run_all_skipped_is_not_green(tmp_path: Path):
    # Stromus R2: a green needs passed >= 1. An all-skipped run has no failure but
    # proves nothing. Dropping the passed>=1 condition reds THIS witness.
    repo, head_tree = _pkg_repo(
        tmp_path, test_body="import pytest\n\ndef test_x():\n    pytest.skip('nope')\n"
    )
    _write_marker(repo, _git(repo, "rev-parse", "HEAD"), head_tree)
    receipt = rr.run_review_lane(
        repo, package="demo_pkg", pytest_args=["-q"], install=_offline_install(repo),
    )
    assert receipt["result"] != "green"
    assert receipt["passed"] == 0 and receipt["skipped"] >= 1 and receipt["selected"] >= 1


def test_run_refuses_when_the_import_escapes_the_lane(tmp_path: Path):
    # Stromus addition 4b: a second checkout shadowing the package is a refusal.
    # the import isolation change moved the escape VECTOR that matters: the check runs
    # with -I, so a PYTHONPATH shadow is IGNORED entirely (it can no longer escape,
    # see the -I test) -- but a rogue reachable from the venv's OWN site (a stale
    # editable install pointing at another checkout) survives -I and must still
    # refuse. Install NOTHING under the lane; put the rogue demo_pkg on the venv site
    # path, so `import demo_pkg` resolves OUTSIDE the lane.
    repo, head_tree = _pkg_repo(tmp_path, test_body=PASS_TEST)
    _write_marker(repo, _git(repo, "rev-parse", "HEAD"), head_tree)
    rogue = tmp_path / "rogue_checkout"
    (rogue / "demo_pkg").mkdir(parents=True)
    (rogue / "demo_pkg" / "__init__.py").write_text("VALUE = 2\n")
    # install command that makes host pytest importable AND puts the rogue checkout
    # on the venv site path (NOT demo_pkg under the lane) -- a stale-editable-install
    # shadow, the vector -I does not neutralize.
    vpy = repo / rr._VENV_DIRNAME / "bin" / "python"
    script = (
        "import site,sys,pathlib;"
        "sp=pathlib.Path(site.getsitepackages()[0]);sp.mkdir(parents=True,exist_ok=True);"
        "(sp/'zz_host.pth').write_text('\\n'.join(sys.argv[1:])+'\\n')"
    )
    install = [str(vpy), "-c", script, str(rogue), *[p for p in sys.path if p]]
    with pytest.raises(rr.ReviewRunRefused, match="import_escapes_lane"):
        rr.run_review_lane(repo, package="demo_pkg", pytest_args=["-q"], install=install)


def test_run_a_pythonpath_rogue_does_not_change_the_run(tmp_path: Path, monkeypatch):
    # The RUN's counterpart to the venv-site escape test above. -I isolates the CHECK,
    # not the pytest RUN (which runs without -I): before the env scrub, a rogue package
    # on PYTHONPATH outside the lane was ignored by resolve_import_file (-I) yet imported
    # by pytest, so the check certified the lane while the run ran someone else's tree —
    # a receipt naming the lane about the wrong code. run_env now scrubs every PYTHON*
    # var and flows to BOTH checks and pytest, so a PYTHONPATH rogue does not change the
    # run's result: the lane's own demo_pkg (VALUE==1) wins and the run is green.
    #
    # This is the mutation witness for the scrub: the rogue's VALUE==2 makes PASS_TEST
    # (assert VALUE==1) fail, so dropping the scrub from run_env reds THIS test at the
    # assertion — the run would import the rogue. With the scrub it is green.
    repo, head_tree = _pkg_repo(tmp_path, test_body=PASS_TEST)
    _write_marker(repo, _git(repo, "rev-parse", "HEAD"), head_tree)
    rogue = tmp_path / "rogue_pythonpath"
    (rogue / "demo_pkg").mkdir(parents=True)
    (rogue / "demo_pkg" / "__init__.py").write_text("VALUE = 2\n")  # a DIFFERENT tree
    monkeypatch.setenv("PYTHONPATH", str(rogue))
    receipt = rr.run_review_lane(
        repo, package="demo_pkg", pytest_args=["-q"], install=_offline_install(repo),
    )
    # green: pytest imported the LANE's demo_pkg (VALUE==1), not the rogue (VALUE==2).
    assert receipt["result"] == "green", receipt
    assert receipt["passed"] >= 1 and receipt["failed"] == 0
    # and the receipt's install path is the lane, matching what the run actually ran.
    assert str(repo.resolve()) in receipt["resolved_install_path"]


def test_run_puts_the_lanes_venv_bin_on_the_pytest_subprocess_path(tmp_path: Path):
    """A repository's own tests can shell out to ITS OWN installed
    console script -- grip's own packaging test does exactly this
    (`gr2/tests/test_gr2_packaging.py::test_gr2_console_script_resolves`, run
    through `gr2`/`git review run` on grip's own clone). Without an
    activation-shaped subprocess env the lookup fails `FileNotFoundError`, RED, in a
    repo whose own developers would never see it: their shell has that venv's
    `bin/` on PATH before they ever run pytest by hand.

    Not hypothetical: Fathom's stranger dogfood hit this on grip's
    OWN suite -- a fresh outer venv, an ordinary clone, the four documented verbs,
    RED. Neither the author's nor either reviewer's own suite run had caught it,
    because all three of us activated a venv by hand before running -- exactly the
    ambient shell state a lane's own tests cannot assume when `run_review_lane`
    builds the subprocess env FRESH here rather than inheriting an activated shell's.

    The lane venv is a brand-new tmp_path-scoped directory every run, so it is
    never on this TEST process's own PATH to begin with -- "the parent PATH does
    not contain the venv" is the ambient default here, not something this test has
    to construct.
    """
    repo, head_tree = _pkg_repo(tmp_path, test_body=CONSOLE_SCRIPT_TEST)
    _write_marker(repo, _git(repo, "rev-parse", "HEAD"), head_tree)
    receipt = rr.run_review_lane(
        repo, package="demo_pkg", pytest_args=["-q"],
        install=_offline_install_with_console_script(repo),
    )
    assert receipt["result"] == "green", receipt
    assert receipt["passed"] >= 1 and receipt["failed"] == 0


# ------------------------------------------ install hint + pytest-absent cause

_SEED_SCRIPT = (
    "import site,sys,pathlib;"
    "sp=pathlib.Path(site.getsitepackages()[0]);"
    "sp.mkdir(parents=True,exist_ok=True);"
    "(sp/'zz_lane.pth').write_text('\\n'.join(sys.argv[1:])+'\\n')"
)


def _offline_install_no_pytest(lane: Path) -> list[str]:
    """Seed a .pth with ONLY the package's src — NOT host sys.path — so demo_pkg
    imports but pytest is absent from the lane venv."""
    vpy = lane / rr._VENV_DIRNAME / "bin" / "python"
    return [str(vpy), "-c", _SEED_SCRIPT, str(lane / "src")]


def _hint_install_string() -> str:
    """The offline install as a .review-install `install =` value, with {venv}/{lane}
    placeholders review run substitutes; includes host sys.path so pytest resolves."""
    paths = ["{lane}/src", *[p for p in sys.path if p]]
    return shlex.join(["{venv}", "-c", _SEED_SCRIPT, *paths])


def test_run_install_flag_substitutes_venv_and_lane(tmp_path: Path):
    # review-run door 2: the --install FLAG must substitute {venv}/{lane} exactly as
    # the .review-install hint does. Before the fix only the hint substituted, so a
    # reviewer who passed the documented `{venv} ... {lane}` template on the flag got
    # LITERAL braces -> the install binary "{venv}" does not exist -> install_failed.
    repo, head_tree = _pkg_repo(tmp_path, test_body=PASS_TEST)
    _write_marker(repo, _git(repo, "rev-parse", "HEAD"), head_tree)
    # the flag carries the SAME placeholder template the hint accepts
    install = shlex.split(_hint_install_string())
    assert "{venv}" in install and any("{lane}" in t for t in install), install
    receipt = rr.run_review_lane(
        repo, package="demo_pkg", pytest_args=["-q"], install=install,
    )
    assert receipt["result"] == "green", receipt
    # no literal placeholder survives, and {venv} resolved to the lane venv python
    assert not any("{venv}" in t or "{lane}" in t for t in receipt["install_command"]), receipt["install_command"]
    assert receipt["install_command"][0] == receipt["interpreter"]["path"]
    # {lane} resolved to the lane path
    assert str(repo.resolve() / "src") in receipt["install_command"], receipt["install_command"]
    assert receipt["install_source"] == "flag"


def test_run_refuses_pytest_absent_with_the_named_cause(tmp_path: Path):
    # half 1: an install that brings the package but NOT pytest must
    # refuse `pytest_not_installed`, NOT `unparseable_summary` (the wrong cause the
    # dogfood hit). Keep it a refusal either way.
    repo, head_tree = _pkg_repo(tmp_path, test_body=PASS_TEST)
    _write_marker(repo, _git(repo, "rev-parse", "HEAD"), head_tree)
    with pytest.raises(rr.ReviewRunRefused) as exc:
        rr.run_review_lane(
            repo, package="demo_pkg", pytest_args=["-q"],
            install=_offline_install_no_pytest(repo),
        )
    assert exc.value.code == "pytest_not_installed", exc.value.code


def test_review_install_hint_supplies_install_and_package(tmp_path: Path):
    # half 2: a repo that declares .review-install (tracked, so it is part
    # of the bound tree) runs green with NO --install and NO --package.
    repo, _ = _pkg_repo(tmp_path, test_body=PASS_TEST)
    (repo / ".review-install").write_text(
        f"# hint\ninstall = {_hint_install_string()}\npackage = demo_pkg\n"
    )
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "hint")
    head = _git(repo, "rev-parse", "HEAD")
    head_tree = _git(repo, "rev-parse", "HEAD^{tree}")
    _write_marker(repo, head, head_tree)
    receipt = rr.run_review_lane(repo, pytest_args=["-q"])  # no install, no package
    assert receipt["result"] == "green", receipt
    assert receipt["passed"] >= 1


def test_no_review_install_hint_still_needs_the_flags(tmp_path: Path):
    # Control: a repo WITHOUT the hint does not auto-resolve — read returns None and a
    # run with neither flag refuses `no_package`, so the hint is what removes the flag.
    repo, head_tree = _pkg_repo(tmp_path, test_body=PASS_TEST)
    _write_marker(repo, _git(repo, "rev-parse", "HEAD"), head_tree)
    assert rr.read_install_hint(repo) is None
    with pytest.raises(rr.ReviewRunRefused) as exc:
        rr.run_review_lane(repo, pytest_args=["-q"])  # no hint, no --package
    assert exc.value.code == "no_package", exc.value.code


# ---------------------------------------------------- v3: R2 REQUEST-CHANGES items

def test_hint_bad_binary_refuses_install_failed(tmp_path: Path):
    # P3 (the block): a committed hint whose install names a binary that does not
    # exist must REFUSE `install_failed` naming the command, never raise an uncaught
    # OSError traceback (the one shape review run promises never to give).
    repo, _ = _pkg_repo(tmp_path, test_body=PASS_TEST)
    (repo / ".review-install").write_text(
        "install = /nonexistent/binary --boom\npackage = demo_pkg\n"
    )
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "bad hint")
    head = _git(repo, "rev-parse", "HEAD")
    head_tree = _git(repo, "rev-parse", "HEAD^{tree}")
    _write_marker(repo, head, head_tree)
    with pytest.raises(rr.ReviewRunRefused) as exc:
        rr.run_review_lane(repo, pytest_args=["-q"])
    assert exc.value.code == "install_failed", exc.value.code
    assert "/nonexistent/binary" in exc.value.detail


def test_receipt_records_install_command_and_sources(tmp_path: Path):
    # P7: a hint-driven run and a flag-driven run must NOT render identical receipts.
    # Hint run: source is `hint`, and install_command shows the substituted command.
    repo, _ = _pkg_repo(tmp_path, test_body=PASS_TEST)
    (repo / ".review-install").write_text(
        f"install = {_hint_install_string()}\npackage = demo_pkg\n"
    )
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "hint")
    _write_marker(repo, _git(repo, "rev-parse", "HEAD"), _git(repo, "rev-parse", "HEAD^{tree}"))
    rcpt = rr.run_review_lane(repo, pytest_args=["-q"])
    assert rcpt["result"] == "green", rcpt
    assert rcpt["install_source"] == "hint", rcpt["install_source"]
    assert rcpt["package_source"] == "hint", rcpt["package_source"]
    assert any(str(repo / "src") in tok for tok in rcpt["install_command"]), rcpt["install_command"]

    # Flag run on a fresh lane: source is `flag`.
    repo2, head_tree2 = _pkg_repo(tmp_path / "second", test_body=PASS_TEST)
    _write_marker(repo2, _git(repo2, "rev-parse", "HEAD"), head_tree2)
    rcpt2 = rr.run_review_lane(
        repo2, package="demo_pkg", install=_offline_install(repo2), pytest_args=["-q"]
    )
    assert rcpt2["result"] == "green", rcpt2
    assert rcpt2["install_source"] == "flag", rcpt2["install_source"]
    assert rcpt2["package_source"] == "flag", rcpt2["package_source"]


def test_unknown_hint_key_refuses_bad_hint(tmp_path: Path):
    # P1: a typo'd key (`instal`) must refuse `bad_hint` naming the key, not fall
    # through to the default install and refuse under a cause the repo never declared.
    repo, _ = _pkg_repo(tmp_path, test_body=PASS_TEST)
    (repo / ".review-install").write_text("instal = whoops\npackage = demo_pkg\n")
    with pytest.raises(rr.ReviewRunRefused) as exc:
        rr.read_install_hint(repo)
    assert exc.value.code == "bad_hint", exc.value.code
    assert "instal" in exc.value.detail


# ------------------------------------- follow-on: undeclared extra (pip exits 0)

def test_detect_undeclared_extras_parses_pip_warning():
    # The pure detector: pip's real warning (captured from pip 25 against a package
    # with no extras), older pip's version-less spelling, and a clean install (none).
    out = (
        "Obtaining file:///x\n"
        "WARNING: demo_pkg 0.0.0 does not provide the extra 'alsobad'\n"
        "WARNING: demo_pkg 0.0.0 does not provide the extra 'bogus'\n"
        "Successfully installed demo_pkg-0.0.0\n"
    )
    assert rr.detect_undeclared_extras(out) == ["alsobad", "bogus"]  # sorted, unique
    assert rr.detect_undeclared_extras(
        "WARNING: pkg does not provide the extra 'x'"  # older pip: no version
    ) == ["x"]
    assert rr.detect_undeclared_extras("Successfully installed demo_pkg-0.0.0") == []


def _install_no_pytest_with_undeclared_extra_warning(lane: Path, extra: str) -> list[str]:
    """Like `_offline_install_no_pytest` (brings the package, NOT pytest) but also
    prints pip's real undeclared-extra WARNING on stderr and exits 0 — the exact
    shape of `pip install -e <lane>[<extra>]` when <extra> is a typo: pip warns,
    exits 0, and installs none of that extra's dependencies."""
    vpy = lane / rr._VENV_DIRNAME / "bin" / "python"
    script = (
        _SEED_SCRIPT
        + ";import sys;sys.stderr.write("
        + repr(f"WARNING: demo_pkg 0.0.0 does not provide the extra '{extra}'\n")
        + ")"
    )
    return [str(vpy), "-c", script, str(lane / "src")]


def test_run_refuses_undeclared_extra_naming_it_before_pytest(tmp_path: Path):
    # Root-cause naming. This is the SAME scenario as the pytest-absent test — the
    # install brings the package but not pytest — except the install ALSO emits pip's
    # undeclared-extra warning (a typo'd `[devv]`). The undeclared-extra check fires
    # first, so the run refuses `undeclared_extra` naming `devv` instead of the
    # misleading `pytest_not_installed` the reviewer would otherwise chase. Removing
    # the (4a) block flips this to `pytest_not_installed` (mutation witness).
    repo, head_tree = _pkg_repo(tmp_path, test_body=PASS_TEST)
    _write_marker(repo, _git(repo, "rev-parse", "HEAD"), head_tree)
    with pytest.raises(rr.ReviewRunRefused) as exc:
        rr.run_review_lane(
            repo, package="demo_pkg", pytest_args=["-q"],
            install=_install_no_pytest_with_undeclared_extra_warning(repo, "devv"),
        )
    assert exc.value.code == "undeclared_extra", exc.value.code
    assert "devv" in exc.value.detail


def test_spaced_lane_path_survives(tmp_path: Path):
    # P2: split the template FIRST, then substitute per token, so a lane path with a
    # space survives even with an unquoted {venv}/{lane} in the hint line. The prior
    # code substituted before shlex.split, so the space broke the token apart; this
    # test's helper writes the placeholders UNQUOTED (only the seed script + host
    # paths are quoted), which the earlier author suite's shlex.join helper could not.
    spaced = tmp_path / "has space"
    spaced.mkdir()
    repo, _ = _pkg_repo(spaced, test_body=PASS_TEST)
    assert " " in str(repo), str(repo)
    host = [shlex.quote(p) for p in sys.path if p]
    hint_install = " ".join(
        ["{venv}", "-c", shlex.quote(_SEED_SCRIPT), "{lane}/src", *host]
    )
    (repo / ".review-install").write_text(f"install = {hint_install}\npackage = demo_pkg\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "spaced hint")
    _write_marker(repo, _git(repo, "rev-parse", "HEAD"), _git(repo, "rev-parse", "HEAD^{tree}"))
    rcpt = rr.run_review_lane(repo, pytest_args=["-q"])
    assert rcpt["result"] == "green", rcpt
    assert rcpt["passed"] >= 1
