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

import pytest

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


# The exact codes, written out by hand: a floor let a renamed row pass. Change this set
# in the same PR as next_steps.py, in front of a reader.
EXPECTED_CODES = {
    "approvals_insufficient", "approvals_unset", "base_moved", "check_absent", "check_fail", "check_set*",
    "check_set_fail",
    "feature_moved", "head_already_on_remote", "head_behind_remote_branch", "head_not_fetched", "not_received",
    "platform_head_moved_after_merge", "platform_head_unmeasured_after_merge", "platform_pr_ambiguous", "platform_pr_not_ready", "platform_unmeasurable",
    "push_rejected", "remote_branch_diverged", "review_not_on_member_remote", "self_approval", "target_deleted_at_push",
    "target_moved_at_push", "target_unread_at_push",
}


def test_the_refusal_codes_are_exactly_the_expected_set():
    codes = {code for code, _ in _generator().refusal_rows()}
    assert codes == EXPECTED_CODES, (
        f"missing {sorted(EXPECTED_CODES - codes)}, unexpected {sorted(codes - EXPECTED_CODES)}")


def test_a_real_zero_default_is_printed_and_an_empty_one_is_not():
    default = _generator()._default
    assert default(0) == " [default: 0]"
    assert default(0.0) == " [default: 0.0]"
    for empty in (None, False, "", (), []):
        assert default(empty) == "", empty


def _frontmatter(path):
    # Parsed as YAML, the way a skill loader reads it: an unquoted ": " in a value is an
    # error there, and a split on the first colon would hide it.
    import yaml

    return yaml.safe_load(path.read_text(encoding="utf-8").split("---")[1])


GITGRIP_SKILL = GR2.parent / "docs" / "SKILL.md"


def test_the_skill_says_it_is_for_gr2_not_gitgrip():
    """Two similar skills installed together conflict, and the model picks by name and
    description alone, so this one names gr2 first and says it is not gitgrip's `gr`."""
    ours = _frontmatter(GR2 / "api" / "skill.intro.md")
    assert ours["description"].startswith("For the gr2 command (not the gr command of gitgrip)")


def test_the_skill_name_differs_from_the_gitgrip_skill():
    # gitgrip's skill lives outside gr2/, so a gr2-only tree (an archive of gr2/ alone) has
    # nothing to compare against: that is a skip with its reason, never a pass. The skip keys
    # on the REPO being absent (no Cargo.toml beside gr2/), not on the file: inside a real
    # checkout a deleted docs/SKILL.md must fail here, not skip quietly.
    if not (GR2.parent / "Cargo.toml").is_file():
        pytest.skip(f"{GR2.parent} is not the grip repo (a gr2-only tree), so {GITGRIP_SKILL} is not here")
    assert _frontmatter(GR2 / "api" / "skill.intro.md")["name"] != _frontmatter(GITGRIP_SKILL)["name"]
