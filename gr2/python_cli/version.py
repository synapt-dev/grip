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

`released` means **no editable record**: a wheel, or a `direct_url.json` that is absent,
unreadable, malformed, or does not declare `dir_info.editable`. It does NOT mean a record
that SAYS editable and could not be read -- that one is the declared `<version>+unknown`,
because the record states a checkout exists while its commit cannot be read here.

That split was wrong in this module until three readers raised it independently, and they
were right: `released` was being printed for records that said the opposite. Getting it right
within the four forms is why there is one rule for a declared-editable record rather than a
branch per failure -- a branch per failure is also how a stat ended up re-raising errnos that
`is_dir()` does not ignore.

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
# git absent, or the recorded path is not a repository. Saying so states what the call
# established; a number alone would claim a currency it cannot verify.
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
        try:
            metadata = dist.metadata
        except (UnicodeDecodeError, OSError):
            # THE SEVENTH SEAM, and it is wider than the function it was found in.
            # `dist.metadata` is not a second primitive -- it is `read_text('METADATA')`,
            # with the same five-exception suppression list `direct_url.json` has, so a
            # non-UTF-8 METADATA raises UnicodeDecodeError straight through here.
            #
            # AND IT IS READ BEFORE THE NAME FILTER, so ONE corrupt METADATA anywhere on
            # `sys.path` breaks the lookup for EVERY name, not only its own. Measured: with a
            # single bad-metadata dist-info present, lookups for an unrelated, perfectly
            # readable name raised too -- and that unrelated name was the CONTROL, which is
            # how the shape was found. Skipping the unreadable distribution is the fix that
            # lets the scan finish; if it was the only match, the caller falls through to
            # the declared bare `unknown`.
            continue
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
    # THE RECORD SAYS EDITABLE, SO THE QUESTION IS NOT WHETHER THE PATH IS A DIRECTORY -- it
    # is whether a COMMIT can be read from it, and that is `_clone_short_sha`'s job.
    #
    # The probe that used to sit here (`path.is_dir()`) was both unnecessary and a crash
    # surface. Unnecessary: `pathlib._IGNORED_ERRNOS` is only (2, 20, 9, 62) -- ENOENT,
    # ENOTDIR, EBADF, ELOOP -- so `is_dir()` swallows a missing path and RE-RAISES
    # everything else, which is how a path under a non-traversable directory raises
    # PermissionError and an over-long component raises Errno 63, both straight out of
    # `--version`. Measured on this module, with the ordinary missing path as the control
    # that makes the probe look verified. A crash surface, because every way an editable
    # checkout can fail to yield a commit -- gone, a file, not a repository, `git` absent,
    # a path this host cannot represent, a path it cannot probe -- is ONE declared outcome
    # already: `<version>+unknown`.
    #
    # And it was answering the wrong question. `released` asserts there is no editable
    # checkout; the record in hand says there is. Three readers raised that objection
    # independently, so the answer is the form that already means what happened: a checkout
    # exists and its commit could not be read. That is why this returns the path for EVERY
    # declared-editable record, including the Windows-shaped one below -- one rule, not a
    # branch per failure.
    try:
        return _file_url_to_path(url)
    except ValueError:
        # THE SIXTH SHAPE, and it is the FIFTH SEAM ONE MEMBER FURTHER OUT. That one let a
        # truthy non-dict through to `.get` and raised AttributeError; this lets a malformed
        # STRING through to `urlparse`, which raises `ValueError: Invalid IPv6 URL` on an
        # unclosed bracket -- `file://[::1/repo`. The well-formed `file://[fe80::1]:80/x`
        # parses fine, so it is the bracket and not IPv6 as such.
        #
        # Measured across eighteen corrupt record shapes, one fresh process each: seventeen
        # answer a declared form and this one raised, out of `_editable_path` AND
        # `version_line`. The comment above calls its own case the last member of the set,
        # and the set was not closed -- which is why the type check on the STRING lives here
        # rather than being assumed from the `isinstance` and `startswith` above it.
        #
        # The record still SAYS editable, so `released` is the same false claim this module
        # already refused elsewhere; a value that is not a path cannot yield a commit, so the
        # declared `<version>+unknown` is the outcome and the pure value keeps it there.
        # `_clone_short_sha` already catches `ValueError` for this exact class one function
        # away, so the exception is known here rather than new.
        return PurePath(url)


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
    if not native:
        # THE SEVENTH SHAPE, AND IT IS THE WORST OF THEM: a CONFIDENT FALSE ANSWER rather
        # than a stated non-answer. `file://`, `file://host` and `file://.` all parse
        # CLEANLY (the netloc is ignored by the `startswith("file://")` guard above), so the
        # `ValueError` catch in `_editable_path` never fires for them --
        # `urlparse("file://").path` is `""`, `url2pathname("")` is `""`, and `Path("")`
        # **IS** `Path(".")`. `_clone_short_sha` then runs `git rev-parse` with the process
        # cwd and reports THAT repository's HEAD in the declared `<version>+g<sha>` form.
        #
        # Measured, one record, two working directories: `2.0.0a5+g759b21bc` from a clone and
        # `2.0.0a5+unknown` from a directory that is not a repository. The module names an
        # unrelated commit, with no warning, decided by where the caller stood -- which is
        # this module's own declared wrongness reached through the CWD instead of through
        # `sys.path`. `file:///` gives `Path("/")` and lands harmlessly on `+unknown`; it is
        # the EMPTY component that lies.
        #
        # A url naming no path names no checkout, so the honest outcome is the declared
        # `+unknown` and it is reached by the branch `_editable_path` already has.
        raise ValueError(f"this file:// URL names no path: {url!r}")
    if _WINDOWS_FILE_PATH.match(native):
        return PureWindowsPath(native.lstrip("/"))
    return Path(native)


def _version_of(dist: importlib.metadata.Distribution) -> str | None:
    try:
        metadata = dist.metadata
    except (UnicodeDecodeError, OSError):
        # The second call site of the same read guarded in `_matching_distributions`, and
        # the same rule: a value that cannot be measured returns None rather than raising.
        # A distribution reaching here with unreadable metadata yields no version, which
        # leaves the caller with no single version to state -- the declared bare `unknown`.
        return None
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
    * `<version> released` -- no editable record at all: a wheel, or a `direct_url.json` that
      is absent, unreadable, malformed, or does not declare `dir_info.editable`;
    * `<version>+unknown` -- an editable record exists but no commit could be read from the
      checkout it names: `git` absent, not a repository, gone, a file, a path this host
      cannot represent, or a path it cannot probe. The version is stated and the commit is
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
