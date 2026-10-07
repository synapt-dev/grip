"""Unimplemented context gaps on 56fc49f4; real binds/lanes, fake PR transport.

These are intentionally red until the next admitted implementation. No handwritten
review rows, group documents, IDs, or lane markers are used to arrange the subject.
"""
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from gr2.python_cli import app as app_mod
from gr2.python_cli import pr as pr_ops
from tests.test_pr_review_subject import KEYS, URL, git, gr2, reviewed  # noqa: F401


def observed(name, cwd, argv, result, **facts):
    """Optional raw evidence sink, independent of the success assertion."""
    directory = os.environ.get("FATHOM_WITNESS_OUTPUT")
    if directory:
        out = Path(directory)
        out.mkdir(parents=True, exist_ok=True)
        (out / (name + ".json")).write_text(json.dumps(dict(
            cwd=str(cwd), argv=argv, exit_code=result.exit_code,
            stdout=result.stdout, stderr=result.stderr,
            exception=repr(result.exception), **facts,
        ), indent=2) + "\n")


@pytest.fixture
def opened(reviewed, monkeypatch):
    author = reviewed["author"]
    # Reconstruction clones outside the workspace. Only after store init and bind
    # recorded canonical remotes, give that clone the same isolated local transport.
    # This file belongs to the fixture; no human/global Git configuration is touched.
    for key in KEYS:
        git(author, "config", "--file", os.environ["GIT_CONFIG_GLOBAL"],
            f"url.file://{reviewed['bare'][key]}.insteadOf", URL[key])
    result = gr2(author, monkeypatch, "review", "open", "--json")
    assert result.exit_code == 0, "fixture open failed: " + result.output
    rows = json.loads(result.stdout)
    assert set(rows) == set(KEYS)
    lane = Path(rows["alpha"]["lane"]).parent
    marker = json.loads((lane / ".grip-review-open.json").read_text())
    assert marker["workspace_root"] == str(author)
    assert marker["gr_commit"] == reviewed["target"][3:]
    assert {r["key"]: r["bound_head"] for r in marker["repos"]} == reviewed["heads"]
    assert all(r["tree_match"] for r in marker["repos"])
    return dict(**reviewed, lane=lane, marker=marker)


@pytest.mark.parametrize("verb", ["checks", "view"])
def test_bare_pr_reader_uses_current_review_group(reviewed, monkeypatch, verb):
    root = reviewed["author"]
    created = gr2(root, monkeypatch, "pr", "create", "--json")
    assert created.exit_code == 0, "fixture create failed: " + created.output
    target, members = app_mod.resolve_review_subject(root, None)
    assert target == reviewed["target"]
    assert {m["key"]: m["commit"] for m in members} == reviewed["heads"]
    groups = pr_ops.review_pr_groups(root, target)
    assert len(groups) == 1
    group = groups[0][1]
    # This is only the platform detail response; group membership is production-owned.
    reviewed["adapter"].pr_view = lambda repo, number: SimpleNamespace(
        as_dict=lambda: dict(repo=repo, number=number))
    argv = ["pr", verb, "--json"]
    result = gr2(root, monkeypatch, *argv)
    observed("pr_" + verb, root, argv, result, target=target, group=group)
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["pr_group_id"] == group["pr_group_id"]
    rows = payload["checks" if verb == "checks" else "members"]
    assert {r["repo"]: r["number"] for r in rows} == {
        p["repo"]: p["pr_number"] for p in group["prs"]}


@pytest.mark.parametrize("verb", ["show", "verify"])
def test_bare_review_reader_inside_lane_uses_marker(opened, monkeypatch, verb):
    lane = opened["lane"]
    explicit = gr2(lane, monkeypatch, "review", verb, opened["author"], opened["target"], "--json")
    assert explicit.exit_code == 0, "fixture explicit reader failed: " + explicit.output
    argv = ["review", verb, "--json"]
    result = gr2(lane, monkeypatch, *argv)
    observed("review_" + verb, lane, argv, result, marker=opened["marker"],
             explicit=json.loads(explicit.stdout))
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == json.loads(explicit.stdout)


def test_bare_review_close_inside_lane_uses_marker(opened, monkeypatch):
    lane = opened["lane"]
    argv = ["review", "close", "--json"]
    result = gr2(lane, monkeypatch, *argv)
    observed("review_close", lane, argv, result, marker=opened["marker"])
    assert result.exit_code == 0, result.output
    assert not lane.exists()
    assert all((opened["author"] / key / "p.txt").is_file() for key in KEYS)


def test_explicit_readers_inside_lane_control(opened, monkeypatch):
    """Passing base control: marker/subject/reader fixture is valid before inference."""
    for verb in ("show", "verify"):
        argv = ["review", verb, str(opened["author"]), opened["target"], "--json"]
        result = gr2(opened["lane"], monkeypatch, *argv)
        observed("control_" + verb, opened["lane"], argv, result, marker=opened["marker"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)
        if verb == "show":
            assert payload["id"] == opened["target"]
            assert {r["key"]: r["head"] for r in payload["members"]} == opened["heads"]
        else:
            assert payload["tree_matches"] is True
