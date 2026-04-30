"""Root conftest: make python_cli importable as gr2.python_cli."""

from __future__ import annotations

import sys
import types
from pathlib import Path

_project_root = str(Path(__file__).parent)

if "gr2" not in sys.modules:
    _gr2 = types.ModuleType("gr2")
    _gr2.__path__ = [_project_root]
    sys.modules["gr2"] = _gr2
