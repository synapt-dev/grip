"""`store status` answers, per member, the questions operators otherwise ask with plain git.

THE MEASUREMENT: in a store-phase eval, operators typed 32 to 57 git commands per 5-repo arm to inspect members:
`rev-parse HEAD`, `status -sb`, `branch --show-current`, `ls-remote <member> refs/heads/main`. `store status` already
printed each member's pin and head, but not the branch it is on, how far it is ahead of or behind its upstream, or
whether it has uncommitted changes (it reported only the ROOT's porcelain).

THE RULE, bounded to those questions and nothing else: each member row also carries
  * `branch`: the branch the member is on, or null when its HEAD is detached;
  * `ahead` / `behind`: commits on HEAD not on the recorded upstream, and the reverse, read after the fetch status
    already makes (no new network read); null when the upstream cannot be measured;
  * `dirty`: the number of the member's own `status --porcelain` entries, 0 when clean, read with
    GIT_OPTIONAL_LOCKS=0 so the member's index is never rewritten (the member belongs to the operator; a plain
    `git status` refreshes stale stat data and writes the index, which the control row shows).
The text line answers at a glance, in a fixed order with short shas:
  `<name> <state> branch=<branch|-> head=<7> pin=<7> ahead=<n|?> behind=<n|?> dirty=<n>`

Fixtures are real git with bare local origins and no network.
"""
from __future__ import annotations

import json
from pathlib import Path

from tests.test_store_git_native_smoke import configure_identity, git, gr2, make_member, run


def _store(tmp_path: Path) -> Path:
    """alpha: on main, one commit ahead of origin, one untracked file. beta: detached at its pin, clean, and its
    origin one commit past the pin."""
    root = tmp_path / "workspace"
    root.mkdir()
    for name in ("alpha", "beta"):
        remote, _ = make_member(tmp_path, name)
        run(root, "git", "clone", "-q", str(remote), name)
        configure_identity(root / name)
    assert gr2(root, "init").returncode == 0
    configure_identity(root)
    assert gr2(root, "commit", "-m", "first").returncode == 0

    (root / "alpha" / "local.txt").write_text("local\n")
    git(root / "alpha", "add", "local.txt")
    git(root / "alpha", "commit", "-q", "-m", "not pushed")
    (root / "alpha" / "scratch.txt").write_text("untracked\n")

    git(root / "beta", "checkout", "-q", "--detach", "HEAD")
    other = tmp_path / "beta-elsewhere"
    run(tmp_path, "git", "clone", "-q", str(tmp_path / "beta.git"), str(other))
    configure_identity(other)
    (other / "later.txt").write_text("later\n")
    git(other, "add", "later.txt")
    git(other, "commit", "-q", "-m", "later on origin")
    git(other, "push", "-q", "origin", "main")
    return root


def _rows(root: Path) -> dict[str, dict]:
    done = gr2(root, "status", "--json", check=False)
    assert done.stdout.strip(), done.stderr
    return {row["name"]: row for row in json.loads(done.stdout.splitlines()[0])["members"]}


def test_each_row_names_the_branch_the_member_is_on_or_null_when_detached(tmp_path: Path) -> None:
    rows = _rows(_store(tmp_path))
    assert rows["alpha"]["branch"] == "main"
    assert rows["beta"]["branch"] is None


def test_each_row_counts_commits_ahead_and_behind_its_upstream(tmp_path: Path) -> None:
    rows = _rows(_store(tmp_path))
    assert (rows["alpha"]["ahead"], rows["alpha"]["behind"]) == (1, 0), rows["alpha"]
    assert (rows["beta"]["ahead"], rows["beta"]["behind"]) == (0, 1), rows["beta"]


def test_each_row_counts_the_members_own_uncommitted_entries(tmp_path: Path) -> None:
    rows = _rows(_store(tmp_path))
    assert rows["alpha"]["dirty"] == 1, rows["alpha"]
    assert rows["beta"]["dirty"] == 0, rows["beta"]


def test_head_and_pin_are_still_answered(tmp_path: Path) -> None:
    root = _store(tmp_path)
    rows = _rows(root)
    assert rows["alpha"]["head"] == git(root / "alpha", "rev-parse", "HEAD").stdout.strip()
    assert rows["alpha"]["pin"] != rows["alpha"]["head"]  # the unpushed commit moved HEAD off the pin
    assert rows["beta"]["head"] == rows["beta"]["pin"]


def test_the_text_line_answers_at_a_glance_in_a_fixed_order_with_short_shas(tmp_path: Path) -> None:
    root = _store(tmp_path)
    rows = _rows(root)
    lines = {line.split()[0]: line for line in gr2(root, "status", check=False).stdout.splitlines() if line.strip()}
    a, b = rows["alpha"], rows["beta"]
    assert lines["alpha"].startswith(
        f"alpha {a['state']} branch=main head={a['head'][:7]} pin={a['pin'][:7]} ahead=1 behind=0 dirty=1"), lines["alpha"]
    assert lines["beta"].startswith(
        f"beta {b['state']} branch=- head={b['head'][:7]} pin={b['pin'][:7]} ahead=0 behind=1 dirty=0"), lines["beta"]


def _index_fingerprint(member: Path) -> tuple[int, bytes]:
    index = member / ".git" / "index"
    return index.stat().st_mtime_ns, index.read_bytes()


def _stale_stat(member: Path) -> None:
    """Touch a tracked file without changing it, so its index entry's stat data is stale and a refreshing status
    would rewrite the index."""
    import os
    import time
    readme = member / "README.md"
    later = time.time() + 5
    os.utime(readme, (later, later))


def test_store_status_does_not_rewrite_a_members_index(tmp_path: Path) -> None:
    root = _store(tmp_path)
    _stale_stat(root / "alpha")
    before = _index_fingerprint(root / "alpha")
    _rows(root)
    assert _index_fingerprint(root / "alpha") == before


def test_control_a_plain_git_status_does_rewrite_that_index(tmp_path: Path) -> None:
    """Without this the row above could pass because nothing would have written the index anyway."""
    root = _store(tmp_path)
    _stale_stat(root / "alpha")
    before = _index_fingerprint(root / "alpha")
    git(root / "alpha", "status", "--porcelain")
    assert _index_fingerprint(root / "alpha") != before
