#!/usr/bin/env python3
"""Run gr2's text guards -- the tests that hold the words gr2 prints and ships.

The guards are the vocabulary and help-surface tests: they read the README, the
docs and the rendered help, and refuse retired words. They run here the way CI
runs them, with ``GITHUB_ACTIONS=true`` in the child environment, because rich
renders help differently on a CI terminal and a guard that only passes locally
holds nothing.

The exit is honest about what happened. 0 only when pytest ran the guard files
and every test passed. Anything else is non-zero with one line naming which:
the guards failed, nothing was collected, pytest is missing from this
interpreter, or the files could not be imported. A run that collected nothing
is not a pass.

Usage:
    python scripts/text_guards.py --check            # run every guard file
    python scripts/text_guards.py --check PATH ...   # also name the guard files PATH touches
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent

NAMED_GUARDS = (
    "tests/test_review_vocabulary.py",
    "tests/test_cli_help_surface.py",
    "tests/test_vocabulary.py",
)
HELP_GLOB = "tests/test_*help*.py"

# pytest's own exit codes, by name, so the one-line reason is not a guess.
PYTEST_FAILED = 1
PYTEST_INTERRUPTED = 2
PYTEST_USAGE = 4
PYTEST_NO_TESTS = 5


def guard_files() -> list[str]:
    files = list(NAMED_GUARDS)
    for path in sorted(ROOT.glob(HELP_GLOB)):
        rel = path.relative_to(ROOT).as_posix()
        if rel not in files:
            files.append(rel)
    return files


def touched(paths: list[str], files: list[str]) -> list[str]:
    """The guard files among ``paths``, which may be repo- or gr2-relative."""
    names = set()
    for p in paths:
        p = p.removeprefix("gr2/")
        if p in files:
            names.add(p)
    return [f for f in files if f in names]


def child_env() -> dict[str, str]:
    env = dict(os.environ)
    env["GITHUB_ACTIONS"] = "true"
    return env


def reason(rc: int) -> str:
    if rc == PYTEST_FAILED:
        return "text guards failed"
    if rc == PYTEST_INTERRUPTED:
        return "text guards could not be collected (import or collection error)"
    if rc == PYTEST_USAGE:
        return "pytest usage error"
    if rc == PYTEST_NO_TESTS:
        return "no tests collected from the text guard files"
    return f"pytest exited {rc}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="run the text guards; exit non-zero unless every one passed",
    )
    parser.add_argument("paths", nargs="*", help="paths the range touched")
    args = parser.parse_args(argv)
    if not args.check:
        parser.error("only --check is supported")

    files = guard_files()
    missing = [f for f in files if not (ROOT / f).is_file()]
    if missing:
        print(f"text guards: guard file missing: {', '.join(missing)}", flush=True)
        return 1
    if importlib.util.find_spec("pytest") is None:
        print(f"text guards: pytest is not importable by {sys.executable}", flush=True)
        return 1

    if args.paths:
        hit = touched(args.paths, files)
        print(f"text guards: range touched {', '.join(hit) if hit else 'no guard file'}", flush=True)
    print(f"text guards: running {' '.join(files)}", flush=True)

    cmd = [
        sys.executable, "-m", "pytest", "-q", "-rs",
        "-p", "no:cacheprovider", "-o", "addopts=", *files,
    ]
    proc = subprocess.run(cmd, cwd=ROOT, env=child_env(), stdin=subprocess.DEVNULL)
    if proc.returncode != 0:
        print(f"text guards: {reason(proc.returncode)} (pytest rc {proc.returncode})", flush=True)
        return 1
    print("text guards: passed", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
