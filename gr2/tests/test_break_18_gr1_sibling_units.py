"""Break attempt 18 — the sibling-layout fixture: gr1 SIBLING agent workspaces.

gr1 keeps each agent's workspace BESIDE the gripspace root, as
`<parent>/<workspace>-<agent>/`, declared as `worktree = "<dir>"` in
`.gitgrip/agents.toml`. `migrate-gr1` instead declared every unit at
`agents/<unit>/home`, inside the root, and read the real `worktree` into
`migration_source` without ever using it for the path (migration.py:776; the
renderer does not render `migration_source` at all).

So a user who applied the generated spec got fresh nested unit homes while
their real desks sat untouched beside the root.

Measured shape, taken from Layne's own setup: five agents, four siblings plus
`"main"` (the desk that works in the root itself).

Design: the thin-slice design note, section 6c items 1-2.
"""
from __future__ import annotations

import subprocess
import tomllib
from pathlib import Path

import pytest
import yaml

from gr2.python_cli.migration import migrate_gr1_workspace
from gr2.python_cli.spec_apply import build_plan


# ---------------------------------------------------------------------------
# The fixture: gr1's real sibling layout
# ---------------------------------------------------------------------------

# name -> gr1 `worktree`, exactly as a real agents.toml declares it.
# "main" is the desk whose worktree IS the root.
_SIBLING_AGENTS = {
    "apollo": "synapt-dev",
    "atlas": "synapt-codex",
    "fathom": "synapt-fathom",
    "sentinel": "synapt-global",
    "opus": "main",
}

# repo name -> path relative to the workspace root, where the NAME differs from
# the PATH for some entries on purpose: section 6c gap 2 measured 13 of our 25
# repos differ, so a fixture where every name equals its path cannot see that
# class at all.
_REPOS = {
    "grip": "./gitgrip",
    "synapt-config": "./config",
    "synapt": "./synapt",
    "mem0": "reference/mem0",
}


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
        env={
            "PATH": "/usr/bin:/bin:/usr/local/bin",
            "HOME": str(cwd),
            "GIT_AUTHOR_NAME": "fixture", "GIT_AUTHOR_EMAIL": "f@example.com",
            "GIT_COMMITTER_NAME": "fixture", "GIT_COMMITTER_EMAIL": "f@example.com",
        },
    )


def _checkout(path: Path) -> None:
    """A real git checkout holding one commit, so 'adopt, do not clone' is testable."""
    path.mkdir(parents=True, exist_ok=True)
    _git("init", "-q", "-b", "main", cwd=path)
    (path / "MARKER").write_text(f"{path.name}\n")
    _git("add", ".", cwd=path)
    _git("commit", "-q", "-m", "fixture", cwd=path)


def _write_gr1_sibling_workspace(parent: Path) -> Path:
    """A gr1 gripspace at `parent/synapt` with four sibling desks beside it.

    Returns the workspace root. The sibling desks are created OUTSIDE it, which
    is the whole point: a path that names them must contain `..`.
    """
    root = parent / "synapt"
    gitgrip = root / ".gitgrip"
    (gitgrip / "spaces" / "main").mkdir(parents=True)

    (gitgrip / "spaces" / "main" / "gripspace.yml").write_text(
        yaml.dump({
            "version": 2,
            "manifest": {"url": "git@github.com:synapt-dev/synapt-gripspace.git"},
            "repos": {
                name: {"url": f"git@github.com:synapt-dev/{name}.git",
                       "path": spec["path"], "revision": "main"}
                for name, spec in
                {n: {"path": p} for n, p in _REPOS.items()}.items()
            },
        })
    )

    (gitgrip / "agents.toml").write_text(
        "".join(
            f"[agents.{name}]\nworktree = \"{worktree}\"\nchannel = \"dev\"\n\n"
            for name, worktree in _SIBLING_AGENTS.items()
        )
    )

    # The sibling desks, each already holding a checkout at a member's path.
    # A sibling desk belongs to its agent; these are the dirs the spec must name.
    for name, worktree in _SIBLING_AGENTS.items():
        if worktree == "main":
            continue
        desk = parent / worktree
        desk.mkdir(parents=True, exist_ok=True)
        _checkout(desk / "gitgrip")
        _checkout(desk / "config")

    return root


@pytest.fixture
def sibling_workspace(tmp_path: Path) -> Path:
    return _write_gr1_sibling_workspace(tmp_path)


# ---------------------------------------------------------------------------
# Break 18, first half: the emitted paths are the siblings
# ---------------------------------------------------------------------------

class TestBreak18EmittedUnitPaths:
    def test_emitted_unit_paths_are_the_gr1_siblings(self, sibling_workspace: Path) -> None:
        """THE SIBLING-LAYOUT DEFECT. Before the fix every unit is `agents/<u>/home`.

        gr1 declares `worktree = "synapt-dev"` for apollo, so the emitted path
        must be `../synapt-dev` — and `"."` for the desk whose worktree is the
        root itself, because that agent works IN the root.
        """
        migrate_gr1_workspace(sibling_workspace)

        spec = tomllib.loads(
            (sibling_workspace / ".grip" / "workspace_spec.toml").read_text()
        )
        got = {unit["name"]: unit["path"] for unit in spec["units"]}

        assert got == {
            "apollo": "../synapt-dev",
            "atlas": "../synapt-codex",
            "fathom": "../synapt-fathom",
            "sentinel": "../synapt-global",
            "opus": ".",
        }

    def test_no_unit_path_is_the_hard_coded_nested_default(self, sibling_workspace: Path) -> None:
        """The narrow form of the same claim, so a partial fix cannot pass the
        test above by getting four right and one wrong: no unit may be emitted
        at `agents/<unit>/home` when gr1 declared a worktree for it.
        """
        migrate_gr1_workspace(sibling_workspace)
        spec = tomllib.loads(
            (sibling_workspace / ".grip" / "workspace_spec.toml").read_text()
        )
        offenders = [
            (u["name"], u["path"])
            for u in spec["units"]
            if u["path"] == f"agents/{u['name']}/home"
        ]
        assert offenders == [], (
            "these units kept the hard-coded nested default although gr1 "
            f"declared a worktree for them: {offenders}"
        )

    def test_every_emitted_path_is_one_of_the_three_legal_forms(self, sibling_workspace: Path) -> None:
        """Section 6c item 1: a unit path is a root-relative path, `"."`, or
        exactly one `../<single-component>`. Nothing else is legal, and this
        asserts the fixture itself stays inside that grammar.
        """
        migrate_gr1_workspace(sibling_workspace)
        spec = tomllib.loads(
            (sibling_workspace / ".grip" / "workspace_spec.toml").read_text()
        )
        for unit in spec["units"]:
            path = unit["path"]
            legal = (
                path == "."
                or (not path.startswith("/") and path.startswith("../")
                    and path.count("/") == 1 and ".." not in path[3:])
                or (".." not in path and not path.startswith("/"))
            )
            assert legal, f"unit {unit['name']} has an illegal path form: {path!r}"


# ---------------------------------------------------------------------------
# The mutation row — its OWN test, not a clause inside another one
# ---------------------------------------------------------------------------

def test_control_mutation_hard_coded_unit_path_turns_break_18_red(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Restoring the pre-fix behaviour must turn break 18 red.

    The mutation is exactly the pre-fix code: every unit at
    `agents/<unit>/home`, gr1's `worktree` ignored. If this row PASSES, the
    fixture does not actually pin the fix — a fix nobody can un-fix is a fix
    nobody can test.

    Kept as its own row rather than a clause inside the assertion above, so the
    mutation is visible by name in the suite's output and cannot be dropped
    silently while the test above still passes.
    """
    from gr2.python_cli import migration

    def hard_coded_unit_path(safe_unit_name: str, unit_doc: dict) -> str:
        # The pre-fix line, verbatim: migration.py's
        #     "path": f"agents/{safe_unit_name}/home",
        return f"agents/{safe_unit_name}/home"

    monkeypatch.setattr(migration, "_gr1_unit_path", hard_coded_unit_path)

    root = _write_gr1_sibling_workspace(tmp_path)
    migration.migrate_gr1_workspace(root)
    spec = tomllib.loads((root / ".grip" / "workspace_spec.toml").read_text())
    got = {unit["name"]: unit["path"] for unit in spec["units"]}

    repaired = {
        "apollo": "../synapt-dev",
        "atlas": "../synapt-codex",
        "fathom": "../synapt-fathom",
        "sentinel": "../synapt-global",
        "opus": ".",
    }
    assert got != repaired, (
        "restoring the hard-coded path did NOT change the emitted units, so "
        f"break 18 does not pin the fix. Emitted: {got!r}"
    )
    # State the mutant's shape too, so the row says WHICH wrong answer the
    # fixture catches rather than only that it caught something.
    assert got == {name: f"agents/{name}/home" for name in repaired}, (
        f"the mutant produced something other than the nested default: {got!r}"
    )


# ---------------------------------------------------------------------------
# Break 18, second half: apply ACCEPTS the shape now that its consumer landed
# ---------------------------------------------------------------------------

class TestBreak18ApplyAcceptsSiblingUnitsNowThatTheConsumerLanded:
    """The declaration is right AND the consumer has caught up with it.

    THESE ROWS ARE THE MIRROR OF A LIFT, and they are turned over in the same
    change that lifts it. Until that change, `migrate-gr1` declared a sibling
    desk (and `.` for the root unit) while apply still placed members by NAME and
    wrote `unit.toml` into the unit home — so for a sibling unit it cloned `grip`
    into `<desk>/grip` beside that desk's own `./gitgrip` and dropped a gr2 file
    into the desk. The shape was REFUSED, with the landing point named, "until
    slice items 3, 5 and 6 land". They landed:

      * item 3 — `spec_apply.unit_member_path` resolves a member at its SPEC PATH
        inside the unit home, and every production site goes through it;
      * item 5 — `spec_apply.unit_metadata_path` sends a sibling unit's metadata
        to `<root>/.grip/state/units/<unit>/`, never into the desk;
      * the refusal (`unit_path_not_yet_appliable`) and the exit-4 branch in
        `build_plan` are gone with the reason that produced them.

    The strongest claim about the APPLY behaviour — the desk byte-identical, no
    clone, metadata under the state root — lives in
    `tests/test_apply_gr1_sibling_adoption.py`, where it can be measured against a
    real bare remote. These rows assert the planning half: the shape builds, and a
    desk member already at its path is not planned for convergence.
    """

    def test_apply_builds_a_plan_for_a_sibling_unit(self, sibling_workspace: Path) -> None:
        """Was `test_apply_refuses_a_sibling_unit_and_names_the_reason`.

        The assertion inverts with the contract: `build_plan` must RETURN, and the
        plan must carry the sibling path the migration emitted. A refusal here now
        means migrate-gr1's own output cannot be applied, which is the whole path
        section 6b promises.
        """
        migrate_gr1_workspace(sibling_workspace)
        plan, operations = build_plan(sibling_workspace)
        got = {unit["name"]: unit["path"] for unit in plan["units"]}
        assert got["apollo"] == "../synapt-dev", got
        assert isinstance(operations, list)

    def test_the_root_unit_is_appliable(self, sibling_workspace: Path) -> None:
        """Was `test_apply_refuses_the_root_unit_and_names_the_reason`.

        `worktree = "main"` migrates to `"."` — the desk that works IN the root.
        It is appliable too, and it is the shape that used to share the refusal.
        """
        migrate_gr1_workspace(sibling_workspace)
        plan, _ = build_plan(sibling_workspace)
        got = {unit["name"]: unit["path"] for unit in plan["units"]}
        assert got["opus"] == ".", got

    def test_a_desk_member_already_at_its_path_is_not_planned_for_convergence(
        self, sibling_workspace: Path
    ) -> None:
        """THE ROW THE TWO ABOVE CANNOT SEE.

        `grip`'s spec path is `./gitgrip`, and this fixture's desks all hold
        `<desk>/gitgrip` — so under the NAME-keyed join (`<desk>/grip`) the member
        read as missing and the plan cloned a second copy beside the agent's own
        checkout. Asserted on the PLAN, so a green here means the resolver, not a
        clone that happened not to run.
        """
        migrate_gr1_workspace(sibling_workspace)
        _, operations = build_plan(sibling_workspace)
        # THE `"."` UNIT IS EXCLUDED, and the exclusion is the fixture's shape
        # rather than a convenience: `opus` works IN the root, and this fixture
        # creates no root-level checkouts (only the four desks). So a converge for
        # `opus` is correct here while the same converge for a SIBLING desk is the
        # defect. Asserted per unit, which is what makes the two distinguishable.
        checked = 0
        for op in operations:
            if op.kind != "converge_unit_repos" or op.subject == "opus":
                continue
            checked += 1
            assert "grip" not in op.details.get("missing_repos", []), (
                f"unit {op.subject!r} plans a clone of a member its desk already holds "
                f"at the spec path ./gitgrip: {op.details}"
            )
        assert checked, "no sibling unit was planned for convergence; the row proved nothing"

    def test_control_a_nested_unit_still_builds_a_plan(self, tmp_path: Path) -> None:
        """THE HALF THAT MATTERS: the refusal is keyed to the SHAPE, not to apply.

        Without this row, a refusal that also refuses the WORKING case passes
        both rows above — the class this file exists against. Same workspace,
        same repos, only the unit path is nested.
        """
        root = tmp_path / "synapt"
        (root / ".grip").mkdir(parents=True)
        (root / ".grip" / "workspace_spec.toml").write_text(
            'workspace_name = "synapt"\n'
            "\n[[repos]]\n"
            'name = "grip"\npath = "gitgrip"\nurl = "https://example.invalid/grip.git"\n'
            "\n[[units]]\n"
            'name = "atlas"\npath = "agents/atlas/home"\nrepos = ["grip"]\n'
        )
        plan, operations = build_plan(root)
        assert isinstance(plan, dict)
        assert isinstance(operations, list)

    def test_a_worktree_carrying_a_separator_is_translated_like_gr1(
        self, tmp_path: Path
    ) -> None:
        """gr1's rule, mirrored: `resolve_worktree_path` replaces `/` with `-`
        and joins the PARENT, so `agents/nested` is a SIBLING at `../agents-nested`.

        Refusing it made this migration STRICTER than gr1 and diverged loudly on
        an input gr1 supports — a five-frame traceback with NO spec written. Both
        halves are asserted: the translation, and that a spec EXISTS afterwards.
        """
        root = _write_gr1_sibling_workspace(tmp_path)
        (root / ".gitgrip" / "agents.toml").write_text(
            '[agents.nested]\nworktree = "agents/nested"\nchannel = "dev"\n'
        )
        migrate_gr1_workspace(root)
        spec_path = root / ".grip" / "workspace_spec.toml"
        assert spec_path.exists(), (
            "no spec was written — the migration failed instead of translating"
        )
        spec = tomllib.loads(spec_path.read_text())
        got = {unit["name"]: unit["path"] for unit in spec["units"]}
        assert got == {"nested": "../agents-nested"}, got

    def test_the_sanitised_sibling_is_appliable_too(self, tmp_path: Path) -> None:
        """Was `test_the_sanitised_sibling_is_still_refused_by_apply`.

        `../agents-nested` is a sibling produced by the TRANSLATION path rather
        than by a literal worktree name, and it used to share the refusal. Now
        that the shape is supported, the translated output must be appliable for
        the same reason the literal one is: otherwise migrate-gr1 writes a spec
        that the next verb refuses.
        """
        root = _write_gr1_sibling_workspace(tmp_path)
        (root / ".gitgrip" / "agents.toml").write_text(
            '[agents.nested]\nworktree = "agents/nested"\nchannel = "dev"\n'
        )
        migrate_gr1_workspace(root)
        plan, _ = build_plan(root)
        got = {unit["name"]: unit["path"] for unit in plan["units"]}
        assert got["nested"] == "../agents-nested", got

    def test_the_shape_does_not_claim_exit_code_four(self, sibling_workspace: Path) -> None:
        """Was `test_the_refusal_carries_its_own_exit_code_four`.

        Exit 4 keyed a caller to "this workspace needs slice items 3/5/6". Those
        items are in, so no caller may keep reading the code as "unbuilt": a
        sibling-unit spec is an ordinary workspace and must NOT exit 4. The
        control below still holds the other direction — a generic spec error must
        not claim 4 either.
        """
        migrate_gr1_workspace(sibling_workspace)
        build_plan(sibling_workspace)  # must not raise at all

    def test_control_a_generic_spec_error_does_not_claim_exit_four(
        self, tmp_path: Path
    ) -> None:
        """THE CONTROL: the code is keyed to the SHAPE.

        An unrelated validation failure must NOT report 4, or a caller keying on
        the code would misread a generic error as "this workspace needs items
        3/5/6".
        """
        root = tmp_path / "synapt"
        (root / ".grip").mkdir(parents=True)
        (root / ".grip" / "workspace_spec.toml").write_text(
            'workspace_name = "synapt"\n'
            "\n[[repos]]\n"
            'name = "grip"\npath = "gitgrip"\nurl = "https://example.invalid/grip.git"\n'
            "\n[[units]]\n"
            'name = ""\npath = "agents/a/home"\nrepos = ["grip"]\n'
        )
        with pytest.raises(SystemExit) as exc:
            build_plan(root)
        assert exc.value.code != 4, "a generic spec error must not report exit 4"

    def test_a_worktree_gr2_refuses_is_a_sentence_not_a_traceback(
        self, tmp_path: Path
    ) -> None:
        """A colon worktree is refused by gr2 as a POLICY of gr2's own.

        gr1 ACCEPTS it — its entire sanitisation is `/` to `-` and it validates
        `agent.worktree` nowhere — so the refusal must be a stated choice,
        delivered as a sentence. Before the wrapper this escaped as a ValueError:
        five frames, no spec written, leaving the user mid-migration with neither
        a result nor a reason.
        """
        root = _write_gr1_sibling_workspace(tmp_path)
        (root / ".gitgrip" / "agents.toml").write_text(
            '[agents.probe]\nworktree = "a:b"\nchannel = "dev"\n'
        )
        with pytest.raises(SystemExit) as exc:
            migrate_gr1_workspace(root)
        message = str(exc.value)
        assert "probe" in message, f"the refusal did not name the unit: {message}"
        assert "Traceback" not in message

    def test_two_units_colliding_on_one_sanitised_path_are_refused(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """gr1 sanitises `/` to `-`, so worktrees `x/y` and `x-y` are ONE sibling.

        Distinct unit NAMES are not enough: the invariant is the PATH, and two
        units at one path means one unit's worktree lands on the other's.
        """
        root = _write_gr1_sibling_workspace(tmp_path)
        (root / ".gitgrip" / "agents.toml").write_text(
            '[agents.one]\nworktree = "x/y"\nchannel = "dev"\n\n'
            '[agents.two]\nworktree = "x-y"\nchannel = "dev"\n'
        )
        migrate_gr1_workspace(root)
        with pytest.raises(SystemExit):
            build_plan(root)
        message = capsys.readouterr().err
        assert "both resolve to" in message, (
            f"the collision itself was not reported: {message}"
        )
        assert "one" in message and "two" in message

    def test_control_two_distinct_units_still_validate(self, tmp_path: Path) -> None:
        """THE CONTROL: two units with distinct paths are NOT refused.

        Without it, a check that refused every workspace with more than one unit
        would satisfy the collision row above.
        """
        root = tmp_path / "synapt"
        (root / ".grip").mkdir(parents=True)
        (root / ".grip" / "workspace_spec.toml").write_text(
            'workspace_name = "synapt"\n'
            "\n[[repos]]\n"
            'name = "grip"\npath = "gitgrip"\nurl = "https://example.invalid/grip.git"\n'
            "\n[[units]]\n"
            'name = "atlas"\npath = "agents/atlas/home"\nrepos = ["grip"]\n'
            "\n[[units]]\n"
            'name = "orion"\npath = "agents/orion/home"\nrepos = ["grip"]\n'
        )
        plan, operations = build_plan(root)
        assert isinstance(plan, dict)
        assert isinstance(operations, list)
