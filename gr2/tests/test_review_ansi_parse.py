"""A coloured pytest run (FORCE_COLOR, a caller's own `--color=yes`) still reports its failed ids. The colour escapes
sit inside the summary and the `FAILED <id>` lines; the parsers strip them, so only the parse seam has to be right
for every input. Found reading a PR in the stranger-path work: under FORCE_COLOR=1 a red lane came back with an
empty failed-id list (the result stayed red)."""
from __future__ import annotations

from pathlib import Path

import pytest

from gr2.python_cli import review_run as rr
from tests.test_review_run import _git, _offline_install, _pkg_repo, _write_marker

BODY = (
    "from demo_pkg import VALUE\n\n"
    "def test_ok():\n    assert VALUE == 1\n\n"
    "def test_bad():\n    assert VALUE == 2\n"
)


def _red_lane(tmp_path: Path) -> Path:
    repo, head_tree = _pkg_repo(tmp_path, test_body=BODY)
    _write_marker(repo, _git(repo, "rev-parse", "HEAD"), head_tree)
    return repo


RED = "\x1b[31m"
GREEN = "\x1b[32m"
OFF = "\x1b[0m"


def test_the_parsers_read_a_coloured_summary_and_ids() -> None:
    out = (
        f"{RED}FAILED{OFF} t.py::test_bad - assert 1 == 2\n"
        f"{RED}ERROR{OFF} t.py::test_err\n"
        f"{RED}1 failed{OFF}, {GREEN}1 passed{OFF}, {RED}1 error{OFF} in 0.01s\n"
    )
    assert rr.parse_failed_ids(out) == ["t.py::test_bad", "t.py::test_err"]
    s = rr.parse_pytest_summary(out)
    assert s is not None and (s["passed"], s["failed"], s["errors"]) == (1, 1, 1)


def test_an_uncoloured_run_reads_the_same() -> None:
    out = "FAILED t.py::test_bad - assert 1 == 2\n1 failed, 1 passed in 0.01s\n"
    assert rr.parse_failed_ids(out) == ["t.py::test_bad"]
    assert rr.parse_pytest_summary(out)["failed"] == 1


def test_a_red_run_under_force_color_names_the_failed_id(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("FORCE_COLOR", "1")
    repo = _red_lane(tmp_path)
    receipt = rr.run_review_lane(repo, package="demo_pkg", pytest_args=["-q"], install=_offline_install(repo))
    assert receipt["result"] == "red"
    assert any(i.endswith("::test_bad") for i in receipt["failed_ids"]), receipt["failed_ids"]


def test_a_callers_own_color_yes_after_ours_still_names_the_failed_id(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("FORCE_COLOR", raising=False)
    repo = _red_lane(tmp_path)
    receipt = rr.run_review_lane(
        repo, package="demo_pkg", pytest_args=["-q", "--color=yes"], install=_offline_install(repo),
    )
    assert receipt["result"] == "red"
    assert any(i.endswith("::test_bad") for i in receipt["failed_ids"]), receipt["failed_ids"]


def test_the_uncoloured_control_names_the_failed_id(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("FORCE_COLOR", raising=False)
    monkeypatch.delenv("NO_COLOR", raising=False)
    repo = _red_lane(tmp_path)
    receipt = rr.run_review_lane(repo, package="demo_pkg", pytest_args=["-q"], install=_offline_install(repo))
    assert receipt["result"] == "red"
    assert any(i.endswith("::test_bad") for i in receipt["failed_ids"]), receipt["failed_ids"]


def test_an_escape_between_a_count_and_its_word_still_parses() -> None:
    # Colour plugins (and some pytest versions) close the colour right after the number.
    out = f"{RED}1{OFF} failed, {GREEN}2{OFF} passed in 0.01s\n"
    s = rr.parse_pytest_summary(out)
    assert s is not None and (s["passed"], s["failed"]) == (2, 1)


def test_a_cursor_or_erase_escape_before_a_failed_line_is_stripped() -> None:
    # Progress UIs (xdist, sugar) emit non-colour CSI codes such as erase-line; they must not hide a failed id.
    out = "\x1b[2K\x1b[1AFAILED t.py::test_bad - assert False\n1 failed in 0.01s\n"
    assert rr.parse_failed_ids(out) == ["t.py::test_bad"]


def test_an_osc8_hyperlink_round_a_failed_id_does_not_change_the_id() -> None:
    # A terminal-aware plugin wraps the node id in a hyperlink: ESC ] 8 ; ; url ST text ESC ] 8 ; ; ST.
    # Read raw, the url becomes part of the id (a WRONG id, worse than an empty one).
    for st in ("\x1b\\", "\x07"):
        link = f"\x1b]8;;file:///tmp/t.py{st}t.py::test_bad\x1b]8;;{st}"
        out = f"FAILED {link} - assert False\n1 failed in 0.01s\n"
        assert rr.parse_failed_ids(out) == ["t.py::test_bad"], repr(out)
