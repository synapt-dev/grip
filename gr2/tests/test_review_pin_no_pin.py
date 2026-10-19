"""A bare `review pin` in a workspace that records no pin (no `store init`) takes each member's base from the live
remote head of its ref, and refuses by name when that base cannot be the change's base.

Written by the author of the change (not by an independent reader). Each row names what must make it go red:
  * a member with no pin is skipped again (the old `pin is None -> skipped` branch)  -> the rows-equal row
  * a detached HEAD with no pin is pinned against the live head                      -> the detached row
  * members on different branches with no pin are pinned as one review               -> the branches row
  * a checkout the live head is not an ancestor of is pinned (or only skipped)       -> the behind row
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from tests.review_ref_helper import REVIEW_REF_PREFIX
from tests.test_store_break_attempts import _cli, _git, _git_out, _ls_remote, two_member_ws  # noqa: F401


def _flat(text: str) -> str:
    text = re.sub(r"\x1b\[[0-9;]*m", "", text)
    return " ".join(re.sub(r"[│╭╮╰╯─]", " ", text).split())


def _remote_url(clone: Path) -> str:
    return _git_out(clone, "remote", "get-url", "origin")


def _spec_ws(ws: Path) -> Path:
    """The workspace spec alone declares the members: no pin is recorded anywhere (no `store init`)."""
    (ws / ".grip").mkdir(exist_ok=True)
    text = "".join(
        f'[[repos]]\nname = "{n}"\npath = "{n}"\nurl = "{_remote_url(ws / n)}"\n\n' for n in ("alpha", "beta")
    )
    (ws / ".grip" / "workspace_spec.toml").write_text(text)
    assert not (ws / "grip.toml").exists()
    return ws


def _feature(clone: Path, branch: str = "feat/x") -> str:
    _git(clone, "checkout", "-q", "-b", branch)
    (clone / f"{clone.name}-change.txt").write_text(f"{clone.name} change\n")
    _git(clone, "add", ".")
    _git(clone, "commit", "-q", "-m", f"{clone.name} change")
    return _git_out(clone, "rev-parse", "HEAD")


def _gr_id(out: str) -> str:
    return [l for l in out.splitlines() if l.startswith("gr:")][-1]


def _shown(ws: Path, gr_id: str) -> list[tuple[str, str, str]]:
    code, out = _cli("review", "show", str(ws), gr_id, "--json")
    assert code == 0, out
    return sorted((m["key"], m["base"], m["head"]) for m in json.loads(out[out.index("{"):])["members"])


def _no_review_ref(ws: Path) -> bool:
    proc = subprocess.run(["git", "-C", str(ws), "for-each-ref", REVIEW_REF_PREFIX], capture_output=True, text=True)
    return not proc.stdout.strip()


def test_bare_pin_with_no_recorded_pin_equals_the_hand_built_rows(two_member_ws: Path, tmp_path: Path) -> None:
    """THE SMALLEST WORKING PROOF. Red on dev: exit 2 "nothing to pin ... the workspace records no usable pin"."""
    ws = _spec_ws(two_member_ws)
    heads = {n: _feature(ws / n) for n in ("alpha", "beta")}
    code, out = _cli("review", "pin", str(ws))
    assert code == 0, out
    bare = _shown(ws, _gr_id(out))
    rows = [{"key": n, "remote": _remote_url(ws / n), "path": n, "ref": "refs/heads/main",
             "base": _ls_remote(_remote_url(ws / n), "refs/heads/main"), "head": heads[n],
             "source": str((ws / n).resolve())} for n in ("alpha", "beta")]
    rows_file = tmp_path / "rows.json"
    rows_file.write_text(json.dumps(rows))
    code, out2 = _cli("review", "pin", str(ws), "--rows-json", str(rows_file))
    assert code == 0, out2
    assert bare == _shown(ws, _gr_id(out2)) == sorted((r["key"], r["base"], r["head"]) for r in rows), (bare, rows)


def test_detached_head_with_no_pin_refuses_by_name(two_member_ws: Path) -> None:
    ws = _spec_ws(two_member_ws)
    _feature(ws / "alpha")
    head = _feature(ws / "beta")
    _git(ws / "beta", "checkout", "-q", "--detach", head)
    code, out = _cli("review", "pin", str(ws))
    text = _flat(out)
    assert code == 2 and "Traceback" not in out, out
    assert "beta: detached HEAD and no workspace pin" in text and "no review id was minted" in text, text
    assert _no_review_ref(ws)


def test_members_on_different_branches_with_no_pin_refuse_the_set(two_member_ws: Path) -> None:
    ws = _spec_ws(two_member_ws)
    _feature(ws / "alpha", "feat/x")
    _feature(ws / "beta", "feat/y")
    code, out = _cli("review", "pin", str(ws))
    text = _flat(out)
    assert code == 2 and "Traceback" not in out, out
    assert "members on different branches and no workspace pin: alpha on feat/x, beta on feat/y" in text, text
    assert "no review id was minted" in text and _no_review_ref(ws)


def test_a_member_behind_its_live_head_refuses_rebase(two_member_ws: Path, tmp_path: Path) -> None:
    """The remote's main moved after the feature branch was cut, and the checkout fetched it: base would be a
    commit the change does not descend from, so the member is named behind, not pinned and not skipped."""
    ws = _spec_ws(two_member_ws)
    _feature(ws / "alpha")
    _feature(ws / "beta")
    other = tmp_path / "beta-other"
    subprocess.run(["git", "clone", "-q", _remote_url(ws / "beta"), str(other)], check=True, capture_output=True)
    (other / "moved.txt").write_text("moved\n")
    _git(other, "add", ".")
    _git(other, "-c", "user.name=t", "-c", "user.email=t@e.invalid", "commit", "-q", "-m", "moved")
    _git(other, "push", "-q", "origin", "main")
    _git(ws / "beta", "fetch", "-q", "origin")
    code, out = _cli("review", "pin", str(ws))
    text = _flat(out)
    assert code == 2 and "Traceback" not in out, out
    assert "beta is behind refs/heads/main; rebase onto it" in text and "no review id was minted" in text, text
    assert _no_review_ref(ws)
