"""§6c items 3 and 5 at the APPLY layer: a gr1 SIBLING desk is adopted, not re-cloned.

WHAT THIS FILE IS FOR, and why it is not `test_store_break_attempts.py`'s break 19.
Break 19 drives `store status` over `grip.toml`. Design §4 makes `[[units]]` a
BETA-REFUSED field in `grip.toml` (`BETA_GRIP_KEYS`, grip_cli.py), and §3 places a
member at `<root>/<member.path>` with no unit in the picture — so the store is right
to report the member absent when it is only inside a desk, and break 19 cannot go
green where it stands. The gap §6c item 3 actually names lives HERE: the apply path
over the alpha spec, `.grip/workspace_spec.toml`, which is the only spec that carries
`[[units]]`.

THE THREE CLAIMS, one row each:

  * item 3 — a member is placed by the MEMBER'S SPEC PATH, not its name. Measured
    today: `unit_member_path` is `(unit_home / member).resolve()`, the planner plans
    `missing_repos` as `(unit_home / r).exists()` (spec_apply.py:402), and
    `converge_unit_repos` clones to `unit_home / repo_name` (:516). All three are the
    name coordinate.
  * item 3's one-release condition: when
    nothing exists at the member's PATH but a checkout exists at its NAME, gr2 reads
    it there, prints ONE line naming both locations, and never clones over it.
  * item 5 — unit metadata for a unit outside the root goes to
    `<root>/.grip/state/units/<unit>/unit.toml`, never into the desk. A sibling desk
    belongs to its agent, and a gr2 file dropped into it is the pollution class that
    moved lanes out of `agents/`.

AND THE PRECONDITION THAT MAKES ALL THREE UNREACHABLE TODAY, asserted first so a red
row names its own blocker: `validate_spec` refuses every `../x` unit path as
`unit_path_not_yet_appliable`, and `build_plan` turns that into exit 4 — so
`migrate-gr1`'s own output cannot be applied at all.

Premium boundary: OSS (grip). Local workspace orchestration over git; no identity,
org, or entitlement semantics.
"""
from __future__ import annotations

import contextlib
import io
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from gr2.python_cli import spec_apply
from gr2.python_cli.spec_apply import (
    apply_plan,
    build_plan,
    repo_cache_path,
    workspace_spec_path,
)

GIT_IDENT = {
    "GIT_AUTHOR_NAME": "gr2 suite",
    "GIT_AUTHOR_EMAIL": "gr2-suite@example.invalid",
    "GIT_COMMITTER_NAME": "gr2 suite",
    "GIT_COMMITTER_EMAIL": "gr2-suite@example.invalid",
}


def _git(repo: Path, *args: str) -> str:
    env = {**__import__("os").environ, **GIT_IDENT}
    out = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, env=env, check=True
    )
    return out.stdout.strip()


def _a_real_checkout(path: Path, name: str) -> None:
    """A real git repo with one commit, so HEAD is a value a row can compare."""
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q", "-b", "main")
    (path / "README.md").write_text(f"# {name}\n")
    _git(path, "add", ".")
    _git(path, "commit", "-q", "-m", "initial")


def _listing(path: Path) -> set[str]:
    """Every path under `path`, so 'the desk is unchanged' is checkable by bytes."""
    if not path.exists():
        return set()
    return {
        str(p.relative_to(path))
        for p in sorted(path.rglob("*"))
        if ".git" not in p.parts
    }


class Gr1SiblingApplyTest(unittest.TestCase):
    """A root with a SIBLING desk, where the member's name differs from its path.

    Shape (the gr1 agent layout, design §6c):
        <tmp>/ws            the gripspace root
        <tmp>/desk-a        the desk, BESIDE the root  -> unit path "../desk-a"
        <tmp>/desk-a/config the desk's own checkout, at the member's PATH
    The member's NAME is "desk-config" and its PATH is "config", which is the §6c
    gap-2 shape (13 of 25 live repos have a name different from their path).
    """

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.root = self.tmp / "ws"
        (self.root / ".grip").mkdir(parents=True)
        self.desk = self.tmp / "desk-a"
        self.member_name = "desk-config"
        self.member_path = "config"
        self.unit_name = "a"
        self.unit_path = "../desk-a"
        # A REAL, local, bare remote, so a row that accidentally plans a clone still
        # cannot reach the network and the cache below is a genuine bare git dir.
        # Measured: a plain `mkdir` at the cache path makes validate_spec report
        # "repo cache path exists but is not a bare git dir", which would keep every
        # row red for a reason that is not its contract.
        self.remote = self.tmp / "desk-config.git"
        self._make_bare_remote()
        self._write_spec()

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- fixture ----------------------------------------------------------------

    def _make_bare_remote(self) -> None:
        src = self.tmp / "_src" / self.member_name
        _a_real_checkout(src, self.member_name)
        subprocess.run(
            ["git", "clone", "-q", "--bare", str(src), str(self.remote)],
            capture_output=True,
            check=True,
        )

    def _write_spec(self, *, unit_path: str | None = None, member_path: str | None = None) -> None:
        workspace_spec_path(self.root).write_text(
            'workspace_name = "gr1-sibling"\n'
            "\n"
            "[[repos]]\n"
            f'name = "{self.member_name}"\n'
            f'path = "{self.member_path if member_path is None else member_path}"\n'
            f'url = "{self.remote.as_uri()}"\n'
            "\n"
            "[[units]]\n"
            f'name = "{self.unit_name}"\n'
            f'path = "{self.unit_path if unit_path is None else unit_path}"\n'
            f'repos = ["{self.member_name}"]\n'
        )

    def _root_member(self) -> Path:
        return self.root / self.member_path

    def _desk_member_at_path(self) -> Path:
        return self.desk / self.member_path

    def _desk_member_at_name(self) -> Path:
        return self.desk / self.member_name

    def _seed_caches_and_root(self) -> None:
        """The root holds its own checkout and the cache is a REAL bare repo, so a
        converge row cannot pass because the cache was missing rather than because
        the member was found at its path."""
        _a_real_checkout(self._root_member(), self.member_name)
        cache = repo_cache_path(self.root, self.member_name)
        cache.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["git", "clone", "-q", "--bare", str(self.remote), str(cache)],
            capture_output=True,
            check=True,
        )

    # -- the precondition, asserted first so a red row names its blocker --------

    def test_precondition_the_sibling_unit_path_is_not_refused(self) -> None:
        """The blocker, pinned as its own row.

        Today `validate_spec` reports `unit_path_not_yet_appliable` and `build_plan`
        exits 4, so nothing below can be measured. Asserting it HERE means the three
        rows under it fail on their own contract rather than on an unhandled
        SystemExit from a fixture.
        """
        codes = [i.code for i in spec_apply.validate_spec(self.root)]
        self.assertNotIn(
            "unit_path_not_yet_appliable",
            codes,
            "a sibling unit path must be appliable once items 3/5/6 land; got " f"{codes}",
        )
        build_plan(self.root)  # must not raise SystemExit(4)

    # -- item 3 ------------------------------------------------------------------

    def test_item3_member_is_placed_by_its_spec_path_not_its_name(self) -> None:
        """The desk already holds the member AT ITS PATH, so there is nothing to
        converge: no clone, and the desk is byte-identical afterwards."""
        self._seed_caches_and_root()
        _a_real_checkout(self._desk_member_at_path(), self.member_name)
        before = _listing(self.desk)
        head_before = _git(self._desk_member_at_path(), "rev-parse", "HEAD")

        _, operations = build_plan(self.root)
        converge = [op for op in operations if op.kind == "converge_unit_repos"]
        self.assertEqual(
            converge,
            [],
            "the member is present at its spec PATH inside the desk; a name-keyed "
            f"join reports it missing and plans a clone. ops={operations}",
        )

        apply_plan(self.root, yes=True)

        self.assertEqual(_listing(self.desk), before, "the desk must be byte-identical")
        self.assertEqual(
            _git(self._desk_member_at_path(), "rev-parse", "HEAD"),
            head_before,
            "adoption reads HEAD and never re-clones over it",
        )
        self.assertFalse(
            self._desk_member_at_name().exists(),
            "a name-placed clone must never appear beside the desk's own checkout",
        )

    def test_item3_alpha_fallback_reads_the_name_placed_checkout_and_says_so(self) -> None:
        """The one-release condition: nothing at the PATH, a checkout at the NAME.
        gr2 reads it there, prints ONE line naming both, and never clones over it."""
        self._seed_caches_and_root()
        _a_real_checkout(self._desk_member_at_name(), self.member_name)
        before = _listing(self.desk)

        # The note is a PRINT, so it is captured where a user would see it. The
        # design's word is ONE line, and a plan-then-apply verb resolves the same
        # member twice -- so the capture wraps BOTH calls, and the assertion is a
        # COUNT. Capturing only the apply call would see nothing once the dedupe
        # works, which is how the first version of this row passed for the wrong
        # reason and then failed on an empty string.
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            _, operations = build_plan(self.root)
            converge = [op for op in operations if op.kind == "converge_unit_repos"]
            self.assertEqual(
                converge,
                [],
                f"the name-placed checkout must be adopted, not cloned over. ops={operations}",
            )
            apply_plan(self.root, yes=True)
        note = err.getvalue()

        self.assertEqual(_listing(self.desk), before, "the desk must be byte-identical")
        self.assertIn(self.member_name, note, "the note names the NAME it read from")
        self.assertIn(
            str(self._desk_member_at_path()),
            note,
            "the note names the PATH the spec declares",
        )
        self.assertEqual(
            note.count("note:"),
            1,
            f"exactly one note line, got {note.count('note:')}: {note!r}",
        )
        self.assertFalse(
            self._desk_member_at_path().exists(),
            "adoption never MOVES the checkout to the path; nothing is moved automatically",
        )

    # -- item 5 ------------------------------------------------------------------

    def test_item5_unit_metadata_goes_under_state_units_not_into_the_desk(self) -> None:
        """A sibling desk belongs to its agent. gr2 writes its metadata under the
        root's `.grip/state/units/<unit>/`, and no gr2 file lands in the desk."""
        self._seed_caches_and_root()
        _a_real_checkout(self._desk_member_at_path(), self.member_name)
        before = _listing(self.desk)

        apply_plan(self.root, yes=True)

        expected = self.root / ".grip" / "state" / "units" / self.unit_name / "unit.toml"
        self.assertTrue(
            expected.is_file(),
            f"unit metadata must live at {expected}; item 5",
        )
        self.assertFalse(
            (self.desk / "unit.toml").exists(),
            "unit.toml must never be dropped into another agent's desk",
        )
        self.assertEqual(
            _listing(self.desk),
            before,
            "no gr2 file, and nothing else, may be written into the desk",
        )

    # -- the MEMBER coordinate, contained ----------------------------------------

    def test_a_member_path_that_escapes_the_unit_home_is_refused(self) -> None:
        """A declared member path reaches a CLONE DESTINATION, so it is contained.

        `converge_unit_repos` clones to `unit_member_path(...)`, which joins the
        unit home to the member's DECLARED PATH. Members used to be placed by
        NAME, and a name cannot walk out of a directory, so nothing contained
        this; this range is what put the declared path into that join.

        MEASURED BEFORE THIS ROW COULD PASS: with the validator unchecked, this
        spec reported NO issues and `build_plan` planned a clone into the sibling
        desk -- a write into another agent's checkout, which is the property this
        whole path exists to protect. A unit at "." is used deliberately: it is
        the shape where the escape is widest.
        """
        self._write_spec(unit_path=".", member_path="../desk-a/sub")
        codes = [issue.code for issue in spec_apply.validate_spec(self.root)]
        self.assertIn(
            "member_path_outside_unit",
            codes,
            f"a member path that leaves the unit home must be refused; got {codes}",
        )
        with self.assertRaises(SystemExit):
            build_plan(self.root)

    def test_control_a_member_path_inside_the_unit_home_still_validates(self) -> None:
        """The control for the row above, so the refusal is containment and not
        a blanket refusal of members. Without it a `member_path_outside_unit`
        that fired on EVERY spec would satisfy the escape row exactly."""
        self._write_spec(unit_path=".", member_path="config")
        codes = [issue.code for issue in spec_apply.validate_spec(self.root)]
        self.assertNotIn(
            "member_path_outside_unit",
            codes,
            f"a legal member path must not be refused; got {codes}",
        )
        build_plan(self.root)  # must not raise

    def test_an_empty_dir_at_the_declared_path_is_not_silent(self) -> None:
        """`empty_placeholder` at the declared path, a real checkout at the name.

        The RESOLUTION is unchanged and stays the PATH: an empty directory at a
        declared path is the ordinary state of a freshly cloned superproject and
        `validate_spec` blesses it rather than calling it a conflict. What is
        asserted here is only that it is no longer SILENT -- before, this
        combination returned the path and printed nothing at all, so the member
        read as PRESENT to the planner (`.exists()`) while the checkout the user
        means sat one directory over.
        """
        _a_real_checkout(self._desk_member_at_name(), self.member_name)
        self._desk_member_at_path().mkdir(parents=True)  # empty, not a checkout
        spec = spec_apply.load_workspace_spec_doc(self.root)
        unit = spec["units"][0]

        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            resolved = spec_apply.unit_member_path(self.root, spec, unit, self.member_name)
        note = err.getvalue()

        self.assertEqual(
            Path(resolved),
            self._desk_member_at_path().resolve(),
            "the PATH still wins; this row does not change the resolution",
        )
        self.assertEqual(note.count("note:"), 1, f"exactly one note, got {note!r}")
        self.assertIn("holds no checkout", note)
        self.assertIn(str(self._desk_member_at_name()), note)


if __name__ == "__main__":
    unittest.main()
