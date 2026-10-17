from __future__ import annotations

import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from gr2.python_cli.merge_verification import (
    CompletedMerge,
    ParentVerdict,
    ParentVerdictKind,
    consume_merge_receipt,
    verify_parent_shape,
)
from gr2.python_cli.platform import MergeMethod, MergeReceipt, PRRef


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise AssertionError(
            f"git {' '.join(args)} failed: {completed.stderr or completed.stdout}"
        )
    return completed.stdout.strip()


def _commit(repo: Path, name: str, content: str) -> str:
    (repo / name).write_text(content)
    _git(repo, "add", name)
    _git(repo, "commit", "-m", f"commit {name}")
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def dag(tmp_path: Path) -> dict[str, object]:
    source = tmp_path / "source"
    source.mkdir()
    _git(source, "init", "-b", "main")
    _git(source, "config", "user.name", "gr2 test")
    _git(source, "config", "user.email", "gr2@example.invalid")
    root = _commit(source, "root.txt", "root\n")

    # Clone the verifier before the candidate objects exist. A verifier that
    # only inspects its initial object database therefore cannot pass this
    # fixture by coincidence. It must fetch the exact receipt OID.
    remote = tmp_path / "remote.git"
    subprocess.run(
        ["git", "clone", "--bare", str(source), str(remote)],
        capture_output=True,
        text=True,
        check=True,
    )
    checkout = tmp_path / "checkout"
    subprocess.run(
        ["git", "clone", str(remote), str(checkout)],
        capture_output=True,
        text=True,
        check=True,
    )
    _git(source, "remote", "add", "origin", str(remote))

    _git(source, "checkout", "-b", "feature")
    _commit(source, "feature.txt", "feature\n")
    _git(source, "checkout", "main")
    one_parent = _commit(source, "main.txt", "main\n")
    _git(source, "merge", "--no-ff", "feature", "-m", "merge feature")
    two_parent = _git(source, "rev-parse", "HEAD")
    _git(
        source,
        "commit",
        "--allow-empty",
        "-m",
        "ordinary one-parent commit",
        "-m",
        "parent deadbeef is message text, not a DAG edge",
    )
    parent_word_in_message = _git(source, "rev-parse", "HEAD")
    _git(source, "branch", "target-one-parent", one_parent)
    _git(source, "branch", "noise-two-parent", two_parent)
    _git(
        source,
        "push",
        "origin",
        "main",
        "feature",
        "target-one-parent",
        "noise-two-parent",
    )
    return {
        "root": root,
        "one_parent": one_parent,
        "two_parent": two_parent,
        "parent_word_in_message": parent_word_in_message,
        "checkout": checkout,
    }


def _receipt(
    commit_sha: str | None,
    *,
    method: MergeMethod = MergeMethod.MERGE,
) -> MergeReceipt:
    ref = PRRef(
        repo="synapt-dev/widget",
        number=41,
        url="https://github.com/synapt-dev/widget/pull/41",
    )
    return MergeReceipt(
        requested=PRRef(repo="synapt-dev/widget", number=41),
        observed=ref,
        commit_sha=commit_sha,
        requested_method=method,
    )


def test_merge_commit_parent_shape_is_read_from_the_receipt_oid(
    dag: dict[str, object],
) -> None:
    checkout = dag["checkout"]
    assert isinstance(checkout, Path)

    good = verify_parent_shape(
        repo_root=checkout,
        receipt=_receipt(str(dag["two_parent"])),
        remote="origin",
    )
    wrong = verify_parent_shape(
        repo_root=checkout,
        receipt=_receipt(str(dag["one_parent"])),
        remote="origin",
    )

    assert good.kind is ParentVerdictKind.VERIFIED
    assert wrong.kind is ParentVerdictKind.WRONG


def test_parent_words_in_commit_messages_are_not_dag_edges(
    dag: dict[str, object],
) -> None:
    checkout = dag["checkout"]
    assert isinstance(checkout, Path)

    verdict = verify_parent_shape(
        repo_root=checkout,
        receipt=_receipt(str(dag["parent_word_in_message"])),
        remote="origin",
    )

    assert verdict.kind is ParentVerdictKind.WRONG
    assert verdict.parent_count == 1


@pytest.mark.parametrize("method", [MergeMethod.SQUASH, MergeMethod.REBASE])
def test_single_parent_methods_are_explicitly_not_applicable(
    dag: dict[str, object],
    method: MergeMethod,
) -> None:
    checkout = dag["checkout"]
    assert isinstance(checkout, Path)

    verdict = verify_parent_shape(
        repo_root=checkout,
        receipt=_receipt(str(dag["one_parent"]), method=method),
        remote="origin",
    )

    assert verdict.kind is ParentVerdictKind.NOT_APPLICABLE


@pytest.mark.parametrize(
    "method",
    [MergeMethod.MERGE, MergeMethod.SQUASH, MergeMethod.REBASE],
)
def test_missing_commit_evidence_is_unverifiable_for_every_method(
    dag: dict[str, object],
    method: MergeMethod,
) -> None:
    checkout = dag["checkout"]
    assert isinstance(checkout, Path)

    verdict = verify_parent_shape(
        repo_root=checkout,
        receipt=_receipt(None, method=method),
        remote="origin",
    )

    assert verdict.kind is ParentVerdictKind.UNVERIFIABLE


def test_broken_local_dag_is_unverifiable_not_verified(tmp_path: Path) -> None:
    verdict = verify_parent_shape(
        repo_root=tmp_path / "not-a-repository",
        receipt=_receipt("a" * 40),
        remote="origin",
    )

    assert verdict.kind is ParentVerdictKind.UNVERIFIABLE


def test_fetch_head_is_poison_not_evidence(dag: dict[str, object]) -> None:
    checkout = dag["checkout"]
    assert isinstance(checkout, Path)
    git_dir = Path(_git(checkout, "rev-parse", "--git-dir"))
    if not git_dir.is_absolute():
        git_dir = checkout / git_dir
    fetch_head = git_dir / "FETCH_HEAD"
    fetch_head.write_text(
        f"{dag['two_parent']}\t\tbranch 'noise-two-parent' of test.invalid\n"
    )

    verdict = verify_parent_shape(
        repo_root=checkout,
        receipt=_receipt(str(dag["one_parent"])),
        remote="origin",
    )

    assert verdict.kind is ParentVerdictKind.WRONG
    assert str(dag["two_parent"]) in fetch_head.read_text()


def test_concurrent_fetch_cannot_swap_the_verified_object(
    dag: dict[str, object],
) -> None:
    checkout = dag["checkout"]
    assert isinstance(checkout, Path)

    for _ in range(20):
        with ThreadPoolExecutor(max_workers=2) as pool:
            verdict_future = pool.submit(
                verify_parent_shape,
                repo_root=checkout,
                receipt=_receipt(str(dag["one_parent"])),
                remote="origin",
            )
            noise_future = pool.submit(
                _git,
                checkout,
                "fetch",
                "origin",
                "noise-two-parent",
            )
            verdict = verdict_future.result()
            noise_future.result()
        assert verdict.kind is ParentVerdictKind.WRONG


def test_consumption_reports_the_verdict_before_completion(
    dag: dict[str, object],
) -> None:
    checkout = dag["checkout"]
    assert isinstance(checkout, Path)
    reports: list[str] = []

    completed = consume_merge_receipt(
        repo_root=checkout,
        receipt=_receipt(str(dag["one_parent"])),
        remote="origin",
        report=reports.append,
    )

    assert isinstance(completed, CompletedMerge)
    assert completed.parent_verdict.kind is ParentVerdictKind.WRONG
    assert len(reports) == 1
    assert str(dag["one_parent"]) in reports[0]


@pytest.mark.parametrize(
    ("commit_key", "method", "expected_kind", "expected_reports"),
    [
        ("two_parent", MergeMethod.MERGE, ParentVerdictKind.VERIFIED, 0),
        ("one_parent", MergeMethod.MERGE, ParentVerdictKind.WRONG, 1),
        (None, MergeMethod.MERGE, ParentVerdictKind.UNVERIFIABLE, 1),
        ("one_parent", MergeMethod.SQUASH, ParentVerdictKind.NOT_APPLICABLE, 0),
        ("one_parent", MergeMethod.REBASE, ParentVerdictKind.NOT_APPLICABLE, 0),
    ],
)
def test_consumption_reports_exactly_the_verdicts_that_need_operator_attention(
    dag: dict[str, object],
    commit_key: str | None,
    method: MergeMethod,
    expected_kind: ParentVerdictKind,
    expected_reports: int,
) -> None:
    checkout = dag["checkout"]
    assert isinstance(checkout, Path)
    commit_sha = None if commit_key is None else str(dag[commit_key])
    reports: list[str] = []

    completed = consume_merge_receipt(
        repo_root=checkout,
        receipt=_receipt(commit_sha, method=method),
        remote="origin",
        report=reports.append,
    )

    assert completed.parent_verdict.kind is expected_kind
    assert len(reports) == expected_reports


def test_report_failure_prevents_completion(dag: dict[str, object]) -> None:
    checkout = dag["checkout"]
    assert isinstance(checkout, Path)

    def refuse_report(_message: str) -> None:
        raise RuntimeError("report sink unavailable")

    with pytest.raises(RuntimeError, match="report sink unavailable"):
        consume_merge_receipt(
            repo_root=checkout,
            receipt=_receipt(str(dag["one_parent"])),
            remote="origin",
            report=refuse_report,
        )


def test_completion_cannot_be_constructed_from_an_unreported_verdict(
    dag: dict[str, object],
) -> None:
    checkout = dag["checkout"]
    assert isinstance(checkout, Path)
    verdict = verify_parent_shape(
        repo_root=checkout,
        receipt=_receipt(str(dag["two_parent"])),
        remote="origin",
    )

    with pytest.raises(TypeError):
        CompletedMerge(_receipt(str(dag["two_parent"])), verdict)


def test_public_parent_verdict_constructor_claims_nothing() -> None:
    verdict = ParentVerdict()

    assert verdict.kind is ParentVerdictKind.NOT_PERFORMED
    with pytest.raises(TypeError):
        ParentVerdict(kind=ParentVerdictKind.VERIFIED)


@pytest.mark.parametrize(
    ("attribute", "forged"),
    [
        ("_kind", ParentVerdictKind.VERIFIED),
        ("_commit_sha", "f" * 40),
        ("_parent_count", 2),
        ("_detail", "forged verification"),
    ],
)
def test_parent_verdict_cannot_be_rewritten_after_construction(
    attribute: str,
    forged: object,
) -> None:
    verdict = ParentVerdict()

    with pytest.raises(AttributeError, match="immutable"):
        setattr(verdict, attribute, forged)

    assert verdict.kind is ParentVerdictKind.NOT_PERFORMED
    assert verdict.commit_sha is None
    assert verdict.parent_count is None
    assert verdict.detail == "parent verification was not performed"


@pytest.mark.parametrize(
    ("attribute", "forged"),
    [
        ("_receipt", object()),
        ("_parent_verdict", ParentVerdict()),
    ],
)
def test_completed_merge_cannot_be_rewritten_after_construction(
    dag: dict[str, object],
    attribute: str,
    forged: object,
) -> None:
    checkout = dag["checkout"]
    assert isinstance(checkout, Path)
    receipt = _receipt(str(dag["two_parent"]))
    completed = consume_merge_receipt(
        repo_root=checkout,
        receipt=receipt,
        remote="origin",
        report=lambda _message: None,
    )

    with pytest.raises(AttributeError, match="immutable"):
        setattr(completed, attribute, forged)

    assert completed.receipt is receipt
    assert completed.parent_verdict.kind is ParentVerdictKind.VERIFIED


def test_a_stored_remote_shaped_like_an_option_never_reaches_fetch_as_one(tmp_path: Path) -> None:
    # The remote comes from a stored PR group; the receipt oid is hex. A directory in the repo named by
    # that oid is a repository git would fetch from, so an option-shaped remote would run its program.
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    oid = "a" * 40
    _git(tmp_path, "init", "-q", "--bare", str(repo / oid))
    marker = tmp_path / "UPLOAD_PACK_RAN"
    verdict = verify_parent_shape(
        repo_root=repo,
        receipt=_receipt(oid),
        remote=f"--upload-pack=touch {marker}; git-upload-pack",
    )
    assert not marker.exists()
    assert verdict.kind is not None


def test_a_receipt_commit_shaped_like_an_option_never_reaches_fetch_as_one(tmp_path: Path) -> None:
    # The receipt's commit id comes from the host and is not checked as hex; git parses options after positionals,
    # so without `--` a commit id shaped like an option would run its program even with a plain remote.
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    remote = tmp_path / "remote.git"
    _git(tmp_path, "init", "-q", "--bare", str(remote))
    marker = tmp_path / "UPLOAD_PACK_RAN"
    verify_parent_shape(
        repo_root=repo,
        receipt=_receipt(f"--upload-pack=touch {marker}; git-upload-pack"),
        remote=str(remote),
    )
    assert not marker.exists()
