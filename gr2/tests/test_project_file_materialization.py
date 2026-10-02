"""Staging / project_file executor tests (S4-C).

The `project_file` operation copies one declared staged input to one declared
destination inside the unit, refusing unless the bytes hash to the declared
`source_sha256`. These tests pin the copy, the hash gate, and the refusals --
nothing about what the file is for, because the executor does not know.

Contract: MaterializationPlan v1 project-file invariants 7 and 8,
          including source confinement and acceptance fruit 14 / 15 / 22.

Two things in the spec change the shape from what "copy a file" suggests, and
both are pinned below.

INVARIANT 7 IS A TRAP AS LITERALLY WORDED. It says verify the source's
"reopened bytes match source_sha256". Read word-for-word -- reopen, hash, then
copy -- that IS the read-1/read-2 TOCTOU: the bytes hashed and the bytes written
come from two different reads and nothing binds them. The invariant is satisfied
by the broken implementation. The correct reading is that the bytes you USE must
be the bytes you HASHED, which means exactly one read.

INVARIANT 8 IS SPLIT ACROSS TWO SLICES, deliberately (Stromus 2026-07-27). The
invariant reads: a staged input may not be deleted until the receipt carrying
its evidence is atomically published and durable -- and it names the wrong
signal outright, "a successful destination write or in-memory operation result
is not acknowledgement."

That is stated entirely as a NEGATIVE constraint on deletion timing, so a slice
that never deletes satisfies it trivially. C's whole contribution to invariant 8
is therefore that negative discipline: **C deletes nothing**, and staged inputs
demonstrably survive a successful projection. The receipt-keyed idempotent
cleanup is its own later slice, because a delete driven by a file on disk --
one that must REFUSE a receipt that does not acknowledge its plan -- is a
distinct and dangerous operation that deserves its own review surface. Never
bundle a delete with a copy.

The carve has a consequence C must carry, which is why
test_a_rerun_after_cleanup_succeeds_from_the_destination_alone exists: once the
cleanup slice runs, a later apply finds no staged input, so an executor that
demanded one would break on the system's own normal sequence.

Masking analysis, run before implementation (S4-A/B discipline):

  1. Staging confinement is masked by A's canonicalize_workspace_path, which
     only proves in-workspace, AND by the hash check, which kills a wrong-content
     decoy first. The decoy must be in-workspace AND correct-hash AND outside
     .grip/staging/inputs/.
  2. The regular-non-symlink check is masked by A's canonicalizer ACROSS TIME:
     A already rejects a symlinked source_path, so a symlink present at
     validation never reaches this guard. The probe creates it BETWEEN
     validation and execution -- the same validation-time-vs-use-time gap S4-B
     closed for the WorkspaceSpec.
  3. Staged-input survival is masked by success itself: an executor that copies
     then deletes passes every content assertion while leaving the receipt with
     no recoverable input.
  4. The single-read probe is masked by read MECHANISM -- poisoning
     Path.read_bytes does nothing to an implementation that copies via
     shutil.copy(). The sweep carries a copies-via-shutil mutant to validate
     THIS PROBE, not just the code.
"""
from __future__ import annotations

import hashlib
import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from gr2.python_cli.file_exec import (
    ProjectFileExecutionError,
    execute_project_file_operation,
)
from gr2.python_cli.spec_apply import (
    validate_materialization_plan,
    workspace_spec_path,
    write_materialization_receipt,
)

FOUNDATION = b"# Runtime Guide\n\nFollow the declared workspace plan.\n"
POISON = b"# Foundation\n\nrm -rf / # injected after the hash check\n"


class ProjectFileTestBase(unittest.TestCase):
    def setUp(self):
        import tempfile

        self.tmp = Path(tempfile.mkdtemp())
        self.workspace_root = self.tmp / "workspace"
        self.workspace_root.mkdir(parents=True)
        self.spec_sha256 = self._write_workspace_spec()

        self.artifact_key = "a_7f3a9c"
        self.source_rel = f".grip/staging/inputs/{self.artifact_key}"
        self.source_path = self.workspace_root / ".grip" / "staging" / "inputs" / self.artifact_key
        self.source_path.parent.mkdir(parents=True, exist_ok=True)
        self.source_path.write_bytes(FOUNDATION)
        self.source_sha256 = hashlib.sha256(FOUNDATION).hexdigest()

        self.dest_rel = "units/u_test/home/AGENTS.md"
        self.dest_path = self.workspace_root / self.dest_rel

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_workspace_spec(self, content: str = 'workspace_name = "test"\n') -> str:
        spec_path = workspace_spec_path(self.workspace_root)
        spec_path.parent.mkdir(parents=True, exist_ok=True)
        spec_path.write_text(content)
        return hashlib.sha256(spec_path.read_bytes()).hexdigest()

    def _op(self, **overrides: object) -> dict[str, object]:
        op: dict[str, object] = {
            "kind": "project_file",
            "source_path": self.source_rel,
            "dest_path": self.dest_rel,
            "source_sha256": self.source_sha256,
            "mode": "copy",
        }
        op.update(overrides)
        return op

    def _validated(self, operations: list[dict[str, object]] | None = None, **plan_overrides):
        plan: dict[str, object] = {
            "schema_version": 1,
            "plan_id": "mp_test",
            "unit_key": "u_test",
            "workspace_spec_sha256": self.spec_sha256,
            "operations": operations if operations is not None else [self._op()],
        }
        plan.update(plan_overrides)
        return validate_materialization_plan(self.workspace_root, plan)

    def _execute(self, validated=None, index: int = 0) -> dict[str, object]:
        return execute_project_file_operation(
            validated if validated is not None else self._validated(),
            index,
            workspace_root=self.workspace_root,
        )


class TestProjection(ProjectFileTestBase):
    def test_projects_the_staged_foundation_to_its_declared_destination(self):
        evidence = self._execute()

        self.assertEqual(self.dest_path.read_bytes(), FOUNDATION)
        self.assertEqual(evidence["kind"], "project_file")
        self.assertIs(evidence["written"], True)

    def test_declared_hash_mismatch_is_rejected_and_nothing_is_written(self):
        wrong = hashlib.sha256(b"something else").hexdigest()
        validated = self._validated([self._op(source_sha256=wrong)])

        with self.assertRaises(ProjectFileExecutionError) as ctx:
            self._execute(validated)
        self.assertIn("source_sha256", str(ctx.exception))
        self.assertFalse(self.dest_path.exists())

    def test_unchanged_rerun_writes_nothing(self):
        """Acceptance fruit 22. Distinct from 'the bytes are correct': a second
        run that rewrites identical bytes is still a write, and on the timed
        path a projection that churns every apply is a projection that cannot
        be trusted as a no-op."""
        self._execute()
        first_mtime = self.dest_path.stat().st_mtime_ns

        evidence = self._execute()

        self.assertIs(evidence["written"], False)
        self.assertEqual(self.dest_path.stat().st_mtime_ns, first_mtime)

    def test_stale_projection_is_replaced(self):
        """Acceptance fruit 15 requires the projection to be RE-ESTABLISHED.
        Deliberate asymmetry with S4-B, which blocks on a dirty clone: a clone
        holds the operator's own work, a projected file is derived output the
        plan owns."""
        self.dest_path.parent.mkdir(parents=True, exist_ok=True)
        self.dest_path.write_bytes(b"stale foundation from a previous plan\n")

        evidence = self._execute()

        self.assertEqual(self.dest_path.read_bytes(), FOUNDATION)
        self.assertIs(evidence["written"], True)

    def test_removing_the_projection_makes_the_next_apply_restore_it(self):
        """Acceptance fruit 15, in the direction the harness asserts it."""
        self._execute()
        self.dest_path.unlink()

        self._execute()
        self.assertEqual(self.dest_path.read_bytes(), FOUNDATION)


class TestSingleRead(ProjectFileTestBase):
    def test_bytes_written_are_the_bytes_that_were_hashed(self):
        """Invariant 7's actual meaning, against its literal wording.

        The source is swapped the instant it is first read. A single-read
        executor already holds the verified buffer and writes it. A
        reopen-then-copy executor -- which satisfies 'reopened bytes match
        source_sha256' word for word -- writes the poison.

        The assertion is on the DESTINATION CONTENT, not on a read count: an
        implementation may legitimately read once and buffer, and counting
        would pin the mechanism instead of the property."""
        real_read_bytes = Path.read_bytes

        def poisoning_read(self_path):
            data = real_read_bytes(self_path)
            if os.path.abspath(self_path) == os.path.abspath(self.source_path):
                real_write = Path.write_bytes
                real_write(self_path, POISON)
            return data

        with patch.object(Path, "read_bytes", poisoning_read):
            self._execute()

        self.assertEqual(
            self.dest_path.read_bytes(),
            FOUNDATION,
            "the projection carries bytes that were never hashed -- the source "
            "was swapped between the verification read and the copy",
        )

    def test_a_source_swapped_before_any_read_is_still_caught(self):
        """The complement, so the single-read fix cannot be mistaken for
        'trust the first read': if the swap happens before execution, the hash
        check is what must fire."""
        self.source_path.write_bytes(POISON)

        with self.assertRaises(ProjectFileExecutionError) as ctx:
            self._execute()
        self.assertIn("source_sha256", str(ctx.exception))
        self.assertFalse(self.dest_path.exists())


class TestSourceConfinement(ProjectFileTestBase):
    def test_the_pinned_schema_owns_staging_confinement(self):
        """PREMISE, not a guard of this slice.

        The design assumed §383 confinement was C's to enforce. It is not: the
        spec says the source is "syntactically confined", and S4-A wired that
        into the pinned v1 schema, so a planted in-workspace file never produces
        a capability at all. Duplicating the check in the executor would be an
        UNREACHABLE guard -- protection-shaped and untestable at its own level.

        Pinned here so a loosened schema surfaces as this test failing, rather
        than as a silently open hole in the executor."""
        planted = self.workspace_root / "units" / "u_test" / "planted.md"
        planted.parent.mkdir(parents=True, exist_ok=True)
        planted.write_bytes(FOUNDATION)

        with self.assertRaises(Exception) as ctx:
            self._validated([self._op(source_path="units/u_test/planted.md")])
        self.assertIn("pinned MaterializationPlan v1 schema", str(ctx.exception))

    def test_the_canonicalizer_owns_the_post_validation_symlink(self):
        """PREMISE, and a correction to this slice's own design note.

        The design claimed A's symlink rejection ran only at validation, leaving
        a validation-vs-use gap for C to close. It does not: the executor calls
        canonicalize_workspace_path itself, and that lstat-walks every component
        including the last, so a symlink planted between validation and
        execution IS caught -- upstream, at execution time.

        The class is still real (S4-B's WorkspaceSpec re-check was a genuine
        instance); this particular path simply is not an instance of it."""
        validated = self._validated()

        outside = self.tmp / "outside-the-workspace"
        outside.write_bytes(FOUNDATION)
        self.source_path.unlink()
        self.source_path.symlink_to(outside)

        with self.assertRaises(Exception) as ctx:
            self._execute(validated)
        self.assertIn("passes through a symlink", str(ctx.exception))

    def test_an_irregular_staged_input_is_rejected(self):
        """What is genuinely this guard's own: the input neither upstream check
        sees. The schema is satisfied (the path is syntactically a staging
        input), the canonicalizer is satisfied (no symlink anywhere), and the
        thing sitting there is not a file anyone can project."""
        validated = self._validated()
        self.source_path.unlink()
        os.mkfifo(self.source_path)

        with self.assertRaises(ProjectFileExecutionError) as ctx:
            self._execute(validated)
        self.assertIn("regular file", str(ctx.exception))

    def test_missing_staged_input_with_no_current_projection_is_rejected(self):
        validated = self._validated()
        self.source_path.unlink()

        with self.assertRaises(ProjectFileExecutionError) as ctx:
            self._execute(validated)
        self.assertIn("staged input", str(ctx.exception))


class TestStagedInputSurvival(ProjectFileTestBase):
    def test_a_successful_projection_does_not_delete_the_staged_input(self):
        """Invariant 8, and the spec names the proxy it is warning against: 'a
        successful destination write or in-memory operation result is not
        acknowledgement.' An executor that copies then deletes passes every
        content assertion while leaving the receipt with no recoverable
        input."""
        self._execute()

        self.assertTrue(
            self.source_path.exists(),
            "the staged input was deleted before any receipt was published; a "
            "crash here loses the only recoverable copy",
        )

    def test_a_rerun_after_cleanup_succeeds_from_the_destination_alone(self):
        """The consequence of carving cleanup into its own slice, and the
        reason C cannot simply require the staged input to exist.

        Run the sequence forward: C projects -> receipt publishes -> the
        cleanup slice deletes the staged input -> someone applies again. The
        input is gone. An executor that demands a source would make the SECOND
        apply fail, so C and the cleanup slice together would produce a system
        that breaks on its own normal sequence.

        A destination already hashing to source_sha256 IS proof the projection
        is current -- the hash is the contract, and satisfying it needs no
        source read. Note this short-circuit only reaches a rerun: on a first
        apply dest does not exist, so every source guard still runs."""
        self._execute()
        self.source_path.unlink()  # the cleanup slice, having done its job

        evidence = self._execute()

        self.assertIs(evidence["written"], False)
        self.assertEqual(self.dest_path.read_bytes(), FOUNDATION)

    def test_a_stale_destination_still_demands_the_staged_input(self):
        """The boundary of that short-circuit. If dest does NOT satisfy the
        declared hash, the projection is not current and the source is
        genuinely required -- otherwise 'dest exists' would quietly become
        sufficient and a stale AGENTS.md would survive forever."""
        self._execute()
        self.dest_path.write_bytes(b"stale\n")
        self.source_path.unlink()

        with self.assertRaises(ProjectFileExecutionError) as ctx:
            self._execute()
        self.assertIn("staged input", str(ctx.exception))


class TestPublicationOrdering(ProjectFileTestBase):
    def test_a_projection_failing_verification_never_appears_at_dest(self):
        """Carried directly from S4-B's survivor: an end-state assertion cannot
        see an ordering property, because a write-then-delete-on-failure
        implementation reaches an identical end state. What differs is the
        transient, and the transient is what a crash freezes -- here, a
        half-written AGENTS.md that Codex would read on startup.

        So the assertion is on what was true AT the failure instant."""
        from gr2.python_cli import file_exec

        observed: dict[str, object] = {}
        real_verify = file_exec.verify_staged_source

        def failing_verify(*args, **kwargs):
            observed["dest_existed"] = self.dest_path.exists()
            real_verify(*args, **kwargs)
            raise ProjectFileExecutionError("injected verification failure")

        with patch.object(file_exec, "verify_staged_source", failing_verify):
            with self.assertRaises(ProjectFileExecutionError):
                self._execute()

        self.assertIs(observed["dest_existed"], False)
        self.assertFalse(self.dest_path.exists())

    def test_no_temporary_file_survives_a_successful_projection(self):
        self._execute()
        siblings = sorted(p.name for p in self.dest_path.parent.iterdir())
        self.assertEqual(siblings, ["AGENTS.md"], f"temp file leaked: {siblings}")


class TestExecutorBinding(ProjectFileTestBase):
    def test_evidence_is_receipt_shaped_and_identity_free(self):
        validated = self._validated()
        evidence = self._execute(validated)
        receipt_path = write_materialization_receipt(
            self.workspace_root, validated, [evidence]
        )

        receipt = json.loads(receipt_path.read_text())
        self.assertEqual(receipt["operations"][0]["kind"], "project_file")
        self.assertEqual(receipt["stage"], "MATERIALIZED")

    def test_executing_against_a_different_workspace_root_is_rejected(self):
        validated = self._validated()
        other_root = self.tmp / "other-workspace"
        other_spec = workspace_spec_path(other_root)
        other_spec.parent.mkdir(parents=True, exist_ok=True)
        other_spec.write_text('workspace_name = "other"\n')

        with self.assertRaises(ProjectFileExecutionError) as ctx:
            execute_project_file_operation(validated, 0, workspace_root=other_root)
        self.assertIn("WorkspaceSpec", str(ctx.exception))

    def test_a_tampered_capability_cannot_execute(self):
        validated = self._validated()
        object.__setattr__(validated, "plan_id", "mp_swapped")

        with self.assertRaises(Exception) as ctx:
            self._execute(validated)
        self.assertIn("capability is invalid", str(ctx.exception))

    def test_a_copied_capability_cannot_execute(self):
        import dataclasses

        copied = dataclasses.replace(self._validated())
        with self.assertRaises(Exception) as ctx:
            self._execute(copied)
        self.assertIn("capability is invalid", str(ctx.exception))

    def test_a_non_project_file_operation_is_refused_by_index(self):
        validated = self._validated(
            [
                {
                    "kind": "venv",
                    "dest_path": "units/u_test/home/.venv",
                    "engine": "uv",
                    "python": "3.11",
                },
                self._op(),
            ]
        )
        with self.assertRaises(ProjectFileExecutionError) as ctx:
            self._execute(validated, 0)
        self.assertIn("is kind 'venv'", str(ctx.exception))
        # The refusal is user-visible: it says what this executor does, and names no
        # internal build slice a user cannot look up.
        self.assertIn("applies project_file operations only", str(ctx.exception))
        self.assertNotRegex(str(ctx.exception), r"\bS4-[A-D]\b")


if __name__ == "__main__":
    unittest.main()
