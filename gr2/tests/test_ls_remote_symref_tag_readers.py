"""The last three ls-remote readers read each listing through gitops.ls_remote_rows, by "\\n" only.

A ref name may hold U+2028 or U+0085 (<sep>); str.splitlines cuts a row at either, and str.strip eats one at the end
of the listing. A `--symref` row reads "ref: refs/heads/x\\tHEAD": it holds a tab, so ls_remote_rows keeps it as
("ref: refs/heads/x", "HEAD") and the readers test the "ref: " prefix as before; no separate symref parse is needed.

Rows:
S1 app._resolve_merged_defaults reads the remote default trunk past a lookalike symref refs/heads/x<sep>/HEAD. A
   guard: no ref name holds a space or a colon, so no lookalike can make a row that starts "ref: refs/heads/".
S2 app._resolve_merged_defaults reads a default named main<sep>x whole (a splitlines reader refuses it).
D1 defaults.target_branch refuses when main has moved and main<sep>x sits at the base (not main).
D2 the same with main<sep> at the base: the listing ends with <sep>, which strip would eat.
T1 app._remote_tag_shas does not count a local commit C as held because the remote has a tag named t<sep>C.
"""
from __future__ import annotations

import subprocess

import pytest

from gr2.python_cli import app, defaults, grip, merge_receipts

SEPS = pytest.mark.parametrize("sep", ["\u2028", "\u0085"], ids=["u2028", "u0085"])


def _g(repo, *args):
    return subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args],
                          capture_output=True, text=True, check=True).stdout.strip()


def _world(tmp_path):
    """A clone with commits A < B < C, and a bare remote holding main at B, HEAD -> main. Returns (remote, clone, A, B, C)."""
    clone = tmp_path / "clone"
    subprocess.run(["git", "init", "-q", str(clone)], check=True)
    tips = []
    for n in ("a", "b", "c"):
        _g(clone, "commit", "-q", "--allow-empty", "-m", n)
        tips.append(_g(clone, "rev-parse", "HEAD"))
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    _g(clone, "push", "-q", "--no-verify", str(remote), f"{tips[1]}:refs/heads/main")
    _g(remote, "symbolic-ref", "HEAD", "refs/heads/main")
    _g(clone, "remote", "add", "origin", str(remote))
    return remote, clone, *tips


def _merged_into(monkeypatch, tmp_path, clone, remote):
    view = {"id": "rv", "members": [{"path": clone.name, "remote": str(remote), "head": "x"}]}
    monkeypatch.setattr(grip, "show_review_commit", lambda ws, rid: view)
    monkeypatch.setattr(merge_receipts, "runs", lambda ws, rid: [])
    return app._resolve_merged_defaults(tmp_path, "gr:rv", None)[1]


@SEPS
def test_s1_merged_default_past_a_lookalike_symref(monkeypatch, tmp_path, sep):
    remote, clone, a, b, _ = _world(tmp_path)
    _g(remote, "update-ref", "refs/heads/other", a)
    _g(remote, "symbolic-ref", f"refs/heads/x{sep}/HEAD", "refs/heads/other")
    assert _merged_into(monkeypatch, tmp_path, clone, remote) == "main"


@SEPS
def test_s2_merged_default_named_with_sep(monkeypatch, tmp_path, sep):
    remote, clone, _, b, _ = _world(tmp_path)
    _g(remote, "update-ref", f"refs/heads/main{sep}x", b)
    _g(remote, "symbolic-ref", "HEAD", f"refs/heads/main{sep}x")
    assert _merged_into(monkeypatch, tmp_path, clone, remote) == f"main{sep}x"


@SEPS
@pytest.mark.parametrize("tail", ["x", ""], ids=["d1_inside", "d2_at_end"])
def test_d_target_branch_refuses_a_lookalike_at_the_base(tmp_path, sep, tail):
    remote, clone, a, b, _ = _world(tmp_path)
    _g(remote, "update-ref", f"refs/heads/main{sep}{tail}", a)  # the lookalike sits at the reviewed base A
    with pytest.raises(defaults.Unresolved):  # main itself is at B: no default branch sits at the base
        defaults.target_branch([(clone, str(remote), a)], None)


@SEPS
def test_t1_remote_tag_shas_not_fooled_by_a_tag_name(tmp_path, sep):
    remote, clone, a, _, c = _world(tmp_path)
    _g(remote, "update-ref", f"refs/tags/t{sep}{c}", a)  # C is only in the clone; the remote holds no ref at C
    assert app._remote_tag_shas(clone) == [a]
