"""A review record stored as a Git tree with one entry per protobuf field is a "field tree".

The earlier working name is retired everywhere in gr2: module, functions, errors, tests and prose.
The one exception is the pinned schema file, whose bytes gr2 refuses to change (its comment
belongs to the schema's owner and changes there).
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

GR2 = Path(__file__).resolve().parents[1]
PINNED = {"gr2/schemas/dev.synapt.grip.review.v1alpha1/review.proto"}
# Spelled in parts, so this file does not match itself.
RETIRED = re.compile("form" + r"[ _-]?" + "d" + r"(?![a-z])", re.IGNORECASE)


def test_the_retired_name_appears_nowhere_in_gr2_but_the_pinned_schema() -> None:
    tracked = subprocess.run(["git", "-C", str(GR2), "ls-files"], capture_output=True, text=True, check=True)
    hits = []
    for rel in tracked.stdout.split():
        if rel in PINNED:
            continue
        try:
            text = (GR2 / rel).read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        hits += [f"{rel}:{n}" for n, line in enumerate(text.splitlines(), 1) if RETIRED.search(line)]
    assert not hits, f"the retired name is back: {hits}"


def test_the_check_sees_the_name_where_it_is_still_allowed() -> None:
    """Control: the pattern matches the pinned schema's comment, so an empty result above is a real one."""
    (pinned,) = PINNED
    assert RETIRED.search((GR2 / pinned).read_text(encoding="utf-8"))
