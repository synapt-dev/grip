"""A store push refused on a lock while another push of the same refs still holds it retries, bounded, until that push
lands; a ref at a third value, a lock held past the bound, and a refusal that is not a lock still exit 5."""
from __future__ import annotations

import shutil
import time
from pathlib import Path

from tests.test_store_git_native_smoke import gr2
from tests.test_store_push_landed_by_another import _popen
from tests.test_store_shadow_s1 import SHADOW, _refs, _refuse_shadow, _store
from tests.test_store_shadow_fence import _hand_note


def _hold(origin: Path, seconds: int) -> None:
    """The first transaction origin prepares after this is held `seconds` with its locks taken and no ref moved;
    held.marker shows it is waiting."""
    hook = origin / "hooks" / "reference-transaction"
    hook.write_text('#!/bin/sh\ncat >/dev/null\n[ "$1" = prepared ] || exit 0\nmkdir held.claim 2>/dev/null || exit 0\n'
                    f': > held.marker\nsleep {seconds}\nexit 0\n')
    hook.chmod(0o755)


def _race(origin: Path, winner: Path, loser: Path):
    """The winner's push starts; the loser's starts only once the winner is held. Returns both exit codes, the
    loser's seconds, and both stderrs."""
    first = _popen(winner, "push")
    deadline = time.monotonic() + 60
    while not (origin / "held.marker").exists():
        assert first.poll() is None and time.monotonic() < deadline, "the winner never reached the hold"
        time.sleep(0.02)
    start = time.monotonic()
    second = _popen(loser, "push")
    _, err2 = second.communicate()
    took = time.monotonic() - start
    _, err1 = first.communicate()
    return first.returncode, second.returncode, took, err1, err2


def test_refused_while_the_same_push_holds_its_locks_lands(tmp_path):
    root, origin = _store(tmp_path, "full")
    gr2(root, "push")
    _hold(origin, 3)
    hand = _hand_note(root, "held\n")
    rc1, rc2, took, err1, err2 = _race(origin, root, root)
    print(f"OBS held rc=({rc1},{rc2}) loser_s={took:.1f}")
    assert (rc1, rc2) == (0, 0), (err1, err2)
    remote = _refs(origin)
    assert remote.get("refs/heads/main") == hand == remote.get(SHADOW) == _refs(root)[SHADOW]


def test_a_third_value_still_exits_5_when_it_lands(tmp_path):
    root, origin = _store(tmp_path, "full")
    gr2(root, "push")
    other = tmp_path / "workspace2"
    shutil.copytree(root, other, symlinks=True)
    _hold(origin, 3)
    theirs, _ = _hand_note(other, "theirs\n"), _hand_note(root, "mine\n")
    before = _refs(root)[SHADOW]
    rc1, rc2, took, err1, err2 = _race(origin, other, root)
    print(f"OBS third rc=({rc1},{rc2}) loser_s={took:.1f}")
    assert (rc1, rc2) == (0, 5), (err1, err2)
    assert took < 8, "refused once the other push's values landed, not at the retry bound"
    assert _refs(origin).get("refs/heads/main") == theirs == _refs(origin).get(SHADOW)
    assert _refs(root)[SHADOW] == before, "the loser's local shadow must not move"


def test_a_lock_held_past_the_bound_exits_5_near_it(tmp_path):
    root, origin = _store(tmp_path, "full")
    gr2(root, "push")
    _hold(origin, 18)
    _hand_note(root, "held long\n")
    rc1, rc2, took, err1, err2 = _race(origin, root, root)
    print(f"OBS long rc=({rc1},{rc2}) loser_s={took:.1f}")
    assert (rc1, rc2) == (0, 5), (err1, err2)
    assert 9 < took < 15, "the retry ends at its bound, about 10 s"


def test_a_refusal_that_is_not_a_lock_exits_5_at_once(tmp_path):
    root, origin = _store(tmp_path, "full")
    gr2(root, "push")
    _refuse_shadow(origin)
    _hand_note(root, "refused\n")
    start = time.monotonic()
    proc = _popen(root, "push")
    _, err = proc.communicate()
    took = time.monotonic() - start
    print(f"OBS hook rc={proc.returncode} s={took:.1f}")
    assert proc.returncode == 5, err
    assert took < 6, "a hook refusal is not retried"
