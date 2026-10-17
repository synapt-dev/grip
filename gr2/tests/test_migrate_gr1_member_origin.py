"""migrate-gr1 takes each member's url from the member's own checkout when it has one.

A gr1 manifest's url is what the manifest said when the repo was added. The checkout at the member's
path is what the desk actually uses, and the two drift: a repo renamed upstream, an SSH url a host
cannot reach unattended, a fork. A spec written from the manifest sends every later verb that fetches
(materialize, --apply, a native lane) to the manifest's url instead of the member's.

Measured on 2026-10-17 on the first real gr2 root: six of ten members differed, one of them a repo
whose manifest name was retired upstream. A scratch copy whose members all pointed at local bare
repos still wrote GitHub urls into its spec.

The checkout counts only when the member is a repository of its own at its path. A plain folder inside
the root's own repository would answer with the ROOT's origin, which is not the member's url.
"""
from __future__ import annotations

import json
import os
import subprocess
import tomllib
from pathlib import Path

import yaml

from gr2.python_cli.app import app
from gr2.python_cli.migration import migrate_gr1_workspace, render_migration
from tests.conftest import make_cli_runner

runner = make_cli_runner()

_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "fx", "GIT_AUTHOR_EMAIL": "fx@example.invalid",
    "GIT_COMMITTER_NAME": "fx", "GIT_COMMITTER_EMAIL": "fx@example.invalid",
    "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull,
}

MANIFEST_URL = "https://example.invalid/manifest-said/alpha.git"
CHECKOUT_URL = "https://example.invalid/checkout-says/alpha.git"


def _git(cwd: Path, *args: str) -> None:
    done = subprocess.run(["git", *args], cwd=cwd, env=_ENV, capture_output=True, text=True)
    assert done.returncode == 0, f"git {' '.join(args)} failed in {cwd}: {done.stderr}"


def _gr1_root(tmp_path: Path, repos: dict[str, str]) -> Path:
    """A gr1 gripspace with one desk at the root and the given repos (name -> manifest url)."""
    root = tmp_path / "desk"
    (root / ".gitgrip" / "spaces" / "main").mkdir(parents=True)
    (root / ".gitgrip" / "spaces" / "main" / "gripspace.yml").write_text(
        yaml.dump({
            "version": 2,
            "manifest": {"url": "https://example.invalid/gripspace.git"},
            "repos": {name: {"url": url, "path": f"./{name}", "revision": "main"} for name, url in repos.items()},
        })
    )
    (root / ".gitgrip" / "agents.toml").write_text('[agents.opus]\nworktree = "main"\nchannel = "dev"\n')
    return root


def _checkout(path: Path, origin: str | None) -> None:
    path.mkdir(parents=True)
    _git(path, "init", "-q", "-b", "main", ".")
    if origin is not None:
        _git(path, "remote", "add", "origin", origin)


def _spec_urls(root: Path) -> dict[str, str]:
    spec = tomllib.loads((root / ".grip" / "workspace_spec.toml").read_text())
    return {repo["name"]: repo["url"] for repo in spec["repos"]}


def test_a_checkout_whose_origin_differs_wins_and_is_named(tmp_path: Path) -> None:
    root = _gr1_root(tmp_path, {"alpha": MANIFEST_URL})
    _checkout(root / "alpha", CHECKOUT_URL)
    payload = migrate_gr1_workspace(root)
    assert _spec_urls(root) == {"alpha": CHECKOUT_URL}
    assert payload["member_origins"] == [
        {"repo": "alpha", "manifest_url": MANIFEST_URL, "checkout_url": CHECKOUT_URL}
    ]
    text = render_migration(payload)
    assert "MEMBER ORIGIN" in text, text
    assert f"- alpha: {MANIFEST_URL} -> {CHECKOUT_URL}" in text, text


def test_an_equal_origin_changes_nothing_and_names_nothing(tmp_path: Path) -> None:
    root = _gr1_root(tmp_path, {"alpha": MANIFEST_URL})
    _checkout(root / "alpha", MANIFEST_URL)
    payload = migrate_gr1_workspace(root)
    assert _spec_urls(root) == {"alpha": MANIFEST_URL}
    assert payload["member_origins"] == []
    assert "MEMBER ORIGIN" not in render_migration(payload)


def test_an_absent_member_keeps_the_manifest_url(tmp_path: Path) -> None:
    root = _gr1_root(tmp_path, {"alpha": MANIFEST_URL})
    payload = migrate_gr1_workspace(root)
    assert _spec_urls(root) == {"alpha": MANIFEST_URL}
    assert payload["member_origins"] == []


def test_a_plain_folder_inside_the_root_repo_does_not_lend_the_root_origin(tmp_path: Path) -> None:
    """The root is a repository with its own origin; the member path is only a folder in it.
    Asking git for remote.origin.url there would answer with the root's url."""
    root = _gr1_root(tmp_path, {"alpha": MANIFEST_URL})
    _git(root, "init", "-q", "-b", "main", ".")
    _git(root, "remote", "add", "origin", "https://example.invalid/the-root-itself.git")
    (root / "alpha").mkdir()
    (root / "alpha" / "README").write_text("not a checkout\n")
    payload = migrate_gr1_workspace(root)
    assert _spec_urls(root) == {"alpha": MANIFEST_URL}
    assert payload["member_origins"] == []


def test_a_checkout_with_no_origin_keeps_the_manifest_url(tmp_path: Path) -> None:
    root = _gr1_root(tmp_path, {"alpha": MANIFEST_URL})
    _checkout(root / "alpha", None)
    payload = migrate_gr1_workspace(root)
    assert _spec_urls(root) == {"alpha": MANIFEST_URL}
    assert payload["member_origins"] == []


def test_the_cli_json_carries_the_rows(tmp_path: Path) -> None:
    root = _gr1_root(tmp_path, {"alpha": MANIFEST_URL, "beta": "https://example.invalid/beta.git"})
    _checkout(root / "alpha", CHECKOUT_URL)
    _checkout(root / "beta", "https://example.invalid/beta.git")
    result = runner.invoke(app, ["workspace", "migrate-gr1", str(root), "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["member_origins"] == [
        {"repo": "alpha", "manifest_url": MANIFEST_URL, "checkout_url": CHECKOUT_URL}
    ]
    summary = json.loads((root / ".grip" / "migrations" / "gr1" / "migration-summary.json").read_text())
    assert summary["member_origins"] == payload["member_origins"]
