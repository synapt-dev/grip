"""`store check` reads a layer's `git bundle list-heads` by "\\n", never str.splitlines (U+2028, U+0085 sit inside a
ref name).

Each row forges alpha's record with one real bundle. A bundle of a commit other than the pin, whose one head is named
refs/heads/a<sep><pin>, does not carry the pin: splitlines cut the name and read the tail as a carried oid. The control
bundle's head is the pin itself, under the same kind of name, and is accepted.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tests.test_store_full_mode_layers import _layered_store, _record_with_layers
from tests.test_store_git_native_smoke import git, gr2

SEPS = pytest.mark.parametrize("sep", ["\u2028", "\u0085"], ids=["u2028", "u0085"])


def _bundle_of(repo: Path, oid: str, name: str) -> bytes:
    git(repo, "update-ref", name, oid)
    return subprocess.run(["git", "-C", str(repo), "bundle", "create", "-", name, "--not", "origin/main"],
                          capture_output=True, check=True).stdout


@SEPS
def test_a_layer_whose_head_name_holds_the_pin_after_a_separator_is_refused(tmp_path, sep):
    root, pin = _layered_store(tmp_path)
    git(root / "alpha", "switch", "-q", "-c", "other", "origin/main")
    (root / "alpha" / "other.txt").write_text("other\n")
    git(root / "alpha", "add", "other.txt")
    git(root / "alpha", "commit", "-q", "-m", "a different commit")
    other = git(root / "alpha", "rev-parse", "HEAD").stdout.strip()
    raw = _bundle_of(root / "alpha", other, f"refs/heads/a{sep}{pin}")
    git(root / "alpha", "switch", "-q", "--detach", pin)

    checked = gr2(_record_with_layers(root, [raw], pin=pin), "check", check=False)

    assert checked.returncode == 3 and "full_mode_layer_does_not_carry_pin" in f"{checked.stdout}{checked.stderr}", (
        checked.stdout, checked.stderr)


@SEPS
def test_control_a_layer_whose_head_is_the_pin_under_such_a_name_is_accepted(tmp_path, sep):
    root, pin = _layered_store(tmp_path)
    raw = _bundle_of(root / "alpha", pin, f"refs/heads/a{sep}x")

    checked = gr2(_record_with_layers(root, [raw], pin=pin), "check", check=False)

    assert checked.returncode == 0, (checked.stdout, checked.stderr)
