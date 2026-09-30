"""Deprecated compatibility shim: ``gr2_overlay`` was renamed to ``gr2.overlay``.

Import ``gr2.overlay`` instead. Every submodule of the real package is aliased
through ``sys.modules`` here, so ``gr2_overlay.units`` IS ``gr2.overlay.units``
(the same module object, not a parallel copy) — a consumer that has not yet
migrated keeps working with no behavioural drift. Removed at the ``beta``
milestone; downstream consumers migrate to ``gr2.overlay`` before then.

The milestone is recorded in ``api/deprecations.toml`` and checked by
``tests/test_deprecation_registry.py``. It is not restated as a version here: this
file used to say "removed in the release after 1.5.0", which had already passed at
2.0.0a5 without anything going red.
"""
from __future__ import annotations

import importlib as _importlib
import pkgutil as _pkgutil
import sys as _sys
import warnings as _warnings

_warnings.warn(
    "gr2_overlay is deprecated and will be removed at the beta milestone; "
    "import gr2.overlay instead.",
    DeprecationWarning,
    stacklevel=2,
)

_real = _importlib.import_module("gr2.overlay")

# Alias every submodule so `import gr2_overlay.X` and `from gr2_overlay.X import Y`
# resolve to the SAME module object as gr2.overlay.X. Discovered dynamically from
# the real package so a new submodule is covered without editing this shim.
for _info in _pkgutil.iter_modules(_real.__path__):
    _mod = _importlib.import_module(f"gr2.overlay.{_info.name}")
    _sys.modules[f"{__name__}.{_info.name}"] = _mod
    globals()[_info.name] = _mod

# Re-export the real package's declared public names (`from gr2_overlay import X`).
for _attr in getattr(_real, "__all__", ()):
    globals()[_attr] = getattr(_real, _attr)

del _importlib, _pkgutil, _sys, _warnings, _info, _mod, _real
