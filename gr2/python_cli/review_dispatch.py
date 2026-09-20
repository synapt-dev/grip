"""The review verbs collapse
to bind / open / run / close / verify. ``open`` dispatches on its argument; ``close``
reads the lane's marker to tell a reconstruction lane from a PR lane. ``open-gr``,
``close-gr``, ``open-project``, ``exit-gr`` become hidden aliases for one release.

This module is the dispatch DECISION, split from the command bodies so it is testable
without a workspace or a remote (the working-first spike). The command wiring in app.py
calls these and forwards to the existing implementations, which stay as the aliases.
"""
from __future__ import annotations

import re
from pathlib import Path

# open-gr's teardown marker (kept in sync with open_gr_review._OPEN_GR_MARKER;
# imported below so the two never drift).
from .open_gr_review import _OPEN_GR_MARKER

_HEX_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")


def classify_open_target(target: str) -> str:
    """Which open the argument names:

    - ``"gr"``      a ``gr:<sha>`` bind id, or a bare 7-40 hex sha (open-gr reconstruction)
    - ``"pr"``      an all-digits PR number (PR-head review lane)
    - ``"project"`` anything else (a project-review id)

    Layne: "gr2 review open doesn't need the gr differentiation" -- the caller types
    ``review open <target>`` and the shape of *target* selects the path.
    """
    t = target.strip()
    if t.startswith("gr:"):
        return "gr"
    if t.isdigit():
        return "pr"
    if _HEX_RE.match(t):
        return "gr"
    return "project"


def classify_close_lane(lane_dir: Path) -> str:
    """Which teardown the lane needs, read from its marker (never from a flag):

    - ``"reconstruction"``  the lane carries open-gr's reconstruct marker -> close_open_gr_lane
    - ``"pr"``              no reconstruct marker -> the PR-head close_review_lane

    "close reads the lane's marker to tell a reconstruction lane from a PR lane."
    """
    if (lane_dir / _OPEN_GR_MARKER).is_file():
        return "reconstruction"
    return "pr"
