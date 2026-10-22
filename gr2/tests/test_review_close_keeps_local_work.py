"""Reconstruction close must keep user work and its recovery evidence.

Both the public close verb and its close-gr compatibility route own the same
disposal boundary. A review's marker and run artifacts are tool-owned, so they
must not turn the clean-lane control into a dirty-work refusal.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from gr2.python_cli import app as gr2_app
from gr2.python_cli import open_gr_review, review_allocation, review_run
from tests.conftest import make_cli_runner
from tests.test_review_close_gr import _git, _open_gr_lane


@pytest.fixture
def reconstruction(tmp_path: Path):
    runner = make_cli_runner()
    lane, review_id = _open_gr_lane(tmp_path, runner)
    workspace = tmp_path / "ws"
    allocation = review_allocation.allocation_path(workspace, lane)
    marker = lane / open_gr_review._OPEN_GR_MARKER
    assert allocation.is_file() and marker.is_file()
    # These are the exact artifact names/schema written by review run. The
    # failing result is evidence to retain, not uncommitted user work.
    receipt = lane / review_run._RECEIPT_NAME
    log = lane / review_run._OUTPUT_LOG_NAME
    log.write_text("FAILED tests/example.py::test_example - boundary witness\n")
    receipt.write_text(json.dumps({
        "kind": "review-run", "created": "2026-10-07T00:00:00+00:00",
        "gr_commit": review_id, "result": "red", "failed": 1,
        "failed_ids": ["tests/example.py::test_example"],
        "output_log": review_run._OUTPUT_LOG_NAME,
    }) + "\n")
    # No tracked work is dirty before the witness changes it.
    assert _git(lane, "diff", "--name-only") == ""
    assert _git(lane, "diff", "--cached", "--name-only") == ""
    return runner, lane, allocation, (marker, receipt, log)



def _text(result) -> str:
    """Both streams: stdout and stderr are separate on click<8.2 (see make_cli_runner), merged after."""
    return result.stdout + result.stderr

def _put_local_work(lane: Path, kind: str) -> tuple[Path, str | None]:
    file = lane / ("new-user-file.txt" if kind == "untracked" else "f.txt")
    file.write_text("user work must survive close\n")
    if kind in {"staged", "unpublished"}:
        _git(lane, "add", file.name)
    if kind == "unpublished":
        reviewed_head = _git(lane, "rev-parse", "HEAD")
        _git(lane, "checkout", "-qb", "local-review-work")
        _git(lane, "-c", "user.name=Fixture", "-c",
             "user.email=fixture@example.invalid", "commit", "-qm", "local work")
        commit = _git(lane, "rev-parse", "HEAD")
        assert _git(lane, "branch", "-r", "--contains", commit) == ""
        # Query the real local bare origin as well as cached remote-tracking
        # refs: this commit is absent from every advertised remote branch.
        # A read lane's origin is the host's review mirror (a file:// URL),
        # which carries every ref the remote advertises.
        assert _git(lane, "remote") == "origin"
        remote = _git(lane, "remote", "get-url", "origin").removeprefix("file://")
        assert commit not in _git(Path(remote), "rev-list", "--all").splitlines()
        _git(lane, "checkout", "-q", "--detach", reviewed_head)
        assert _git(lane, "rev-parse", "HEAD") == reviewed_head
        assert _git(lane, "diff", "--name-only") == ""
        assert _git(lane, "diff", "--cached", "--name-only") == ""
        assert _git(lane, "rev-parse", "refs/heads/local-review-work") == commit
        return file, commit
    status = _git(lane, "status", "--porcelain", "--", file.name)
    assert status == {"staged": "M  f.txt", "unstaged": "M f.txt",
                      "untracked": "?? new-user-file.txt"}[kind]
    return file, None


@pytest.mark.parametrize("verb", ["close", "close-gr"])
@pytest.mark.parametrize("kind", ["staged", "unstaged", "untracked", "unpublished"])
def test_reconstruction_close_refuses_local_work_and_keeps_evidence(reconstruction, verb, kind):
    runner, lane, allocation, evidence = reconstruction
    file, commit = _put_local_work(lane, kind)
    before = {path: path.read_bytes() for path in (allocation, *evidence)}
    status = _git(lane, "status", "--porcelain")
    head = _git(lane, "rev-parse", "HEAD")
    result = runner.invoke(gr2_app.app, ["review", verb, str(lane), "--json"])

    assert result.exit_code != 0, (
        f"unsafe {verb} accepted {kind} user work: rc={result.exit_code}, "
        f"lane_kept={lane.exists()}, allocation_kept={allocation.exists()}, "
        f"run_evidence_kept={all(p.exists() for p in evidence[1:])}; {_text(result)}"
    )
    text = _text(result).lower()
    assert "refused" in text and str(lane) in _text(result), _text(result)
    assert ("unpublished" in text or "unpushed" in text) if commit else (
        "uncommitted" in text or "dirty" in text
    ), _text(result)
    assert lane.is_dir() and (lane / ".git").is_dir()
    assert {path: path.read_bytes() for path in before} == before
    assert _git(lane, "status", "--porcelain") == status
    assert _git(lane, "rev-parse", "HEAD") == head
    assert _git(lane, "stash", "list") == ""
    if commit:
        assert _git(lane, "rev-parse", "refs/heads/local-review-work") == commit
        assert _git(lane, "show", f"{commit}:f.txt") == "user work must survive close"
    else:
        assert file.read_text() == "user work must survive close\n"


@pytest.mark.parametrize("verb", ["close", "close-gr"])
def test_reconstruction_close_clean_control_preserves_run_evidence(reconstruction, verb):
    runner, lane, allocation, evidence = reconstruction
    receipt_before, log_before = evidence[1].read_bytes(), evidence[2].read_bytes()
    result = runner.invoke(gr2_app.app, ["review", verb, str(lane), "--json"])
    assert result.exit_code == 0, _text(result)
    assert not lane.exists() and not allocation.exists()
    payload = json.loads(result.stdout)
    preserved = payload["preserved_run"]
    assert Path(preserved["original_receipt"]).read_bytes() == receipt_before
    assert Path(preserved["log"]).read_bytes() == log_before
    usable = json.loads(Path(preserved["receipt"]).read_text())
    assert usable["failed_ids"] == ["tests/example.py::test_example"]
    assert Path(usable["output_log"]) == Path(preserved["log"])


@pytest.mark.parametrize("verb", ["close", "close-gr"])
def test_reconstruction_close_refuses_a_lane_holding_a_stash(reconstruction, verb):
    """A stash is work too: a clean tree with a stash must not be removed with it."""
    runner, lane, allocation, _ = reconstruction
    (lane / "f.txt").write_text("stashed user work\n")
    _git(lane, "stash", "push", "-q", "-m", "user stash")
    assert _git(lane, "status", "--porcelain", "--", "f.txt") == ""
    result = runner.invoke(gr2_app.app, ["review", verb, str(lane), "--json"])
    assert result.exit_code != 0, _text(result)
    assert "stash" in _text(result).lower(), _text(result)
    assert lane.is_dir() and allocation.exists()
    assert _git(lane, "show", "stash@{0}:f.txt") == "stashed user work"


@pytest.mark.parametrize("verb", ["close", "close-gr"])
def test_reconstruction_close_refuses_when_git_cannot_read_the_lane(reconstruction, verb):
    """A git call that fails must not read as a clean lane: a corrupt index makes status fail."""
    runner, lane, allocation, _ = reconstruction
    (lane / "new-user-file.txt").write_text("user work\n")
    (lane / ".git" / "index").write_bytes(b"not an index")
    result = runner.invoke(gr2_app.app, ["review", verb, str(lane), "--json"])
    assert result.exit_code != 0, _text(result)
    assert "could not read" in _text(result), _text(result)
    assert (lane / "new-user-file.txt").read_text() == "user work\n" and allocation.exists()


@pytest.mark.parametrize("verb", ["close", "close-gr"])
def test_reconstruction_close_refuses_a_commit_on_the_reviewed_head(reconstruction, verb):
    runner, lane, allocation, _ = reconstruction
    (lane / "f.txt").write_text("committed on the reconstruction\n")
    _git(lane, "-c", "user.name=F", "-c", "user.email=f@x.invalid", "commit", "-qam", "user commit")
    commit = _git(lane, "rev-parse", "HEAD")
    result = runner.invoke(gr2_app.app, ["review", verb, str(lane), "--json"])
    assert result.exit_code != 0, _text(result)
    assert lane.is_dir() and allocation.exists() and _git(lane, "rev-parse", "HEAD") == commit


@pytest.mark.parametrize("verb", ["close", "close-gr"])
def test_reconstruction_close_refuses_a_local_only_tag(reconstruction, verb):
    runner, lane, allocation, _ = reconstruction
    # Made without moving HEAD, so no reflog holds it: only the tag reaches this commit.
    tree = _git(lane, "rev-parse", "HEAD^{tree}")
    commit = _git(lane, "-c", "user.name=F", "-c", "user.email=f@x.invalid", "commit-tree", tree,
                  "-p", "HEAD", "-m", "tagged")
    _git(lane, "tag", "kept-work", commit)
    assert commit not in _git(lane, "rev-list", "--reflog").split()
    result = runner.invoke(gr2_app.app, ["review", verb, str(lane), "--json"])
    assert result.exit_code != 0 and "unpublished" in _text(result).lower(), _text(result)
    assert lane.is_dir() and allocation.exists()


def _multi_member_lane(tmp_path: Path, before_allocation=None) -> Path:
    from tests.test_review_run_multi_repo import _lane
    lane = _lane(tmp_path)
    if before_allocation is not None:
        before_allocation(lane)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    review_allocation.record_created_allocation(
        workspace, lane, "workspace", "review", [lane / "demo-core", lane / "demo-web"], disposable=True)
    marker_path = lane / review_run._MARKER_NAME
    marker = json.loads(marker_path.read_text())
    marker["workspace_root"] = str(workspace)
    marker_path.write_text(json.dumps(marker))
    return lane


def test_close_refuses_a_user_file_at_the_root_of_a_multi_member_lane(tmp_path: Path):
    runner = make_cli_runner()
    lane = _multi_member_lane(tmp_path)
    (lane / "notes.txt").write_text("user notes\n")
    result = runner.invoke(gr2_app.app, ["review", "close", str(lane), "--json"])
    assert result.exit_code != 0 and "lane root" in _text(result), _text(result)
    assert (lane / "notes.txt").read_text() == "user notes\n"


def test_a_clean_multi_member_lane_still_closes(tmp_path: Path):
    runner = make_cli_runner()
    lane = _multi_member_lane(tmp_path)
    (lane / ".venv").mkdir()
    (lane / f"demo-core{review_run._OUTPUT_LOG_NAME}").write_text("log\n")
    result = runner.invoke(gr2_app.app, ["review", "close", str(lane), "--json"])
    assert result.exit_code == 0, _text(result)
    assert not lane.exists()


@pytest.mark.parametrize("name", ["mine.grip-review-run.log", ".grip-review-run.log.bak"])
def test_a_root_file_that_only_looks_like_a_run_log_is_work(tmp_path: Path, name: str):
    runner = make_cli_runner()
    lane = _multi_member_lane(tmp_path)
    (lane / name).write_text("user file\n")
    result = runner.invoke(gr2_app.app, ["review", "close", str(lane), "--json"])
    assert result.exit_code != 0 and name in _text(result), _text(result)
    assert (lane / name).read_text() == "user file\n"


def test_the_exact_run_log_names_at_the_root_are_the_reviews_own(tmp_path: Path):
    runner = make_cli_runner()
    lane = _multi_member_lane(tmp_path)
    for name in (review_run._OUTPUT_LOG_NAME, f"demo-web{review_run._OUTPUT_LOG_NAME}",
                 f"{review_run._OUTPUT_LOG_NAME}.demo-core", *review_run._LEGACY_MARKERS):
        (lane / name).write_text("tool output\n")
    result = runner.invoke(gr2_app.app, ["review", "close", str(lane), "--json"])
    assert result.exit_code == 0, _text(result)


def test_a_stash_is_named_once_and_not_as_an_unpublished_commit(reconstruction):
    runner, lane, _, _ = reconstruction
    (lane / "f.txt").write_text("stashed\n")
    _git(lane, "stash", "push", "-q")
    result = runner.invoke(gr2_app.app, ["review", "close", str(lane), "--json"])
    assert result.exit_code != 0 and "a stash" in _text(result), _text(result)
    assert "unpublished" not in _text(result).lower(), _text(result)


def test_a_directory_named_like_a_run_log_at_the_root_is_work(tmp_path: Path):
    runner = make_cli_runner()
    lane = _multi_member_lane(tmp_path)
    notes = lane / review_run._OUTPUT_LOG_NAME
    notes.mkdir()
    (notes / "valuable.txt").write_text("user notes\n")
    result = runner.invoke(gr2_app.app, ["review", "close", str(lane), "--json"])
    assert result.exit_code != 0, _text(result)
    assert (notes / "valuable.txt").read_text() == "user notes\n"


def test_a_commit_left_only_in_the_reflog_is_unpublished(reconstruction):
    runner, lane, allocation, _ = reconstruction
    head = _git(lane, "rev-parse", "HEAD")
    (lane / "f.txt").write_text("reflog-only work\n")
    _git(lane, "-c", "user.name=F", "-c", "user.email=f@x.invalid", "commit", "-qam", "reflog only")
    commit = _git(lane, "rev-parse", "HEAD")
    _git(lane, "checkout", "-q", "--detach", head)
    assert commit not in _git(lane, "rev-list", "--all").split()
    result = runner.invoke(gr2_app.app, ["review", "close", str(lane), "--json"])
    assert result.exit_code != 0 and commit[:12] in _text(result), _text(result)
    assert lane.is_dir() and allocation.exists()


def test_a_runners_own_outputs_do_not_refuse_a_clean_lane(reconstruction):
    """The receipt names the runner; its created outputs (cargo: Cargo.lock, target/) are the review's."""
    runner, lane, allocation, (_, receipt, _) = reconstruction
    data = json.loads(receipt.read_text())
    data["runner"] = "cargo"
    receipt.write_text(json.dumps(data) + "\n")
    (lane / "Cargo.lock").write_text("# lock\n")
    (lane / "target" / "debug").mkdir(parents=True)
    (lane / "target" / "debug" / "out").write_text("bin\n")
    result = runner.invoke(gr2_app.app, ["review", "close", str(lane), "--json"])
    assert result.exit_code == 0, _text(result)
    assert not lane.exists()


def test_the_same_outputs_without_that_runner_are_work(reconstruction):
    runner, lane, allocation, _ = reconstruction
    (lane / "Cargo.lock").write_text("# lock\n")
    result = runner.invoke(gr2_app.app, ["review", "close", str(lane), "--json"])
    assert result.exit_code != 0 and "Cargo.lock" in _text(result), _text(result)


def test_a_file_named_venv_at_the_root_is_work(tmp_path: Path):
    """The shared environment is a directory; a regular file by that name is not the review's."""
    runner = make_cli_runner()
    lane = _multi_member_lane(tmp_path)
    (lane / ".venv").write_text("user notes\n")
    result = runner.invoke(gr2_app.app, ["review", "close", str(lane), "--json"])
    assert result.exit_code != 0 and ".venv" in _text(result), _text(result)
    assert (lane / ".venv").read_text() == "user notes\n"


# ---- what `review run` leaves in a lane is the review's: the install's build/ and the clones it placed


def _run_receipt(lane: Path, downstream: dict | None = None, install_outputs: list[str] | None = None) -> None:
    receipt = {"kind": "review-run", "created": "2026-10-13T00:00:00+00:00", "result": "green",
               "install_outputs": list(install_outputs or [])}
    if downstream is not None:
        receipt["downstream"] = downstream
    (lane / review_run._RECEIPT_NAME).write_text(json.dumps(receipt) + "\n")


def _build_output(member: Path, rel: str = "build/lib/demo_core/__init__.py") -> Path:
    path = member / rel
    path.parent.mkdir(parents=True)
    path.write_text("# what setuptools copies on a non-editable pip install\n")
    return path


def test_the_build_dir_a_run_left_in_a_member_does_not_block_close(tmp_path: Path):
    """A non-editable `pip install <member>` writes `build/` into the member; `review run` admits it as
    install output, so close must too. Before, the tutorial's `review close` exited 2 after a green run."""
    runner = make_cli_runner()
    lane = _multi_member_lane(tmp_path)
    _build_output(lane / "demo-core")
    _build_output(lane / "demo-web", "build/lib/demo_web/app.py")
    _run_receipt(lane, install_outputs=["demo-core/build/lib/demo_core/__init__.py",
                                        "demo-web/build/lib/demo_web/app.py"])
    result = runner.invoke(gr2_app.app, ["review", "close", str(lane), "--json"])
    assert result.exit_code == 0, _text(result)
    assert not lane.exists()


def test_a_build_dir_in_a_lane_no_run_has_touched_is_still_work(tmp_path: Path):
    runner = make_cli_runner()
    lane = _multi_member_lane(tmp_path)
    kept = _build_output(lane / "demo-core")
    result = runner.invoke(gr2_app.app, ["review", "close", str(lane), "--json"])
    assert result.exit_code != 0 and "build/" in _text(result), _text(result)
    assert kept.is_file()


def test_a_build_dir_below_the_member_top_is_still_work(tmp_path: Path):
    """Only the member's top-level `build/` is install output; a `conftest.py` deeper down is not."""
    runner = make_cli_runner()
    lane = _multi_member_lane(tmp_path)
    kept = _build_output(lane / "demo-core", "src/build/conftest.py")
    # Even recorded: the receipt admits a path only under the install's own top-level `build/`.
    _run_receipt(lane, install_outputs=["demo-core/src/build/conftest.py"])
    result = runner.invoke(gr2_app.app, ["review", "close", str(lane), "--json"])
    assert result.exit_code != 0 and "src/build/" in _text(result), _text(result)
    assert kept.is_file()


def _placed_clone(tmp_path: Path, lane: Path, key: str) -> str:
    """A member `review run` cloned into the lane root at its pin, as it does for a downstream member."""
    source = tmp_path / f"{key}-source"
    source.mkdir()
    _git(source, "init", "-q", "-b", "main")
    (source / "README.md").write_text(f"{key}\n")
    _git(source, "add", "README.md")
    _git(source, "-c", "user.name=F", "-c", "user.email=f@x.invalid", "commit", "-qm", "pin")
    _git(lane, "clone", "-q", str(source), key)
    return _git(lane / key, "rev-parse", "HEAD")


def _skip_unless_case_sensitive(tmp_path: Path) -> None:
    """Rows that clone into two names differing only in case need a disk that keeps them apart; ask the disk."""
    probe = tmp_path / "case-probe"
    probe.mkdir()
    if (tmp_path / "CASE-PROBE").exists():
        pytest.skip("case-insensitive filesystem: the fixture's case-only names are one directory here")


def _selection(downstream: dict[str, str]) -> dict:
    return {"status": "ran", "changed": ["demo-core", "demo-web"], "upstream": [],
            "downstream": list(downstream), "pins": dict(downstream)}


def test_the_downstream_clones_a_run_recorded_do_not_block_close(tmp_path: Path):
    """At five members the tutorial's run clones demo-api and demo-worker into the lane root and logs
    each; close named all four as work at the lane root and exited 2 after a green run."""
    runner = make_cli_runner()
    lane = _multi_member_lane(tmp_path)
    pins = {key: _placed_clone(tmp_path, lane, key) for key in ("demo-api", "demo-worker")}
    for key in pins:
        (lane / f"{review_run._OUTPUT_LOG_NAME}.{key}").write_text("log\n")
    _build_output(lane / "demo-api", "build/lib/demo_api/__init__.py")
    _run_receipt(lane, _selection(pins), install_outputs=["demo-api/build/lib/demo_api/__init__.py"])
    result = runner.invoke(gr2_app.app, ["review", "close", str(lane), "--json"])
    assert result.exit_code == 0, _text(result)
    assert not lane.exists()


@pytest.mark.parametrize("case", ["unrecorded", "moved_off_its_pin", "work_inside", "no_run_receipt"])
def test_a_clone_at_the_lane_root_the_run_did_not_place_is_still_work(tmp_path: Path, case: str):
    """Only a clone the run's receipt names, at the pin it recorded, is the review's; a clone examined
    as a member still refuses when it holds work."""
    runner = make_cli_runner()
    lane = _multi_member_lane(tmp_path)
    pin = _placed_clone(tmp_path, lane, "demo-api")
    recorded = {"demo-api": pin}
    if case == "unrecorded":
        recorded = {"demo-worker": pin}
    elif case == "moved_off_its_pin":
        recorded = {"demo-api": "f" * 40}
    elif case == "work_inside":
        (lane / "demo-api" / "notes.txt").write_text("user notes\n")
    if case != "no_run_receipt":
        _run_receipt(lane, _selection(recorded))
    result = runner.invoke(gr2_app.app, ["review", "close", str(lane), "--json"])
    assert result.exit_code != 0, _text(result)
    expected = "notes.txt" if case == "work_inside" else "work at the lane root"
    assert expected in _text(result) and "demo-api" in _text(result), _text(result)
    assert (lane / "demo-api" / ".git").is_dir()


def _track_in_reviewed_head(member: Path, rel: str) -> None:
    """`rel` becomes part of the member's reviewed tree (HEAD), as if the reviewed commit tracked it."""
    _git(member, "add", rel)
    _git(member, "-c", "user.name=F", "-c", "user.email=f@x.invalid", "commit", "-qm", "track it")


@pytest.mark.parametrize("case", ["tracked_build_dir", "written_after_the_run", "written_before_the_run"])
def test_a_build_file_the_run_did_not_record_is_work(tmp_path: Path, case: str):
    """Close admits the exact files the run's install wrote, never `build/` as a whole: a member that tracks a
    `build/` and a file a person adds before or after the run all stay work."""
    runner = make_cli_runner()

    def tracked_build(lane: Path) -> None:
        _build_output(lane / "demo-core", "build/tool.sh")
        _track_in_reviewed_head(lane / "demo-core", "build/tool.sh")

    lane = _multi_member_lane(tmp_path, tracked_build if case == "tracked_build_dir" else None)
    member = lane / "demo-core"
    if case == "tracked_build_dir":
        assert _git(member, "ls-files", "build") == "build/tool.sh"
    if case == "written_before_the_run":
        kept = _build_output(member, "build/notes.txt")
        kept.write_text("user notes\n")
    _build_output(member)
    _run_receipt(lane, install_outputs=["demo-core/build/lib/demo_core/__init__.py"])
    if case != "written_before_the_run":
        kept = member / "build" / "notes.txt"
        kept.write_text("user notes\n")
    result = runner.invoke(gr2_app.app, ["review", "close", str(lane), "--json"])
    assert result.exit_code != 0 and "build/notes.txt" in _text(result), _text(result)
    assert "build/lib/demo_core/__init__.py" not in _text(result), _text(result)
    assert kept.read_text() == "user notes\n"


def test_a_lane_root_entry_whose_git_is_a_file_is_not_a_placed_clone(tmp_path: Path):
    """A directory whose `.git` is a file (a linked worktree's pointer) is not a clone the run made, even at a
    recorded key and pin."""
    runner = make_cli_runner()
    lane = _multi_member_lane(tmp_path)
    pin = _placed_clone(tmp_path, lane, "demo-api")
    gitdir = tmp_path / "moved-gitdir"
    (lane / "demo-api" / ".git").rename(gitdir)
    (lane / "demo-api" / ".git").write_text(f"gitdir: {gitdir}\n")
    assert _git(lane / "demo-api", "rev-parse", "HEAD") == pin
    _run_receipt(lane, _selection({"demo-api": pin}))
    result = runner.invoke(gr2_app.app, ["review", "close", str(lane), "--json"])
    assert result.exit_code != 0 and "work at the lane root" in _text(result), _text(result)
    assert (lane / "demo-api" / ".git").is_file()


def _lane_whose_install_writes_build(tmp_path: Path, monkeypatch) -> Path:
    """The multi-member lane, with an install that also writes `build/lib/...` into the member the way a
    non-editable `pip install <member>` does."""
    from tests import test_review_run_multi_repo as multi
    writes_build = ("import pathlib,sys;b=pathlib.Path(sys.argv[2]).parent/'build'/'lib'/'demo_core';"
                    "b.mkdir(parents=True,exist_ok=True);(b/'__init__.py').write_text('copied\\n');")
    monkeypatch.setattr(multi, "PTH_SCRIPT", writes_build + multi.PTH_SCRIPT)
    return _multi_member_lane(tmp_path)


@pytest.mark.parametrize("after_the_run", [None, "demo-core/build/notes.txt"])
def test_a_run_records_its_install_outputs_and_close_admits_exactly_those(tmp_path: Path, monkeypatch,
                                                                         after_the_run):
    runner = make_cli_runner()
    lane = _lane_whose_install_writes_build(tmp_path, monkeypatch)
    receipt = review_run.run_review_lane(lane, pytest_args=["-q"])
    assert receipt["result"] == "green", receipt
    written = ["demo-core/build/lib/demo_core/__init__.py", "demo-web/build/lib/demo_core/__init__.py"]
    assert all((lane / rel).is_file() for rel in written)
    assert json.loads((lane / review_run._RECEIPT_NAME).read_text())["install_outputs"] == written
    if after_the_run:
        (lane / after_the_run).write_text("user notes\n")
    result = runner.invoke(gr2_app.app, ["review", "close", str(lane), "--json"])
    if after_the_run is None:
        assert result.exit_code == 0, _text(result)
        assert not lane.exists()
    else:
        assert result.exit_code != 0 and "build/notes.txt" in _text(result), _text(result)
        assert (lane / after_the_run).read_text() == "user notes\n"


def test_install_outputs_names_only_new_paths_under_build_lane_relative(tmp_path: Path):
    lane = tmp_path / "lane"
    member = lane / "demo-core"
    member.mkdir(parents=True)
    _git(member, "init", "-q", "-b", "main")
    (member / "build").mkdir()
    (member / "build" / "before.txt").write_text("x\n")
    baseline = review_run.list_untracked(member)
    for rel in ("build/lib/a.py", "notes.txt", "src/build/b.py"):
        (member / rel).parent.mkdir(parents=True, exist_ok=True)
        (member / rel).write_text("x\n")
    assert review_run.install_outputs(member, baseline, lane) == ["demo-core/build/lib/a.py"]
    assert review_run.install_outputs(lane / "demo-core", baseline, member) == ["build/lib/a.py"]


def test_a_symlinked_key_at_the_lane_root_is_not_a_placed_clone(tmp_path: Path):
    """A recorded key whose lane-root entry is a symlink to a real clone at the recorded pin is not the run's."""
    runner = make_cli_runner()
    lane = _multi_member_lane(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    pin = _placed_clone(tmp_path, outside, "demo-api")
    (lane / "demo-api").symlink_to(outside / "demo-api")
    _run_receipt(lane, _selection({"demo-api": pin}))
    result = runner.invoke(gr2_app.app, ["review", "close", str(lane), "--json"])
    assert result.exit_code != 0 and "work at the lane root" in _text(result), _text(result)
    assert (outside / "demo-api" / ".git").is_dir()


@pytest.mark.parametrize("key", [".GIT", ".Git", "git~1", "GIT~1", ".git.", ".git ", ".git::$INDEX_ALLOCATION",
                                 "\u200c.git", ".g\u200dit"])
def test_a_key_git_reads_as_dotgit_is_not_a_placed_clone(tmp_path: Path, key: str):
    """A recorded key fsck would refuse as a name for .git (any case, its NTFS forms, its HFS+ forms) is never the
    review's, even as a real clone at its pin; on a filesystem that reads it as .git it would be the lane's own."""
    if key.lower() == ".git":
        _skip_unless_case_sensitive(tmp_path)
    runner = make_cli_runner()
    lane = _multi_member_lane(tmp_path)
    pin = _placed_clone(tmp_path, lane, key)
    _run_receipt(lane, _selection({key: pin}))
    result = runner.invoke(gr2_app.app, ["review", "close", str(lane), "--json"])
    assert result.exit_code != 0 and "work at the lane root" in _text(result), _text(result)
    assert (lane / key / ".git").is_dir()


def test_a_key_named_dotgit_is_not_placed_at_the_lane_root(tmp_path: Path):
    """A lane root with a `.git` never reaches the placed-member read through close, so the read is asked
    directly: a clone at `.git` at its pin is not a placed member."""
    lane = tmp_path / "lane"
    lane.mkdir()
    pin = _placed_clone(tmp_path, lane, ".git")
    receipt = {"kind": "review-run", "downstream": _selection({".git": pin})}
    assert open_gr_review._run_placed_members(lane, receipt, set()) == []
    assert open_gr_review._run_placed_members(lane, {"kind": "review-run", "downstream": _selection(
        {"demo-api": _placed_clone(tmp_path, lane, "demo-api")})}, set()) == ["demo-api"]


def test_a_key_that_differs_from_a_member_only_in_case_is_not_placed(tmp_path: Path):
    """A case-insensitive filesystem reads `Demo-core` as the member `demo-core`, so the key is never placed."""
    _skip_unless_case_sensitive(tmp_path)
    runner = make_cli_runner()
    lane = _multi_member_lane(tmp_path)
    pin = _placed_clone(tmp_path, lane, "Demo-core")
    _run_receipt(lane, _selection({"Demo-core": pin}))
    result = runner.invoke(gr2_app.app, ["review", "close", str(lane), "--json"])
    assert result.exit_code != 0 and "work at the lane root" in _text(result), _text(result)
    assert "Demo-core" in _text(result), _text(result)
    assert (lane / "Demo-core" / ".git").is_dir()
    # Folded on both sides: a lowercase key beside a mixed-case member is refused the same way.
    receipt = {"kind": "review-run", "downstream": _selection({"Demo-core": pin})}
    assert open_gr_review._run_placed_members(lane, receipt, {"DEMO-CORE"}) == []
    assert open_gr_review._run_placed_members(lane, receipt, {"demo-web"}) == ["Demo-core"]


def test_two_keys_that_differ_only_in_case_are_neither_placed(tmp_path: Path):
    """Two recorded keys a case-insensitive filesystem reads as one directory: neither is the review's."""
    _skip_unless_case_sensitive(tmp_path)
    runner = make_cli_runner()
    lane = _multi_member_lane(tmp_path)
    pins = {key: _placed_clone(tmp_path, lane, key) for key in ("demo-api", "Demo-API")}
    _run_receipt(lane, _selection(pins))
    assert open_gr_review._run_placed_members(lane, json.loads(
        (lane / review_run._RECEIPT_NAME).read_text()), {"demo-core", "demo-web"}) == []
    result = runner.invoke(gr2_app.app, ["review", "close", str(lane), "--json"])
    assert result.exit_code != 0 and "work at the lane root" in _text(result), _text(result)
    assert (lane / "demo-api" / ".git").is_dir() and (lane / "Demo-API" / ".git").is_dir()


def test_a_mixed_case_key_with_no_twin_is_still_placed(tmp_path: Path):
    """Case alone refuses nothing: a mixed-case key, recorded once in each role, is the review's."""
    runner = make_cli_runner()
    lane = _multi_member_lane(tmp_path)
    pin = _placed_clone(tmp_path, lane, "Demo-API")
    selection = _selection({"Demo-API": pin})
    selection["upstream"] = ["Demo-API"]
    _run_receipt(lane, selection)
    result = runner.invoke(gr2_app.app, ["review", "close", str(lane), "--json"])
    assert result.exit_code == 0, _text(result)
    assert not lane.exists()


@pytest.mark.parametrize("key", [".grip-open-gr-reconstruct.json", ".GRIP-Open-GR-Reconstruct.JSON",
                                 "demo-core.grip-review-run.log", "Demo-Core.Grip-Review-Run.LOG",
                                 "demo-api.grip-review-run.log"])
def test_a_key_named_as_a_file_the_review_writes_is_not_placed(tmp_path: Path, key: str):
    """A recorded key a case-insensitive filesystem reads as a file the review writes at the lane root (its legacy
    marker, or the run log of a member or of another recorded key) is never the review's, even as a real clone at
    its pin; the plain keys recorded around it still are."""
    lane = _multi_member_lane(tmp_path)
    pins = {k: _placed_clone(tmp_path, lane, k) for k in ("demo-zz", key, "demo-api")}
    receipt = {"kind": "review-run", "downstream": _selection(pins)}
    assert open_gr_review._run_placed_members(lane, receipt, {"demo-core", "demo-web"}) == ["demo-zz", "demo-api"]
    _run_receipt(lane, _selection(pins))
    result = make_cli_runner().invoke(gr2_app.app, ["review", "close", str(lane), "--json"])
    assert result.exit_code != 0 and "work at the lane root" in _text(result), _text(result)
    assert (lane / key / ".git").is_dir()


@pytest.mark.parametrize("role", ["upstream", "downstream"])
@pytest.mark.parametrize("bad", [7, None, True, ["demo-api"], {"demo-api": 1}], ids=repr)
def test_a_receipt_naming_a_non_string_member_key_is_refused_as_corrupt(tmp_path: Path, bad, role):
    """A receipt is JSON anyone can edit: a list or dict where a member key belongs raised TypeError
    out of close, and an int, null or bool was dropped silently. Either way close refuses it, named,
    and nothing in the lane moves."""
    runner = make_cli_runner()
    lane = _multi_member_lane(tmp_path)
    pin = _placed_clone(tmp_path, lane, "demo-api")
    selection = _selection({"demo-api": pin})
    selection[role].append(bad)
    _run_receipt(lane, selection)
    result = runner.invoke(gr2_app.app, ["review", "close", str(lane), "--json"])
    assert result.exception is None or isinstance(result.exception, SystemExit), repr(result.exception)
    assert result.exit_code != 0, _text(result)
    assert "corrupt run receipt" in _text(result) and "Traceback" not in _text(result), _text(result)
    assert (lane / "demo-api" / ".git").is_dir() and (lane / review_run._RECEIPT_NAME).is_file()
    assert review_allocation.allocation_path(tmp_path / "workspace", lane).is_file()


_DEEP = b"[" * 100000 + b"]" * 100000


@pytest.mark.parametrize("raw", [b"[]\n", b"{not json\n", b'{"kind": "review-run", "result": "\xff"}\n', b'"review-run"\n',
                                 _DEEP, b'{"kind": "review-run", "result": ' + _DEEP + b"}\n"],
                         ids=["list", "not-json", "not-utf8", "string", "too-deep", "too-deep-in-an-object"])
def test_a_run_receipt_that_is_not_a_json_object_is_refused_as_corrupt(tmp_path: Path, raw: bytes):
    """Close read a receipt it could not parse as {} for the root check, then parsed it again to keep it and
    raised out of close with a traceback (JSONDecodeError, UnicodeDecodeError, AttributeError). Now it
    refuses the receipt by name before anything moves."""
    runner = make_cli_runner()
    lane = _multi_member_lane(tmp_path)
    (lane / review_run._RECEIPT_NAME).write_bytes(raw)
    result = runner.invoke(gr2_app.app, ["review", "close", str(lane), "--json"])
    assert result.exception is None or isinstance(result.exception, SystemExit), repr(result.exception)
    assert result.exit_code == 2, _text(result)
    assert "corrupt run receipt" in _text(result) and "Traceback" not in _text(result), _text(result)
    assert (lane / review_run._RECEIPT_NAME).read_bytes() == raw
    assert (lane / "demo-core" / ".git").exists() and (lane / "demo-web" / ".git").exists()
    assert review_allocation.allocation_path(tmp_path / "workspace", lane).is_file()


@pytest.mark.parametrize("field", ["gr_commit", "created"])
def test_a_receipt_whose_key_fields_are_not_strings_still_closes(tmp_path: Path, field: str):
    """An int gr_commit raised TypeError where close keys the kept evidence; a field that is not a string
    keys it by the default instead, and the receipt is kept."""
    runner = make_cli_runner()
    lane = _multi_member_lane(tmp_path)
    receipt = {"kind": "review-run", "created": "2026-10-13T00:00:00+00:00", "result": "green", field: 7}
    (lane / review_run._RECEIPT_NAME).write_text(json.dumps(receipt) + "\n")
    result = runner.invoke(gr2_app.app, ["review", "close", str(lane), "--json"])
    assert result.exit_code == 0, _text(result)
    assert not lane.exists()
    archive = review_allocation.allocation_path(tmp_path / "workspace", lane).with_suffix("")
    prefix = "unknown-" if field == "gr_commit" else "unknown-no-timestamp"
    kept = [p for p in archive.rglob(review_run._RECEIPT_NAME) if p.parent.name.startswith(prefix)]
    assert len(kept) == 1 and json.loads(kept[0].read_text())[field] == 7, list(archive.rglob("*"))


_MEMBER_LOG = "demo-web" + review_run._OUTPUT_LOG_NAME


@pytest.mark.parametrize("fields, carried", [
    ({"members": 7}, False),
    ({"members": [1, {"key": "demo-web", "output_log": _MEMBER_LOG}]}, True),
    ({"output_log": 7}, False),
    ({"members": [{"key": "demo-core", "output_log": 7}, {"key": "demo-web", "output_log": _MEMBER_LOG}]}, True),
], ids=["members-int", "members-entry-int", "output-log-int", "member-output-log-int"])
def test_a_receipt_whose_log_fields_are_the_wrong_type_still_closes(tmp_path: Path, fields: dict, carried: bool):
    """A receipt that parsed as an object but held members: 7, a member that is not an object, or an output_log
    that is not a string raised out of close after the archive dir was made. Each such field is skipped, as a
    wrong-typed gr_commit is: close exits 0, keeps the receipt byte for byte, and still carries a well-formed
    sibling's log."""
    runner = make_cli_runner()
    lane = _multi_member_lane(tmp_path)
    raw = (json.dumps({"kind": "review-run", "created": "2026-10-13T00:00:00+00:00", "result": "green",
                       "gr_commit": "abc", **fields}) + "\n").encode()
    (lane / review_run._RECEIPT_NAME).write_bytes(raw)
    (lane / _MEMBER_LOG).write_text("member log\n")
    result = runner.invoke(gr2_app.app, ["review", "close", str(lane), "--json"])
    assert result.exit_code == 0 and "Traceback" not in _text(result), _text(result)
    assert not lane.exists()
    archive = review_allocation.allocation_path(tmp_path / "workspace", lane).with_suffix("")
    kept = list(archive.rglob(review_run._RECEIPT_NAME + ".original"))
    assert len(kept) == 1 and kept[0].read_bytes() == raw, list(archive.rglob("*"))
    assert json.loads((kept[0].parent / review_run._RECEIPT_NAME).read_text())["kind"] == "review-run"
    log = kept[0].parent / _MEMBER_LOG
    assert (log.read_text() == "member log\n") if carried else not log.exists()


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root reads a mode-000 file")
def test_an_unreadable_run_receipt_is_refused_before_anything_is_written(tmp_path: Path):
    """A receipt close cannot read is corrupt, not missing: read as missing, close went on to copy it into the
    archive and raised PermissionError there, leaving a half-written archive dir. Now it refuses first."""
    runner = make_cli_runner()
    lane = _multi_member_lane(tmp_path)
    receipt = lane / review_run._RECEIPT_NAME
    receipt.write_text(json.dumps({"kind": "review-run", "gr_commit": "abc", "created": "c"}) + "\n")
    receipt.chmod(0)
    try:
        result = runner.invoke(gr2_app.app, ["review", "close", str(lane), "--json"])
    finally:
        receipt.chmod(0o644)
    assert result.exit_code == 2, _text(result)
    assert "corrupt run receipt" in _text(result) and "Traceback" not in _text(result), _text(result)
    assert (lane / "demo-core" / ".git").exists() and (lane / "demo-web" / ".git").exists()
    alloc = review_allocation.allocation_path(tmp_path / "workspace", lane)
    assert alloc.is_file() and not alloc.with_suffix("").exists(), list(alloc.parent.rglob("*"))


def test_a_corrupt_member_receipt_is_refused_before_the_root_receipt_is_archived(tmp_path: Path):
    """Close checked only the root receipt before archiving; a MEMBER receipt it could not parse was refused
    after the root copy was written, leaving a partial archive. Every receipt is now read before any copy."""
    runner = make_cli_runner()
    lane = _multi_member_lane(tmp_path)
    _run_receipt(lane)
    (lane / "demo-core" / review_run._RECEIPT_NAME).write_bytes(b"{not json\n")
    result = runner.invoke(gr2_app.app, ["review", "close", str(lane), "--json"])
    assert result.exit_code == 2, _text(result)
    assert "corrupt run receipt" in _text(result) and "Traceback" not in _text(result), _text(result)
    assert (lane / "demo-core" / ".git").exists() and (lane / "demo-web" / ".git").exists()
    alloc = review_allocation.allocation_path(tmp_path / "workspace", lane)
    assert alloc.is_file() and not alloc.with_suffix("").exists(), list(alloc.parent.rglob("*"))
