"""`workspace init` at a griplink full root names each member by its `.gitmodules` section, as the full-mode layout
spells it: `[submodule "<m>"] path = <m>.griplink/001.2_pin`. The member is `<m>`; the gitlink path is never its name.
Before, the scan named a pinned member by its path, so the spec carried `api.griplink/001.2_pin` as a member name and
`review pin` then refused that key as corrupt.

Only a path that is EXACTLY `<section>.griplink/001.2_pin` takes the section name. A compat member keeps its path as its
name, and so does a path that only resembles the shape, and so does a section that is not a member name griplink takes
(`..`, `x/..`, `a/b`), and so does a pinned member whose section another member already has as its name.

Written by the author of the change (not by an independent reader). Each row names what must make it go red:
  * the scan names a pinned member by its path again                  -> the init-names row, the review-pin row
  * the review store accepts any key                                  -> the malformed-key row
  * any pinned member takes its section name                          -> the compat row
  * a path that ends with the shape, or the path's own prefix, counts -> the resembles-the-shape rows
  * a section is adopted without griplink's member-name rule          -> the not-a-member-name rows
  * a section is adopted when another member already has that name    -> the already-named row
"""
from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from gr2.python_cli import app as app_mod
from gr2.python_cli import grip as grip_mod
from gr2.python_cli import spec_apply
from gr2.python_cli import workspace_mode
from tests.test_full_root_declared_sets import MEMBERS, full  # noqa: F401
from tests.test_griplink_cli import git, gr2

PIN_PATH = "{}.griplink/001.2_pin"


def _spec_repos(root: Path) -> dict[str, str]:
    spec = tomllib.loads((root / ".grip" / "workspace_spec.toml").read_text())
    return {repo["name"]: repo["path"] for repo in spec.get("repos", [])}


def _init(root: Path, monkeypatch) -> None:
    r = gr2(root, monkeypatch, "workspace", "init", ".")
    assert r.exit_code == 0, (r.stdout, r.stderr)


def test_workspace_init_at_a_full_root_names_members_by_their_section(full, monkeypatch):
    root = full["root"]
    _init(root, monkeypatch)
    assert _spec_repos(root) == {m: PIN_PATH.format(m) for m in MEMBERS}


def test_review_pin_after_workspace_init_at_a_full_root_stores_and_reads_back_the_keys(full, monkeypatch):
    root = full["root"]
    _init(root, monkeypatch)
    checkout = root / PIN_PATH.format("api")
    git(checkout, "checkout", "-q", "-b", "feat/f1")
    (checkout / "api.txt").write_text("api f1\n")
    git(checkout, "commit", "-q", "-am", "api f1")
    r = gr2(root, monkeypatch, "review", "pin")
    out = (r.stdout or "") + (r.stderr or "")
    assert r.exit_code == 0, out
    assert "invalid review key" not in out, out
    # Read back from the root's own .git, where a full root keeps its binds.
    [ref] = git(root, "for-each-ref", "--format=%(refname)", "refs/dev.synapt.grip/__reviews__/").split()
    keys = [git(root, "cat-file", "blob", f"{ref}:{path}")
            for path in git(root, "ls-tree", "-r", "--name-only", ref).split() if path.endswith("/001.2_key")]
    assert keys == ["api"]


def test_a_malformed_review_key_still_refuses_as_corrupt(full, monkeypatch):
    root = full["root"]
    _init(root, monkeypatch)
    with pytest.raises(grip_mod.GripCorruptError, match="invalid review key"):
        grip_mod.create_review_bind_commit(root, [{"key": PIN_PATH.format("api"), "remote": "", "path": "x",
                                                   "head": "ab" * 20, "base": "ab" * 20, "ref": "main"}])


def _pinned_root(tmp_path: Path, section: str, path: str, *more: tuple[str, str]) -> Path:
    root = tmp_path / "root"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    pairs = [(section, path), *more]
    (root / ".gitmodules").write_text("".join(f'[submodule "{s}"]\n\tpath = {p}\n\turl = https://example.invalid/x\n'
                                              for s, p in pairs))
    for _, p in pairs:
        git(root, "update-index", "--add", "--cacheinfo", "160000," + "ab" * 20 + "," + p)
    git(root, "add", ".gitmodules")
    git(root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "root")
    for _, p in pairs:
        (root / p).mkdir(parents=True)
    return root


def test_a_section_another_member_already_names_is_not_adopted(tmp_path, monkeypatch):
    # `api` is already the other member's name (its path): adopting the section would write two members named `api`,
    # a spec its own validator refuses. The pinned member keeps its path as its name.
    root = _pinned_root(tmp_path, "api", PIN_PATH.format("api"), ("other", "api"))
    _init(root, monkeypatch)
    assert _spec_repos(root) == {"api": "api", PIN_PATH.format("api"): PIN_PATH.format("api")}
    assert [issue.message for issue in spec_apply.validate_spec(root)] == []


def test_a_compat_member_keeps_its_path_as_its_name(tmp_path):
    root = _pinned_root(tmp_path, "c", "libs/c")
    [entry] = app_mod._scan_existing_repos(root)
    assert (entry["name"], entry["path"]) == ("libs/c", "libs/c")


@pytest.mark.parametrize(("section", "path"), [
    ("api", "x/api.griplink/001.2_pin"),  # another prefix
    ("web", "api.griplink/001.2_pin"),    # the section differs from the path's <m>
])
def test_a_pinned_path_that_only_resembles_the_shape_keeps_its_path(tmp_path, section, path):
    root = _pinned_root(tmp_path, section, path)
    [entry] = app_mod._scan_existing_repos(root)
    assert (entry["name"], entry["path"]) == (path, path)


@pytest.mark.parametrize(("section", "path"), [
    ("..", "...griplink/001.2_pin"),
    ("x/..", "x/...griplink/001.2_pin"),
    ("a/b", "a/b.griplink/001.2_pin"),
])
def test_a_section_that_is_not_a_member_name_keeps_its_path(tmp_path, section, path):
    # The section is written by the root's remote, and a member name is joined into paths: a section that griplink's
    # own member-name rule refuses is never adopted, even when the path has the exact shape.
    root = _pinned_root(tmp_path, section, path)
    [entry] = app_mod._scan_existing_repos(root)
    assert (entry["name"], entry["path"]) == (path, path)


@pytest.mark.xfail(strict=True, reason="pending: of_root reading a full root by shape, and the superproject scan then "
                   "reading a full root's pins instead of refusing it")
def test_workspace_init_at_a_full_root_read_as_full_writes_section_names(full, monkeypatch):
    # Once of_root reads a full root by its shape, init there must still write `api` and `web`, not refuse by name.
    # Simulated until then: of_root answers "full".
    root = full["root"]
    monkeypatch.setattr(workspace_mode, "of_root", lambda *a, **k: "full")
    _init(root, monkeypatch)
    assert _spec_repos(root) == {m: PIN_PATH.format(m) for m in MEMBERS}
