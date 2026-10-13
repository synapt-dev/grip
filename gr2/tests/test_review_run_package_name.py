"""`package` in `.review-install` (or --package) names an IMPORT, not a path.

`package = .` used to reach `python -c "import . as _m"` and come back as a raw SyntaxError traceback inside an
`import_failed` refusal. A value that is not a dotted Python name is now refused by name before any import runs.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from gr2.python_cli import review_run as rr
from tests.test_review_run import PASS_TEST, _offline_install, _pkg_repo, _write_marker, _git

NOT_NAMES = [".", "..", "src/demo", "demo pkg", "x; import os", "demo.", ".demo", "import", "demo.class", "1demo"]


@pytest.mark.parametrize("package", NOT_NAMES)
def test_a_package_that_is_not_an_import_name_is_refused_by_name_before_any_import(package):
    with pytest.raises(rr.ReviewRunRefused) as exc:
        rr.resolve_import_file(Path(sys.executable), package, {})
    assert exc.value.code == "import_failed"
    assert "is not an import name" in exc.value.detail and "SyntaxError" not in exc.value.detail, exc.value.detail


@pytest.mark.parametrize("package", ["demo_pkg", "demo.sub", "_private", "Demo2"])
def test_a_dotted_python_name_still_reaches_the_import(package):
    """The must-not-move rows: a real name is not refused as a non-name (it may fail to import, which is another refusal)."""
    with pytest.raises(rr.ReviewRunRefused) as exc:
        rr.resolve_import_file(Path(sys.executable), package, {})
    assert "is not an import name" not in exc.value.detail, exc.value.detail


def test_the_refusal_comes_through_a_review_run_with_package_dot(tmp_path):
    repo, head_tree = _pkg_repo(tmp_path, test_body=PASS_TEST)
    _write_marker(repo, _git(repo, "rev-parse", "HEAD"), head_tree)
    with pytest.raises(rr.ReviewRunRefused) as exc:
        rr.run_review_lane(repo, package=".", pytest_args=["-q"], install=_offline_install(repo))
    assert exc.value.code == "import_failed" and "is not an import name" in exc.value.detail, exc.value.detail
