"""A verb that reads `.grip/workspace_spec.toml` on a root without one refuses in one line, naming the root and the
step that suits it.

`load_workspace_spec` was a bare open, so `review checkout-pr` (and every other verb through that seam) printed a
FileNotFoundError traceback on a native root (grip.toml, no spec) and on a gr1 desk not yet adopted. The rows run the
real CLI in a subprocess (`-P`: no cwd on sys.path), so stdout and stderr are separate on every click version and the
console-script exit path is the one measured.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from gr2.prototypes import lane_workspace_prototype as lane_proto
from gr2.python_cli import spec_apply

GR2_ROOT = Path(__file__).parents[1]

_CLI = """
import os, runpy, sys
sys.argv = [os.path.join(os.path.dirname(sys.executable), "gr2")] + sys.argv[1:]
runpy.run_module("gr2.python_cli.app", run_name="__main__")
"""

GRIP_TOML = 'schema_version = 1\nworkspace_name = "nativews"\n\n[[members]]\nname = "m"\npath = "m"\n'
SPEC_TOML = 'workspace_name = "legacyws"\n\n[[repos]]\nname = "app"\npath = "app"\nurl = "https://example.invalid/o/app.git"\n'


def _gr2(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "PYTHONPATH": str(GR2_ROOT)}
    return subprocess.run([sys.executable, "-P", "-c", _CLI, *args], cwd=cwd, text=True, capture_output=True, env=env)


def _root(tmp_path: Path, kind: str) -> Path:
    root = tmp_path / kind
    root.mkdir()
    if kind == "native":
        subprocess.run(["git", "init", "-q", str(root)], check=True)
        (root / "grip.toml").write_text(GRIP_TOML, encoding="utf-8")
    elif kind == "gr1":
        manifest = root / ".gitgrip" / "spaces" / "main" / "gripspace.yml"
        manifest.parent.mkdir(parents=True)
        manifest.write_text("repos: {}\n", encoding="utf-8")
    elif kind == "spec":
        (root / ".grip").mkdir()
        (root / ".grip" / "workspace_spec.toml").write_text(SPEC_TOML, encoding="utf-8")
    return root


def _refusal(result: subprocess.CompletedProcess[str]) -> str:
    assert "Traceback" not in result.stderr and "FileNotFoundError" not in result.stderr, result.stderr
    assert result.returncode == 1, (result.returncode, result.stdout, result.stderr)
    lines = result.stderr.strip().splitlines()
    assert len(lines) == 1, result.stderr
    return lines[0]


VERBS = {
    "review checkout-pr": lambda root: ("review", "checkout-pr", str(root), "u", "app", "1"),
    "review open": lambda root: ("review", "open", str(root), "u", "app", "1"),
}


@pytest.mark.parametrize("verb", ["review checkout-pr", "review open"])
def test_a_native_root_is_named_and_not_told_to_migrate(tmp_path, verb):
    root = _root(tmp_path, "native")
    line = _refusal(_gr2(tmp_path, *VERBS[verb](root)))
    assert "workspace spec not found" in line and str(root) in line
    assert "native gr2 root" in line and "not supported on a native root yet" in line
    assert "migrate-gr1" not in line and "workspace init" not in line


@pytest.mark.parametrize("verb", ["review checkout-pr", "review open"])
def test_a_gr1_desk_is_told_to_migrate_its_own_root(tmp_path, verb):
    root = _root(tmp_path, "gr1")
    line = _refusal(_gr2(tmp_path, *VERBS[verb](root)))
    assert f"`gr2 workspace migrate-gr1 {root}`" in line
    assert "native" not in line and "workspace init" not in line


def test_a_root_with_neither_is_told_to_init(tmp_path):
    root = _root(tmp_path, "plain")
    line = _refusal(_gr2(tmp_path, *VERBS["review checkout-pr"](root)))
    assert "workspace spec not found" in line and f"`gr2 workspace init {root}`" in line
    assert "migrate-gr1" not in line and "native" not in line


def test_control_a_spec_root_reaches_the_verbs_own_refusal(tmp_path):
    """The seam is passed on a root that has a spec: the verb goes on to its own check of the named repo."""
    root = _root(tmp_path, "spec")
    result = _gr2(tmp_path, "review", "checkout-pr", str(root), "u", "nosuchrepo", "1")
    assert "Traceback" not in result.stderr and result.returncode == 1, result.stderr
    assert "repo not found in workspace spec: nosuchrepo" in result.stderr + result.stdout
    assert "workspace spec not found" not in result.stderr + result.stdout


def test_the_refusal_is_a_system_exit_so_degrading_callers_keep_working(tmp_path):
    """commit catches SystemExit around the spec read and goes on without spec paths; an Exception subclass would
    escape that handler as a traceback."""
    root = _root(tmp_path, "native")
    with pytest.raises(SystemExit) as caught:
        lane_proto.load_workspace_spec(root)
    assert isinstance(caught.value, lane_proto.WorkspaceSpecMissing)
    assert "native gr2 root" in str(caught.value.code)


def test_the_spec_doc_loader_gives_the_same_line(tmp_path):
    for kind in ("native", "gr1", "plain"):
        root = _root(tmp_path, kind)
        with pytest.raises(lane_proto.WorkspaceSpecMissing) as from_doc:
            spec_apply.load_workspace_spec_doc(root)
        with pytest.raises(lane_proto.WorkspaceSpecMissing) as from_seam:
            lane_proto.load_workspace_spec(root)
        assert from_doc.value.code == from_seam.value.code, kind
