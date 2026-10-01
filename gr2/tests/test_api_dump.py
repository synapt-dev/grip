"""The two gates on gr2's committed public-surface dump.

- ``test_api_dump_is_current``: the committed ``api/cli.api`` must equal what
  the generator renders now. A change to the public surface fails here unless
  the file changes in the same PR.
- ``test_api_stable_share_at_least_90``: stable items over all non-reserved,
  non-hidden items must be at least 0.90, with both counts printed. The lever
  is hiding our internal process verbs, not freezing everything: hidden items
  leave the denominator, which is why the hide list is load-bearing.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

GR2 = Path(__file__).resolve().parents[1]
CLI_API = GR2 / "api" / "cli.api"
GENERATOR = GR2 / "scripts" / "dump_api.py"

# Population floors, one per kind. Without them the two gates above pass on a
# dump that has lost whole kinds: mutating the generator's parameter loop to
# iterate over nothing drops this file from 414 items to 85 -- every flag and
# every positional gone -- and both gates still report success, because the
# share gate only ever divides what is left and the currency gate only compares
# the file to whatever the generator now renders. A witness that cannot notice
# a shrunken subject is not measuring the subject.
#
# These are population ALARMS, not ratchets: each sits under the count at the
# change that introduced it, so a deliberate small removal still lands, and
# what makes a removal visible in review is the currency gate forcing the
# `.api` file to change in the same PR.
#
# What they do NOT catch is drift inside the margin -- flags can still go
# unnoticed one at a time -- and that is the deliberate trade. A floor bounds
# catastrophic per-kind loss, which is the failure that reads as success.
MIN_VERBS = 70
MIN_FLAGS = 175
MIN_ARGS = 95

# `--json` verbs that are NOT in `JSON_SHAPES` yet. A COUNT rather than a list,
# so it is cheap to keep and it still fires on the thing that matters: the
# moment a verb grows a `--json` flag with no shape, this number moves and the
# author has to either document the shape or raise this constant IN FRONT OF A
# READER. LOWER it whenever you cover a verb; never raise it to quiet a red.
#
# The covered set today is the `store` group and its hidden `grip` alias (22
# verb paths, one table, two mounts). Everything else carrying `--json` -- the
# review machinery, lanes, exec, hooks, the workspace conversions -- is named
# here as a residual, not left as a silence: section 7's rule ("a verb with
# `--json` and no table entry fails the dump") is enforced over the group this
# slice promises, and this count is what makes the rest visible until they are
# covered one at a time.
# 48, raised from 47 by `pr view` (its `--json` carries the member rows and the
# source they came from, and it has no entry in the store group's table -- the
# group that table covers is the store group and its hidden alias, and this verb
# is not in it). Raised IN FRONT OF A READER: the shape is stated in that range's
# PR body rather than left as a number nobody can check. NOT a quieting -- this
# verb's shape is genuinely unregistered, and registering it in the store table
# would be this author guessing at another slice's conventions for a verb outside
# its group.
JSON_VERBS_PENDING = 48

# The kinds a stranger BUILDS ON INDEPENDENTLY: a verb, a flag, a positional
# and an exit code are each actionable on their own -- a caller writes
# `gr2 store status --json` and branches on exit 3.
#
# `json` IS DELIBERATELY NOT ONE OF THEM, and the reason is a measurement, not
# a preference. A key path is reachable only THROUGH its verb, which is already
# counted, so counting its keys again measures how much we DOCUMENTED rather
# than how much we PROMISED -- and it points the incentive the wrong way, where
# describing your surface lowers your score (measured with the markers as
# shipped: the STORE mount's 51 key paths, may-change with their verbs, take the
# share from 325/358 = 0.9078 to 325/409 = 0.7946, i.e. under the floor, for
# adding nothing but documentation). The population is named because the dump
# carries 102 key paths, not 51 -- the hidden `grip` mount carries the other 51
# and counting those too gives 325/460 = 0.7065.
#
# The exclusion is SYMMETRIC -- json items leave the numerator as well as the
# denominator -- so the kind can move the ratio in NEITHER direction, which
# `test_json_items_are_score_neutral` pins. An exclusion that could only raise
# the number would be a lever for meeting this gate, which is the one thing it
# must not be.
#
# `path` AND `ref` ARE BOTH IN THE LIST, and the difference from `json` is the
# whole reason the list is explicit rather than "whatever the dump carries". A
# `path` row (`grip.toml`) and a `ref` namespace (`refs/dev.synapt/...`) are
# each actionable ON THEIR OWN -- a stranger references the file, or the ref,
# without going through any verb -- which is the property this list is made of.
# A json key path has no such independent handle; it is reachable only through
# its verb. Measured with the three layout rows in the dump: 326/359 = 0.9081,
# the stable `path` row joining both sides and the two `ref` rows leaving the
# denominator as reserved (a reserved name is not in the dump at all, in this
# table's sense). Leaving `path` and `ref` out would read 325/358 = 0.9078 on
# the same dump, so the two kinds would be free -- and a kind that cannot move
# the ratio is the lever the paragraph above exists to deny.
SURFACE_KINDS = ("verb", "flag", "arg", "exit", "path", "ref")


def _generator():
    """Load the generator by path -- it is a script, not an installed module."""
    spec = importlib.util.spec_from_file_location("_gr2_dump_api", GENERATOR)
    assert spec and spec.loader, f"cannot load {GENERATOR}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _items() -> list[tuple[str, str, str]]:
    """(kind, label, marker) for every item, by IDENTITY rather than by text.

    THE DUMP IS GROUPED BY COMMAND NOW, so a parser over its lines has to learn
    to skip a heading, and each item's first tokens are its command path -- which
    means a gate reading the text would be RE-DERIVING the command from the very
    thing it is checking. A parser that re-derives its own subject is measuring
    itself: get the grouping wrong and the expected value moves with it, and the
    gate agrees.

    So "what does the dump carry" is answered from the registry the dump is
    BUILT from, and ``test_api_dump_is_current`` is what ties that registry to
    the file, byte for byte -- a stronger tie than the old parse was, since it
    cannot pass on a file with a heading in the wrong place. The FORMAT keeps
    its own gates below, and they call the generator's ``_line`` and ``_label``
    directly instead of going through here.
    """
    gen = _generator()
    registry = gen._registry()
    markers = gen._markers()
    return [
        (
            row.kind,
            gen._label(row.spelling, row.hidden, row.hidden_by),
            registry.marker(row.kind, row.spelling, markers),
        )
        for row in registry.rows()
    ]


def _counted(rows: list[tuple[str, str, str]]) -> list[tuple[str, str, str]]:
    """The rows the share gate measures. ONE home for the rule, so the gate and
    the neutrality proof below cannot drift apart."""
    return [
        (kind, label, marker)
        for kind, label, marker in rows
        if kind in SURFACE_KINDS and marker != "reserved" and "(hidden" not in label
    ]


def _share(rows: list[tuple[str, str, str]]) -> tuple[int, int, float]:
    """(stable, counted, share) -- the gate's own arithmetic."""
    counted = _counted(rows)
    stable = [row for row in counted if row[2] == "stable"]
    return len(stable), len(counted), (len(stable) / len(counted) if counted else 1.0)


def test_api_dump_is_current() -> None:
    rendered = _generator().render()
    assert rendered.strip(), (
        "the generator rendered nothing; an empty dump must never compare equal "
        "to a committed file, or two empty things read as current"
    )
    committed = CLI_API.read_text() if CLI_API.exists() else ""
    assert committed == rendered, (
        f"{CLI_API} is not current.\n"
        f"  regenerate: python scripts/dump_api.py\n"
        f"  then name the public-surface change in the PR body.\n"
        f"  committed lines={len(committed.splitlines())} "
        f"rendered lines={len(rendered.splitlines())}"
    )


def test_api_stable_share_at_least_90() -> None:
    rows = _items()
    stable_count, counted_count, share = _share(rows)
    json_rows = [row for row in rows if row[0] == "json"]
    print(
        f"stable share: {stable_count}/{counted_count} = {share:.4f}"
        f"  ({len(json_rows)} json key paths excluded: a key path is a facet of "
        f"its verb, which is counted)"
    )
    assert share >= 0.90, (
        f"stable share {stable_count}/{counted_count} = {share:.4f} is under 0.90.\n"
        f"  There are levers here and they are not interchangeable:\n"
        f"    hidden     an internal verb goes hidden=True and leaves the denominator\n"
        f"    reserved   a reserved name leaves the denominator the same way\n"
        f"    may-change the item STAYS in the denominator and stops counting as stable\n"
        f"  Use may-change only for surface that is public and genuinely still moving,\n"
        f"  and note the two are not interchangeable: taking a MAY-CHANGE item out of\n"
        f"  the denominator raises this ratio, and taking a STABLE one out lowers it.\n"
        f"  Hiding a family you would have marked may-change is what makes the surface\n"
        f"  smaller in the direction this gate measures."
    )


def _known_items() -> set[tuple[str, str]]:
    """(kind, bare spelling) for every item the dump carries, hidden included."""
    known = set()
    for kind, label, _ in _items():
        known.add((kind, label.split(" (hidden")[0].strip()))
    return known


def test_may_change_entries_all_name_a_real_item() -> None:
    """Every may-change entry must match an item the dump actually carries.

    An entry that names nothing is SILENT: a typo, or one left behind by a
    renamed verb, marks no line, the regenerated file comes out byte-identical,
    and the file reads as applied while the item it meant to mark is untouched.
    Every other gate here passes on it, so this is the only place it can be
    caught.
    """
    known = _known_items()
    entries = _generator()._markers()
    missing = sorted(
        f"{kind} {spelling}"
        for (kind, spelling), marker in entries.items()
        if marker == "may-change" and (kind, spelling) not in known
    )
    assert not missing, (
        "api/stability.toml may-change entries naming no item in the dump (their "
        "marker is silently never applied):\n  " + "\n  ".join(missing)
    )


def test_reserved_entries_name_no_item() -> None:
    """Every reserved entry must match NO item -- the opposite direction.

    Reserved holds a name for a later slice, so the entry asserts the spelling
    is ABSENT. The day the slice lands it, this turns red and the entry moves
    deliberately to stable or may-change instead of quietly becoming a marker
    that means nothing. A reserved entry naming a live item is the failure this
    exists to catch: it would read as reserved while the name is already taken.
    """
    known = _known_items()
    entries = _generator()._markers()
    already = sorted(
        f"{kind} {spelling}"
        for (kind, spelling), marker in entries.items()
        if marker == "reserved" and (kind, spelling) in known
    )
    assert not already, (
        "api/stability.toml reserved entries naming an item the dump ALREADY carries "
        "(the reservation has been overtaken):\n  " + "\n  ".join(already)
    )


def test_may_change_and_reserved_lists_are_disjoint() -> None:
    """No spelling may sit on both lists, because the lookup silently picks one.

    `_markers()` builds ONE dict keyed by (kind, spelling) and reads the
    reserved list last, so a duplicated entry makes its may-change marker vanish
    with no error anywhere -- worse, the share gate then treats the item as
    reserved and takes it out of the denominator, so a duplicate silently
    INFLATES the number it is supposed to measure. The other two checks each
    catch one duplicate shape by accident; this one names the cause.
    """
    import tomllib

    data = tomllib.loads((GR2 / "api" / "stability.toml").read_text())
    both = sorted(set(data.get("may-change", [])) & set(data.get("reserved", [])))
    assert not both, (
        "api/stability.toml entries on BOTH lists (the marker lookup keeps one and "
        "silently drops the other):\n  " + "\n  ".join(both)
    )


def test_api_dump_has_a_population() -> None:
    """Each kind must still be populated, so a silent loss of one cannot pass.

    This is the floor the other two gates lack: the share gate divides whatever
    is left, and the currency gate compares the file to whatever is rendered,
    so a generator that stopped emitting flags and positionals would satisfy
    both. Measured: the parameter loop mutated to iterate over nothing takes
    the dump from 414 items to 85 and both gates stay green.
    """
    rows = _items()
    verbs = [r for r in rows if r[0] == "verb"]
    flags = [r for r in rows if r[0] == "flag"]
    args = [r for r in rows if r[0] == "arg"]
    print(f"population: {len(verbs)} verbs, {len(flags)} flags, {len(args)} positionals")
    assert len(verbs) >= MIN_VERBS, f"only {len(verbs)} verbs (floor {MIN_VERBS})"
    assert len(flags) >= MIN_FLAGS, f"only {len(flags)} flags (floor {MIN_FLAGS})"
    assert len(args) >= MIN_ARGS, f"only {len(args)} positionals (floor {MIN_ARGS})"
    # `exit` deliberately has no floor beside these three: it is the one kind
    # whose POPULATION is pinned exactly, by
    # `test_api_dump_carries_every_store_exit_code`, which asserts the whole set.
    # A floor under an exact-set gate would be the weaker of the two.


def test_a_label_at_or_past_column_keeps_its_separator() -> None:
    """The two-column format must survive a label that reaches COLUMN.

    `_rows` recovers the marker by splitting on the LAST space, and the render
    pads the label to COLUMN. A label at or past COLUMN is emitted at its own
    length, so the padding adds nothing and the marker runs straight into it --
    and every label here contains spaces, so the split then lands INSIDE the
    label and BOTH columns come back wrong.

    THE SUBJECT IS SYNTHETIC ON PURPOSE. Every other label in the dump is under
    COLUMN today, so a test that walked only the real lines would go vacuous the
    moment that one long label changed, and would pass for the wrong reason.
    Measured on the real label that exposed this -- the 65-char store exit line:
    it rendered as `...store verb: "may-change`, the marker lookup missed, and
    all five exit lines silently read `stable`.

    The mutation that reddens this is dropping the `max(COLUMN, len(label) + 1)`
    in `_line` and padding to COLUMN: the marker then comes back welded to the
    label, and the precondition below keeps the fixture long enough to catch it.
    """
    label = 'store 5 cannot-measure prefix:"' + "x" * 40 + ': "'
    assert len(label) > _generator().COLUMN, (
        f"the fixture label is {len(label)} chars and must exceed COLUMN "
        f"({_generator().COLUMN}) or this test cannot fail"
    )
    line = _generator()._line("exit", label, "may-change")
    _, _, rest = line.partition(" ")
    got_label, _, got_marker = rest.rstrip().rpartition(" ")
    assert got_marker == "may-change", (
        f"the marker did not survive a label at COLUMN -- it came back as "
        f"{got_marker!r}, which means the separator was lost and the split "
        f"landed inside the label"
    )
    assert got_label.strip() == label, (
        f"the label did not round-trip through the column parse:\n"
        f"  sent: {label!r}\n  got:  {got_label.strip()!r}"
    )


def test_no_label_ends_in_a_space() -> None:
    """The format cannot carry a label whose LAST character is a space.

    `_line` guarantees the column separator, but the ambiguity here is on the
    READER's side of the line and the separator cannot help: `_rows` recovers the
    label by stripping padding, and a trailing space in the label is
    indistinguishable from that padding. Such a label comes back one character
    short -- which then reads as a WRONG LABEL IN THE DUMP when the dump is
    right. A false red on a gate is not the safe direction of failure; it only
    feels like one.

    Measured on a synthetic probe, alongside the shapes that DO work: the real
    65-char store exit label, a hidden label past COLUMN, and a label exactly
    COLUMN long all round-trip; `'some verb --flag <str> '` comes back as
    `'some verb --flag <str>'`.

    NO LABEL ENDS IN A SPACE TODAY, which is why this is a GATE rather than a
    fix. It costs nothing now, and it stops the next label that grows a trailing
    space from failing an unrelated gate with a misleading red -- which is
    exactly the failure mode this branch exists to remove.
    """
    # Labels are read from the generator's OWN `_label`, NOT through `_rows`:
    # that parser strips the padding, and the trailing space this test is asking
    # about is precisely what stripping removes -- so a version reading `_rows`
    # could not fail for ANY input. Measured, and it is why `_label` exists.
    gen = _generator()
    labels = [gen._label(row.spelling, row.hidden, row.hidden_by) for row in gen._items()]
    assert labels, "the dump rendered no labels, so this gate cannot fail"
    bad = sorted({label for label in labels if label.endswith(" ")})
    assert not bad, (
        "labels ending in a space -- the two-column parse cannot round-trip "
        "them, so each will surface later as a wrong label in a DIFFERENT "
        "gate's failure:\n  " + "\n  ".join(repr(b) for b in bad)
    )


def test_api_dump_carries_every_store_exit_code() -> None:
    """The `exit` kind: every code the `store` group can return is in the dump.

    §5's table for the group is 0 ok, 2 usage, 3 refused on coverage or
    cleanliness, 4 refused as inconsistent or beta, 5 cannot measure. A caller
    reads these; a verb whose refusal code is not in the dump is a refusal the
    dump does not promise.

    CODE 5 CARRIES TWO SHAPES AT ONE CODE, and that is why the prefix is in
    here. A MEASURED cannot-measure and a wrapped unexpected exception both
    report 5 -- the group's table has no code of its own for an internal
    failure -- and `STORE_INCOMPLETE_PREFIX` is the only thing that separates
    them. `grip_cli.py` says so in as many words: "THE PREFIX IS PART OF THE
    SURFACE, not a style choice: it is what tells a measured 'cannot measure'
    apart from a wrapped exception, and a caller reading the code alone
    cannot." So it rides the reason field rather than a kind of its own, since
    it is the reason for the code's second shape that a caller matches on.
    """
    rows = _items()
    got = {label for kind, label, marker in rows if kind == "exit" and label.startswith("store ")}
    want = {
        "store 0 ok",
        "store 2 usage",
        "store 3 refused-coverage",
        "store 4 refused-inconsistent",
        'store 5 cannot-measure prefix:"cannot complete this store verb: "',
    }
    assert got == want, (
        f"the store exit table in the dump is not §5's.\n"
        f"  missing: {sorted(want - got)}\n"
        f"  extra:   {sorted(got - want)}"
    )
    # The store group is still moving, so its exit table is may-change with its
    # verbs rather than stable on its own. Marking it stable here would promise
    # the codes while the verbs that return them are not yet promised.
    store_markers = {marker for kind, label, marker in rows if kind == "exit" and label.startswith("store ")}
    assert store_markers == {"may-change"}, (
        f"the store exit table must travel with the store verbs' stability, "
        f"found {sorted(store_markers)}"
    )


def _json_verb_paths() -> set[str]:
    """Every verb path in the app that carries a `--json` flag.

    Walked from the app rather than read from the table, because the question
    these gates ask is what the CLI OFFERS vs what the dump PROMISES -- a
    comparison that is vacuous if both sides come from the same source.
    """
    import typer

    from gr2.python_cli.app import app

    top = typer.main.get_command(app)

    def walk(cmd, prefix: tuple[str, ...] = ()):
        subs = getattr(cmd, "commands", None)
        if subs:
            for name, sub in subs.items():
                yield from walk(sub, prefix + (name,))
        else:
            yield " ".join(prefix), cmd

    return {
        verb
        for verb, cmd in walk(top)
        if any("--json" in (getattr(param, "opts", None) or []) for param in cmd.params)
    }


def test_the_store_group_json_verbs_all_have_a_shape() -> None:
    """Section 7 over the surface this slice promises: a verb carrying `--json`
    with no `JSON_SHAPES` entry is a payload the dump does not promise.

    An EXACT-SET assertion in both directions, because either half alone can go
    quiet: a missing shape is an undocumented payload, and a shape naming no
    `--json` verb is a promise about something that emits nothing.
    """
    from gr2.python_cli import grip_cli

    covered = set(grip_cli.JSON_SHAPES)
    group = {verb for verb in _json_verb_paths() if verb.split(" ")[0] in ("store", "grip")}
    assert covered, "JSON_SHAPES is empty, so this gate cannot fail"
    assert covered == group, (
        "JSON_SHAPES and the store/grip verbs carrying --json disagree:\n"
        f"  a --json verb with no shape: {sorted(group - covered)}\n"
        f"  a shape naming no --json verb: {sorted(covered - group)}"
    )


def test_uncovered_json_verbs_are_counted() -> None:
    """The residual is a NUMBER that moves, so it cannot grow in silence.

    The covered set is the group this slice promises; every other `--json` verb
    is a payload with no shape. A count rather than a list: it costs one line,
    and it still fires the moment a verb grows a `--json` flag -- which is the
    event that matters, since that author is the one who can document it.

    MEASURED, and the reason the share exclusion is safe only while this holds:
    adding `--json` to one verb outside the group (applied to app.py and
    restored by blob hash) takes the count 47 -> 48 and reddens this row, naming
    the verb it found. Without that mutation on the record, "47" is a number
    nobody has seen move, and an exclusion resting on it would be resting on a
    constant rather than on a property.
    """
    from gr2.python_cli import grip_cli

    pending = _json_verb_paths() - set(grip_cli.JSON_SHAPES)
    assert len(pending) == JSON_VERBS_PENDING, (
        f"{len(pending)} verbs carry --json with no shape in JSON_SHAPES, "
        f"expected {JSON_VERBS_PENDING}:\n  "
        + "\n  ".join(sorted(pending))
        + "\n  Document the new shape in grip_cli.JSON_SHAPES, or move "
        "JSON_VERBS_PENDING in this file deliberately."
    )


def test_every_json_item_names_a_real_verb() -> None:
    """The INHERITANCE lookup resolves by verb path, so a typo in the table
    would silently take the default marker instead of the verb's -- the item
    would read `stable` while the verb it belongs to is `may-change`.
    """
    gen = _generator()
    verbs = _json_verb_paths()
    spellings = [row.spelling for row in gen._items() if row.kind == "json"]
    assert spellings, "no json items were emitted, so this gate cannot fail"
    unknown = sorted({gen._verb_of_json(spelling) for spelling in spellings} - verbs)
    assert not unknown, (
        "json item(s) whose verb path names no --json verb (the marker then "
        "defaults to stable instead of inheriting):\n  " + "\n  ".join(unknown)
    )


def test_a_json_item_inherits_its_verb_marker() -> None:
    """The INHERITANCE RULE and the MOUNT TWINS, which nothing else here witnessed.

    MEASURED, and the measurement is why this row exists: deleting the
    inheritance branch in ``_marker`` and regenerating the dump leaves every
    OTHER row here green -- the file's json items simply all read ``stable``,
    because a json spelling is never an entry in ``api/stability.toml`` and the
    covered-set, count, neutrality and verb-resolution rows are each blind to a
    marker. So the rule this change rests on ("a key cannot be more promised than
    the verb that emits it") had no witness at all until this row.

    THREE ASSERTIONS, because any one of them alone is satisfiable by accident:

    1. every json marker equals its CANONICAL verb's marker;
    2. every json key path carried under BOTH mounts carries the SAME marker --
       `api/stability.toml` names the canonical mount only, so an alias spelling
       looks up as if absent and silently takes the `stable` default, publishing
       a STRONGER promise than the identical payload one mount over. Measured
       before the alias resolution landed: 51 `grip` key paths read `stable`
       against 51 `store` twins reading `may-change`;
    3. at least one marker is non-stable, since a dump whose verbs were all
       stable would satisfy 1 and 2 vacuously.

    The twin is built by SWAPPING THE MOUNT IN THE SPELLING, never by calling the
    resolver under test, so this row can disagree with the code it holds.
    """
    from gr2.python_cli import grip_cli

    gen = _generator()
    mounts = grip_cli.STORE_MOUNTS
    canonical, aliases = mounts[0], tuple(mounts[1:])
    assert len(mounts) > 1 and canonical not in aliases, (
        f"STORE_MOUNTS must name a canonical mount first, then one or more "
        f"aliases: {mounts}"
    )

    # (kind, bare spelling) -> the marker(s) published for it. The bare spelling
    # drops the `(hidden:...)` annotation a hidden row carries, so an alias row
    # and its canonical twin are keyed the same way.
    by_spelling: dict[tuple[str, str], set[str]] = {}
    for kind, label, marker in _items():
        by_spelling.setdefault((kind, label.split(" (hidden")[0].strip()), set()).add(marker)

    json_spellings = sorted(spelling for kind, spelling in by_spelling if kind == "json")
    assert json_spellings, "no json items were emitted, so this gate cannot fail"

    markers = gen._markers()
    wrong: list[str] = []

    # A json spelling's first token is its MOUNT, so one that names neither the
    # canonical mount nor an alias is an item the mount tuple does not explain.
    stray = sorted(s for s in json_spellings if s.partition(" ")[0] not in mounts)
    if stray:
        wrong.append(f"json spelling(s) under a mount STORE_MOUNTS does not name: {stray}")

    twin_pairs = 0
    for spelling in json_spellings:
        found = by_spelling[("json", spelling)]
        assert len(found) == 1, (
            f"json {spelling!r} carries more than one marker: {sorted(found)}"
        )
        marker = next(iter(found))

        # (1) INHERITANCE, resolved through the canonical mount.
        json_verb = gen._verb_of_json(spelling)
        verb_mount, sep, verb_rest = json_verb.partition(" ")
        canonical_verb = (
            f"{canonical} {verb_rest}" if sep and verb_mount in aliases else json_verb
        )
        want = markers.get(("verb", canonical_verb), "stable")
        if marker != want:
            wrong.append(
                f"{spelling}: marker {marker!r} but its verb {canonical_verb!r} is {want!r}"
            )

        # (2) TWIN AGREEMENT, across the mounts of the same callbacks.
        item_mount, item_sep, item_rest = spelling.partition(" ")
        if not item_sep or item_mount not in aliases:
            continue
        twin_pairs += 1
        twin_key = ("json", f"{canonical} {item_rest}")
        twin = by_spelling.get(twin_key)
        if twin is None:
            wrong.append(
                f"{spelling}: no {canonical}-mount twin {twin_key[1]!r}; the mounts are the "
                f"same callbacks, so a key path cannot exist under one only"
            )
        elif twin != found:
            wrong.append(f"{spelling}: {sorted(found)} but its twin reads {sorted(twin)}")

    assert not wrong, (
        "json item(s) whose marker is not their canonical verb's:\n  " + "\n  ".join(wrong)
    )
    assert twin_pairs, (
        "no json key path is carried under an alias mount, so the twin assertion "
        "cannot fail and is proving nothing"
    )
    assert any(by_spelling[("json", s)] != {"stable"} for s in json_spellings), (
        "every json item reads stable, so this row cannot tell inheritance from "
        "the default: the store group's key paths must carry its may-change"
    )


def test_json_items_are_score_neutral() -> None:
    """A json item must move the share in NEITHER direction.

    This is what makes the exclusion honest rather than convenient: an
    exclusion that could only RAISE the ratio would be a lever for meeting this
    gate, and a json item that could lower it would put a price on describing
    the surface. Adding one of each marker to the real rows must leave the
    arithmetic exactly where it was.
    """
    rows = _items()
    base = _share(rows)
    assert base[1], "the measured set is empty, so this proof is vacuous"
    for marker in ("stable", "may-change"):
        probe = rows + [("json", "store probe .key", marker)]
        assert _share(probe) == base, (
            f"adding a {marker} json item moved the share from {base} to "
            f"{_share(probe)}; the exclusion must be symmetric"
        )
def test_the_layout_rows_are_the_ones_the_design_fixes() -> None:
    """The `path` and `ref` kinds carry exactly section 7's worked example.

    AN EXACT SET IN BOTH DIRECTIONS, because the two ways this can go wrong are
    opposite and both silent:

      - a MISSING row is a promise the dump stopped making, and every other gate
        here passes on it (the share gate divides what is left, the currency
        gate compares the file to whatever is rendered);
      - an ADDED row is a LAYOUT DECISION FROZEN by whoever wrote it, which is
        exactly why the rest of section 3's table is deliberately absent -- those
        rows are open on another desk, and a dump freezes what it carries.

    So this row is not a ratchet to be raised casually: raising it is the act of
    freezing a layout row, and it is meant to happen in the change that lands
    `layout.api`.
    """
    rows = _items()
    got = {(kind, label, marker) for kind, label, marker in rows if kind in ("path", "ref")}
    want = {
        ("path", "grip.toml tracked", "stable"),
        ("ref", "refs/dev.synapt/__members__/<member>/heads/*", "reserved"),
        ("ref", "refs/dev.synapt/__overlays__/<member>/...", "reserved"),
    }
    assert got == want, (
        "the layout kinds in the dump are not section 7's:\n"
        f"  missing: {sorted(want - got)}\n"
        f"  extra:   {sorted(got - want)}"
    )


def test_every_layout_row_carries_a_known_marker() -> None:
    """A layout row's marker comes from the ROW, so a typo would make a fourth
    marker that the share gate then reads as "not reserved" and counts."""
    from gr2.python_cli.layout import LAYOUT, MARKERS

    assert LAYOUT, "the layout table is empty, so this gate cannot fail"
    bad = sorted(
        f"{row.kind} {row.spelling}: {row.marker!r}"
        for row in LAYOUT
        if row.marker not in MARKERS
    )
    assert not bad, (
        "layout rows carrying a marker outside MARKERS (the share gate treats "
        "anything that is not `reserved` as counted):\n  " + "\n  ".join(bad)
    )


def test_no_spelling_carries_a_marker_from_both_sources() -> None:
    """A layout spelling must not also sit in `api/stability.toml`.

    The two sources mean different things by `reserved` -- stability.toml's
    asserts the spelling is ABSENT from the dump, the layout's marks a row that
    is PRESENT -- so a spelling in both would be answered twice, and which answer
    won would depend on lookup order rather than on anyone's decision.
    """
    gen = _generator()
    layout_keys = {(kind, label) for kind, label, _ in gen._layout_rows()}
    assert layout_keys, "no layout rows were built, so this gate cannot fail"
    both = sorted(
        f"{kind} {spelling}" for kind, spelling in layout_keys & set(gen._markers())
    )
    assert not both, (
        "spelling(s) carrying a marker from BOTH the layout table and "
        "api/stability.toml:\n  " + "\n  ".join(both)
    )

