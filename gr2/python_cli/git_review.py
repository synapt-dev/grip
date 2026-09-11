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

The thinnest working slice ships ``open`` / ``status`` / ``close`` of the ruled
bind / open / run / close surface. ``base`` defaults to the merge-base of HEAD and the
repo's default branch (the remote-tracking ref ``origin/HEAD`` points at, else local
main/master); when no diverging branch is found the base falls back to HEAD and the
command says so on one line.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

STORE_DIRNAME = "grip"
RECORD_FILENAME = "review.json"


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
    record = {"repo": repo_root.name, "base": base, "head": head}
    store = _store_dir(repo_root)
    store.mkdir(parents=True, exist_ok=True)
    _record_path(repo_root).write_text(json.dumps(record, indent=2) + "\n")
    return record


def read_review(repo_root: Path) -> dict[str, str] | None:
    p = _record_path(repo_root)
    if not p.is_file():
        return None
    return json.loads(p.read_text())


def close_review(repo_root: Path) -> bool:
    """Remove the review record; returns True if one was present."""
    p = _record_path(repo_root)
    if not p.is_file():
        return False
    p.unlink()
    try:
        _store_dir(repo_root).rmdir()  # leave no trace when empty
    except OSError:
        pass
    return True


_USAGE = "usage: git review <open [<base>] | status | close>"


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
                print("  warning: base = HEAD (empty diff); review was opened with no diverging branch")
            return 0
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
