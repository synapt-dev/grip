"""The two full-mode detectors agree: `workspace_mode.of_root` answers "full" for a root with no grip.toml that has the
shape `store init --mode full` makes, as `workspace_guidance.is_full_mode_root` does. Where grip.toml exists it still
decides, so a native root whose tree only resembles the shape stays compat.

`of_grip_toml` keeps the old meaning (what grip.toml declares, and only that) for the one reader that takes a tree entry
as the member's pin: the superproject scan behind `workspace init`. A full root's `<m>.griplink/001.2_pin` is the
member's real commit, so the scan reads it; a grip.toml `mode = "full"` workspace is still refused by name, and the
sentence ends in a verb that reads that workspace.

Written by the author of the change (not by an independent reader). Each row names what must make it go red:
  * of_root drops the shape branch                          -> the full-root row, the agreement row
  * the shape branch answers "full" without asking the shape -> the compat-superproject row
  * the shape is asked before (or regardless of) grip.toml   -> the native-root row, the agreement row
  * the shape branch also requires that no spec exists       -> the spec-beside-it row
  * of_grip_toml ignores the document's mode                 -> the refusal row
  * the refusal drops the step                               -> the refusal row, the step row
  * of_grip_toml delegates to of_root                        -> the narrowness row (and #1683's full-root init rows)
"""
from __future__ import annotations

import os
import shlex
import subprocess
import sys
import tomllib
from pathlib import Path

from gr2.python_cli import root_verbs, workspace_mode
from gr2.python_cli.workspace_guidance import is_full_mode_root
from tests.test_full_root_declared_sets import MEMBERS, full  # noqa: F401
from tests.test_store_full_mode_carrier import _full_store
from tests.test_store_git_native_smoke import configure_identity, git, gr2 as gr2_store, make_member, run

STEP = "run gr2 status in "


def _cli(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).parents[1])}
    return subprocess.run([sys.executable, "-m", "gr2.python_cli.app", *args], cwd=cwd, text=True,
                          capture_output=True, env=env)


def _spec_repos(root: Path) -> dict[str, str]:
    spec = tomllib.loads((root / ".grip" / "workspace_spec.toml").read_text())
    return {repo["name"]: repo["path"] for repo in spec.get("repos", [])}


def _sub(tmp_path: Path, name: str) -> Path:
    (tmp_path / name).mkdir()
    return tmp_path / name


def _compat_superproject(tmp_path: Path) -> Path:
    """A plain superproject: no grip.toml, members committed as gitlinks, no stook and no griplink path."""
    root = tmp_path / "super"
    root.mkdir()
    git(root, "init", "-b", "main")
    configure_identity(root)
    for name in ("alpha", "beta"):
        remote, _ = make_member(tmp_path, name)
        run(root, "git", "clone", str(remote), name)
    git(root, "add", "alpha", "beta")
    git(root, "commit", "-m", "members")
    return root


def _native_root_with_griplink_entry(tmp_path: Path) -> Path:
    """A native store root (grip.toml, no mode) whose HEAD also holds an `x.griplink/001.2_pin` gitlink."""
    root = tmp_path / "workspace"
    root.mkdir()
    for name in ("alpha", "beta"):
        remote, _ = make_member(tmp_path, name)
        run(root, "git", "clone", str(remote), name)
        configure_identity(root / name)
    assert gr2_store(root, "init").returncode == 0
    configure_identity(root)
    sha = git(root / "alpha", "rev-parse", "HEAD").stdout.strip()
    git(root, "update-index", "--add", "--cacheinfo", f"160000,{sha},x.griplink/001.2_pin")
    git(root, "commit", "-m", "griplink-shaped entry")
    return root


def test_a_full_root_reads_full_from_its_shape(full):
    root = full["root"]
    assert not (root / "grip.toml").exists()
    assert workspace_mode.of_root(root) == workspace_mode.of_root(root, "HEAD") == "full"


def test_a_compat_superproject_stays_compat_and_init_names_its_members(tmp_path):
    root = _compat_superproject(tmp_path)
    assert workspace_mode.of_root(root) == workspace_mode.of_root(root, "HEAD") == "compat"
    r = _cli(root, "workspace", "init", ".")
    assert r.returncode == 0, (r.stdout, r.stderr)
    assert _spec_repos(root) == {"alpha": "alpha", "beta": "beta"}


def test_a_native_root_whose_tree_resembles_the_shape_stays_compat(tmp_path):
    root = _native_root_with_griplink_entry(tmp_path)
    # The shape is there during the read, so it is grip.toml that decides.
    assert (root / "grip.toml").is_file()
    assert root_verbs._is_full_mode_shape(root)
    assert workspace_mode.of_root(root) == workspace_mode.of_root(root, "HEAD") == "compat"
    assert not is_full_mode_root(root)


def test_a_full_root_with_a_spec_beside_it_is_still_full(full):
    root = full["root"]
    (root / ".grip").mkdir(exist_ok=True)
    (root / ".grip" / "workspace_spec.toml").write_text('workspace_name = "R"\n')
    assert workspace_mode.of_root(root) == workspace_mode.of_root(root, "HEAD") == "full"


def test_a_grip_toml_full_workspace_is_refused_by_name_with_the_step(tmp_path):
    root = _full_store(tmp_path)
    r = _cli(root, "workspace", "init", ".")
    assert r.returncode != 0, (r.stdout, r.stderr)
    assert r.stderr.strip() == (
        f'full_mode_unsupported: the superproject scan of {root} does not read a full-mode workspace (this '
        f'workspace\'s grip.toml says mode = "full"): run gr2 status in {root} to read it')
    # The root was there and declared full during the refusal, and init wrote no spec.
    assert workspace_mode.of_document(tomllib.loads((root / "grip.toml").read_text())) == "full"
    assert not (root / ".grip" / "workspace_spec.toml").exists()


def test_the_step_in_the_refusal_reads_that_workspace(tmp_path):
    root = _full_store(tmp_path)
    refused = _cli(root, "workspace", "init", ".").stderr.strip()
    assert STEP in refused and refused.endswith(" to read it"), refused
    verb_and_root = refused.split(STEP, 1)[1][: -len(" to read it")]
    [named] = shlex.split(verb_and_root)
    r = _cli(Path(named), "status")
    assert r.returncode == 0, (r.stdout, r.stderr)
    rows = {line.split()[0]: line for line in r.stdout.splitlines() if line.strip()}
    assert sorted(rows) == ["alpha", "beta"], r.stdout
    for name in ("alpha", "beta"):
        head = git(root / name, "rev-parse", "--short=7", "HEAD").stdout.strip()
        assert f"pin={head}" in rows[name], rows[name]


def test_of_root_agrees_with_is_full_mode_root(full, tmp_path):
    spec_root = tmp_path / "spec-copy"
    run(tmp_path, "git", "clone", "-q", str(full["root"]), str(spec_root))
    (spec_root / ".grip").mkdir(exist_ok=True)
    (spec_root / ".grip" / "workspace_spec.toml").write_text('workspace_name = "R"\n')
    roots = {
        "full": full["root"],
        "full+spec": spec_root,
        "compat": _compat_superproject(_sub(tmp_path, "c")),
        "native+griplink": _native_root_with_griplink_entry(_sub(tmp_path, "n")),
    }
    answers = {k: (workspace_mode.of_root(r), is_full_mode_root(r)) for k, r in roots.items()}
    assert answers == {"full": ("full", True), "full+spec": ("full", True), "compat": ("compat", False),
                       "native+griplink": ("compat", False)}


def test_of_grip_toml_reads_only_what_grip_toml_declares(full):
    root = full["root"]
    assert workspace_mode.of_root(root, "HEAD") == "full"
    assert workspace_mode.of_grip_toml(root, "HEAD") == workspace_mode.of_grip_toml(root) == "compat"
