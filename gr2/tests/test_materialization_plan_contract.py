"""Plan-contract tests for the neutral MaterializationPlan v1 (S4-A).

Covers the PLAN-LEVEL contract only: pinned-schema conformance, the
schema SHA pin, identity-freedom, opaque-token safety, WorkspaceSpec
binding, path canonicalization, destination-collision detection, and
durable receipt publication.

Operation EXECUTION is deliberately absent from S4-A and lands with
S4-B (clone/cache/alternates), S4-C (staging/project_file), and S4-D
(venv/editable + PEP 610), each with its own domain validation and
mutation set -- see the grip#797 split.

Testing discipline carried forward from that review cycle: assert WHICH
invariant fired, not merely that some MaterializationPlanError was
raised. A fixture broken enough to trip the invariant under test is
usually broken enough to trip its neighbours too, and a type-only
assertRaises cannot tell them apart.
"""
from __future__ import annotations

import copy
import dataclasses
import hashlib
import json
import os
import stat
import unittest
from pathlib import Path
from unittest.mock import patch

from gr2.python_cli import spec_apply
from gr2.python_cli.spec_apply import (
    MaterializationPlanError,
    canonicalize_workspace_path,
    compute_plan_hash,
    materialization_receipt_path,
    validate_materialization_plan,
    workspace_spec_path,
    write_materialization_receipt,
)


class PlanContractTestBase(unittest.TestCase):
    def setUp(self):
        import tempfile

        self.tmp = Path(tempfile.mkdtemp())
        self.workspace_root = self.tmp / "workspace"
        self.workspace_root.mkdir(parents=True)
        self.spec_sha256 = self._write_workspace_spec()

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_workspace_spec(self, content: str = 'workspace_name = "test"\n') -> str:
        spec_path = workspace_spec_path(self.workspace_root)
        spec_path.parent.mkdir(parents=True, exist_ok=True)
        spec_path.write_text(content)
        return hashlib.sha256(spec_path.read_bytes()).hexdigest()

    def _plan(self, operations: list[dict[str, object]], **overrides: object) -> dict[str, object]:
        plan: dict[str, object] = {
            "schema_version": 1,
            "plan_id": "mp_test",
            "unit_key": "u_test",
            "workspace_spec_sha256": self.spec_sha256,
            "operations": operations,
        }
        plan.update(overrides)
        return plan

    def _clone_op(self, **fields: object) -> dict[str, object]:
        op: dict[str, object] = {
            "kind": "clone",
            "branch": "main",
            "repo_url": "https://example.com/product.git",
            "dest_path": "units/u1/product",
        }
        op.update(fields)
        return op

    def _venv_op(self, **fields: object) -> dict[str, object]:
        op: dict[str, object] = {
            "kind": "venv",
            "dest_path": "units/u1/.venv",
            "engine": "uv",
            "python": ">=3.11",
        }
        op.update(fields)
        return op

    def _project_file_op(self, **fields: object) -> dict[str, object]:
        op: dict[str, object] = {
            "kind": "project_file",
            "source_path": ".grip/staging/inputs/f_01",
            "source_sha256": "a" * 64,
            "dest_path": "units/u1/AGENTS.md",
            "mode": "copy",
        }
        op.update(fields)
        return op

    def _reject(self, plan: dict[str, object], expected_fragment: str, label: str) -> None:
        with self.assertRaises(MaterializationPlanError, msg=f"{label} must be rejected") as ctx:
            validate_materialization_plan(self.workspace_root, plan)
        self.assertIn(expected_fragment, str(ctx.exception), f"{label}: wrong invariant fired")


class TestSchemaPin(PlanContractTestBase):
    def test_packaged_schema_matches_pinned_sha256(self):
        raw = spec_apply._read_plan_schema_bytes()
        self.assertEqual(hashlib.sha256(raw).hexdigest(), spec_apply._PLAN_SCHEMA_SHA256)

    def test_schema_load_fails_closed_on_hash_mismatch(self):
        """A tampered or unpinned schema must refuse to validate at all,
        rather than silently enforcing a different contract."""
        spec_apply._plan_validator = None
        try:
            with patch.object(spec_apply, "_read_plan_schema_bytes", return_value=b"{}"):
                self._reject(
                    self._plan([self._venv_op()]),
                    "hash mismatch",
                    "tampered schema bytes",
                )
        finally:
            spec_apply._plan_validator = None


class TestPinnedSchemaConformance(PlanContractTestBase):
    """The MaterializationPlan v1 malformed-plan set. A hand-rolled validator is a
    separate, looser contract by construction; these pin conformance to
    the pinned schema itself."""

    def test_valid_plan_accepted(self):
        validate_materialization_plan(self.workspace_root, self._plan([self._venv_op()]))

    def test_clone_without_branch_rejected(self):
        op = self._clone_op()
        del op["branch"]
        self._reject(self._plan([op]), "schema", "clone without required branch")

    def test_venv_without_engine_and_python_rejected(self):
        self._reject(
            self._plan([{"kind": "venv", "dest_path": "units/u1/.venv"}]),
            "schema",
            "venv without required engine/python",
        )

    def test_editable_install_without_extras_rejected(self):
        self._reject(
            self._plan(
                [{"kind": "editable_install", "venv_path": "units/u1/.venv", "source_path": "units/u1/src"}]
            ),
            "schema",
            "editable_install without required extras",
        )

    def test_project_file_without_mode_rejected(self):
        op = self._project_file_op()
        del op["mode"]
        self._reject(self._plan([op]), "schema", "project_file without required mode")

    def test_short_workspace_spec_sha_rejected(self):
        self._reject(
            self._plan([self._venv_op()], workspace_spec_sha256="abc123"),
            "schema",
            "six-character workspace_spec_sha256",
        )

    def test_dot_path_segment_rejected(self):
        self._reject(
            self._plan([self._venv_op(dest_path="units/./u1/.venv")]),
            "schema",
            "literal '.' path segment",
        )

    def test_backslash_path_rejected(self):
        self._reject(
            self._plan([self._venv_op(dest_path="units\\u1\\.venv")]),
            "schema",
            "backslash path",
        )

    def test_duplicate_extras_rejected(self):
        self._reject(
            self._plan(
                [
                    {
                        "kind": "editable_install",
                        "venv_path": "units/u1/.venv",
                        "source_path": "units/u1/src",
                        "extras": ["dev", "dev"],
                    }
                ]
            ),
            "schema",
            "duplicate extras (uniqueItems)",
        )

    def test_overlong_plan_id_rejected(self):
        self._reject(
            self._plan([self._venv_op()], plan_id="p" * 129),
            "schema",
            "129-character plan_id",
        )

    def test_nested_staged_input_path_rejected(self):
        self._reject(
            self._plan([self._project_file_op(source_path=".grip/staging/inputs/a/b")]),
            "schema",
            "nested staged input path",
        )

    def test_boolean_schema_version_rejected(self):
        """bool is an int subclass in Python, so True == 1 slips a naive
        `!= 1` check; JSON Schema's const:1 distinguishes the types."""
        self._reject(
            self._plan([self._venv_op()], schema_version=True),
            "schema",
            "boolean schema_version",
        )

    def test_invalid_opaque_unit_key_rejected(self):
        self._reject(
            self._plan([self._venv_op()], unit_key="../escape"),
            "schema",
            "invalid opaque unit_key",
        )

    def test_reference_base_outside_cache_namespace_rejected(self):
        """The schema confines reference_base syntactically to
        .grip/cache/repos/<name>.git. Filesystem provenance (is it a real
        bare cache with the right origin) is S4-B's."""
        self._reject(
            self._plan([self._clone_op(reference_base="units/u1/rogue-cache.git")]),
            "schema",
            "reference_base outside the cache namespace",
        )

    def test_unknown_top_level_field_rejected(self):
        self._reject(self._plan([self._venv_op()], org="synapt"), "schema", "unknown top-level field")

    def test_unknown_operation_field_rejected(self):
        """An allowlist rejects any field not explicitly permitted, by
        construction -- a blacklist only catches names someone enumerated."""
        self._reject(
            self._plan([self._venv_op(metadata={"display_name": "Apollo"})]),
            "schema",
            "unknown operation field",
        )

    def test_empty_operations_rejected(self):
        self._reject(self._plan([]), "schema", "empty operations list")


class TestIdentityFreedom(PlanContractTestBase):
    def test_rejects_top_level_identity_field_on_operation(self):
        self._reject(
            self._plan([self._clone_op(agent_name="apollo")]),
            "schema",
            "operation carrying agent_name",
        )

    def test_recursive_identity_rejection_is_reachable(self):
        """Defence in depth beneath the allowlist: even inside an allowed
        field's VALUE, an identity-shaped key must be rejected. Exercised
        directly since the allowlist would reject the carrier field first."""
        with self.assertRaises(MaterializationPlanError) as ctx:
            spec_apply._reject_identity_fields_recursive(
                {"extras": [{"channel": "dev"}]}, path="operations[0]"
            )
        self.assertIn("identity-bearing", str(ctx.exception))


class TestWorkspaceSpecBinding(PlanContractTestBase):
    def test_missing_workspace_spec_file_rejected(self):
        workspace_spec_path(self.workspace_root).unlink()
        self._reject(
            self._plan([self._venv_op()]),
            "cannot be verified",
            "absent canonical WorkspaceSpec",
        )

    def test_workspace_spec_hash_mismatch_rejected(self):
        self._write_workspace_spec('workspace_name = "drifted"\n')
        self._reject(
            self._plan([self._venv_op()]),
            "workspace_spec_sha256 mismatch",
            "plan compiled against different workspace state",
        )


class TestDestinationCollisions(PlanContractTestBase):
    def test_case_only_duplicate_dest_paths_rejected(self):
        self._reject(
            self._plan([self._venv_op(dest_path="units/u1/.venv"), self._venv_op(dest_path="units/u1/.VENV")]),
            "collides",
            "case-only destination collision",
        )

    def test_unicode_casefold_duplicate_dest_paths_rejected(self):
        """Kills a `.lower()` substitution specifically: lower() leaves 'ß'
        alone, only casefold() folds it to 'ss'."""
        self._reject(
            self._plan(
                [
                    self._venv_op(dest_path="units/straße/.venv"),
                    self._venv_op(dest_path="units/STRASSE/.venv"),
                ]
            ),
            "collides",
            "Unicode casefold collision",
        )

    def test_distinct_destinations_accepted(self):
        validate_materialization_plan(
            self.workspace_root,
            self._plan(
                [self._venv_op(dest_path="units/a/.venv"), self._venv_op(dest_path="units/b/.venv")]
            ),
        )


class TestCanonicalizationDefenseInDepth(PlanContractTestBase):
    """The pinned schema's workspaceRelativePath pattern is a strictly
    stronger FIRST gate, so plan-level tests never reach canonicalization.
    That makes this layer invisible to them -- and it is genuinely
    load-bearing, because the legacy apply_plan adapter and the S4-B/C/D
    handlers call it without schema validation. Tested directly, at its
    own level."""

    def _reject_path(self, relative: str, fragment: str) -> None:
        with self.assertRaises(MaterializationPlanError, msg=f"{relative!r} must be rejected") as ctx:
            canonicalize_workspace_path(self.workspace_root, relative, field_name="probe")
        self.assertIn(fragment, str(ctx.exception))

    def test_rejects_single_dot_segment(self):
        self._reject_path("a/./b", "segments")

    def test_rejects_dotdot_segment(self):
        self._reject_path("a/../b", "segments")

    def test_rejects_empty_segment(self):
        self._reject_path("a//b", "segments")

    def test_rejects_backslash(self):
        self._reject_path("a\\b", "backslashes or NUL")

    def test_rejects_nul_byte(self):
        """Without the raw guard this escapes as an uncaught ValueError from
        Path.resolve() ("embedded null character") -- crash-class, not just
        a weakened check."""
        self._reject_path("a/b\x00c", "backslashes or NUL")

    def test_rejects_absolute_path(self):
        self._reject_path("/etc/passwd", "relative to the workspace root")

    def test_rejects_tilde(self):
        self._reject_path("~/secrets", "relative to the workspace root")

    def test_rejects_symlinked_directory_prefix(self):
        """A resolve()-only containment check cannot catch this: with a
        prefix directory that is itself a symlink, both the candidate and
        the root resolve THROUGH the same link."""
        outside = self.tmp / "outside"
        outside.mkdir()
        (self.workspace_root / "linked").symlink_to(outside)
        self._reject_path("linked/payload", "symlink")

    def test_rejects_symlinked_final_component(self):
        outside = self.tmp / "secret.txt"
        outside.write_text("x")
        (self.workspace_root / "alias").symlink_to(outside)
        self._reject_path("alias", "symlink")

    def test_accepts_ordinary_relative_path(self):
        resolved = canonicalize_workspace_path(
            self.workspace_root, "units/u1/product", field_name="probe"
        )
        self.assertEqual(resolved, (self.workspace_root / "units" / "u1" / "product").resolve())


class TestPlanHash(PlanContractTestBase):
    def test_uses_pinned_canonical_serialization(self):
        """Two independent proofs that this is the PINNED serialization,
        not merely a deterministic one: it equals the pinned recipe, and it
        does NOT equal the default-separator / ensure_ascii variants."""
        plan = self._plan([self._venv_op()], unit_key="u_straße")
        pinned = hashlib.sha256(
            json.dumps(plan, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        ).hexdigest()
        self.assertEqual(compute_plan_hash(plan), pinned)
        self.assertNotEqual(
            compute_plan_hash(plan),
            hashlib.sha256(json.dumps(plan, sort_keys=True).encode()).hexdigest(),
        )
        self.assertNotEqual(
            compute_plan_hash(plan),
            hashlib.sha256(
                json.dumps(plan, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
            ).hexdigest(),
        )

    def test_hash_changes_with_plan_content(self):
        a = self._plan([self._venv_op(dest_path="units/a/.venv")])
        b = self._plan([self._venv_op(dest_path="units/b/.venv")])
        self.assertNotEqual(compute_plan_hash(a), compute_plan_hash(b))


class TestReceiptPublication(PlanContractTestBase):
    def _validated(self, **overrides: object):
        plan = self._plan([self._venv_op()], plan_id="mp_receipt", **overrides)
        return validate_materialization_plan(self.workspace_root, plan)

    def _evidence(self) -> list[dict[str, object]]:
        """Evidence must correspond to the plan's operations, in order."""
        return [{"kind": "venv", "dest_path": "units/u1/.venv"}]

    def test_receipt_schema_is_exact(self):
        validated = self._validated()
        write_materialization_receipt(self.workspace_root, validated, self._evidence())
        receipt = json.loads(materialization_receipt_path(self.workspace_root, "mp_receipt").read_text())
        self.assertEqual(
            set(receipt),
            {
                "plan_id",
                "unit_key",
                "plan_hash",
                "schema_version",
                "workspace_spec_sha256",
                "stage",
                "applied_at",
                "operations",
            },
        )
        self.assertEqual(receipt["stage"], "MATERIALIZED")
        self.assertEqual(receipt["unit_key"], "u_test")
        self.assertEqual(receipt["workspace_spec_sha256"], self.spec_sha256)
        # Recomputed independently -- a constant-hash mutant dies here.
        self.assertEqual(receipt["plan_hash"], compute_plan_hash(validated.plan))

    def test_publication_requires_a_validated_plan_capability(self):
        """Atlas P1: the writer used to accept the raw live plan. Holding a
        ValidatedPlan IS the proof the contract ran; a raw dict is not."""
        raw = self._plan([self._venv_op()], plan_id="mp_receipt")
        with self.assertRaises(MaterializationPlanError) as ctx:
            write_materialization_receipt(self.workspace_root, raw, self._evidence())
        self.assertIn("requires a ValidatedPlan", str(ctx.exception))

    def test_validated_plan_cannot_be_forged(self):
        """The capability is only mintable by the validator -- otherwise it
        would be a naming convention rather than a guarantee.

        The forged shell is otherwise FULLY self-consistent: a real frozen
        snapshot, its true hash, and facts that agree with it. That makes the
        seal check provably the thing that rejects it, rather than one of the
        consistency checks that run ahead of it."""
        genuine = validate_materialization_plan(
            self.workspace_root, self._plan([self._venv_op()], plan_id="mp_forge")
        )
        with self.assertRaises(MaterializationPlanError) as ctx:
            spec_apply.ValidatedPlan(
                plan=genuine.plan,
                plan_id=genuine.plan_id,
                unit_key=genuine.unit_key,
                schema_version=genuine.schema_version,
                workspace_spec_sha256=genuine.workspace_spec_sha256,
                operation_kinds=genuine.operation_kinds,
                plan_hash=genuine.plan_hash,
                _seal="0" * 64,
            )
        self.assertIn("not minted by", str(ctx.exception))

    def test_invalid_plan_id_cannot_reach_publication(self):
        """Atlas P1 fruit: plan_id="../../escaped" published
        .grip/escaped.json outside the receipt directory. It can no longer
        be validated, so it can no longer be published."""
        with self.assertRaises(MaterializationPlanError) as ctx:
            validate_materialization_plan(
                self.workspace_root, self._plan([self._venv_op()], plan_id="../../escaped")
            )
        self.assertIn("schema", str(ctx.exception))
        self.assertFalse((self.workspace_root / ".grip" / "escaped.json").exists())

    def test_empty_evidence_cannot_claim_materialized(self):
        """Atlas P1 fruit: a one-operation plan with op_results=[] published
        stage=MATERIALIZED with no evidence at all."""
        with self.assertRaises(MaterializationPlanError) as ctx:
            write_materialization_receipt(self.workspace_root, self._validated(), [])
        self.assertIn("evidence for every operation", str(ctx.exception))
        self.assertFalse(materialization_receipt_path(self.workspace_root, "mp_receipt").exists())

    def test_wrong_kind_evidence_rejected(self):
        with self.assertRaises(MaterializationPlanError) as ctx:
            write_materialization_receipt(
                self.workspace_root, self._validated(), [{"kind": "clone"}]
            )
        self.assertIn("must correspond to its operation", str(ctx.exception))

    def test_direct_identity_field_in_evidence_rejected(self):
        with self.assertRaises(MaterializationPlanError) as ctx:
            write_materialization_receipt(
                self.workspace_root, self._validated(), [{"kind": "venv", "secret": "TOKEN"}]
            )
        self.assertIn("identity-bearing", str(ctx.exception))

    def test_nested_identity_field_in_evidence_rejected(self):
        """Atlas P1 fruit, the real smuggling boundary: the plan's closed
        schema permits no nested object carrier, but the RESULT graph is
        open and is what gets persisted."""
        with self.assertRaises(MaterializationPlanError) as ctx:
            write_materialization_receipt(
                self.workspace_root,
                self._validated(),
                [{"kind": "venv", "nested": {"memory_body": "PRIVATE"}}],
            )
        self.assertIn("identity-bearing", str(ctx.exception))
        self.assertFalse(materialization_receipt_path(self.workspace_root, "mp_receipt").exists())

    def test_publication_order_is_exact(self):
        """Counting two fsync calls does not
        pin the SEQUENCE -- moving the parent-directory fsync before
        os.replace left all five prior tests green.

        Deleting `fh.flush()` ALSO left the suite green, and it is the
        subtler mutant: close() flushes anyway, so the published bytes come
        out identical while the durability protocol is quietly false --
        fsync would sync an empty file and the bytes would land after it.
        Content assertions can never see that; only an ordered oracle can.

        So the oracle observes the whole chain including flush, with fd
        roles resolved via fstat so the file fsync is distinguished from the
        directory fsync. Each mutant then dies for THIS invariant rather
        than through some unrelated failure."""
        events: list[str] = []
        original_fsync = os.fsync
        original_replace = os.replace
        original_fdopen = os.fdopen

        class _TrackingWriter:
            """Records write/flush without being able to forge them: close()
            flushes the INNER object directly, bypassing this wrapper, so a
            deleted explicit flush cannot be masked by the close-flush."""

            def __init__(self, inner):
                self._inner = inner

            def write(self, data):
                events.append("write")
                return self._inner.write(data)

            def flush(self):
                events.append("flush")
                return self._inner.flush()

            def fileno(self):
                return self._inner.fileno()

            def __enter__(self):
                self._inner.__enter__()
                return self

            def __exit__(self, *exc_info):
                return self._inner.__exit__(*exc_info)

        def tracking_fdopen(fd, *args, **kwargs):
            return _TrackingWriter(original_fdopen(fd, *args, **kwargs))

        def tracking_fsync(fd):
            kind = "dir" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file"
            events.append(f"fsync:{kind}")
            return original_fsync(fd)

        def tracking_replace(src, dst, **kwargs):
            events.append("replace")
            return original_replace(src, dst, **kwargs)

        with patch.object(spec_apply.os, "fdopen", tracking_fdopen):
            with patch.object(spec_apply.os, "fsync", tracking_fsync):
                with patch.object(spec_apply.os, "replace", tracking_replace):
                    write_materialization_receipt(
                        self.workspace_root, self._validated(), self._evidence()
                    )

        self.assertEqual(
            events, ["write", "flush", "fsync:file", "replace", "fsync:dir"], events
        )

    def test_no_receipt_published_when_fsync_fails(self):
        with patch.object(spec_apply.os, "fsync", side_effect=OSError("simulated fsync failure")):
            with self.assertRaises(OSError):
                write_materialization_receipt(
                    self.workspace_root, self._validated(), self._evidence()
                )
        self.assertFalse(materialization_receipt_path(self.workspace_root, "mp_receipt").exists())

    def test_publication_is_atomic_replace_not_direct_write(self):
        """Asserting only "no temp file left behind" cannot distinguish
        atomic publication from a direct write -- a direct write also
        trivially leaves no temp file. Failing the replace step itself can."""
        original_replace = os.replace

        def failing_replace(src, dst, **kwargs):
            if "materialization" in str(src):
                raise OSError("simulated replace failure")
            return original_replace(src, dst, **kwargs)

        with patch.object(spec_apply.os, "replace", failing_replace):
            with self.assertRaises(OSError):
                write_materialization_receipt(
                    self.workspace_root, self._validated(), self._evidence()
                )
        self.assertFalse(materialization_receipt_path(self.workspace_root, "mp_receipt").exists())

    def test_no_temp_file_left_behind_on_success(self):
        write_materialization_receipt(self.workspace_root, self._validated(), self._evidence())
        state_dir = self.workspace_root / ".grip" / "state" / "materialization"
        self.assertEqual(list(state_dir.glob("*.tmp-*")), [])

    def test_temp_file_failure_leaves_no_residue(self):
        with patch.object(spec_apply.os, "fsync", side_effect=OSError("boom")):
            with self.assertRaises(OSError):
                write_materialization_receipt(
                    self.workspace_root, self._validated(), self._evidence()
                )
        state_dir = self.workspace_root / ".grip" / "state" / "materialization"
        self.assertEqual(list(state_dir.glob("*.tmp-*")), [])


class TestDerivedPathHardening(PlanContractTestBase):
    """Atlas P2: §6.2.1 #2 applies to the A-owned DERIVED paths (canonical
    WorkspaceSpec, receipt directory, receipt temp file), not only to
    operation paths. All three probes succeeded before this closure."""

    def test_symlinked_workspace_spec_rejected(self):
        """A symlink at .grip/workspace_spec.toml pointing outside the team
        root was accepted whenever its bytes hashed to the declared value.
        The hash confirms CONTENT; it says nothing about whether the file
        read is inside the workspace."""
        outside = self.tmp / "outside_spec.toml"
        outside.write_text('workspace_name = "outside"\n')
        outside_sha = hashlib.sha256(outside.read_bytes()).hexdigest()

        spec_path = workspace_spec_path(self.workspace_root)
        spec_path.unlink()
        spec_path.symlink_to(outside)

        with self.assertRaises(MaterializationPlanError) as ctx:
            validate_materialization_plan(
                self.workspace_root,
                self._plan([self._venv_op()], workspace_spec_sha256=outside_sha),
            )
        self.assertIn("symlink", str(ctx.exception))

    def test_symlinked_receipt_directory_rejected(self):
        """A symlinked .grip/state/materialization published the terminal
        receipt outside the team root entirely."""
        outside_dir = self.tmp / "outside_receipts"
        outside_dir.mkdir()
        state_dir = self.workspace_root / ".grip" / "state"
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / "materialization").symlink_to(outside_dir)

        validated = validate_materialization_plan(
            self.workspace_root, self._plan([self._venv_op()], plan_id="mp_escape")
        )
        with self.assertRaises(MaterializationPlanError) as ctx:
            write_materialization_receipt(
                self.workspace_root, validated, [{"kind": "venv"}]
            )
        self.assertIn("symlink", str(ctx.exception))
        self.assertEqual(list(outside_dir.iterdir()), [])

    def test_pre_created_temp_symlink_is_not_followed(self):
        """The temp name is predictable (mp_<id>.json.tmp-<pid>), so a plain
        open() followed a pre-created symlink, overwrote the external
        target, and then published that symlink as the final receipt.
        O_EXCL|O_NOFOLLOW refuses instead."""
        outside_target = self.tmp / "victim.txt"
        outside_target.write_text("ORIGINAL")

        receipt_dir = self.workspace_root / ".grip" / "state" / "materialization"
        receipt_dir.mkdir(parents=True, exist_ok=True)
        validated = validate_materialization_plan(
            self.workspace_root, self._plan([self._venv_op()], plan_id="mp_receipt")
        )
        (receipt_dir / f"mp_receipt.json.tmp-{os.getpid()}").symlink_to(outside_target)

        with self.assertRaises(OSError):
            write_materialization_receipt(
                self.workspace_root, validated, [{"kind": "venv"}]
            )
        self.assertEqual(outside_target.read_text(), "ORIGINAL")
        self.assertFalse(materialization_receipt_path(self.workspace_root, "mp_receipt").exists())


class TestCanonicalizerFieldWiring(PlanContractTestBase):
    """Replacing EVERY operation-path call with a raw
    resolve left all 43 tests green -- the canonicalizer was reachable in
    principle and unpinned per field in practice.

    Each of the seven path-bearing fields gets its own production-shaped
    symlink fixture, so removing that specific call site turns exactly this
    test RED. Symlink (not traversal) is the right probe: the schema
    rejects traversal syntactically first, so only a filesystem-level
    fixture can prove the call is actually wired."""

    def _link(self, name: str) -> None:
        outside = self.tmp / f"outside_{name}"
        outside.mkdir(exist_ok=True)
        link = self.workspace_root / name
        if not link.exists():
            link.symlink_to(outside)

    def _expect_symlink_rejection(self, plan: dict[str, object], label: str) -> None:
        with self.assertRaises(MaterializationPlanError, msg=f"{label} must be rejected") as ctx:
            validate_materialization_plan(self.workspace_root, plan)
        self.assertIn("symlink", str(ctx.exception), f"{label}: wrong invariant fired")

    def test_clone_dest_path_is_canonicalized(self):
        self._link("linked")
        self._expect_symlink_rejection(
            self._plan([self._clone_op(dest_path="linked/product")]), "clone.dest_path"
        )

    def test_clone_reference_base_is_canonicalized(self):
        """reference_base must resolve inside .grip/cache/repos/ per the
        schema, so the symlink is planted at the cache directory itself."""
        outside = self.tmp / "outside_cache"
        outside.mkdir()
        cache_parent = self.workspace_root / ".grip" / "cache"
        cache_parent.mkdir(parents=True, exist_ok=True)
        (cache_parent / "repos").symlink_to(outside)
        self._expect_symlink_rejection(
            self._plan([self._clone_op(reference_base=".grip/cache/repos/product.git")]),
            "clone.reference_base",
        )

    def test_venv_dest_path_is_canonicalized(self):
        self._link("linked")
        self._expect_symlink_rejection(
            self._plan([self._venv_op(dest_path="linked/.venv")]), "venv.dest_path"
        )

    def test_editable_install_venv_path_is_canonicalized(self):
        self._link("linked")
        self._expect_symlink_rejection(
            self._plan(
                [
                    {
                        "kind": "editable_install",
                        "venv_path": "linked/.venv",
                        "source_path": "units/u1/src",
                        "extras": [],
                    }
                ]
            ),
            "editable_install.venv_path",
        )

    def test_editable_install_source_path_is_canonicalized(self):
        self._link("linked")
        self._expect_symlink_rejection(
            self._plan(
                [
                    {
                        "kind": "editable_install",
                        "venv_path": "units/u1/.venv",
                        "source_path": "linked/src",
                        "extras": [],
                    }
                ]
            ),
            "editable_install.source_path",
        )

    def test_project_file_source_path_is_canonicalized(self):
        outside = self.tmp / "outside_staging"
        outside.mkdir()
        staging_parent = self.workspace_root / ".grip" / "staging"
        staging_parent.mkdir(parents=True, exist_ok=True)
        (staging_parent / "inputs").symlink_to(outside)
        self._expect_symlink_rejection(
            self._plan([self._project_file_op()]), "project_file.source_path"
        )

    def test_project_file_dest_path_is_canonicalized(self):
        self._link("linked")
        self._expect_symlink_rejection(
            self._plan([self._project_file_op(dest_path="linked/AGENTS.md")]),
            "project_file.dest_path",
        )


class TestCollisionParticipation(PlanContractTestBase):
    """Returning None after canonicalizing the clone
    destination -- and separately the project-file destination -- each left
    43/43 green, because collision accounting was only ever proven with
    venv-vs-venv pairs. Those destinations simply vanished from the ledger.

    Every destination-bearing kind must participate, including across kinds."""

    def _expect_collision(self, ops: list[dict[str, object]], label: str) -> None:
        with self.assertRaises(MaterializationPlanError, msg=f"{label} must collide") as ctx:
            validate_materialization_plan(self.workspace_root, self._plan(ops))
        self.assertIn("collides", str(ctx.exception), f"{label}: wrong invariant fired")

    def test_clone_destination_participates(self):
        self._expect_collision(
            [
                self._clone_op(dest_path="units/u1/shared"),
                self._clone_op(dest_path="units/u1/shared"),
            ],
            "clone vs clone",
        )

    def test_project_file_destination_participates(self):
        self._expect_collision(
            [
                self._project_file_op(source_path=".grip/staging/inputs/f_01", dest_path="units/u1/X"),
                self._project_file_op(source_path=".grip/staging/inputs/f_02", dest_path="units/u1/X"),
            ],
            "project_file vs project_file",
        )

    def test_clone_and_venv_collide_across_kinds(self):
        self._expect_collision(
            [
                self._clone_op(dest_path="units/u1/shared"),
                self._venv_op(dest_path="units/u1/shared"),
            ],
            "clone vs venv",
        )

    def test_clone_and_project_file_collide_across_kinds(self):
        self._expect_collision(
            [
                self._clone_op(dest_path="units/u1/shared"),
                self._project_file_op(dest_path="units/u1/shared"),
            ],
            "clone vs project_file",
        )

    def test_venv_and_project_file_collide_across_kinds(self):
        self._expect_collision(
            [
                self._venv_op(dest_path="units/u1/shared"),
                self._project_file_op(dest_path="units/u1/shared"),
            ],
            "venv vs project_file",
        )

    def test_unicode_nfc_nfd_destination_alias_rejected(self):
        """NFC "café" and NFD "café" are distinct
        Python strings that casefold to distinct values, yet name ONE
        destination on a normalization-insensitive filesystem. Normalization
        and case folding are separate aliasing axes."""
        nfc = "units/café/.venv"
        nfd = "units/café/.venv"
        self.assertNotEqual(nfc, nfd)
        self.assertNotEqual(nfc.casefold(), nfd.casefold())
        self._expect_collision(
            [self._venv_op(dest_path=nfc), self._venv_op(dest_path=nfd)],
            "NFC vs NFD alias",
        )


class TestPlanHashOrdering(PlanContractTestBase):
    """A mutant that sorts `operations` before applying
    the otherwise-exact JSON recipe left 43/43 green. The normative formula
    preserves list order -- sort_keys sorts KEYS, never array elements."""

    def _two_op_plan(self, first_dest: str, second_dest: str) -> dict[str, object]:
        return self._plan(
            [self._venv_op(dest_path=first_dest), self._venv_op(dest_path=second_dest)]
        )

    def test_operation_order_changes_the_hash(self):
        a = self._two_op_plan("units/a/.venv", "units/b/.venv")
        b = self._two_op_plan("units/b/.venv", "units/a/.venv")
        self.assertNotEqual(compute_plan_hash(a), compute_plan_hash(b))

    def test_hash_matches_independently_computed_canonical_bytes(self):
        plan = self._two_op_plan("units/a/.venv", "units/b/.venv")
        expected_bytes = json.dumps(
            plan, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
        self.assertEqual(compute_plan_hash(plan), hashlib.sha256(expected_bytes).hexdigest())
        # And the canonical bytes themselves preserve array order.
        self.assertLess(
            expected_bytes.index(b"units/a/.venv"), expected_bytes.index(b"units/b/.venv")
        )


class TestIdentityRejectionProductionWiring(PlanContractTestBase):
    """Removing the recursive-identity call from
    validate_materialization_plan left 43/43 green, and removing "secret"
    from the inventory also left 43/43 green -- because the only direct
    helper test exercised "channel".

    Two closures: pin the PRODUCTION call beneath a permissive validator
    double (so the schema cannot shadow it), and table-test the complete
    declared inventory rather than one sampled member."""

    class _PermissiveValidator:
        """Stands in for the pinned schema validator so operations reach the
        hand layer. Without this the closed schema rejects the carrier field
        first and the production identity call is never exercised."""

        def iter_errors(self, _instance):
            return iter(())

    def test_production_call_is_wired_not_merely_present(self):
        plan = self._plan([self._venv_op(metadata={"channel": "dev"})])
        with patch.object(spec_apply, "_load_plan_validator", return_value=self._PermissiveValidator()):
            with patch.object(
                spec_apply, "_OPERATION_ALLOWED_FIELDS", dict(spec_apply._OPERATION_ALLOWED_FIELDS)
            ) as allowed:
                # Permit the carrier field so the allowlist cannot reject it
                # first -- isolating the recursive identity seam itself.
                allowed["venv"] = spec_apply._VENV_FIELDS | {"metadata"}
                with self.assertRaises(MaterializationPlanError) as ctx:
                    validate_materialization_plan(self.workspace_root, plan)
        self.assertIn("identity-bearing", str(ctx.exception))

    def test_forbidden_inventory_is_pinned(self):
        """The table test below iterates the LIVE set, so it is satisfied by
        whatever that set happens to contain: delete a member and the table
        simply gets shorter while staying green. A self-referential table
        cannot detect removal from the thing it enumerates.

        Sentinel's probe caught the `secret` removal only because a separate
        test hardcodes that one key -- which means every OTHER member was
        unprotected. Pinning the literal inventory closes the whole class."""
        self.assertEqual(
            spec_apply._FORBIDDEN_IDENTITY_KEYS,
            frozenset(
                {
                    "agent_name",
                    "agent_id",
                    "persistent_identity_ref",
                    "role",
                    "org",
                    "project",
                    "channel",
                    "channels",
                    "entitlement",
                    "entitlement_reason",
                    "secret",
                    "secret_ref",
                    "memory",
                    "memory_body",
                }
            ),
        )

    def test_every_declared_forbidden_key_is_enforced(self):
        """Table-test the whole inventory: sampling one member cannot catch
        a removal of any other. Pairs with the pin above -- the pin catches
        removal FROM the inventory, this catches non-enforcement OF it."""
        for key in sorted(spec_apply._FORBIDDEN_IDENTITY_KEYS):
            with self.subTest(forbidden_key=key):
                with self.assertRaises(MaterializationPlanError) as ctx:
                    spec_apply._reject_identity_fields_recursive(
                        {"outer": {key: "x"}}, path="probe"
                    )
                self.assertIn("identity-bearing", str(ctx.exception))

    def test_every_declared_forbidden_key_is_rejected_in_receipt_evidence(self):
        """The same closed rule on the persisted result graph."""
        validated = validate_materialization_plan(
            self.workspace_root, self._plan([self._venv_op()], plan_id="mp_inv")
        )
        for key in sorted(spec_apply._FORBIDDEN_IDENTITY_KEYS):
            with self.subTest(forbidden_key=key):
                with self.assertRaises(MaterializationPlanError) as ctx:
                    write_materialization_receipt(
                        self.workspace_root, validated, [{"kind": "venv", "metadata": {key: "x"}}]
                    )
                # Assert WHICH invariant fired: the cardinality and kind
                # checks run first and would reject a malformed probe for an
                # unrelated reason, leaving this test vacuous.
                self.assertIn("identity-bearing", str(ctx.exception))
        self.assertFalse(materialization_receipt_path(self.workspace_root, "mp_inv").exists())


class TestReceiptValueBinding(PlanContractTestBase):
    """Replacing `operations: op_results` with
    `operations: []`, and replacing the receipt plan_id with a constant,
    each left 43/43 green -- the receipt test asserted a key set plus four
    selected leaves, so every unasserted field was free to drift.

    Bind the WHOLE value, reconstructed independently."""

    def test_receipt_equals_independently_constructed_value(self):
        plan = self._plan(
            [self._venv_op(dest_path="units/a/.venv"), self._venv_op(dest_path="units/b/.venv")],
            plan_id="mp_bind",
            unit_key="u_bind",
        )
        validated = validate_materialization_plan(self.workspace_root, plan)
        evidence = [
            {"kind": "venv", "dest_path": "units/a/.venv", "created": True},
            {"kind": "venv", "dest_path": "units/b/.venv", "created": False},
        ]
        path = write_materialization_receipt(self.workspace_root, validated, evidence)
        receipt = json.loads(path.read_text())

        applied_at = receipt.pop("applied_at")
        self.assertIsInstance(applied_at, str)
        self.assertTrue(applied_at)
        self.assertEqual(
            receipt,
            {
                "plan_id": "mp_bind",
                "unit_key": "u_bind",
                "plan_hash": compute_plan_hash(plan),
                "schema_version": 1,
                "workspace_spec_sha256": self.spec_sha256,
                "stage": "MATERIALIZED",
                "operations": evidence,
            },
        )


class TestCapabilitySnapshotIsolation(PlanContractTestBase):
    """Atlas final re-gate: `frozen=True` on the dataclass freezes the field
    BINDING, not the nested graph. `plan` was the caller's live dict, and the
    writer recomputed plan_hash from it at publication time -- so mutating
    the plan after validation produced a receipt attesting to a graph that
    was never validated. The capability proved one graph was checked while
    vouching for another.

    Closure is a mint-time SNAPSHOT plus captured facts: the hash is computed
    once during validation and the receipt reads only what was captured.
    Both aliases must be dead ends -- the caller's original dict AND the
    representation the capability exposes."""

    def test_mutating_the_callers_plan_after_validation_cannot_move_the_receipt(self):
        """Atlas's exact repro, taken verbatim as the RED probe."""
        op = self._venv_op()
        plan = self._plan([op], plan_id="mp_alias")
        pre_validation_hash = compute_plan_hash(plan)

        validated = validate_materialization_plan(self.workspace_root, plan)

        # Mutate the ORIGINAL operation the caller still holds: escape the
        # workspace and smuggle an identity field, both of which validation
        # would have rejected outright.
        op["dest_path"] = "../../post_validation_escape"
        op["secret"] = "POST_VALIDATION_SECRET"

        path = write_materialization_receipt(
            self.workspace_root, validated, [{"kind": "venv"}]
        )
        receipt = json.loads(path.read_text())

        self.assertEqual(
            receipt["plan_hash"],
            pre_validation_hash,
            "the receipt must attest to the bytes that were validated, not to a "
            "post-validation mutation of the caller's dict",
        )
        self.assertNotEqual(receipt["plan_hash"], compute_plan_hash(plan))
        self.assertNotIn("POST_VALIDATION_SECRET", path.read_text())

    def test_mutating_the_capability_representation_is_impossible(self):
        """A snapshot alone still leaves the second alias open: whatever the
        capability EXPOSES is reachable by anyone holding it. Atlas asked for
        mutation of both, so the exposed graph is deeply immutable rather
        than merely a private copy."""
        plan = self._plan([self._venv_op()], plan_id="mp_frozen")
        validated = validate_materialization_plan(self.workspace_root, plan)

        with self.assertRaises(TypeError):
            validated.plan["plan_id"] = "mutated"
        with self.assertRaises(TypeError):
            validated.plan["operations"][0]["dest_path"] = "../escape"
        with self.assertRaises((TypeError, AttributeError)):
            validated.plan["operations"].append({"kind": "venv"})

    def test_snapshot_is_not_the_callers_object(self):
        """A shallow copy is explicitly insufficient (Atlas): the nested
        operation dicts must not be shared either."""
        op = self._venv_op()
        plan = self._plan([op], plan_id="mp_ident")
        validated = validate_materialization_plan(self.workspace_root, plan)
        self.assertIsNot(validated.plan, plan)
        self.assertIsNot(validated.plan["operations"][0], op)

    def test_mutation_during_validation_cannot_reach_the_validated_bytes(self):
        """The snapshot is taken on ENTRY, not at mint. Deep-freezing at mint
        already builds fresh objects, so it MASKS a missing snapshot in any
        single-threaded probe -- both the "no snapshot" and "shallow copy"
        mutants survived every other test in this class. What freezing at
        mint cannot do is defend the window between the checks and the
        capture: it copies whatever the graph has become by then.

        _read_canonical_workspace_spec_bytes runs after the schema check and
        before the operation loop, so patching it to mutate the caller's
        object reproduces that window deterministically, without threads."""
        op = self._venv_op()
        plan = self._plan([op], plan_id="mp_race")
        expected = compute_plan_hash(plan)
        original = spec_apply._read_canonical_workspace_spec_bytes

        def mutate_then_read(workspace_root):
            op["dest_path"] = "../../escape_during_validation"
            op["secret"] = "RACE_SECRET"
            return original(workspace_root)

        with patch.object(spec_apply, "_read_canonical_workspace_spec_bytes", mutate_then_read):
            validated = validate_materialization_plan(self.workspace_root, plan)

        self.assertEqual(
            validated.plan_hash,
            expected,
            "a mutation landing mid-validation must not reach the captured bytes",
        )
        path = write_materialization_receipt(
            self.workspace_root, validated, [{"kind": "venv"}]
        )
        self.assertNotIn("RACE_SECRET", path.read_text())

    def test_captured_hash_matches_the_validated_bytes(self):
        plan = self._plan([self._venv_op()], plan_id="mp_captured")
        expected = compute_plan_hash(plan)
        validated = validate_materialization_plan(self.workspace_root, plan)
        self.assertEqual(validated.plan_hash, expected)

    def test_hash_helper_accepts_the_frozen_representation(self):
        """The freeze must not turn the public canonical-hash helper into a
        trap for the handlers that will hold these plans in B/C/D."""
        plan = self._plan([self._venv_op()], plan_id="mp_helper")
        validated = validate_materialization_plan(self.workspace_root, plan)
        self.assertEqual(compute_plan_hash(validated.plan), compute_plan_hash(plan))


class TestCapabilityIsContentBound(PlanContractTestBase):
    """Sentinel's converging seam: a capability keyed on OBJECT IDENTITY (an
    opaque sentinel token) is copyable. `dataclasses.replace` re-invokes
    __init__ with the existing field values, so the real token rides into a
    modified shell -- a laundered "validated" object that binds facts nobody
    validated.

    Atlas's seam and this one are one design truth: the capability must bind
    to immutable CONTENT, not to identity or to a field that copies. The
    seal is therefore derived from the snapshot's canonical hash and every
    fact published alongside it, and verified at every use rather than only
    at construction."""

    def _valid(self, plan_id: str = "mp_seal"):
        return validate_materialization_plan(
            self.workspace_root, self._plan([self._venv_op()], plan_id=plan_id)
        )

    def test_replace_cannot_launder_the_capability(self):
        """Sentinel's witness 1, verbatim: replace() preserved the real token
        and publication used the altered plan_id, writing `.grip/escaped.json`
        outside the receipt directory."""
        validated = self._valid()
        with self.assertRaises(MaterializationPlanError) as ctx:
            dataclasses.replace(validated, plan_id="../../escaped")
        self.assertIn("disagree with the plan snapshot", str(ctx.exception))
        self.assertFalse((self.workspace_root / ".grip" / "escaped.json").exists())

    def test_replace_of_any_published_fact_is_rejected(self):
        """plan_id is the field with the escape, but every fact the receipt
        attests to has to be bound, or the next one becomes the seam.

        Each row names the layer that must reject it. The three layers shadow
        each other -- the snapshot-agreement check fires before the seal for
        every field it covers -- so asserting only "some capability error"
        would let a removed layer hide behind the one beneath it. That is the
        same shadowing Sentinel flagged for the pinned schema."""
        validated = self._valid()
        for field, value, reason in (
            ("plan_id", "mp_other", "disagree with the plan snapshot"),
            ("unit_key", "u_other", "disagree with the plan snapshot"),
            ("schema_version", 2, "disagree with the plan snapshot"),
            ("workspace_spec_sha256", "f" * 64, "disagree with the plan snapshot"),
            ("operation_kinds", ("clone",), "disagree with the plan snapshot"),
            ("plan_hash", "0" * 64, "does not describe its plan snapshot"),
        ):
            with self.subTest(field=field):
                with self.assertRaises(MaterializationPlanError) as ctx:
                    dataclasses.replace(validated, **{field: value})
                self.assertIn(reason, str(ctx.exception))

    def test_publication_rejects_a_capability_altered_in_place(self):
        """frozen=True blocks __setattr__, not object.__setattr__. Verifying
        at USE, not only at construction, is what makes the binding hold
        against an object that was already minted."""
        validated = self._valid(plan_id="mp_inplace")
        object.__setattr__(validated, "plan_id", "../../escaped")
        with self.assertRaises(MaterializationPlanError) as ctx:
            write_materialization_receipt(
                self.workspace_root, validated, [{"kind": "venv"}]
            )
        self.assertIn("disagree with the plan snapshot", str(ctx.exception))
        self.assertFalse((self.workspace_root / ".grip" / "escaped.json").exists())

    def test_publication_rejects_a_swapped_plan_snapshot(self):
        """The snapshot itself is a published fact: plan_hash attests to it,
        so swapping the graph must invalidate the seal too."""
        validated = self._valid(plan_id="mp_swap")
        other = validate_materialization_plan(
            self.workspace_root,
            self._plan([self._venv_op(dest_path="units/u2/.venv")], plan_id="mp_swap"),
        )
        object.__setattr__(validated, "plan", other.plan)
        with self.assertRaises(MaterializationPlanError) as ctx:
            write_materialization_receipt(
                self.workspace_root, validated, [{"kind": "venv"}]
            )
        self.assertIn("does not describe its plan snapshot", str(ctx.exception))

    def test_the_seal_itself_rejects_a_self_consistent_forgery(self):
        """The layer that has no shadow above it. Snapshot agreement and hash
        agreement both hold here, so only the seal can refuse -- which is the
        test that keeps the seal from being dead code."""
        genuine = self._valid(plan_id="mp_sealonly")
        object.__setattr__(genuine, "_seal", "0" * 64)
        with self.assertRaises(MaterializationPlanError) as ctx:
            write_materialization_receipt(
                self.workspace_root, genuine, [{"kind": "venv"}]
            )
        self.assertIn("not minted by", str(ctx.exception))

    def test_atlas_forge_family_cannot_publish(self):
        """Atlas's forge family verbatim: he published a receipt claiming an
        all-zero plan hash and `clone` evidence from a validated `venv` plan,
        and escaped the receipt directory via plan_id. Every vector he named
        gets a row, asserting no filesystem publication occurred."""
        validated = self._valid(plan_id="mp_forge_family")
        for field, value in (
            ("plan_id", "../../escaped"),
            ("unit_key", "u_forged"),
            ("workspace_spec_sha256", "f" * 64),
            ("plan_hash", "0" * 64),
            ("operation_kinds", ("clone",)),
        ):
            with self.subTest(field=field):
                with self.assertRaises(MaterializationPlanError):
                    forged = dataclasses.replace(validated, **{field: value})
                    write_materialization_receipt(
                        self.workspace_root, forged, [{"kind": "venv"}]
                    )
        self.assertFalse((self.workspace_root / ".grip" / "escaped.json").exists())
        state = self.workspace_root / ".grip" / "state" / "materialization"
        self.assertEqual(
            [p.name for p in state.glob("*.json")] if state.exists() else [], []
        )

    def test_copying_the_capability_does_not_preserve_authority(self):
        """The layer content-checking cannot reach. A copy has identical
        fields and therefore an identical, genuinely valid seal -- only
        ORIGIN can refuse it. Atlas's closure says copying must not preserve
        authority, so this is its own layer with its own probe."""
        validated = self._valid(plan_id="mp_copy")

        # A shallow copy IS constructible and carries a genuinely valid seal,
        # so provenance is the only thing that can refuse it.
        with self.assertRaises(MaterializationPlanError) as ctx:
            write_materialization_receipt(
                self.workspace_root, copy.copy(validated), [{"kind": "venv"}]
            )
        self.assertIn("not one this validator", str(ctx.exception))

        # A deep copy cannot be produced at all: the frozen snapshot is not
        # picklable, so the clone never comes into existence. Different
        # mechanism, same guarantee -- asserted separately rather than folded
        # in, because a shared assertion would hide which one actually holds.
        with self.assertRaises(TypeError):
            copy.deepcopy(validated)

    def test_provenance_rejects_a_correctly_sealed_hand_built_shell(self):
        """Pinning provenance at its OWN level: this shell carries a real
        seal, computed with the production helper, so every content check
        passes. If provenance were removed, nothing else would refuse it."""
        genuine = self._valid(plan_id="mp_prov")
        facts = {
            "plan_hash": genuine.plan_hash,
            "plan_id": genuine.plan_id,
            "unit_key": genuine.unit_key,
            "schema_version": genuine.schema_version,
            "workspace_spec_sha256": genuine.workspace_spec_sha256,
            "operation_kinds": genuine.operation_kinds,
        }
        shell = spec_apply.ValidatedPlan(
            plan=genuine.plan, _seal=spec_apply._capability_seal(**facts), **facts
        )
        with self.assertRaises(MaterializationPlanError) as ctx:
            write_materialization_receipt(
                self.workspace_root, shell, [{"kind": "venv"}]
            )
        self.assertIn("not one this validator", str(ctx.exception))

    def test_a_genuine_capability_still_publishes(self):
        """The seal must not be so strict it rejects the real thing."""
        validated = self._valid(plan_id="mp_genuine")
        path = write_materialization_receipt(
            self.workspace_root, validated, [{"kind": "venv"}]
        )
        self.assertTrue(path.exists())
        self.assertEqual(json.loads(path.read_text())["plan_id"], "mp_genuine")


class TestConsumptionIsSnapshotBound(PlanContractTestBase):
    """Atlas's use-time TOCTOU. It survived two heads, which is the signal
    that the model was wrong rather than a guard missing: I had been fixing
    the capability so it could not be *forged*, while the writer went on
    reading the live shell AFTER verification, across a caller-controlled
    callback.

    The content seal is what made this look covered -- it gates content, so
    tampering could not change the sealed facts. But nothing gated the READS.
    That is the adjacent-strong-guard masking class again, this time in the
    fix rather than in the test.

    Closure is Atlas's, exactly: one verified consumption yields an immutable
    local binding derived from the sealed snapshot, and every later evidence
    check, receipt field, filename and temp filename consumes only that."""

    def _victim(self, plan_id: str):
        return validate_materialization_plan(
            self.workspace_root, self._plan([self._venv_op()], plan_id=plan_id)
        )

    def test_mutation_between_verify_and_use_cannot_move_the_receipt(self):
        """Atlas's callback witness, verbatim. It published
        changed_after_verify.json carrying a changed identity beside the
        original hash -- one receipt describing two different states."""
        victim = self._victim("mp_toctou")

        class MutatingEvidence(list):
            def __iter__(inner):
                object.__setattr__(victim, "plan_id", "changed_after_verify")
                return super().__iter__()

        path = write_materialization_receipt(
            self.workspace_root, victim, MutatingEvidence([{"kind": "venv"}])
        )
        receipt = json.loads(path.read_text())

        state_dir = self.workspace_root / ".grip" / "state" / "materialization"
        self.assertFalse((state_dir / "changed_after_verify.json").exists())
        self.assertEqual(path.name, "mp_toctou.json")
        self.assertEqual(receipt["plan_id"], "mp_toctou")
        self.assertEqual(receipt["plan_hash"], victim.plan_hash)

    def test_temp_filename_also_comes_from_the_binding(self):
        """The temp filename is derived from the same identity, so it has to
        come from the binding too -- otherwise the window merely moves.

        It cannot be checked by listing the directory afterwards: the rename
        CONSUMES the temp file, so a post-hoc listing shows the same thing
        whether or not the name was attacker-influenced. That is why this
        mutant survived a directory assertion. Observing the transient name
        at os.replace is the only probe that can see it.

        It matters even though the temp stays inside the receipt directory:
        a caller-chosen temp name can collide with another in-flight
        publication's temp and have it unlinked on the failure path."""
        victim = self._victim("mp_tmp")
        seen: dict[str, str] = {}
        original_replace = os.replace

        def tracking_replace(src, dst, **kwargs):
            seen["src"] = Path(src).name
            seen["dst"] = Path(dst).name
            return original_replace(src, dst, **kwargs)

        class MutatingEvidence(list):
            def __iter__(inner):
                object.__setattr__(victim, "plan_id", "escaped_tmp")
                return super().__iter__()

        with patch.object(spec_apply.os, "replace", tracking_replace):
            write_materialization_receipt(
                self.workspace_root, victim, MutatingEvidence([{"kind": "venv"}])
            )

        self.assertTrue(seen["src"].startswith("mp_tmp.json.tmp-"), seen)
        self.assertEqual(seen["dst"], "mp_tmp.json")

    def test_evidence_is_read_once_so_it_cannot_screen_clean_and_persist_dirty(self):
        """The other input, same class. The evidence list was walked TWICE --
        once by the screen and once by json serialization -- so an object
        returning different contents per iteration passed the identity screen
        and then persisted `secret` into the receipt anyway.

        That is a boundary failure, not just a correctness one: the
        receipt's whole claim is that it carries no identity-bearing content.
        Not in Atlas's witness; found by asking what else is read after it is
        checked."""
        victim = self._victim("mp_twoface")

        class TwoFaced(list):
            calls = 0

            def __iter__(inner):
                TwoFaced.calls += 1
                if TwoFaced.calls == 1:
                    return iter([{"kind": "venv"}])
                return iter([{"kind": "venv", "secret": "LEAKED"}])

        path = write_materialization_receipt(
            self.workspace_root, victim, TwoFaced([{"kind": "venv"}])
        )
        body = path.read_text()
        self.assertNotIn("LEAKED", body)
        self.assertEqual(json.loads(body)["operations"], [{"kind": "venv"}])

    def test_evidence_screening_sees_what_gets_persisted(self):
        """The positive face: a two-faced object that is DIRTY on its first
        read must be rejected, proving the screen inspects the same
        materialized value the receipt persists rather than a fresh walk."""
        victim = self._victim("mp_dirtyfirst")

        class DirtyFirst(list):
            def __iter__(inner):
                return iter([{"kind": "venv", "agent_id": "a-1"}])

        with self.assertRaises(MaterializationPlanError) as ctx:
            write_materialization_receipt(
                self.workspace_root, victim, DirtyFirst([{"kind": "venv"}])
            )
        self.assertIn("identity-bearing", str(ctx.exception))
        self.assertFalse(
            materialization_receipt_path(self.workspace_root, "mp_dirtyfirst").exists()
        )


class TestDurabilityFailureMatrix(PlanContractTestBase):
    """Durability is a FAILURE-PATH contract, not only
    an ordering one. The prior failure test injected only at the FIRST
    fsync; letting the file fsync succeed and failing the parent-directory
    fsync left a plausible final receipt published at its path."""

    def _validated(self, plan_id: str = "mp_dur"):
        return validate_materialization_plan(
            self.workspace_root, self._plan([self._venv_op()], plan_id=plan_id)
        )

    def _evidence(self):
        return [{"kind": "venv"}]

    def test_failure_at_file_fsync_publishes_nothing(self):
        with patch.object(spec_apply.os, "fsync", side_effect=OSError("file fsync failed")):
            with self.assertRaises(OSError):
                write_materialization_receipt(self.workspace_root, self._validated(), self._evidence())
        self.assertFalse(materialization_receipt_path(self.workspace_root, "mp_dur").exists())

    def test_failure_at_parent_fsync_invalidates_the_receipt(self):
        """The exact survivor: fsync_calls=2 and final_receipt_exists=True.
        A publication that cannot be made durable must not remain
        published, or a caller doing destructive cleanup on the strength of
        a receipt acts on one that may vanish."""
        calls = {"n": 0}
        original_fsync = os.fsync

        def fail_second_fsync(fd):
            calls["n"] += 1
            if calls["n"] >= 2:
                raise OSError("parent-directory fsync failed")
            return original_fsync(fd)

        with patch.object(spec_apply.os, "fsync", fail_second_fsync):
            with self.assertRaises(OSError):
                write_materialization_receipt(self.workspace_root, self._validated(), self._evidence())
        self.assertEqual(calls["n"], 2)
        self.assertFalse(
            materialization_receipt_path(self.workspace_root, "mp_dur").exists(),
            "a receipt that could not be made durable must not remain published",
        )

    def test_failure_at_replace_publishes_nothing_and_leaves_no_residue(self):
        original_replace = os.replace

        def failing_replace(src, dst, **kwargs):
            if "materialization" in str(src):
                raise OSError("replace failed")
            return original_replace(src, dst, **kwargs)

        with patch.object(spec_apply.os, "replace", failing_replace):
            with self.assertRaises(OSError):
                write_materialization_receipt(self.workspace_root, self._validated(), self._evidence())
        state_dir = self.workspace_root / ".grip" / "state" / "materialization"
        self.assertFalse(materialization_receipt_path(self.workspace_root, "mp_dur").exists())
        self.assertEqual(list(state_dir.glob("*.tmp-*")), [])

    def test_successful_publication_is_a_regular_in_root_file(self):
        """The positive face: assert the published receipt
        is a regular file inside the team root, not merely that something
        exists at the path."""
        path = write_materialization_receipt(self.workspace_root, self._validated(), self._evidence())
        self.assertTrue(stat.S_ISREG(os.lstat(path).st_mode))
        self.assertIn(self.workspace_root.resolve(), path.resolve().parents)

    def test_symlinked_state_directory_rejected(self):
        """Sentinel's exact fixture: `.grip/state` itself (not the
        materialization leaf) as a symlink to an outside directory."""
        outside = self.tmp / "outside_state"
        outside.mkdir()
        grip = self.workspace_root / ".grip"
        grip.mkdir(parents=True, exist_ok=True)
        (grip / "state").symlink_to(outside)

        validated = self._validated(plan_id="mp_state")
        with self.assertRaises(MaterializationPlanError) as ctx:
            write_materialization_receipt(self.workspace_root, validated, self._evidence())
        self.assertIn("symlink", str(ctx.exception))
        self.assertEqual(list(outside.rglob("*")), [])


if __name__ == "__main__":
    unittest.main()
