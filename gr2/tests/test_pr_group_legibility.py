"""A two-repo PR set has to be legible on GitHub, and on a4 it was not.

Every gap covered here was MEASURED on released a4 against the real test repos, not
inferred from reading the code: both PRs titled ``set-lane``, both bodies the fixed
string ``gr2 PR group for default/set-lane``, no sibling link in either, and
``pr_number: null`` in the group JSON while the ``url`` beside it carried the number.

The gh witnesses shell out to a stub on disk rather than monkeypatching the call, so
the subprocess path and the parse are both exercised: a stub injected past
``subprocess.run`` would prove the parse and not the call.
"""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest
from gr2.python_cli.platform import (
    AdapterError,
    CreatePRRequest,
    GitHubAdapter,
    PRRef,
    pr_number_from_url,
)


def _stub_gh(tmp_path: Path, stdout: str) -> str:
    """A ``gh`` that prints exactly what we tell it to, on disk and executable."""
    path = tmp_path / "gh-stub"
    path.write_text(f"#!/bin/sh\necho {json.dumps(stdout)}\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path)


def _recording_gh(tmp_path: Path, stdout: str) -> tuple[str, Path, Path]:
    """A ``gh`` that records its argv AND the body file it was handed, on disk.

    Recording the body's CONTENT at the moment gh runs is the point: the temp file is
    removed when the call returns, so reading it afterwards proves only that a path
    was passed, not that gh could read the caller's body from it.
    """
    argv_log = tmp_path / "gh-argv.txt"
    body_log = tmp_path / "gh-body.txt"
    path = tmp_path / "gh-recording"
    path.write_text(
        "#!/bin/sh\n"
        f"printf '%s\\n' \"$@\" > {json.dumps(str(argv_log))}\n"
        "prev=''\n"
        'for a in "$@"; do\n'
        f'  if [ "$prev" = "--body-file" ]; then cat "$a" > {json.dumps(str(body_log))}; fi\n'
        '  prev="$a"\n'
        "done\n"
        f"echo {json.dumps(stdout)}\n"
    )
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path), argv_log, body_log


def _request(body: str = "b") -> CreatePRRequest:
    return CreatePRRequest(
        repo="synapt-dev/gitgrip-test-1",
        title="t",
        body=body,
        head_branch="set-branch",
        base_branch="main",
    )


def test_the_pr_body_reaches_gh_in_a_file_not_on_argv(tmp_path: Path) -> None:
    """The body is caller text and other PRs' URLs, and it travels in a file. argv is
    world-readable for the life of the process and bounded by ARG_MAX, so a body on the
    command line is both visible to every process on the host and a hard limit on how
    long it can be."""
    body = "line one\nline two\n- https://github.com/o/r/pull/1\n"
    gh, argv_log, body_log = _recording_gh(tmp_path, "https://github.com/o/r/pull/42")
    GitHubAdapter(gh_binary=gh).create_pr(_request(body))

    argv = argv_log.read_text().splitlines()
    assert "--body" not in argv, f"the body must not travel on argv; got {argv}"
    assert "--body-file" in argv, f"the body must travel in a file; got {argv}"
    assert body_log.read_text() == body, (
        "gh must read exactly the caller's body from the file it was handed"
    )


def test_pr_number_is_parsed_from_the_url_gh_prints(tmp_path: Path) -> None:
    """THE GAP. PRRef.number defaults to None, and nothing filled it, so the group
    JSON carried `pr_number: null` beside a url that ended in /pull/33."""
    adapter = GitHubAdapter(gh_binary=_stub_gh(tmp_path, "https://github.com/o/r/pull/42"))
    ref = adapter.create_pr(_request())
    assert ref.number == 42, f"pr_number came back {ref.number!r} with url {ref.url!r}"
    assert ref.url == "https://github.com/o/r/pull/42"


def test_a_url_carrying_no_pull_number_refuses_rather_than_nulling(tmp_path: Path) -> None:
    """Refusing is the point: a silent None is indistinguishable from a PR that has
    no number, which is the state that made the gap hard to see in the first place."""
    url = "https://github.com/o/r/issues/7"
    adapter = GitHubAdapter(gh_binary=_stub_gh(tmp_path, url))
    with pytest.raises(AdapterError) as excinfo:
        adapter.create_pr(_request())
    assert url in str(excinfo.value), (
        "the refusal must NAME the url, or a caller cannot tell which repo produced it"
    )


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "not a url",
        "https://github.com/o/r/pull/",
        "https://github.com/o/r/pull/abc",
        "https://github.com/o/r/pull/42/files",
    ],
)
def test_pr_number_from_url_refuses_everything_that_is_not_pull_digits(bad: str) -> None:
    with pytest.raises(AdapterError):
        pr_number_from_url(bad)


def test_pr_number_from_url_accepts_a_trailing_slash() -> None:
    # gh has printed the URL with and without a trailing slash across versions, and a
    # refusal there would be a false alarm on a perfectly good PR.
    assert pr_number_from_url("https://github.com/o/r/pull/42/") == 42


# ---------------------------------------------------------------------------
# The sibling block. A PR body must let a reviewer on one PR reach the others.
# ---------------------------------------------------------------------------

class _RecordingAdapter:
    """Stub adapter that records creations and edits, and can fail one edit."""

    name = "fake"

    def __init__(self, *, fail_edit_for: str | None = None) -> None:
        self.created: list[CreatePRRequest] = []
        self.edits: list[tuple[str, int, str]] = []
        self._fail_edit_for = fail_edit_for

    def create_pr(self, request: CreatePRRequest) -> PRRef:
        self.created.append(request)
        number = 100 + len(self.created)
        return PRRef(
            repo=request.repo,
            number=number,
            url=f"https://github.com/{request.repo}/pull/{number}",
            head_branch=request.head_branch,
            base_branch=request.base_branch,
            title=request.title,
        )

    def edit_pr_body(self, repo: str, number: int, body: str) -> None:
        if self._fail_edit_for == repo:
            raise AdapterError(f"gh pr edit failed for {repo}#{number}: pull request is closed")
        self.edits.append((repo, number, body))


def _group(workspace: Path, adapter: _RecordingAdapter, *, body: str = "gr2 PR group for default/set-lane"):
    from gr2.python_cli.pr import create_pr_group

    return create_pr_group(
        workspace_root=workspace,
        owner_unit="default",
        lane_name="set-lane",
        title="a real title",
        base_branch="main",
        head_branch="set-lane",
        repos=["synapt-dev/gitgrip-test-1", "synapt-dev/gitgrip-test-2"],
        adapter=adapter,
        actor="agent:apollo",
        body=body,
    )


def test_each_pr_body_names_its_siblings_by_url(workspace: Path) -> None:
    """THE SIBLING GAP. On a4 both bodies were the same fixed line, so a reviewer who
    landed on one PR of the set had no path to the other. The links can only be added
    after every PR exists, which is why this is a second pass."""
    adapter = _RecordingAdapter()
    group = _group(workspace, adapter)
    by_repo = {str(item["repo"]): item for item in group["prs"]}

    assert len(adapter.edits) == 2, f"expected both PRs to be edited, got {len(adapter.edits)}"
    for repo, _number, body in adapter.edits:
        others = [r for r in by_repo if r != repo]
        for other in others:
            assert by_repo[other]["url"] in body, f"{repo}'s body does not name {other}'s url"
    # the caller's body is the base of the edited one, not replaced by the block
    assert all(b.startswith("gr2 PR group for default/set-lane") for _r, _n, b in adapter.edits)
    assert group["sibling_edits"] == {
        "synapt-dev/gitgrip-test-1": "linked",
        "synapt-dev/gitgrip-test-2": "linked",
    }


def test_a_failed_sibling_edit_is_fatal_and_names_the_repo(workspace: Path) -> None:
    """A half-linked set that prints success is worse than no links: the reader
    believes the set is navigable. Every failure is recorded per repo and any failure
    fails the operation."""
    from gr2.python_cli.pr import SiblingLinkError

    adapter = _RecordingAdapter(fail_edit_for="synapt-dev/gitgrip-test-2")
    with pytest.raises(SiblingLinkError) as excinfo:
        _group(workspace, adapter)

    exc = excinfo.value
    assert "synapt-dev/gitgrip-test-2#102" in str(exc), f"the unlinked PR must be named; got: {exc}"
    assert exc.group["sibling_edits"]["synapt-dev/gitgrip-test-2"].startswith("FAILED")
    assert exc.group["sibling_edits"]["synapt-dev/gitgrip-test-1"] == "linked", (
        "the repo that DID link must still be reported as linked"
    )
    # and the record survives, because the group is persisted before the raise
    assert exc.group["state_path"], "the persisted group must be reachable from the error"


def test_a_single_repo_group_gets_no_sibling_pass(workspace: Path) -> None:
    from gr2.python_cli.pr import create_pr_group

    adapter = _RecordingAdapter()
    group = create_pr_group(
        workspace_root=workspace,
        owner_unit="default",
        lane_name="solo",
        title="t",
        base_branch="main",
        head_branch="solo",
        repos=["synapt-dev/gitgrip-test-1"],
        adapter=adapter,
        actor="agent:apollo",
    )
    assert adapter.edits == [], "a one-PR group has no siblings and must not be edited"
    assert group["sibling_edits"] == {}
