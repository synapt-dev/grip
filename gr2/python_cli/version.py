"""What `gr2 --version` answers: which number was installed.

TODO: this answers install-time metadata only, so a checkout that is months stale reports
the number its virtualenv was built with rather than the commit it contains.

This file exists as the FIRST HALF of the change: the rows in
`gr2/tests/test_version_identity.py` are written against the behaviour this range
implements, and at this commit they are RED. The next commit makes them green.
"""

from __future__ import annotations

import importlib.metadata

DISTRIBUTION = "gitgrip"


def version_line(distribution: str = DISTRIBUTION) -> str:
    """The one line `gr2 --version` prints.

    Today: the installed distribution's version and nothing else.
    """
    try:
        return importlib.metadata.version(distribution)
    except importlib.metadata.PackageNotFoundError:
        return "unknown"
