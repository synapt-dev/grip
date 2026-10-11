"""The gates on gr2's generated review skill, ``api/skill.md``.

- The committed file must equal what ``scripts/dump_skill.py`` renders now, so a changed
  verb, default or next step changes the skill in the same PR.
- Population floors, so a generator that silently lost its verbs or its refusal rows
  cannot pass by also rewriting the committed file to match.
- The facts that cost real runs when a hand skill missed them are asserted by name: the
  default of ``review pin --ref`` and the next step for ``head_already_on_remote``.
- Options with no help text in the code are COUNTED. Lower the count when you add help;
  never raise it to quiet a red.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

GR2 = Path(__file__).resolve().parents[1]
SKILL = GR2 / "api" / "skill.md"
GENERATOR = GR2 / "scripts" / "dump_skill.py"

MIN_VERBS = 9
MIN_REFUSAL_ROWS = 10
MAX_OPTIONS_WITHOUT_HELP = 5


def _generator():
    spec = importlib.util.spec_from_file_location("dump_skill", GENERATOR)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_skill_dump_is_current():
    assert SKILL.read_text(encoding="utf-8") == _generator().render(), (
        "api/skill.md is stale: run `python scripts/dump_skill.py > api/skill.md` from gr2/")


def test_skill_keeps_its_population():
    text = _generator().render()
    verbs = text.count("\n### gr2 ")
    refusals = text.split("## When gr2 prints `refused: <code>`", 1)[1].count("\n### ")
    assert verbs >= MIN_VERBS, verbs
    assert refusals >= MIN_REFUSAL_ROWS, refusals


def test_the_defaults_and_next_steps_that_cost_runs_are_present():
    text = _generator().render()
    assert "`--ref` [default: refs/heads/dev]" in text
    after = text.split("### head_already_on_remote\n\n", 1)[1]
    assert after.startswith("next: pin a head before you push it")


def test_options_without_help_do_not_grow():
    count = _generator().render().count(_generator().NO_HELP)
    assert count <= MAX_OPTIONS_WITHOUT_HELP, (
        f"{count} options have no help text (limit {MAX_OPTIONS_WITHOUT_HELP}); "
        "add help to the new option")
