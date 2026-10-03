"""`review run <lane-dir>`: the review-owned in-lane test run — the last raw-shell
exit point (venv + install + pytest by hand) folded into one verb.

It runs ONLY inside a `review open --enter` reconstruction lane (it reads the
`.grip-review-open.json` marker, or the pre-rename one), so a green is always about a bound tree.
Two structural bindings make the green mean something:

  * THE TREE COMPARISON — the lane's current working tree must equal the marker's
    bound head-tree. One comparison catches both a wrong reconstruction and a lane
    drifted after open (a touched tracked file changes the tree). Asserted BEFORE
    the venv is created, so the venv never pollutes the hash.
  * IMPORT UNDER THE LANE — after install, the package's resolved `__file__` must
    live under the lane, or a second checkout shadowing it on `PYTHONPATH` would let
    a pass be about someone else's tree (the stale editable-install trap).

Counts come from pytest's SUMMARY LINE, never the exit code (a run that collected
zero tests exits 0). A run that collects/selects zero tests, or whose summary line
cannot be parsed, is a REFUSAL, not a green.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import tomllib
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from . import lane_graph, lane_plugins

# An in-repo hint read when `--install` is omitted. It lives at the REPO ROOT
# (the lane), NOT in a pyproject table, on purpose: a repo whose importable
# package is a SUBDIR (grip's is `gr2/`) has no top-level pyproject, which is the
# exact case the default `pip install -e <lane>` cannot handle — so a pyproject
# hint would be unreadable precisely where it is needed. A root sentinel file
# works regardless of where the package lives. Format: `key = value` lines, `#`
# comments; keys `install` (a command with {venv} and {lane} placeholders,
# shell-split FIRST, then {venv}/{lane} substituted per token — so a lane path with
# a space stays one token) and optional `package` (the import name whose __file__
# must resolve under the lane).
_HINT_NAME = ".review-install"


_HINT_KEYS = frozenset({"install", "package", "runner", "test", "reports"})


def _apply_install_placeholders(tokens: list[str], venv_python: Path, repo_dir: Path) -> list[str]:
    """Substitute `{venv}` and `{lane}` per token, so a lane path containing a space
    stays one token (substituting before shell-splitting would let the space break the
    token apart). The single substitution point shared by the --install flag and the
    .review-install hint, so both accept the identical template (review-run door 2)."""
    return [
        tok.replace("{venv}", str(venv_python)).replace("{lane}", str(repo_dir))
        for tok in tokens
    ]


def read_install_hint(repo_dir: Path) -> dict | None:
    """Parse `<repo_dir>/.review-install`; return optional runner settings or None
    when the file is absent. `reports` is the JUnit XML glob for the `junit-xml`
    runner. An unrecognised key is a
    REFUSAL (`bad_hint`), not a silent skip: a typo like `instal = ...` would
    otherwise fall through to the default install and refuse under a cause the repo
    never declared."""
    p = repo_dir / _HINT_NAME
    if not p.is_file():
        return None
    out: dict[str, str] = {}
    for raw in p.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        if key not in _HINT_KEYS:
            raise ReviewRunRefused(
                "bad_hint",
                f"unrecognised key {key!r} in {p}; allowed keys are "
                f"{sorted(_HINT_KEYS)}",
            )
        out[key] = val.strip()
    return out

_MARKER_NAME = ".grip-review-open.json"
_MARKER_KIND = "review-open"
# The marker lanes opened before the open-gr -> open rename carry on disk. READ only,
# never written, and dropped with the open-gr/close-gr aliases (same drop path), so a
# stranger who upgrades mid-lane still gets their own lane reclaimed.
_LEGACY_MARKERS = {".grip-open-gr-reconstruct.json": "open-gr-reconstruct"}


def find_marker(lane_dir: Path) -> Path | None:
    """The lane's reconstruction marker: the current name first, then a legacy one."""
    for name in (_MARKER_NAME, *_LEGACY_MARKERS):
        path = Path(lane_dir) / name
        if path.is_file():
            return path
    return None


def marker_kind_ok(marker: dict) -> bool:
    """Whether ``marker`` is a reconstruction marker: the current kind or a legacy one."""
    return marker.get("kind") in {_MARKER_KIND, *_LEGACY_MARKERS.values()}

_RECEIPT_NAME = ".grip-review-run.json"
# The full pytest output, persisted beside the receipt. The receipt's counts and
# `failed_ids` say WHAT failed; this file is the raw text a reviewer reads to see
# WHY. Named so `close-gr` can carry it (and the receipt) out before it reclaims
# the lane (review-run door 1).
_OUTPUT_LOG_NAME = ".grip-review-run.log"
_VENV_DIRNAME = ".venv"


class ReviewRunRefused(Exception):
    """A structural refusal: the run cannot yield a trustworthy green."""

    def __init__(self, code: str, detail: str, member: str | None = None) -> None:
        self.code = code
        self.detail = detail
        # A multi-member lane stops at the first refusal. The driver fills these in so the
        # receipt and the CLI can say WHICH member refused, which members still ran green
        # or red before it, and which never ran.
        self.member = member
        self.order: list[str] | None = None
        self.order_source: str | None = None
        self.completed: list[dict] = []
        self.not_run: list[str] = []
        super().__init__(f"{code}: {detail}")


def _git(repo_dir: Path, *args: str, env: dict | None = None) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo_dir), *args],
        text=True,
        capture_output=True,
        env=env,
    )
    if proc.returncode != 0:
        raise ReviewRunRefused(
            "git_failed",
            f"git {' '.join(args)} in {repo_dir} exited {proc.returncode}: "
            f"{proc.stderr.strip()}",
        )
    return proc.stdout.strip()


# ---- the tree comparison (drift + reconstruction, ONE check) ----------------

def compute_working_tree(repo_dir: Path) -> str:
    """The tree hash of the current TRACKED content of repo_dir, computed in a
    throwaway index so the real index is untouched. `add -u` stages modifications
    and deletions of tracked files but NOT untracked additions, so the open-gr
    marker, the lane `.venv`, and the run receipt do not read as drift — only a
    change to a reconstructed (tracked) file does."""
    with tempfile.TemporaryDirectory() as td:
        idx = str(Path(td) / "index")
        env = {**os.environ, "GIT_INDEX_FILE": idx}
        _git(repo_dir, "read-tree", "HEAD", env=env)
        _git(repo_dir, "add", "-u", env=env)
        return _git(repo_dir, "write-tree", env=env)


def assert_lane_tree_bound(repo_dir: Path, bound_head_tree: str) -> str:
    """THE tracked-tree comparison. Refuse unless the lane's current tracked tree
    equals the bound head-tree recorded at open. Returns the computed tree. Dropping
    this comparison lets a MODIFIED tracked file pass as a green."""
    if not bound_head_tree:
        raise ReviewRunRefused(
            "no_bound_tree",
            f"the open-gr marker for {repo_dir} records no bound_head_tree; "
            "reopen the lane with a build that records it",
        )
    current = compute_working_tree(repo_dir)
    if current != bound_head_tree:
        raise ReviewRunRefused(
            "tree_drift",
            f"lane tree {current} != bound head-tree {bound_head_tree} "
            f"({repo_dir}); the run would not be about the pinned head",
        )
    return current


# Untracked paths the run itself is expected to create; everything else untracked in
# the lane is drift, because an injected conftest.py or module can change what the
# tests do WITHOUT touching the tracked tree (which `assert_lane_tree_bound` sees).
_UNTRACKED_ALLOW_NAMES = frozenset({_MARKER_NAME, *_LEGACY_MARKERS, _RECEIPT_NAME, _OUTPUT_LOG_NAME})
_UNTRACKED_ALLOW_TOP = (_VENV_DIRNAME + "/",)
_UNTRACKED_ALLOW_SEGMENTS = frozenset({"__pycache__", ".pytest_cache", ".mypy_cache"})


def _is_allowlisted_untracked(
    rel_path: str,
    extra_names: frozenset[str] = frozenset(),
    extra_tops: tuple[str, ...] = (),
    extra_segments: frozenset[str] = frozenset(),
) -> bool:
    if rel_path in _UNTRACKED_ALLOW_NAMES or rel_path in extra_names:
        return True
    tops = _UNTRACKED_ALLOW_TOP + tuple(extra_tops)
    if any(rel_path == pre.rstrip("/") or rel_path.startswith(pre) for pre in tops):
        return True
    segments = rel_path.strip("/").split("/")
    if any(
        seg in _UNTRACKED_ALLOW_SEGMENTS or seg in extra_segments or seg.endswith(".egg-info")
        for seg in segments
    ):
        return True
    return False


def assert_no_untracked_drift(
    repo_dir: Path,
    *,
    extra_allow_names: frozenset[str] = frozenset(),
    extra_allow_tops: tuple[str, ...] = (),
    extra_allow_segments: frozenset[str] = frozenset(),
) -> None:
    """Refuse if the lane holds any untracked path the run did not create. Without
    this an untracked `conftest.py` (or shadow module) that patches the package turns
    a failing tree green while the tracked-tree comparison passes. The complement of
    `assert_lane_tree_bound`: dropping either reds only its own drift witness.

    ``extra_allow_names``/``extra_allow_tops`` are a runner's OWN created outputs (a
    cargo run writes `target/` and `Cargo.lock`, a jest run `node_modules/`/`coverage/`),
    the language-specific analogue of the pytest path's `.venv`/receipt exemptions — so a
    second `review run` on an un-gitignored lane does not refuse the first run's outputs as
    drift.

    The status call neutralizes the HOST's own ignore rules (`-c core.excludesFile=`,
    which git treats as case-insensitively the same key whichever spelling a host's
    config used, and which suppresses BOTH an explicit `core.excludesFile` and git's
    own `$XDG_CONFIG_HOME/git/ignore` fallback when neither is set). Without this, a
    host whose global config ignores a directory name — measured on one host in this
    fleet: `__pycache__/` — makes `git status` blind to anything planted inside a
    directory with that name, so an injected module the trust boundary exists to
    catch passes through it silently instead of refusing. Repo-local `.gitignore`
    and `.git/info/exclude` are deliberately left in force: those belong to the
    repository under review, not to whatever the reviewing machine happens to
    ignore."""
    out = _git(repo_dir, "-c", "core.excludesFile=", "status", "--porcelain")
    offending = []
    for line in out.splitlines():
        if line.startswith("?? "):
            rel = line[3:].strip().strip('"')
            if not _is_allowlisted_untracked(
                rel, extra_allow_names, extra_allow_tops, extra_allow_segments
            ):
                offending.append(rel)
    if offending:
        raise ReviewRunRefused(
            "untracked_drift",
            f"untracked path(s) in the lane the run did not create: "
            f"{', '.join(offending[:5])}; an injected conftest/module can change test "
            "behavior without touching the tracked tree",
        )


# What a plain `pip install <dir>` leaves in the tree it installs (measured: an untracked `build/`
# and `src/<name>.egg-info/`). `*.egg-info` is already allowed by `_is_allowlisted_untracked`, so
# `build/` is the one extra. This is the ONLY place a NEW untracked path is admitted after the
# baseline was taken; widening it re-opens the hole `assert_no_new_untracked` closes (an install or
# a test dropping a `conftest.py` or any importable `.py`).
_INSTALL_OUTPUT_TOPS = ("build/",)


def list_untracked(repo_dir: Path) -> set[str]:
    """The untracked paths git reports in `repo_dir`, host ignore rules neutralized (see
    `assert_no_untracked_drift` for why). Every file is listed on its own: without
    `--untracked-files=all` git collapses a wholly untracked directory to one entry, and a file
    added inside it later would leave the listing unchanged."""
    out = _git(
        repo_dir, "-c", "core.excludesFile=", "status", "--porcelain", "--untracked-files=all"
    )
    return {line[3:].strip().strip('"') for line in out.splitlines() if line.startswith("?? ")}


def assert_no_new_untracked(repo_dir: Path, baseline: set[str]) -> None:
    """Refuse when `repo_dir` holds an untracked path that was not there at `baseline` and is not
    something a run or an install is expected to create. The baseline is taken after the first
    drift check, before the install, so everything in it already passed that check; what is new
    since was written by the install step or by tests, which is the code under review. Naming
    each offender is the point: an untracked `conftest.py` or `.pth`-reachable `.py` changes what
    runs without touching the tracked tree."""
    offending = sorted(
        rel
        for rel in list_untracked(repo_dir) - baseline
        if not _is_allowlisted_untracked(rel, extra_tops=_INSTALL_OUTPUT_TOPS)
    )
    if offending:
        raise ReviewRunRefused(
            "untracked_drift",
            f"new untracked path(s) appeared in the lane after the checks: "
            f"{', '.join(offending[:5])}; an install or test that adds a conftest or an importable "
            "module changes what runs without touching the tracked tree. If it is a test artifact "
            "(a coverage file, say), ignore it in the repository's .gitignore",
        )


# ---- import resolves under the lane -----------------------------------------

def scrubbed_python_env(base: dict | None = None, *, venv_dir: Path | None = None) -> dict:
    """Return a copy of the environment with every ``PYTHON*`` variable removed, and
    (when ``venv_dir`` is given) shaped to look like that venv was ACTIVATED.

    ``-I`` isolates the CHECK subprocesses (it drops cwd and, via ``-E``, ignores
    ``PYTHON*`` env), so the import resolves under the lane no matter what the caller
    exported. But pytest itself runs WITHOUT ``-I`` — a plugin or conftest may
    legitimately need the ambient interpreter — so a rogue package on ``PYTHONPATH``
    outside the lane would be imported by the RUN while the check certified the lane:
    a receipt naming the lane about someone else's tree (a full stale checkout would
    even produce a GREEN one), the class review run exists to refuse. Passing this
    scrubbed env to BOTH checks and pytest makes the check's environment the run's
    environment, so ``PYTHONPATH``/``PYTHONHOME``/``PYTHONSAFEPATH``/
    ``PYTHONNOUSERSITE``/``PYTHONSTARTUP`` and any other ``PYTHON*`` var can no longer
    redirect the import the check just proved. ``-I`` still guards the CHECK against
    the cwd shadow (the scrub does not touch cwd; ``-I`` does not touch the run).

    ``venv_dir`` closes a SEPARATE gap: the venv's interpreter is invoked
    by absolute path, which needs no PATH entry for itself, but a repository's OWN
    tests can shell out to ITS OWN installed console scripts (an editable install of
    `gr2` puts `gr2`/`git-review` in ``<venv>/bin``, and grip's own packaging test does
    exactly this) -- and without an activation-shaped env, that lookup fails with
    `FileNotFoundError`, RED, in a repo whose own developers would never see it,
    because their shell has that venv's `bin/` on PATH. Fathom's stranger dogfood hit
    this on grip's OWN suite the first time anyone ran `git review run` from a fresh
    venv and an ordinary clone -- neither the author's nor either
    reviewer's suite run caught it, because all three of us activated the venv by hand
    before running, which is exactly the ambient shell state a lane's own tests cannot
    assume when the run's *subprocess* env is built fresh here rather than inherited.
    Matches what `python -m venv --prompt` activation actually does: the venv's
    `bin/` is PREPENDED to PATH (so its console scripts and its own `python`/`pytest`
    resolve first) and ``VIRTUAL_ENV`` is set to the venv root. ``PYTHONHOME`` is
    already gone via the PYTHON* scrub above, which is the other half real
    activation does."""
    env = dict(os.environ if base is None else base)
    for key in [k for k in env if k.startswith("PYTHON")]:
        del env[key]
    if venv_dir is not None:
        venv_bin = venv_dir / "bin"
        existing_path = env.get("PATH", "")
        env["PATH"] = str(venv_bin) + (os.pathsep + existing_path if existing_path else "")
        env["VIRTUAL_ENV"] = str(venv_dir)
    return env


def resolve_import_file(venv_python: Path, package: str, env: dict) -> str:
    """Import `package` in the venv python (under `env`) and return its __file__.

    Run with `-I` (isolated): `python -c` otherwise prepends the process cwd to
    `sys.path[0]`, and this subprocess inherits the lane as cwd, so a repo whose
    ROOT holds a directory named like its importable package (grip's `gr2/`)
    resolves that project directory as a PEP 420 namespace package with no
    `__file__` — `import_no_file` on a lane whose editable install is perfectly
    correct. `-I` drops cwd (and PYTHON* env / user site) from the path, so the
    CHECK's import resolves to the installed package under the lane, which is exactly
    what `assert_import_under_lane` then verifies. `-I` isolates the CHECK only; the
    RUN is isolated by `scrubbed_python_env` (a `PYTHONPATH` shadow is neutralized for
    pytest there, not here)."""
    proc = subprocess.run(
        [str(venv_python), "-I", "-c", f"import {package} as _m; print(_m.__file__ or '')"],
        text=True,
        capture_output=True,
        env=env,
    )
    if proc.returncode != 0:
        raise ReviewRunRefused(
            "import_failed",
            f"could not import {package!r} in the lane venv: {proc.stderr.strip()}",
        )
    path = proc.stdout.strip()
    if not path:
        raise ReviewRunRefused(
            "import_no_file",
            f"{package!r} has no __file__ (namespace package?); cannot bind the "
            "install to the lane",
        )
    return path


def assert_import_under_lane(resolved_file: str, lane_dir: Path) -> None:
    """Refuse unless the resolved import path lives under the lane. A second checkout
    on PYTHONPATH would otherwise let a pass be about a different tree."""
    p = Path(resolved_file).resolve()
    root = lane_dir.resolve()
    if root != p and root not in p.parents:
        raise ReviewRunRefused(
            "import_escapes_lane",
            f"{p} does not resolve under the lane {root}; a checkout outside the "
            "lane is shadowing the reconstruction (stale editable install)",
        )


# ---- undeclared-extra detection (pip exits 0 but warns) ----

# pip exits 0 when an install requests an extra the package does not declare,
# emitting `WARNING: <name> <version> does not provide the extra 'X'` on stderr
# (older pip omits the version). The invariant is the phrase, so anchor on it and
# ignore the version. Match either quote style pip might use.
_UNDECLARED_EXTRA_RE = re.compile(r"""does not provide the extra ['"]([^'"]+)['"]""")


def detect_undeclared_extras(output: str) -> list[str]:
    """Return the sorted unique extra names pip reported as undeclared in `output`.

    A typo'd or undeclared extra in a repo's own `.review-install` (say `gr2[devv]`
    for `gr2[dev]`) makes pip install NOTHING of what that extra promised — pytest
    and the rest of the test deps — while exiting 0. The run then fails later under
    `pytest_not_installed`, which points at the symptom, not the bad extra name. This
    lets the run name the root cause first."""
    return sorted({m.group(1) for m in _UNDECLARED_EXTRA_RE.finditer(output)})


# ---- pytest summary parsing (counts from the summary line, not exit code) ----

_COLLECTED_RE = re.compile(r"collected (\d+) item")
_SELECTED_RE = re.compile(r"(\d+) selected")
# A summary line ends with "in <time>s" (barred in normal mode, bare in -q), or is
# the "no tests ran in <time>s" line; leading/trailing "=" bars are optional.
_SUMMARY_LINE_RE = re.compile(r"(?:in \d+\.\d+s|no tests ran)")
_TIME_TAIL_RE = re.compile(r"\bin \d+\.\d+s\b|\bno tests ran\b")
_COUNT_RE = re.compile(
    r"(\d+) (passed|failed|error|errors|skipped|xfailed|xpassed|deselected|warning|warnings)"
)


def parse_pytest_summary(stdout: str) -> dict | None:
    """Counts from pytest's SUMMARY LINE, never the exit code. Handles both the
    barred normal-mode line (`===== 3 passed in 0.01s =====`) and the bare `-q` line
    (`1 passed in 0.00s`, `1 deselected in 0.00s`). Returns a dict with
    collected/selected/deselected/passed/failed/skipped/xfailed/errors, or None if
    no summary line exists (unparseable output -> the caller refuses). A run where no
    test ran yields selected==0 (the caller refuses)."""
    lines = stdout.splitlines()
    summary_body: str | None = None
    for line in reversed(lines):
        if _SUMMARY_LINE_RE.search(line):
            summary_body = line.strip().strip("=").strip()
            break
    if summary_body is None:
        return None

    counts = {k: 0 for k in ("passed", "failed", "errors", "skipped", "xfailed", "xpassed")}
    deselected = 0
    for n, word in _COUNT_RE.findall(summary_body):
        if word in ("error", "errors"):
            counts["errors"] = int(n)
        elif word in ("warning", "warnings"):
            continue
        elif word == "deselected":
            deselected = int(n)
        else:
            counts[word] = int(n)

    collected = None
    selected = None
    for line in lines:
        cm = _COLLECTED_RE.search(line)
        if cm:
            collected = int(cm.group(1))
        sm = _SELECTED_RE.search(line)
        if sm:
            selected = int(sm.group(1))
        dm = re.search(r"(\d+) deselected", line)
        if dm:
            deselected = max(deselected, int(dm.group(1)))

    ran = (
        counts["passed"] + counts["failed"] + counts["errors"]
        + counts["skipped"] + counts["xfailed"] + counts["xpassed"]
    )
    if "no tests ran" in summary_body:
        selected = 0
    elif selected is None:
        selected = ran
    return {
        "collected": collected,
        "deselected": deselected,
        "selected": selected,
        **counts,
    }


# ---- failed-test node ids (which tests failed, not just how many) -----------

# pytest's short test summary info section (emitted under `-rfE`, which the run
# always passes) lists one line per non-passing outcome:
#   FAILED tests/test_x.py::test_bad - AssertionError: ...
#   FAILED tests/test_x.py::test_p[case 2 with spaces] - AssertionError
#   ERROR tests/test_x.py::test_y - fixture 'conn' not found   (setup/collection)
# The node id is everything between the status word and pytest's ` - <message>`
# separator (space-dash-space), or the rest of the line when there is no message.
# A `\S+` token would truncate a PARAMETRIZED id at the first space inside its
# brackets, losing the exact case a reviewer must re-run — so match to the ` - `
# instead. Anchored at line start (MULTILINE) so the `ERRORS` banner and the
# `___ ERROR at setup of ___` divider lines, which do not start with the word, are
# not mistaken for summary rows. review-run door 1: the 35 env failures in the
# real review were unrecoverable from the receipt because this was never captured.
# Known edge: a param whose brackets literally contain " - " (space-dash-space)
# truncates there, since that is also the id/message separator; pytest usually
# sanitizes such ids and the truncation still keeps the file and test stem, so it
# is accepted rather than guarded.
_FAILED_ID_RE = re.compile(r"^(?:FAILED|ERROR)\s+(.+?)(?: - .*)?$", re.MULTILINE)


def parse_failed_ids(output: str) -> list[str]:
    """Return the sorted unique node ids pytest reported as FAILED or ERROR in
    `output`'s short test summary. A count of failures with no ids is a dead end
    for a reviewer; this is the path back to the exact tests to re-run."""
    return sorted({m.group(1) for m in _FAILED_ID_RE.finditer(output)})


def merge_report_flags(pytest_args: list[str]) -> list[str]:
    """Return `pytest_args` with a single `-r` spec GUARANTEED to make the short test
    summary list every FAILED and ERROR node id for parse_failed_ids.

    Two pytest facts drive this and neither is `-r`-is-additive:
      * `-r` is LAST-WINS across tokens, so a prepended `-rfE` is silently overridden
        by any later caller `-r` — failed_ids then comes back EMPTY on a real red run.
      * within one `-r` spec, the chars are processed IN ORDER and `N` (none) CLEARS
        everything before it. So a sorted union like `-rENf` loses ERROR: E is added,
        N clears it, f is added — a red run with an ERROR-at-setup keeps FAILED and
        drops the ERROR ids. (Measured: `-rfE` prints both, `-rENf` FAILED only.)

    So: collect the caller's `-r` chars in ORDER (deduped), DROP `N` (the run requires
    output, so "none" cannot stand), drop any caller f/E, then append `f` and `E` LAST
    so nothing that follows can clear them. `a`/`A` (all / all-but-passed) stay, ahead
    of f/E, and are harmless supersets."""
    required = ("f", "E")
    caller_seq: list[str] = []
    seen: set[str] = set()
    rest: list[str] = []
    i = 0
    while i < len(pytest_args):
        a = pytest_args[i]
        chars: str | None = None
        if a == "-r" and i + 1 < len(pytest_args):  # `-r fE` (separate arg)
            chars = pytest_args[i + 1]
            i += 2
        elif a.startswith("-r") and len(a) > 2:  # `-rfE` (attached)
            chars = a[2:]
            i += 1
        else:
            rest.append(a)
            i += 1
            continue
        for c in chars:
            if c not in seen:
                seen.add(c)
                caller_seq.append(c)
    ordered = [c for c in caller_seq if c not in ("N", *required)]
    ordered.extend(required)
    return ["-r" + "".join(ordered), *rest]


# ---- the verb ---------------------------------------------------------------

def _read_marker(lane_dir: Path) -> dict:
    marker_path = find_marker(lane_dir)
    if marker_path is None:
        raise ReviewRunRefused(
            "no_marker",
            f"no review marker at {lane_dir / _MARKER_NAME}; `review run` only runs "
            "inside a lane opened by `review open --enter`",
        )
    marker = json.loads(marker_path.read_text())
    if not marker_kind_ok(marker):
        raise ReviewRunRefused(
            "not_open_gr", f"{marker_path} is not a review reconstruction marker"
        )
    return marker


_NOT_A_LANE_CODES = frozenset({"no_marker", "not_open_gr"})


def _write_refusal_receipt(lane_dir: Path, exc: "ReviewRunRefused") -> None:
    """Persist WHY a run refused so the refusal is not invisible on disk (review-run
    door 2): close-gr carries the receipt out of the lane, and `review run --json` has
    something to print on a refusal instead of only a stderr line. Best-effort — a
    failure to write the refusal record must never mask the refusal itself."""
    receipt = {
        "kind": "review-run",
        "created": datetime.now(timezone.utc).isoformat(),
        "result": "refused",
        "refusal_code": exc.code,
        "refusal_detail": exc.detail,
        # A refusal after pytest ran (unparseable_summary, zero_collected) has already
        # written the log; name it when present so close-gr carries it out too.
        "output_log": _OUTPUT_LOG_NAME if (lane_dir / _OUTPUT_LOG_NAME).exists() else None,
    }
    if exc.order is not None:
        # A multi-member lane: name the member that refused, the members that finished
        # before it, and the members that never ran, so a stop never reads as a green.
        member_log = f"{exc.member}{_OUTPUT_LOG_NAME}" if exc.member else None
        has_log = bool(member_log) and (lane_dir / member_log).exists()
        receipt["output_log"] = member_log if has_log else None
        receipt["refusal_member"] = exc.member
        receipt["order"] = exc.order
        receipt["order_source"] = exc.order_source
        receipt["members"] = exc.completed
        receipt["not_run"] = exc.not_run
    try:
        (lane_dir / _RECEIPT_NAME).write_text(json.dumps(receipt, indent=2) + "\n")
    except OSError:
        pass


def run_review_lane(
    lane_dir: Path,
    *,
    package: str | None = None,
    pytest_args: list[str],
    python: str | None = None,
    install: list[str] | None = None,
    system_site_packages: bool = False,
    order: list[str] | None = None,
) -> dict:
    """Run the lane (see `_run_review_lane`) and, on a refusal that is about a real
    lane, leave a receipt recording why (review-run door 2). `no_marker`/`not_open_gr`
    mean the directory is not a lane at all, so no receipt is written there."""
    lane_dir = Path(lane_dir).resolve()
    try:
        return _run_review_lane(
            lane_dir,
            package=package,
            pytest_args=pytest_args,
            python=python,
            install=install,
            system_site_packages=system_site_packages,
            order=order,
        )
    except ReviewRunRefused as exc:
        if exc.code not in _NOT_A_LANE_CODES:
            _write_refusal_receipt(lane_dir, exc)
        raise


def _run_review_lane(
    lane_dir: Path,
    *,
    package: str | None = None,
    pytest_args: list[str],
    python: str | None = None,
    install: list[str] | None = None,
    system_site_packages: bool = False,
    order: list[str] | None = None,
) -> dict:
    """Create `<lane>/.venv`, install the reconstructed tree, and run pytest — but
    only after the lane's tree is proven to equal the bound head-tree and the import
    is proven to resolve under the lane. Returns a receipt. Raises ReviewRunRefused
    for any structural problem (no marker, tree drift, import escape, zero collected,
    unparseable summary).

    A lane that binds more than one repository runs each member in its own directory
    against one shared venv (`_run_multi_member_lane`); a single-repo lane is the clone
    itself and behaves exactly as it always has."""
    lane_dir = Path(lane_dir).resolve()
    marker = _read_marker(lane_dir)
    repos = marker.get("repos", [])
    if not repos:
        raise ReviewRunRefused("no_members", f"the lane marker at {lane_dir} binds no repos")
    if len(repos) > 1:
        return _run_multi_member_lane(
            lane_dir,
            marker,
            repos,
            package=package,
            pytest_args=pytest_args,
            python=python,
            install=install,
            system_site_packages=system_site_packages,
            order=order,
        )
    repo = repos[0]
    if order is not None and list(order) != [repo.get("key", "")]:
        raise ReviewRunRefused(
            "bad_order",
            f"--order names {list(order)}, but this lane binds one member, "
            f"{repo.get('key', '')!r}",
        )
    bound_tree = repo.get("bound_head_tree", "")
    repo_dir = lane_dir  # single-repo lane: the clone IS the lane

    # (1) THE TREE COMPARISON — before the venv exists, so it never pollutes the hash.
    #     Two halves: tracked content equals the bound tree, AND no untracked path the
    #     run did not create (an injected conftest changes behavior invisibly to the
    #     tracked-tree hash).
    assert_lane_tree_bound(repo_dir, bound_tree)
    assert_no_untracked_drift(repo_dir)

    # (2) venv in the lane, so close-gr reclaims it.
    venv_dir, venv_python = _create_lane_venv(lane_dir, python, system_site_packages)

    # The install step and the tests are the repo's own code. After the checks above, the tracked
    # tree must stay the bound tree and nothing new may appear untracked except what an install or
    # a run is expected to leave (see `_INSTALL_OUTPUT_TOPS`).
    baseline = list_untracked(repo_dir)

    def _tree_intact() -> None:
        assert_lane_tree_bound(repo_dir, bound_tree)
        assert_no_new_untracked(repo_dir, baseline)

    body = _run_member_steps(
        lane_dir,
        repo_dir,
        venv_dir,
        venv_python,
        package=package,
        install=install,
        pytest_args=pytest_args,
        log_name=_OUTPUT_LOG_NAME,
        before_tests=_tree_intact,
        after_tests=_tree_intact,
    )
    receipt = {
        "kind": "review-run",
        # When this run happened, so close-gr can key the preserved evidence by
        # (gr commit, run time) and two closes of the same lane name do not overwrite
        # each other's receipt/log.
        "created": datetime.now(timezone.utc).isoformat(),
        "gr_commit": marker.get("gr_commit", ""),
        "bound_head": repo.get("bound_head", ""),
        "bound_head_tree": bound_tree,
        **body,
    }
    (lane_dir / _RECEIPT_NAME).write_text(json.dumps(receipt, indent=2) + "\n")
    return receipt


def _create_lane_venv(
    lane_dir: Path, python: str | None, system_site_packages: bool
) -> tuple[Path, Path]:
    """Create `<lane>/.venv`, so close-gr reclaims it. Returns (venv_dir, venv_python)."""
    interpreter = python or sys.executable
    venv_dir = lane_dir / _VENV_DIRNAME
    venv_cmd = [interpreter, "-m", "venv"]
    if system_site_packages:
        venv_cmd.append("--system-site-packages")
    venv_cmd.append(str(venv_dir))
    proc = subprocess.run(venv_cmd, text=True, capture_output=True)
    if proc.returncode != 0:
        raise ReviewRunRefused("venv_failed", f"venv create failed: {proc.stderr.strip()}")
    return venv_dir, venv_dir / "bin" / "python"


def _run_member_steps(
    lane_dir: Path,
    repo_dir: Path,
    venv_dir: Path,
    venv_python: Path,
    *,
    package: str | None,
    install: list[str] | None,
    pytest_args: list[str],
    log_name: str,
    before_tests: Callable[[], None] | None = None,
    after_tests: Callable[[], None] | None = None,
) -> dict:
    """Steps (3) to (7) for ONE repository: resolve install and package, install, prove the
    package imports from under the lane, prove pytest imports, run pytest in `repo_dir`, and
    return the per-run fields of the receipt. The output log lands at `<lane>/<log_name>`.
    The single-repo lane calls this with `repo_dir == lane_dir`; a multi-member lane calls it
    once per member with the shared venv."""
    # (3) resolve install + package, tracking WHERE each came from. An explicit
    #     --install/--package always wins; otherwise the repo's own .review-install
    #     hint supplies them, so a repo that declares itself (like grip, whose package
    #     is the gr2/ subdir) needs no hand-written flags. The hint's install template
    #     is SHELL-SPLIT FIRST, then {venv}/{lane} substituted per token, so a lane
    #     path containing a space stays one token even with an unquoted hint line
    #     (substituting before splitting would let the space break the token apart).
    install_source = "flag" if install is not None else None
    package_source = "flag" if package is not None else None
    hint = read_install_hint(repo_dir)
    if install is not None:
        # The --install FLAG supports the SAME {venv}/{lane} placeholders as the hint,
        # so the documented template works identically whether typed on the CLI or
        # declared in .review-install. review-run door 2: only the hint substituted, so
        # a reviewer who passed the documented `{venv} -m pip install -e {lane}` on the
        # flag got literal braces and a failed install.
        install = _apply_install_placeholders(install, venv_python, repo_dir)
    elif hint and hint.get("install"):
        install = _apply_install_placeholders(shlex.split(hint["install"]), venv_python, repo_dir)
        install_source = "hint"
    if package is None and hint and hint.get("package"):
        package = hint["package"]
        package_source = "hint"
    if package is None:
        raise ReviewRunRefused(
            "no_package",
            "no --package given and the lane's .review-install declares none; a "
            "package name is required so the install can be proven to resolve under "
            "the lane",
        )

    # (4) install the reconstructed tree editable. A hint (or flag) can name a binary
    #     that does not exist; subprocess.run then raises OSError, which must become a
    #     refusal, never an uncaught traceback (tree content chooses the command, so a
    #     typo in a repo's own hint must not produce the one shape review run promises
    #     never to give).
    if install is not None:
        install_cmd = install
    else:
        install_cmd = [str(venv_python), "-m", "pip", "install", "-e", str(repo_dir)]
        install_source = "default"
    try:
        proc = subprocess.run(install_cmd, text=True, capture_output=True, cwd=str(repo_dir))
    except OSError as exc:
        raise ReviewRunRefused(
            "install_failed",
            f"install `{' '.join(install_cmd)}` could not run: {exc}",
        )
    if proc.returncode != 0:
        raise ReviewRunRefused(
            "install_failed",
            f"install `{' '.join(install_cmd)}` failed: {proc.stderr.strip()[-800:]}",
        )

    # (4a) An undeclared extra does NOT fail the install — pip warns and exits 0,
    #      installing none of that extra's dependencies. Named here, BEFORE the import
    #      and pytest checks, so a typo'd extra surfaces as its own root cause instead
    #      of the misleading `pytest_not_installed` symptom it would otherwise produce.
    undeclared = detect_undeclared_extras(proc.stdout + "\n" + proc.stderr)
    if undeclared:
        raise ReviewRunRefused(
            "undeclared_extra",
            "the install requested extra(s) the package does not declare: "
            f"{', '.join(undeclared)}. pip exits 0 on an undeclared extra and installs "
            "nothing for it, so the test dependencies it was meant to bring (pytest and "
            "the rest) are silently absent. Fix the extra name in --install or the "
            f"repo's .review-install. install: `{' '.join(install_cmd)}`",
        )

    # (5) IMPORT UNDER THE LANE — in the same env pytest will use. run_env scrubs every
    #     PYTHON* variable, so a rogue package on PYTHONPATH can neither shadow the CHECK
    #     (already isolated by -I) nor be imported by the RUN, which runs without -I. The
    #     SAME env object flows to the import check, the pytest-import check, and pytest
    #     below, so "resolves under the lane" is a fact about the run and not just the
    #     check (the review-run env-isolation fix: -I isolates the check, not the run;
    #     the check and the run must see one environment).
    run_env = scrubbed_python_env(venv_dir=venv_dir)
    resolved_file = resolve_import_file(venv_python, package, run_env)
    assert_import_under_lane(resolved_file, lane_dir)

    # (6) pytest must be importable in the lane venv. A plain editable install does
    #     not bring it (pytest is a test-time extra), and running pytest anyway
    #     yields exit 1 with no summary — which the summary check would mislabel
    #     `unparseable_summary`. Name the real cause instead, and keep it a refusal.
    # `-I` for the same reason as resolve_import_file: a lane whose root holds a
    # `pytest/` directory would otherwise import that namespace dir from cwd and
    # pass this check while real pytest is absent. Resolve against the install only.
    proc = subprocess.run(
        [str(venv_python), "-I", "-c", "import pytest"], text=True, capture_output=True, env=run_env
    )
    if proc.returncode != 0:
        raise ReviewRunRefused(
            "pytest_not_installed",
            "pytest is not importable in the lane venv; a plain editable install does "
            "not bring it. Add pytest to --install or the repo's .review-install "
            f"(it is a test-time dependency). stderr: {proc.stderr.strip()[-300:]}",
        )

    # (7) run pytest; counts from the summary line, never the exit code. The full
    #     output is persisted to a log in the lane and the failed node ids are parsed
    #     from it, so a red receipt says WHICH tests failed and the raw text survives
    #     for close-gr to carry out (review-run door 1).
    # Guarantee the short test summary lists every FAILED/ERROR node id for
    # parse_failed_ids. pytest emits those summary lines only under `-r`, and `-r` is
    # LAST-WINS: a prepended `-rfE` would be silently overridden by a caller `-rN`/`-rs`,
    # leaving failed_ids empty on a real red run. merge_report_flags folds f/E INTO the
    # caller's own -r chars, so f and E survive whatever the caller passed.
    test_cmd = [str(venv_python), "-m", "pytest", *merge_report_flags(pytest_args)]
    # The integrity checks ran before the venv, so the install step above (the repo's own code) has
    # had its chance to change what was checked. Re-assert, immediately before the tests run.
    if before_tests is not None:
        before_tests()
    proc = subprocess.run(
        test_cmd, text=True, capture_output=True, cwd=str(repo_dir), env=run_env
    )
    pytest_output = proc.stdout + "\n" + proc.stderr
    (lane_dir / log_name).write_text(pytest_output)
    # And once the tests have run: a result is about the bound head only if the tracked tree is
    # still the bound tree. The log is already written, so a refusal here keeps the evidence.
    if after_tests is not None:
        after_tests()
    failed_ids = parse_failed_ids(pytest_output)
    summary = parse_pytest_summary(pytest_output)
    if summary is None:
        raise ReviewRunRefused(
            "unparseable_summary",
            "no pytest summary line found; refusing to call this a green "
            f"(pytest exit was {proc.returncode})",
        )
    if not summary.get("selected"):
        raise ReviewRunRefused(
            "zero_collected",
            f"pytest selected 0 tests (collected={summary.get('collected')}, "
            f"deselected={summary.get('deselected')}); a zero-test run is not a green",
        )

    version = subprocess.run(
        [str(venv_python), "--version"], text=True, capture_output=True
    ).stdout.strip() or subprocess.run(
        [str(venv_python), "-V"], text=True, capture_output=True
    ).stderr.strip()

    # A green requires at least one PASS: an all-skipped or all-deselected run has no
    # failure but proves nothing, so it is not a green.
    result = (
        "green"
        if (summary["passed"] >= 1 and summary["failed"] == 0 and summary["errors"] == 0)
        else "red"
    )
    return {
        "interpreter": {"path": str(venv_python), "version": version},
        "resolved_install_path": resolved_file,
        "install_command": install_cmd,
        "install_source": install_source,
        "package_source": package_source,
        "test_command": test_cmd,
        "collected": summary["collected"],
        "deselected": summary["deselected"],
        "selected": summary["selected"],
        "passed": summary["passed"],
        "failed": summary["failed"],
        "skipped": summary["skipped"],
        "xfailed": summary["xfailed"],
        "errors": summary["errors"],
        # WHICH tests failed (node ids parsed from the summary), so a red receipt is
        # actionable and not just a count, and the raw output log this run wrote.
        "failed_ids": failed_ids,
        "output_log": log_name,
        "result": result,
    }


# ---- a lane that binds more than one repository ---------------------------------

def _member_dir(lane_dir: Path, key: str) -> Path:
    """Where a member lives in a multi-member lane: `<lane>/<key>`, the row key `open` used."""
    if not key or key in (".", "..") or "/" in key or "\\" in key:
        raise ReviewRunRefused(
            "bad_marker",
            f"the lane marker names a member key {key!r} that is not a plain directory name",
        )
    return lane_dir / key


def _resolve_member_order(keys: list[str], order: list[str] | None) -> list[str]:
    """The order members install and run in: the marker's row order (sorted by key at bind)
    unless `--order` names one. A member installs after the ones it depends on, which the run
    cannot infer, so an explicit order must name every member exactly once."""
    if len(set(keys)) != len(keys):
        raise ReviewRunRefused("bad_marker", f"the lane marker lists a member key twice: {keys}")
    if order is None:
        return list(keys)
    order = list(order)
    if sorted(order) != sorted(keys):
        raise ReviewRunRefused(
            "bad_order",
            f"--order must name every member exactly once; this lane binds {keys}, got {order}",
        )
    return order


_REQUIREMENT_NAME_RE = re.compile(r"^\s*([A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)")


def _normalized_dist_name(name: str) -> str:
    """PEP 503 normalisation, so `Demo_Core`, `demo.core` and `demo-core` are one distribution."""
    return re.sub(r"[-_.]+", "-", name).lower()


def _member_distribution(mdir: Path) -> tuple[str | None, list[str]]:
    """(the member's own distribution name, the distribution names it requires) read from its
    pyproject.toml `[project]`, or (None, []) when the member has no readable pyproject: a
    member that installs some other way declares nothing here, so it is placed by the marker
    order alone, exactly as before this derivation existed."""
    path = mdir / "pyproject.toml"
    try:
        project = tomllib.loads(path.read_text(encoding="utf-8")).get("project", {})
    except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError):
        return None, []
    if not isinstance(project, dict):
        # Valid TOML that is not a [project] table (`project = 5`, `[[project]]`) declares nothing,
        # the same as an unreadable file.
        return None, []
    name = project.get("name")
    declared = project.get("dependencies", [])
    required: list[str] = []
    for spec in declared if isinstance(declared, list) else []:
        match = _REQUIREMENT_NAME_RE.match(spec) if isinstance(spec, str) else None
        if match:
            required.append(_normalized_dist_name(match.group(1)))
    return (_normalized_dist_name(name) if isinstance(name, str) and name else None), required


def _derive_member_order(lane_dir: Path, keys: list[str], plugins: dict | None = None) -> list[str]:
    """Install order from what each member DECLARES, read by the ecosystem plugins: a member installs after
    every other lane member that one of its units needs. The built-in Python plugin reads each member's
    `[project].dependencies` (what uv does for a workspace), and every `grip-ecosystem-<name>` executable on the
    USER's PATH may claim members of other ecosystems; a plugin never comes from a member, so the change under
    review cannot choose its own order. `--order` stays the explicit override and bypasses this.

    Stable: among members that are ready, the one earliest in the marker order goes first, so a lane with no
    declared dependencies between its members keeps the marker order it always had. A cycle is refused by name
    (a group installed in one invocation is a later step), two members claiming one unit are refused too, and a
    plugin that fails refuses the lane rather than falling back to the marker order."""
    table = plugins if plugins is not None else lane_plugins.plugin_table(os.environ.get("PATH", ""))
    try:
        plan = lane_graph.plan_lane(lane_dir, list(keys), table)
    except lane_graph.LaneRefused as refusal:
        raise ReviewRunRefused(refusal.code, refusal.detail) from refusal
    member_of = {u.id: u.member for u in plan.units}
    cyclic = plan.cyclic_groups
    if cyclic:
        # The first elementary loop of the first cyclic group, in member names: the loop and nothing outside it.
        loop = plan.loops[cyclic[0].index][0][0]
        walk = [member_of[u] for u in loop]
        raise ReviewRunRefused(
            "dependency_cycle",
            "the lane's members depend on each other in a cycle, so no install order exists: "
            + " -> ".join(walk + [walk[0]])
            + "; pass --order to choose one",
        )
    placed: list[str] = []
    for group in plan.groups:
        for unit in group.units:
            if member_of[unit] not in placed:
                placed.append(member_of[unit])
    return placed


def _run_multi_member_lane(
    lane_dir: Path,
    marker: dict,
    repos: list[dict],
    *,
    package: str | None,
    pytest_args: list[str],
    python: str | None,
    install: list[str] | None,
    system_site_packages: bool,
    order: list[str] | None,
) -> dict:
    """Run every member of a multi-member lane in ONE shared venv, in a stated order.

    Integrity first: every member's tree and untracked-drift check, and its declared runner,
    run BEFORE the venv exists, so a lane that already fails integrity installs and runs
    nothing. Then, per member in order, the single-repo steps against `<lane>/<key>`; the
    import-under-lane check is against the LANE ROOT, so a member whose package resolves
    outside the lane refuses, naming that member.

    A refusal stops the run (the environment or tree is not what the review claims, so a
    later member would be a result about something else); a red does not (every member's
    failures are information). The lane result is refused > red > green, and the members that
    never ran are named in `not_run`, so a stop never reads as a green."""
    keys = [str(r.get("key", "")) for r in repos]
    ran_order = _resolve_member_order(keys, order)
    # How the order was chosen, said only once it is true: None until the derivation runs, so a refusal
    # that happens BEFORE it (an integrity check) does not claim a derived order next to the marker order.
    order_source = "explicit" if order is not None else None
    by_key = {str(r.get("key", "")): r for r in repos}
    members: list[dict] = []
    done: list[str] = []
    current: str | None = None
    try:
        if package is not None or install is not None:
            raise ReviewRunRefused(
                "member_flags_ambiguous",
                "--package and --install name one package for one repo, and this lane binds "
                f"{len(repos)}; declare `package` and `install` in each member's own "
                ".review-install instead",
            )
        for key in ran_order:
            current = key
            mdir = _member_dir(lane_dir, key)
            if not mdir.is_dir():
                raise ReviewRunRefused(
                    "member_missing", f"member {key!r} has no directory at {mdir}"
                )
            assert_lane_tree_bound(mdir, by_key[key].get("bound_head_tree", ""))
            assert_no_untracked_drift(mdir)
            hint = read_install_hint(mdir) or {}
            if hint.get("runner") not in (None, "pytest"):
                raise ReviewRunRefused(
                    "member_runner_unsupported",
                    f"member {key!r} declares runner {hint['runner']!r}; a multi-member lane "
                    "runs pytest members only in this version",
                )
        current = None
        # DERIVED ONLY NOW, after every member's tree passed its integrity check: the order is read from
        # each member's pyproject, and a pyproject that was tampered with must refuse as a tamper (above),
        # not steer the install order. `--order` bypasses this entirely (the explicit override).
        if order is None:
            # Both assigned only once the derivation RETURNS: a cycle or a name clash refuses from inside it,
            # and that receipt must not label the untouched marker order as derived.
            ran_order = _derive_member_order(lane_dir, ran_order)
            order_source = "dependencies"
        # Taken after the checks above and before the venv and any install: everything in it already
        # passed the drift check, so what is new later was written by an install or by tests.
        baselines = {member: list_untracked(_member_dir(lane_dir, member)) for member in ran_order}
        venv_dir, venv_python = _create_lane_venv(lane_dir, python, system_site_packages)

        def _lane_intact() -> None:
            # ANY member's install or an earlier member's tests may have rewritten ANY member's
            # tracked source or planted a file in it, so every member is re-checked each time.
            for member in ran_order:
                mdir = _member_dir(lane_dir, member)
                try:
                    assert_lane_tree_bound(mdir, by_key[member].get("bound_head_tree", ""))
                    assert_no_new_untracked(mdir, baselines[member])
                except ReviewRunRefused as refusal:
                    refusal.member = member
                    raise

        for key in ran_order:
            current = key
            body = _run_member_steps(
                lane_dir,
                _member_dir(lane_dir, key),
                venv_dir,
                venv_python,
                package=None,
                install=None,
                pytest_args=pytest_args,
                log_name=f"{key}{_OUTPUT_LOG_NAME}",
                before_tests=_lane_intact,
                after_tests=_lane_intact,
            )
            members.append({
                "key": key,
                "bound_head": by_key[key].get("bound_head", ""),
                "bound_head_tree": by_key[key].get("bound_head_tree", ""),
                **body,
            })
            done.append(key)
        current = None
    except ReviewRunRefused as exc:
        if exc.member is None:
            exc.member = current
        exc.order = ran_order
        exc.order_source = order_source
        exc.completed = members
        exc.not_run = [k for k in ran_order if k not in done and k != exc.member]
        raise

    result = "green" if all(m["result"] == "green" for m in members) else "red"
    receipt = {
        "kind": "review-run",
        "created": datetime.now(timezone.utc).isoformat(),
        "gr_commit": marker.get("gr_commit", ""),
        "order": ran_order,
        "order_source": order_source,
        "members": members,
        "not_run": [],
        "selected": sum(m["selected"] for m in members),
        "passed": sum(m["passed"] for m in members),
        "failed": sum(m["failed"] for m in members),
        "errors": sum(m["errors"] for m in members),
        "result": result,
    }
    (lane_dir / _RECEIPT_NAME).write_text(json.dumps(receipt, indent=2) + "\n")
    return receipt


# ---- the runner contract: `review run --test "<cmd>"` for any runner -----------
#
# The pytest path above is venv + pip + import-under-lane + pytest, and stays exactly
# as it is. This path is for a NON-Python runner (cargo, jest, …): it keeps the two
# language-agnostic trust checks — the tracked-tree comparison and the untracked-drift
# refusal, so a green is still about the bound tree — then runs the declared test
# command in the lane and takes the counts from THAT runner's own summary line (never
# the exit code). A summary the parser cannot read is a refusal with the raw tail, not
# a zero-green. No venv, no install, no import check: those are Python-specific and a
# cargo/jest tree neither has nor needs them.

def run_test_command_in_lane(
    lane_dir: Path,
    *,
    runner: str,
    test_command: list[str],
    reports: str | None = None,
) -> dict:
    """Run a non-Python runner's test command in a bound lane and build a receipt.
    ``runner`` selects the summary parser (review_runners.RUNNERS). Raises
    ReviewRunRefused for a structural problem (no marker, tree drift, unknown runner,
    unparseable summary, zero tests)."""
    from .review_runners import (
        VALID_RUNNERS,
        JUNIT_XML_DEFAULT_REPORTS,
        RUNNER_CREATED_PATHS,
        RunnerSummaryRefusal,
        snapshot_junit_xml_reports,
        summarize_runner,
    )

    lane_dir = Path(lane_dir).resolve()
    try:
        if runner not in VALID_RUNNERS:
            raise ReviewRunRefused(
                "unknown_runner",
                f"runner {runner!r} has no summary parser; known runners: {sorted(VALID_RUNNERS)}",
            )
        marker = _read_marker(lane_dir)
        repos = marker.get("repos", [])
        if not repos:
            raise ReviewRunRefused("no_members", f"the lane marker at {lane_dir} binds no repos")
        if len(repos) > 1:
            raise ReviewRunRefused(
                "member_runner_unsupported",
                f"a multi-member lane runs pytest members only in this version; marker binds "
                f"{len(repos)} repos and a non-pytest runner has no per-member form yet",
            )
        repo = repos[0]
        repo_dir = lane_dir

        # The two language-agnostic trust checks (same as the pytest path). The drift
        # check exempts this runner's OWN created outputs (cargo: target/, Cargo.lock;
        # jest: node_modules/, coverage/) so a second run on an un-gitignored lane does
        # not refuse the first run's artifacts — the analogue of the pytest .venv/receipt
        # exemptions.
        created = RUNNER_CREATED_PATHS.get(
            runner, {"names": frozenset(), "tops": (), "segments": frozenset()}
        )
        assert_lane_tree_bound(repo_dir, repo.get("bound_head_tree", ""))
        assert_no_untracked_drift(
            repo_dir,
            extra_allow_names=created["names"],
            extra_allow_tops=created["tops"],
            extra_allow_segments=created.get("segments", frozenset()),
        )

        report_pattern = reports or JUNIT_XML_DEFAULT_REPORTS if runner == "junit-xml" else None
        try:
            report_snapshot = (
                snapshot_junit_xml_reports(repo_dir, report_pattern) if report_pattern is not None else None
            )
        except RunnerSummaryRefusal as exc:
            raise ReviewRunRefused(exc.code, exc.detail) from exc
        try:
            proc = subprocess.run(
                test_command, text=True, capture_output=True, cwd=str(repo_dir)
            )
        except OSError as exc:
            raise ReviewRunRefused(
                "test_command_failed",
                f"test command `{' '.join(test_command)}` could not run: {exc}",
            )
        output = proc.stdout + "\n" + proc.stderr
        (lane_dir / _OUTPUT_LOG_NAME).write_text(output)

        try:
            summary, report_pattern, report_files, stale_reports = summarize_runner(
                runner, output, repo_dir, reports, report_snapshot
            )
        except RunnerSummaryRefusal as exc:
            raise ReviewRunRefused(exc.code, exc.detail) from exc
        if summary is None:
            tail = "\n".join(output.strip().splitlines()[-15:])
            raise ReviewRunRefused(
                "unparseable_summary",
                f"no {runner} summary line found; refusing to call this a green "
                f"(exit was {proc.returncode}). raw tail:\n{tail}",
            )
        if not summary.get("selected"):
            raise ReviewRunRefused(
                "zero_collected",
                f"{runner} ran 0 tests (selected={summary.get('selected')}); "
                "a zero-test run is not a green",
            )

        result = (
            "green"
            if (summary["passed"] >= 1 and summary["failed"] == 0 and summary["errors"] == 0)
            else "red"
        )
        receipt = {
            "kind": "review-run",
            "created": datetime.now(timezone.utc).isoformat(),
            "gr_commit": marker.get("gr_commit", ""),
            "bound_head": repo.get("bound_head", ""),
            "bound_head_tree": repo.get("bound_head_tree", ""),
            "runner": runner,
            "test_command": test_command,
            "selected": summary["selected"],
            "passed": summary["passed"],
            "failed": summary["failed"],
            "skipped": summary["skipped"],
            "errors": summary["errors"],
            "output_log": _OUTPUT_LOG_NAME,
            "result": result,
        }
        if report_pattern is not None:
            receipt["reports"] = report_pattern
            receipt["report_files"] = [str(path.relative_to(repo_dir)) for path in report_files]
            receipt["stale_reports_ignored"] = stale_reports
        (lane_dir / _RECEIPT_NAME).write_text(json.dumps(receipt, indent=2) + "\n")
        return receipt
    except ReviewRunRefused as exc:
        if exc.code not in _NOT_A_LANE_CODES:
            _write_refusal_receipt(lane_dir, exc)
        raise
