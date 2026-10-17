"""A value from a workspace spec, a flag or a sources file that reaches git as a positional and begins with '-' is
refused before git runs, so git never reads it as an option such as --upload-pack, which runs a program.
Rows:
S1 a spec pin that is neither a sha nor ref-shaped is refused at the spec load; a sha and a ref still load; apply on
   such a spec is refused end to end and runs nothing;
S2 checkout_declared_pin refuses an option-shaped pin before its fetch; a real pin is still fetched by sha;
S3 a --local-source path that is option-shaped is refused before the mirror top-up fetch; a real path still tops up;
S4 store init --clone refuses an option-shaped url before any clone;
S5 a lane seed source path that begins with '-' (a --sources-json source) reaches both seed fetches after '--'.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from gr2.python_cli import gitops, grip_cli, open_gr_review, project_review, spec_apply

ENV = dict(os.environ, GIT_AUTHOR_NAME="positional-test", GIT_AUTHOR_EMAIL="positional-test@example.invalid",
           GIT_COMMITTER_NAME="positional-test", GIT_COMMITTER_EMAIL="positional-test@example.invalid")
OPTIONS = ["--upload-pack", "--receive-pack"]


@pytest.fixture(autouse=True)
def _identity(monkeypatch):
    for k, v in ENV.items():
        if k.startswith("GIT_"):
            monkeypatch.setenv(k, v)


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, check=True,
                          env=ENV).stdout.decode().strip()


@pytest.fixture
def world(tmp_path):
    """`origin` (bare) holds `base`; the clone `me` of it also holds `head`, which origin does not."""
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    me = tmp_path / "me"
    subprocess.run(["git", "init", "-q", str(me)], check=True)
    git(me, "commit", "-q", "--allow-empty", "-m", "base")
    base = git(me, "rev-parse", "HEAD")
    git(origin, "fetch", "-q", str(me), "HEAD:refs/heads/main")
    git(me, "remote", "add", "origin", str(origin))
    git(me, "commit", "-q", "--allow-empty", "-m", "head")
    return origin, me, base, git(me, "rev-parse", "HEAD")


def write_spec(root: Path, pin: str) -> None:
    (root / ".grip").mkdir(parents=True)
    (root / ".grip" / "workspace_spec.toml").write_text(
        f'workspace_name = "w"\n\n[[repos]]\nname = "a"\npath = "a"\nurl = "/srv/a.git"\npin = "{pin}"\n',
        encoding="utf-8")


@pytest.mark.parametrize("pin", [f"{o}=touch x" for o in OPTIONS] + ["-", "a b", "a..b"])
def test_s1_spec_load_refuses_a_pin_that_is_not_a_sha_or_a_ref(tmp_path, pin):
    write_spec(tmp_path, pin)
    with pytest.raises(SystemExit, match="pin"):
        spec_apply.load_workspace_spec_doc(tmp_path)


@pytest.mark.parametrize("pin", ["0" * 40, "v1.0", "refs/heads/main", "release/2-x"])
def test_s1_spec_load_keeps_a_sha_or_a_ref_pin(tmp_path, pin):
    write_spec(tmp_path, pin)
    assert spec_apply.load_workspace_spec_doc(tmp_path)["repos"][0]["pin"] == pin


@pytest.mark.parametrize("option", OPTIONS)
def test_s2_checkout_declared_pin_never_runs_an_option_shaped_pin(world, tmp_path, option):
    origin, _, _, _ = world
    clone = tmp_path / "c"
    subprocess.run(["git", "clone", "-q", str(origin), str(clone)], check=True)
    marker = tmp_path / "ran"
    with pytest.raises(SystemExit, match="pin_option_shaped"):
        gitops.checkout_declared_pin(clone, f"{option}=touch {marker}", member="a")
    assert not marker.exists()


def test_s2_checkout_declared_pin_still_fetches_a_pin_by_sha(world, tmp_path):
    origin, me, _, head = world
    git(origin, "fetch", "-q", str(me), f"{head}:refs/heads/side")
    clone = tmp_path / "c"
    subprocess.run(["git", "clone", "-q", "--single-branch", "-b", "main", str(origin), str(clone)], check=True)
    gitops.checkout_declared_pin(clone, head, member="a")
    assert git(clone, "rev-parse", "HEAD") == head


def resolve(tmp_path, origin, base, head, local):
    pin = project_review.ProjectReviewPin(key="a", repo=f"local:{origin}", path="repos/a", base=base, head=head)
    return open_gr_review.resolve_sources_from_pins([pin], cache_root=tmp_path / "cache", local_sources={"a": local})


@pytest.mark.parametrize("option", OPTIONS)
def test_s3_local_source_never_runs_an_option_shaped_path(world, tmp_path, option):
    origin, _, base, head = world
    marker = tmp_path / "ran"
    with pytest.raises(open_gr_review.OpenGrReviewError, match="path_option_shaped"):
        resolve(tmp_path, origin, base, head, f"{option}=touch {marker}")
    assert not marker.exists()


def test_s3_local_source_still_tops_up_from_a_real_path(world, tmp_path):
    origin, me, base, head = world
    srcmap, _ = resolve(tmp_path, origin, base, head, str(me))
    assert srcmap["a"][1] == head


@pytest.mark.parametrize("option", OPTIONS)
def test_s4_store_init_clone_refuses_an_option_shaped_url(tmp_path, option):
    marker = tmp_path / "ran"
    with pytest.raises(grip_cli.NativeStoreRefusal, match="url"):
        grip_cli._parse_clone_values([f"a={option}=touch {marker}"])
    assert not marker.exists()


def test_s4_store_init_clone_keeps_a_path_or_a_url():
    assert grip_cli._parse_clone_values(["a=/srv/a.git", "b=https://example.invalid/b.git"]) == [
        ("a", "/srv/a.git"), ("b", "https://example.invalid/b.git")]


def test_s5_sources_json_never_runs_an_option_shaped_source_path(tmp_path, monkeypatch):
    """A --sources-json source path is passed to the lane seed fetch as written; a relative path that
    begins with '-' names a real clone and still reaches git as a path, never as an option."""
    import json
    import tomllib

    from typer.testing import CliRunner

    from gr2.python_cli import app as gr2_app

    from .test_open_gr_enter import _review_kind_commit, _world

    workspace, sources, _ = _world(tmp_path)
    gr_commit, srcmap = _review_kind_commit(workspace, sources)
    marker = tmp_path / "ran"
    monkeypatch.setenv("GR2_POSITIONAL_MARK", str(marker))
    odd = "--upload-pack=touch $GR2_POSITIONAL_MARK"
    alpha = sources["alpha"][0]
    subprocess.run(["git", "clone", "-q", "--", str(alpha), str(tmp_path / odd)], check=True)
    git(tmp_path / odd, "remote", "set-url", "origin", git(alpha, "remote", "get-url", "origin"))
    git(tmp_path / odd, "branch", "-q", "review/alpha", sources["alpha"][2])
    rows = {key: {"source": str(src), "branch": branch} for key, (src, branch) in srcmap.items()}
    rows["alpha"]["source"] = odd
    (tmp_path / "sources.json").write_text(json.dumps(rows), encoding="utf-8")
    owner = tomllib.loads((workspace / ".grip" / "workspace_spec.toml").read_text())["units"][0]["name"]
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(gr2_app.app, ["review", "open-project", str(workspace), gr_commit, owner, "review-m1",
                                     "--enter", "--allow-local", "--sources-json", str(tmp_path / "sources.json")])
    assert result.exit_code == 0 and "status=opened" in result.output, result.output
    assert not marker.exists()


def test_s1_apply_refuses_an_option_shaped_pin_end_to_end(world, tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from gr2.python_cli import app as gr2_app

    origin, _, base, _ = world
    marker = tmp_path / "ran"
    monkeypatch.setenv("GR2_POSITIONAL_MARK", str(marker))
    root = tmp_path / "ws"
    (root / ".grip").mkdir(parents=True)
    (root / ".grip" / "workspace_spec.toml").write_text(
        f'workspace_name = "w"\n\n[[repos]]\nname = "a"\npath = "a"\nurl = "{origin}"\n'
        'pin = "--upload-pack=touch $GR2_POSITIONAL_MARK"\n', encoding="utf-8")
    result = CliRunner().invoke(gr2_app.app, ["apply", str(root), "--yes"])
    assert result.exit_code != 0 and "pin" in str(result.output) + str(result.exception), result.output
    assert not marker.exists()


def test_s5_lane_seed_fallback_fetch_never_runs_an_option_shaped_source_path(world, tmp_path, monkeypatch):
    """When the by-sha seed fetch fails, the fallback fetch by ref also passes the source path after '--'."""
    from gr2.python_cli import clone_exec

    origin, _, _, _ = world
    marker = tmp_path / "ran"
    monkeypatch.setenv("GR2_POSITIONAL_MARK", str(marker))
    odd = "--upload-pack=touch $GR2_POSITIONAL_MARK"
    subprocess.run(["git", "clone", "-q", "-b", "main", "--", str(origin), str(tmp_path / odd)], check=True)
    real_git = gitops.git

    def by_sha_fetch_refused(cwd, *args, **kw):
        if args[:2] == ("fetch", "--no-tags") and len(args[-1]) == 40:
            return subprocess.CompletedProcess(args, 1, "", "by-sha want refused")
        return real_git(cwd, *args, **kw)

    monkeypatch.setattr(gitops, "git", by_sha_fetch_refused)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(clone_exec.CloneExecutionError, match="failed to fetch seed"):
        clone_exec.materialize_lane_clone(source_repo_root=Path(odd), dest=tmp_path / "lanes" / "a", branch="main",
                                          workspace_root=tmp_path)
    assert not marker.exists()
