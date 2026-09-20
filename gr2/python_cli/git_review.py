"""`git review` — the single-repo review entry point.

Installed as the console script ``git-review`` so it resolves through git's own
``git-<name>`` custom-subcommand convention: run ``git review …`` inside any git
repository and git execs this. It is single-repo by design — no workspace, no
manifest, no remote host — and keeps its state in a directory under the repo's own
git directory:

    <git-dir>/grip/review.json   {"repo": <name>, "base": <sha>, "head": <sha>}

The store root is the resolved git directory (``git rev-parse --absolute-git-dir``),
so a repo whose ``.git`` is a FILE — a separate git dir, a linked worktree, a
submodule — stores correctly instead of tracebacking on a ``.git/grip`` path.

``open`` / ``status`` / ``run`` / ``close`` of the ruled bind / open / run / close
surface. ``base`` defaults to the merge-base of HEAD and the repo's default branch
(the remote-tracking ref ``origin/HEAD`` points at, else local main/master); when no
diverging branch is found the base falls back to HEAD and the command says so on one
line.

``run`` is the single-repo counterpart of ``gr2 review run``: it runs the reviewed
repo's own tests in the clone and writes a receipt, and a green is trustworthy for the
same two reasons it is there. The tracked tree must still equal the tree ``open``
bound (a clone edited after ``open`` is not the code under review), no untracked path
the run did not create may be present (an injected ``conftest.py`` changes behavior
without touching the tracked tree), and the counts come from the runner's own summary
line, never its exit code. It reuses ``review_run``'s checks, parsers and
``.review-install`` contract unchanged; what differs is that the clone IS the tree, so
there is no lane, no marker and no reconstruction -- and the venv and receipt live
under ``<git-dir>/grip/`` rather than in the working tree, so the reviewed repo is
left exactly as it was found.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from .clone_exec import IncompleteRemoval, rmtree_or_refuse

STORE_DIRNAME = "grip"
RECORD_FILENAME = "review.json"
# The run's artifacts live beside the review record, INSIDE the git directory: a
# reviewer's clone must look untouched afterwards, and anything written into the
# working tree would also read as untracked drift on the next run.
RECEIPT_FILENAME = "run.json"
OUTPUT_LOG_FILENAME = "run.log"
VENV_DIRNAME = "venv"


class GitReviewError(RuntimeError):
    """A single-repo review operation could not proceed."""


def _git(repo: Path, *args: str) -> str:
    r = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
    )
    if r.returncode != 0:
        raise GitReviewError(f"git {' '.join(args)} failed: {r.stderr.strip()}")
    return r.stdout.strip()


def _repo_root(start: Path) -> Path:
    """The top of the git working tree containing ``start`` (refuses outside a repo)."""
    try:
        top = _git(start, "rev-parse", "--show-toplevel")
    except GitReviewError as exc:
        raise GitReviewError(f"not inside a git repository: {start}") from exc
    return Path(top)


def _git_dir(repo_root: Path) -> Path:
    """The resolved git directory, absolute — the real store root even when ``.git``
    is a file (separate git dir, linked worktree, submodule)."""
    return Path(_git(repo_root, "rev-parse", "--absolute-git-dir"))


def _store_dir(repo_root: Path) -> Path:
    return _git_dir(repo_root) / STORE_DIRNAME


def _record_path(repo_root: Path) -> Path:
    return _store_dir(repo_root) / RECORD_FILENAME


def _default_branch_ref(repo_root: Path) -> str | None:
    """The ref to measure divergence against, or None when there is no diverging
    branch to find. Prefers the REMOTE-TRACKING ref that ``origin/HEAD`` points at
    (``refs/remotes/origin/<name>``) — so a clone whose local default branch was
    deleted, or a single-branch clone, still resolves — then a local main/master."""
    try:
        # symbolic-ref resolves refs/remotes/origin/HEAD -> refs/remotes/origin/<name>
        target = _git(repo_root, "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD")
        if target:
            return target  # a full remote-tracking ref; merge-base against it directly
    except GitReviewError:
        pass
    for name in ("main", "master"):
        try:
            _git(repo_root, "rev-parse", "--verify", "--quiet", f"refs/heads/{name}")
            return f"refs/heads/{name}"
        except GitReviewError:
            continue
    return None


def _resolve_base(repo_root: Path, base_arg: str | None) -> tuple[str, bool]:
    """Return (base_sha, fell_back). ``fell_back`` is True when no diverging branch was
    found and the base is HEAD itself (an explicit base_arg never falls back)."""
    head = _git(repo_root, "rev-parse", "HEAD")
    if base_arg:
        return _git(repo_root, "rev-parse", base_arg), False
    ref = _default_branch_ref(repo_root)
    if ref is None:
        return head, True
    try:
        base = _git(repo_root, "merge-base", "HEAD", ref)
    except GitReviewError:
        return head, True
    return base, base == head


def open_review(repo_root: Path, base_arg: str | None = None) -> dict[str, str]:
    head = _git(repo_root, "rev-parse", "HEAD")
    base, _fell_back = _resolve_base(repo_root, base_arg)
    # The head TREE, not only the head commit: `run` compares the clone's current
    # tracked content against it, and a commit sha cannot answer "has this working
    # tree been edited since open" (an edited tracked file leaves HEAD untouched).
    head_tree = _git(repo_root, "rev-parse", "HEAD^{tree}")
    record = {"repo": repo_root.name, "base": base, "head": head, "head_tree": head_tree}
    store = _store_dir(repo_root)
    store.mkdir(parents=True, exist_ok=True)
    _record_path(repo_root).write_text(json.dumps(record, indent=2) + "\n")
    return record


def _receipt_path(repo_root: Path) -> Path:
    return _store_dir(repo_root) / RECEIPT_FILENAME


def _output_log_path(repo_root: Path) -> Path:
    return _store_dir(repo_root) / OUTPUT_LOG_FILENAME


def read_run_receipt(repo_root: Path) -> dict | None:
    """The last `git review run` receipt, or None when the repo has never run one."""
    p = _receipt_path(repo_root)
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return None


def read_review(repo_root: Path) -> dict[str, str] | None:
    p = _record_path(repo_root)
    if not p.is_file():
        return None
    return json.loads(p.read_text())


def close_review(repo_root: Path) -> bool:
    """Remove the review record and everything `run` left behind; returns True if a
    review record was present. The venv and the run artifacts go too: a closed review
    that leaves a stale venv behind would have the NEXT review's first run install
    into it, and a receipt outliving its record describes a review nobody can read."""
    p = _record_path(repo_root)
    present = p.is_file()
    if present:
        p.unlink()
    for leftover in (_receipt_path(repo_root), _output_log_path(repo_root)):
        try:
            leftover.unlink()
        except OSError:
            pass
    venv = _store_dir(repo_root) / VENV_DIRNAME
    if venv.is_dir():
        # Through the shared helper, never raw `rmtree(ignore_errors=True)`: it drops
        # a locked or permission-denied entry silently, so "closed" would be reported
        # over a venv still sitting in the git directory, and the NEXT review's first
        # run would install into it. The repo closes that class repo-wide and a guard
        # test enforces it.
        try:
            rmtree_or_refuse(venv)
        except IncompleteRemoval as exc:
            raise GitReviewError(
                f"the review record is gone but its venv could not be fully removed: "
                f"{exc}. Remove {venv} by hand before opening the next review."
            ) from exc
    try:
        _store_dir(repo_root).rmdir()  # leave no trace when empty
    except OSError:
        pass
    return present



# ---- `git review run`: the single-repo in-clone test run ---------------------
#
# Everything that makes a `gr2 review run` green trustworthy is reused verbatim from
# `review_run` -- the tracked-tree comparison, the untracked-drift refusal, the
# `.review-install` contract, the import-under-the-tree check, the summary parsers and
# the refusal vocabulary. Only the orchestration differs, and only in the three ways a
# single clone differs from a reconstruction lane: the tree to bind against comes from
# the review record rather than an open-gr marker, the clone IS the tree so there is
# nothing to reconstruct, and the venv and receipt live under `<git-dir>/grip/` so the
# reviewer's working tree is left exactly as it was found.


def _bound_head_tree(record: dict) -> str:
    """The tree `open` bound, or a refusal naming the fix. A record written before
    `open` recorded the tree has no `head_tree`, and running against it would compare
    nothing at all -- so it refuses rather than silently dropping the binding that
    makes the green mean something."""
    from .review_run import ReviewRunRefused

    tree = record.get("head_tree", "")
    if not tree:
        raise ReviewRunRefused(
            "no_bound_tree",
            "this review record predates the head-tree binding, so a run could not be "
            "tied to the reviewed code. Re-open it: `git review close && git review open`",
        )
    return tree


def _write_run_receipt(repo_root: Path, receipt: dict) -> None:
    store = _store_dir(repo_root)
    store.mkdir(parents=True, exist_ok=True)
    _receipt_path(repo_root).write_text(json.dumps(receipt, indent=2) + "\n")


def _write_run_refusal(repo_root: Path, exc) -> None:
    """Persist WHY a run refused, so the refusal survives the terminal scrollback and
    `git review status` can say the last run refused instead of going quiet.
    Best-effort: a failure to record the refusal must never mask the refusal."""
    try:
        _write_run_receipt(repo_root, {
            "kind": "git-review-run",
            "created": datetime.now(UTC).isoformat(),
            "result": "refused",
            "refusal_code": exc.code,
            "refusal_detail": exc.detail,
            "output_log": OUTPUT_LOG_FILENAME if _output_log_path(repo_root).exists() else None,
        })
    except OSError:
        pass


def run_review(
    repo_root: Path,
    *,
    runner: str | None = None,
    test: str | None = None,
    install: str | None = None,
    package: str | None = None,
    python: str | None = None,
    test_args: list[str] | None = None,
) -> dict:
    """Run the open review's tests in this clone and return the receipt.

    Raises ``GitReviewError`` when there is no open review, and
    ``review_run.ReviewRunRefused`` for every structural refusal (drift, a bad
    ``.review-install`` hint, an install that did not take, a zero-test run, a summary
    the parser cannot read). A refusal is recorded to the receipt before it is raised.
    """
    import shlex

    from . import review_run as rr

    record = read_review(repo_root)
    if record is None:
        raise GitReviewError(
            "no open review in this repo; run `git review open` first"
        )

    try:
        return _run_review(
            repo_root,
            record,
            runner=runner,
            test=test,
            install=install,
            package=package,
            python=python,
            test_args=list(test_args or []),
            rr=rr,
            shlex=shlex,
        )
    except rr.ReviewRunRefused as exc:
        _write_run_refusal(repo_root, exc)
        raise


def _run_review(repo_root, record, *, runner, test, install, package, python, test_args, rr, shlex):
    bound_tree = _bound_head_tree(record)

    # (1) THE TREE COMPARISON, both halves, before anything is created -- so nothing
    #     this run writes can pollute what it measures. Identical to the lane path:
    #     tracked content equals the bound tree, and no untracked path the run did not
    #     create. The venv and receipt live inside the git directory, which `git
    #     status` never reports, so neither needs an exemption here.
    rr.assert_lane_tree_bound(repo_root, bound_tree)

    hint = rr.read_install_hint(repo_root) or {}
    eff_runner = runner or hint.get("runner") or "pytest"
    eff_test = test or hint.get("test")

    if eff_runner == "pytest" and test is not None:
        # The same refusal `gr2 review run` gives: silently ignoring --test would run
        # pytest and report a green about a command the caller never asked for.
        raise rr.ReviewRunRefused(
            "test_with_pytest",
            "--test is for a non-pytest runner; the pytest runner builds its own "
            "invocation. Pass --runner cargo|jest with --test, or drop --test.",
        )

    if eff_runner != "pytest":
        return _run_non_pytest(repo_root, record, bound_tree, eff_runner, eff_test, rr, shlex)
    return _run_pytest(
        repo_root, record, bound_tree, install, package, python, test_args, hint, rr, shlex
    )


def _run_non_pytest(repo_root, record, bound_tree, runner, test, rr, shlex):
    """A declared non-Python runner (cargo, jest, ...). The two language-agnostic trust
    checks still hold; there is no venv, install or import check, because a cargo or
    jest tree neither has nor needs them. Counts come from that runner's own summary."""
    from .review_runners import RUNNER_CREATED_PATHS, RUNNERS, parse_runner_summary

    if runner not in RUNNERS:
        raise rr.ReviewRunRefused(
            "unknown_runner",
            f"runner {runner!r} has no summary parser; known runners: {sorted(RUNNERS)}",
        )
    if not test:
        raise rr.ReviewRunRefused(
            "no_test_command",
            f"runner {runner!r} needs a test command: pass --test \"<cmd>\" or declare "
            "`test = <cmd>` in the repo's .review-install",
        )
    created = RUNNER_CREATED_PATHS.get(runner, {"names": frozenset(), "tops": ()})
    rr.assert_no_untracked_drift(
        repo_root, extra_allow_names=created["names"], extra_allow_tops=created["tops"]
    )

    test_command = shlex.split(test)
    try:
        proc = subprocess.run(test_command, text=True, capture_output=True, cwd=str(repo_root))
    except OSError as exc:
        raise rr.ReviewRunRefused(
            "test_command_failed",
            f"test command `{' '.join(test_command)}` could not run: {exc}",
        )
    output = proc.stdout + "\n" + proc.stderr
    _store_dir(repo_root).mkdir(parents=True, exist_ok=True)
    _output_log_path(repo_root).write_text(output)

    summary = parse_runner_summary(runner, output)
    if summary is None:
        tail = "\n".join(output.strip().splitlines()[-15:])
        raise rr.ReviewRunRefused(
            "unparseable_summary",
            f"no {runner} summary line found; refusing to call this a green "
            f"(exit was {proc.returncode}). raw tail:\n{tail}",
        )
    if not summary.get("selected"):
        raise rr.ReviewRunRefused(
            "zero_collected",
            f"{runner} ran 0 tests; a zero-test run is not a green",
        )
    receipt = _base_receipt(record, bound_tree)
    receipt.update({
        "runner": runner,
        "test_command": test_command,
        "selected": summary["selected"],
        "passed": summary["passed"],
        "failed": summary["failed"],
        "skipped": summary["skipped"],
        "errors": summary["errors"],
        "output_log": OUTPUT_LOG_FILENAME,
        "result": _verdict(summary),
    })
    _write_run_receipt(repo_root, receipt)
    return receipt


def _run_pytest(
    repo_root, record, bound_tree, install, package, python, test_args, hint, rr, shlex
):
    """The default Python path: venv under the git directory, install the clone, prove
    the import resolves inside the clone, then pytest with counts from the summary."""
    rr.assert_no_untracked_drift(repo_root)

    store = _store_dir(repo_root)
    store.mkdir(parents=True, exist_ok=True)
    venv_dir = store / VENV_DIRNAME
    interpreter = python or sys.executable
    proc = subprocess.run(
        [interpreter, "-m", "venv", str(venv_dir)], text=True, capture_output=True
    )
    if proc.returncode != 0:
        raise rr.ReviewRunRefused("venv_failed", f"venv create failed: {proc.stderr.strip()}")
    venv_python = venv_dir / "bin" / "python"

    install_source = "flag" if install is not None else None
    package_source = "flag" if package is not None else None
    install_cmd_tokens = None
    if install is not None:
        install_cmd_tokens = rr._apply_install_placeholders(
            shlex.split(install), venv_python, repo_root
        )
    elif hint.get("install"):
        install_cmd_tokens = rr._apply_install_placeholders(
            shlex.split(hint["install"]), venv_python, repo_root
        )
        install_source = "hint"
    if package is None and hint.get("package"):
        package = hint["package"]
        package_source = "hint"
    if package is None:
        raise rr.ReviewRunRefused(
            "no_package",
            "no --package given and this repo's .review-install declares none; a "
            "package name is required so the install can be proven to resolve inside "
            "the clone rather than in some other checkout on the path",
        )

    if install_cmd_tokens is None:
        install_cmd_tokens = [str(venv_python), "-m", "pip", "install", "-e", str(repo_root)]
        install_source = "default"
    # What was untracked BEFORE the install, so the cleanup below can tell this
    # run's build droppings from a file the reviewer already had. Captured once,
    # here, and consumed once, in the `finally` below -- on EVERY exit from that
    # block, not only the green path. The install writes `*.egg-info` into
    # `repo_root` (the reviewer's own working tree, not a throwaway clone) as its
    # very first act; a refusal on any later step (pytest missing, unparseable
    # summary, zero collected) used to skip the cleanup entirely, because it ran
    # as the last line of the green path only. The dropped egg-info then reads as
    # untracked drift to the reviewer's own `git status`, and to the NEXT run's
    # `untracked_before` snapshot it reads as pre-existing, so a later green run
    # neither removes it nor names it as removed either.
    untracked_before = _untracked_paths(repo_root)
    removed_build_dirs: list[str] = []
    try:
        try:
            proc = subprocess.run(
                install_cmd_tokens, text=True, capture_output=True, cwd=str(repo_root)
            )
        except OSError as exc:
            raise rr.ReviewRunRefused(
                "install_failed",
                f"install `{' '.join(install_cmd_tokens)}` could not run: {exc}",
            )
        if proc.returncode != 0:
            raise rr.ReviewRunRefused(
                "install_failed",
                f"install `{' '.join(install_cmd_tokens)}` failed: {proc.stderr.strip()[-800:]}",
            )
        undeclared = rr.detect_undeclared_extras(proc.stdout + "\n" + proc.stderr)
        if undeclared:
            raise rr.ReviewRunRefused(
                "undeclared_extra",
                "the install requested extra(s) the package does not declare: "
                f"{', '.join(undeclared)}. pip exits 0 on an undeclared extra and installs "
                "nothing for it, so the test dependencies it was meant to bring are "
                "silently absent.",
            )

        run_env = rr.scrubbed_python_env(venv_dir=venv_dir)
        resolved_file = rr.resolve_import_file(venv_python, package, run_env)
        # The clone is the tree here, so "under the lane" is "under the clone".
        rr.assert_import_under_lane(resolved_file, repo_root)

        proc = subprocess.run(
            [str(venv_python), "-I", "-c", "import pytest"],
            text=True, capture_output=True, env=run_env,
        )
        if proc.returncode != 0:
            raise rr.ReviewRunRefused(
                "pytest_not_installed",
                "pytest is not importable in the review venv; a plain editable install "
                "does not bring it. Add pytest to --install or this repo's .review-install "
                f"(it is a test-time dependency). stderr: {proc.stderr.strip()[-300:]}",
            )

        test_command = [str(venv_python), "-m", "pytest", *rr.merge_report_flags(list(test_args))]
        proc = subprocess.run(
            test_command, text=True, capture_output=True, cwd=str(repo_root), env=run_env
        )
        output = proc.stdout + "\n" + proc.stderr
        _output_log_path(repo_root).write_text(output)
        failed_ids = rr.parse_failed_ids(output)
        summary = rr.parse_pytest_summary(output)
        if summary is None:
            raise rr.ReviewRunRefused(
                "unparseable_summary",
                "no pytest summary line found; refusing to call this a green "
                f"(pytest exit was {proc.returncode})",
            )
        if not summary.get("selected"):
            raise rr.ReviewRunRefused(
                "zero_collected",
                f"pytest selected 0 tests (collected={summary.get('collected')}, "
                f"deselected={summary.get('deselected')}); a zero-test run is not a green",
            )

        version = subprocess.run(
            [str(venv_python), "--version"], text=True, capture_output=True
        ).stdout.strip()
    finally:
        removed_build_dirs = _remove_new_run_artifacts(repo_root, untracked_before)

    receipt = _base_receipt(record, bound_tree)
    receipt.update({
        "runner": "pytest",
        "interpreter": {"path": str(venv_python), "version": version},
        "resolved_install_path": resolved_file,
        "install_command": install_cmd_tokens,
        "install_source": install_source,
        "package_source": package_source,
        "test_command": test_command,
        "collected": summary["collected"],
        "deselected": summary["deselected"],
        "selected": summary["selected"],
        "passed": summary["passed"],
        "failed": summary["failed"],
        "skipped": summary["skipped"],
        "xfailed": summary["xfailed"],
        "errors": summary["errors"],
        "failed_ids": failed_ids,
        "output_log": OUTPUT_LOG_FILENAME,
        # Named in the receipt rather than done silently: a run that deletes
        # something says what it deleted.
        "removed_build_dirs": removed_build_dirs,
        "result": _verdict(summary),
    })
    _write_run_receipt(repo_root, receipt)
    return receipt


def _untracked_paths(repo_root: Path) -> set[str]:
    """Every untracked path git can see, with the HOST's ignore rules neutralized.

    `git status` honours `core.excludesFile`, so on a machine whose global gitignore
    lists `__pycache__/` this returns nothing for a directory the run itself just
    created -- and the cleanup below, which can only remove what it sees, leaves the
    clone dirtier than it found it while every assertion stays green. Not
    hypothetical: that is the condition CI hit, and ignoring `__pycache__` globally is
    common enough advice that a stranger's laptop is likely to be in it too.

    Measured as a discriminating pair on one repo holding one `__pycache__/`: with a
    global excludes file ignoring it, `status --porcelain` reports it 0 times; with
    `core.excludesFile` emptied, once.

    The empty value rather than `/dev/null`: git config names are case-insensitive, so
    `core.excludesFile` and `core.excludesfile` are one key and a second `-c` merely
    overrides the first -- and an empty value carries no filesystem path, so it means
    the same thing on Windows, where `/dev/null` would be a path question.

    It also covers the OTHER way a host ignores paths everywhere, which is easy to miss
    because it is configured somewhere else entirely: when `core.excludesFile` is UNSET
    git falls back to `$XDG_CONFIG_HOME/git/ignore`. Measured -- with that file
    ignoring `__pycache__/` and no `core.excludesFile` anywhere, `status --porcelain`
    hides the directory, and setting the key empty on the command line reveals it. Both
    mechanisms are witnessed.

    Repo-local `.gitignore` and `.git/info/exclude` are deliberately left alone: they
    belong to the repository under review, and a path the repo itself ignores is not
    this run's business (verified -- a repo-local ignore still hides its path here).

    Both cleanup call sites go through this function, the pre-install capture and the
    post-pytest sweep, so the two sets are computed under identical rules and their
    difference stays exactly "what this run created". The drift guard is a separate
    path (`review_run.assert_no_untracked_drift`) and is unaffected.
    """
    out = _git(repo_root, "-c", "core.excludesFile=", "status", "--porcelain")
    return {
        line[3:].strip().strip('"')
        for line in out.splitlines()
        if line.startswith("?? ")
    }


# The directory kinds a run creates in the source tree as a side effect of doing its
# job: the install writes `*.egg-info`, importing the package under test writes
# `__pycache__`, pytest writes `.pytest_cache`. Matched on the directory NAME only,
# and only for directories the run itself created (see the scoping below).
#
# This list is deliberately NOT the drift allowlist. The drift check answers "may this
# path be PRESENT without refusing"; this answers "did the run CREATE this, so must it
# clean it up". `.venv/` is on the first list and must never be on this one -- a
# reviewer's own virtualenv is not ours to delete.
def _is_run_artifact_dir(rel: str) -> bool:
    name = rel.rstrip("/").rsplit("/", 1)[-1]
    return name.endswith(".egg-info") or name in ("__pycache__", ".pytest_cache")


def _remove_new_run_artifacts(repo_root: Path, before: set[str]) -> list[str]:
    """Delete the artifact directories THIS run created in the source tree, and only
    those.

    A run that promises to leave the clone as it found it has to clean up after itself,
    and it creates droppings in more than one place: an editable install writes
    `<pkg>.egg-info/`, importing the package under test writes `__pycache__/`, pytest
    writes `.pytest_cache/`.

    Scoped three ways so it can never remove a reviewer's own file: only paths ABSENT
    from `before` (captured before the install), only directories whose NAME is a known
    run-artifact kind, and only untracked ones (a tracked directory never appears in
    this set). A collapsed untracked parent is never matched, because the name test is
    on the directory git actually reported.

    Measured twice, and the second one is why this is a list rather than one suffix:
    the first end-to-end run through the installed console script left
    `src/<pkg>.egg-info/` behind while the fixture's offline `.pth` install could not
    produce one; then CI on Linux failed on `src/<pkg>/__pycache__/` while the same
    assertion passed locally. Both times the claim was "leaves the clone as found" and
    both times the witness was green about a case it could not reach.
    """
    removed = []
    for rel in sorted(_untracked_paths(repo_root) - before):
        if not _is_run_artifact_dir(rel):
            continue
        target = repo_root / rel
        if target.is_dir():
            try:
                rmtree_or_refuse(target)
            except IncompleteRemoval:
                # Not listed as removed, because it was not: the receipt must not
                # claim a cleanup that did not happen. The leftover is allowlisted
                # by the drift check, so the next run still proceeds rather than
                # refusing on this run's residue.
                continue
            removed.append(rel)
    return removed


def _base_receipt(record: dict, bound_tree: str) -> dict:
    return {
        "kind": "git-review-run",
        "created": datetime.now(UTC).isoformat(),
        "repo": record.get("repo", ""),
        "base": record.get("base", ""),
        "bound_head": record.get("head", ""),
        "bound_head_tree": bound_tree,
    }


def _verdict(summary: dict) -> str:
    """A green needs at least one PASS: an all-skipped or all-deselected run has no
    failure and proves nothing, so it is not a green."""
    return (
        "green"
        if (summary["passed"] >= 1 and summary["failed"] == 0 and summary["errors"] == 0)
        else "red"
    )

_USAGE = """usage: git review <command>

  open [<base>]   bind this clone's HEAD (and its tree) as the code under review.
                  <base> defaults to the merge-base with the default branch.
  run [options]   run the reviewed repo's own tests in this clone and record a
                  receipt. Options: --runner pytest|cargo|jest, --test "<cmd>",
                  --install "<cmd>", --package <name>, --python <interpreter>,
                  --json, and anything after `--` is passed to pytest.
                  Defaults come from the repo's .review-install file.
  status          show the open review and the last run.
  close           drop the review and everything run created."""


class _RunHelpRequested(Exception):
    """`git review run -h` — print the usage and exit 0, rather than argparse's own
    text. (`git review run --help` never reaches here on some gits, which hand
    `--help` to `man git-review` before the script runs; `-h` and `help` always do.)"""


def _parse_run_args(rest: list[str]) -> tuple[argparse.Namespace, list[str]]:
    """Split `git review run`'s own options from the args meant for pytest. Everything
    after a bare `--` goes to the runner untouched, so a `-k` or a path selector reaches
    pytest instead of being eaten here."""
    passthrough: list[str] = []
    if "--" in rest:
        cut = rest.index("--")
        rest, passthrough = rest[:cut], rest[cut + 1:]
    # `add_help=False` and an explicit -h/--help below, rather than argparse's own:
    # argparse would print ITS usage and exit the process, which is the wrong text
    # (it omits the `--` passthrough and the .review-install defaults) and the wrong
    # control flow for a dispatcher that owns its own exit codes. Without this the
    # help flags fell through to "unrecognized arguments: --help" -- measured.
    if any(a in ("-h", "--help", "help") for a in rest):
        raise _RunHelpRequested
    ap = argparse.ArgumentParser(prog="git review run", add_help=False)
    ap.add_argument("--runner")
    ap.add_argument("--test")
    ap.add_argument("--install")
    ap.add_argument("--package")
    ap.add_argument("--python")
    ap.add_argument("--json", action="store_true", dest="json_output")
    return ap.parse_args(rest), passthrough


def _print_receipt(receipt: dict) -> None:
    counts = " ".join(
        f"{k}={receipt[k]}"
        for k in ("selected", "passed", "failed", "errors", "skipped")
        if receipt.get(k) is not None
    )
    print(f"{receipt['result'].upper()}: {counts}")
    for node_id in receipt.get("failed_ids", [])[:20]:
        print(f"  {node_id}")


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] in ("-h", "--help", "help"):
        print(_USAGE)
        return 0
    cmd = args[0] if args else "status"
    rest = args[1:]
    try:
        repo_root = _repo_root(Path.cwd())
        if cmd == "open":
            base_arg = rest[0] if rest else None
            rec = open_review(repo_root, base_arg)
            print(f"opened review: {rec['repo']} base {rec['base'][:12]} head {rec['head'][:12]}")
            if rec["base"] == rec["head"] and not base_arg:
                print("  warning: no diverging default branch found; base = HEAD (empty diff)")
            print(f"  stored at {_record_path(repo_root)}")
            return 0
        if cmd == "status":
            rec = read_review(repo_root)
            if rec is None:
                print("no open review in this repo")
                return 0
            print(f"review: {rec['repo']} base {rec['base'][:12]} head {rec['head'][:12]}")
            if rec["base"] == rec["head"]:
                print(
                    "  warning: base = HEAD (empty diff); review was opened with no "
                    "diverging branch"
                )
            last = read_run_receipt(repo_root)
            if last is None:
                print("  no run yet: `git review run`")
            elif last.get("result") == "refused":
                print(f"  last run REFUSED ({last.get('refusal_code')}) at {last.get('created')}")
            else:
                print(
                    f"  last run {str(last.get('result')).upper()} at {last.get('created')}: "
                    f"selected={last.get('selected')} passed={last.get('passed')} "
                    f"failed={last.get('failed')} errors={last.get('errors')}"
                )
            return 0
        if cmd == "run":
            from .review_run import ReviewRunRefused

            try:
                opts, passthrough = _parse_run_args(rest)
            except _RunHelpRequested:
                print(_USAGE)
                return 0
            try:
                receipt = run_review(
                    repo_root,
                    runner=opts.runner,
                    test=opts.test,
                    install=opts.install,
                    package=opts.package,
                    python=opts.python,
                    test_args=passthrough,
                )
            except ReviewRunRefused as exc:
                if opts.json_output:
                    print(json.dumps(
                        {"result": "refused", "refusal_code": exc.code,
                         "refusal_detail": exc.detail}, indent=2))
                else:
                    print(f"refused: {exc}", file=sys.stderr)
                # 2, not 1: a REFUSAL is structural (the run could not be trusted) and
                # is a different thing from a red test run, which exits 1 below.
                return 2
            if opts.json_output:
                print(json.dumps(receipt, indent=2))
            else:
                _print_receipt(receipt)
                print(f"  receipt {_receipt_path(repo_root)}")
            return 0 if receipt["result"] == "green" else 1

        if cmd == "close":
            removed = close_review(repo_root)
            print("closed review" if removed else "no open review to close")
            return 0
        print(f"git review: unknown subcommand {cmd!r}\n{_USAGE}", file=sys.stderr)
        return 2
    except GitReviewError as exc:
        print(f"git review: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
