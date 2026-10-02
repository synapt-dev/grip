"""Contract: a nulled current-lane record refuses; it does not raise.

``lane exit`` writes ``{"current": null, "recent": []}``.  Every reader then
went straight to ``doc["current"]["lane_name"]`` and got
``TypeError: 'NoneType' object is not subscriptable`` -- a traceback in front
of the user, for a state gr2 writes itself, by the most ordinary sequence
there is: enter, then exit.

The readers guarded a MISSING FILE and not a NULL FIELD.  Two absent-shapes,
one of them handled, which is why the crash was invisible until a lane cycle
had completed: before any ``lane enter`` the file does not exist and the
refusal is clean, so a probe that runs ``lane current`` early sees a healthy
verb.

Measured on a scratch workspace before the fix: eight verbs raised --
``lane current``, ``lane exit`` (on its second call), ``exec status``,
``exec run``, ``pr status``, ``pr checks``, ``pr create``, ``pr merge``.
Four separate call sites had each written the unguarded read, so the fix is a
required accessor rather than a fourth copy of the guard: a fourth copy only
postpones the fifth.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from gr2.prototypes import lane_workspace_prototype as lane_proto


def _unit_with_current(tmp_path: Path, current) -> Path:
    """A workspace whose current-lane record holds ``current`` verbatim."""
    path = lane_proto.current_lane_file(tmp_path, "default")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"current": current, "recent": []}))
    return tmp_path


def test_a_nulled_record_refuses_rather_than_raising(tmp_path):
    """The production state: what ``lane exit`` actually leaves behind."""
    _unit_with_current(tmp_path, None)
    with pytest.raises(SystemExit) as excinfo:
        lane_proto.require_current_lane(tmp_path, "default")
    assert "no current lane recorded for unit: default" in str(excinfo.value)


def test_a_present_record_is_returned(tmp_path):
    """The control. Without this the test above passes in a world where the
    accessor refuses unconditionally, which would break all eight verbs."""
    _unit_with_current(tmp_path, {"lane_name": "feat-x", "owner_unit": "default"})
    assert lane_proto.require_current_lane(tmp_path, "default")["lane_name"] == "feat-x"


def test_a_missing_file_still_refuses_with_the_same_message(tmp_path):
    """The other absent-shape, which was always handled. Both must refuse
    identically: a caller cannot act differently on "never entered" than on
    "entered and exited", and two different messages would imply it could."""
    with pytest.raises(SystemExit) as excinfo:
        lane_proto.require_current_lane(tmp_path, "default")
    assert "no current lane recorded for unit: default" in str(excinfo.value)


def test_an_empty_dict_refuses_too(tmp_path):
    """``{}`` is falsy but not ``None``. A guard written as ``is None`` would
    admit it and then raise ``KeyError`` one line later, which is the same
    defect wearing a different exception."""
    _unit_with_current(tmp_path, {})
    with pytest.raises(SystemExit):
        lane_proto.require_current_lane(tmp_path, "default")


def test_no_shipped_caller_reads_the_field_unguarded():
    """The class, not the instances.

    Four call sites had independently written ``doc["current"][...]``.  This
    asserts none remain in shipped code, so a fifth cannot be added quietly.

    Parsed with ``ast`` rather than grepped.  A text search cannot tell code
    from prose and matched this module's own docstrings on the first run --
    the check reported a defect that did not exist, which is the same class of
    error as a check that misses one that does.  An AST walk sees expressions
    only.

    Playground and demo modules under ``gr2/gr2/prototypes/`` are excluded by path
    because they are not reachable from any registered verb; the exclusion is
    one named file's worth of surface and is listed here rather than implied.
    """
    import ast

    root = Path(__file__).resolve().parents[1]
    targets = [
        *(root / "gr2" / "python_cli").rglob("*.py"),
        root / "gr2" / "prototypes" / "lane_workspace_prototype.py",
    ]
    offenders = []
    for f in targets:
        tree = ast.parse(f.read_text(), filename=str(f))
        for node in ast.walk(tree):
            # ANY subscript with the constant key "current" -- doc["current"]
            # on its own, not only doc["current"]["lane_name"].  The narrower
            # two-level form was the first version and it MISSED one of the
            # four original call sites: the base file read
            # `current_doc = doc["current"]` on one line and subscripted the
            # result on the next, so the two-level walk saw nothing and
            # reported the defective file CLEAN.  A dict access is safe here
            # only via .get(), which is a Call and never matches.
            if not isinstance(node, ast.Subscript):
                continue
            key = node.slice
            if isinstance(key, ast.Constant) and key.value == "current":
                offenders.append(f"{f.name}:{node.lineno}")
    assert offenders == [], "unguarded current-lane reads: " + ", ".join(offenders)


def test_the_class_check_can_actually_fail():
    """The control for the check above.

    A class assertion that has never been shown to fail is indistinguishable
    from one whose query is wrong -- and this one's first version WAS wrong in
    the opposite direction. Feeding it the exact shape it hunts must produce a
    hit, or its empty result means nothing.
    """
    import ast

    def scan(src):
        return [
            n for n in ast.walk(ast.parse(src))
            if isinstance(n, ast.Subscript)
            and isinstance(n.slice, ast.Constant)
            and n.slice.value == "current"
        ]

    # The two-level shape three call sites used.  ONE hit, not two: the outer
    # subscript's key is "lane_name" and only the inner one keys on "current".
    assert len(scan('lane_name = doc["current"]["lane_name"]\n')) == 1

    # The ONE-LEVEL shape the fourth used, split across two statements.  The
    # first version of this detector walked for a subscript-of-a-subscript and
    # returned ZERO on this, which means it reported the file holding the
    # original defect as clean.  A check that cannot fail on the case it exists
    # for is not a check.
    assert len(scan('current_doc = doc["current"]\nname = current_doc["lane_name"]\n')) == 1

    # The safe form must NOT be flagged, or the check would fire on its own fix.
    assert scan('current = doc.get("current")\n') == []
