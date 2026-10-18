"""One canonical key per kind. A value of a kind is compared, looked up and stored as a key only after it passes
through that kind's function here, so two spellings of the same thing can never be two keys."""
from __future__ import annotations

from pathlib import PurePosixPath


def member_path_key(path: object) -> str:
    """A member path as git spells its gitlink: `./alpha`, `alpha/` and `alpha//` are all `alpha`. It does not fold
    case or resolve `..`; git does neither, so `Alpha` and `x/../alpha` stay distinct keys."""
    return str(PurePosixPath(str(path)))
