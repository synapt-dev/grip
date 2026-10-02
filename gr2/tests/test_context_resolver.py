"""The context resolver (one function per value, each answering ``(value, source)``), proved on `lane show`.

Inside a workspace with a lane entered, `lane show` needs nothing typed: the root, the unit and the lane
come from the workspace and the entered lane's record, each inferred value is named on stderr with its
source, and `--json` carries the same pairs. A call with everything typed prints no source line and
gives the same answer.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from gr2.prototypes import lane_workspace_prototype as lane_proto
from gr2.python_cli.app import app

from tests.conftest import make_cli_runner


def _entered_workspace(tmp_path: Path, units: tuple[str, ...] = ("default",), entered: str = "default") -> tuple[Path, Path]:
    """A workspace root with a spec naming `units` and a current-lane record for `entered`; returns the
    root and a directory inside the entered lane (where a person who ran `lane enter` stands)."""
    root = tmp_path / "ws"
    (root / ".grip").mkdir(parents=True)
    (root / ".grip" / "workspace_spec.toml").write_text(
        "".join(f'[[units]]\nname = "{unit}"\npath = "agents/{unit}"\nrepos = []\n\n' for unit in units)
    )
    record = lane_proto.current_lane_file(root, entered)
    record.parent.mkdir(parents=True, exist_ok=True)
    record.write_text(json.dumps({"current": {"lane_name": "feat-x", "owner_unit": entered, "lane_type": "feature",
                                              "actor": "human:t", "entered_at": "t0"}, "recent": []}))
    inside = lane_proto.lane_dir(root, entered, "feat-x") / "repos"
    inside.mkdir(parents=True)
    return root, inside


def _plain(text: str) -> str:
    """A usage refusal is drawn by rich as a boxed, coloured, width-wrapped panel (CI's terminal width and
    colour differ from a developer's), so the words a row asserts on are compared with the colour codes and
    the box characters removed and the whitespace collapsed."""
    import re

    text = re.sub(r"\x1b\[[0-9;]*m", "", text)
    text = re.sub(r"[│╭╮╰╯─]", " ", text)
    return " ".join(text.split())


def _run(*args: str, cwd: Path, quiet: bool = False, verb: tuple[str, ...] = ("lane", "show"),
         extra_env: dict[str, str] | None = None):
    old = Path.cwd()
    env = dict(os.environ)
    env.pop("GR2_QUIET_CONTEXT", None)
    env.pop("GR2_ACTOR", None)
    if quiet:
        env["GR2_QUIET_CONTEXT"] = "1"
    env.update(extra_env or {})
    os.chdir(cwd)
    try:
        return make_cli_runner().invoke(app, [*verb, *args], env=env)
    finally:
        os.chdir(old)


def test_lane_show_inside_an_entered_lane_needs_nothing_typed(tmp_path: Path) -> None:
    root, inside = _entered_workspace(tmp_path)
    result = _run(cwd=inside)
    assert result.exit_code == 0, (result.stdout, result.stderr)
    assert "lane=feat-x" in result.stdout, "the lane must be shown"
    lines = [line for line in result.stderr.splitlines() if line.startswith("gr2:")]
    assert [line.split("=")[0] for line in lines] == ["gr2: root", "gr2: unit", "gr2: lane"], lines
    assert str(root.resolve()) in lines[0] and "nearest workspace" in lines[0]
    assert "default" in lines[1] and "entered lane" in lines[1]
    assert "feat-x" in lines[2] and "current lane of unit default" in lines[2]


def test_json_carries_the_chosen_pairs(tmp_path: Path) -> None:
    root, inside = _entered_workspace(tmp_path)
    result = _run("--json", cwd=inside)
    assert result.exit_code == 0, (result.stdout, result.stderr)
    doc = json.loads(result.stdout)
    assert doc["context"]["unit"]["value"] == "default" and "entered lane" in doc["context"]["unit"]["source"]
    assert doc["context"]["lane"]["value"] == "feat-x"
    assert doc["context"]["root"]["value"] == str(root.resolve())
    assert doc["current"]["lane_name"] == "feat-x"


def test_everything_typed_prints_no_source_line_and_answers_the_same(tmp_path: Path) -> None:
    root, inside = _entered_workspace(tmp_path)
    inferred = _run("--json", cwd=inside)
    typed = _run(str(root), "default", "--json", cwd=tmp_path)
    assert typed.exit_code == 0, (typed.stdout, typed.stderr)
    assert not [line for line in typed.stderr.splitlines() if line.startswith("gr2:")], typed.stderr
    assert json.loads(typed.stdout)["current"] == json.loads(inferred.stdout)["current"]
    assert json.loads(typed.stdout)["context"]["unit"]["source"] == "explicit"


def test_a_root_that_is_the_current_directory_is_not_announced(tmp_path: Path) -> None:
    root, _inside = _entered_workspace(tmp_path)
    result = _run(cwd=root)
    assert result.exit_code == 0, (result.stdout, result.stderr)
    assert not [line for line in result.stderr.splitlines() if line.startswith("gr2: root=")], result.stderr
    assert "gr2: unit=" in result.stderr


def test_the_quiet_switch_silences_the_lines_and_not_the_json(tmp_path: Path) -> None:
    _root, inside = _entered_workspace(tmp_path)
    result = _run("--json", cwd=inside, quiet=True)
    assert result.exit_code == 0 and "gr2:" not in result.stderr
    assert json.loads(result.stdout)["context"]["lane"]["value"] == "feat-x"


def test_several_units_with_no_entered_lane_refuse_and_list_them(tmp_path: Path) -> None:
    root, _inside = _entered_workspace(tmp_path, units=("alpha", "beta"), entered="alpha")
    (lane_proto.current_lane_file(root, "alpha")).write_text(json.dumps({"current": None, "recent": []}))
    result = _run(cwd=root)
    assert result.exit_code == 2
    text = _plain(result.stderr)
    assert "alpha" in text and "beta" in text and "--unit" in text, text


def test_a_word_that_is_both_a_root_and_a_unit_refuses_naming_both_readings(tmp_path: Path) -> None:
    root, _inside = _entered_workspace(tmp_path, units=("a",), entered="a")
    clash = root / "a"
    (clash / ".grip").mkdir(parents=True)
    (clash / ".grip" / "workspace_spec.toml").write_text("")
    result = _run("a", cwd=root)
    assert result.exit_code == 2
    text = _plain(result.stderr)
    assert "-C a" in text and "--unit a" in text, text


def test_the_only_unit_in_the_spec_is_used_when_no_lane_is_entered(tmp_path: Path) -> None:
    root, _inside = _entered_workspace(tmp_path)
    lane_proto.current_lane_file(root, "default").write_text(json.dumps({"current": None, "recent": []}))
    result = _run("--json", cwd=root)
    assert result.exit_code == 0, (result.stdout, result.stderr)
    assert "gr2: unit=default (the only unit in the workspace spec)" in result.stderr
    doc = json.loads(result.stdout)
    assert doc["current"] is None and "lane" not in doc["context"]


# ---- the actor: only from what the caller or the environment says -------------------------------------

from gr2.python_cli import context as ctx_mod  # noqa: E402


def test_the_actor_branches() -> None:
    explicit = ctx_mod.resolve_actor("agent:x", env={"GR2_ACTOR": "agent:env"}, tty=True, git_name="Ann")
    assert (explicit.value, explicit.source) == ("agent:x", "explicit")
    named = ctx_mod.resolve_actor(None, env={"GR2_ACTOR": "agent:env"}, tty=True, git_name="Ann")
    assert (named.value, named.source) == ("agent:env", "env GR2_ACTOR")
    person = ctx_mod.resolve_actor(None, env={}, tty=True, git_name="Ann")
    assert (person.value, person.source) == ("human:Ann", "git user.name")
    with pytest.raises(ctx_mod.ActorRefused):
        ctx_mod.resolve_actor(None, env={}, tty=False, git_name="Ann")  # an agent session has no terminal
    with pytest.raises(ctx_mod.ActorRefused):
        ctx_mod.resolve_actor(None, env={}, tty=True, git_name="")  # a terminal but no name to use


def test_lane_exit_bare_takes_its_actor_from_gr2_actor_and_marks_the_source(tmp_path: Path) -> None:
    root, inside = _entered_workspace(tmp_path)
    _lane_toml(root)
    result = _run(cwd=inside, verb=("lane", "exit"), extra_env={"GR2_ACTOR": "agent:t"})
    assert result.exit_code == 0, (result.stdout, result.stderr)
    lines = [line for line in result.stderr.splitlines() if line.startswith("gr2:")]
    assert [line.split("=")[0] for line in lines] == ["gr2: root", "gr2: unit", "gr2: actor"], lines
    assert lines[2] == "gr2: actor=agent:t (env GR2_ACTOR)"
    assert "((" not in result.stderr, "a source phrase carries nested parentheses"
    assert json.loads(lane_proto.current_lane_file(root, "default").read_text())["current"] is None


def test_lane_exit_with_no_actor_refuses_with_exit_4_and_changes_nothing(tmp_path: Path) -> None:
    root, inside = _entered_workspace(tmp_path)
    _lane_toml(root)
    before = lane_proto.current_lane_file(root, "default").read_text()
    result = _run(cwd=inside, verb=("lane", "exit"))
    assert result.exit_code == 4, (result.stdout, result.stderr)
    assert len(result.stderr.strip().splitlines()) == 1 and "GR2_ACTOR" in result.stderr
    assert lane_proto.current_lane_file(root, "default").read_text() == before


def test_a_typed_actor_still_wins_and_adds_no_source_line(tmp_path: Path) -> None:
    root, inside = _entered_workspace(tmp_path)
    _lane_toml(root)
    result = _run("--actor", "human:me", cwd=inside, verb=("lane", "exit"), extra_env={"GR2_ACTOR": "agent:t"})
    assert result.exit_code == 0, (result.stdout, result.stderr)
    assert "gr2: actor=" not in result.stderr


def test_lane_enter_with_only_the_lane_named_infers_the_root_and_the_unit(tmp_path: Path) -> None:
    root, inside = _entered_workspace(tmp_path)
    _lane_toml(root, "feat-y")
    result = _run("feat-y", cwd=inside, verb=("lane", "enter"), extra_env={"GR2_ACTOR": "agent:t"})
    assert result.exit_code == 0, (result.stdout, result.stderr)
    assert "gr2: unit=default" in result.stderr and "gr2: actor=agent:t (env GR2_ACTOR)" in result.stderr
    assert json.loads(lane_proto.current_lane_file(root, "default").read_text())["current"]["lane_name"] == "feat-y"


def test_the_hidden_exit_gr_no_longer_defaults_its_actor_to_agent_cli(tmp_path: Path) -> None:
    root, _inside = _entered_workspace(tmp_path)
    result = _run(str(root), "default", str(tmp_path), cwd=tmp_path, verb=("review", "exit-gr"))
    assert result.exit_code == 4, (result.stdout, result.stderr)
    assert "agent:cli" not in result.stdout + result.stderr


def _lane_toml(root: Path, name: str = "feat-x") -> None:
    path = lane_proto.lane_file(root, "default", name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'owner_unit = "default"\nlane_name = "{name}"\ntype = "feature"\nlane_type = "feature"\nrepos = []\n')


def _events(root: Path, kind: str) -> list[dict]:
    from gr2.python_cli import events

    outbox = events._outbox_path(root)
    rows = [json.loads(line) for line in outbox.read_text().splitlines() if line.strip()] if outbox.exists() else []
    return [row for row in rows if row.get("type") == kind]


def test_an_inferred_actor_is_marked_in_the_event_and_a_typed_one_is_not(tmp_path: Path) -> None:
    root, inside = _entered_workspace(tmp_path)
    _lane_toml(root)
    assert _run(cwd=inside, verb=("lane", "exit"), extra_env={"GR2_ACTOR": "agent:t"}).exit_code == 0
    inferred = _events(root, "lane.exited")
    assert inferred and inferred[-1].get("actor_source") == "env GR2_ACTOR", inferred
    typed_root, typed_inside = _entered_workspace(tmp_path / "again")
    _lane_toml(typed_root)
    assert _run("--actor", "human:me", cwd=typed_inside, verb=("lane", "exit")).exit_code == 0
    typed = _events(typed_root, "lane.exited")
    assert typed and "actor_source" not in typed[-1], typed


def test_a_terminal_with_no_git_on_path_refuses_with_the_actor_message_not_a_traceback(monkeypatch) -> None:
    monkeypatch.setenv("PATH", "/nonexistent")
    with pytest.raises(ctx_mod.ActorRefused):
        ctx_mod.resolve_actor(None, env={}, tty=True)  # git_name unset: it asks git, which is absent


def test_the_unit_given_twice_is_refused(tmp_path: Path) -> None:
    root, inside = _entered_workspace(tmp_path)
    result = _run(str(root), "default", "--unit", "other", cwd=tmp_path)
    assert result.exit_code == 2, (result.stdout, result.stderr)
    assert "given twice" in _plain(result.stderr), result.stderr
