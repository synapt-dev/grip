"""A value from a workspace spec, a flag or a sources file that reaches git as a positional and begins with '-' is
refused before git runs, so git never reads it as an option such as --upload-pack, which runs a program.
Rows:
S1 a spec pin that is neither a sha nor ref-shaped is refused at the spec load; a sha and a ref still load;
S2 checkout_declared_pin refuses an option-shaped pin before its fetch; a real pin is still fetched by sha;
S3 a --local-source path that is option-shaped is refused before the mirror top-up fetch; a real path still tops up;
S4 store init --clone refuses an option-shaped url before any clone.
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
