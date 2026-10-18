"""An in-test breakpoint() reads the runner's stdin, not /dev/null.

Whenever pytest leaves fd 0 alone (with capture off, -s / --capture=no, and
with --capture=sys or tee-sys), the session fixture in conftest.py is all that
stands between fd 0 and the runner's stdin, so the
pytest_enter_pdb hook puts the saved fd 0 back for pdb and pytest_leave_pdb
points it at /dev/null again. This test needs a pdb command on stdin, so it
runs only when asked:

    printf 'c\\n' | GR2_PDB_STDIN_PROBE=1 python -m pytest -q -s <this file>
"""
from __future__ import annotations

import os

import pytest


class _Probe:
    def __init__(self) -> None:
        self.in_pdb = None
        self.after_leave = None

    @pytest.hookimpl(trylast=True)
    def pytest_enter_pdb(self, config, pdb):
        self.in_pdb = os.fstat(0)

    @pytest.hookimpl(trylast=True)
    def pytest_leave_pdb(self, config, pdb):
        self.after_leave = os.fstat(0)


@pytest.mark.skipif(
    os.environ.get("GR2_PDB_STDIN_PROBE") != "1",
    reason="needs a pdb command on stdin; set GR2_PDB_STDIN_PROBE=1",
)
def test_breakpoint_reads_stdin_then_devnull_again(request):
    if request.config.getoption("capture") == "fd":
        pytest.skip("the default fd capture already hands pdb the terminal")
    devnull = os.stat(os.devnull)
    probe = _Probe()
    request.config.pluginmanager.register(probe, "gr2-pdb-stdin-probe")
    try:
        breakpoint()  # stdin feeds `c`; with fd 0 at /dev/null, pdb gets EOF
    finally:
        request.config.pluginmanager.unregister(probe)
    assert probe.in_pdb is not None
    assert not os.path.samestat(probe.in_pdb, devnull)
    assert os.path.samestat(probe.after_leave, devnull)
    assert os.path.samestat(os.fstat(0), devnull)
