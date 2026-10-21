"""A full-mode root is a plain git repository: `git status`, `git diff` and `git log` at the root agree with
`gr2 store status`, `gr2 store diff` and `gr2 store log`.

Three states on grip's own full-mode fixture: A after `gr2 store commit`; B after a member commit gr2 has not recorded
(the root shows ` M alpha`); C after `gr2 store commit` records it. beta stays upstream throughout as the control.
These rows pin an invariant that already holds. A plain `git merge` at the root carries gitlinks and grip.toml pins
together, with no gr2 verb. A plain root `git commit` moves the gitlink but not the pin, so status and check refuse
with exit 4 until `gr2 store commit` records it.
"""
from __future__ import annotations

import json
import tomllib
from pathlib import Path

from tests.test_store_git_native_smoke import git
from tests.test_store_git_native_smoke import gr2
from tests.test_store_shadow_s1 import _advance, _store


def _status(root: Path) -> tuple[dict, dict[str, dict]]:
    doc = json.loads(gr2(root, "status", "--json").stdout)
    return doc["root"], {m["name"]: m for m in doc["members"]}


def _gitlink(root: Path, rev: str, name: str) -> str:
    return git(root, "rev-parse", f"{rev}:{name}").stdout.strip()


def _plain_diff_moves(text: str) -> dict[str, tuple[str, str]]:
    """{path: (old, new)} for the gitlinks a plain `git diff` moves (its `Subproject commit` lines); files are skipped."""
    moves: dict[str, list[str]] = {}
    path = None
    for line in text.splitlines():
        if line.startswith("diff --git a/"):
            path = line.split(" b/", 1)[1]
            moves[path] = ["", ""]
        elif line.startswith("-Subproject commit "):
            moves[path][0] = line.split()[-1]
        elif line.startswith("+Subproject commit "):
            moves[path][1] = line.split()[-1]
    return {p: (old, new) for p, (old, new) in moves.items() if old or new}


def _log_pair(root: Path) -> tuple[list[str], list[str]]:
    """Root subjects oldest first (gr2 store log lists oldest first) and gr2 store log messages."""
    plain = git(root, "log", "--reverse", "--format=%s").stdout.splitlines()
    entries = json.loads(gr2(root, "log", "--json").stdout)["entries"]
    return plain, [e["message"] for e in entries]


def test_root_git_status_and_store_status_agree_at_every_state(tmp_path: Path) -> None:
    root, _ = _store(tmp_path, "full")
    beta = _gitlink(root, "HEAD", "beta")

    porcelain = git(root, "status", "--porcelain").stdout.splitlines()
    head, rows = _status(root)
    assert (porcelain, head["state"], head["porcelain"]) == ([], "clean", [])
    assert rows["alpha"]["head"] == rows["alpha"]["gitlink"] == _gitlink(root, "HEAD", "alpha")

    _advance(root, "alpha")
    porcelain = git(root, "status", "--porcelain").stdout.splitlines()
    head, rows = _status(root)
    assert porcelain == [" M alpha"]
    assert (head["state"], head["porcelain"]) == ("dirty", porcelain)
    assert rows["alpha"]["gitlink"] == _gitlink(root, "HEAD", "alpha")
    assert rows["alpha"]["head"] == git(root / "alpha", "rev-parse", "HEAD").stdout.strip() != rows["alpha"]["gitlink"]
    assert (rows["beta"]["state"], rows["beta"]["gitlink"]) == ("upstream", beta)

    gr2(root, "commit", "-m", "record alpha")
    porcelain = git(root, "status", "--porcelain").stdout.splitlines()
    head, rows = _status(root)
    assert (porcelain, head["state"], head["porcelain"]) == ([], "clean", [])
    assert rows["alpha"]["head"] == rows["alpha"]["gitlink"] == _gitlink(root, "HEAD", "alpha")
    assert (rows["beta"]["state"], rows["beta"]["gitlink"]) == ("upstream", beta)


def test_root_git_diff_and_store_diff_name_the_same_gitlink_move(tmp_path: Path) -> None:
    root, _ = _store(tmp_path, "full")
    beta = _gitlink(root, "HEAD", "beta")

    _advance(root, "alpha")
    _, rows = _status(root)
    moves = _plain_diff_moves(git(root, "diff").stdout)
    assert moves == {"alpha": (rows["alpha"]["gitlink"], rows["alpha"]["head"])}
    assert rows["alpha"]["gitlink"] != rows["alpha"]["head"]

    gr2(root, "commit", "-m", "record alpha")
    before, after = _gitlink(root, "HEAD~1", "alpha"), _gitlink(root, "HEAD", "alpha")
    assert before != after
    assert _plain_diff_moves(git(root, "diff", "HEAD~1", "HEAD").stdout) == {"alpha": (before, after)}
    members = {m["name"]: m for m in json.loads(gr2(root, "diff", "HEAD~1", "HEAD", "--json").stdout)["members"]}
    assert (members["alpha"]["old_pin"], members["alpha"]["new_pin"], members["alpha"]["changed"]) == (before, after, True)
    assert (members["beta"]["old_pin"], members["beta"]["new_pin"], members["beta"]["changed"]) == (beta, beta, False)


def test_root_git_log_and_store_log_list_the_same_entries_in_order(tmp_path: Path) -> None:
    root, _ = _store(tmp_path, "full")

    plain, store = _log_pair(root)
    assert plain == store and len(plain) == 1

    _advance(root, "alpha")
    gr2(root, "commit", "-m", "record alpha")
    plain, store = _log_pair(root)
    assert plain == store and plain[-1] == "record alpha" and len(plain) == 2


# today's model; the section 13 port replaces this seam


def test_root_plain_git_commit_is_refused_until_store_commit(tmp_path: Path) -> None:
    root, _ = _store(tmp_path, "full")
    beta = _gitlink(root, "HEAD", "beta")
    _advance(root, "alpha")
    git(root, "add", "alpha")
    git(root, "commit", "-q", "-m", "plain root commit")
    assert _gitlink(root, "HEAD", "alpha") == git(root / "alpha", "rev-parse", "HEAD").stdout.strip()

    status = gr2(root, "status", "--json", check=False)
    assert status.returncode == 4, status.stdout + status.stderr
    states = {m["name"]: m["state"] for m in json.loads(status.stdout)["members"]}
    assert states == {"alpha": "unpinned", "beta": "upstream"}
    check = gr2(root, "check", check=False)
    assert check.returncode == 4, check.stdout + check.stderr
    said = check.stdout + check.stderr
    assert "outside gr2" in said and "gr2 store commit" in said

    gr2(root, "commit", "-m", "record alpha")
    assert _gitlink(root, "HEAD", "beta") == beta
    _assert_carried(root)


def _assert_carried(root: Path) -> None:
    """Root gitlinks equal the member heads, grip.toml pins equal the same shas, and status and check both pass."""
    heads = {m: git(root / m, "rev-parse", "HEAD").stdout.strip() for m in ("alpha", "beta")}
    links = {m: _gitlink(root, "HEAD", m) for m in heads}
    pins = {m["name"]: m["pin"] for m in tomllib.loads((root / "grip.toml").read_text(encoding="utf-8"))["members"]}
    assert links == heads == pins
    status = gr2(root, "status", "--json", check=False)
    assert status.returncode == 0, status.stdout + status.stderr
    assert {m["name"]: m["state"] for m in json.loads(status.stdout)["members"]} == {"alpha": "upstream", "beta": "upstream"}
    check = gr2(root, "check", check=False)
    assert check.returncode == 0, check.stdout + check.stderr


def test_root_plain_git_merge_carries_gitlink_and_pin(tmp_path: Path) -> None:
    root, _ = _store(tmp_path, "full")
    main = git(root, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    base = {m: _gitlink(root, "HEAD", m) for m in ("alpha", "beta")}

    # fast-forward: a side branch records alpha with gr2; main takes it with plain git only
    git(root, "checkout", "-q", "-b", "side")
    _advance(root, "alpha")
    gr2(root, "commit", "-m", "side records alpha")
    side = git(root, "rev-parse", "HEAD").stdout.strip()
    git(root, "checkout", "-q", main)
    git(root, "merge", "--ff-only", "-q", "side")
    assert git(root, "rev-parse", "HEAD").stdout.strip() == side
    assert _gitlink(root, "HEAD", "alpha") != base["alpha"] and _gitlink(root, "HEAD", "beta") == base["beta"]
    _assert_carried(root)

    # three-way: main records beta, side2 records alpha; the member worktrees are shared, so each side's other member
    # is put back at that side's gitlink before its commit, and beta returns to main's pin after the merge
    fork = git(root, "rev-parse", "HEAD").stdout.strip()
    old_beta = _gitlink(root, "HEAD", "beta")
    _advance(root, "beta")
    gr2(root, "commit", "-m", "main records beta")
    new_beta = _gitlink(root, "HEAD", "beta")
    git(root, "checkout", "-q", "-b", "side2", fork)
    git(root / "beta", "reset", "-q", "--hard", old_beta)
    _advance(root, "alpha")
    gr2(root, "commit", "-m", "side2 records alpha")
    new_alpha = _gitlink(root, "HEAD", "alpha")
    assert _gitlink(root, "HEAD", "beta") == old_beta
    git(root, "checkout", "-q", main)
    git(root, "merge", "--no-edit", "side2")
    git(root / "beta", "reset", "-q", "--hard", new_beta)
    assert len(git(root, "rev-list", "--parents", "-n1", "HEAD").stdout.split()) == 3
    assert (_gitlink(root, "HEAD", "alpha"), _gitlink(root, "HEAD", "beta")) == (new_alpha, new_beta)
    assert git(root, "status", "--porcelain").stdout == ""
    _assert_carried(root)
