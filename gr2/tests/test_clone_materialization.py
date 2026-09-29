"""Clone/cache/alternates executor tests for the neutral MaterializationPlan (S4-B).

S4-A landed the plan CONTRACT (validate -> capability -> receipt) and nothing
consumed it. This is the first operation EXECUTOR: `kind == "clone"`.

Contract: MaterializationPlan v1 clone contract and acceptance fruit 6/7/8/9.

Testing discipline carried forward from the S4-A review cycle, and applied at
DESIGN time rather than at test time: for every guard, ask what ELSE would
reject this input first, and is the guard therefore untested at its own level.
Five masking pairs came out of that question before any implementation existed,
and each one dictates the shape of a probe below:

  1. Zero-byte alternates is masked by the entries-match-cache guard whenever
     that guard is spelled `all(entry == declared)` -- vacuously true over zero
     lines. Set-equality, never all().
  2. Cache-root containment is masked by S4-A's canonicalize_workspace_path,
     which rejects anything OUTSIDE the workspace. An out-of-workspace decoy
     dies upstream and this guard never runs.
  3. Cache-root containment is also masked by the origin-match guard, because
     another unit's clone of the same repository has the SAME origin url.
  4. Cache-root containment is also masked by a bare-repository check, because
     another unit's clone is not bare.
     => 2+3+4 combine: the only decoy that reaches containment is a BARE mirror
        with the SAME origin url at an IN-workspace NON-cache path.
  5. `.git is a directory` is masked by the existing gitops.is_git_repo(), which
     returns True inside a linked worktree. test_is_git_repo_cannot_see_the_
     difference pins that fact so the mask cannot silently return.

Two behaviours of git itself are load-bearing here and are pinned by tests
rather than assumed, because both were verified empirically and neither is
obvious from the flag name:

  - `--reference-if-able` SILENTLY DEGRADES: against a missing cache it exits 0
    and writes no alternates file at all. A declared reference is therefore a
    claim the executor must verify positively after the fact.
  - `git rev-parse --git-common-dir` returns a RELATIVE path (".git") in a
    normal clone and an ABSOLUTE one inside a worktree, so it must be resolved
    against the clone root before comparison.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from gr2.python_cli import gitops, spec_apply
from gr2.python_cli.clone_exec import (
    CloneExecutionError,
    execute_clone_operation,
    verify_cache_provenance,
    verify_clone_isolation,
)
from gr2.python_cli.spec_apply import (
    validate_materialization_plan,
    workspace_spec_path,
    write_materialization_receipt,
)


def run_git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=False
    )
    if proc.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed in {cwd}:\n{proc.stderr}")
    return proc.stdout


class CloneExecTestBase(unittest.TestCase):
    """Real git repositories on local paths. No network, no fixtures-of-fixtures:
    the guards under test read actual .git state, so simulating that state would
    be testing the simulation."""

    def setUp(self):
        import tempfile

        self.tmp = Path(tempfile.mkdtemp())
        self.workspace_root = self.tmp / "workspace"
        self.workspace_root.mkdir(parents=True)
        self.spec_sha256 = self._write_workspace_spec()

        # Upstream: a real repository with one commit on `main`.
        self.origin = self.tmp / "origin"
        self.origin.mkdir()
        run_git(self.origin, "init", "-q", "-b", "main")
        run_git(self.origin, "config", "user.email", "t@example.com")
        run_git(self.origin, "config", "user.name", "T")
        (self.origin / "README.md").write_text("hello\n")
        run_git(self.origin, "add", ".")
        run_git(self.origin, "commit", "-q", "-m", "init")
        self.repo_url = str(self.origin)

        # The workspace-managed object cache (section 8.2).
        self.cache_rel = ".grip/cache/repos/product.git"
        self.cache_path = self.workspace_root / ".grip" / "cache" / "repos" / "product.git"
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        run_git(self.tmp, "clone", "-q", "--mirror", self.repo_url, str(self.cache_path))

        self.dest_rel = "units/u_test/repos/product"

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_workspace_spec(self, content: str = 'workspace_name = "test"\n') -> str:
        spec_path = workspace_spec_path(self.workspace_root)
        spec_path.parent.mkdir(parents=True, exist_ok=True)
        spec_path.write_text(content)
        return hashlib.sha256(spec_path.read_bytes()).hexdigest()

    def _clone_op(self, **overrides: object) -> dict[str, object]:
        op: dict[str, object] = {
            "kind": "clone",
            "repo_url": self.repo_url,
            "dest_path": self.dest_rel,
            "branch": "main",
            "reference_base": self.cache_rel,
        }
        op.update(overrides)
        return op

    def _validated(self, operations: list[dict[str, object]] | None = None):
        plan = {
            "schema_version": 1,
            "plan_id": "mp_test",
            "unit_key": "u_test",
            "workspace_spec_sha256": self.spec_sha256,
            "operations": operations if operations is not None else [self._clone_op()],
        }
        return validate_materialization_plan(self.workspace_root, plan)

    def _make_clone(self, dest: Path, *, reference: Path | None = None) -> Path:
        """A real clone, the way the executor would make one."""
        command = ["git", "clone", "-q"]
        if reference is not None:
            command.extend(["--reference-if-able", str(reference)])
        command.extend([self.repo_url, str(dest)])
        proc = subprocess.run(command, capture_output=True, text=True, check=False)
        if proc.returncode != 0:
            raise AssertionError(f"fixture clone failed:\n{proc.stderr}")
        return dest

    def _healthy_clone(self) -> Path:
        dest = self.tmp / "healthy"
        return self._make_clone(dest, reference=self.cache_path)

    def _alternates_path(self, clone_root: Path) -> Path:
        return clone_root / ".git" / "objects" / "info" / "alternates"

    def _verify(self, clone_root: Path, **overrides: object) -> None:
        kwargs: dict[str, object] = {
            "workspace_root": self.workspace_root,
            "repo_url": self.repo_url,
            "reference_base": self.cache_path,
        }
        kwargs.update(overrides)
        verify_clone_isolation(clone_root, **kwargs)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# The adjacent-guard facts these probes depend on. If git or the helper ever
# changes, these fail FIRST and name the reason, instead of quietly turning a
# guard's probe into a test of its neighbour.
# ---------------------------------------------------------------------------


class TestMaskingPremises(CloneExecTestBase):
    def test_reference_if_able_silently_degrades(self):
        """The flag is chosen by section 8.2 so a missing cache does not fail the
        timed path. The cost is that a declared reference can vanish with no
        error: rc=0, and no alternates file at all. This is WHY verification
        must be positive rather than 'we passed the flag'."""
        dest = self.tmp / "degraded"
        proc = subprocess.run(
            [
                "git", "clone", "-q",
                "--reference-if-able", str(self.tmp / "does-not-exist.git"),
                self.repo_url, str(dest),
            ],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertFalse(
            self._alternates_path(dest).exists(),
            "git wrote an alternates file for a missing reference -- the "
            "silent-degrade premise behind the positive-verification guards "
            "no longer holds",
        )

    def test_is_git_repo_cannot_see_a_worktree(self):
        """gitops.is_git_repo() answers 'is-inside-work-tree', which is TRUE
        inside a linked worktree. Any health check gated on it reads a
        section-8.1-forbidden worktree as a healthy clone. Pinned so the mask
        cannot return unnoticed."""
        host = self._healthy_clone()
        wt = self.tmp / "linked-worktree"
        run_git(host, "worktree", "add", "-q", str(wt), "-b", "side")

        self.assertTrue(
            gitops.is_git_repo(wt),
            "is_git_repo no longer accepts a worktree; the .git-is-a-directory "
            "guard may now be masked by a different neighbour -- re-run the "
            "masking analysis",
        )
        self.assertTrue((wt / ".git").is_file(), "worktree .git should be a pointer file")


# ---------------------------------------------------------------------------
# Clone isolation (section 8.1) and alternates binding (section 8.2), each
# guard probed at its own level on a real clone.
# ---------------------------------------------------------------------------


class TestCloneIsolation(CloneExecTestBase):
    def test_healthy_clone_with_declared_reference_passes(self):
        """The guards must not false-positive on the shape they exist to admit."""
        self._verify(self._healthy_clone())

    def test_git_dir_as_pointer_file_is_rejected(self):
        """Acceptance fruit 8. The probe is a REAL worktree, not a hand-written
        pointer file, because the mask being closed is that a real worktree
        looks healthy to is_git_repo()."""
        host = self._healthy_clone()
        wt = self.tmp / "as-worktree"
        run_git(host, "worktree", "add", "-q", str(wt), "-b", "side")

        with self.assertRaises(CloneExecutionError) as ctx:
            self._verify(wt)
        self.assertIn(".git must be a directory", str(ctx.exception))

    def test_common_dir_outside_the_clone_is_rejected(self):
        """.git stays a real directory, so the pointer-file guard passes. Only
        the common-dir guard can see this one."""
        clone = self._healthy_clone()
        foreign = self._make_clone(self.tmp / "foreign", reference=self.cache_path)
        (clone / ".git" / "commondir").write_text(f"{foreign / '.git'}\n")

        with self.assertRaises(CloneExecutionError) as ctx:
            self._verify(clone)
        self.assertIn("git common directory", str(ctx.exception))

    def test_clone_hosting_a_worktree_is_rejected(self):
        """This clone's OWN state is fine -- .git is a directory and the common
        dir is its own -- so neither neighbour fires. Section 8.1 forbids
        .git/worktrees because a host shares refs and locks with its links."""
        clone = self._healthy_clone()
        run_git(clone, "worktree", "add", "-q", str(self.tmp / "linked"), "-b", "side")

        with self.assertRaises(CloneExecutionError) as ctx:
            self._verify(clone)
        self.assertIn("worktrees", str(ctx.exception))

    def test_origin_url_mismatch_is_rejected(self):
        clone = self._healthy_clone()
        run_git(clone, "remote", "set-url", "origin", str(self.tmp / "somewhere-else"))

        with self.assertRaises(CloneExecutionError) as ctx:
            self._verify(clone)
        self.assertIn("origin", str(ctx.exception))


class TestAlternatesBinding(CloneExecTestBase):
    def test_zero_byte_alternates_does_not_satisfy_a_declared_reference(self):
        """Sentinel's survivor. An empty file makes 'every entry is the declared
        cache' VACUOUSLY TRUE, so an all()-shaped guard admits it while the
        clone has no object sharing at all -- the perf lever silently absent
        and the contract silently unmet."""
        clone = self._healthy_clone()
        self._alternates_path(clone).write_text("")

        with self.assertRaises(CloneExecutionError) as ctx:
            self._verify(clone)
        self.assertIn("declares no alternate", str(ctx.exception))

    def test_whitespace_only_alternates_does_not_satisfy_a_declared_reference(self):
        """Same vacuous-truth class as zero-byte, one layer up: a guard that
        strips blank lines before comparing sets is equally empty, and a guard
        that only checks st_size > 0 admits this one."""
        clone = self._healthy_clone()
        self._alternates_path(clone).write_text("\n   \n\n")

        with self.assertRaises(CloneExecutionError) as ctx:
            self._verify(clone)
        self.assertIn("declares no alternate", str(ctx.exception))

    def test_missing_alternates_file_does_not_satisfy_a_declared_reference(self):
        """Distinct INPUT from zero-byte, reaching a distinct code path: absent
        raises FileNotFoundError where empty returns "". This is the state
        --reference-if-able actually produces when the cache is missing."""
        clone = self._healthy_clone()
        self._alternates_path(clone).unlink()

        with self.assertRaises(CloneExecutionError) as ctx:
            self._verify(clone)
        self.assertIn("declares no alternate", str(ctx.exception))

    def test_foreign_alternate_entry_alongside_the_cache_is_rejected(self):
        """The declared cache IS present, so a guard asking 'is the cache in
        there?' passes. Only set-equality sees the extra entry."""
        clone = self._healthy_clone()
        rogue = self.tmp / "rogue.git"
        run_git(self.tmp, "clone", "-q", "--mirror", self.repo_url, str(rogue))
        path = self._alternates_path(clone)
        path.write_text(path.read_text().rstrip("\n") + f"\n{rogue / 'objects'}\n")

        with self.assertRaises(CloneExecutionError) as ctx:
            self._verify(clone)
        self.assertIn("alternate", str(ctx.exception))

    def test_alternate_to_a_non_cache_path_is_rejected(self):
        """Acceptance fruit 9, with the decoy the masking analysis demands: a
        BARE mirror (survives a bare-repo check) with the SAME origin url
        (survives origin-match) at an IN-workspace path (survives S4-A's
        canonicalizer). Only cache-root containment can reject it."""
        decoy = self.workspace_root / "units" / "u_other" / "rogue.git"
        decoy.parent.mkdir(parents=True, exist_ok=True)
        run_git(self.tmp, "clone", "-q", "--mirror", self.repo_url, str(decoy))
        self.assertTrue(gitops.is_git_dir(decoy), "decoy must be bare to reach containment")
        self.assertEqual(
            gitops.remote_origin_url(decoy),
            self.repo_url,
            "decoy must share the declared origin to reach containment",
        )

        clone = self._make_clone(self.tmp / "referencing-decoy", reference=decoy)
        with self.assertRaises(CloneExecutionError) as ctx:
            self._verify(clone, reference_base=decoy)
        self.assertIn("object cache", str(ctx.exception))

    def test_undeclared_alternate_is_rejected(self):
        """The inverse direction: the plan declares no reference, so there is
        nothing to compare against -- and an unguarded 'no reference declared'
        branch would skip the check entirely and let the clone share objects
        with anything."""
        rogue = self.tmp / "undeclared.git"
        run_git(self.tmp, "clone", "-q", "--mirror", self.repo_url, str(rogue))
        clone = self._make_clone(self.tmp / "undeclared-alt", reference=rogue)

        with self.assertRaises(CloneExecutionError) as ctx:
            self._verify(clone, reference_base=None)
        self.assertIn("alternate", str(ctx.exception))

    def test_no_reference_declared_and_none_present_passes(self):
        self._verify(self._make_clone(self.tmp / "standalone"), reference_base=None)


class TestCacheProvenance(CloneExecTestBase):
    def test_declared_cache_passes(self):
        verify_cache_provenance(
            self.cache_path, workspace_root=self.workspace_root, repo_url=self.repo_url
        )

    def test_cache_seeded_from_a_different_upstream_is_rejected(self):
        """Containment passes -- it IS under the cache root. Only provenance
        sees that its objects came from somewhere else."""
        other = self.tmp / "other-origin"
        other.mkdir()
        run_git(other, "init", "-q", "-b", "main")
        run_git(other, "config", "user.email", "t@example.com")
        run_git(other, "config", "user.name", "T")
        (other / "OTHER.md").write_text("other\n")
        run_git(other, "add", ".")
        run_git(other, "commit", "-q", "-m", "other")
        run_git(self.cache_path, "remote", "set-url", "origin", str(other))

        with self.assertRaises(CloneExecutionError) as ctx:
            verify_cache_provenance(
                self.cache_path, workspace_root=self.workspace_root, repo_url=self.repo_url
            )
        self.assertIn("seeded from", str(ctx.exception))

    def test_non_bare_cache_is_rejected(self):
        not_bare = self.workspace_root / ".grip" / "cache" / "repos" / "notbare.git"
        self._make_clone(not_bare)

        with self.assertRaises(CloneExecutionError) as ctx:
            verify_cache_provenance(
                not_bare, workspace_root=self.workspace_root, repo_url=self.repo_url
            )
        self.assertIn("bare", str(ctx.exception))

    def test_cache_outside_the_cache_root_is_rejected(self):
        """Same decoy discipline as the alternates probe: bare, same origin,
        in-workspace. Containment is the only guard left standing."""
        decoy = self.workspace_root / "units" / "u_other" / "rogue.git"
        decoy.parent.mkdir(parents=True, exist_ok=True)
        run_git(self.tmp, "clone", "-q", "--mirror", self.repo_url, str(decoy))

        with self.assertRaises(CloneExecutionError) as ctx:
            verify_cache_provenance(
                decoy, workspace_root=self.workspace_root, repo_url=self.repo_url
            )
        self.assertIn("object cache", str(ctx.exception))

    def test_missing_cache_is_rejected(self):
        with self.assertRaises(CloneExecutionError) as ctx:
            verify_cache_provenance(
                self.workspace_root / ".grip" / "cache" / "repos" / "absent.git",
                workspace_root=self.workspace_root,
                repo_url=self.repo_url,
            )
        self.assertIn("does not exist", str(ctx.exception))


# ---------------------------------------------------------------------------
# Publication (section 8.3) and reuse, through the executor itself.
# ---------------------------------------------------------------------------


class TestClonePublication(CloneExecTestBase):
    def test_publishes_a_referenced_clone_on_the_declared_branch(self):
        validated = self._validated()
        evidence = execute_clone_operation(validated, 0, workspace_root=self.workspace_root)

        dest = self.workspace_root / self.dest_rel
        self.assertTrue((dest / ".git").is_dir(), "published clone must own its git dir")
        self.assertEqual(gitops.current_branch(dest), "main")
        self.assertEqual(gitops.remote_origin_url(dest), self.repo_url)
        self.assertEqual(
            self._alternates_path(dest).read_text().split(),
            [str((self.cache_path / "objects").resolve())],
            "the declared cache must be the clone's only alternate",
        )
        self.assertEqual(evidence["kind"], "clone")
        self.assertIs(evidence["reused"], False)

    def test_no_staging_path_survives_a_successful_publish(self):
        validated = self._validated()
        execute_clone_operation(validated, 0, workspace_root=self.workspace_root)

        dest = self.workspace_root / self.dest_rel
        siblings = [p.name for p in dest.parent.iterdir()]
        self.assertEqual(siblings, [dest.name], f"staging leaked: {siblings}")

    def test_an_invalid_cache_is_refused_before_any_clone_runs(self):
        """Early refusal: no work at all against a cache we would reject
        afterwards. Note this path never reaches staging, which is precisely
        why it cannot double as the publication-ordering probe below."""
        self.cache_path.rename(self.tmp / "cache-moved-away")

        validated = self._validated()
        with self.assertRaises(CloneExecutionError) as ctx:
            execute_clone_operation(validated, 0, workspace_root=self.workspace_root)
        self.assertIn("does not exist", str(ctx.exception))
        self.assertFalse((self.workspace_root / self.dest_rel).exists())

    def test_a_clone_failing_verification_is_never_published(self):
        """Publication happens only AFTER staging verifies. A half-verified
        clone at dest_path is worse than none, because the reuse path would
        later find it and call it healthy.

        The failure is injected rather than provoked through a bad cache: every
        naturally-bad cache is refused earlier, so the staging path would go
        unprobed and an executor that cloned straight to dest_path would still
        pass. The probe has to reach the code it is named for.

        The load-bearing assertion is what is true DURING the failure, not
        after it. An executor that clones straight to dest_path and deletes it
        on failure reaches an identical end state -- nothing at dest_path --
        so an end-state assertion cannot tell the two apart. It is the
        transient that differs, and the transient is what a crash freezes: an
        unverified clone left at the canonical path, which the reuse path will
        later find and call healthy."""
        from unittest.mock import patch

        from gr2.python_cli import clone_exec

        dest = self.workspace_root / self.dest_rel
        observed: dict[str, object] = {}

        def failing_verify(clone_root, **kwargs):
            observed["dest_existed_during_verify"] = dest.exists()
            observed["verified_path"] = Path(clone_root)
            raise CloneExecutionError("injected staging failure")

        with patch.object(clone_exec, "verify_clone_isolation", failing_verify):
            validated = self._validated()
            with self.assertRaises(CloneExecutionError):
                execute_clone_operation(validated, 0, workspace_root=self.workspace_root)

        self.assertIs(
            observed["dest_existed_during_verify"],
            False,
            "an unverified clone occupied dest_path while it was still being verified; "
            "a crash at that moment leaves it there permanently",
        )
        self.assertNotEqual(
            observed["verified_path"], dest, "verification ran on the published path"
        )
        self.assertFalse(dest.exists(), "an unverified clone was published at dest_path")
        self.assertEqual(
            list(dest.parent.iterdir()) if dest.parent.exists() else [],
            [],
            "staging survived a failed publish",
        )

    def test_the_declared_cache_listed_twice_is_still_the_declared_cache(self):
        """Duplicate entries name the same object store, and git tolerates
        them. Pinning this keeps the permitted-set comparison about WHICH
        stores are reachable rather than about the file's exact bytes."""
        clone = self._healthy_clone()
        path = self._alternates_path(clone)
        entry = path.read_text().strip()
        path.write_text(f"{entry}\n{entry}\n")

        self._verify(clone)

    def test_evidence_is_receipt_shaped_and_identity_free(self):
        """The evidence has to survive S4-A's receipt screen unchanged; proving
        it here means the executor and the contract cannot drift apart
        silently."""
        validated = self._validated()
        evidence = execute_clone_operation(validated, 0, workspace_root=self.workspace_root)

        receipt_path = write_materialization_receipt(
            self.workspace_root, validated, [evidence]
        )
        import json

        receipt = json.loads(receipt_path.read_text())
        self.assertEqual(receipt["stage"], "MATERIALIZED")
        self.assertEqual(receipt["operations"][0]["kind"], "clone")


class TestExistingCloneHandling(CloneExecTestBase):
    def _publish_once(self) -> Path:
        validated = self._validated()
        execute_clone_operation(validated, 0, workspace_root=self.workspace_root)
        return self.workspace_root / self.dest_rel

    def test_healthy_existing_clone_is_reused(self):
        dest = self._publish_once()
        marker = dest / ".git" / "REUSE_MARKER"
        marker.write_text("x")

        validated = self._validated()
        evidence = execute_clone_operation(validated, 0, workspace_root=self.workspace_root)

        self.assertIs(evidence["reused"], True)
        self.assertTrue(marker.exists(), "reuse must not re-clone over the existing git dir")

    def test_dirty_existing_clone_blocks_and_is_not_reset(self):
        """Section 8.3: a dirty clone is NEVER reset or replaced. The assertion
        that matters is the survival of the operator's work, not the raise --
        a guard that raises AFTER discarding is still data loss."""
        dest = self._publish_once()
        (dest / "UNCOMMITTED.md").write_text("work in progress\n")

        validated = self._validated()
        with self.assertRaises(CloneExecutionError) as ctx:
            execute_clone_operation(validated, 0, workspace_root=self.workspace_root)

        self.assertIn("dirty", str(ctx.exception))
        self.assertTrue(
            (dest / "UNCOMMITTED.md").exists(),
            "uncommitted work was destroyed by a materialization run",
        )

    def test_existing_clone_of_a_different_repo_blocks(self):
        dest = self._publish_once()
        run_git(dest, "remote", "set-url", "origin", str(self.tmp / "elsewhere"))

        validated = self._validated()
        with self.assertRaises(CloneExecutionError) as ctx:
            execute_clone_operation(validated, 0, workspace_root=self.workspace_root)
        self.assertIn("origin", str(ctx.exception))

    def test_existing_worktree_at_dest_blocks(self):
        """The reuse path is exactly where the is_git_repo() mask would bite:
        a worktree parked at dest_path answers 'yes, healthy clone here'."""
        host = self._healthy_clone()
        dest = self.workspace_root / self.dest_rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        run_git(host, "worktree", "add", "-q", str(dest), "-b", "side")

        validated = self._validated()
        with self.assertRaises(CloneExecutionError) as ctx:
            execute_clone_operation(validated, 0, workspace_root=self.workspace_root)
        self.assertIn(".git must be a directory", str(ctx.exception))


class TestExecutorBinding(CloneExecTestBase):
    def test_executing_against_a_different_workspace_root_is_rejected(self):
        """S4-A binds the plan to a WorkspaceSpec at validation. The executor
        takes workspace_root as a separate argument, so nothing structurally
        stops a caller from validating against one workspace and executing
        against another -- every relative path in the plan would then resolve
        somewhere else. Re-checking the spec at USE is the same lesson S4-A's
        TOCTOU round ended on."""
        validated = self._validated()

        other_root = self.tmp / "other-workspace"
        other_root.mkdir()
        other_spec = workspace_spec_path(other_root)
        other_spec.parent.mkdir(parents=True, exist_ok=True)
        other_spec.write_text('workspace_name = "other"\n')

        with self.assertRaises(CloneExecutionError) as ctx:
            execute_clone_operation(validated, 0, workspace_root=other_root)
        self.assertIn("WorkspaceSpec", str(ctx.exception))

    def test_a_tampered_capability_cannot_execute(self):
        """frozen=True blocks __setattr__ but not object.__setattr__, so the
        capability must be verified at USE and not trusted because it exists."""
        validated = self._validated()
        object.__setattr__(validated, "plan_id", "mp_swapped")

        with self.assertRaises(Exception) as ctx:
            execute_clone_operation(validated, 0, workspace_root=self.workspace_root)
        self.assertIn("capability is invalid", str(ctx.exception))

    def test_a_copied_capability_cannot_execute(self):
        """S4-A layered content-sealing and validator-provenance separately
        because they catch different forgeries: a faithful copy preserves every
        field AND the seal, and only provenance can see it is not one the
        validator minted. The executor must ask for provenance, not just a
        valid seal -- nothing else in this module would notice if it stopped."""
        import dataclasses

        validated = self._validated()
        copied = dataclasses.replace(validated)

        with self.assertRaises(Exception) as ctx:
            execute_clone_operation(copied, 0, workspace_root=self.workspace_root)
        self.assertIn("capability is invalid", str(ctx.exception))

    def test_out_of_range_index_is_refused(self):
        validated = self._validated()
        with self.assertRaises(CloneExecutionError) as ctx:
            execute_clone_operation(validated, 7, workspace_root=self.workspace_root)
        self.assertIn("out of range", str(ctx.exception))

    def test_declared_non_default_branch_is_honoured(self):
        """Without this the branch field is unfalsifiable: every fixture branch
        equals the upstream default, so an executor that ignored `branch`
        entirely would still land on the right one."""
        run_git(self.origin, "checkout", "-q", "-b", "side")
        (self.origin / "SIDE.md").write_text("side\n")
        run_git(self.origin, "add", ".")
        run_git(self.origin, "commit", "-q", "-m", "side")
        run_git(self.origin, "checkout", "-q", "main")
        run_git(self.cache_path, "remote", "update")

        validated = self._validated([self._clone_op(branch="side")])
        evidence = execute_clone_operation(validated, 0, workspace_root=self.workspace_root)

        dest = self.workspace_root / self.dest_rel
        self.assertEqual(gitops.current_branch(dest), "side")
        self.assertEqual(evidence["branch"], "side")
        self.assertTrue((dest / "SIDE.md").exists())

    def test_a_non_clone_operation_is_refused_by_index(self):
        """Ordering is load-bearing for the receipt: evidence[i] must describe
        operation i. An executor that silently skipped a non-clone op would
        shift every later result."""
        validated = self._validated(
            [
                {
                    "kind": "venv",
                    "dest_path": "units/u_test/home/.venv",
                    "engine": "uv",
                    "python": "3.11",
                },
                self._clone_op(),
            ]
        )
        with self.assertRaises(CloneExecutionError) as ctx:
            execute_clone_operation(validated, 0, workspace_root=self.workspace_root)
        self.assertIn("is kind 'venv'", str(ctx.exception))


class TestIsolationCompleteness(CloneExecTestBase):
    """The defects present at bd7afe5. Every one is a real git witness this
    executor accepted; each lands here with the guard that closes it, so the
    mutant that reopens it has a declared victim."""

    def test_a_git_dir_that_is_itself_a_symlink_is_rejected(self):
        """The nastiest of the set, because it defeats TWO guards with
        one link: Path.is_dir() FOLLOWS the symlink and answers True, and
        `rev-parse --git-common-dir` resolves THROUGH the same link so both
        sides of the common-dir comparison land on the foreign directory and
        agree. Hence lstat, never is_dir()."""
        victim = self._make_clone(self.tmp / "victim", reference=self.cache_path)
        foreign = self._make_clone(self.tmp / "foreign", reference=self.cache_path)
        run_git(foreign, "checkout", "-q", "-b", "foreign-only")

        shutil.rmtree(victim / ".git")
        (victim / ".git").symlink_to(foreign / ".git")

        self.assertTrue((victim / ".git").is_dir(), "premise: is_dir() follows the link")
        with self.assertRaises(CloneExecutionError) as ctx:
            self._verify(victim)
        self.assertIn("redirects .git through a symlink", str(ctx.exception))

    def test_symlinked_refs_are_rejected(self):
        """.git stays a real local directory and the
        common dir is its own, so every prior guard passes -- while the clone
        reads another unit's refs."""
        victim = self._make_clone(self.tmp / "victim", reference=self.cache_path)
        foreign = self._make_clone(self.tmp / "foreign", reference=self.cache_path)
        run_git(foreign, "checkout", "-q", "-b", "foreign-only")

        shutil.rmtree(victim / ".git" / "refs")
        (victim / ".git" / "refs").symlink_to(foreign / ".git" / "refs")

        with self.assertRaises(CloneExecutionError) as ctx:
            self._verify(victim)
        self.assertIn(".git/refs", str(ctx.exception))

    def test_symlinked_objects_are_rejected_when_no_reference_is_declared(self):
        """The second route to object sharing, and the one the
        alternates check structurally cannot see: with no reference_base
        declared there is no alternates file to inspect, so that guard passes
        VACUOUSLY while the objects directory itself is joined to another
        unit's store."""
        victim = self._make_clone(self.tmp / "victim")
        foreign = self._make_clone(self.tmp / "foreign")

        shutil.rmtree(victim / ".git" / "objects")
        (victim / ".git" / "objects").symlink_to(foreign / ".git" / "objects")

        with self.assertRaises(CloneExecutionError) as ctx:
            self._verify(victim, reference_base=None)
        self.assertIn(".git/objects", str(ctx.exception))

    def test_a_shallow_clone_is_rejected(self):
        """Correct origin, local git dir, clean tree, valid
        alternate -- indistinguishable from healthy to every other guard.
        Section 8.1 requires the complete reachable history and the v1 plan
        declares no shallow profile."""
        run_git(self.origin, "commit", "-q", "--allow-empty", "-m", "second")
        run_git(self.cache_path, "remote", "update")
        shallow = self.tmp / "shallow"
        # file:// on purpose: git IGNORES --depth for a local-path clone, so a
        # plain path silently produces a full clone and the fixture would prove
        # nothing. Origin is then restored to the declared value, exactly as
        # Sentinel's witness did, so the origin guard cannot be what rejects it.
        subprocess.run(
            ["git", "clone", "-q", "--depth", "1", "--reference-if-able",
             str(self.cache_path), Path(self.repo_url).as_uri(), str(shallow)],
            capture_output=True, text=True, check=False,
        )
        run_git(shallow, "remote", "set-url", "origin", self.repo_url)
        self.assertTrue((shallow / ".git" / "shallow").exists(), "premise: fixture is shallow")
        self.assertEqual(
            run_git(shallow, "rev-list", "--count", "HEAD").strip(),
            "1",
            "premise: fixture holds truncated history",
        )

        with self.assertRaises(CloneExecutionError) as ctx:
            self._verify(shallow)
        self.assertIn("shallow", str(ctx.exception))

    def test_a_partial_clone_is_rejected(self):
        """The sibling of shallow: history fetched lazily from the network
        rather than truncated. .git/shallow is absent, so the shallow probe
        cannot see it -- it needs its own."""
        clone = self._healthy_clone()
        run_git(clone, "config", "remote.origin.promisor", "true")
        run_git(clone, "config", "remote.origin.partialclonefilter", "blob:none")

        with self.assertRaises(CloneExecutionError) as ctx:
            self._verify(clone)
        self.assertIn("partial clone", str(ctx.exception))


class TestCacheBoundaryIsTransitive(CloneExecTestBase):
    def test_a_cache_whose_objects_are_a_symlink_is_rejected(self):
        """Containment is only ONE HOP without this: the cache is at
        the right path, bare, with the right origin -- and its object store
        lives somewhere else entirely."""
        outside = self.tmp / "global-cache.git"
        run_git(self.tmp, "clone", "-q", "--mirror", self.repo_url, str(outside))
        shutil.rmtree(self.cache_path / "objects")
        (self.cache_path / "objects").symlink_to(outside / "objects")

        with self.assertRaises(CloneExecutionError) as ctx:
            verify_cache_provenance(
                self.cache_path, workspace_root=self.workspace_root, repo_url=self.repo_url
            )
        self.assertIn("objects directory through a symlink", str(ctx.exception))

    def test_a_cache_that_alternates_onward_to_a_global_store_is_rejected(self):
        """The decisive form. The clone's own alternate correctly
        names the workspace cache -- so the clone-side check is satisfied --
        and the cache then alternates out to a machine-global store, which the
        clone transitively reads. Object sharing must TERMINATE at the
        workspace cache."""
        global_cache = self.tmp / "global-cache.git"
        run_git(self.tmp, "clone", "-q", "--mirror", self.repo_url, str(global_cache))
        info = self.cache_path / "objects" / "info"
        info.mkdir(parents=True, exist_ok=True)
        (info / "alternates").write_text(f"{global_cache / 'objects'}\n")

        with self.assertRaises(CloneExecutionError) as ctx:
            verify_cache_provenance(
                self.cache_path, workspace_root=self.workspace_root, repo_url=self.repo_url
            )
        self.assertIn("must terminate at", str(ctx.exception))

    def test_relative_alternate_lines_resolve_the_way_git_resolves_them(self):
        """git resolves a relative alternates entry against the OBJECT
        DIRECTORY; resolving it against the process working directory makes the
        verifier and git disagree about what the clone actually reads -- and a
        verifier that disagrees with git is worse than none.

        The entry below is the declared cache written relatively. It must be
        ACCEPTED, which it can only be if resolution matches git's rule."""
        clone = self._healthy_clone()
        objects_dir = clone / ".git" / "objects"
        relative = os.path.relpath(self.cache_path / "objects", objects_dir)
        (objects_dir / "info" / "alternates").write_text(f"{relative}\n")

        self._verify(clone)


class TestDamagedCloneIsNotReused(CloneExecTestBase):
    """The one case reached independently from two directions."""

    def test_a_corrupt_index_blocks_reuse_instead_of_reading_as_clean(self):
        """gitops.repo_dirty() maps EVERY nonzero exit to False, so an
        unreadable index reads as 'clean' and the damaged clone is reused as
        healthy. Clean, dirty and unreadable are three outcomes; collapsing the
        one we understand least into the most permissive one is a fail-open."""
        validated = self._validated()
        execute_clone_operation(validated, 0, workspace_root=self.workspace_root)
        dest = self.workspace_root / self.dest_rel
        index_path = dest / ".git" / "index"
        index_path.write_bytes(b"not a git index")
        before = index_path.read_bytes()

        self.assertFalse(
            gitops.repo_dirty(dest),
            "premise: the shared helper reports a damaged clone as not-dirty",
        )
        with self.assertRaises(CloneExecutionError) as ctx:
            execute_clone_operation(self._validated(), 0, workspace_root=self.workspace_root)

        self.assertIn("damaged", str(ctx.exception))
        self.assertEqual(
            index_path.read_bytes(), before, "blocking must not modify the damaged clone"
        )

    def test_a_clone_whose_head_does_not_resolve_blocks_even_though_status_is_clean(self):
        """The second half of the same requirement: 'HEAD/reachability must succeed.'

        Finding the witness for this took two tries, and the first failure is
        worth recording. Deleting the branch ref makes git report an unborn
        branch with every tracked file staged-new, so status reads DIRTY and the
        dirty guard rejects it -- the HEAD check would have looked covered while
        being untested at its own level. Adjacent-guard masking inside the very
        guard set added to close these blockers.

        The witness that actually reaches it is an unborn branch with nothing
        tracked: status exits 0 with empty output, so the tri-state answers
        'clean', and only HEAD resolution can see that this clone has no
        commit to work from."""
        validated = self._validated()
        execute_clone_operation(validated, 0, workspace_root=self.workspace_root)
        dest = self.workspace_root / self.dest_rel
        run_git(dest, "checkout", "-q", "--orphan", "fresh")
        run_git(dest, "rm", "-rq", "-f", ".")

        status = gitops.git(dest, "status", "--porcelain")
        self.assertEqual(status.returncode, 0, "premise: status still succeeds")
        self.assertEqual(status.stdout.strip(), "", "premise: status still reads clean")

        with self.assertRaises(CloneExecutionError) as ctx:
            execute_clone_operation(self._validated(), 0, workspace_root=self.workspace_root)
        self.assertIn("no resolvable HEAD", str(ctx.exception))


class TestPreflightRunsBeforeAnyMutation(CloneExecTestBase):
    def test_an_invalid_cache_is_refused_before_git_clone_runs(self):
        """The masking case I missed: my own staging
        verifier is a stronger neighbour that produces the SAME error and the
        SAME end state, so removing the preflight left every test green while
        a clone had already run.

        The contract requires validation before the first filesystem mutation,
        so the assertion has to be that no clone happened -- not that an error
        was raised."""
        from gr2.python_cli import clone_exec

        self.cache_path.rename(self.tmp / "cache-moved-away")
        calls: list[object] = []

        def spy(*args, **kwargs):
            calls.append(args)

        with patch.object(clone_exec, "_git_clone", spy):
            with self.assertRaises(CloneExecutionError) as ctx:
                execute_clone_operation(
                    self._validated(), 0, workspace_root=self.workspace_root
                )

        self.assertEqual(calls, [], "git clone ran against a cache we then refused")
        self.assertIn("does not exist", str(ctx.exception))


class TestPublicationResidue(CloneExecTestBase):
    def test_a_failed_rename_leaves_no_staging_residue(self):
        """The rename sat OUTSIDE the cleanup boundary, so the
        no-residue guarantee held for every failure except the one occurring at
        the publication seam itself."""
        from gr2.python_cli import clone_exec

        dest = self.workspace_root / self.dest_rel
        real_replace = os.replace

        def failing_replace(src, dst, **kwargs):
            raise OSError("injected publication failure")

        with patch.object(clone_exec.os, "replace", failing_replace):
            with self.assertRaises(OSError):
                execute_clone_operation(
                    self._validated(), 0, workspace_root=self.workspace_root
                )

        self.assertIs(os.replace, real_replace, "patch leaked")
        self.assertFalse(dest.exists())
        self.assertEqual(
            sorted(p.name for p in dest.parent.iterdir()) if dest.parent.exists() else [],
            [],
            "a populated staging sibling survived a failed publication",
        )


class TestCapabilityWindow(CloneExecTestBase):
    def test_a_capability_swapped_during_path_work_cannot_redirect_the_clone(self):
        """The validation-vs-use class inside B's own code.

        The executor verified, then ran callback-capable path work, then re-read
        validated.plan live -- so a caller-supplied Path subclass could swap the
        plan after verification and have the forged destination cloned while the
        sealed one was never created.

        Closed two ways, both of which this probe exercises: workspace_root is
        normalised to a plain Path BEFORE verification, so the callback cannot
        run at all; and every operation field is captured into one immutable
        binding, so a later swap is irrelevant rather than merely detectable."""
        validated = self._validated()
        forged_dest = "units/u_forged/repos/product"
        forged_plan = {
            "schema_version": 1,
            "plan_id": "mp_test",
            "unit_key": "u_test",
            "workspace_spec_sha256": self.spec_sha256,
            "operations": [self._clone_op(dest_path=forged_dest)],
        }

        class SwappingPath(type(Path())):
            def __truediv__(inner, other):
                object.__setattr__(
                    validated, "plan", spec_apply._deep_freeze(forged_plan)
                )
                return super().__truediv__(other)

        execute_clone_operation(
            validated, 0, workspace_root=SwappingPath(self.workspace_root)
        )

        self.assertTrue(
            (self.workspace_root / self.dest_rel / ".git").is_dir(),
            "the sealed destination was never created",
        )
        self.assertFalse(
            (self.workspace_root / forged_dest).exists(),
            "the executor cloned a destination the capability never sealed",
        )


class TestReceiptEvidenceCompleteness(CloneExecTestBase):
    def test_evidence_records_what_was_observed_not_what_was_declared(self):
        """Section 12.1 requires repo URL, destination, HEAD,
        clone-state evidence, and cache path plus APPROVED-ALTERNATE evidence.
        Echoing the plan's reference_base back is not evidence -- a receipt that
        repeats the plan proves nothing about what is on disk. The previous
        receipt test asserted only stage and kind, which is what let the
        omissions read as green."""
        validated = self._validated()
        evidence = execute_clone_operation(validated, 0, workspace_root=self.workspace_root)

        self.assertEqual(evidence["repo_url"], self.repo_url)
        self.assertEqual(evidence["dest_path"], self.dest_rel)
        self.assertRegex(str(evidence["head_sha"]), r"^[0-9a-f]{40}$")
        self.assertEqual(evidence["cache_path"], self.cache_rel)
        self.assertEqual(
            evidence["approved_alternates"],
            [str(Path(self.cache_rel) / "objects")],
            "approved-alternate evidence must be the alternate git will actually "
            "read, expressed workspace-relatively",
        )
        self.assertEqual(evidence["clone_state"]["working_tree"], "clean")

        receipt_path = write_materialization_receipt(
            self.workspace_root, validated, [evidence]
        )
        import json

        receipt = json.loads(receipt_path.read_text())
        self.assertEqual(receipt["operations"][0]["repo_url"], self.repo_url)


if __name__ == "__main__":
    unittest.main()
