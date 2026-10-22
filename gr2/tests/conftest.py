"""Shared fixtures for gr2 tests.

The suite-wide `_isolated_git_config` autouse fixture lives in the ROOT
conftest (`gr2/conftest.py`), not here -- it must be an ancestor of both
`tests/` and the sibling `gr2/overlay/tests/` tree, and this directory is
not.

CLI diagnostic witnesses assert displayed semantics after dependency-provided
strip_ansi (typer._click.utils, or click.utils on older Typer), not raw styled
option names. Styling can split a literal such as --root or --remote. Preserve
raw output and named diagnostics, exercise actual styled/plain modes, and assert
stripping changes only the styled output. Keep JSON assertions on stdout alone.
"""
from __future__ import annotations

import os

import pytest
from pathlib import Path
from typer.testing import CliRunner


_stdin: dict[str, int] = {}


@pytest.fixture(scope="session", autouse=True)
def _children_get_closed_stdin(request: pytest.FixtureRequest):
    """Point fd 0 at /dev/null for the whole session, so every child a test
    spawns inherits a closed stdin and reads EOF at once.

    A git (or any) subprocess that reads stdin and is given none inherits the
    runner's fd 0. Whenever pytest leaves fd 0 alone (with capture off, -s /
    --capture=no, and with --capture=sys or tee-sys) and stdin is a live pipe
    (an agent harness, ``cmd | pytest``), such a child waits on that pipe forever
    and the run hangs; only the default fd capture points fd 0 at /dev/null.
    The fixture stays unconditional so the capture mode never decides whether a
    run can hang. Skipped under pytest's own debugger options (--pdb, --trace),
    and an in-test breakpoint() gets the saved fd 0 back through the pdb hooks
    below, so interactive debugging still reads the terminal.
    """
    if request.config.getoption("usepdb") or request.config.getoption("trace"):
        yield
        return
    devnull = os.open(os.devnull, os.O_RDONLY)
    saved = os.dup(0)
    os.dup2(devnull, 0)
    _stdin.update(saved=saved, devnull=devnull)
    try:
        yield
    finally:
        _stdin.clear()
        os.dup2(saved, 0)
        os.close(saved)
        os.close(devnull)


# These modules fake git by replacing `subprocess.run`. When stdin is not a terminal (CI, a box, the
# fixture above), a timed git call leads its own process group and goes through `gitops._run_group`, which
# uses `subprocess.Popen`, so their stub is never reached and real git answers instead. They are kept on
# the plain `subprocess.run` path they were written for; every other module stays on the production path,
# and test_gitops_spawn_timeout_group.py holds the parity row between the two. A module whose `run` stub
# sits on a timed call and is missing here fails under CI (as these nine did) rather than passing.
_STUBS_SUBPROCESS_RUN = frozenset({
    "test_review_merge_target_moved", "test_review_merge_gate_lost_ack", "test_review_merge_push_failed",
    "test_git_error_redaction", "test_sync_ssh_rewrite", "test_sync_status_probe",
    "test_ancestry_unmeasurable_rows", "test_exec_apostrophe_and_oserror", "test_review_merge_feature_moved_next",
})


@pytest.fixture(autouse=True)
def _run_stubs_keep_the_plain_spawn(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch):
    if request.module.__name__.rsplit(".", 1)[-1] in _STUBS_SUBPROCESS_RUN:
        from gr2.python_cli import gitops
        monkeypatch.setattr(gitops, "_own_group", lambda env: False)
    yield


@pytest.fixture(autouse=True)
def _once_per_process_notices_start_empty():
    # Both sets dedup a notice once per process, keyed on absolute paths. A passed test's tmp dir is deleted
    # and the next test can reuse its path, so a line printed by one test would be swallowed in the next.
    from gr2.python_cli import lane_downstream, spec_apply
    lane_downstream._NOTICED.clear()
    spec_apply._ALPHA_FALLBACK_NOTICED.clear()
    yield


def pytest_enter_pdb(config, pdb):
    # Only when fd 0 is still our /dev/null: under the default fd capture,
    # pytest has already handed pdb the terminal by the time this hook runs.
    if _stdin and os.path.samestat(os.fstat(0), os.fstat(_stdin["devnull"])):
        os.dup2(_stdin["saved"], 0)
        _stdin["restored"] = 1


def pytest_leave_pdb(config, pdb):
    if _stdin.pop("restored", None):
        os.dup2(_stdin["devnull"], 0)


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """Create a minimal workspace with .grip/ directory."""
    grip = tmp_path / ".grip"
    grip.mkdir()
    events = grip / "events"
    events.mkdir()
    return tmp_path


def make_cli_runner() -> CliRunner:
    """A runner whose ``result.stdout`` is the command's stdout ALONE, on every
    click version.

    click<8.2 folds stderr into stdout unless ``mix_stderr=False``; click>=8.2
    removed ``mix_stderr`` and separates the streams by default (passing the kwarg
    raises ``TypeError``). Without this, ``json.loads(result.stdout)`` reds under
    click<8.2 whenever the command also writes a stderr warning (e.g. an
    UNVERIFIABLE merge-parent verdict prepended to the JSON payload) and greens
    under click>=8.2 -- a gate that flips on the ambient click version, not on the
    code under test. Two agents parse ``--json`` output as data, so the assertion
    must be pinned to the data channel regardless of the installed click.

    Lifted from test_sprint21_sync_platform.py's private ``_make_runner``
    into one shared copy so every test file that parses a command's stdout as JSON
    gets the same version-safety, rather than seven near-identical private copies
    drifting apart.
    """
    try:
        return CliRunner(mix_stderr=False)
    except TypeError:
        return CliRunner()
