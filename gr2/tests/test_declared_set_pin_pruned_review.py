"""A review the remote advertises but can no longer serve (its oid pruned) does not hide the others.

The tie reads every review on a sibling's remote in one fetch by id. When one oid is gone that fetch fails whole, so the
tie falls back to one fetch per review: a review that still reads and names the pin ties it, as the review-by-review
read did before. When none does, the refusal names the ref(s) whose oid failed and prints git's first fatal or error
line once. Any other batch failure refuses with it, after that one fetch."""
from __future__ import annotations

import os
import subprocess

from gr2.python_cli import merge_gate
from tests.test_declared_set_pins import _move_beta, _refusal
from tests.test_declared_sets import merge
from tests.test_declared_sets import PASS, record, untouched, world  # noqa: F401  (fixtures)

PRUNED = "refs/dev.synapt.grip/__reviews__/zz-pruned"
GONE = "0" * 39 + "1"  # sorts before every real oid, so the per-review read meets it first


def _plant_pruned(world, ref=PRUNED):
    """beta's remote advertises a review whose objects it no longer has (a ref file written by hand)."""
    remote = world["members"]["beta"]["remote"]
    path = remote / ref
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(GONE + "\n")
    shown = subprocess.run(["git", "ls-remote", str(remote), ref], capture_output=True, text=True).stdout
    assert shown.split() == [GONE, ref], shown  # was there: the remote advertises it


def test_a_pruned_review_does_not_block_a_pin_another_review_names(world):
    assert record(world, PASS)["result"] == "pass"
    _plant_pruned(world)  # after the record: a push to a remote with a broken ref fails on its own

    code, receipt = merge(world, "alpha")

    assert code == merge_gate.EXIT_MERGED, receipt


def test_a_pruned_review_with_no_other_match_refuses_naming_the_ref_and_git_once(world):
    moved = _move_beta(world, push=True)
    _plant_pruned(world)  # after the record, as above

    code, why, receipt = _refusal(world)

    assert code == merge_gate.EXIT_REFUSED, receipt
    assert why.startswith(f"declared_set_pin_unmeasured: beta's pin {moved[:12]} "), why
    assert PRUNED in why, why
    assert why.count("not our ref") == 1 and "\n" not in why and "\\n" not in why, why  # git's fatal line, once
    assert untouched(world)


def test_a_pruned_review_ref_with_a_bidi_override_is_printed_escaped(world):
    moved = _move_beta(world, push=True)
    _plant_pruned(world, PRUNED + "\u202e")

    code, why, receipt = _refusal(world)

    assert code == merge_gate.EXIT_REFUSED, receipt
    assert "\u202e" not in why and "zz-pruned\\u202e" in why, why
    assert untouched(world)


def _reviews(remote):
    out = subprocess.run(["git", "-C", str(remote), "for-each-ref", "--format=%(objectname) %(refname)",
                          "refs/dev.synapt.grip/__reviews__/"], capture_output=True, text=True, check=True).stdout
    return [line.split(" ", 1) for line in out.splitlines()]


def _review_fetches(monkeypatch, oids):
    """Every fetch the gate makes that asks for a review oid."""
    calls, real = [], merge_gate._git

    def spy(repo, *args, **kw):
        if args[:1] == ("fetch",) and set(args) & oids:
            calls.append(args)
        return real(repo, *args, **kw)
    monkeypatch.setattr(merge_gate, "_git", spy)
    return calls


def test_a_warning_before_git_s_error_prints_the_error_line(world):
    moved = _move_beta(world, push=True)
    remote = world["members"]["beta"]["remote"]
    stores = sorted(p for p in (remote / "objects").iterdir() if p.is_dir())
    for store in stores:  # the remote's objects unreadable: every fetch warns first, then errors
        os.chmod(store, 0)
    try:
        code, why, receipt = _refusal(world)
    finally:
        for store in stores:
            os.chmod(store, 0o755)

    assert code == merge_gate.EXIT_REFUSED, receipt
    assert why.startswith(f"declared_set_pin_unmeasured: beta's pin {moved[:12]} is named by no review"), why
    assert why.endswith(": git fetch --no-tags: error: unable to open object pack directory: ./objects/pack: "
                        "Permission denied"), why
    assert "warning:" not in why, why


def test_a_pruned_review_reads_the_others_one_by_one(world, monkeypatch):
    assert record(world, PASS)["result"] == "pass"
    remote = world["members"]["beta"]["remote"]
    _plant_pruned(world)
    oids = {oid for oid, _ in _reviews(remote)}
    calls = _review_fetches(monkeypatch, oids)

    code, receipt = merge(world, "alpha")

    assert code == merge_gate.EXIT_MERGED, receipt
    assert len(calls) == 1 + len(oids), calls  # the batch, then one per review


def test_any_other_batch_failure_refuses_with_it_after_one_fetch(world, monkeypatch):
    moved = _move_beta(world, push=True)
    beta = world["members"]["beta"]
    oids = {oid for oid, _ in _reviews(beta["remote"])}
    calls = _review_fetches(monkeypatch, oids)
    store = beta["repo"] / ".git" / "objects"
    dirs = [store, *sorted(p for p in store.rglob("*") if p.is_dir())]
    for d in dirs:  # this clone cannot write what it fetches: not one review's fault
        os.chmod(d, 0o555)
    try:
        code, why, receipt = _refusal(world)
    finally:
        for d in dirs:
            os.chmod(d, 0o755)

    assert code == merge_gate.EXIT_REFUSED, receipt
    assert why.startswith(f"declared_set_pin_unmeasured: beta's pin {moved[:12]} could not be read from its remote: "
                          "git fetch --no-tags: "), why
    assert len(calls) == 1, calls


IMPOSTOR = "refs/foo/refs/dev.synapt.grip/__reviews__/zz"


def test_a_ref_that_only_ends_like_a_review_is_not_taken_for_one(world):
    assert record(world, PASS)["result"] == "pass"
    remote = world["members"]["beta"]["remote"]
    for oid, ref in _reviews(remote):  # the only review naming the pin now lives under another prefix
        subprocess.run(["git", "-C", str(remote), "update-ref", IMPOSTOR, oid], check=True)
        subprocess.run(["git", "-C", str(remote), "update-ref", "-d", ref], check=True)
    assert _reviews(remote) == []

    code, why, receipt = _refusal(world)

    assert code == merge_gate.EXIT_REFUSED, receipt
    assert why.startswith("declared_set_pin_unreviewed: beta is pinned at "), why


def test_a_ref_ending_like_a_real_review_does_not_drop_it(world):
    assert record(world, PASS)["result"] == "pass"
    remote = world["members"]["beta"]["remote"]
    (_, ref), = _reviews(remote)
    _plant_pruned(world, "refs/foo/" + ref)  # same tail as the real review, an oid the remote cannot serve

    code, receipt = merge(world, "alpha")

    assert code == merge_gate.EXIT_MERGED, receipt


def _retract_after_scan(monkeypatch, remote, to=None, only=None):
    """Between the gate's scan and its fetch, the review ref (or just `only`) is deleted (objects kept) or moved to
    `to`."""
    real, done = merge_gate._git, []

    def spy(repo, *args, **kw):
        if args[:1] == ("fetch",) and not done and set(args) & {oid for oid, _ in _reviews(remote)}:
            for oid, ref in [(oid, ref) for oid, ref in _reviews(remote) if only in (None, ref)]:
                subprocess.run(["git", "-C", str(remote), "update-ref", *((ref, to) if to else ("-d", ref))], check=True)
                done.append((oid, ref))
        return real(repo, *args, **kw)
    monkeypatch.setattr(merge_gate, "_git", spy)
    return done


def test_a_review_deleted_after_the_scan_with_its_objects_kept_is_not_counted(world, monkeypatch):
    assert record(world, PASS)["result"] == "pass"
    remote = world["members"]["beta"]["remote"]
    done = _retract_after_scan(monkeypatch, remote)

    code, why, receipt = _refusal(world)

    assert len(done) == 1 and _reviews(remote) == [], done
    subprocess.run(["git", "-C", str(remote), "cat-file", "-e", done[0][0]], check=True)  # objects kept
    assert code == merge_gate.EXIT_REFUSED, receipt
    assert why.startswith("declared_set_pin_unreviewed: beta is pinned at "), why
    assert "retracted" in why and done[0][1] in why, why
    assert untouched(world)


def test_a_review_moved_after_the_scan_is_not_counted_for_the_old_oid(world, monkeypatch):
    assert record(world, PASS)["result"] == "pass"
    remote = world["members"]["beta"]["remote"]
    elsewhere = subprocess.run(["git", "-C", str(remote), "rev-parse", "refs/heads/main"],
                               capture_output=True, text=True, check=True).stdout.strip()
    done = _retract_after_scan(monkeypatch, remote, to=elsewhere)

    code, why, receipt = _refusal(world)

    assert [ref for _, ref in done] == [ref for _, ref in _reviews(remote)] and done[0][0] != elsewhere, done
    assert code == merge_gate.EXIT_REFUSED, receipt
    assert why.startswith("declared_set_pin_unreviewed: beta is pinned at ") and done[0][1] in why, why
    assert untouched(world)


def _git_out(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout.strip()


def test_one_of_two_reviews_retracted_after_the_scan_still_ties_by_the_other(world, monkeypatch):
    assert record(world, PASS)["result"] == "pass"
    remote = world["members"]["beta"]["remote"]
    (oid, ref), = _reviews(remote)
    second = ref + "-again"  # a second review naming the same pin, sorting after the first
    subprocess.run(["git", "-C", str(remote), "update-ref", second, oid], check=True)
    done = _retract_after_scan(monkeypatch, remote, only=ref)

    code, receipt = merge(world, "alpha")

    assert done == [(oid, ref)] and _reviews(remote) == [[oid, second]], done
    assert code == merge_gate.EXIT_MERGED, receipt


def test_a_review_republished_after_the_scan_with_the_same_content_still_ties(world, monkeypatch):
    assert record(world, PASS)["result"] == "pass"
    remote = world["members"]["beta"]["remote"]
    (oid, ref), = _reviews(remote)
    again = _git_out(remote, "commit-tree", f"{oid}^{{tree}}", "-m", "re-published")  # new oid, same content
    done = _retract_after_scan(monkeypatch, remote, to=again)

    code, receipt = merge(world, "alpha")

    assert done == [(oid, ref)] and _reviews(remote) == [[again, ref]] and again != oid, done
    assert code == merge_gate.EXIT_MERGED, receipt


def test_an_unreviewed_pin_names_the_rerun_and_the_publish_with_its_remote_form(world, monkeypatch):
    assert record(world, PASS)["result"] == "pass"
    remote = world["members"]["beta"]["remote"]
    _retract_after_scan(monkeypatch, remote)

    code, why, receipt = _refusal(world)

    assert code == merge_gate.EXIT_REFUSED and "retracted" in why, receipt
    line = receipt["members"][0].get("next") or ""
    review, pin = world["members"]["alpha"]["review"], why.split(" is pinned at ", 1)[1][:12]
    assert line.startswith(f"next: rerun gr2 review merge {world['author']} {review}; "), line
    assert f"no review of beta at {pin} " in line and "gr2 review publish " in line, line
    assert line.endswith("<beta's review id> --remote <beta's remote>"), line


def test_both_unreviewed_texts_have_a_next_line():
    for why in ("declared_set_pin_unreviewed: beta is pinned at 0123456789ab, which is neither merged into main nor "
                "named by a review published on its remote",
                "declared_set_pin_unreviewed: beta is pinned at 0123456789ab, which is not merged into main, and the "
                "reviews that named it were retracted from its remote (ref deleted or moved): refs/x"):
        line = merge_gate.next_steps.merge_row(why, workspace="/ws", review="gr:abc", remote="/r.git", path="/ws/m",
                                               head="h", checks=("test",))
        assert line == ("next: rerun gr2 review merge /ws gr:abc; if no review of beta at 0123456789ab is on its "
                        "remote (the ref deleted), publish a fresh one: gr2 review publish /ws <beta's review id> "
                        "--remote <beta's remote>"), line
