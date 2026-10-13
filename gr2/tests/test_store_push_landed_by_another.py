"""(c2) a refused atomic push whose refs ALL already sit at their targets on origin reports pushed; a real loser
(different target) still exits 5 and its local shadow does not move.."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from tests.test_store_git_native_smoke import git, gr2
from tests.test_store_shadow_s1 import SHADOW, _refs, _store
from tests.test_store_shadow_fence import _hand_note


def _popen(cwd, *args):
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).parents[1])}
    return subprocess.Popen([sys.executable, "-m", "gr2.python_cli.app", "store", *args], cwd=cwd, text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)

LINE = "Another push landed the same refs first"


def test_same_root_two_pushes_at_once_no_false_exit_5(tmp_path):
    root, origin = _store(tmp_path, "full")
    gr2(root, "push")
    fired = 0
    for i in range(3):
        hand = _hand_note(root, f"round {i}\n")
        p1, p2 = _popen(root, "push"), _popen(root, "push")
        (o1, e1), (o2, e2) = p1.communicate(), p2.communicate()
        remote = _refs(origin)
        fired += (LINE in o1) + (LINE in o2)
        print(f"OBS c2 round={i} rc=({p1.returncode},{p2.returncode}) line=({LINE in o1},{LINE in o2}) "
              f"e={(e1 + ' | ' + e2).strip()[-200:]!r}")
        assert (p1.returncode, p2.returncode) == (0, 0), (e1, e2)
        assert remote.get("refs/heads/main") == hand == remote.get(SHADOW) == _refs(root)[SHADOW]
    print(f"OBS c2 landed_by_another fired={fired} of 3 rounds")
    assert fired >= 1, "the race never reached the landed-by-another path; the row proves nothing"


def test_real_loser_still_exits_5_and_its_shadow_stays(tmp_path):
    root, origin = _store(tmp_path, "full")
    gr2(root, "push")
    other = tmp_path / "workspace2"
    shutil.copytree(root, other, symlinks=True)
    for i in range(3):
        before = {ws: _refs(ws)[SHADOW] for ws in (root, other)}
        mine, theirs = _hand_note(root, f"one {i}\n"), _hand_note(other, f"two {i}\n")
        p1, p2 = _popen(root, "push"), _popen(other, "push")
        (o1, e1), (o2, e2) = p1.communicate(), p2.communicate()
        remote = _refs(origin)
        win = root if remote.get("refs/heads/main") == mine else other if remote.get("refs/heads/main") == theirs else None
        print(f"OBS c round={i} rc=({p1.returncode},{p2.returncode}) winner={win and win.name} "
              f"e={(e1 + ' | ' + e2).strip()[-160:]!r}")
        assert win is not None and remote.get("refs/heads/main") == remote.get(SHADOW)
        loser = other if win == root else root
        assert sorted((p1.returncode, p2.returncode)) == [0, 5], "different targets: exactly one wins"
        assert _refs(loser)[SHADOW] == before[loser], "the loser's local shadow must not move"
        assert LINE not in o1 + o2
        for ws in (root, other):
            git(ws, "fetch", "-q", "origin")
            git(ws, "reset", "-q", "--hard", "origin/main")
            git(ws, "update-ref", SHADOW, remote[SHADOW])
