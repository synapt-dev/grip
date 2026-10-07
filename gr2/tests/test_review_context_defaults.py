"""Explicit intent and marker provenance beside the certified bare-command rows."""
import json
import os

import pytest

from gr2.python_cli import app as app_mod
from tests.test_pr_review_subject import KEYS, URL, git, gr2, reviewed  # noqa: F401
from tests.test_review_context_witnesses import opened  # noqa: F401


@pytest.mark.parametrize("verb", ["checks", "view"])
def test_review_pr_reader_missing_group_names_subject(reviewed, monkeypatch, verb):
    calls = []
    monkeypatch.setattr(app_mod.platform_ops, "get_platform_adapter", lambda *a: calls.append(a))
    result = gr2(reviewed["author"], monkeypatch, "pr", verb, "--json")
    assert result.exit_code != 0
    diagnostic = result.output + str(result.exception)
    assert f"no PR group for review {reviewed['target']}" in diagnostic
    assert "gr2 pr create" in diagnostic
    assert calls == []


@pytest.mark.parametrize("verb", ["checks", "view"])
def test_review_pr_reader_invalid_explicit_never_uses_sole_bind(reviewed, monkeypatch, verb):
    calls = []
    monkeypatch.setattr(app_mod.platform_ops, "get_platform_adapter", lambda *a: calls.append(a))
    result = gr2(reviewed["author"], monkeypatch, "pr", verb, "--review", "gr:not-a-bind", "--json")
    assert result.exit_code == 2
    assert "not_bound" in result.output
    assert calls == []


@pytest.mark.parametrize("verb", ["checks", "view"])
def test_review_pr_reader_legacy_unit_form_stays_explicit(reviewed, monkeypatch, verb):
    result = gr2(reviewed["author"], monkeypatch, "pr", verb, reviewed["author"], "nobody")
    assert result.exit_code != 0
    assert "no current lane recorded for unit: nobody" in result.output + str(result.exception)


@pytest.mark.parametrize("verb", ["show", "verify"])
def test_lane_reader_uses_marked_bind_even_when_author_has_two(opened, monkeypatch, verb):
    # Produce a second bind through the existing writer; do not forge a bind ref.
    first = gr2(opened["author"], monkeypatch, "review", "show", opened["author"], opened["target"], "--json")
    assert first.exit_code == 0, first.output
    row = next(m for m in json.loads(first.stdout)["members"] if m["key"] == "alpha")
    member = opened["author"] / row["path"]
    # The fixture pushed the first head for PR-create controls. The second bind
    # therefore needs a fresh local-only head, not a fabricated ratify receipt.
    git(member, "commit", "--allow-empty", "-m", "another review subject")
    new_head = git(member, "rev-parse", "HEAD")
    second = gr2(opened["author"], monkeypatch, "review", "bind", opened["author"],
                 "--repo", row["key"], "--remote", row["remote"], "--base", row["base"],
                 "--head", new_head, "--ref", git(member, "symbolic-ref", "HEAD"), "--path", row["path"],
                 "--source", member, "--title", "another subject")
    assert second.exit_code == 0, second.output
    assert second.stdout.strip() != opened["target"]
    cwd = opened["lane"] / "alpha"
    result = gr2(cwd, monkeypatch, "review", verb, "--json")
    explicit = gr2(cwd, monkeypatch, "review", verb, opened["author"], opened["target"], "--json")
    assert result.exit_code == explicit.exit_code == 0, result.output + explicit.output
    assert json.loads(result.stdout) == json.loads(explicit.stdout)


@pytest.mark.parametrize("verb", ["show", "verify"])
def test_lane_reader_invalid_explicit_target_does_not_use_marker(opened, monkeypatch, verb):
    result = gr2(opened["lane"] / "alpha", monkeypatch, "review", verb, "gr:not-a-bind", "--json")
    assert result.exit_code == 2 and "not_bound" in result.output


@pytest.mark.parametrize("verb", ["show", "verify", "close"])
def test_malformed_marker_refuses_without_ambient_fallback(opened, monkeypatch, verb):
    # Deliberate corrupted marker negative; the positive marker is production-written.
    marker = opened["lane"] / ".grip-review-open.json"
    marker.write_text("not-json\n")
    result = gr2(opened["lane"] / "alpha", monkeypatch, "review", verb, "--json")
    assert result.exit_code == 2 and "bad_marker" in result.output
    assert opened["lane"].is_dir()


@pytest.mark.parametrize("verb", ["show", "verify", "close"])
def test_nested_markers_refuse_to_choose_a_lane(opened, monkeypatch, verb):
    # Synthetic extra marker is a deliberate ambiguity, not positive setup.
    parent_marker = opened["lane"].parent / ".grip-review-open.json"
    parent_marker.write_text(json.dumps(opened["marker"]))
    result = gr2(opened["lane"] / "alpha", monkeypatch, "review", verb, "--json")
    assert result.exit_code == 2 and "ambiguous_review_context" in result.output
    assert opened["lane"].exists()


def test_close_from_member_reclaims_marked_parent_only(opened, monkeypatch):
    sibling = opened["lane"].parent / "unrelated"
    sibling.mkdir()
    sentinel = sibling / "keep.txt"
    sentinel.write_text("keep\n")
    result = gr2(opened["lane"] / "alpha", monkeypatch, "review", "close", "--json")
    assert result.exit_code == 0, result.output
    assert not opened["lane"].exists()
    assert sentinel.read_text() == "keep\n"
    assert all((opened["author"] / key / "p.txt").is_file() for key in ("alpha", "beta"))


def test_close_outside_lane_names_missing_context(tmp_path, monkeypatch):
    result = gr2(tmp_path, monkeypatch, "review", "close", "--json")
    assert result.exit_code == 2
    assert "no_review_context" in result.output and "pass TARGET" in result.output


def test_close_invalid_explicit_path_does_not_fall_back(opened, monkeypatch):
    result = gr2(opened["lane"], monkeypatch, "review", "close", opened["lane"] / "missing", "--json")
    assert result.exit_code != 0 and opened["lane"].exists()


def test_explicit_close_from_author_preserves_author_and_sibling(opened, monkeypatch):
    sibling = opened["lane"].parent / "unrelated"
    sibling.mkdir()
    sentinel = sibling / "keep.txt"
    sentinel.write_text("keep\n")
    result = gr2(opened["author"], monkeypatch, "review", "close", opened["lane"], "--json")
    assert result.exit_code == 0, result.output
    assert not opened["lane"].exists() and sentinel.read_text() == "keep\n"
    assert all((opened["author"] / key / "p.txt").is_file() for key in KEYS)


@pytest.mark.parametrize("verb", ["publish", "receive"])
def test_transport_keeps_independent_id_required_inside_lane(opened, monkeypatch, verb):
    result = gr2(opened["lane"], monkeypatch, "review", verb, "-C", opened["author"],
                 "--remote", str(opened["bare"]["alpha"]))
    assert result.exit_code == 2
    assert "Missing argument" in result.output and "commit" in result.output


def test_marker_workspace_wins_over_different_nearest_native_workspace(reviewed, monkeypatch):
    workspace = reviewed["author"]
    ambient = reviewed["tmp"] / "ambient"
    ambient.mkdir()
    for key in KEYS:
        git(ambient, "clone", "--branch", "main", reviewed["bare"][key], key)
    initialized = gr2(ambient, monkeypatch, "store", "init", ambient)
    assert initialized.exit_code == 0, "fixture ambient store failed: " + initialized.output
    assert app_mod._is_workspace_root(ambient)
    # Only the fixture's isolated global config is changed for reconstruction.
    for key in KEYS:
        git(workspace, "config", "--file", os.environ["GIT_CONFIG_GLOBAL"],
            f"url.file://{reviewed['bare'][key]}.insteadOf", URL[key])
    lane = ambient / "opened"
    result = gr2(workspace, monkeypatch, "review", "open", "--lane-dir", lane, "--json")
    assert result.exit_code == 0, "fixture nested open failed: " + result.output
    marker = json.loads((lane / ".grip-review-open.json").read_text())
    assert marker["workspace_root"] == str(workspace) and marker["gr_commit"] == reviewed["target"][3:]
    monkeypatch.chdir(lane / "alpha")
    assert app_mod._resolve_workspace_root() == ambient.resolve(), "fixture must expose a different nearest root"
    result = gr2(lane / "alpha", monkeypatch, "review", "show", "--json")
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["id"] == reviewed["target"]
    assert {m["key"]: m["head"] for m in payload["members"]} == reviewed["heads"]


@pytest.mark.parametrize("field,value,code", [
    ("workspace_root", "relative/workspace", "bad_review_workspace"),
    ("workspace_root", None, "bad_review_workspace"),
    ("gr_commit", "a" * 12, "bad_review_commit"),
])
def test_marker_bad_subject_fields_refuse_instead_of_sole_bind(opened, monkeypatch, field, value, code):
    marker = dict(opened["marker"])
    if value is None:
        marker.pop(field)
    else:
        marker[field] = value
    (opened["lane"] / ".grip-review-open.json").write_text(json.dumps(marker))
    result = gr2(opened["lane"], monkeypatch, "review", "show", "--json")
    assert result.exit_code == 2 and code in result.output


def test_publish_missing_independent_id_inside_lane_names_commit(opened, monkeypatch):
    result = gr2(opened["lane"], monkeypatch, "review", "publish",
                 "--remote", str(opened["bare"]["alpha"]))
    assert result.exit_code == 2
    assert "Missing argument 'commit'" in result.output


def test_review_view_unknown_repo_filter_names_members(reviewed, monkeypatch):
    root = reviewed["author"]
    created = gr2(root, monkeypatch, "pr", "create", "--json")
    assert created.exit_code == 0, created.output
    result = gr2(root, monkeypatch, "pr", "view", "--repo", "o/unknown", "--json")
    assert result.exit_code == 1
    assert "--repo o/unknown is not a member of this change" in result.output
    assert all(f"o/{key}" in result.output for key in KEYS)


def test_show_help_ignores_broken_enclosing_marker(opened, monkeypatch):
    (opened["lane"] / ".grip-review-open.json").write_text("not-json\n")
    result = gr2(opened["lane"], monkeypatch, "review", "show", "--help")
    assert result.exit_code == 0, result.output
    assert "Usage:" in result.output and "show" in result.output
    assert "bad_marker" not in result.output
