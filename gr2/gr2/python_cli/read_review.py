"""A read-only review of a PR in a repo that is not a member of the reader's workspace.

`review checkout-pr` makes a lane from the workspace spec, so it cannot open another project's PR. `review read-pr`
fetches the PR head and the forge's default branch into a fresh directory (by URL, with no remote configured, so a
bare `git push` there has nowhere to go) and marks it with ``gr2.readOnlyReview``. gr2's write verbs (add, commit,
push) call :func:`refuse_if_read_only` and refuse in such a directory with one sentence.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from . import clone_exec, gitops

READ_ONLY_KEY = "gr2.readOnlyReview"
_OWNER_REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
_HEAD_REF = "refs/gr2-read/head"
_BASE_REF = "refs/gr2-read/base"


class ReadReviewError(Exception):
    pass


@dataclass(frozen=True)
class ReadReview:
    path: Path
    url: str
    head: str
    base: str
    stat: str


def _redact(url: str) -> str:
    """``url`` with any userinfo (``user:token@``) masked, so an error line does not echo a credential."""
    scheme, sep, rest = url.partition("://")
    if not sep:
        return url
    authority, slash, path = rest.partition("/")
    if "@" not in authority:
        return url
    return f"{scheme}://***@{authority.rpartition('@')[2]}{slash}{path}"


def _check(result, what: str) -> str:
    if result.returncode != 0:
        raise ReadReviewError(f"{what} failed: {(result.stderr or result.stdout).strip()}")
    return result.stdout.strip()


def open_read_review(owner_repo: str, pr_number: int, dest: Path, url: str | None = None) -> ReadReview:
    if not _OWNER_REPO.fullmatch(owner_repo) or not {".", ".."}.isdisjoint(owner_repo.split("/")):
        raise ReadReviewError(f"{owner_repo!r} is not OWNER/REPO (for example acme/widget)")
    if pr_number < 1:
        raise ReadReviewError(f"PR number must be positive, got {pr_number}")
    # A dangling symlink is not .exists(); mkdir would then fail on the link itself.
    if dest.exists() or dest.is_symlink():
        raise ReadReviewError(f"{dest} already exists; pass --dir <new directory>")
    source = url or f"https://github.com/{owner_repo}.git"
    try:
        dest.mkdir(parents=True)
    except OSError as exc:
        raise ReadReviewError(f"cannot create {dest}: {exc.strerror}") from exc
    try:
        return _fill(dest, owner_repo, pr_number, source)
    except ReadReviewError as exc:
        # A half-made directory would make the retry fail with "already exists".
        try:
            clone_exec.rmtree_or_refuse(dest)
        except clone_exec.IncompleteRemoval as left:
            raise ReadReviewError(f"{exc}; and the half-made directory was not removed: {left}") from exc
        raise


def _fill(dest: Path, owner_repo: str, pr_number: int, source: str) -> ReadReview:
    _check(gitops.git(dest, "init", "-q"), "git init")
    _check(gitops.git(dest, "config", READ_ONLY_KEY, "true"), "marking the review read-only")
    _check(
        gitops.git(
            # ext:: runs a command; refuse it here even if the user's config allows it. A --url that is a bare word
            # can name a remote in the user's global config; --refmap= keeps that remote's fetch refspecs from
            # writing refs beside the two named here.
            dest, "-c", "protocol.ext.allow=never", "fetch", "-q", "--no-tags", "--refmap=", "--", source,
            f"+refs/pull/{pr_number}/head:{_HEAD_REF}", f"+HEAD:{_BASE_REF}",
        ),
        f"fetching PR {pr_number} of {owner_repo} from {_redact(source)}",
    )
    head = _check(gitops.git(dest, "rev-parse", "--verify", f"{_HEAD_REF}^{{commit}}"), "resolving the PR head")
    base = _check(gitops.git(dest, "merge-base", _BASE_REF, _HEAD_REF), "finding the PR base")
    _check(gitops.git(dest, "checkout", "-q", "--detach", head), "checking out the PR head")
    stat = _check(gitops.git(dest, "diff", "--stat", f"{base}...{head}"), "summarizing the diff")
    return ReadReview(path=dest.resolve(), url=source, head=head, base=base, stat=stat)


def refuse_if_read_only(path: Path, verb: str) -> None:
    """Raise when ``path`` is inside a `review read-pr` directory: the reader may look, not write."""
    marked = gitops.git(path, "config", "--local", "--get", READ_ONLY_KEY)
    if marked.returncode == 0 and marked.stdout.strip() == "true":
        top = gitops.git(path, "rev-parse", "--show-toplevel").stdout.strip() or str(path)
        raise ReadReviewError(
            f"{Path(top).resolve()} {_READ_ONLY_SENTENCE}; gr2 {verb} refuses here. "
            "Review it with `git diff` or `git log`, and make changes in your own checkout"
        )


_READ_ONLY_SENTENCE = "is a read-only review (opened by `gr2 review read-pr`)"
