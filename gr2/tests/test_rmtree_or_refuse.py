"""The rmtree partial-failure class.

Nine production sites across clone_exec.py, review.py, and open_gr_review.py
called ``shutil.rmtree(path, ignore_errors=True)`` and then either reported
success unconditionally ("lane discarded", "reclaimed"), silently continued
to reuse another lane, silently swallowed a cleanup failure while re-raising
an unrelated original error, or (a finally-block cleanup) had no way to
surface a failure at all. ``ignore_errors=True`` drops any failure -- a
locked file, a permission denial, a busy mount -- and none of these sites
could tell "cleaned" from "partially cleaned." A first pass fixed five of the
nine (v3 of the review that produced this file); a second reader's own
completeness probe -- grepping the reconstructed tree for the same pattern
outside the helper -- found the other four, which is why that probe is now a
permanent test here (``RmtreeIgnoreErrorsClassClosedTest``) rather than a
one-time finding.

``rmtree_or_refuse`` (clone_exec.py) is the one shared helper: remove, then
verify by checking the path is actually gone (``shutil.rmtree`` only removes
a directory's own entry as its LAST step, so any survivor beneath it leaves
the directory itself present -- a complete detector, not a sample), and
raise ``IncompleteRemoval`` naming a leftover entry if it is not.

Each test below patches the REAL stdlib ``shutil.rmtree`` to a no-op --
simulating exactly what ``ignore_errors=True`` used to hide, a rmtree call
that does not fully remove its target -- and asserts the call site now
surfaces that instead of reporting or behaving as if cleanup succeeded.
"""
from __future__ import annotations

import ast
import io
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from gr2.python_cli import open_gr_review as open_gr_review_mod
from gr2.python_cli import review as review_mod
from gr2.python_cli.clone_exec import (
    CloneExecutionError,
    IncompleteRemoval,
    _publish_lane_atomically,
    _stage_and_publish,
    rmtree_or_refuse,
)
from gr2.python_cli.open_gr_review import (
    OpenGrReviewError,
    close_open_gr_lane,
    exit_gr_review,
    open_gr_receipt_path,
)
from gr2.python_cli.review import ReviewError, open_review_lane

_PYTHON_CLI_DIR = Path(open_gr_review_mod.__file__).resolve().parent


def run_git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=False
    )
    if proc.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed in {cwd}:\n{proc.stderr}")
    return proc.stdout


def _init_repo(root: Path, *, content: str) -> str:
    # `content` must differ across fixture repos: an identical tree, message,
    # author, and committer timestamp produce the IDENTICAL commit sha, which
    # would make two "different" repos silently share one HEAD.
    root.mkdir(parents=True, exist_ok=True)
    run_git(root, "init", "-q", "-b", "main")
    run_git(root, "config", "user.email", "t@example.com")
    run_git(root, "config", "user.name", "T")
    (root / "f.txt").write_text(content)
    run_git(root, "add", ".")
    run_git(root, "commit", "-q", "-m", f"init {content!r}")
    return run_git(root, "rev-parse", "HEAD").strip()


class RmtreeOrRefuseUnitTest(unittest.TestCase):
    """The shared helper itself: the core mechanism every call site relies on."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_a_normal_tree_is_fully_removed_and_returns_quietly(self):
        target = self.tmp / "gone"
        (target / "sub").mkdir(parents=True)
        (target / "sub" / "f.txt").write_text("x\n")
        rmtree_or_refuse(target)
        self.assertFalse(target.exists())

    def test_an_already_absent_path_is_not_an_error(self):
        rmtree_or_refuse(self.tmp / "never-existed")  # must not raise

    def test_a_survivor_beneath_the_path_raises_and_names_it(self):
        target = self.tmp / "stuck"
        (target / "sub").mkdir(parents=True)
        leftover = target / "sub" / "wont-go.txt"
        leftover.write_text("x\n")
        with patch("shutil.rmtree", side_effect=lambda *a, **k: None):
            with self.assertRaises(IncompleteRemoval) as cm:
                rmtree_or_refuse(target)
        msg = str(cm.exception)
        self.assertIn(str(target), msg)
        self.assertIn("could not be fully removed", msg)

    def test_mutation_the_existence_recheck_is_load_bearing(self):
        """If the post-rmtree existence check were removed (the exact shape of
        the original defect -- call rmtree, trust it, move on), this would
        pass silently instead of raising. Restated as a positive assertion so
        deleting the `if not path.exists(): return` guard reds this test."""
        target = self.tmp / "stuck2"
        target.mkdir()
        (target / "f.txt").write_text("x\n")
        with patch("shutil.rmtree", side_effect=lambda *a, **k: None):
            with self.assertRaises(IncompleteRemoval):
                rmtree_or_refuse(target)
        self.assertTrue(target.exists(), "fixture assumption: the mock never removes anything")


class StageAndPublishCleanupTest(unittest.TestCase):
    """Site 1 and (structurally identical) site 4 in clone_exec.py: an
    ``except BaseException:`` handler that used to call
    ``shutil.rmtree(staging, ignore_errors=True)`` and unconditionally
    re-raise the ORIGINAL exception, discarding any cleanup failure."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.workspace_root = self.tmp / "workspace"
        self.workspace_root.mkdir()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_clean_cleanup_still_reraises_the_original_error_unchanged(self):
        """Control: when cleanup succeeds, behavior is UNCHANGED from before
        this fix -- the original error propagates bare, not wrapped."""
        dest = self.tmp / "dest"
        with self.assertRaises(CloneExecutionError) as cm:
            _stage_and_publish(
                dest,
                workspace_root=self.workspace_root,
                repo_url="/no/such/repo/anywhere",
                branch="main",
                reference_base=None,
            )
        self.assertIn("failed to clone", str(cm.exception))
        self.assertNotIn("cleanup also left", str(cm.exception))

    def test_a_leftover_staging_dir_is_named_alongside_the_original_error(self):
        dest = self.tmp / "dest2"
        with patch("shutil.rmtree", side_effect=lambda *a, **k: None):
            with self.assertRaises(CloneExecutionError) as cm:
                _stage_and_publish(
                    dest,
                    workspace_root=self.workspace_root,
                    repo_url="/no/such/repo/anywhere",
                    branch="main",
                    reference_base=None,
                )
        msg = str(cm.exception)
        self.assertIn("failed to clone", msg, "the original failure must still be named")
        self.assertIn("cleanup also left it behind", msg)


class PublishLaneAtomicallyRaceLossCleanupTest(unittest.TestCase):
    """Sites 2 and 3 in clone_exec.py (byte-identical duplicated blocks): a
    race loser used to discard its own staging with
    ``shutil.rmtree(staging, ignore_errors=True)`` and silently proceed to
    reuse the winner's lane regardless of whether that succeeded."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.workspace_root = self.tmp / "workspace"
        self.workspace_root.mkdir()
        # dest already "published" by a winner -- any real directory suffices,
        # since _reuse_existing_lane is stubbed out below.
        self.dest = self.tmp / "dest"
        self.dest.mkdir()
        self.staging = self.tmp / "staging"
        self.staging.mkdir()
        (self.staging / "f.txt").write_text("x\n")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _publish(self):
        return _publish_lane_atomically(
            self.staging,
            self.dest,
            workspace_root=self.workspace_root,
            repo_url="https://example.invalid/repo.git",
            reference_base=None,
            expected_branch="main",
        )

    def test_clean_cleanup_reuses_the_winner_and_returns_false(self):
        with patch(
            "gr2.python_cli.clone_exec._reuse_existing_lane"
        ) as mock_reuse:
            result = self._publish()
        self.assertFalse(result)
        mock_reuse.assert_called_once()
        self.assertFalse(self.staging.exists())

    def test_a_leftover_staging_dir_refuses_instead_of_silently_reusing(self):
        """The exact defect: dest already exists (we lost the race), our own
        staging cleanup fails, and the old code called _reuse_existing_lane
        anyway -- a leftover directory with nothing anywhere saying so."""
        with patch("shutil.rmtree", side_effect=lambda *a, **k: None), patch(
            "gr2.python_cli.clone_exec._reuse_existing_lane"
        ) as mock_reuse:
            with self.assertRaises(IncompleteRemoval) as cm:
                self._publish()
        mock_reuse.assert_not_called()
        self.assertIn(str(self.staging), str(cm.exception))
        self.assertTrue(self.staging.exists(), "fixture assumption: the mock never removes anything")


class OpenReviewLaneCleanupTest(unittest.TestCase):
    """review.py's site: a freshly materialized lane at the wrong head used to
    call ``shutil.rmtree(lane_repo_root, ignore_errors=True)`` and then claim
    'lane discarded' unconditionally, regardless of whether it was."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.source = self.tmp / "source"
        self.source_head = _init_repo(self.source, content="source\n")
        self.lane = self.tmp / "lane"
        _init_repo(self.lane, content="lane\n")  # different content -> different HEAD
        self.workspace_root = self.tmp / "workspace"
        self.workspace_root.mkdir()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _open(self):
        with patch.object(review_mod, "ensure_lane_checkout", return_value=True), \
             patch.object(review_mod, "canonical_source_identity", return_value="x/y"), \
             patch.object(review_mod.gitops, "remote_origin_url", return_value="https://example/x.git"):
            return open_review_lane(
                source_repo_root=self.source,
                review_branch="main",
                expected_head_sha=self.source_head,
                base_sha="a" * 40,
                lane_repo_root=self.lane,
                workspace_root=self.workspace_root,
                allow_local=True,
            )

    def test_clean_discard_reports_discarded_and_the_lane_is_actually_gone(self):
        with self.assertRaises(ReviewError) as cm:
            self._open()
        self.assertIn("lane discarded", str(cm.exception))
        self.assertFalse(self.lane.exists())

    def test_a_leftover_lane_reports_the_leftover_instead_of_a_false_discarded_claim(self):
        with patch("shutil.rmtree", side_effect=lambda *a, **k: None):
            with self.assertRaises(ReviewError) as cm:
                self._open()
        msg = str(cm.exception)
        self.assertNotIn("lane discarded", msg, "must never claim success over a survivor")
        self.assertIn("could not be fully discarded", msg)
        self.assertTrue(self.lane.exists(), "fixture assumption: the mock never removes anything")


class OpenReviewLaneEphemeralCleanupTest(unittest.TestCase):
    """review.py:265, the review-ephemeral twin of the site already covered by
    OpenReviewLaneCleanupTest above (same file, same "lane discarded" shape,
    reached through the ``ephemeral=True`` branch instead)."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.source = self.tmp / "source"
        self.source_head = _init_repo(self.source, content="source\n")
        self.lane = self.tmp / "lane"
        self.workspace_root = self.tmp / "workspace"
        self.workspace_root.mkdir()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _open(self, *, leave_leftover: bool):
        def fake_materialize(*, mirror, dest, head, base, repo_name):
            # Simulate a botched materialize: a real directory lands, but not
            # at the expected head -- the shape that triggers first_materialize
            # discard below, without needing a real review-ephemeral clone.
            dest.mkdir(parents=True, exist_ok=True)
            (dest / "leftover.txt").write_text("x\n")

        with patch("gr2.python_cli.review_ephemeral.materialize_review_ephemeral", side_effect=fake_materialize), \
             patch.object(review_mod, "canonical_source_identity", return_value="x/y"), \
             patch.object(review_mod.gitops, "remote_origin_url", return_value="https://example/x.git"):
            rmtree_ctx = (
                patch("shutil.rmtree", side_effect=lambda *a, **k: None)
                if leave_leftover else patch("shutil.rmtree", wraps=shutil.rmtree)
            )
            with rmtree_ctx:
                return open_review_lane(
                    source_repo_root=self.source,
                    review_branch="main",
                    expected_head_sha=self.source_head,
                    base_sha="a" * 40,
                    lane_repo_root=self.lane,
                    workspace_root=self.workspace_root,
                    allow_local=True,
                    ephemeral=True,
                )

    def test_clean_discard_reports_discarded_and_the_lane_is_actually_gone(self):
        with self.assertRaises(ReviewError) as cm:
            self._open(leave_leftover=False)
        self.assertIn("lane discarded", str(cm.exception))
        self.assertFalse(self.lane.exists())

    def test_a_leftover_lane_reports_the_leftover_instead_of_a_false_discarded_claim(self):
        with self.assertRaises(ReviewError) as cm:
            self._open(leave_leftover=True)
        msg = str(cm.exception)
        self.assertNotIn("lane discarded", msg, "must never claim success over a survivor")
        self.assertIn("could not be fully discarded", msg)
        self.assertTrue(self.lane.exists(), "fixture assumption: the mock never removes anything")


class CloseOpenGrLaneCleanupTest(unittest.TestCase):
    """open_gr_review.py:82 (close_open_gr_lane): reported {"reclaimed": ...}
    unconditionally after the rmtree, whatever it actually removed."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.lane_dir = self.tmp / "lane"
        self.lane_dir.mkdir()
        (self.lane_dir / "leftover.txt").write_text("x\n")
        marker = self.lane_dir / ".grip-open-gr-reconstruct.json"
        marker.write_text(json.dumps({"kind": "open-gr-reconstruct", "gr_commit": "abc123"}))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_clean_removal_reports_reclaimed_and_the_dir_is_gone(self):
        result = close_open_gr_lane(self.lane_dir)
        self.assertEqual(result["gr_commit"], "abc123")
        self.assertFalse(self.lane_dir.exists())

    def test_a_leftover_lane_refuses_instead_of_reporting_reclaimed(self):
        with patch("shutil.rmtree", side_effect=lambda *a, **k: None):
            with self.assertRaises(OpenGrReviewError) as cm:
                close_open_gr_lane(self.lane_dir)
        self.assertIn("could not be fully reclaimed", str(cm.exception))
        self.assertTrue(self.lane_dir.exists(), "fixture assumption: the mock never removes anything")


class ExitGrReviewCleanupTest(unittest.TestCase):
    """open_gr_review.py:624 (exit_gr_review): returned OpenGrExit
    unconditionally after the rmtree of a review-ephemeral lane."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.review_root = self.tmp / "review"
        self.review_root.mkdir()
        (self.review_root / "leftover.txt").write_text("x\n")
        open_gr_receipt_path(self.review_root).write_text(json.dumps({
            "lane_kind": "review-ephemeral",
            "prior_cwd": str(self.tmp),
            "gr_commit": "deadbeef",
        }))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _exit(self):
        with patch.object(open_gr_review_mod.lane_proto, "exit_lane", return_value=None), \
             patch.object(open_gr_review_mod, "_current_lane_name", return_value="some-lane"):
            return exit_gr_review(self.tmp, "unit", self.review_root, actor="test")

    def test_clean_removal_returns_the_exit_receipt_and_the_root_is_gone(self):
        result = self._exit()
        self.assertEqual(result.gr_commit, "deadbeef")
        self.assertFalse(self.review_root.exists())

    def test_a_leftover_review_root_refuses_instead_of_reporting_success(self):
        with patch("shutil.rmtree", side_effect=lambda *a, **k: None):
            with self.assertRaises(OpenGrReviewError) as cm:
                self._exit()
        self.assertIn("could not be fully removed", str(cm.exception))
        self.assertTrue(self.review_root.exists(), "fixture assumption: the mock never removes anything")


class OpenGrEnterScratchCleanupTest(unittest.TestCase):
    """open_gr_review.py:313 (open_gr_enter, inside a finally:): the ONE
    log-and-continue site. The finally spans whatever the try block just did
    (a real outcome, or an exception in flight), so a leftover scratch clone
    must be reported without ever raising here -- unlike the other eight
    sites, a passing test here means the ORIGINAL return value survives."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.workspace = self.tmp / "workspace"
        self.workspace.mkdir()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, *, leave_leftover: bool):
        from gr2.python_cli.project_review import ProjectReviewOutcome

        row = {"key": "repo-a", "repo": "x", "path": "p", "base": "b" * 40, "head": "h" * 40}
        scratch_holder: dict[str, Path] = {}

        def fake_reconstruct(workspace, gr_commit, key, dest):
            dest.mkdir(parents=True, exist_ok=True)
            (dest / "leftover.txt").write_text("x\n")
            scratch_holder["root"] = dest.parent
            return {"reconstructed_head": "n" * 40}

        # status="refused" short-circuits right after the finally block, so
        # this fixture never has to satisfy the receipt-writing tail below it.
        refused_outcome = ProjectReviewOutcome(
            status="refused", grip_commit="gr-commit-1", observed=(),
            failures=(), review_root=None, current_lane_changed=False,
        )

        rmtree_ctx = (
            patch("shutil.rmtree", side_effect=lambda *a, **k: None)
            if leave_leftover else patch("shutil.rmtree", wraps=shutil.rmtree)
        )
        with patch.object(open_gr_review_mod.grip, "read_project_review_commit", return_value=[row]), \
             patch.object(open_gr_review_mod.grip, "project_review_carried_keys", return_value={"repo-a"}), \
             patch.object(open_gr_review_mod.grip, "reconstruct_project_review_lane", side_effect=fake_reconstruct), \
             patch.object(open_gr_review_mod, "resolve_sources_from_pins", return_value=({}, {})), \
             patch.object(open_gr_review_mod.project_review, "open_project_review", return_value=refused_outcome), \
             rmtree_ctx, \
             patch("sys.stderr", new_callable=io.StringIO) as fake_err:
            result = open_gr_review_mod.open_gr_enter(
                self.workspace, "unit", "lane", "gr-commit-1", prior_cwd=self.tmp,
            )
        return result, scratch_holder["root"], fake_err.getvalue()

    def test_clean_cleanup_returns_the_real_outcome_and_scratch_is_gone(self):
        result, scratch_root, stderr = self._run(leave_leftover=False)
        self.assertEqual(result.status, "refused")
        self.assertFalse(scratch_root.exists())
        self.assertEqual(stderr, "")

    def test_a_leftover_scratch_dir_is_logged_not_raised(self):
        result, scratch_root, stderr = self._run(leave_leftover=True)
        # The ORIGINAL outcome survives -- a raise here would have replaced it.
        self.assertEqual(result.status, "refused")
        self.assertTrue(scratch_root.exists(), "fixture assumption: the mock never removes anything")
        self.assertIn(str(scratch_root), stderr)


class RmtreeIgnoreErrorsClassClosedTest(unittest.TestCase):
    """The completeness probe that found the four sites this file's second
    reader caught: every ``shutil.rmtree(..., ignore_errors=True)`` call
    anywhere under gr2/python_cli must live inside rmtree_or_refuse itself.
    A future site written the old way fails THIS test, not a code review
    that has to remember to look for it."""

    def test_ignore_errors_true_appears_only_inside_the_shared_helper(self):
        offenders = []
        for path in sorted(_PYTHON_CLI_DIR.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            stack: list[str] = []

            class _Visitor(ast.NodeVisitor):
                def visit_FunctionDef(self, node):  # noqa: N802 (ast visitor name)
                    stack.append(node.name)
                    self.generic_visit(node)
                    stack.pop()

                def visit_Call(self, node):  # noqa: N802 (ast visitor name)
                    func = node.func
                    is_rmtree = (
                        (isinstance(func, ast.Attribute) and func.attr == "rmtree")
                        or (isinstance(func, ast.Name) and func.id == "rmtree")
                    )
                    if is_rmtree:
                        for kw in node.keywords:
                            if (
                                kw.arg == "ignore_errors"
                                and isinstance(kw.value, ast.Constant)
                                and kw.value.value is True
                            ):
                                enclosing = stack[-1] if stack else None
                                offenders.append((path.name, enclosing, node.lineno))
                    self.generic_visit(node)

            _Visitor().visit(tree)

        unexpected = [
            o for o in offenders
            if not (o[0] == "clone_exec.py" and o[1] == "rmtree_or_refuse")
        ]
        self.assertEqual(
            unexpected, [],
            f"shutil.rmtree(..., ignore_errors=True) found outside rmtree_or_refuse: "
            f"{unexpected} -- route it through rmtree_or_refuse instead",
        )
        # Control: the scan itself must find the ONE call inside the helper, or a
        # broken AST walk would report a false-clean empty list. Asserted on
        # (file, function) plus the count, NOT on the call's line number: the
        # number moves for any unrelated insertion above the helper, which turns
        # a working control into a false red about the code that was inserted.
        helper_hits = [
            o for o in offenders if o[0] == "clone_exec.py" and o[1] == "rmtree_or_refuse"
        ]
        self.assertEqual(
            len(helper_hits), 1,
            f"the control must find exactly one call inside the helper; got {offenders}",
        )


if __name__ == "__main__":
    unittest.main()
