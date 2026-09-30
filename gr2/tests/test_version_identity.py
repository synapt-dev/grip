"""`gr2 --version` must name the CODE that runs, not when the venv was built.

The measured basis: three desks at the SAME code printed `2.0.0a1`, `2.0.0a2` and
`2.0.0a5`, because the callback read `importlib.metadata`'s install-time metadata and
nothing else. A stale build could not be told from a current one, and one desk's stale
build printed the usage block instead of a number at all.

These rows are FIXTURE-ONLY and exercise the real mechanism rather than a monkeypatched
one: a real dist-info directory is placed on `sys.path` so the PEP 610 `direct_url.json`
read is the production read, and a real temporary git repo supplies the commit so the
subprocess call is exercised. A stub injected past either would prove the parse and not
the call.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from gr2.python_cli import app as gr2_app
from gr2.python_cli.version import version_line

# A name no real installation uses, so the fixture can never be shadowed by -- or shadow
# -- the interpreter's own gitgrip. The mechanism under test is the same either way.
# No hyphen in the name on purpose: `importlib.metadata` parses a `.dist-info` directory
# by splitting on the LAST hyphen, so a hyphenated fixture name is not found by
# `version()` even though it appears in `distributions()` -- measured, not assumed.
DIST = "gitgripversionfixture"


def _one_line(text: str) -> str:
    assert text.endswith("\n") or "\n" not in text, "must be exactly one line"
    assert text.count("\n") <= 1, f"more than one line: {text!r}"
    assert text.strip() != "", "must not be empty"
    return text.strip()


def _git(args: list[str], cwd: Path) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


def _temp_repo(path: Path) -> str:
    """A real git repo with one commit; returns its short HEAD sha.

    Identity is pinned on the command line rather than read from the host's config, so the
    fixture does not depend on who is running the suite.
    """
    path.mkdir(parents=True, exist_ok=True)
    ident = ["-c", "user.email=fixture@example.com", "-c", "user.name=Fixture"]
    _git(["-c", "init.defaultBranch=dev", "init", "-q"], path)
    (path / "seed.txt").write_text("seed\n")
    _git([*ident, "add", "-A"], path)
    _git([*ident, "commit", "-q", "-m", "seed"], path)
    return _git(["rev-parse", "--short", "HEAD"], path)


def _dist_info(
    root: Path,
    *,
    version: str,
    editable: Path | None = None,
    install_dir: Path | None = None,
) -> None:
    """Write a real dist-info that `importlib.metadata` will find.

    ``editable`` writes the PEP 610 shape an editable install leaves behind; ``install_dir``
    writes one with ``editable: false``, which is a plain local-directory install. Neither
    means no ``direct_url.json`` at all, which is what a released wheel looks like.
    """
    di = root / f"{DIST}-{version}.dist-info"
    di.mkdir(parents=True, exist_ok=True)
    (di / "METADATA").write_text(
        f"Metadata-Version: 2.1\nName: {DIST}\nVersion: {version}\n"
    )
    if editable is not None:
        (di / "direct_url.json").write_text(
            json.dumps({"url": editable.as_uri(), "dir_info": {"editable": True}})
        )
    elif install_dir is not None:
        (di / "direct_url.json").write_text(
            json.dumps({"url": install_dir.as_uri(), "dir_info": {"editable": False}})
        )


@pytest.fixture
def fixture_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    holder = tmp_path / "site"
    holder.mkdir()
    monkeypatch.syspath_prepend(str(holder))
    return holder


def test_editable_install_names_the_clone_commit(fixture_path: Path, tmp_path: Path) -> None:
    """The property the whole story is for: the line names the code that runs."""
    clone = tmp_path / "clone"
    sha = _temp_repo(clone)
    _dist_info(fixture_path, version="2.0.0a5", editable=clone)

    assert _one_line(version_line(DIST)) == f"2.0.0a5+g{sha}"


def test_released_install_says_released(fixture_path: Path) -> None:
    """A wheel install has no checkout to name, and says so rather than guessing."""
    _dist_info(fixture_path, version="2.0.0a5")

    assert _one_line(version_line(DIST)) == "2.0.0a5 released"


def test_local_non_editable_install_is_not_called_editable(
    fixture_path: Path, tmp_path: Path
) -> None:
    """`direct_url.json` alone does not mean editable: the flag does.

    A reader who checked only for the file's presence would print a commit for an install
    that has none to print.
    """
    src = tmp_path / "src"
    src.mkdir()
    _dist_info(fixture_path, version="2.0.0a5", install_dir=src)

    assert _one_line(version_line(DIST)) == "2.0.0a5 released"


def test_no_git_on_the_path_does_not_crash_and_still_names_the_version(
    fixture_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The hard requirement: a missing tool must not become a traceback.

    `--version` is the first thing a stranger runs; a crash there is the worst place for
    one, and the version number is knowable without git anyway.
    """
    clone = tmp_path / "clone"
    _temp_repo(clone)
    _dist_info(fixture_path, version="2.0.0a5", editable=clone)
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))

    line = _one_line(version_line(DIST))

    assert line.startswith("2.0.0a5"), line
    assert "+g" not in line or "unknown" in line, (
        "without git the commit cannot be named, and it must not be invented: " + line
    )


def test_editable_pointing_at_something_that_is_not_a_repo_does_not_crash(
    fixture_path: Path, tmp_path: Path
) -> None:
    """Present git, absent repo. The other half of 'never crashes'."""
    not_a_repo = tmp_path / "not-a-repo"
    not_a_repo.mkdir()
    _dist_info(fixture_path, version="2.0.0a5", editable=not_a_repo)

    line = _one_line(version_line(DIST))

    assert line.startswith("2.0.0a5"), line


def test_an_unknown_distribution_does_not_crash() -> None:
    """A name that is installed nowhere still has to answer, not raise.

    `PackageNotFoundError` is a `ModuleNotFoundError`; letting it out of the callback is
    the same traceback-instead-of-a-number failure this range removes.
    """
    assert _one_line(version_line("gitgrip-no-such-distribution-xyz"))


def test_the_cli_prints_exactly_one_line() -> None:
    """The wiring, on the real installation this suite runs against.

    The shape, not a fixed value: this row must not break when the installed number
    changes, and it must fail if the usage block comes back in place of a number.
    """
    res = CliRunner().invoke(gr2_app.app, ["--version"])

    assert res.exit_code == 0, res.output
    line = _one_line(res.output)
    assert line[0].isdigit(), f"a number, not a usage block: {line!r}"
    assert ("+g" in line) or line.endswith(" released"), line
