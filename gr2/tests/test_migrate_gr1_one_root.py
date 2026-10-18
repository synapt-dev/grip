"""migrate-gr1 migrates ONE root: this root's own unit, and units inside it.

A gr1 agents manifest describes a FLEET: every desk sits beside the gripspace root at
`../<worktree>`, and the desk whose worktree is "main" works in the root itself. gr2 is one
root per workspace, so a unit whose desk is another directory is another desk's root. It is
not declared in this root's spec, and nothing under it is read; the receipt names it.

This retires the fleet-root mapping (one root declaring every desk). The harm the first sibling
fix removed stays removed: a dropped unit gets no unit and no nested home at all.

Measured on 2026-10-16: `gr2 workspace migrate-gr1 ~/Development/synapt-dev` wrote five units
(apollo -> ../synapt-dev, atlas, fathom, sentinel as siblings, and a stale opus -> "."), read
the other desks' dirty counts and stashes, and its own output then failed validation because
apollo and opus both resolve to the root.
"""
from __future__ import annotations

import json
import os
import subprocess
import tomllib
from pathlib import Path

import pytest
import yaml

from gr2.python_cli import gitops
from gr2.python_cli.app import app
from gr2.python_cli.migration import migrate_gr1_workspace, render_migration
from tests.conftest import make_cli_runner

runner = make_cli_runner()

_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "fx", "GIT_AUTHOR_EMAIL": "fx@example.invalid",
    "GIT_COMMITTER_NAME": "fx", "GIT_COMMITTER_EMAIL": "fx@example.invalid",
    "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull,
}

# The synapt-dev agents manifest as it was on 2026-10-16: five agents, one root.
_FLEET = {
    "opus": "main",
    "apollo": "synapt-dev",
    "atlas": "synapt-codex",
    "fathom": "synapt-fathom",
    "sentinel": "synapt-global",
}


def _git(cwd: Path, *args: str) -> None:
    done = subprocess.run(["git", *args], cwd=cwd, env=_ENV, capture_output=True, text=True)
    assert done.returncode == 0, f"git {' '.join(args)} failed in {cwd}: {done.stderr}"


def _gr1_root(parent: Path, name: str, agents: dict[str, str | None]) -> Path:
    """A gr1 gripspace at parent/<name>, one repo, and the given agents (None = no worktree)."""
    root = parent / name
    (root / ".gitgrip" / "spaces" / "main").mkdir(parents=True)
    (root / ".gitgrip" / "spaces" / "main" / "gripspace.yml").write_text(
        yaml.dump({
            "version": 2,
            "manifest": {"url": "https://example.invalid/gripspace.git"},
            "repos": {"alpha": {"url": "https://example.invalid/alpha.git", "path": "./alpha", "revision": "main"}},
        })
    )
    lines = []
    for unit, worktree in agents.items():
        lines.append(f"[agents.{unit}]")
        if worktree is not None:
            lines.append(f'worktree = "{worktree}"')
        lines.append('channel = "dev"\n')
    (root / ".gitgrip" / "agents.toml").write_text("\n".join(lines))
    return root


def _spec_units(root: Path) -> dict[str, str]:
    spec = tomllib.loads((root / ".grip" / "workspace_spec.toml").read_text())
    return {unit["name"]: unit["path"] for unit in spec["units"]}


# --- the head's four rows ---------------------------------------------------------------

def test_two_units_at_the_root_refuse_and_name_both(tmp_path: Path) -> None:
    """Apollo's exact command: apollo (worktree = this directory) and opus ("main") are both
    this root. The migration refuses, names both, says --unit, and writes no spec."""
    root = _gr1_root(tmp_path, "synapt-dev", _FLEET)
    result = runner.invoke(app, ["workspace", "migrate-gr1", str(root)])
    assert result.exit_code != 0
    text = result.output + str(result.exception or "")
    assert "apollo" in text and "opus" in text, text
    assert "--unit" in text, text
    assert not (root / ".grip" / "workspace_spec.toml").exists()


def test_unit_keeps_one_and_names_the_rest(tmp_path: Path) -> None:
    """--unit apollo writes ONE unit at "."; opus and the three siblings are named, not declared."""
    root = _gr1_root(tmp_path, "synapt-dev", _FLEET)
    result = runner.invoke(app, ["workspace", "migrate-gr1", str(root), "--unit", "apollo", "--json"])
    assert result.exit_code == 0, result.output
    assert _spec_units(root) == {"apollo": "."}
    payload = json.loads(result.output)
    assert {(row["unit"], row["path"]) for row in payload["not_migrated"]} == {
        ("opus", "."),
        ("atlas", "../synapt-codex"),
        ("fathom", "../synapt-fathom"),
        ("sentinel", "../synapt-global"),
    }


def test_a_unit_inside_the_root_is_kept(tmp_path: Path) -> None:
    """A unit with no worktree lives at agents/<unit>/home, inside the root: kept beside the root unit."""
    root = _gr1_root(tmp_path, "synapt", {"opus": "main", "helper": None, "atlas": "synapt-codex"})
    payload = migrate_gr1_workspace(root)
    assert _spec_units(root) == {"opus": ".", "helper": "agents/helper/home"}
    assert [(row["unit"], row["path"]) for row in payload["not_migrated"]] == [("atlas", "../synapt-codex")]


def test_a_sibling_desk_is_never_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The sibling desk holds a checkout whose .git cannot be read. The migration must not run
    git anywhere under it (the old receipt read each desk's dirty count, ahead count and
    stashes), and must not report on it beyond its name and path."""
    root = _gr1_root(tmp_path, "synapt", {"opus": "main", "atlas": "synapt-codex"})
    desk = tmp_path / "synapt-codex"
    checkout = desk / "alpha"
    checkout.mkdir(parents=True)
    _git(checkout, "init", "-q", "-b", "main", ".")
    # Every git command gr2 builds goes through gitops._spawn (looked up at call time), except the
    # version report's clone read (version.py), which the migration never calls;
    # gitops.start, the one long-lived process, is recorded beside it. The migration itself runs
    # no git here, so the recorder is proven live afterwards by one git call of the test's own.
    calls: list[tuple[list[str], str | None]] = []
    real = gitops._spawn

    def recording(argv, *, cwd, **kwargs):
        calls.append(([str(a) for a in argv], None if cwd is None else str(cwd)))
        return real(argv, cwd=cwd, **kwargs)

    real_start = gitops.start

    def recording_start(cwd, *args, **kwargs):
        calls.append((["git", "-C", str(cwd), *args], None))
        return real_start(cwd, *args, **kwargs)

    monkeypatch.setattr(gitops, "_spawn", recording)
    monkeypatch.setattr(gitops, "start", recording_start)
    (checkout / ".git").chmod(0)
    try:
        payload = migrate_gr1_workspace(root)
    finally:
        (checkout / ".git").chmod(0o755)
    desk_real = str(desk.resolve())

    def under_desk(path: str) -> bool:
        resolved = str(Path(path).resolve())
        return resolved == desk_real or resolved.startswith(desk_real + os.sep)

    seen = len(calls)
    gitops.git(tmp_path, "--version")
    assert len(calls) == seen + 1, "the recorder did not see git, so it cannot witness a read"
    assert [c for c in calls if c[1] is not None and under_desk(c[1])] == []
    assert [
        argv for argv, _ in calls
        if any(a == "-C" and i + 1 < len(argv) and under_desk(argv[i + 1]) for i, a in enumerate(argv))
    ] == []
    assert payload["not_migrated"] == [
        {"unit": "atlas", "path": "../synapt-codex", "reason": "another root, not migrated here"}
    ]
    assert "unreadable" not in json.dumps(payload)


def test_a_dropped_sibling_leaves_no_home_and_no_unit_state(tmp_path: Path) -> None:
    """The 09-26 harm stays blocked: migrating and then planning leaves nothing for the
    sibling inside this root (no agents/<name>/home, no .grip/state/units/<name>), and the
    plan has no operation for it."""
    root = _gr1_root(tmp_path, "synapt", {"opus": "main", "atlas": "synapt-codex"})
    migrate_gr1_workspace(root)
    from gr2.python_cli.spec_apply import build_plan

    _, operations = build_plan(root)
    assert [op for op in operations if op.subject == "atlas"] == []
    assert not (root / "agents" / "atlas").exists()
    assert not (root / ".grip" / "state" / "units" / "atlas").exists()


# --- receipt, refusals and repeat -------------------------------------------------------

def test_receipt_names_each_unit_not_migrated_in_render_and_summary(tmp_path: Path) -> None:
    root = _gr1_root(tmp_path, "synapt", {"opus": "main", "atlas": "synapt-codex", "fathom": "synapt-fathom"})
    payload = migrate_gr1_workspace(root)
    rendered = render_migration(payload)
    assert "NOT MIGRATED HERE" in rendered
    assert "- unit atlas -> ../synapt-codex: another root, not migrated here" in rendered
    assert "- unit fathom -> ../synapt-fathom: another root, not migrated here" in rendered
    assert "2 unit(s)" in rendered
    summary = json.loads((root / ".grip" / "migrations" / "gr1" / "migration-summary.json").read_text())
    assert summary["not_migrated"] == payload["not_migrated"]
    assert "not_adopted" not in summary


def test_control_only_the_root_unit_prints_no_section(tmp_path: Path) -> None:
    root = _gr1_root(tmp_path, "synapt", {"opus": "main"})
    payload = migrate_gr1_workspace(root)
    assert payload["not_migrated"] == []
    assert "NOT MIGRATED HERE" not in render_migration(payload)
    assert _spec_units(root) == {"opus": "."}


def test_unit_naming_a_unit_not_at_the_root_refuses(tmp_path: Path) -> None:
    root = _gr1_root(tmp_path, "synapt-dev", _FLEET)
    with pytest.raises(SystemExit) as exc:
        migrate_gr1_workspace(root, unit="atlas")
    message = str(exc.value)
    assert "--unit atlas is not a unit at this root" in message
    assert "apollo" in message and "opus" in message
    assert not (root / ".grip" / "workspace_spec.toml").exists()


def test_a_root_with_no_unit_of_its_own_refuses(tmp_path: Path) -> None:
    root = _gr1_root(tmp_path, "coordinator", {"atlas": "synapt-codex", "fathom": "synapt-fathom"})
    with pytest.raises(SystemExit) as exc:
        migrate_gr1_workspace(root)
    assert "no unit in the gr1 agents manifest is at this root" in str(exc.value)
    assert not (root / ".grip" / "workspace_spec.toml").exists()


def test_a_second_run_on_the_same_root_writes_the_same_spec(tmp_path: Path) -> None:
    """Re-running the verb (with --force, as a desk re-migrating would) gives the same one-unit spec."""
    root = _gr1_root(tmp_path, "synapt-dev", _FLEET)
    migrate_gr1_workspace(root, unit="apollo")
    first = (root / ".grip" / "workspace_spec.toml").read_bytes()
    migrate_gr1_workspace(root, force=True, unit="apollo")
    assert (root / ".grip" / "workspace_spec.toml").read_bytes() == first
    assert _spec_units(root) == {"apollo": "."}


def test_bootstrap_takes_the_same_unit_choice(tmp_path: Path) -> None:
    """bootstrap-gr1 compiles the same spec, so its refusal says --unit and the verb takes it:
    the printed advice, followed through the real verb, writes the one-unit spec."""
    root = _gr1_root(tmp_path, "synapt-dev", _FLEET)
    refused = runner.invoke(app, ["workspace", "bootstrap-gr1", str(root)])
    assert refused.exit_code != 0
    assert "--unit" in refused.output + str(refused.exception or "")
    assert not (root / ".grip" / "workspace_spec.toml").exists()
    done = runner.invoke(app, ["workspace", "bootstrap-gr1", str(root), "--unit", "apollo"])
    assert done.exit_code == 0, done.output
    assert _spec_units(root) == {"apollo": "."}
