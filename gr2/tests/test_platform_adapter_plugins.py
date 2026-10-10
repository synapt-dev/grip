"""External discovery reaches the CLI without patching its factory or installing."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from gr2.python_cli import platform, pr


@pytest.fixture
def plugin(tmp_path: Path):
    external = tmp_path / "external"
    external.mkdir()
    metadata = external / "fixture_adapter-1.0.dist-info"
    metadata.mkdir()
    (metadata / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: fixture-adapter\nVersion: 1.0\n"
    )
    (metadata / "entry_points.txt").write_text(
        "[gr2.platform_adapters]\nfixture = fixture_adapter:factory\n"
    )
    (external / "fixture_adapter.py").write_text("""from dataclasses import asdict
import json, os
from pathlib import Path
from gr2.python_cli.platform import PRRef
class Adapter:
    name = "fixture"
    def create_pr(self, request):
        Path(os.environ["FIXTURE_REQUEST"]).write_text(json.dumps(asdict(request)))
        with Path(os.environ["FIXTURE_EVENTS"]).open("a") as log:
            log.write(json.dumps(dict(kind="create", request=asdict(request))) + "\\n")
        number = 1 if request.repo == "sample" else 2
        remote = getattr(request, "remote", None)
        target = (remote.removesuffix(".git") if remote
                  else "https://example.invalid/" + request.repo)
        url = target + "/pull/" + str(number)
        return PRRef(repo=request.repo, number=number, url=url)
    def edit_pr_body(self, repo, number, body):
        with Path(os.environ["FIXTURE_EVENTS"]).open("a") as log:
            log.write(json.dumps(dict(kind="edit", repo=repo, body=body)) + "\\n")
    def merge_pr(self, *args, **kwargs):
        raise AssertionError("create or body edit must never merge")
    def ready(self, *args, **kwargs):
        raise AssertionError("create or body edit must never publish")
    def auto_complete(self, *args, **kwargs):
        raise AssertionError("create or body edit must never auto-complete")
def factory(): return Adapter()
""")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    subprocess.run(["git", "init", "-q", str(workspace)], check=True)
    grip = workspace / ".grip"
    grip.mkdir()
    (grip / "workspace_spec.toml").write_text(
        '[[repos]]\nname="sample"\nurl="https://example.invalid/sample.git"\n'
    )
    lane = grip / "state/lanes/default/proof"
    lane.mkdir(parents=True)
    (lane / "lane.toml").write_text(
        'repos=["sample"]\nlane_kind="materialized"\n[branch_map]\nsample="feature/proof"\n'
    )
    capture = tmp_path / "request.json"
    env = dict(
        os.environ,
        PYTHONDONTWRITEBYTECODE="1",
        FIXTURE_REQUEST=str(capture),
        FIXTURE_EVENTS=str(tmp_path / "events.jsonl"),
        PYTHONPATH=os.pathsep.join([str(external), str(Path(platform.__file__).parents[2])]),
    )
    return workspace, capture, env, metadata


def _create_cli(plugin, *flags):
    workspace, _, env, _ = plugin
    return subprocess.run(
        [sys.executable, "-m", "gr2.python_cli", "pr", "create", str(workspace),
         "default", "proof", "--platform", "fixture", *flags],
        env=env, cwd=workspace, text=True, capture_output=True,
    )


def test_per_member_remotes_reach_entry_point_and_stored_group(plugin):
    workspace, _, env, _ = plugin
    remotes = ["https://example.invalid/context-one/_git/common",
               "https://example.invalid/context-two/_git/common"]
    (workspace / ".grip/workspace_spec.toml").write_text(
        f'[[repos]]\nname="sample"\nurl="{remotes[0]}"\n'
        f'[[repos]]\nname="second"\nurl="{remotes[1]}"\n'
    )
    (workspace / ".grip/state/lanes/default/proof/lane.toml").write_text(
        'repos=["sample","second"]\nlane_kind="materialized"\n'
        '[branch_map]\nsample="feature/proof"\nsecond="feature/proof"\n'
    )
    subprocess.run(["git", "-C", str(workspace), "remote", "add", "origin",
                    "https://example.invalid/ambient/decoy.git"], check=True)
    result = _create_cli(plugin, "--json")
    assert result.returncode == 0, result.stderr
    events = [json.loads(line) for line in Path(env["FIXTURE_EVENTS"]).read_text().splitlines()]
    requests = [row["request"] for row in events if row["kind"] == "create"]
    assert [request.get("remote") for request in requests] == remotes
    group = json.loads(result.stdout)
    saved = json.loads(Path(group["state_path"]).read_text())
    assert [row.get("remote") for row in saved["prs"]] == remotes
    assert [row["url"] for row in saved["prs"]] == [
        remotes[0] + "/pull/1", remotes[1] + "/pull/2",
    ]
    assert [request["repo"] for request in requests] == ["sample", "second"]
    assert all(request["draft"] for request in requests)
    assert [row["kind"] for row in events] == ["create", "create", "edit", "edit"]


def test_duplicate_member_identity_refuses_before_adapter_calls(tmp_path):
    calls = []
    def create(request):
        calls.append(request)
        return platform.PRRef(request.repo, 1, "https://example.invalid/pull/1")

    adapter = SimpleNamespace(
        create_pr=create, edit_pr_body=lambda *args: None,
    )
    with pytest.raises(platform.AdapterError, match="duplicate.*repo"):
        pr.create_pr_group(tmp_path, "default", "proof", "title", "main", "head",
                           ["same", "same"], adapter, "local",
                           remotes={"same": "https://example.invalid/selected.git"})
    assert calls == []
    assert not (tmp_path / ".grip").exists()


@pytest.mark.parametrize("url_line", ["", 'url="   "\n'], ids=["missing", "whitespace"])
def test_absent_lane_url_omits_creation_remote(plugin, url_line):
    workspace, capture, _, _ = plugin
    (workspace / ".grip/workspace_spec.toml").write_text(
        '[[repos]]\nname="sample"\n' + url_line
    )
    result = _create_cli(plugin, "--json")
    assert result.returncode == 0, result.stderr
    assert json.loads(capture.read_text())["remote"] is None
    group = json.loads(result.stdout)
    saved = json.loads(Path(group["state_path"]).read_text())
    assert "remote" not in saved["prs"][0]


@pytest.mark.parametrize("remote", [
    "https://user:fixture-private-token@github.com/o/sample.git",
    "https://fixture-private-token@github.com/o/sample.git",
    "https://github.com/o/sample.git?access_token=fixture-private-token",
    "https://github.com/o/sample.git#fixture-private-token",
])
def test_credential_lane_url_refuses_before_adapter_or_state(plugin, remote):
    workspace, capture, env, _ = plugin
    (workspace / ".grip/workspace_spec.toml").write_text(
        f'[[repos]]\nname="sample"\nurl="{remote}"\n'
    )
    result = _create_cli(plugin, "--json")
    assert result.returncode == 2, result.stderr
    assert "sample: its url carries credentials" in result.stderr
    assert "fixture-private-token" not in result.stdout + result.stderr
    assert remote not in result.stdout + result.stderr
    assert not capture.exists()
    assert not Path(env["FIXTURE_EVENTS"]).exists()
    assert not (workspace / ".grip/pr_groups").exists()
    assert not (workspace / ".grip/events").exists()


@pytest.mark.parametrize("remote", [
    "https://github.com/o/sample.git",
    "git@github.com:o/sample.git",
    "ssh://git@github.com/o/sample.git",
])
def test_clean_lane_url_shapes_still_reach_adapter(plugin, remote):
    workspace, capture, _, _ = plugin
    (workspace / ".grip/workspace_spec.toml").write_text(
        f'[[repos]]\nname="sample"\nurl="{remote}"\n'
    )
    result = _create_cli(plugin, "--json")
    assert result.returncode == 0, result.stderr
    assert json.loads(capture.read_text())["remote"] == remote
    saved = json.loads(Path(json.loads(result.stdout)["state_path"]).read_text())
    assert saved["prs"][0]["remote"] == remote


def test_duplicate_cli_identity_names_refusal_before_adapter_calls(plugin):
    workspace, capture, env, _ = plugin
    (workspace / ".grip/workspace_spec.toml").write_text(
        '[[repos]]\nname="sample"\nurl="https://github.com/example/common.git"\n'
        '[[repos]]\nname="second"\nurl="https://github.com/example/common.git"\n'
    )
    (workspace / ".grip/state/lanes/default/proof/lane.toml").write_text(
        'repos=["sample","second"]\nlane_kind="materialized"\n'
        '[branch_map]\nsample="feature/proof"\nsecond="feature/proof"\n'
    )
    result = _create_cli(plugin, "--json")
    assert result.returncode == 1
    assert "duplicate repo identity" in result.stderr
    assert "Traceback" not in result.stderr
    assert not capture.exists()
    assert not Path(env["FIXTURE_EVENTS"]).exists()


@pytest.mark.parametrize("flags,draft", [([], True), (["--draft"], True), (["--no-draft"], False)])
def test_external_entry_point_actual_cli_carries_policy_and_target(plugin, flags, draft):
    workspace, capture, env, _ = plugin
    result = _create_cli(plugin, "--base", "integration", "--title", "Local proof",
                         "--body", "Fixture text", "--json", *flags)
    assert result.returncode == 0, result.stderr
    assert json.loads(capture.read_text()) == dict(
        repo="sample",
        title="Local proof",
        body="Fixture text",
        head_branch="feature/proof",
        base_branch="integration",
        draft=draft,
        remote="https://example.invalid/sample.git",
        target=None,  # additive field (adapter API v2): a v1 plugin is handed None, never a guess
    )
    assert json.loads(result.stdout)["platform"] == "fixture"


def test_plugin_load_failure_refuses_without_provider_action(plugin):
    workspace, capture, env, metadata = plugin
    (metadata / "entry_points.txt").write_text(
        "[gr2.platform_adapters]\nfixture = absent_module:factory\n"
    )
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "gr2.python_cli",
            "pr",
            "create",
            str(workspace),
            "default",
            "proof",
            "--platform",
            "fixture",
        ],
        env=env,
        cwd=workspace,
        text=True,
        capture_output=True,
    )
    assert result.returncode == 1
    assert "cannot load platform adapter fixture" in result.stderr
    assert "Traceback" not in result.stderr
    assert not capture.exists()


def test_duplicate_entry_points_refuse_before_load(monkeypatch):
    def load():
        pytest.fail("duplicate plugins must not load")

    entry = SimpleNamespace(name="fixture", load=load)
    monkeypatch.setattr(platform.importlib.metadata, "entry_points", lambda **kw: [entry, entry])
    with pytest.raises(platform.AdapterError, match="duplicate"):
        platform.get_platform_adapter("fixture")


def test_registry_and_version_contract(monkeypatch):
    monkeypatch.setattr(platform, "_ADAPTER_FACTORIES", {})
    monkeypatch.setattr(platform.importlib.metadata, "entry_points", lambda **kw: [])
    adapter = SimpleNamespace(create_pr=lambda request: None)
    platform.register_platform_adapter("Fixture", lambda: adapter)
    assert platform.get_platform_adapter("fixture") is adapter
    for name in ["fixture", "github", "gh"]:
        with pytest.raises(platform.AdapterError, match="registered"):
            platform.register_platform_adapter(name, lambda: adapter)
    with pytest.raises(platform.AdapterError, match="invalid"):
        platform.register_platform_adapter("../fixture", lambda: adapter)
    with pytest.raises(platform.AdapterError, match="not callable"):
        platform.register_platform_adapter("bad", None)
    adapter.platform_adapter_api_version = 99
    with pytest.raises(platform.AdapterError, match="API version"):
        platform.get_platform_adapter("fixture")
    assert isinstance(platform.get_platform_adapter("github"), platform.GitHubAdapter)
    assert isinstance(platform.get_platform_adapter("gh"), platform.GitHubAdapter)


def test_missing_cross_link_capability_refuses_before_first_create(tmp_path):
    calls = []
    adapter = SimpleNamespace(create_pr=lambda request: calls.append(request))
    with pytest.raises(platform.AdapterError, match="edit_pr_body"):
        pr.create_pr_group(
            tmp_path, "default", "proof", "title", "main", "head", ["a", "b"], adapter, "local"
        )
    assert calls == []
    assert not (tmp_path / ".grip").exists()


def test_single_member_group_default_is_draft(tmp_path):
    calls = []

    class Adapter:
        name = "fixture"

        def create_pr(self, request):
            calls.append(request)
            return platform.PRRef(repo=request.repo, number=1, url="https://example.invalid/pull/1")

    group = pr.create_pr_group(
        tmp_path, "default", "proof", "title", "main", "head", ["sample"], Adapter(), "local"
    )
    assert calls[0].draft is True
    assert calls[0].remote is None
    assert group["prs"] == [{"repo": "sample", "pr_number": 1, "url": "https://example.invalid/pull/1"}]
    assert platform.CreatePRRequest("sample", "title", "body", "head", "main").draft is True


def test_old_merge_signature_refuses_without_invocation():
    calls = []

    class Old:
        def merge_pr(self, repo, number, *, method):
            calls.append((repo, number, method))

    with pytest.raises(platform.AdapterError, match="expected_head"):
        platform.require_adapter_capability(Old(), "merge_pr")
    assert calls == []


@pytest.mark.parametrize("flags,draft", [([], True), (["--draft"], True), (["--no-draft"], False)])
def test_two_member_actual_cli_body_edits_do_not_publish(plugin, flags, draft):
    workspace, capture, env, _ = plugin
    (workspace / ".grip/workspace_spec.toml").write_text(
        '[[repos]]\nname="sample"\nurl="https://example.invalid/sample.git"\n'
        '[[repos]]\nname="second"\nurl="https://example.invalid/second.git"\n'
    )
    (workspace / ".grip/state/lanes/default/proof/lane.toml").write_text(
        'repos=["sample","second"]\nlane_kind="materialized"\n'
        '[branch_map]\nsample="feature/proof"\nsecond="feature/proof"\n'
    )
    result = _create_cli(plugin, "--base", "integration", "--title", "Local proof",
                         "--body", "Fixture text", "--json", *flags)
    assert result.returncode == 0, result.stderr
    events = [json.loads(line) for line in Path(env["FIXTURE_EVENTS"]).read_text().splitlines()]
    assert [row["kind"] for row in events] == ["create", "create", "edit", "edit"]
    assert [row["request"]["repo"] for row in events[:2]] == ["sample", "second"]
    assert all(row["request"]["draft"] is draft for row in events[:2])
    assert all(row["request"]["base_branch"] == "integration" for row in events[:2])
    for row in events[2:]:
        assert "https://example.invalid/sample/pull/1" in row["body"]
        assert "https://example.invalid/second/pull/2" in row["body"]


def test_unselected_broken_plugin_is_not_imported(plugin):
    workspace, capture, env, metadata = plugin
    sentinel = workspace / "broken-imported"
    (metadata / "entry_points.txt").write_text(
        "[gr2.platform_adapters]\nfixture = fixture_adapter:factory\n"
        "broken = broken_adapter:factory\n"
    )
    (metadata.parent / "broken_adapter.py").write_text(
        f"from pathlib import Path\nPath({str(sentinel)!r}).touch()\n"
        "raise RuntimeError('unselected plugin imported')\n"
    )
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "gr2.python_cli",
            "pr",
            "create",
            str(workspace),
            "default",
            "proof",
            "--platform",
            "fixture",
            "--json",
        ],
        env=env,
        cwd=workspace,
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr
    assert capture.exists()
    assert not sentinel.exists()
    events = Path(env["FIXTURE_EVENTS"]).read_text().splitlines()
    assert len(events) == 1
    assert json.loads(events[0])["request"]["draft"] is True


def test_direct_python_explicit_non_draft_override(tmp_path):
    calls = []

    class Adapter:
        name = "fixture"

        def create_pr(self, request):
            calls.append(request)
            return platform.PRRef(repo=request.repo, number=1)

    pr.create_pr_group(
        tmp_path,
        "default",
        "proof",
        "title",
        "main",
        "head",
        ["sample"],
        Adapter(),
        "local",
        draft=False,
    )
    assert calls[0].draft is False
    assert (
        platform.CreatePRRequest("sample", "title", "body", "head", "main", draft=False).draft
        is False
    )
