"""What `gr2 --version` answers: WHICH CODE is running, not which number was installed.

The distinction is the whole point of this module. `importlib.metadata.version()` answers
"what did the package claim at install time", and in a virtualenv built weeks ago that is a
different answer from "which bytes compute this command". So the same commit can print
more than one number, and a stale build prints the number it was built with rather than the
commit it contains -- which is exactly the state a reader cannot see and cannot detect.

ONE RESOLUTION, TWO FACTS, and that is a rule rather than an implementation detail. Two
facts are read -- the version, and whether the install is editable -- and they must come
from the SAME resolved distribution. Reading them separately lets the number come from one
install and the commit from another, which prints a stale number beside a current commit
and reads as one coherent installation. The resolution is deterministic and independent of
the caller's `sys.path`: an editable match wins wherever it sits; MORE THAN ONE editable
match must AGREE, and if they disagree the answer is the bare `unknown` rather than the
first one the path reached; and with no editable match the matches must agree on the
version.

The two facts, both read from that one distribution:

* whether the install is EDITABLE, from the PEP 610 `direct_url.json` beside the metadata;
* for an editable install, the commit at the checkout's `HEAD`.

A `file://` URL is decoded with `url2pathname`, not with `Path(urlparse(url).path)`: the
latter drops a Windows drive letter, so `file:///C:/repo` becomes `/C:/repo` -- a path that
cannot exist -- and an editable install on Windows would read as `released`.

A `released` form has no READABLE editable checkout to name: a wheel, or a record that is
absent, malformed, or shaped unlike a directory record. That sentence is deliberately wider
than "a released wheel", because the wider reading is what the code does -- several branches
below return None on a record they could not read, and a reader told only the wheel meaning
would take an unreadable record for a wheel.

FOUR FORMS, and the fourth is declared here because it was not. It is a **bare `unknown`**,
and it means NO SINGLE VERSION COULD BE DETERMINED, for either of two reasons: nothing is
installed under that name for this interpreter, or two installs under that name disagree
and neither is editable. It is not `<something> unknown` and no version is invented to keep
the shape regular. All three other forms carry a version, so a reader who has read only
those three has been told something false about what this can return.
"""

from __future__ import annotations

import importlib.metadata
import json
import re
import shutil
import subprocess
from pathlib import Path, PurePath, PureWindowsPath
from urllib.parse import urlparse
from urllib.request import url2pathname

DISTRIBUTION = "gitgrip"

# What the line says when an editable checkout exists but its commit cannot be read --
# git absent, or the recorded path is not a repository. Saying so is the honest answer: a
# number alone would claim a currency this call cannot verify.
UNKNOWN_COMMIT = "unknown"


def _normalise(name: str) -> str:
    """PEP 503 normalisation, so `gitgrip`, `GitGrip` and `git_grip` are one name."""
    return re.sub(r"[-_.]+", "-", name).strip().lower()


def _matching_distributions(distribution: str) -> list[importlib.metadata.Distribution]:
    """EVERY installed distribution with this name, not just the first one found.

    A source tree can carry a legacy `<name>.egg-info` BESIDE a properly installed
    `<name>-<version>.dist-info`, and `importlib.metadata.distribution()` returns whichever
    comes first on `sys.path`. So which one answers depends on the CALLER's path rather
    than on the code that is running. Measured on this repository: with the source
    directory on `sys.path` (which is how this project's own test suite runs) the egg-info
    won and the install read as RELEASED, while the console script -- a different process,
    a different path -- named a commit for the same install. Same bytes, two answers,
    decided by where the caller stood.
    """
    wanted = _normalise(distribution)
    found: list[importlib.metadata.Distribution] = []
    for dist in importlib.metadata.distributions():
        metadata = dist.metadata
        name = metadata["Name"] if metadata is not None else None
        if name and _normalise(str(name)) == wanted:
            found.append(dist)
    return found


def _editable_path(dist: importlib.metadata.Distribution) -> PurePath | None:
    """The checkout THIS distribution records as an editable install, or None.

    The presence of `direct_url.json` is NOT the test. A plain local-directory install
    (`pip install ./dir`) writes the same file with `dir_info.editable` false, and naming a
    commit for it would point at a checkout the running code does not come from.
    """
    try:
        raw = dist.read_text("direct_url.json")
    except (UnicodeDecodeError, OSError):
        # THE SIXTH SEAM: this is the module's one unguarded READ, and the guards below it
        # all assume the read succeeded. `PathDistribution.read_text` suppresses exactly five
        # exceptions (FileNotFoundError, IsADirectoryError, KeyError, NotADirectoryError,
        # PermissionError), and two malformed shapes escape that set: non-UTF-8 bytes, which
        # raise `UnicodeDecodeError` -- a `ValueError`, NOT an `OSError`, so catching OSError
        # alone misses it -- and a symlink loop, which raises `OSError` ELOOP. Measured: both
        # escaped through this function AND through `version_line`. A record that cannot be
        # READ gets the declared answer for a record that cannot be PARSED: no readable
        # editable checkout, so None.
        return None
    if not raw:
        return None
    try:
        doc = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(doc, dict):
        return None
    # THE FIFTH SEAM, and it is the same member of the same finite set as the two guards
    # above. `(doc.get("dir_info") or {}).get(...)` reads as a null guard and is not one:
    # `or {}` fires on a FALSY value, so a truthy NON-dict -- the string "boom", the
    # integer 5, a list -- passes through and `.get` raises AttributeError out of
    # `--version`. Measured, one fresh process per case: all three raise, while `null` is
    # fine because it is falsy. The module promises three times that no failure path
    # raises, and a corrupt record is exactly the input that promise is about, so the
    # TYPE is checked rather than assumed. `dir_info` is the last member of the set the
    # two guards above already cover: unreadable JSON, then a non-object document, then a
    # non-object `dir_info`.
    dir_info = doc.get("dir_info")
    if not isinstance(dir_info, dict) or not dir_info.get("editable"):
        return None
    url = doc.get("url")
    if not isinstance(url, str) or not url.startswith("file://"):
        return None
    path = _file_url_to_path(url)
    if isinstance(path, Path):
        return path if path.is_dir() else None
    # A Windows-shaped checkout recorded on a host that cannot hold it. The record SAYS
    # editable, so this is not "not editable" -- it is a checkout whose commit cannot be
    # read here, which is the declared `<version>+unknown` and not a crash. `PurePath` has
    # no `is_dir()` AT ALL, so calling one on this branch raises AttributeError inside
    # `--version`: the guard above is the difference between a stated outcome and a
    # traceback, and the docstring's promise that no failure path raises depends on it.
    return path


#: `/C:/Users/repo` -- a Windows-shaped path, slash-separated.
_WINDOWS_FILE_PATH = re.compile(r"^/[A-Za-z]:[/\\]")


def _file_url_to_path(url: str) -> PurePath:
    """A `file://` URL as a path, with a Windows DRIVE LETTER kept intact.

    `Path(urlparse(url).path)` drops the drive: on `file:///C:/repo` the path component is
    `/C:/repo` and the drive is empty, so an editable install on Windows would be tested
    against a path that cannot exist and would read as `released`. Measured through
    `PureWindowsPath`; the consequence for `is_dir()` follows from it.

    `url2pathname` is the standard library's own answer to exactly this, and it is keyed on
    the RUNNING platform -- so it converts on Windows and leaves `/C:/repo` untouched
    anywhere else. The explicit fallback below decodes the Windows form on a host where
    `url2pathname` will not, which is what makes the behaviour testable off Windows
    instead of being taken on trust until a Windows user meets it.
    """
    native = url2pathname(urlparse(url).path)
    if _WINDOWS_FILE_PATH.match(native):
        return PureWindowsPath(native.lstrip("/"))
    return Path(native)


def _version_of(dist: importlib.metadata.Distribution) -> str | None:
    metadata = dist.metadata
    if metadata is None:
        return None
    version = metadata["Version"]
    return str(version) if version else None


def _distribution_facts(distribution: str) -> tuple[str | None, PurePath | None]:
    """The version AND the editable checkout, read from ONE resolved distribution.

    ONE RESOLUTION, TWO FACTS, and this is the whole point of the function. Reading the
    version through `importlib.metadata.version()` while the editable source came from a
    separate scan let the two facts come from two DIFFERENT installs: the number from
    whichever `sys.path` reached first, the commit from the editable one. Measured on one
    clone at a single commit with only path order changed, that printed
    `2.0.0a2+g3d28372` in one order and `2.0.0a5+g3d28372` in the other -- a stale number
    spliced onto a current commit, presented as one coherent installation.

    The rule, deterministic and independent of the caller's path:

    * an EDITABLE match wins wherever it sits, because an editable checkout is the
      strongest available evidence of which bytes are running;
    * with no editable match, the matches must AGREE on the version; if two installed
      distributions under one name disagree and neither is editable, there is no single
      version to state and the answer is the bare `unknown` form rather than whichever one
      the path reached first.
    """
    matches = _matching_distributions(distribution)
    if not matches:
        return None, None
    editables = [
        (_version_of(dist), _editable_path(dist))
        for dist in matches
        if _editable_path(dist) is not None
    ]
    if editables:
        # MORE THAN ONE EDITABLE IS ALSO AN AMBIGUITY, and picking the first would put the
        # caller's path back in charge of BOTH facts. Two editable installs under one name
        # are two claims about which checkout is running; if they agree, either answers,
        # and if they disagree there is no single answer to give.
        distinct = {(version, str(source)) for version, source in editables}
        if len(distinct) > 1:
            return None, None
        return editables[0]
    versions = {_version_of(dist) for dist in matches}
    if len(versions) > 1:
        return None, None
    return versions.pop(), None


def _clone_short_sha(clone: PurePath) -> str | None:
    """The checkout's short HEAD sha, or None when it cannot be read.

    Every failure returns None rather than raising: `--version` is the first thing a
    stranger runs, so a missing or broken tool must degrade to a less specific answer, not
    to a traceback.
    """
    git = shutil.which("git")
    if git is None:
        return None
    try:
        proc = subprocess.run(
            [git, "rev-parse", "--short", "HEAD"],
            cwd=clone,
            capture_output=True,
            text=True,
            check=False,
        )
    except (OSError, TypeError, ValueError):
        # `TypeError`/`ValueError` are for a path this host cannot represent at all -- a
        # Windows-shaped record read on POSIX. Same rule as the rest of this module: a
        # value that cannot be measured returns None, it does not raise.
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def version_line(distribution: str = DISTRIBUTION) -> str:
    """The one line `gr2 --version` prints.

    Four forms, in the order they are decided:

    * `<version>+g<sha>` -- an editable checkout, named by the commit at its HEAD;
    * `<version> released` -- no readable editable checkout was recorded: a wheel, or a
      `direct_url.json` that is absent, malformed, or not a directory record;
    * `<version>+unknown` -- a checkout exists but its commit cannot be read (no `git`, or
      the recorded path is not a repository), so the version is stated and the commit is
      not invented;
    * a **bare `unknown`** -- the distribution is not installed for this interpreter, so
      there is no version to state at all.
    """
    version, clone = _distribution_facts(distribution)
    if version is None:
        return "unknown"
    if clone is None:
        return f"{version} released"
    sha = _clone_short_sha(clone)
    if sha is None:
        return f"{version}+{UNKNOWN_COMMIT}"
    return f"{version}+g{sha}"
