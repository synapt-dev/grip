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
import re
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


def _temp_repo(path: Path, *, marker: str = "seed") -> str:
    """A real git repo with one commit; returns its short HEAD sha.

    Identity is pinned on the command line rather than read from the host's config, so the
    fixture does not depend on who is running the suite.
    """
    path.mkdir(parents=True, exist_ok=True)
    ident = ["-c", "user.email=fixture@example.com", "-c", "user.name=Fixture"]
    _git(["-c", "init.defaultBranch=dev", "init", "-q"], path)
    # The marker is a parameter so two fixture repos in one test get DIFFERENT commits:
    # they would otherwise share a sha whenever they committed within the same second, and
    # a test whose two subjects are identical cannot witness which one answered.
    (path / "seed.txt").write_text(f"{marker}\n")
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


def test_an_absent_distribution_returns_the_declared_bare_unknown() -> None:
    """The FOURTH form, pinned by VALUE and not only by "does not crash".

    Two claims in one row, and the second is the one that was missing: a name installed
    nowhere must answer rather than raise, because `PackageNotFoundError` is a
    `ModuleNotFoundError` and letting it out of the callback is the same
    traceback-instead-of-a-number failure this range removes -- AND the answer must be the
    declared bare `unknown`, because a version-prefixed form here would mean a version had
    been invented for a distribution that is not installed.
    """
    assert version_line("gitgrip-not-installed-for-this-interpreter") == "unknown"


def test_the_cli_prints_one_line_of_a_declared_shape() -> None:
    """The shape, on whatever installation this suite runs against.

    FOUR declared forms, not three: `<version>+g<sha>`, `<version> released`,
    `<version>+unknown`, and a **bare `unknown`** when the distribution is not installed
    for this interpreter at all -- there is no version to prefix, so none is invented.

    THE ASSERTION CARRIES THE MESSAGE, and that is the half that was wrong before. This
    row used to assert `line[0].isdigit()` under "a number, not a usage block", which is
    what a bare `unknown` LOOKS like and is not what it is: a reader (and the reviewer who
    found it) is sent looking at the usage-block path, which was not the cause. The row now
    names the shape it got and the four it accepts, so a failure diagnoses itself.
    """
    res = CliRunner().invoke(gr2_app.app, ["--version"])

    assert res.exit_code == 0, res.output
    line = _one_line(res.output)
    assert re.fullmatch(r"\S+(?:\+g[0-9a-f]+|\+unknown| released)|unknown", line), (
        "not one of the four declared forms "
        "(<version>+g<sha> | <version> released | <version>+unknown | unknown): " + repr(line)
    )


def test_the_cli_still_answers_when_the_distribution_is_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The environment the block was found in, reproduced as a row.

    A reviewer's interpreter had `gitgrip` absent while another synapt distribution was
    installed, so the CLI ran and the lookup worked -- the distribution was simply not
    there. That is a legitimate environment and the verb must still answer one of its four
    declared forms. It used to answer `unknown` while the docstring declared three
    version-prefixed forms, and the row above failed on a message that pointed at the
    usage-block path instead.
    """
    import importlib.metadata

    # Patch the LIBRARY seam, not this module's own collaborator: the code resolves a
    # distribution object and reads both facts from it, so "nothing is installed under that
    # name" is `distributions()` yielding nothing -- which is what an absent distribution
    # actually looks like. The previous version of this row patched `version()`, which was
    # the old seam and stopped simulating anything the moment the resolution changed: it
    # passed for the wrong reason, which is how it went unnoticed.
    monkeypatch.setattr(importlib.metadata, "distributions", lambda: iter(()))

    res = CliRunner().invoke(gr2_app.app, ["--version"])

    assert res.exit_code == 0, res.output
    assert _one_line(res.output) == "unknown"

def test_the_cli_agrees_with_the_library() -> None:
    """The wiring, asserted as AGREEMENT rather than as a shape.

    Both sides run in THIS process, so this row does NOT catch the bug that motivated it
    and I am not going to let it read as though it does. That bug was a path-dependent
    answer -- the console script saw the installed dist-info and named a commit, the
    in-process runner saw a legacy egg-info first and said `released` -- and an in-process
    comparison cannot see a difference that lives in `sys.path`. The egg-info row below
    reproduces the arrangement and is the one that catches it.

    What this row does pin: the callback must go through the same function the library
    exports, so rewiring it to read metadata directly reddens here.
    """
    res = CliRunner().invoke(gr2_app.app, ["--version"])

    assert res.exit_code == 0, res.output
    assert _one_line(res.output) == version_line()

def test_a_distribution_with_no_direct_url_does_not_hide_the_editable_one(
    fixture_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A legacy `.egg-info` beside a real `.dist-info` must not decide the answer.

    This is the bug the first version of this module shipped. `importlib.metadata`
    resolves a name to whichever distribution comes FIRST on `sys.path`, and a source tree
    carrying `gr2/<name>.egg-info` puts that one first whenever the source directory is on
    the path -- so the install read as released from inside the package's own directory and
    as editable from everywhere else. Same code, two answers, decided by the caller's
    path: the wrongness this module exists to remove, reproduced inside it.
    """
    clone = tmp_path / "clone"
    sha = _temp_repo(clone)
    _dist_info(fixture_path, version="2.0.0a5", editable=clone)

    legacy = tmp_path / "legacy"
    egg = legacy / f"{DIST}.egg-info"
    egg.mkdir(parents=True)
    # A DIFFERENT number from the dist-info's, and that is the point: a fixture that gives
    # both installs the same version holds the version constant and cannot witness a
    # path-decided VERSION, only a path-decided direct_url. This row carried 2.0.0a5 in
    # both files and passed while the version was still decided by path order.
    (egg / "PKG-INFO").write_text(
        f"Metadata-Version: 1.0\nName: {DIST}\nVersion: 2.0.0a1\n"
    )
    # Prepend last, so the egg-info sits FIRST on the path -- the arrangement that made the
    # wrong answer win.
    monkeypatch.syspath_prepend(str(legacy))

    # BOTH fields, together: the commit from the editable install AND the number from the
    # same one. The egg-info's 2.0.0a1 must not appear at all.
    assert _one_line(version_line(DIST)) == f"2.0.0a5+g{sha}"


def test_the_two_matching_distributions_are_both_seen(fixture_path: Path, tmp_path: Path) -> None:
    """The control for the row above: the fixture really does present two of them.

    Without this, `test_a_distribution_with_no_direct_url_...` would also pass if the
    egg-info had simply not been found, which is a different situation entirely.
    """
    import importlib.metadata

    clone = tmp_path / "clone"
    _temp_repo(clone)
    _dist_info(fixture_path, version="2.0.0a5", editable=clone)
    legacy = tmp_path / "legacy"
    egg = legacy / f"{DIST}.egg-info"
    egg.mkdir(parents=True)
    (egg / "PKG-INFO").write_text(f"Metadata-Version: 1.0\nName: {DIST}\nVersion: 2.0.0a5\n")

    import sys

    sys.path.insert(0, str(legacy))
    try:
        names = [
            (d.metadata["Name"], d.read_text("direct_url.json"))
            for d in importlib.metadata.distributions()
            if d.metadata and (d.metadata["Name"] or "").lower() == DIST
        ]
    finally:
        sys.path.remove(str(legacy))

    assert len(names) >= 2, f"expected both fixtures present, saw {names}"
    assert any(raw is None for _n, raw in names), "the egg-info must carry no direct_url"
    assert any(raw for _n, raw in names), "the dist-info must carry one"

def _resolve_with_path_order(paths: list[Path], monkeypatch: pytest.MonkeyPatch) -> str:
    """Resolve with ``paths[0]`` FIRST on sys.path, for the duration of one call."""
    import sys

    added = [str(p) for p in paths]
    for entry in reversed(added):
        sys.path.insert(0, entry)
    try:
        return version_line(DIST)
    finally:
        for entry in added:
            sys.path.remove(entry)


def test_two_installs_of_one_name_resolve_the_same_way_in_both_path_orders(
    fixture_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """THE ROW FOR THE FIELD THAT WAS STILL PATH-DECIDED, asserted in both orders.

    An editable install at 2.0.0a2 and a plain one at 2.0.0a5, under one name. Reading the
    version from `importlib.metadata.version()` (first match) while the commit came from a
    scan of all matches spliced a STALE NUMBER onto a CURRENT COMMIT: with only path order
    changed, the line read `2.0.0a2+g<sha>` in one order and `2.0.0a5+g<sha>` in the other,
    each of them coherent-looking and only one of them true.

    The mutation that proves this row is the variable: restore the independent first-match
    version read and the row reddens **in one order only** -- which is what makes the order
    the thing being measured rather than the value.
    """
    clone = tmp_path / "clone"
    sha = _temp_repo(clone)
    editable_dir = tmp_path / "editable-entry"
    plain_dir = tmp_path / "plain-entry"
    editable_dir.mkdir()
    plain_dir.mkdir()
    _dist_info(editable_dir, version="2.0.0a2", editable=clone)
    _dist_info(plain_dir, version="2.0.0a5")

    editable_first = _resolve_with_path_order([editable_dir, plain_dir], monkeypatch)
    plain_first = _resolve_with_path_order([plain_dir, editable_dir], monkeypatch)

    assert editable_first == plain_first, (editable_first, plain_first)
    assert editable_first == f"2.0.0a2+g{sha}", editable_first


def test_two_non_editable_installs_that_disagree_report_unknown(
    fixture_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no editable match, a disagreement is STATED rather than decided by path order.

    Two installed distributions under one name, neither editable, different versions: there
    is no single version to state, and "whichever sys.path reached first" is the module's
    own declared wrongness. Same answer in BOTH orders.
    """
    one = tmp_path / "one"
    two = tmp_path / "two"
    one.mkdir()
    two.mkdir()
    _dist_info(one, version="2.0.0a2")
    _dist_info(two, version="2.0.0a5")

    first = _resolve_with_path_order([one, two], monkeypatch)
    second = _resolve_with_path_order([two, one], monkeypatch)

    assert first == second == "unknown", (first, second)

def test_two_editable_installs_that_disagree_report_unknown(
    fixture_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MORE THAN ONE EDITABLE IS ALSO AN AMBIGUITY, and this is the third seam.

    Preferring "the editable one" is not enough when there are two of them: returning the
    first puts the caller's `sys.path` back in charge of BOTH facts, which is how one
    process printed `2.0.0a2+g<sha>` and `2.0.0a6+g<sha>` with only path order changed.
    Two editable installs under one name are two claims about which checkout is running;
    if they disagree there is no single answer, and the declared bare `unknown` is the
    honest one. Same answer in BOTH orders.
    """
    clone_one = tmp_path / "clone-one"
    clone_two = tmp_path / "clone-two"
    _temp_repo(clone_one, marker="one")
    _temp_repo(clone_two, marker="two")
    first_dir = tmp_path / "first-entry"
    second_dir = tmp_path / "second-entry"
    first_dir.mkdir()
    second_dir.mkdir()
    _dist_info(first_dir, version="2.0.0a2", editable=clone_one)
    _dist_info(second_dir, version="2.0.0a6", editable=clone_two)

    one_first = _resolve_with_path_order([first_dir, second_dir], monkeypatch)
    two_first = _resolve_with_path_order([second_dir, first_dir], monkeypatch)

    assert one_first == two_first == "unknown", (one_first, two_first)


def test_two_editable_installs_that_agree_answer_with_their_shared_value(
    fixture_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The control for the row above: agreement is not an ambiguity.

    Without this, `..._that_disagree_report_unknown` would also pass if ANY two matches
    produced `unknown`, which would be a different and much worse rule.
    """
    clone = tmp_path / "shared-clone"
    sha = _temp_repo(clone)
    first_dir = tmp_path / "agree-one"
    second_dir = tmp_path / "agree-two"
    first_dir.mkdir()
    second_dir.mkdir()
    _dist_info(first_dir, version="2.0.0a5", editable=clone)
    _dist_info(second_dir, version="2.0.0a5", editable=clone)

    one_first = _resolve_with_path_order([first_dir, second_dir], monkeypatch)
    two_first = _resolve_with_path_order([second_dir, first_dir], monkeypatch)

    assert one_first == two_first == f"2.0.0a5+g{sha}", (one_first, two_first)


def test_a_windows_file_url_keeps_its_drive_letter() -> None:
    """`Path(urlparse(url).path)` DROPS the drive, and the consequence is a wrong answer.

    On `file:///C:/repo` the path component is `/C:/repo`, whose Windows drive is empty --
    so an editable install on Windows would be tested against a path that cannot exist and
    would read as `released`. Asserted on the PARSE, which is the measured half; whether
    `is_dir()` then succeeds is a consequence of it and is not witnessed here, because this
    suite runs where a Windows path cannot exist. gr2's CI is ubuntu-only, so nothing else
    would catch it either.
    """
    from gr2.python_cli.version import _file_url_to_path

    windows = _file_url_to_path("file:///C:/Users/someone/repo")
    assert windows.drive == "C:", repr(str(windows))
    assert str(windows).replace("\\", "/") == "C:/Users/someone/repo", repr(str(windows))

    # CONTROL: a POSIX file URL is decoded unchanged, so the fix cannot be passing by
    # mangling every path into a Windows one.
    posix = _file_url_to_path("file:///Users/someone/repo")
    assert str(posix) == "/Users/someone/repo", repr(str(posix))
    assert not getattr(posix, "drive", "") or posix.drive == "/", repr(str(posix))

def test_a_windows_shaped_editable_record_answers_unknown_and_does_not_raise(
    fixture_path: Path,
) -> None:
    """THE FOURTH SEAM: the branch I added to make Windows testable CRASHED where it ran.

    `_file_url_to_path` returns a `PureWindowsPath` for a Windows-shaped record, and
    `PurePath` has no `is_dir()` AT ALL -- so the call raised `AttributeError` through
    `_editable_path` and through `version_line` itself, on a host where the fallback branch
    fires. It does not crash on real Windows, where `url2pathname` converts first and the
    regex never matches, which is the inverse of the usual shape: the branch that exists to
    be testable off Windows was the only branch that could crash.

    It also broke this module's own promise that no failure path raises. The row above
    called `_file_url_to_path` and checked its `.drive` -- never `is_dir()` -- so it passed
    over the exception, and nothing static would have caught it: there is no mypy or
    pyright anywhere in gr2.

    The correct outcome is one the module already declares: the record SAYS editable, so
    this is not "not editable", it is a checkout whose commit cannot be read here.
    """
    import json as _json

    di = fixture_path / f"{DIST}-2.0.0a5.dist-info"
    di.mkdir(parents=True)
    (di / "METADATA").write_text(f"Metadata-Version: 2.1\nName: {DIST}\nVersion: 2.0.0a5\n")
    (di / "direct_url.json").write_text(
        _json.dumps({"url": "file:///C:/Users/someone/repo", "dir_info": {"editable": True}})
    )

    assert version_line(DIST) == "2.0.0a5+unknown"


def test_the_same_record_shape_pointing_at_a_real_checkout_names_its_commit(
    fixture_path: Path, tmp_path: Path
) -> None:
    """The control: a POSIX-shaped editable record still resolves to a commit.

    Without it, the row above would pass if EVERY editable record answered `+unknown`,
    which would be a different and much worse rule.
    """
    clone = tmp_path / "clone"
    sha = _temp_repo(clone)
    _dist_info(fixture_path, version="2.0.0a5", editable=clone)

    assert version_line(DIST) == f"2.0.0a5+g{sha}"


@pytest.mark.parametrize("dir_info", ["boom", 5, [1], None])
def test_a_direct_url_whose_dir_info_is_not_an_object_answers_and_does_not_raise(
    fixture_path: Path, dir_info: object
) -> None:
    """THE FIFTH SEAM: the guard above it reads as a null check and is only a falsy check.

    `if not (doc.get("dir_info") or {}).get("editable")` fires `or {}` on a FALSY value, so a
    truthy NON-dict passes straight through and `.get` raises `AttributeError` out of
    `--version`. Measured, one fresh process per case: `"boom"`, `5` and `[1]` each raised
    `'str'/'int'/'list' object has no attribute 'get'`, while `None` was fine because it is
    falsy -- which is why the bug survives a reading of that line.

    The row is parametrized over all four so the falsy control runs beside the three that
    reddened: a fix that special-cased one type would pass a single-case row.
    """
    import json as _json

    di = fixture_path / f"{DIST}-2.0.0a5.dist-info"
    di.mkdir(parents=True)
    (di / "METADATA").write_text(f"Metadata-Version: 2.1\nName: {DIST}\nVersion: 2.0.0a5\n")
    (di / "direct_url.json").write_text(
        _json.dumps({"url": "file:///tmp/somewhere", "dir_info": dir_info})
    )

    # A record we could not read is not a checkout we can name, and it is not evidence of a
    # file layout we can test either -- so the answer is the declared `released` form and
    # never a raise. Asserted EXACTLY rather than as membership in the four declared forms:
    # a membership set would be satisfied by `released` and by `+unknown` alike, which is
    # the shape of assertion that let an earlier row in this file pass on either answer.
    assert version_line(DIST) == "2.0.0a5 released"


def test_the_same_non_object_dir_info_with_editable_absent_is_also_readable(
    fixture_path: Path,
) -> None:
    """CONTROL: the shape with NO `dir_info` key at all was never the failing case.

    The row above would pass if every non-editable record answered `released` for an
    unrelated reason, so this pins the ordinary VCS-install shape the guard also covers.
    """
    import json as _json

    di = fixture_path / f"{DIST}-2.0.0a5.dist-info"
    di.mkdir(parents=True)
    (di / "METADATA").write_text(f"Metadata-Version: 2.1\nName: {DIST}\nVersion: 2.0.0a5\n")
    (di / "direct_url.json").write_text(
        _json.dumps({"url": "https://example.invalid/repo.git", "vcs_info": {"vcs": "git"}})
    )

    assert version_line(DIST) == "2.0.0a5 released"


def test_a_non_utf8_record_answers_and_does_not_raise(fixture_path: Path) -> None:
    """THE SIXTH SEAM, shape one: the module's one unguarded READ.

    `read_text` on a `PathDistribution` suppresses exactly five exceptions, read from the
    stdlib source: FileNotFoundError, IsADirectoryError, KeyError, NotADirectoryError and
    PermissionError. A record holding non-UTF-8 BYTES is not one of them -- it raises
    `UnicodeDecodeError`, which is a `ValueError` and NOT an `OSError`, so a catch written
    for `OSError` alone still misses it. Measured: it escaped through `_editable_path` and
    through `version_line`, which is the promise this module makes three times.
    """
    di = fixture_path / f"{DIST}-2.0.0a5.dist-info"
    di.mkdir(parents=True)
    (di / "METADATA").write_text(f"Metadata-Version: 2.1\nName: {DIST}\nVersion: 2.0.0a5\n")
    (di / "direct_url.json").write_bytes(
        b'{"url": "file:///tmp/somewhere", "dir_info": {"editable": \xff\xfe}}'
    )

    assert version_line(DIST) == "2.0.0a5 released"


def test_a_symlink_loop_in_the_record_answers_and_does_not_raise(fixture_path: Path) -> None:
    """THE SIXTH SEAM, shape two: the read fails with an `OSError`, but not a suppressed one.

    A `direct_url.json` that is a symlink to itself raises `OSError` ELOOP, which is not in
    `read_text`'s five. It is a different failing CLASS from the row above -- one is a
    decode failure, one is an I/O failure -- which is why the fix catches both and why one
    witness row could not stand for the other.
    """
    di = fixture_path / f"{DIST}-2.0.0a5.dist-info"
    di.mkdir(parents=True)
    (di / "METADATA").write_text(f"Metadata-Version: 2.1\nName: {DIST}\nVersion: 2.0.0a5\n")
    loop = di / "loop"
    os.symlink(str(loop), str(loop))
    os.symlink(str(loop), str(di / "direct_url.json"))

    assert version_line(DIST) == "2.0.0a5 released"


def test_a_record_under_a_non_traversable_directory_answers_and_does_not_raise(
    fixture_path: Path, tmp_path: Path
) -> None:
    """ATLAS'S EIGHTH SEAM: the stat was the last unguarded probe in this module.

    `pathlib._IGNORED_ERRNOS` is only `(2, 20, 9, 62)` -- ENOENT, ENOTDIR, EBADF, ELOOP --
    so `is_dir()` swallows an ORDINARY MISSING PATH and re-raises everything else. That is
    why the probe read as safe: the shape anyone checks by hand is the one it suppresses. A
    recorded checkout under a directory the process cannot traverse raises `PermissionError`
    out of `--version`, measured.

    The answer is `<version>+unknown` rather than `released`, which is the design decision
    this version took: the record SAYS editable, so asserting there is no checkout is false.
    """
    parent = tmp_path / "closed"
    clone = parent / "clone"
    clone.mkdir(parents=True)
    _temp_repo(clone)
    parent.chmod(0o000)
    try:
        _dist_info(fixture_path, version="2.0.0a5", editable=clone)
        assert version_line(DIST) == "2.0.0a5+unknown"
    finally:
        parent.chmod(0o755)


def test_a_record_with_an_over_long_component_answers_and_does_not_raise(
    fixture_path: Path, tmp_path: Path
) -> None:
    """The other errno, and one row cannot stand for the other.

    A component past `NAME_MAX` raises `OSError` ENAMETOOLONG -- which is 63, and NOT in
    `is_dir()`'s ignored set, so it re-raised. A different failing class from the
    permission row above, reached by a different syscall outcome.
    """
    unreachable = tmp_path / ("x" * 300)
    _dist_info(fixture_path, version="2.0.0a5", editable=unreachable)

    assert version_line(DIST) == "2.0.0a5+unknown"


def test_a_recorded_path_that_is_gone_answers_unknown_rather_than_released(
    fixture_path: Path, tmp_path: Path
) -> None:
    """THE SEMANTIC PIN, and the one that makes the choice visible.

    `released` asserts there is no editable checkout. The record here says there IS one and
    the checkout has since been deleted, so neither `released` (which the old probe
    answered, because `is_dir()` returns False for a missing path) nor a crash is true.
    `+unknown` is the declared form that means exactly this, and a change back to a probe
    that reports `released` has to move this row to do it.
    """
    _dist_info(fixture_path, version="2.0.0a5", editable=tmp_path / "never-created")

    assert version_line(DIST) == "2.0.0a5+unknown"


def test_a_file_where_a_checkout_was_recorded_answers_unknown(
    fixture_path: Path, tmp_path: Path
) -> None:
    """The second half of the same pin: a FILE at the recorded path is still a record that
    says editable, so it is still not `released`."""
    target = tmp_path / "not-a-directory"
    target.write_text("this is a file, not a checkout\n")
    _dist_info(fixture_path, version="2.0.0a5", editable=target)

    assert version_line(DIST) == "2.0.0a5+unknown"


def test_a_corrupt_metadata_for_another_name_does_not_break_this_lookup(
    fixture_path: Path, tmp_path: Path
) -> None:
    """ATLAS'S SEVENTH-SEAM CONTROL BECAME THE WITNESS, and it is the wider half.

    `_matching_distributions` reads `dist.metadata` for EVERY distribution
    `importlib.metadata.distributions()` yields, BEFORE it filters by name -- so ONE
    unreadable METADATA anywhere on `sys.path` breaks the lookup for EVERY name, not only
    its own. That was found because the CONTROL for a narrower claim was a different,
    perfectly readable name, and it raised too.

    The control for THIS row is the suite's own editable row: the same lookup, with the same
    fixture, succeeds when the corrupt dist-info is not there.
    """
    corrupt = fixture_path / "othername-1.0.dist-info"
    corrupt.mkdir()
    (corrupt / "METADATA").write_bytes(b"Name: othername\nVersion: 1.0\n\xff\xfe\n")

    clone = tmp_path / "clone"
    sha = _temp_repo(clone)
    _dist_info(fixture_path, version="2.0.0a5", editable=clone)

    assert version_line(DIST) == f"2.0.0a5+g{sha}"


def test_a_corrupt_metadata_for_this_name_answers_the_bare_unknown(fixture_path: Path) -> None:
    """The other side of the skip: when the unreadable distribution is the ONLY match, the
    caller has nothing to read and falls through to the declared bare `unknown` -- not a
    crash, and not an invented version."""
    corrupt = fixture_path / f"{DIST}-2.0.0a5.dist-info"
    corrupt.mkdir()
    (corrupt / "METADATA").write_bytes(b"Metadata-Version: 2.1\nName: gitgrip\n\xff\xfe\n")

    assert version_line(DIST) == "unknown"
