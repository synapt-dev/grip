"""The out-of-set hint: printed by `check set` and `review merge`, never written into the receipt, never an exit code.

World: the NP-Bench contract scenario (test_np_bench_contract_scenario). The producer renames `amount` to
`amountCents`; `ui` reads `amount` and, unless declared, is in no set. Row D there measured that ui merges with every
gate green and stays broken; these rows pin that the commands now say so, and say nothing more than that.
"""
from __future__ import annotations

import json

from gr2.python_cli import set_hints
from gr2.python_cli.app import app
from tests.conftest import make_cli_runner
from tests.test_declared_sets import store_commit
from tests.test_np_bench_contract_scenario import ACCEPT, _world
from tests.test_review_merge_gate import _git_identity, git  # noqa: F401  (fixture and helper)

runner = make_cli_runner()


def check_set(w):
    return runner.invoke(app, ["check", "set", str(w["author"]), "--root-commit", w["root"],
                               "--set", "money", "--", *ACCEPT], catch_exceptions=False)


def hint_lines(result):
    return [line for line in result.stderr.splitlines() if line.startswith("hint: ")]


def test_check_set_prints_the_hint_for_ui_and_keeps_it_out_of_the_record(tmp_path, monkeypatch):
    w = _world(tmp_path, monkeypatch, consumer_adapted=True, in_set=["producer", "consumer"])
    out = check_set(w)
    assert out.exit_code == 0, out.output
    assert hint_lines(out) == [
        "hint: ui is in no declared set and still names `amount` in field.txt; set money moved off it "
        "(consumer, producer). Declare ui into the set if it reads what the set changes."], out.stderr
    assert "hint" not in out.stdout and json.loads(out.stdout)["result"] == "pass"


def test_ui_declared_into_the_set_has_nothing_to_hint(tmp_path, monkeypatch):
    w = _world(tmp_path, monkeypatch, consumer_adapted=True, in_set=["producer", "consumer", "ui"])
    out = check_set(w)
    assert out.exit_code == 1, out.output  # the joint check is red on ui, as row D' measured
    assert hint_lines(out) == []


def test_an_adapted_reader_outside_the_set_is_not_a_hit(tmp_path, monkeypatch):
    w = _world(tmp_path, monkeypatch, consumer_adapted=True, in_set=["producer"])
    out = check_set(w)
    assert [line.split()[1] for line in hint_lines(out)] == ["ui"], out.stderr  # amountCents is not the WORD amount


def test_review_merge_prints_the_hint_and_the_receipt_and_exit_are_unchanged(tmp_path, monkeypatch):
    w = _world(tmp_path, monkeypatch, consumer_adapted=True, in_set=["producer", "consumer"])
    first = check_set(w)
    assert first.exit_code == 0, first.output
    with monkeypatch.context() as quiet:  # the same merge with the hint switched off is the control
        quiet.setattr(set_hints, "out_of_set_hints", lambda *a, **k: [])
        control = runner.invoke(app, ["review", "merge", w["members"]["consumer"]["review"], "--from", "feat",
                                      "--into", "main"], catch_exceptions=False)
    out = runner.invoke(app, ["review", "merge", w["members"]["producer"]["review"], "--from", "feat",
                              "--into", "main"], catch_exceptions=False)
    assert control.exit_code == out.exit_code == 0, (control.output, out.output)
    assert hint_lines(control) == []
    assert [line.split()[1] for line in hint_lines(out)] == ["ui"], out.stderr
    receipt = json.loads(out.stdout)
    assert "hint" not in out.stdout and receipt["exit"] == 0 and receipt["members"][0]["merged"], receipt


def test_a_common_removed_name_never_prints_a_wall(tmp_path, monkeypatch):
    w = _world(tmp_path, monkeypatch, consumer_adapted=True, in_set=["producer", "consumer"])
    names = [f"common_name_{i}" for i in range(set_hints.MAX_LINES + 3)]
    producer, ui = w["members"]["producer"]["repo"], w["members"]["ui"]["repo"]
    (producer / "names.txt").write_text("\n".join(names) + "\n")
    git(producer, "add", "names.txt")
    git(producer, "commit", "-qm", "names")
    git(producer, "push", "-q", "origin", "feat")
    for i in range(set_hints.MAX_PATHS + 9):
        (ui / f"reader_{i:02}.txt").write_text(" ".join(names) + "\n")
    git(ui, "add", ".")
    git(ui, "commit", "-qm", "ui reads every name in many files")
    git(ui, "push", "-q", "origin", "feat")
    store_commit(w["author"], "producer adds names; ui reads them")
    git(producer, "rm", "-q", "names.txt")
    git(producer, "commit", "-qm", "producer removes every name")
    git(producer, "push", "-q", "origin", "feat")
    w["root"] = store_commit(w["author"], "producer moves off every name")
    lines = set_hints.out_of_set_hints(w["author"], w["root"], "money")
    print("\n".join(lines))
    assert len(lines) == set_hints.MAX_LINES + 1, lines
    # nine hits: the eight common names, plus `amount`, which the consumer still moves off against main; `amount` is
    # a plain word, so the identifier-shaped names fill the five lines and it is one of the four not shown
    assert lines[-1] == "hint: 4 more out-of-set name(s) not shown"
    assert sum("reader_00.txt, reader_01.txt, reader_02.txt and 9 more" in line for line in lines[:-1]) == set_hints.MAX_LINES, lines
    assert max(len(line) for line in lines) < 300, lines


def _producer_changes(w, before, after, ui_text, keep=None):
    producer, ui = w["members"]["producer"]["repo"], w["members"]["ui"]["repo"]
    (producer / "order.py").write_text(before)
    if keep is not None:
        (producer / "keep.txt").write_text(keep)
    git(producer, "add", ".")
    git(producer, "commit", "-qm", "order")
    git(producer, "push", "-q", "origin", "feat")
    (ui / "view.py").write_text(ui_text)
    git(ui, "add", ".")
    git(ui, "commit", "-qm", "ui reads")
    git(ui, "push", "-q", "origin", "feat")
    store_commit(w["author"], "before")
    (producer / "order.py").write_text(after)
    git(producer, "commit", "-qam", "change")
    git(producer, "push", "-q", "origin", "feat")
    w["root"] = store_commit(w["author"], "after")
    return set_hints.out_of_set_hints(w["author"], w["root"], "money")


def test_a_rename_is_not_crowded_out_by_the_words_of_a_deleted_comment(tmp_path, monkeypatch):
    # In alphabetical order, `add`, `all`, `also`, `amount`, `and` from a deleted comment would fill the five
    # lines and drop the real rename into "more not shown".
    w = _world(tmp_path, monkeypatch, consumer_adapted=True, in_set=["producer", "consumer"])
    lines = _producer_changes(w, "# all of the old one, and also any add-on\nzeta_total = 1\n", "zetaTotalCents = 100\n",
                              "# and also all of any one old add-on here\nprint(order.zeta_total)\n")
    assert "`zeta_total`" in lines[0], lines


def test_a_word_the_set_member_still_uses_was_not_moved_off(tmp_path, monkeypatch):
    w = _world(tmp_path, monkeypatch, consumer_adapted=True, in_set=["producer", "consumer"])
    lines = _producer_changes(w, "# also the old one\nzeta_total = 1\n", "zetaTotalCents = 100\n",
                              "# also\nprint(order.zeta_total)\n", keep="also kept here\n")
    named = [line.split("`")[1] for line in lines if "`" in line]
    assert "zeta_total" in named and "also" not in named, lines


def test_an_error_inside_the_hint_changes_nothing(tmp_path, monkeypatch):
    # The no-hint-on-error path, with its own row.
    w = _world(tmp_path, monkeypatch, consumer_adapted=True, in_set=["producer", "consumer"])
    control = check_set(w)
    assert hint_lines(control), control.stderr

    def boom(*a, **k):
        raise RuntimeError("forced")

    monkeypatch.setattr(set_hints, "_removed_names", boom)
    out = check_set(w)
    assert out.exit_code == control.exit_code and hint_lines(out) == [] and out.stdout == control.stdout
