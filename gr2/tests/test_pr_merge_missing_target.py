"""A member with no local verification target refuses as a SENTENCE, not a traceback.

`merge_pr_group` checks, before it pins a head or merges anything, that every member
of the group has an explicit local verification target. That check raised a bare
`ValueError`, while the merge verb's CLI catches `pr_ops.PRMergeError` -- so the one
refusal decidable from the group and the workspace ALONE reached the operator as a
Python traceback, on the verb whose whole point is to be the safe way to merge a
group.

Two rows, and the second is the one that can fail for the right reason:

- the FUNCTION row pins the class, both names in the message, and the empty
  `completed` list (nothing has merged when this fires, by construction);
- the CLI row drives `main()`, the console entry point, and asserts the operator
  reads a sentence with no `Traceback` in it. **Put the `ValueError` back and that
  row reddens** -- which is what keeps it from being decoration.

No network and no real repositories are needed: the refusal is decided before the
first adapter call, and a workspace spec declaring NO repos leaves every member of
the group without a target.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from gr2.python_cli import app as gr2_app
from gr2.python_cli.merge_verification import MergeVerificationTarget
from gr2.python_cli.platform import MergeMethod
from gr2.python_cli.pr import (
    PRHeadPinError,
    PRMergeError,
    PRMergeTargetError,
    merge_pr_group,
)

OWNER_UNIT = "atlas"
LANE_NAME = "feat-auth"
GROUP_ID = "pg_missing"
READ_SHA = "3f12dcc333ea0f28eac44041062db240b6eae799"
OTHER_SHA = "22bfd64917a49fef52050108c0f1086ddf7c26f5"


class _NeverCalled:
    """An adapter that fails loudly: this refusal must precede every merge call."""

    name = "github"

    def merge_pr(self, *args: object, **kwargs: object) -> object:
        raise AssertionError("an adapter call means the refusal did not run first")


def _workspace(tmp_path: Path, repos: list[str]) -> Path:
    """A workspace root, a spec, and one PR group naming `repos`."""
    workspace = tmp_path / "ws"
    (workspace / ".grip").mkdir(parents=True)
    (workspace / ".grip" / "workspace_spec.toml").write_text(
        '[workspace]\nname = "missing-target"\n\n'
        "[[units]]\n"
        f'name = "{OWNER_UNIT}"\n'
        'path = "agents/atlas/home"\n'
        "repos = []\n"
    )
    group = workspace / ".grip" / "pr_groups"
    group.mkdir(parents=True)
    (group / f"{GROUP_ID}.json").write_text(
        json.dumps(
            {
                "pr_group_id": GROUP_ID,
                "owner_unit": OWNER_UNIT,
                "lane_name": LANE_NAME,
                "prs": [
                    {"repo": repo, "pr_number": index + 1}
                    for index, repo in enumerate(repos)
                ],
            }
        )
    )
    return workspace


def test_a_member_with_no_verification_target_refuses_as_a_merge_error(
    tmp_path: Path,
) -> None:
    """The class is the contract: the CLI catches PRMergeError and nothing else here.

    The message has to name the members it is missing targets for -- a refusal that
    says "some member" sends the reader to diff their group against their spec by
    hand, which is the work the refusal exists to save.
    """
    workspace = _workspace(tmp_path, ["app", "api"])

    with pytest.raises(PRMergeTargetError) as raised:
        merge_pr_group(
            workspace_root=workspace,
            pr_group_id=GROUP_ID,
            adapter=_NeverCalled(),  # type: ignore[arg-type]
            actor="agent:test",
            method=MergeMethod.MERGE,
            verification_targets={},
            report=lambda _message: None,
        )

    exc = raised.value
    assert isinstance(exc, PRMergeError), (
        "the merge verb's CLI catches pr_ops.PRMergeError, so a refusal outside that "
        f"hierarchy reaches the operator as a traceback: {type(exc).__name__}"
    )
    message = str(exc)
    for repo in ("app", "api"):
        assert repo in message, f"the refusal must name {repo}: {message}"
    assert exc.completed == [], (
        "this check runs before the pin pass and before the first merge, so nothing "
        f"may be reported as already merged: {exc.completed}"
    )


def _run_main(argv: list[str]) -> int:
    """Drive the console entry point in-process, exactly as the script does."""
    saved = sys.argv
    sys.argv = ["gr2", *argv]
    try:
        with pytest.raises(SystemExit) as exc:
            gr2_app.main()
        return int(exc.value.code or 0)
    finally:
        sys.argv = saved


def test_the_merge_entry_point_reports_a_sentence_and_not_a_traceback(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """The same refusal through the entry point a user actually types.

    This is the row the mutation reddens. Restore `raise ValueError(...)` in
    `merge_pr_group` and the refusal escapes `main()` as a stack trace, which is
    exactly what the operator met before this change.
    """
    workspace = _workspace(tmp_path, ["app", "api"])

    code = _run_main(["pr", "merge", str(workspace), OWNER_UNIT, LANE_NAME])
    output = "".join(capsys.readouterr())

    assert code == 1, output
    assert "Traceback" not in output, f"the operator met a stack: {output}"
    assert "api" in output, f"the sentence must name the member: {output}"
    # WHICH REFUSAL, not merely that there was one. The three assertions above are ALL
    # satisfied by the pin-coverage message, which also exits 1, also prints no traceback
    # and also names the member -- so a row that checks for "a sentence" is answered yes
    # by the wrong sentence. That is how dropping `pins and` from the coverage guard, which
    # makes the ordinary no-pin merge refuse, left this file green.
    assert "no explicit local verification target" in output, (
        "the operator must be told which refusal this is: "
        f"a refusal about the wrong fault reads the same to every check above. {output}"
    )
    assert "would merge unpinned" not in output, (
        "this is the coverage refusal, not the missing-target one -- and this command "
        f"passes no pin, so the coverage refusal cannot be the right answer here: {output}"
    )


def _write_raw_group(workspace: Path, prs: list[dict]) -> None:
    """Overwrite the group with entries exactly as given, unvalidated."""
    (workspace / ".grip" / "pr_groups" / f"{GROUP_ID}.json").write_text(
        json.dumps(
            {
                "pr_group_id": GROUP_ID,
                "owner_unit": OWNER_UNIT,
                "lane_name": LANE_NAME,
                "prs": prs,
            }
        )
    )


def test_an_unreadable_pr_number_refuses_as_a_sentence_not_as_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """A malformed entry is REFUSED, not raised -- and the group is decidable without the host.

    Three sites on the merge path call `int(pr_number)`: the target check, the pin pass and
    the merge loop. `int()` on an unreadable value raises ValueError or TypeError, and the
    merge verb's CLI catches `PRMergeError` only, so this shape left `main()` with NOTHING
    printed -- not a sentence, not even a rendered traceback. A group whose member carries
    "abc" is refused once, here, before any of the three converts it.

    `code == 1` is load-bearing: `_run_main` requires `SystemExit`, so an exception escaping
    `main()` fails this row rather than being read as an exit.
    """
    workspace = _workspace(tmp_path, [])
    _write_raw_group(
        workspace,
        [{"repo": "app", "pr_number": 1}, {"repo": "api", "pr_number": "abc"}],
    )

    code = _run_main(["pr", "merge", str(workspace), OWNER_UNIT, LANE_NAME])
    output = "".join(capsys.readouterr())

    assert code == 1, output
    assert "not an integer" in output, f"the refusal must say what is wrong: {output}"
    assert "abc" in output, f"the refusal must show what was actually there: {output}"
    # WHICH ENTRY, not merely that one was refused. Naming the group's FIRST member
    # instead of the offending one keeps every other row green -- the malformed entry
    # here is the SECOND -- and the operator then reads the healthy member's name beside
    # a number that was never its own.
    assert '"repo": "api"' in output, (
        f"the refusal must name the OFFENDING entry, not the group's first: {output}"
    )


def test_a_null_pr_number_is_refused_by_the_same_guard_as_a_string(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """The OTHER arm of the same handler, which nothing exercised.

    The guard catches `(TypeError, ValueError)`. The row above feeds `"abc"`, a STRING, so
    it only ever reaches the ValueError arm -- narrow the handler to `except ValueError` and
    the whole selection stays green while the TypeError arm the docstring names is dead.
    JSON `null` arrives as None, and `int(None)` raises TypeError, so this row is the one
    that makes the named arm live.
    """
    workspace = _workspace(tmp_path, [])
    _write_raw_group(
        workspace,
        [{"repo": "app", "pr_number": 1}, {"repo": "api", "pr_number": None}],
    )

    code = _run_main(["pr", "merge", str(workspace), OWNER_UNIT, LANE_NAME])
    output = "".join(capsys.readouterr())

    assert code == 1, f"a TypeError escaping main() prints nothing at all: {output}"
    assert "not an integer" in output, f"the refusal must say what is wrong: {output}"
    assert '"repo": "api"' in output, f"the refusal must name the offending entry: {output}"


def test_a_pr_number_that_is_not_an_integer_at_all_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """THE GUARD IS A QUANTIFIER NOW, NOT AN ENUMERATION OF WHAT int() RAISES.

    It named `(TypeError, ValueError)`, and `int()` also raises **OverflowError** — which is
    what JSON's bare `Infinity` literal produces, and `json` accepts that literal by default,
    so a group file can carry it. Before the change that value ESCAPED `main()` and printed
    NOTHING, which is the exact sentence this guard exists to make false. `int()` also
    ACCEPTS non-integers in silence — `7.9` became 7, `true` became 1 — so "unreadable" was
    narrower than "not an integer".

    `float("inf")` is written through `json.dumps`, which emits the bare `Infinity` literal,
    so this row goes through the same road a group file does rather than through a Python
    float handed straight to the function.
    """
    for bad in (float("inf"), 7.9, True):
        workspace = _workspace(tmp_path / f"case-{bad!r}", [])
        _write_raw_group(
            workspace,
            [{"repo": "app", "pr_number": 1}, {"repo": "api", "pr_number": bad}],
        )
        code = _run_main(["pr", "merge", str(workspace), OWNER_UNIT, LANE_NAME])
        output = "".join(capsys.readouterr())
        assert code == 1, f"{bad!r} escaped main(): {output}"
        assert "not an integer" in output, f"{bad!r}: {output}"


def test_the_refusal_names_the_FIRST_offender_when_several_entries_are_bad(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """Which offender is named when a group has MORE THAN ONE.

    Every other row carries a single bad entry, so validating the group in REVERSE names the
    same object and survives all of them. This is the only shape where the choice among
    several is visible.
    """
    workspace = _workspace(tmp_path, [])
    _write_raw_group(
        workspace,
        [
            {"repo": "app", "pr_number": 1},
            {"repo": "api", "pr_number": "abc"},
            {"repo": "web", "pr_number": None},
        ],
    )

    code = _run_main(["pr", "merge", str(workspace), OWNER_UNIT, LANE_NAME])
    output = "".join(capsys.readouterr())

    assert code == 1, output
    assert "not an integer" in output, output
    assert '"repo": "api"' in output, f"the FIRST offending entry must be named: {output}"


def test_a_malformed_ENTRY_is_refused_by_the_same_guard(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """THE ENTRY, not only the value in it -- and `prs` itself, not only an entry.

    `_load_group` is a bare `json.loads` with no schema validation, so a group file can
    carry an entry missing a key, or one that is not an object at all, or a `prs` that is
    not a list. Every read of a member assumed all three, and the first three shapes below
    escaped uncaught and printed NOTHING: an entry missing `pr_number`; one missing `repo`,
    which the CLI's pin parser reads BEFORE `merge_pr_group` runs; and one that is not a
    dict.

    The pin parser is TOLERANT of these now rather than refusing them, and that is the
    point of driving the CLI row rather than the function: the pin parse sits under the
    plain `except ValueError` handler, which prints the sentence alone, while the merge
    loop's refusal is caught by the `PRMergeError` handler, which prints the offending
    entry inside a JSON payload. A guard moved into the pin parser keeps the sentence and
    silently drops the entry from the operator's view -- so this row asserts the group's
    own refusal arrives, from the merge loop's own guard, entry and all.

    The fourth shape is `prs` ITSELF not being a list, and it is here because a MUTATION
    found it rather than because it was thought of: making the `isinstance(raw_prs, list)`
    guard a no-op left every row in this selection green, since the three shapes above all
    arrive as lists and are caught one guard later. JSON `null` is what a hand-edited group
    file actually carries, and with the guard gone it iterates `None` and raises a bare
    `TypeError` out of `main()`.

    `_run_main` requires `SystemExit`, so an exception escaping `main()` fails these rows
    rather than being read as an exit.
    """
    shapes = [
        ("missing-pr-number", [{"repo": "app", "pr_number": 1}, {"repo": "api"}]),
        ("missing-repo", [{"repo": "app", "pr_number": 1}, {"pr_number": 2}]),
        ("not-an-object", [{"repo": "app", "pr_number": 1}, "api"]),
        ("prs-not-a-list", None),
    ]

    for label, prs in shapes:
        workspace = _workspace(tmp_path / f"case-{label}", [])
        _write_raw_group(workspace, prs)
        code = _run_main(["pr", "merge", str(workspace), OWNER_UNIT, LANE_NAME])
        output = "".join(capsys.readouterr())
        assert code == 1, f"{label}: an exception escaped main() and printed nothing: {output}"
        assert "Traceback" not in output, f"{label}: the operator met a stack: {output}"
        assert "so the group cannot be read" in output, f"{label}: {output}"


class _RecordingAdapter:
    """Records a host status READ as well as a merge.

    THE INSTRUMENT CHANGE, and it is the whole point of the two rows below. The
    adapter this file first used counted `merge_pr` alone, so a head read made by the
    pin pass was invisible to it -- which is how a row named for "any host call" could
    not witness the ORDERING it advertised. Atlas's mutation moved the missing-target
    check to after the pin pass and every existing row stayed green, because none of
    them passes `expected_heads` and the pin loop is therefore a no-op in all of them.
    """

    name = "github"

    def __init__(self, heads: dict[str, str]) -> None:
        self.heads = heads
        self.status_calls: list[str] = []
        self.merge_calls: list[str] = []

    def pr_status(self, repo: str, number: int) -> object:
        self.status_calls.append(repo)
        return type("_Status", (), {"head_oid": self.heads.get(repo, "")})()

    def merge_pr(
        self,
        repo: str,
        number: int,
        *,
        method: object,
        expected_head: str | None = None,
    ) -> object:
        self.merge_calls.append(repo)
        raise AssertionError("nothing may be merged when a member has no target")


def _targets(workspace: Path, repos: list[str]) -> dict[str, MergeVerificationTarget]:
    return {
        repo: MergeVerificationTarget(
            repo_root=workspace / "repos" / repo,
            remote=f"https://example.test/{repo}.git",
        )
        for repo in repos
    }


def _merge_with_pin(workspace: Path, adapter: object, *, pin: str) -> None:
    merge_pr_group(
        workspace_root=workspace,
        pr_group_id=GROUP_ID,
        adapter=adapter,  # type: ignore[arg-type]
        actor="agent:test",
        method=MergeMethod.MERGE,
        verification_targets=_targets(workspace, ["app"]),
        report=lambda _message: None,
        expected_heads={"app": pin} if pin else None,
    )


def test_a_missing_target_refuses_before_the_pin_pass_reads_any_head(
    tmp_path: Path,
) -> None:
    """The ordering, witnessed with a pin that MATCHES -- so the pin pass would have
    SUCCEEDED had it run, and a status read could only mean the check moved after it.

    Built to Atlas's fix-forward after his mutation survived every row this file had.
    Green unmutated; red under the mutation with `status_calls == ['app']`.
    """
    workspace = _workspace(tmp_path, ["app", "api"])
    adapter = _RecordingAdapter({"app": READ_SHA})

    with pytest.raises(PRMergeTargetError) as raised:
        _merge_with_pin(workspace, adapter, pin=READ_SHA)

    # THE HEADER NAMES THE MISSING MEMBER, not the group's first. PRMergeError renders
    # f"merge failed for {repo}#{pr_number}: {reason}", and every OTHER row that drives a
    # missing-target refusal has ALL members missing -- so missing_targets[0] equals
    # group["prs"][0] and the two are indistinguishable. This row builds the discriminating
    # shape (targets for "app" only, so missing is [("api", 2)]) and is therefore the only
    # place the identity fields can be witnessed: substituting the group's first member for
    # the first MISSING one keeps every other row green while making the header blame the
    # member that HAS a target, contradicting the body of its own sentence.
    assert str(raised.value).startswith("merge failed for api#"), str(raised.value)

    assert adapter.status_calls == [], (
        "the refusal must precede the pin pass's head read, or a group with a pin set "
        f"AND a member with no target makes a host call before it refuses: {adapter.status_calls}"
    )
    assert adapter.merge_calls == []


def test_a_missing_target_refuses_before_a_mismatched_pin_can_blame_the_branch(
    tmp_path: Path,
) -> None:
    """The consequence, which is worse than a delayed refusal: the operator gets the
    WRONG one. With a pin that does NOT match, a reordered check lets the pin pass raise
    first and report the branch as having moved since the reads -- telling them to redo a
    review -- when the real problem is a member with no verification target at all.
    """
    workspace = _workspace(tmp_path, ["app", "api"])
    adapter = _RecordingAdapter({"app": OTHER_SHA})

    with pytest.raises(PRMergeTargetError) as raised:
        _merge_with_pin(workspace, adapter, pin=READ_SHA)

    assert not isinstance(raised.value, PRHeadPinError), (
        "a mismatched pin must not be allowed to report a moved branch when the real "
        f"fault is a missing verification target: {raised.value}"
    )
    assert "no explicit local verification target" in str(raised.value)
    assert adapter.status_calls == []
