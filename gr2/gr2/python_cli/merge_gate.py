"""Merge a bound review into plain Git remotes: preflight every member, push nothing if any member fails it.

The gate keys on the review id, not a hosting-platform PR. For each member it requires, read from
the member's remote: the feature branch still at the reviewed head, the target branch still at
the reviewed base, and a passing exact-head check record. Only when every member passes are
merges built (all before any push) and pushed, each under a lease on the base it was checked
against. The exit status is computed from the remote state after the run, not from this run's
actions: 0 all merged, 3 none merged (refused), 4 partial or unknown.

Not atomic across repositories: the pushes are separate. A lost acknowledgement is re-read, and
an unresolved member is `unknown`; no further member is pushed after one.
"""
from __future__ import annotations

import os
import subprocess
import uuid
from pathlib import Path
from typing import Mapping, Sequence

from . import check_records, gitops, grip

EXIT_MERGED, EXIT_REFUSED, EXIT_PARTIAL = 0, 3, 4


def _git(repo: Path, *args: str, check: bool = True) -> str:
    proc = gitops.run(repo, *args, timeout=60, raise_timeout=True)  # an expiry raises, as it always did here
    if check:
        gitops._raise(proc, args, RuntimeError)
    return proc.stdout.strip()


_plain_remote = gitops.plain_remote


def _advertised(repo: Path, remote: str, ref: str) -> str | None:
    """The oid the remote advertises for EXACTLY ref; ls-remote patterns also match nested refs by tail."""
    _plain_remote(remote)
    hits = [line.split("\t") for line in _git(repo, "ls-remote", remote, ref).splitlines()]
    exact = [oid for oid, name in hits if name == ref]
    if len(exact) > 1:
        raise RuntimeError(f"ls-remote: {ref} advertised more than once")
    return exact[0] if exact else None


def _remote_tip(repo: Path, remote: str, branch: str) -> str | None:
    """Fetch one remote branch into a private ref and return its commit, None when absent."""
    _plain_remote(remote)
    if _advertised(repo, remote, f"refs/heads/{branch}") is None:
        return None
    staging = f"refs/dev.synapt.grip/__merge_transfers__/{uuid.uuid4().hex}"
    try:
        _git(repo, "fetch", "--no-tags", "--no-write-fetch-head", remote, f"refs/heads/{branch}:{staging}")
        return _git(repo, "rev-parse", "--verify", f"{staging}^{{commit}}")
    finally:
        _git(repo, "update-ref", "-d", staging, check=False)


def _merged_at(repo: Path, target: str, head: str) -> str | None:
    """The commit on target's first-parent chain whose SECOND parent is head, if any."""
    for line in _git(repo, "rev-list", "--first-parent", "--parents", target).splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[2] == head:
            return parts[0]
    return None


def _state(repo: Path, remote: str, into: str, head: str) -> tuple[str, str | None]:
    """Re-read one member from its remote: ('merged', commit) | ('unmerged', None) | ('unknown', None)."""
    try:
        tip = _remote_tip(repo, remote, into)
        if tip is None:
            return "unknown", None
        merged = _merged_at(repo, tip, head)
    except (RuntimeError, subprocess.SubprocessError, OSError):
        return "unknown", None  # every measurement fault, history reads included, is unknown
    return ("merged", merged) if merged else ("unmerged", None)


def _toplevel(repo: Path) -> Path | None:
    """The worktree git actually selects for repo; a plain directory inherits its enclosing repo."""
    try:
        p = gitops.run(repo, "rev-parse", "--show-toplevel", timeout=60)
    except (subprocess.SubprocessError, OSError):
        return None
    return Path(p.stdout.strip()).resolve() if p.returncode == 0 and p.stdout.strip() else None


_WRITE_PATHS = ("objects", "refs", "packed-refs")
_WALKED = ("objects", "refs", "logs")  # the trees fetch and push write into


def _raise(exc: OSError) -> None:
    raise exc


def _store_inside(repo: Path, root: Path) -> bool:
    """Every place git WRITES for this member resolves (symlinks followed) inside the workspace.

    A .git file or symlink can point the git dir anywhere, and inside the git dir objects/ or refs/ can
    themselves be symlinks; the common dir alone does not bound where a fetch or push lands."""
    args = ["rev-parse", "--absolute-git-dir", "--git-common-dir", *[a for w in _WRITE_PATHS for a in ("--git-path", w)]]
    try:
        p = gitops.run(repo, *args, timeout=60)
    except (subprocess.SubprocessError, OSError):
        return False
    lines = p.stdout.splitlines()
    if p.returncode or len(lines) != 2 + len(_WRITE_PATHS):
        return False
    if not all((repo / line).resolve().is_relative_to(root) for line in lines):
        return False
    # Inside the trees fetch and push WRITE (objects, refs, logs, packed-refs), a symlink at any depth
    # redirects a write: a linked DIRECTORY catches rename-into-place writes (objects/xx, refs/heads/...), a
    # linked FILE catches in-place reflog appends. Walk those trees without following links. Elsewhere
    # (hooks/, config) links are ordinary: our own clones install hooks as symlinks, and nothing here writes there.
    for top in {(repo / lines[0]).resolve(), (repo / lines[1]).resolve()}:
        if os.path.islink(top / "packed-refs"):
            return False
        for sink in _WALKED:
            base = top / sink
            if os.path.islink(base):
                return False
            if not base.exists():
                continue  # an absent optional sink (a fresh repo may have no logs/) holds nothing to redirect
            try:  # a directory the walk cannot list could hide a link: fail closed, never skip it
                for dirpath, dirnames, filenames in os.walk(base, followlinks=False, onerror=_raise):
                    if any(os.path.islink(os.path.join(dirpath, n)) for n in (*dirnames, *filenames)):
                        return False
            except OSError:
                return False
    return True


def _unmeasured(review_id: str, into: str, feature: str, reason: str) -> tuple[int, dict]:
    """A local bind fault: nothing was transferred, and no member's remote state was measured."""
    return EXIT_PARTIAL, {"id": f"gr:{review_id}", "into": into, "feature": feature, "exit": EXIT_PARTIAL,
                          "refused": reason, "members": []}


def review_merge(workspace: Path, review_id: str, *, into: str = "main", feature: str,
                 required_checks: Sequence[str] = ("test",), required_approvals: int | None = None) -> tuple[int, dict]:
    review_id = review_id.removeprefix("gr:")
    try:
        verified = grip.verify_review_commit(workspace, review_id)
        if verified.get("tree_matches") is not True:  # the legacy verifier RETURNS a mismatch, it does not raise
            return _unmeasured(review_id, into, feature, "bind_verification_failed: stored tree does not match")
        view = grip.show_review_commit(workspace, review_id)
    except (grip.GripInitError, grip.GripCorruptError, RuntimeError, OSError, ValueError, KeyError,
            subprocess.SubprocessError) as exc:
        return _unmeasured(review_id, into, feature, f"bind_unreadable: {type(exc).__name__}: {exc}")
    rows: list[dict] = []
    root = Path(workspace).resolve()
    selected: set[str] = set()  # members whose git repository IS the bound path; only these are ever touched
    for m in view["members"]:
        repo = (workspace / m["path"]).resolve()
        row = {"key": m["key"], "remote": m["remote"], "head": m["head"], "base": m["base"],
               "state": None, "merged": None, "refused": None, "check": None}
        rows.append(row)
        if not repo.is_relative_to(root):
            row["refused"] = f"member_path_outside_workspace: {m['path']}"
            continue
        if _toplevel(repo) != repo:
            row["refused"] = f"member_repo_mismatch: {m['path']} is not its own git worktree"
            continue
        if not _store_inside(repo, root):
            # gr2 members are independent clones; an object store outside the workspace is never written to.
            row["refused"] = f"member_store_outside_workspace: {m['path']}"
            continue
        selected.add(m["key"])
        try:
            target = _remote_tip(repo, m["remote"], into)
            feat = _remote_tip(repo, m["remote"], feature)
            published = _advertised(repo, m["remote"], f"refs/dev.synapt.grip/__reviews__/v1/{review_id}")
            merged = _merged_at(repo, target, m["head"]) if target else None
            ancestor = _git(repo, "merge-base", m["head"], target) if target else ""
        except (RuntimeError, subprocess.SubprocessError, OSError) as exc:
            # Every measurement fault, advertisement and history reads included, is a named refusal in the receipt.
            row["refused"] = f"remote_unmeasurable: {exc}"
            continue
        if published != review_id:
            row["refused"] = "review_not_on_member_remote: the remote does not hold this exact review"
            continue
        if merged:
            row.update(state="already_merged", merged=merged)
            continue
        if feat != m["head"]:
            row["refused"] = f"feature_moved: remote {feature} is {feat}, reviewed {m['head']}"
        elif target != m["base"]:
            row["refused"] = f"base_moved: remote {into} is {target}, reviewed base {m['base']}"
        elif ancestor != target:
            row["refused"] = "base_not_ancestor_of_head"
        else:
            check = check_records.read_remote_check(
                m["remote"], {"path": str(repo), "key": m["key"], "remote": m["remote"]},
                m["head"], tuple(required_checks))
            row["check"] = {k: check.get(k) for k in ("status", "record_id", "reason")}
            if check["status"] != "pass":
                row["refused"] = f"check_{check['status']}: {check['reason']}"
            else:
                row["state"] = "ready"

    approval_receipt = None
    if not any(r["refused"] for r in rows):
        from . import approvals
        try:
            required = approvals.required_approvals(root, required_approvals)
            approval_receipt = {"required": required, "skipped": True}
            if required:
                approval_receipt = approvals.count_approvals(root, view["id"])
                approval_receipt["required"] = required
                if approval_receipt["count"] < required:
                    raise approvals.ApprovalRefused(f"approvals_insufficient: {approval_receipt['count']} of {required}")
        except (approvals.ApprovalRefused, grip.GripInitError, grip.GripCorruptError,
                OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
            for r in rows:
                if r["state"] == "ready":
                    r["refused"] = str(exc)

    if not any(r["refused"] for r in rows):
        # Build every merge before any push.
        builds = {}
        for r, m in zip(rows, view["members"]):
            if r["state"] != "ready":
                continue
            repo = (workspace / m["path"]).resolve()
            try:
                proc = gitops.run(repo, "merge-tree", "--write-tree", m["base"], m["head"], timeout=60,
                                 raise_timeout=True)  # an expiry is a build failure, not a conflict
                if proc.returncode:
                    r["refused"] = "merge_conflict"
                    continue
                tree = proc.stdout.split()[0]
                builds[r["key"]] = _git(repo, "commit-tree", tree, "-p", m["base"], "-p", m["head"], "-m",
                                        f"Merge {feature} into {into} (review gr:{view['id'].removeprefix('gr:')})")
            except (RuntimeError, subprocess.SubprocessError, OSError, IndexError) as exc:
                r["refused"] = f"merge_build_failed: {exc}"
        if not any(r["refused"] for r in rows):
            for r, m in zip(rows, view["members"]):
                if r["state"] != "ready":
                    continue
                repo = (workspace / m["path"]).resolve()
                try:
                    still = _remote_tip(repo, m["remote"], feature)
                except (RuntimeError, subprocess.SubprocessError, OSError):
                    still = None
                if still != m["head"]:
                    r["refused"] = f"feature_moved_before_push: remote {feature} is {still}"
                    break  # narrows the head race; no further member is pushed
                try:
                    # An expiry is rc 124 here, so it lands in push_error and the state below is re-measured.
                    proc = gitops.run(repo, "push", f"--force-with-lease=refs/heads/{into}:{m['base']}",
                                      m["remote"], f"{builds[r['key']]}:refs/heads/{into}", timeout=120)
                    push_error = proc.stderr.strip()[-300:] if proc.returncode else None
                except (subprocess.SubprocessError, OSError) as exc:  # a timeout is not evidence nothing moved
                    push_error = f"push_unconfirmed: {type(exc).__name__}"
                state, merged = _state(repo, m["remote"], into, m["head"])
                by = "this_run" if merged == builds[r["key"]] else ("another_writer" if merged else None)
                r.update(state=state, merged=merged, merged_by=by)
                if push_error and by != "this_run":
                    r["push_error"] = push_error  # e.g. our lease lost to a writer who merged the same head
                if state == "unknown":
                    break  # push no further members after an unresolved one

    # Exit from the remote STATE after the run.
    final = []
    for r, m in zip(rows, view["members"]):
        repo = (workspace / m["path"]).resolve()
        if m["key"] not in selected:
            r["final"] = "unknown"  # never touched, so never measured: no fetch writes into a repo outside the workspace
            final.append("unknown")
            continue
        state, merged = _state(repo, m["remote"], into, m["head"])
        r["final"] = state
        if merged:
            r["merged"] = merged
        final.append(state)
    if all(s == "merged" for s in final):
        code = EXIT_MERGED
    elif all(s == "unmerged" for s in final):
        code = EXIT_REFUSED
    else:
        code = EXIT_PARTIAL
    return code, {"id": view["id"], "into": into, "feature": feature, "exit": code, "members": rows,
                  "approvals": approval_receipt}
