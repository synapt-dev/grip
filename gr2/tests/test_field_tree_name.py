"""A review record stored as a Git tree with one entry per protobuf field is a "field tree".

The earlier working name is retired everywhere in gr2: module, functions, errors, tests and prose.
The one exception is the pinned schema file, whose bytes gr2 refuses to change (its comment
belongs to the schema's owner and changes there).
"""
from __future__ import annotations

import re
from pathlib import Path

GR2 = Path(__file__).resolve().parents[1]
SKIP = {".venv", ".git", "__pycache__", ".pytest_cache", "build", "dist"}
PINNED = {"gr2/schemas/dev.synapt.grip.review.v1alpha1/review.proto"}
# Spelled in parts, so this file does not match itself.
RETIRED = re.compile("form" + r"[ _-]?" + "d" + r"(?![a-z])", re.IGNORECASE)


def retired_name_hits(root: Path) -> list[str]:
    """Every file under root's source trees whose relative PATH or contents carry the retired name.
    Walks the trees, not the git index: the suite also runs from a git archive with no .git. Build
    output beside them (virtualenvs, egg-info) is not gr2's text."""
    hits = []
    sources = [root / "gr2", root / "tests", root / "api", root / "docs", root / "scripts",
               *root.glob("*.md"), *root.glob("*.toml")]
    for path in sorted(f for top in sources for f in ([top] if top.is_file() else top.rglob("*"))):
        rel = path.relative_to(root).as_posix()
        if not path.is_file() or rel in PINNED or set(path.relative_to(root).parts) & SKIP:
            continue
        if RETIRED.search(rel):
            hits.append(rel)
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        hits += [f"{rel}:{n}" for n, line in enumerate(text.splitlines(), 1) if RETIRED.search(line)]
    return hits


def test_the_retired_name_appears_nowhere_in_gr2_but_the_pinned_schema() -> None:
    hits = retired_name_hits(GR2)
    assert not hits, f"the retired name is back: {hits}"


def test_a_file_named_with_the_retired_name_is_caught_whatever_it_holds(tmp_path: Path) -> None:
    """A path is checked as well as contents: a clean file under the old name is still the old name."""
    tests = tmp_path / "tests"
    tests.mkdir()
    old = "test_review_" + "form" + "_d.py"
    (tests / old).write_text("pass\n", encoding="utf-8")
    assert retired_name_hits(tmp_path) == [f"tests/{old}"]
    # Control: the same clean contents under the new name are not a hit.
    (tests / old).rename(tests / "test_review_field_tree.py")
    assert retired_name_hits(tmp_path) == []


def test_the_check_sees_the_name_where_it_is_still_allowed() -> None:
    """Control: the pattern matches the pinned schema's comment, so an empty result above is a real one."""
    (pinned,) = PINNED
    assert RETIRED.search((GR2 / pinned).read_text(encoding="utf-8"))
