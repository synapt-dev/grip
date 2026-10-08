"""PR readers follow a production-opened reconstruction, including beneath a decoy workspace."""

import json
import os
from types import SimpleNamespace
import pytest
from tests.review_ref_helper import REVIEW_REF_ROOT
from gr2.python_cli import app as app_mod
from gr2.python_cli import pr as pr_ops
from tests.test_pr_review_subject import KEYS, URL, git, gr2, reviewed
from tests.test_review_context_witnesses import opened


def group(root, target, monkeypatch):
    created = gr2(root, monkeypatch, "pr", "create", "--review", target, "--json")
    assert created.exit_code == 0, created.output
    return pr_ops.review_pr_groups(root, target)[0][1]


def read(root, monkeypatch, reviewed, verb, *args):
    calls = []
    adapter = reviewed["adapter"]
    if verb == "view":
        adapter.pr_view = lambda repo, number: (calls.append((repo, number)) or SimpleNamespace(
            as_dict=lambda: dict(repo=repo, number=number)))
    else:
        adapter.pr_checks = lambda repo, number: (calls.append((repo, number)) or [])
    result = gr2(root, monkeypatch, "pr", verb, *args, "--json")
    return result, calls


def assert_group(result, calls, expected, verb):
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["pr_group_id"] == expected["pr_group_id"]
    numbers = {(p["repo"], p["pr_number"]) for p in expected["prs"]}
    assert set(calls) == numbers
    assert {(p["repo"], p["number"]) for p in payload["members" if verb == "view" else "checks"]} == numbers


@pytest.mark.parametrize("verb", ["checks", "view"])
def test_nested_ambient_pr_reader_uses_marked_group(reviewed, monkeypatch, verb):
    author = reviewed["author"]
    expected = group(author, reviewed["target"], monkeypatch)
    ambient = reviewed["tmp"] / "ambient"
    ambient.mkdir()
    for key in KEYS:
        git(ambient, "clone", "--branch", "main", reviewed["bare"][key], key)
        git(ambient / key, "remote", "set-url", "origin", URL[key])
    init = gr2(ambient, monkeypatch, "store", "init", ambient)
    assert init.exit_code == 0, init.output
    for key in KEYS:
        git(ambient, "config", "--local", f"url.file://{reviewed['bare'][key]}.insteadOf", URL[key])
        git(ambient / key, "switch", "-c", "feat/decoy")
        git(ambient / key, "commit", "--allow-empty", "-m", "decoy review")
    bound = gr2(ambient, monkeypatch, "review", "bind")
    assert bound.exit_code == 0, bound.output
    decoy_target = bound.stdout.strip()
    for key in KEYS:
        git(ambient / key, "push", reviewed["bare"][key], "feat/decoy")
    decoy = group(ambient, decoy_target, monkeypatch)
    assert decoy["pr_group_id"] != expected["pr_group_id"]
    assert {p["pr_number"] for p in decoy["prs"]}.isdisjoint({p["pr_number"] for p in expected["prs"]})
    for key in KEYS:
        git(author, "config", "--file", os.environ["GIT_CONFIG_GLOBAL"],
            f"url.file://{reviewed['bare'][key]}.insteadOf", URL[key])
    lane = ambient / "opened"
    result = gr2(author, monkeypatch, "review", "open", "--lane-dir", lane, "--json")
    assert result.exit_code == 0, result.output
    cwd = lane / "alpha"
    monkeypatch.chdir(cwd)
    assert app_mod._resolve_workspace_root() == ambient.resolve()
    result, calls = read(cwd, monkeypatch, reviewed, verb)
    assert_group(result, calls, expected, verb)
    # An explicit root bypasses the marker and selects the requested decoy.
    result, calls = read(cwd, monkeypatch, reviewed, verb, "-C", ambient)
    assert_group(result, calls, decoy, verb)


@pytest.mark.parametrize("verb", ["checks", "view"])
def test_outside_author_with_second_bind_keeps_marked_pr_subject(opened, monkeypatch, verb):
    author = opened["author"]
    expected = group(author, opened["target"], monkeypatch)
    member = author / "alpha"
    git(member, "commit", "--allow-empty", "-m", "second subject")
    second = gr2(author, monkeypatch, "review", "bind", "--members", "alpha")
    assert second.exit_code == 0 and second.stdout.strip() != opened["target"], second.output
    result, calls = read(opened["lane"] / "alpha", monkeypatch, opened, verb)
    assert_group(result, calls, expected, verb)
    result, calls = read(opened["lane"], monkeypatch, opened, verb, "--review", "gr:not-a-bind")
    assert result.exit_code == 2 and "not_bound" in result.output and calls == []


@pytest.mark.parametrize("verb", ["checks", "view"])
@pytest.mark.parametrize("spelling", ["v1-only", "duplicate"])
def test_pr_reader_infers_one_bind_across_known_spellings(reviewed, monkeypatch, verb, spelling):
    author, target = reviewed["author"], reviewed["target"]
    expected = group(author, target, monkeypatch)
    sha = target[3:]
    prefix = REVIEW_REF_ROOT
    # Deliberate alternate writer spelling, same production-created bind.
    git(author, "update-ref", prefix + "v1/" + sha, sha)
    if spelling == "v1-only":
        git(author, "update-ref", "-d", prefix + sha)
    else:
        git(author, "update-ref", prefix + sha, sha)
    result, calls = read(author, monkeypatch, reviewed, verb)
    assert_group(result, calls, expected, verb)


@pytest.mark.parametrize("verb", ["checks", "view"])
def test_pr_reader_refuses_bad_marker_before_adapter(opened, monkeypatch, verb):
    (opened["lane"] / ".grip-review-open.json").write_text("not-json\n")
    result, calls = read(opened["lane"], monkeypatch, opened, verb)
    assert result.exit_code == 2 and "bad_marker" in result.output and calls == []
