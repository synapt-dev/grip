"""`review show` and `review verify` on what `review create-project` writes (a project-review-KIND id).

show decodes it through `read_project_review_commit` and prints each member's repository, base and head.
verify refuses it by kind and points at `review open-project`: that reader checks the schema and each
member's fields, not the tree (a broken .grip/kind, an extra root entry or a junk objects/ tree still
reads), so verify has no integrity check to run on it and must not call it valid or corrupt. A bound
commit of neither kind is still corrupt from both verbs."""

import json
import re
import shlex
import subprocess
from pathlib import Path

from gr2.python_cli import app as appmod
from gr2.python_cli import grip
from tests.test_review_cli import _fixture_repo, _git, app, runner


def _workspace(tmp_path: Path, lane_commit: bool) -> tuple[Path, str]:
    remote, base, head, work = _fixture_repo(tmp_path, "adopted")
    ws = tmp_path / "workspace"
    ws.mkdir()
    _git(tmp_path, "clone", "--quiet", "--branch", "dev", remote, str(ws / "adopted"))
    assert runner.invoke(app, ["workspace", "init", str(ws)]).exit_code == 0
    assert runner.invoke(app, ["lane", "create", str(ws), "default", "review", "--repos", "adopted",
                               "--branch", "feat/review"]).exit_code == 0
    assert runner.invoke(app, ["lane", "enter", str(ws), "default", "review", "--actor", "agent:test"]).exit_code == 0
    lane = appmod._lane_repo_root(ws.resolve(), "default", "review", "adopted")
    if lane_commit:
        (lane / "g.txt").write_text("lane change\n")
        _git(lane, "add", ".")
        _git(lane, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-m", "feat: lane change")
    return ws, _git(lane, "rev-parse", "HEAD").strip()


def _create_project(ws: Path) -> str:
    project = runner.invoke(app, ["review", "create-project", str(ws), "default", "review"])
    assert project.exit_code == 0, project.output
    return project.stdout.split()[0]


def _gi(ws: Path, data: str, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=grip._bind_dir(ws), input=data, text=True,
                          capture_output=True, check=True).stdout.strip()


def _put(ws: Path, tree: str, parts: list[str], blob: str) -> str:
    entries = [e for e in grip._bind_git(ws, "ls-tree", tree).stdout.splitlines() if e]
    kept = [e for e in entries if e.split("\t", 1)[1] != parts[0]]
    if len(parts) == 1:
        kept.append(f"100644 blob {_gi(ws, blob, 'hash-object', '-w', '--stdin')}\t{parts[0]}")
    else:
        sub = [e.split()[2] for e in entries if e.split("\t", 1)[1] == parts[0]]
        kept.append(f"040000 tree {_put(ws, sub[0] if sub else _gi(ws, '', 'mktree'), parts[1:], blob)}\t{parts[0]}")
    return _gi(ws, "".join(e + "\n" for e in kept), "mktree")


def _bound_edit(ws: Path, gr: str, path: str, blob: str) -> str:
    """A bound copy of `gr` with one blob replaced, so the readers see it and not an unbound refusal."""
    tree = grip._bind_git(ws, "rev-parse", gr[3:] + "^{tree}").stdout.strip()
    new = _gi(ws, "", "commit-tree", _put(ws, tree, path.split("/"), blob), "-m", "edited")
    grip._bind_git(ws, "update-ref", grip._review_refs_for(new)[0], new)
    return f"gr:{new}"


def _assert_show(ws: Path, gr: str, base: str, head: str) -> None:
    shown = runner.invoke(app, ["review", "show", str(ws), gr])
    assert shown.exit_code == 0, shown.output
    assert f"{gr}  project review (1 member)" in shown.output
    assert f"adopted: {base[:12]}..{head[:12]}" in shown.output
    as_json = runner.invoke(app, ["review", "show", str(ws), gr, "--json"])
    assert as_json.exit_code == 0, as_json.output
    record = json.loads(as_json.stdout)
    assert record["id"] == gr and record["kind"] == "gr2-project-review/v1"
    assert [(m["key"], m["path"], m["base"], m["head"]) for m in record["members"]] == [("adopted", "adopted", base, head)]


def _assert_verify_refuses_by_kind(ws: Path, gr: str) -> None:
    verified = runner.invoke(app, ["review", "verify", str(ws), gr])
    assert verified.exit_code == 2, verified.output
    assert "refused: project_review_not_verified" in verified.output
    assert "gr2-project-review/v1" in verified.output and "review open-project" in verified.output
    assert "corrupt" not in verified.output and "tree_matches" not in verified.output
    assert "project review, valid" not in verified.output


def test_create_project_empty_lane_show_decodes_and_verify_refuses_by_kind(tmp_path: Path) -> None:
    ws, lane_head = _workspace(tmp_path, lane_commit=False)
    gr = _create_project(ws)
    _assert_show(ws, gr, lane_head, lane_head)
    _assert_verify_refuses_by_kind(ws, gr)


def test_create_project_with_lane_commit_show_decodes_and_verify_refuses_by_kind(tmp_path: Path) -> None:
    ws, lane_head = _workspace(tmp_path, lane_commit=True)
    gr = _create_project(ws)
    base = grip.read_project_review_commit(ws, gr[3:])[0]["base"]
    assert base != lane_head
    _assert_show(ws, gr, base, lane_head)
    _assert_verify_refuses_by_kind(ws, gr)


def test_project_review_with_a_broken_field_is_corrupt_from_show(tmp_path: Path) -> None:
    ws, _ = _workspace(tmp_path, lane_commit=False)
    broken = _bound_edit(ws, _create_project(ws), "repos/adopted/base", "nothex")
    shown = runner.invoke(app, ["review", "show", str(ws), broken])
    assert shown.exit_code == 2
    assert "corrupt: invalid project review repository tree: adopted" in shown.output


def test_bound_commit_of_neither_kind_is_corrupt_from_show_and_verify(tmp_path: Path) -> None:
    ws, _ = _workspace(tmp_path, lane_commit=False)
    neither = _bound_edit(ws, _create_project(ws), ".grip/schema", "gr2-something-else/v1")
    for verb in ("show", "verify"):
        result = runner.invoke(app, ["review", verb, str(ws), neither])
        assert result.exit_code == 2, (verb, result.output)
        assert "corrupt: not a gr2 pinned review commit" in result.output, (verb, result.output)


def _run_printed_open_project(ws: Path, output: str) -> None:
    """The printed open-project line, its placeholders filled from the fixture, gets past click's usage check."""
    found = re.search(r"gr2 review (open-project .*? --enter)", " ".join(output.split()))
    assert found, output
    fill = {"<workspace>": str(ws), "<owner_unit>": "default", "<lane>": "review2"}
    args = ["review"] + [fill.get(a, a) for a in shlex.split(found.group(1))]
    assert not [a for a in args if a.startswith("<")], args
    ran = runner.invoke(app, args, env={"COLUMNS": "200"})
    assert not (ran.exit_code == 2 and "Missing argument" in ran.output), ran.output
    assert "Usage:" not in ran.output, ran.output


def test_printed_open_project_lines_run_past_usage(tmp_path: Path) -> None:
    ws, _ = _workspace(tmp_path, lane_commit=False)
    gr = _create_project(ws)
    for args in (["review", "show", str(ws), gr, "--diff"], ["review", "verify", str(ws), gr]):
        refused = runner.invoke(app, args, env={"COLUMNS": "200"})
        assert refused.exit_code == 2, refused.output
        assert f"open-project '<workspace>' {gr} '<owner_unit>' '<lane>' --enter" in refused.output
        assert "own remote first" in refused.output
        _run_printed_open_project(ws, refused.output)
