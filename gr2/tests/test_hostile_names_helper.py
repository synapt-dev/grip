"""The hostile-name helper must fail a verb that writes and then refuses (E3): rc alone is not a clean refusal."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tests.hostile_names import HOSTILE_NAMES, classify


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "root"
    subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@e.invalid",
                    "commit", "-q", "--allow-empty", "-m", "base"], check=True)
    return root


def _git(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(root), *args], check=True, text=True, capture_output=True).stdout.strip()


def test_a_verb_that_writes_a_ref_then_exits_2_is_refused_dirty(tmp_path):
    root = _repo(tmp_path)

    def verb():
        _git(root, "update-ref", "refs/heads/leftover", "HEAD")
        return 2, "refused"

    out = classify(root, verb)
    assert out.verdict == "refused-dirty"
    assert ".:refs/heads/leftover" in " ".join(out.changed)


def test_a_verb_that_writes_only_an_object_then_refuses_is_refused_dirty(tmp_path):
    root = _repo(tmp_path)

    def verb():
        subprocess.run(["git", "-C", str(root), "hash-object", "-w", "--stdin"], input="orphan\n", text=True,
                       check=True, capture_output=True)
        return 1, "refused"

    assert classify(root, verb).verdict == "refused-dirty"


def test_a_verb_that_makes_its_own_root_then_refuses_is_refused_dirty(tmp_path):
    root = tmp_path / "new-root"

    def verb():
        root.mkdir()
        return 1, "refused"

    out = classify(root, verb)
    assert out.verdict == "refused-dirty"
    assert "sibling + new-root" in out.changed


def test_a_verb_that_writes_a_file_then_refuses_is_refused_dirty(tmp_path):
    root = _repo(tmp_path)

    def verb():
        (root / ".grip-state.lock").write_text("")
        return 1, "refused"

    assert classify(root, verb).verdict == "refused-dirty"


def test_a_verb_that_refuses_before_any_write_is_refused_clean(tmp_path):
    root = _repo(tmp_path)
    out = classify(root, lambda: (2, "refused"))
    assert (out.verdict, out.changed) == ("refused-clean", [])


def test_a_verb_that_exits_0_is_accepted(tmp_path):
    root = _repo(tmp_path)
    assert classify(root, lambda: (0, "")).verdict == "accepted"


def test_every_hostile_name_has_a_unique_label_and_the_list_holds_a_non_printable():
    labels = [label for label, _ in HOSTILE_NAMES]
    assert len(labels) == len(set(labels))
    assert any(not name.isprintable() for _, name in HOSTILE_NAMES)
