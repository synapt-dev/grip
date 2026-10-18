"""Importing the CLI app must not pull in the slow optional modules.

jsonschema, importlib.metadata, urllib.request and yaml each cost startup time on
every verb, and only a few verbs need them, so they are imported where they are used.
A fresh interpreter proves it: a module already loaded by this test process would
hide a top-level import that came back.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from gr2.python_cli import app

_DEFERRED = ("jsonschema", "importlib.metadata", "urllib.request", "yaml")


def test_app_import_leaves_slow_modules_unloaded() -> None:
    root = str(Path(app.__file__).resolve().parents[2])
    env = dict(os.environ)
    env["PYTHONPATH"] = root + os.pathsep + env.get("PYTHONPATH", "")
    code = (
        "import json, sys\n"
        "import gr2.python_cli.app\n"
        f"print(json.dumps([m for m in {_DEFERRED!r} if m in sys.modules]))\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code], text=True, capture_output=True, env=env, check=False
    )
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout.strip().splitlines()[-1]) == []
