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


def test_the_retired_name_appears_nowhere_in_gr2_but_the_pinned_schema() -> None:
    # Walk the source trees, not the git index: the suite also runs from a git archive with no
    # .git. Build output beside them (virtualenvs, egg-info) is not gr2's text.
    hits = []
    sources = [GR2 / "gr2", GR2 / "tests", GR2 / "api", GR2 / "docs", GR2 / "scripts", *GR2.glob("*.md"), *GR2.glob("*.toml")]
    for path in sorted(f for root in sources for f in ([root] if root.is_file() else root.rglob("*"))):
        rel = path.relative_to(GR2).as_posix()
        if not path.is_file() or rel in PINNED or set(path.relative_to(GR2).parts) & SKIP:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        hits += [f"{rel}:{n}" for n, line in enumerate(text.splitlines(), 1) if RETIRED.search(line)]
    assert not hits, f"the retired name is back: {hits}"


def test_the_check_sees_the_name_where_it_is_still_allowed() -> None:
    """Control: the pattern matches the pinned schema's comment, so an empty result above is a real one."""
    (pinned,) = PINNED
    assert RETIRED.search((GR2 / pinned).read_text(encoding="utf-8"))
