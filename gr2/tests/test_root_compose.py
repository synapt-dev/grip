"""A store root's [[compose]]: one root file built from parts in several members.

gr1's agent-codelab composed a root AGENTS.md from several repos; a member's [[files.compose]] cannot, since its
parts stay inside that member. A root compose names each part "<member>:<path>" and runs at the end of
`gr2 checkout`. The dest is derived, so gr2 keeps it out of the root's commits with one .gitignore line.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from tests.test_store_init_mode import _two_members
from tests.test_store_git_native_smoke import gr2

COMPOSE = '\n[[compose]]\ndest = "AGENTS.md"\nparts = ["alpha:AGENTS.md", "beta:AGENTS.md"]\n'


def top(root: Path, verb: str) -> subprocess.CompletedProcess[str]:
    """A top-level gr2 verb run at the root (the `gr2` helper above prefixes `store`)."""
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).parents[1])}
    return subprocess.run([sys.executable, "-m", "gr2.python_cli.app", verb], cwd=root, text=True,
                          capture_output=True, env=env)


def checkout(root: Path) -> subprocess.CompletedProcess[str]:
    """`gr2 checkout`, the verb a store root's compose runs at."""
    return top(root, "checkout")


def _root_with_compose(tmp_path: Path, table: str = COMPOSE) -> Path:
    root = _two_members(tmp_path)
    assert gr2(root, "init").returncode == 0
    (root / "alpha" / "AGENTS.md").write_text("# alpha\n", encoding="utf-8")
    (root / "beta" / "AGENTS.md").write_text("# beta\n", encoding="utf-8")
    with (root / "grip.toml").open("a", encoding="utf-8") as fh:
        fh.write(table)
    return root


def test_checkout_writes_the_root_file_from_both_members(tmp_path):
    root = _root_with_compose(tmp_path)

    result = checkout(root)

    assert (root / "AGENTS.md").read_text(encoding="utf-8") == "# alpha\n\n\n# beta\n"
    assert "composed AGENTS.md from 2 part(s)" in result.stderr, result.stderr
    assert "/AGENTS.md" in (root / ".gitignore").read_text(encoding="utf-8").splitlines()


def test_a_member_edit_changes_exactly_its_section_at_the_next_checkout(tmp_path):
    root = _root_with_compose(tmp_path)
    checkout(root)
    (root / "beta" / "AGENTS.md").write_text("# beta, edited\n", encoding="utf-8")

    checkout(root)

    assert (root / "AGENTS.md").read_text(encoding="utf-8") == "# alpha\n\n\n# beta, edited\n"
    assert (root / ".gitignore").read_text(encoding="utf-8").splitlines().count("/AGENTS.md") == 1


def test_the_composed_file_never_enters_a_root_commit(tmp_path):
    root = _root_with_compose(tmp_path)
    checkout(root)

    porcelain = subprocess.run(["git", "-C", str(root), "status", "--porcelain", "--", "AGENTS.md"],
                               capture_output=True, text=True, check=True).stdout
    assert porcelain == "", porcelain


def test_a_part_in_a_member_that_is_not_there_refuses_and_writes_nothing(tmp_path):
    table = '\n[[compose]]\ndest = "AGENTS.md"\nparts = ["alpha:AGENTS.md", "gamma:AGENTS.md"]\n'
    root = _root_with_compose(tmp_path, table)

    result = checkout(root)

    assert result.returncode == 4, (result.stdout, result.stderr)
    assert "gamma" in result.stderr and "nothing was composed" in result.stderr, result.stderr
    assert not (root / "AGENTS.md").exists()


def test_a_part_that_climbs_out_of_its_member_refuses(tmp_path):
    table = '\n[[compose]]\ndest = "AGENTS.md"\nparts = ["alpha:../beta/AGENTS.md"]\n'
    root = _root_with_compose(tmp_path, table)

    result = checkout(root)

    assert result.returncode == 4, (result.stdout, result.stderr)
    assert "outside member alpha's own tree" in result.stderr, result.stderr
    assert not (root / "AGENTS.md").exists()


def test_a_dest_outside_the_root_refuses(tmp_path):
    table = '\n[[compose]]\ndest = "../AGENTS.md"\nparts = ["alpha:AGENTS.md"]\n'
    root = _root_with_compose(tmp_path, table)

    result = checkout(root)

    assert result.returncode == 4, (result.stdout, result.stderr)
    assert not (root.parent / "AGENTS.md").exists()


def _store_status(root: Path) -> subprocess.CompletedProcess[str]:
    # status reads a recorded root, so record one first (the composed file is ignored, so it is not in it)
    gr2(root, "commit", "-m", "first snapshot", check=False)
    return top(root, "status")


def test_status_names_a_missing_composed_file(tmp_path):
    root = _root_with_compose(tmp_path)
    checkout(root)
    (root / "AGENTS.md").unlink()

    result = _store_status(root)

    assert "compose: AGENTS.md is missing; gr2 checkout writes it" in result.stderr, result.stderr
    assert not (root / "AGENTS.md").exists()


def test_status_names_a_stale_composed_file_and_writes_nothing(tmp_path):
    root = _root_with_compose(tmp_path)
    checkout(root)
    (root / "alpha" / "AGENTS.md").write_text("# alpha, edited\n", encoding="utf-8")
    before = (root / "AGENTS.md").read_bytes()

    result = _store_status(root)

    assert "compose: AGENTS.md is stale (its parts changed); gr2 checkout rewrites it" in result.stderr, result.stderr
    assert (root / "AGENTS.md").read_bytes() == before


def test_status_is_quiet_when_the_composed_file_is_current(tmp_path):
    root = _root_with_compose(tmp_path)
    checkout(root)

    result = _store_status(root)

    assert "compose:" not in result.stderr, result.stderr


import pytest


@pytest.mark.parametrize("table,code,words", [
    ('dest = ".git/AGENTS.md"\nparts = ["alpha:AGENTS.md"]', 4, "outside .git and .grip"),
    ('dest = ".grip/AGENTS.md"\nparts = ["alpha:AGENTS.md"]', 4, "outside .git and .grip"),
    ('dest = "alpha/AGENTS.md"\nparts = ["beta:AGENTS.md"]', 4, "the dest is inside member alpha"),
    ('dest = "AGENTS.md"\nparts = ["alpha"]', 4, 'is not "<member>:<path>"'),
    ('dest = "AGENTS.md"\nparts = ["alpha:NOPE.md"]', 3, "does not exist in member alpha"),
    ('dest = "AGENTS.md"\nparts = []', 4, "parts must be a list"),
    ('dest = ""\nparts = ["alpha:AGENTS.md"]', 4, "needs a dest"),
    ('dest = "grip.toml"\nparts = ["alpha:AGENTS.md"]', 4, "the root's own grip.toml"),
    ('dest = ".gitignore"\nparts = ["alpha:AGENTS.md"]', 4, "the root's own .gitignore"),
    ('dest = ".gitinclude"\nparts = ["alpha:AGENTS.md"]', 4, "the root's own .gitinclude"),
], ids=["dest-in-git", "dest-in-grip", "dest-in-member", "part-no-colon", "part-missing", "parts-empty", "dest-empty", "dest-grip-toml",
                                           "dest-gitignore", "dest-gitinclude"])
def test_a_bad_compose_refuses_with_its_code_and_writes_nothing(tmp_path, table, code, words):
    root = _root_with_compose(tmp_path, "\n[[compose]]\n" + table + "\n")
    before = (root / "alpha" / "AGENTS.md").read_bytes()
    toml_before, ignore_before = (root / "grip.toml").read_bytes(), (root / ".gitignore").read_bytes()

    result = checkout(root)

    assert result.returncode == code, (result.stdout, result.stderr)
    assert words in result.stderr and "nothing was composed" in result.stderr, result.stderr
    assert not (root / "AGENTS.md").exists() and not (root / ".git" / "AGENTS.md").exists()
    assert (root / "alpha" / "AGENTS.md").read_bytes() == before
    assert (root / "grip.toml").read_bytes() == toml_before and (root / ".gitignore").read_bytes() == ignore_before


def test_a_dest_the_root_tracks_refuses_and_keeps_its_edit(tmp_path):
    root = _root_with_compose(tmp_path)
    (root / "AGENTS.md").write_text("tracked\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "-f", "AGENTS.md"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(root), "-c", "user.name=T", "-c", "user.email=t@example.invalid",
                    "commit", "-q", "-m", "track"], check=True, capture_output=True)
    (root / "AGENTS.md").write_text("tracked, edited\n", encoding="utf-8")

    result = checkout(root)

    assert result.returncode == 4, (result.stdout, result.stderr)
    assert "the root tracks AGENTS.md" in result.stderr and "nothing was composed" in result.stderr, result.stderr
    assert (root / "AGENTS.md").read_text(encoding="utf-8") == "tracked, edited\n"


def _case_insensitive(path: Path) -> bool:
    probe = path / "CaseProbe"
    probe.write_text("x", encoding="utf-8")
    try:
        return (path / "caseprobe").exists()
    finally:
        probe.unlink()


def test_a_dest_differing_only_in_case_from_a_tracked_file_refuses(tmp_path):
    root = _root_with_compose(tmp_path, '\n[[compose]]\ndest = "readme.md"\nparts = ["alpha:AGENTS.md"]\n')
    if not _case_insensitive(root):
        pytest.skip("case-sensitive filesystem: readme.md is a different file from README.md here")
    (root / "README.md").write_text("tracked\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "-f", "README.md"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(root), "-c", "user.name=T", "-c", "user.email=t@example.invalid",
                    "commit", "-q", "-m", "track"], check=True, capture_output=True)

    result = checkout(root)

    assert result.returncode == 4, (result.stdout, result.stderr)
    assert "the root tracks README.md" in result.stderr, result.stderr
    assert (root / "README.md").read_text(encoding="utf-8") == "tracked\n"


def test_a_dest_in_a_member_spelled_in_another_case_refuses(tmp_path):
    root = _root_with_compose(tmp_path, '\n[[compose]]\ndest = "ALPHA/AGENTS.md"\nparts = ["beta:AGENTS.md"]\n')
    if not _case_insensitive(root):
        pytest.skip("case-sensitive filesystem: ALPHA is a different directory from alpha here")
    before = (root / "alpha" / "AGENTS.md").read_bytes()

    result = checkout(root)

    assert result.returncode == 4, (result.stdout, result.stderr)
    assert "the dest is inside member alpha" in result.stderr, result.stderr
    assert (root / "alpha" / "AGENTS.md").read_bytes() == before


def test_a_dest_with_glob_characters_is_a_literal_path(tmp_path):
    root = _root_with_compose(tmp_path, '\n[[compose]]\ndest = "*.md"\nparts = ["alpha:AGENTS.md"]\n')
    (root / "README.md").write_text("tracked\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "-f", "README.md"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(root), "-c", "user.name=T", "-c", "user.email=t@example.invalid",
                    "commit", "-q", "-m", "track"], check=True, capture_output=True)

    result = checkout(root)

    assert result.returncode == 0, (result.stdout, result.stderr)
    assert (root / "*.md").read_text(encoding="utf-8") == "# alpha\n"
    assert (root / "README.md").read_text(encoding="utf-8") == "tracked\n"
