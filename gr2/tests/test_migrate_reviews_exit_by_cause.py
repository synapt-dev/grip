"""`store migrate-reviews` exits by the cause of a setup refusal, not 5 for every one.

THE CONTRACT these rows pin:
  * run in the wrong place (a member checkout: no `.grip/` there), it refuses at 4, as `store add -C
    <member>` does, and names the store root above;
  * a store it could not read keeps 5: `.grip/` without `.git`, an info/exclude that cannot be located,
    a git action that failed.
The last two are not reached from a real migrate (they sit on store setup); their rows drive the real
producer through the verb's call, so the handler's mapping is what they pin.
"""

from __future__ import annotations

from pathlib import Path

from gr2.python_cli import grip as grip_mod
from tests.test_store_break_attempts import _cli
from tests.test_store_member_refusal_names_root_above import _ws


def _refused(code: int, out: str, expected: int) -> None:
    assert code == expected, out
    assert "Traceback" not in out, out
    assert "refused: " in out, out


def test_migrate_reviews_in_a_member_refuses_at_4_naming_the_root_above(tmp_path, monkeypatch):
    ws = _ws(tmp_path, monkeypatch)
    code, out = _cli("store", "migrate-reviews", "-C", str(ws / "alpha"))
    _refused(code, out, 4)
    assert f"No .grip/ directory at {ws / 'alpha'}" in out, out
    assert f"is inside the workspace at {ws}: pass the workspace root {ws}" in out, out


def test_migrate_reviews_on_a_grip_dir_without_git_refuses_at_5(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    root = tmp_path / "d"
    (root / ".grip").mkdir(parents=True)
    code, out = _cli("store", "migrate-reviews", "-C", str(root))
    _refused(code, out, 5)
    assert ".grip/ exists but has no .git/" in out, out


def test_migrate_reviews_when_info_exclude_cannot_be_located_refuses_at_5(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    root = tmp_path / "d"
    root.mkdir()
    monkeypatch.setattr(grip_mod, "migrate_review_binds", grip_mod.exclude_grip_state)
    code, out = _cli("store", "migrate-reviews", "-C", str(root))
    _refused(code, out, 5)
    assert "cannot locate the root's info/exclude" in out, out


def test_migrate_reviews_when_a_git_action_fails_refuses_at_5(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    root = tmp_path / "d"
    root.mkdir()

    def failing(workspace: Path):
        grip_mod._require_git_success(grip_mod.git(workspace, "rev-parse", "--verify", "HEAD"), "git rev-parse")

    monkeypatch.setattr(grip_mod, "migrate_review_binds", failing)
    code, out = _cli("store", "migrate-reviews", "-C", str(root))
    _refused(code, out, 5)
    assert "git rev-parse failed: " in out, out
