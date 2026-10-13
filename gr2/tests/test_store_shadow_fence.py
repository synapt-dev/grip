"""Section 9, the fence rows (full-mode push plan, 2026-10-12, with Apollo's precision of 2026-10-13).

A hand commit on the branch is fenced by the pins it RECORDS (`<commit>:grip.toml`), compared with the pins the shadow
records, never by the root gitlink at a member path. ONE function (`_refuse_unrecorded_pins`) is called by the
pre-commit hook and by `store push`, so the two cannot disagree.
- (a) a hand commit to a tracked non-pin file passes the hook, pushes (one atomic push), and the shadow advances to it.
- (b) a hand-edited pin in grip.toml is refused at commit AND at push, naming the member; neither ref moves.
- (c) full mode: the root gitlink is the member's field commit and differs from its recorded pin, and the fence still
  passes a hand commit.
- compat: the gitlink IS the pin, so (d) a hand gitlink bump the hook folded is
  recorded and a plain push takes it; (e) the same bump past the hook with grip.toml stale is refused naming the
  member; (f) the mode split is one predicate, `_gitlink_may_be_pin`, and its two answers are pinned.
Every row runs in both store modes except (c), which only full mode can set up, and (d)-(f), which are compat's.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from gr2.python_cli import grip_cli as gc
from tests.test_store_git_native_smoke import git, gr2
from tests.test_store_shadow_s1 import MODES, SHADOW, _advance, _refs, _store


def _hand_note(root: Path, text: str = "a hand note\n") -> str:
    """A hand commit (plain `git commit`, hook ON) touching only docs/NOTE.md; returns the new HEAD."""
    (root / "docs").mkdir(exist_ok=True)
    (root / "docs" / "NOTE.md").write_text(text, encoding="utf-8")
    git(root, "add", "-f", "docs/NOTE.md")  # the store's root .gitignore ignores what it does not declare
    made = git(root, "commit", "-m", "hand note", check=False)
    assert made.returncode == 0, f"the hook must let a hand commit that edits no pin through:\n{made.stderr}"
    return git(root, "rev-parse", "HEAD").stdout.strip()


def _hand_pin(root: Path, member: str, mode: str) -> str:
    """Advance `member`, then write its new head into grip.toml BY HAND, leaving the gitlink where it was (in full mode
    the gitlink is the field commit; in compat a gitlink staged to match would make the edit a fold, row d). Returns
    the new head."""
    _advance(root, member)
    new = git(root / member, "rev-parse", "HEAD").stdout.strip()
    toml = root / "grip.toml"
    text = toml.read_text(encoding="utf-8")
    old = {line.split('"')[1] for line in text.splitlines() if line.strip().startswith("pin =")}
    pin = git(root, "show", f"HEAD:grip.toml").stdout
    block = pin.split(f'name = "{member}"', 1)[1].split("[[members]]", 1)[0]
    was = block.split('pin = "', 1)[1].split('"', 1)[0]
    assert was in old
    toml.write_text(text.replace(f'pin = "{was}"', f'pin = "{new}"', 1), encoding="utf-8")
    git(root, "add", "grip.toml")
    return new


@pytest.mark.parametrize("mode", MODES)
def test_fence_a_hand_commit_to_a_non_pin_file_pushes_and_advances_the_shadow(tmp_path, mode):
    root, origin = _store(tmp_path, mode)
    gr2(root, "push")

    hand = _hand_note(root)
    result = gr2(root, "push", check=False)

    assert result.returncode == 0, result.stderr
    remote = _refs(origin)
    assert remote.get("refs/heads/main") == hand and remote.get(SHADOW) == hand, remote
    assert _refs(root)[SHADOW] == hand, "the local shadow advances with origin's (the implicit --snapshot)"


@pytest.mark.parametrize("mode", MODES)
def test_fence_b_hand_edited_pin_is_refused_at_commit_naming_the_member(tmp_path, mode):
    root, origin = _store(tmp_path, mode)
    gr2(root, "push")
    head, local, remote = git(root, "rev-parse", "HEAD").stdout.strip(), _refs(root), _refs(origin)
    _hand_pin(root, "alpha", mode)

    refused = git(root, "commit", "-m", "hand pin", check=False)

    assert refused.returncode != 0
    assert "pin edited by hand" in refused.stderr and "alpha" in refused.stderr, refused.stderr
    assert git(root, "rev-parse", "HEAD").stdout.strip() == head and _refs(root)[SHADOW] == local[SHADOW]
    assert _refs(origin) == remote


@pytest.mark.parametrize("mode", MODES)
def test_fence_b_hand_edited_pin_is_refused_at_push_naming_the_member(tmp_path, mode):
    root, origin = _store(tmp_path, mode)
    gr2(root, "push")
    shadow, remote = _refs(root)[SHADOW], _refs(origin)
    _hand_pin(root, "alpha", mode)
    git(root, "commit", "--no-verify", "-m", "hand pin past the hook")

    result = gr2(root, "push", check=False)

    assert result.returncode == 4, (result.returncode, result.stderr)
    words = "pin edited by hand" if mode == "full" else "gitlink is not the pin grip.toml records"
    assert words in result.stderr and "alpha" in result.stderr, result.stderr
    assert _refs(origin) == remote, "a fenced push moves neither ref on origin"
    assert _refs(root)[SHADOW] == shadow, "nor the local shadow"


def test_fence_c_reads_recorded_pins_not_the_gitlink_in_full_mode(tmp_path):
    root, origin = _store(tmp_path, "full")
    gr2(root, "push")
    pins = {name: git(root, "show", "HEAD:grip.toml").stdout.split(f'name = "{name}"', 1)[1]
            .split('pin = "', 1)[1].split('"', 1)[0] for name in ("alpha", "beta")}
    links = {name: git(root, "ls-tree", "HEAD", "--", name).stdout.split()[2] for name in ("alpha", "beta")}
    assert all(links[n] != pins[n] for n in pins), ("the row needs gitlink != recorded pin (the field commit)", links, pins)

    hand = _hand_note(root)
    result = gr2(root, "push", check=False)

    assert result.returncode == 0, result.stderr
    assert _refs(origin).get(SHADOW) == hand


def test_fence_d_compat_hand_gitlink_bump_folded_by_the_hook_pushes(tmp_path):
    root, origin = _store(tmp_path, "compat")
    gr2(root, "push")
    _advance(root, "alpha")
    new = git(root / "alpha", "rev-parse", "HEAD").stdout.strip()
    git(root, "add", "alpha")  # the hand gitlink bump; the hook folds grip.toml's pin to it
    git(root, "commit", "-m", "hand bump")
    assert f'pin = "{new}"' in git(root, "show", "HEAD:grip.toml").stdout, "the hook folded the pin"
    head = git(root, "rev-parse", "HEAD").stdout.strip()

    result = gr2(root, "push", check=False)

    assert result.returncode == 0, result.stderr
    remote = _refs(origin)
    assert remote.get("refs/heads/main") == head and remote.get(SHADOW) == head, remote
    assert _refs(root)[SHADOW] == head


def test_fence_e_compat_hand_gitlink_bump_past_the_hook_with_a_stale_pin_is_refused(tmp_path):
    root, origin = _store(tmp_path, "compat")
    gr2(root, "push")
    shadow, remote = _refs(root)[SHADOW], _refs(origin)
    _advance(root, "alpha")
    git(root, "add", "alpha")
    git(root, "commit", "--no-verify", "-m", "hand bump past the hook")  # no fold: grip.toml still pins the old head
    assert git(root, "rev-parse", "HEAD:alpha").stdout.strip() not in git(root, "show", "HEAD:grip.toml").stdout

    result = gr2(root, "push", check=False)

    assert result.returncode == 4, (result.returncode, result.stderr)
    assert "gitlink is not the pin grip.toml records" in result.stderr and "alpha" in result.stderr, result.stderr
    assert _refs(origin) == remote and _refs(root)[SHADOW] == shadow, "nothing moves"


def test_fence_f_the_mode_split_is_one_predicate_with_two_answers():
    assert gc._gitlink_may_be_pin("compat") is True, "compat: the gitlink IS the pin"
    assert gc._gitlink_may_be_pin("full") is False, "full: the gitlink is the field commit; only the verb records"
