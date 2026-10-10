"""Adapter API v2: a member's routing target survives creation and the whole lifecycle.

The smallest proof: two members whose Git remotes resolve to DIFFERENT hosting
targets are created through a v2 adapter, and every later lifecycle call (status, merge) receives
each member's OWN target, not a repo string the adapter would have to guess from. A group created
without a remote cannot be routed, so a v2 adapter refuses it by name before it is called. A v1
adapter (no declared version) is untouched: it never sees `target`.

No network, no real repositories: the adapters are doubles that record what they were handed.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import typer
from gr2.python_cli.merge_verification import MergeVerificationTarget
from gr2.python_cli.platform import (
    AdapterError,
    CreatePRRequest,
    MergeMethod,
    MergeReceipt,
    PRRef,
    PRStatus,
    RemoteTarget,
)
from gr2.python_cli.pr import check_pr_group_status, create_pr_group, merge_pr_group

REMOTES = {
    "app": "https://dev.azure.com/acme/web/_git/app",
    "lib": "https://github.com/acme/lib.git",
}


def _parse(remote: str) -> RemoteTarget:
    """What a v2 adapter owns: turning a raw URL into a target. gr2 never parses it."""
    if "dev.azure.com" in remote:
        _, _, rest = remote.partition("dev.azure.com/")
        org, project, _git, repo = rest.split("/")
        return RemoteTarget(raw=remote, host="dev.azure.com", org=org, project=project, repo=repo)
    _, _, rest = remote.partition("github.com/")
    org, repo = rest.removesuffix(".git").split("/")
    return RemoteTarget(raw=remote, host="github.com", org=org, project=None, repo=repo)


class _V2:
    name = "fake"
    platform_adapter_api_version = 2

    def __init__(self) -> None:
        self.created: list[CreatePRRequest] = []
        self.status_targets: dict[str, RemoteTarget | None] = {}
        self.merge_targets: dict[str, RemoteTarget | None] = {}
        self.calls: list[str] = []

    def resolve_target(self, remote: str) -> RemoteTarget:
        return _parse(remote)

    def create_pr(self, request: CreatePRRequest) -> PRRef:
        self.calls.append("create_pr")
        self.created.append(request)
        return PRRef(repo=request.repo, number=len(self.created), url=f"fake://{request.repo}")

    def edit_pr_body(self, repo, number, body, *, target=None) -> None:
        self.calls.append("edit_pr_body")

    def pr_status(self, repo, number, *, target=None) -> PRStatus:
        self.calls.append("pr_status")
        self.status_targets[repo] = target
        return PRStatus(ref=PRRef(repo=repo, number=number), state="OPEN")

    def merge_pr(self, repo, number, *, method, expected_head=None, target=None) -> MergeReceipt:
        self.calls.append("merge_pr")
        self.merge_targets[repo] = target
        return MergeReceipt(
            requested=PRRef(repo=repo, number=number),
            observed=PRRef(repo=repo, number=number, url=f"observed://{repo}/{number}"),
            commit_sha=None,
            requested_method=method,
        )


class _V1:
    """No declared version: the protocol every adapter shipped so far. It must never see `target`."""

    name = "legacy"

    def __init__(self) -> None:
        self.status_calls: list[tuple] = []
        self.created: list[CreatePRRequest] = []

    def create_pr(self, request: CreatePRRequest) -> PRRef:
        self.created.append(request)
        return PRRef(repo=request.repo, number=len(self.created), url=f"legacy://{request.repo}")

    def edit_pr_body(self, repo, number, body) -> None:
        return None

    def pr_status(self, repo, number) -> PRStatus:  # no `target` keyword: a call that passes one is a TypeError
        self.status_calls.append((repo, number))
        return PRStatus(ref=PRRef(repo=repo, number=number), state="OPEN")


def _create(tmp_path: Path, adapter: object) -> dict:
    return create_pr_group(
        workspace_root=tmp_path,
        owner_unit="u",
        lane_name="l",
        title="t",
        base_branch="main",
        head_branch="feat",
        repos=["app", "lib"],
        adapter=adapter,  # type: ignore[arg-type]
        actor="a",
        remotes=REMOTES,
    )


def _stored(tmp_path: Path, group: dict) -> dict:
    return json.loads((tmp_path / ".grip" / "pr_groups" / f"{group['pr_group_id']}.json").read_text())


def test_each_member_is_created_with_its_own_typed_target_and_the_group_stores_it(tmp_path):
    adapter = _V2()
    group = _create(tmp_path, adapter)
    by_repo = {r.repo: r for r in adapter.created}
    assert by_repo["app"].target == RemoteTarget(
        raw=REMOTES["app"], host="dev.azure.com", org="acme", project="web", repo="app"
    )
    assert by_repo["lib"].target.host == "github.com" and by_repo["lib"].target.project is None
    assert by_repo["app"].remote == REMOTES["app"]  # the raw URL stays beside the target
    stored = {m["repo"]: m for m in _stored(tmp_path, group)["prs"]}
    assert stored["app"]["target"] == {
        "raw": REMOTES["app"], "host": "dev.azure.com", "org": "acme", "project": "web", "repo": "app",
    }
    assert stored["lib"]["target"]["host"] == "github.com"


def test_status_gives_each_member_its_own_target(tmp_path):
    adapter = _V2()
    group = _create(tmp_path, adapter)
    check_pr_group_status(tmp_path, group["pr_group_id"], adapter, "a")
    assert adapter.status_targets["app"].project == "web"
    assert adapter.status_targets["lib"].org == "acme" and adapter.status_targets["lib"].host == "github.com"


def test_merge_gives_each_member_its_own_target(tmp_path):
    adapter = _V2()
    group = _create(tmp_path, adapter)
    merge_pr_group(
        workspace_root=tmp_path,
        pr_group_id=group["pr_group_id"],
        adapter=adapter,  # type: ignore[arg-type]
        actor="a",
        method=MergeMethod.MERGE,
        verification_targets={
            repo: MergeVerificationTarget(repo_root=tmp_path / repo, remote=REMOTES[repo]) for repo in REMOTES
        },
        report=lambda _msg: None,
    )
    assert adapter.merge_targets["app"].project == "web"
    assert adapter.merge_targets["lib"].host == "github.com"


def test_a_v2_adapter_refuses_an_old_group_with_no_remote_before_any_call(tmp_path):
    state = tmp_path / ".grip" / "pr_groups"
    state.mkdir(parents=True)
    (state / "pg_old.json").write_text(
        json.dumps({"pr_group_id": "pg_old", "owner_unit": "u", "lane_name": "l",
                    "prs": [{"repo": "app", "pr_number": 1}, {"repo": "lib", "pr_number": 2}]})
    )
    adapter = _V2()
    with pytest.raises(AdapterError, match="group_lacks_routing_context: app"):
        check_pr_group_status(tmp_path, "pg_old", adapter, "a")
    assert adapter.calls == []  # refused before the adapter was touched


def test_a_v1_adapter_is_untouched_and_never_sees_a_target(tmp_path):
    adapter = _V1()
    group = _create(tmp_path, adapter)
    assert all(r.target is None for r in adapter.created)  # a v1 adapter is not asked to resolve
    check_pr_group_status(tmp_path, group["pr_group_id"], adapter, "a")
    assert adapter.status_calls == [("app", 1), ("lib", 2)]
    assert all("target" not in m for m in _stored(tmp_path, group)["prs"])


# --- the CLI verbs and the review preflight ----------------------------------------------------------


class _V2Read(_V2):
    """Adds the read verbs the CLI calls, recording the target each was routed with."""

    def __init__(self) -> None:
        super().__init__()
        self.view_targets: dict[str, RemoteTarget | None] = {}
        self.check_targets: dict[str, RemoteTarget | None] = {}

    def pr_view(self, repo, number, *, target=None):
        from gr2.python_cli.platform import PRDetail

        self.view_targets[repo] = target
        return PRDetail(ref=PRRef(repo=repo, number=number), state="OPEN")

    def pr_checks(self, repo, number, *, target=None):
        self.check_targets[repo] = target
        return []


@pytest.fixture
def cli(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from gr2.python_cli import app as gr2_app

    adapter = _V2Read()
    monkeypatch.setattr(gr2_app.platform_ops, "get_platform_adapter", lambda name: adapter)
    monkeypatch.setattr(gr2_app, "_resolve_lane_name", lambda root, unit, lane: "l")
    group = _create(tmp_path, adapter)
    runner = CliRunner()

    def run(*argv):
        return runner.invoke(gr2_app.app, ["pr", *argv, str(tmp_path), "u", "l", "--json"])

    return adapter, group, run


def test_pr_status_checks_and_view_route_every_member_by_its_own_target(cli):
    adapter, _group, run = cli
    for verb in ("status", "checks", "view"):
        done = run(verb)
        assert done.exit_code == 0, (verb, done.output)
    assert adapter.status_targets["app"].project == "web" and adapter.status_targets["lib"].project is None
    assert adapter.check_targets["app"].org == "acme" and adapter.check_targets["lib"].host == "github.com"
    assert adapter.view_targets["app"].repo == "app" and adapter.view_targets["lib"].repo == "lib"


def test_pr_view_names_a_member_a_v2_adapter_cannot_route_instead_of_reading_it_blind(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from gr2.python_cli import app as gr2_app

    adapter = _V2Read()
    monkeypatch.setattr(gr2_app.platform_ops, "get_platform_adapter", lambda name: adapter)
    monkeypatch.setattr(gr2_app, "_resolve_lane_name", lambda root, unit, lane: "l")
    state = tmp_path / ".grip" / "pr_groups"
    state.mkdir(parents=True)
    (state / "pg_old.json").write_text(
        json.dumps({"pr_group_id": "pg_old", "owner_unit": "u", "lane_name": "l",
                    "prs": [{"repo": "app", "pr_number": 1}]})
    )
    done = CliRunner().invoke(gr2_app.app, ["pr", "view", str(tmp_path), "u", "l", "--json"])
    assert done.exit_code == 0, done.output
    rows = json.loads(done.output)
    rows = rows["members"] if isinstance(rows, dict) and "members" in rows else rows
    assert "group_lacks_routing_context: app" in json.dumps(rows)
    assert adapter.view_targets == {}  # never read through a guessed repo string


def test_member_slug_is_github_for_v1_and_adapter_owned_for_v2():
    from gr2.python_cli import app as gr2_app

    assert gr2_app._member_slug(None, "https://github.com/acme/lib.git") == "acme/lib"
    assert gr2_app._member_slug(None, REMOTES["app"]) is None  # the old GitHub-only reading, unchanged
    assert gr2_app._member_slug(_V2(), REMOTES["app"]) == "dev.azure.com/acme/web/app"


def test_member_slug_is_none_when_the_adapter_cannot_resolve_the_remote():
    from gr2.python_cli import app as gr2_app

    class _Refuses(_V2):
        def resolve_target(self, remote):
            raise AdapterError(f"cannot resolve {remote}")

    assert gr2_app._member_slug(_Refuses(), "not a url at all") is None


def test_review_members_on_host_accepts_a_non_github_remote_only_through_a_v2_adapter(tmp_path):
    """The review preflight used to refuse any remote that was not github.com/owner/repo, which is the
    break an Azure DevOps user hit. With a v2 adapter the adapter resolves it; without one nothing changes."""
    from gr2.python_cli import app as gr2_app

    (tmp_path / "app").mkdir()
    member = {"key": "app", "remote": REMOTES["app"], "path": "app", "commit": "a" * 40}
    on_host = gr2_app._review_members_on_host(tmp_path.resolve(), [member], _V2())
    assert list(on_host) == ["dev.azure.com/acme/web/app"]
    with pytest.raises(typer.Exit):  # the legacy reading still refuses a non-GitHub remote
        gr2_app._review_members_on_host(tmp_path.resolve(), [member], None)


def test_a_remote_that_now_resolves_to_a_different_target_is_refused_before_any_call(tmp_path):
    adapter = _V2()
    group = _create(tmp_path, adapter)
    path = tmp_path / ".grip" / "pr_groups" / f"{group['pr_group_id']}.json"
    doc = json.loads(path.read_text())
    doc["prs"][0]["target"]["project"] = "elsewhere"  # the target this group was created with
    path.write_text(json.dumps(doc))
    before = list(adapter.calls)
    with pytest.raises(AdapterError, match="routing_target_changed: app"):
        check_pr_group_status(tmp_path, group["pr_group_id"], adapter, "a")
    assert adapter.calls == before  # nothing was asked of the adapter


def test_the_head_pin_pass_reads_each_member_through_its_own_target(tmp_path):
    """The pin pass runs before the first merge and reads each pinned member's head: it must be routed too."""
    pin = "c" * 40

    class _Pinned(_V2):
        def pr_status(self, repo, number, *, target=None):
            self.status_targets[repo] = target
            return PRStatus(ref=PRRef(repo=repo, number=number), state="OPEN", head_oid=pin)

    adapter = _Pinned()
    group = _create(tmp_path, adapter)
    merge_pr_group(
        workspace_root=tmp_path,
        pr_group_id=group["pr_group_id"],
        adapter=adapter,  # type: ignore[arg-type]
        actor="a",
        method=MergeMethod.MERGE,
        verification_targets={
            repo: MergeVerificationTarget(repo_root=tmp_path / repo, remote=REMOTES[repo]) for repo in REMOTES
        },
        report=lambda _msg: None,
        expected_heads={"app": pin},
    )
    assert adapter.status_targets["app"] is not None and adapter.status_targets["app"].project == "web"
    assert adapter.merge_targets["app"].project == "web"


def test_two_targets_that_differ_only_in_which_part_is_empty_are_not_the_same_target():
    """(None, a, b, c) and (a, b, None, c) joined with "/" are both "a/b/c". The rewrite preflight compares
    identities as tuples, so a url rewrite from one to the other is seen."""
    from gr2.python_cli import app as gr2_app

    class _Positional(_V2):
        def resolve_target(self, remote):
            if remote == "first":
                return RemoteTarget(raw=remote, host=None, org="a", project="b", repo="c")
            return RemoteTarget(raw=remote, host="a", org="b", project=None, repo="c")

    adapter = _Positional()
    assert gr2_app._member_slug(adapter, "first") == gr2_app._member_slug(adapter, "second") == "a/b/c"
    assert gr2_app._member_identity(adapter, "first") != gr2_app._member_identity(adapter, "second")
    assert gr2_app._member_identity(adapter, "first") == (None, "a", "b", "c")


def test_the_rewrite_preflight_sees_a_rewrite_between_targets_that_join_to_the_same_string(tmp_path, monkeypatch):
    from gr2.python_cli import app as gr2_app

    class _Positional(_V2):
        def resolve_target(self, remote):
            if remote == "bound":
                return RemoteTarget(raw=remote, host=None, org="a", project="b", repo="c")
            return RemoteTarget(raw=remote, host="a", org="b", project=None, repo="c")

    adapter = _Positional()
    monkeypatch.setattr(gr2_app, "_effective_remote", lambda root, remote: "rewritten")
    assert gr2_app._effective_target_matches(adapter, tmp_path, "bound") is False  # a different target
    monkeypatch.setattr(gr2_app, "_effective_remote", lambda root, remote: "bound")
    assert gr2_app._effective_target_matches(adapter, tmp_path, "bound") is True  # control: no rewrite


def test_a_v2_adapter_without_resolve_target_is_refused_by_name_when_it_is_selected(monkeypatch):
    from gr2.python_cli import platform as platform_ops

    class NoResolver:
        platform_adapter_api_version = 2

        def create_pr(self, request):  # pragma: no cover - never reached
            raise AssertionError("selection must refuse first")

    monkeypatch.setattr(platform_ops, "_ADAPTER_FACTORIES", {})
    platform_ops.register_platform_adapter("noresolver", NoResolver)
    with pytest.raises(AdapterError, match="lacks required resolve_target"):
        platform_ops.get_platform_adapter("noresolver")
