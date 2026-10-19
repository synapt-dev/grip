"""Git listings read by "\\n" or NUL, never str.splitlines (U+2028, U+0085 sit inside one name).

Rows:
A1 _refuse_moved_base names a default branch spelled with the separator in its base_moved refusal;
A2 _remote_tips finds a review tip whose ref holds the separator, inside or at the end of the id;
A3 _read_link refuses a link tree whose one entry is approval.json plus the separator as chain_broken,
   under quotePath true and false (not as an unmeasurable show of a path that is not there);
B1 a shadow pins tree is built over pin paths that hold a newline (mktree -z), nested or not;
M1 merge_gate._advertised reads the exact ref when a nested ref holding the separator also tail-matches;
M2 merge_gate._review_refs names reviews whose id holds the separator, inside or at the end;
G1 grip._is_field_tree_bind under quotePath false: an entry "a<sep>.grip" is not a .grip entry;
G2 grip._bind_root_names keeps "objects " and "a<sep>.grip" whole (the reader of :272 and :1011);
G3 grip._tree_keys under quotePath false keeps keys under objects/ and evidence/ whole: "a<U+2028>b",
   "a<U+0085>b", "a<newline>b" (not its C-quoted spelling) and "k " (the reader beside :272 and :1011);
C1 check_records._snapshot refuses a non-UTF-8 lookalike as cannot_measure (not UnicodeDecodeError),
   refuses a separator lookalike, and reads the one true check ref.
"""
from __future__ import annotations

import importlib
import subprocess

import pytest

from tests.test_delete import git  # noqa: F401

SEPS = pytest.mark.parametrize("sep", ["\u2028", "\u0085"], ids=["u2028", "u0085"])


def _mk(repo, data, *args):
    return subprocess.run(["git", "-C", str(repo), *args], input=data, capture_output=True,
                          check=True).stdout.decode().strip()


def _repo(tmp_path, quote_path="true"):
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    repo = tmp_path / "r"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    git(repo, "config", "core.quotePath", quote_path)
    blob = _mk(repo, b"x", "hash-object", "-w", "--stdin")
    tree = _mk(repo, f"100644 blob {blob}\tf\n".encode(), "mktree")
    one = git(repo, "commit-tree", tree, "-m", "one")
    two = git(repo, "commit-tree", tree, "-p", one, "-m", "two")
    return origin, repo, blob, one, two


@SEPS
def test_a1_base_moved_names_a_default_branch_with_a_line_separator(tmp_path, sep):
    ap = importlib.import_module("gr2.python_cli.approvals")
    origin, repo, _, one, two = _repo(tmp_path)
    branch = f"ma{sep}in"
    git(repo, "push", "-q", str(origin), f"{two}:refs/heads/{branch}")
    git(origin, "symbolic-ref", "HEAD", f"refs/heads/{branch}")
    with pytest.raises(ap.ApprovalRefused) as refused:
        ap._refuse_moved_base(repo, "m", str(origin), one, "gr:x")
    assert f"({branch} is now {two[:12]})" in str(refused.value)


@SEPS
@pytest.mark.parametrize("at", ["inside", "end"])
def test_a2_remote_tips_finds_a_review_ref_with_a_line_separator(tmp_path, sep, at):
    ap = importlib.import_module("gr2.python_cli.approvals")
    origin, repo, _, one, _ = _repo(tmp_path)
    rid = f"r{sep}1" if at == "inside" else f"r1{sep}"
    git(repo, "push", "-q", str(origin), f"{one}:{ap.PREFIX}{rid}")
    assert ap._remote_tips(repo, {"review_id": f"gr:{rid}", "members": [{"remote": str(origin)}]}) == {str(origin): one}


@SEPS
@pytest.mark.parametrize("quote_path", ["true", "false"])
def test_a3_read_link_refuses_approval_json_plus_a_line_separator(tmp_path, sep, quote_path):
    ap = importlib.import_module("gr2.python_cli.approvals")
    _, repo, blob, _, _ = _repo(tmp_path, quote_path)
    tree = _mk(repo, f"100644 blob {blob}\tapproval.json{sep}\0".encode(), "mktree", "-z")
    link = git(repo, "commit-tree", tree, "-m", "link")
    with pytest.raises(ap.ApprovalRefused) as refused:
        ap._read_link(repo, link)
    assert str(refused.value) == "approval_chain_broken: expected only plain approval.json"


def test_b1_shadow_pins_tree_holds_a_newline_in_a_pin_path(tmp_path):
    sh = importlib.import_module("gr2.python_cli.shadow")
    _, repo, blob, one, _ = _repo(tmp_path)
    base = _mk(repo, f"100644 blob {blob}\tb\n".encode(), "mktree")
    tree = sh._tree_with_pins(repo, base, {"a\nx": one, "d/e\nf": one, "d/g": one})
    listed = subprocess.run(["git", "-C", str(repo), "ls-tree", "-r", "-z", tree], capture_output=True,
                            check=True).stdout.decode().split("\0")
    assert sorted(e for e in listed if e) == sorted([f"100644 blob {blob}\tb", f"160000 commit {one}\t{sh.PINS}/a\nx",
                                                    f"160000 commit {one}\t{sh.PINS}/d/e\nf",
                                                    f"160000 commit {one}\t{sh.PINS}/d/g"])


def _bare(tmp_path, *names):
    """A bare remote whose refs `names` (str or bytes) all point at one commit; and an empty local repo."""
    origin = tmp_path / "o.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    blob = _mk(origin, b"x", "hash-object", "-w", "--stdin")
    tree = _mk(origin, f"100644 blob {blob}\tf\n".encode(), "mktree")
    commit = _mk(origin, None, "-c", "user.name=t", "-c", "user.email=t@t", "commit-tree", tree, "-m", "c")
    for name in names:
        subprocess.run([b"git", b"-C", str(origin).encode(), b"update-ref",
                        name if isinstance(name, bytes) else name.encode(), commit.encode()], check=True)
    repo = tmp_path / "r"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    return origin, repo, commit


@SEPS
def test_m1_advertised_reads_the_exact_ref_beside_a_nested_separator_ref(tmp_path, sep):
    from gr2.python_cli import merge_gate
    origin, repo, commit = _bare(tmp_path, "refs/heads/main", f"refs/x{sep}/refs/heads/main")
    assert merge_gate._advertised(repo, str(origin), "refs/heads/main") == commit


@SEPS
def test_m2_review_refs_name_ids_holding_the_separator(tmp_path, sep):
    from gr2.python_cli import merge_gate
    p = merge_gate._REVIEW_REF_PREFIX
    names = [f"{p}a{sep}b", f"{p}z{sep}"]
    origin, repo, _ = _bare(tmp_path, *names)
    assert merge_gate._review_refs(repo, str(origin)) == names


def _bind_commit(tmp_path, quote_path, *names):
    ws = tmp_path / "ws"
    subprocess.run(["git", "init", "-q", str(ws)], check=True)
    git(ws, "config", "core.quotePath", quote_path)
    blob = _mk(ws, b"x", "hash-object", "-w", "--stdin")
    tree = _mk(ws, b"".join(b"100644 blob %s\t%s\0" % (blob.encode(), n.encode()) for n in names), "mktree", "-z")
    return ws, _mk(ws, None, "-c", "user.name=t", "-c", "user.email=t@t", "commit-tree", tree, "-m", "c")


@SEPS
@pytest.mark.parametrize("quote_path", ["true", "false"])
def test_g1_field_tree_bind_is_not_fooled_by_a_separator_grip_entry(tmp_path, sep, quote_path):
    from gr2.python_cli import grip
    ws, commit = _bind_commit(tmp_path, quote_path, "f", f"a{sep}{grip.GRIP_DIR}")
    assert grip._is_field_tree_bind(ws, commit) is True


@SEPS
def test_g2_bind_root_names_keep_each_name_whole(tmp_path, sep):
    from gr2.python_cli import grip
    ws, commit = _bind_commit(tmp_path, "false", "objects ", f"a{sep}.grip", "f")
    assert grip._bind_root_names(ws, commit) == {"objects ", f"a{sep}.grip", "f"}


@pytest.mark.parametrize("sub", ["objects", "evidence"])
def test_g3_tree_keys_keep_each_key_whole(tmp_path, sub):
    from gr2.python_cli import grip
    keys = {"a\u2028b", "a\u0085b", "a\nb", "k "}
    ws, inner = _bind_commit(tmp_path, "false", *sorted(keys))
    sub_tree = _mk(ws, None, "rev-parse", f"{inner}^{{tree}}")
    root = _mk(ws, b"040000 tree %s\t%s\0" % (sub_tree.encode(), sub.encode()), "mktree", "-z")
    commit = _mk(ws, None, "-c", "user.name=t", "-c", "user.email=t@t", "commit-tree", root, "-m", "c")
    assert grip._tree_keys(ws, commit, sub) == keys
    assert grip._tree_keys(ws, commit, "absent") == set()


def _check_ref(*names):
    from gr2.python_cli import check_records
    return [n.replace(b"@", check_records.CHECK_REF.encode()) if isinstance(n, bytes)
            else n.replace("@", check_records.CHECK_REF) for n in names]


@pytest.mark.parametrize("name", [b"refs/\xff/@", "refs/x\u2028/@", "refs/x\u0085/@"],
                         ids=["non_utf8", "u2028", "u0085"])
def test_c1_snapshot_refuses_a_lookalike_check_ref(tmp_path, name):
    from gr2.python_cli import check_records
    origin, repo, _ = _bare(tmp_path, *_check_ref(name))
    with pytest.raises(check_records.CheckRefused, match="cannot_measure_check_ref"):
        check_records._snapshot(repo, str(origin))


def test_c1_snapshot_reads_the_one_true_check_ref(tmp_path):
    from gr2.python_cli import check_records
    origin, repo, commit = _bare(tmp_path, *_check_ref("@"))
    assert check_records._snapshot(repo, str(origin)) == commit
