"""The declared-set pin tie reads the target and every review in one ls-remote. A review ref whose name only shares
a prefix with another is never read as it, in either direction: rows are matched by their full name."""
from pathlib import Path

from gr2.python_cli import merge_gate

PREFIX = merge_gate._REVIEW_REF_PREFIX
SHORT, LONG = f"{PREFIX}lane-a", f"{PREFIX}lane-ab"
A, B = "a" * 40, "b" * 40


def _tips(monkeypatch, rows, listed):
    said = "".join(f"{oid}\t{name}\n" for oid, name in rows)
    monkeypatch.setattr(merge_gate, "_git", lambda repo, *args, **kw: said if args[0] == "ls-remote" else "")
    return merge_gate._remote_tips(Path("."), "origin", (), listed, scan=PREFIX)


def test_short_name_is_not_read_as_the_longer_one(monkeypatch):
    tips = _tips(monkeypatch, [(B, LONG)], (SHORT,))
    assert tips[SHORT] is None
    assert tips[LONG] == B


def test_long_name_is_not_read_as_the_shorter_one(monkeypatch):
    tips = _tips(monkeypatch, [(A, SHORT)], (LONG,))
    assert tips[LONG] is None
    assert tips[SHORT] == A


def test_both_present_each_reads_its_own(monkeypatch):
    tips = _tips(monkeypatch, [(A, SHORT), (B, LONG)], ())
    assert tips == {SHORT: A, LONG: B}


def test_a_name_holding_u2028_stays_one_row(monkeypatch):
    spoof = f"{SHORT} x"
    tips = _tips(monkeypatch, [(B, spoof)], (SHORT,))
    assert tips[SHORT] is None
    assert tips[spoof] == B
