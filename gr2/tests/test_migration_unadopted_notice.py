"""TDD spec: migrate-gr1 must SAY which gr1 worktrees it did not adopt.

Layne's finding from the 2026-09-21 walk of a real workspace: the generated spec declared every unit
at `agents/<unit>/home` regardless of where the unit's desk actually was, and the unit's real gr1
working tree was recorded only in `migration_source.worktree`, a field nothing read. A migration that
adopted nothing and one that adopted everything printed the same receipt.

The path half is since fixed: a unit is now declared at the location gr1 named for it. But this
command still writes a spec and inspects no working tree, so the disclosure is still owed — the
payload, the rendered output and the summary JSON must each name the recorded-but-unadopted
worktrees, and must say plainly that they were not adopted. Nothing here asserts a behaviour change
beyond those three surfaces.
"""
from __future__ import annotations

import json
from pathlib import Path

import yaml

from gr2.python_cli.migration import migrate_gr1_workspace, render_migration


def _write_gr1_workspace(root: Path, agents: str) -> None:
    """A minimal but real gr1 workspace: canonical manifest + agents.toml."""
    gitgrip = root / ".gitgrip"
    (gitgrip / "spaces" / "main").mkdir(parents=True)
    (gitgrip / "spaces" / "main" / "gripspace.yml").write_text(
        yaml.dump(
            {
                "version": 2,
                "manifest": {"url": "git@github.com:example/gripspace.git"},
                "repos": {
                    "alpha": {"url": "git@github.com:example/alpha.git", "path": "./alpha", "revision": "main"},
                },
            }
        )
    )
    (gitgrip / "agents.toml").write_text(agents)


_TWO_WORKTREES = (
    '[agents.atlas]\nworktree = "desk-a"\nchannel = "dev"\n\n'
    '[agents.apollo]\nworktree = "desk-b"\nchannel = "dev"\n'
)
_NO_WORKTREES = '[agents.atlas]\nchannel = "dev"\n\n[agents.apollo]\nchannel = "dev"\n'
_ONE_EMPTY_WORKTREE = '[agents.atlas]\nworktree = "  "\nchannel = "dev"\n\n[agents.apollo]\nworktree = "desk-b"\nchannel = "dev"\n'


def test_payload_names_every_recorded_but_unadopted_worktree(tmp_path: Path) -> None:
    """Subject: two units, two distinct recorded worktrees, both reported."""
    _write_gr1_workspace(tmp_path, _TWO_WORKTREES)
    payload = migrate_gr1_workspace(tmp_path)
    assert payload["not_adopted"] == [
        {"unit": "apollo", "worktree": "desk-b"},
        {"unit": "atlas", "worktree": "desk-a"},
    ]


def test_rendered_output_says_not_adopted_and_names_the_count(tmp_path: Path) -> None:
    """The human surface must carry the word, the names and the count."""
    _write_gr1_workspace(tmp_path, _TWO_WORKTREES)
    payload = migrate_gr1_workspace(tmp_path)
    rendered = render_migration(payload)
    assert "NOT ADOPTED" in rendered
    assert "desk-a" in rendered and "desk-b" in rendered
    assert "2 gr1 worktree(s)" in rendered
    assert "not adopted" in rendered.lower()


def test_summary_json_on_disk_carries_the_same_list(tmp_path: Path) -> None:
    """A later reader opens the summary, not the process's stdout."""
    _write_gr1_workspace(tmp_path, _TWO_WORKTREES)
    payload = migrate_gr1_workspace(tmp_path)
    summary_path = tmp_path / ".grip" / "migrations" / "gr1" / "migration-summary.json"
    summary = json.loads(summary_path.read_text())
    assert summary["not_adopted"] == payload["not_adopted"]


def test_control_no_recorded_worktrees_renders_no_section(tmp_path: Path) -> None:
    """Control: with nothing recorded the section must be absent, so it cannot
    be a constant that always prints.
    """
    _write_gr1_workspace(tmp_path, _NO_WORKTREES)
    payload = migrate_gr1_workspace(tmp_path)
    assert payload["not_adopted"] == []
    assert "NOT ADOPTED" not in render_migration(payload)


def test_whitespace_only_worktree_is_not_reported(tmp_path: Path) -> None:
    """A blank value is not a worktree: it must not appear as one, and must not
    suppress the real sibling's line either.
    """
    _write_gr1_workspace(tmp_path, _ONE_EMPTY_WORKTREE)
    payload = migrate_gr1_workspace(tmp_path)
    assert payload["not_adopted"] == [{"unit": "apollo", "worktree": "desk-b"}]


def test_non_string_worktree_value_is_not_reported_as_a_worktree(tmp_path: Path) -> None:
    """A malformed value is not a worktree.

    The field is a name in gr1's manifest. Anything else -- a list, a mapping,
    a number -- is malformed input, and reporting its repr as a worktree would
    put a line in a receipt that names nothing.
    """
    _write_gr1_workspace(
        tmp_path,
        '[agents.atlas]\nworktree = ["desk-a", "desk-b"]\nchannel = "dev"\n\n'
        '[agents.apollo]\nworktree = 7\nchannel = "dev"\n',
    )
    payload = migrate_gr1_workspace(tmp_path)
    assert payload["not_adopted"] == []
    assert "NOT ADOPTED" not in render_migration(payload)


def test_sentence_claims_only_what_the_migration_does(tmp_path: Path) -> None:
    """The rendered sentence is a claim about the migration, so it may only say
    what the migration did: the worktree was not adopted INTO this workspace.
    It must not assert where those trees are, which nothing here checks.
    """
    _write_gr1_workspace(tmp_path, _TWO_WORKTREES)
    rendered = render_migration(migrate_gr1_workspace(tmp_path))
    assert "were not adopted into this workspace" in rendered
    assert "outside this workspace" not in rendered
