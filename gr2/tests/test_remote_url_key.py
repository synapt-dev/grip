"""keys.remote_url_key: one repository, however its URL is spelled, and the store-init site that compares by it."""

from __future__ import annotations

import subprocess
import tomllib
from pathlib import Path

from gr2.python_cli import keys, merge_gate
from gr2.python_cli.app import app

from tests.conftest import make_cli_runner

key = keys.remote_url_key


def test_merge_gate_uses_the_one_key():
    assert merge_gate._url_key is keys.remote_url_key


def test_a_trailing_git_on_a_network_path_is_the_same_repository(tmp_path):
    assert key("https://github.com/org/x.git", tmp_path) == key("https://github.com/org/x", tmp_path)


def test_a_trailing_slash_is_the_same_repository(tmp_path):
    assert key("https://github.com/org/x/", tmp_path) == key("https://github.com/org/x", tmp_path)


def test_the_scp_like_ssh_form_is_the_https_form(tmp_path):
    assert key("git@github.com:org/x.git", tmp_path) == key("https://github.com/org/x", tmp_path)


def test_an_ssh_url_with_user_port_and_host_case_is_the_https_form(tmp_path):
    assert key("ssh://git@GitHub.com:22/org/x.git", tmp_path) == key("https://github.com/org/x/", tmp_path)


def test_a_file_scheme_is_the_local_path(tmp_path):
    (tmp_path / "r.git").mkdir()
    assert key(f"file://{tmp_path}/r.git/", tmp_path) == key(str(tmp_path / "r.git"), tmp_path)


def test_a_relative_local_path_resolves_against_the_clone(tmp_path):
    (tmp_path / "r.git").mkdir()
    assert key("r.git", tmp_path) == key(str(tmp_path / "r.git"), tmp_path)


def test_a_local_path_keeps_its_git(tmp_path):
    assert key(str(tmp_path / "r.git"), tmp_path) != key(str(tmp_path / "r"), tmp_path)


def test_another_repository_or_host_is_another_key(tmp_path):
    assert key("https://github.com/org/x", tmp_path) != key("https://github.com/org/y", tmp_path)
    assert key("https://github.com/org/x", tmp_path) != key("https://gitlab.com/org/x", tmp_path)


def _cli(*args: str) -> tuple[int, str]:
    result = make_cli_runner().invoke(app, list(args))
    return result.exit_code, result.stdout + (result.stderr or "")


def _bare(tmp_path: Path, name: str) -> Path:
    bare = tmp_path / "remotes" / f"{name}.git"
    bare.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True, capture_output=True)
    seed = tmp_path / "seed" / name
    subprocess.run(["git", "clone", "-q", str(bare), str(seed)], check=True, capture_output=True)
    (seed / "README.md").write_text(f"{name}\n")
    git = ["git", "-C", str(seed), "-c", "user.name=gr2", "-c", "user.email=gr2@example.invalid"]
    subprocess.run([*git, "add", "README.md"], check=True, capture_output=True)
    subprocess.run([*git, "commit", "-q", "-m", name], check=True, capture_output=True)
    subprocess.run([*git, "push", "-q", "origin", "HEAD:main"], check=True, capture_output=True)
    return bare


def test_store_init_adopts_a_checkout_whose_origin_is_the_same_repository_spelled_another_way(tmp_path):
    """A checkout cloned as file://<remote>/ is the member --clone names as <remote>; it is adopted, not refused
    as "another origin" with advice to move it."""
    alpha = _bare(tmp_path, "alpha")
    root = tmp_path / "work"
    root.mkdir()
    subprocess.run(["git", "clone", "-q", f"file://{alpha}/", str(root / "alpha")], check=True, capture_output=True)

    rc, out = _cli("store", "init", str(root), "--clone", f"alpha={alpha}")
    assert rc == 0, out
    assert "another origin" not in out, out
    doc = tomllib.loads((root / "grip.toml").read_text())
    assert [m["name"] for m in doc["members"]] == ["alpha"], out


def test_store_init_still_refuses_a_checkout_of_another_repository(tmp_path):
    alpha = _bare(tmp_path, "alpha")
    beta = _bare(tmp_path, "beta")
    root = tmp_path / "work"
    root.mkdir()
    subprocess.run(["git", "clone", "-q", str(beta), str(root / "alpha")], check=True, capture_output=True)

    rc, out = _cli("store", "init", str(root), "--clone", f"alpha={alpha}")
    assert rc == 4, out
    assert "has another origin" in out, out


def test_a_user_other_than_git_or_a_non_default_port_is_part_of_the_key(tmp_path):
    """On a plain ssh server alice@host:repo and bob@host:repo are two repositories (a path with no leading / is
    relative to the login's home), and so are two ports on one host. The forge user `git` and the default port
    still fold, so the SSH and HTTPS spellings of one forge repository stay one key."""
    assert key("alice@host:repo", tmp_path) != key("bob@host:repo", tmp_path)
    assert key("ssh://alice@host/org/x", tmp_path) != key("ssh://bob@host/org/x", tmp_path)
    assert key("ssh://host:2222/org/x", tmp_path) != key("ssh://host/org/x", tmp_path)
    assert key("ssh://git@host:22/org/x", tmp_path) == key("git@host:org/x", tmp_path)
    assert key("https://host:443/org/x", tmp_path) == key("https://host/org/x", tmp_path)


def test_may_be_same_repository_still_folds_any_user_and_port(tmp_path):
    """The refuse-only question keeps the wide fold: for a refusal a false SAME is safe."""
    assert keys.may_be_same_repository("alice@host:repo", tmp_path, "bob@host:repo", tmp_path)
    assert keys.may_be_same_repository("ssh://host:2222/org/x", tmp_path, "https://host/org/x", tmp_path)


def test_store_init_refuses_a_checkout_of_another_users_repository(tmp_path):
    """A checkout whose origin is bob@host:repo is not the member alice@host:repo; it is refused, not adopted."""
    beta = _bare(tmp_path, "beta")
    root = tmp_path / "work"
    root.mkdir()
    subprocess.run(["git", "clone", "-q", str(beta), str(root / "alpha")], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(root / "alpha"), "remote", "set-url", "origin", "bob@host:repo"], check=True,
                   capture_output=True)

    rc, out = _cli("store", "init", str(root), "--clone", "alpha=alice@host:repo")
    assert rc == 4, out
    assert "has another origin" in out, out
