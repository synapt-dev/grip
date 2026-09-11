"""`review run <lane-dir>`: the review-owned in-lane test run — the last raw-shell
exit point (venv + install + pytest by hand) folded into one verb.

It runs ONLY inside an `open-gr --enter` reconstruction lane (it reads the
`.grip-open-gr-reconstruct.json` marker), so a green is always about a bound tree.
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
from datetime import datetime, timezone
from pathlib import Path

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


_HINT_KEYS = frozenset({"install", "package", "runner", "test"})


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
    """Parse `<repo_dir>/.review-install`; return {'install': str, 'package': str}
    (both optional keys) or None when the file is absent. An unrecognised key is a
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

_MARKER_NAME = ".grip-open-gr-reconstruct.json"
_RECEIPT_NAME = ".grip-review-run.json"
# The full pytest output, persisted beside the receipt. The receipt's counts and
# `failed_ids` say WHAT failed; this file is the raw text a reviewer reads to see
# WHY. Named so `close-gr` can carry it (and the receipt) out before it reclaims
# the lane (review-run door 1).
_OUTPUT_LOG_NAME = ".grip-review-run.log"
_VENV_DIRNAME = ".venv"


class ReviewRunRefused(Exception):
    """A structural refusal: the run cannot yield a trustworthy green."""

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
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
_UNTRACKED_ALLOW_NAMES = frozenset({_MARKER_NAME, _RECEIPT_NAME, _OUTPUT_LOG_NAME})
_UNTRACKED_ALLOW_TOP = (_VENV_DIRNAME + "/",)
_UNTRACKED_ALLOW_SEGMENTS = frozenset({"__pycache__", ".pytest_cache", ".mypy_cache"})


def _is_allowlisted_untracked(
    rel_path: str,
    extra_names: frozenset[str] = frozenset(),
    extra_tops: tuple[str, ...] = (),
) -> bool:
    if rel_path in _UNTRACKED_ALLOW_NAMES or rel_path in extra_names:
        return True
    tops = _UNTRACKED_ALLOW_TOP + tuple(extra_tops)
    if any(rel_path == pre.rstrip("/") or rel_path.startswith(pre) for pre in tops):
        return True
    segments = rel_path.strip("/").split("/")
    if any(seg in _UNTRACKED_ALLOW_SEGMENTS or seg.endswith(".egg-info") for seg in segments):
        return True
    return False


def assert_no_untracked_drift(
    repo_dir: Path,
    *,
    extra_allow_names: frozenset[str] = frozenset(),
    extra_allow_tops: tuple[str, ...] = (),
) -> None:
    """Refuse if the lane holds any untracked path the run did not create. Without
    this an untracked `conftest.py` (or shadow module) that patches the package turns
    a failing tree green while the tracked-tree comparison passes. The complement of
    `assert_lane_tree_bound`: dropping either reds only its own drift witness.

    ``extra_allow_names``/``extra_allow_tops`` are a runner's OWN created outputs (a
    cargo run writes `target/` and `Cargo.lock`, a jest run `node_modules/`/`coverage/`),
    the language-specific analogue of the pytest path's `.venv`/receipt exemptions — so a
    second `review run` on an un-gitignored lane does not refuse the first run's outputs as
    drift."""
    out = _git(repo_dir, "status", "--porcelain")
    offending = []
    for line in out.splitlines():
        if line.startswith("?? "):
            rel = line[3:].strip().strip('"')
            if not _is_allowlisted_untracked(rel, extra_allow_names, extra_allow_tops):
                offending.append(rel)
    if offending:
        raise ReviewRunRefused(
            "untracked_drift",
            f"untracked path(s) in the lane the run did not create: "
            f"{', '.join(offending[:5])}; an injected conftest/module can change test "
            "behavior without touching the tracked tree",
        )


# ---- import resolves under the lane -----------------------------------------

def scrubbed_python_env(base: dict | None = None) -> dict:
    """Return a copy of the environment with every ``PYTHON*`` variable removed.

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
    the cwd shadow (the scrub does not touch cwd; ``-I`` does not touch the run)."""
    env = dict(os.environ if base is None else base)
    for key in [k for k in env if k.startswith("PYTHON")]:
        del env[key]
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
    marker_path = lane_dir / _MARKER_NAME
    if not marker_path.exists():
        raise ReviewRunRefused(
            "no_marker",
            f"no open-gr marker at {marker_path}; `review run` only runs inside a "
            "lane opened by `review open-gr --enter`",
        )
    marker = json.loads(marker_path.read_text())
    if marker.get("kind") != "open-gr-reconstruct":
        raise ReviewRunRefused(
            "not_open_gr", f"{marker_path} is not an open-gr reconstruction marker"
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
) -> dict:
    """Create `<lane>/.venv`, install the reconstructed tree, and run pytest — but
    only after the lane's tree is proven to equal the bound head-tree and the import
    is proven to resolve under the lane. Returns a receipt. Raises ReviewRunRefused
    for any structural problem (no marker, tree drift, import escape, zero collected,
    unparseable summary)."""
    lane_dir = Path(lane_dir).resolve()
    marker = _read_marker(lane_dir)
    repos = marker.get("repos", [])
    if len(repos) != 1:
        raise ReviewRunRefused(
            "multi_repo_lane",
            f"v1 review run handles a single-repo lane; marker binds {len(repos)} "
            "repos (multi-repo is a follow-on)",
        )
    repo = repos[0]
    bound_tree = repo.get("bound_head_tree", "")
    repo_dir = lane_dir  # single-repo lane: the clone IS the lane

    # (1) THE TREE COMPARISON — before the venv exists, so it never pollutes the hash.
    #     Two halves: tracked content equals the bound tree, AND no untracked path the
    #     run did not create (an injected conftest changes behavior invisibly to the
    #     tracked-tree hash).
    assert_lane_tree_bound(repo_dir, bound_tree)
    assert_no_untracked_drift(repo_dir)

    # (2) venv in the lane, so close-gr reclaims it.
    interpreter = python or sys.executable
    venv_dir = lane_dir / _VENV_DIRNAME
    venv_cmd = [interpreter, "-m", "venv"]
    if system_site_packages:
        venv_cmd.append("--system-site-packages")
    venv_cmd.append(str(venv_dir))
    proc = subprocess.run(venv_cmd, text=True, capture_output=True)
    if proc.returncode != 0:
        raise ReviewRunRefused("venv_failed", f"venv create failed: {proc.stderr.strip()}")
    venv_python = venv_dir / "bin" / "python"

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
    run_env = scrubbed_python_env()
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
    proc = subprocess.run(
        test_cmd, text=True, capture_output=True, cwd=str(repo_dir), env=run_env
    )
    pytest_output = proc.stdout + "\n" + proc.stderr
    (lane_dir / _OUTPUT_LOG_NAME).write_text(pytest_output)
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
    receipt = {
        "kind": "review-run",
        # When this run happened, so close-gr can key the preserved evidence by
        # (gr commit, run time) and two closes of the same lane name do not overwrite
        # each other's receipt/log.
        "created": datetime.now(timezone.utc).isoformat(),
        "gr_commit": marker.get("gr_commit", ""),
        "bound_head": repo.get("bound_head", ""),
        "bound_head_tree": bound_tree,
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
        "output_log": _OUTPUT_LOG_NAME,
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
) -> dict:
    """Run a non-Python runner's test command in a bound lane and build a receipt.
    ``runner`` selects the summary parser (review_runners.RUNNERS). Raises
    ReviewRunRefused for a structural problem (no marker, tree drift, unknown runner,
    unparseable summary, zero tests)."""
    from .review_runners import RUNNERS, RUNNER_CREATED_PATHS, parse_runner_summary

    lane_dir = Path(lane_dir).resolve()
    try:
        if runner not in RUNNERS:
            raise ReviewRunRefused(
                "unknown_runner",
                f"runner {runner!r} has no summary parser; known runners: "
                f"{sorted(RUNNERS)}",
            )
        marker = _read_marker(lane_dir)
        repos = marker.get("repos", [])
        if len(repos) != 1:
            raise ReviewRunRefused(
                "multi_repo_lane",
                f"v1 review run handles a single-repo lane; marker binds {len(repos)} repos",
            )
        repo = repos[0]
        repo_dir = lane_dir

        # The two language-agnostic trust checks (same as the pytest path). The drift
        # check exempts this runner's OWN created outputs (cargo: target/, Cargo.lock;
        # jest: node_modules/, coverage/) so a second run on an un-gitignored lane does
        # not refuse the first run's artifacts — the analogue of the pytest .venv/receipt
        # exemptions.
        created = RUNNER_CREATED_PATHS.get(runner, {"names": frozenset(), "tops": ()})
        assert_lane_tree_bound(repo_dir, repo.get("bound_head_tree", ""))
        assert_no_untracked_drift(
            repo_dir,
            extra_allow_names=created["names"],
            extra_allow_tops=created["tops"],
        )

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

        summary = parse_runner_summary(runner, output)
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
        (lane_dir / _RECEIPT_NAME).write_text(json.dumps(receipt, indent=2) + "\n")
        return receipt
    except ReviewRunRefused as exc:
        if exc.code not in _NOT_A_LANE_CODES:
            _write_refusal_receipt(lane_dir, exc)
        raise
