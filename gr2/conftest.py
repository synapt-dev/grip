"""Root conftest: make python_cli importable as gr2.python_cli."""

from __future__ import annotations

import sys
import types
from pathlib import Path

_project_root = Path(__file__).parent

if "gr2" not in sys.modules:
    # Namespace with two roots: the project dir (python_cli lives flat at
    # gr2/python_cli, imported as gr2.python_cli) and the real package dir
    # gr2/gr2 (the 1.5.0 import-package layout: gr2.overlay, gr2.schemas).
    _gr2 = types.ModuleType("gr2")
    _gr2.__path__ = [str(_project_root), str(_project_root / "gr2")]
    sys.modules["gr2"] = _gr2