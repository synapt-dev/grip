"""One exact-head observation over a whole review: every member at its pinned head, side by side, one command.

`check run` observes ONE repo at ONE head, so a test that needs the other members cannot run and a pass recorded
for one member says nothing about the set. This module materializes every member of a pinned review next to the
others (the reconstruction `review open` already does, which proves each tree equals the pinned head-tree), runs one
command once at the lane root, and publishes the result to every member's remote as an ordinary check observation
named `set`, plus two fields only this producer writes: the review it belongs to and the identity of the set it was
run over. The merge gate judges those fields (`judge_set_records`), never the name alone.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Mapping, Sequence

from . import check_records, gitops, grip, next_steps

SET_CHECK_NAME = "set"
_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_TAIL = 2000


def set_id(members: Sequence[Mapping[str, str]]) -> str:
    """The identity of the set: sha256 over the canonical sorted [key, head] pairs of the review's members."""
    pairs = sorted([m["key"], m["head"]] for m in members)
    return hashlib.sha256(json.dumps(pairs, separators=(",", ":")).encode("utf-8")).hexdigest()


def judge_set_records(rows: Sequence[Mapping], review: str, expected_set_id: str | None = None) -> tuple[str, str]:
    """('pass' | 'fail' | 'stale' | 'absent', reason) for the `set` observations read at one member's head.

    Only a record carrying THIS review and THIS set identity counts. A `set` record that carries neither (the
    name written by `check run`) or belongs to another review is stale evidence about something else: it is named
    as such and never counted, in either direction (a stale failure does not block, a stale pass does not satisfy).
    Without expected_set_id only the review is compared (a reader holding one record and no workspace); the merge gate
    always passes the set identity it computed from the review's own rows."""
    named = [r for r in rows if r.get("name") == SET_CHECK_NAME]
    mine = [r for r in named if r.get("review") == review
            and (expected_set_id is None or r.get("set_id") == expected_set_id)]
    if not mine:
        if named:
            return "stale", "a set record exists at this head but not for this review and set"
        return "absent", "no set record at this head"
    if any(r.get("result") != "pass" for r in mine):
        return "fail", "the set check failed at these heads"
    return "pass", "the set check passed at these heads"


def read_for_review(remote: str, member: Mapping, head: str, required: Sequence[str], review: str) -> dict:
    """`check show --require set --review`: the read the merge gate would make, with ONE judge for `set`.

    The other required names are read by name as always; the `set` verdict comes from judge_set_records alone, so a
    record named `set` that this producer did not write for this review (a forged `check run --name set -- false`)
    neither blocks nor satisfies it, here or at the gate. A transport or format fault is not a measurement: it stays
    `fail` exactly as a read without --review reports it, never an absence."""
    review = review if review.startswith("gr:") else f"gr:{review}"
    others = tuple(n for n in required if n != SET_CHECK_NAME)
    result = check_records.read_remote_check(remote, member, head, others or (SET_CHECK_NAME,))
    if result["status"] == "fail" and result["reason"] != "required_check_failed":
        return result
    verdict, _ = judge_set_records(result["records"], review)
    parts = [{"pass": ("pass", "required_checks_pass"), "fail": ("fail", "required_check_failed"),
              "stale": ("absent", "required_check_stale"), "absent": ("absent", "required_check_absent")}[verdict]]
    if others:
        parts.insert(0, (result["status"], result["reason"]))
    order = ("fail", "absent", "pass")
    status, reason = min(parts, key=lambda p: order.index(p[0]))
    result.update(status=status, reason=reason)
    return result


def _tail(data: bytes) -> str:
    return data.decode("utf-8", errors="replace")[-_TAIL:]


def run_set_check(workspace: Path, review_id: str, name: str, argv: Sequence[str]) -> dict:
    """Reconstruct every member of the review into one lane, run argv once there, publish one observation per member.

    Raises check_records.CheckRefused before anything is published for every fault but a PARTIAL publication, which
    names the members that did and did not receive the record (a merge refuses the ones that did not)."""
    workspace = Path(workspace)  # as given: the bind store is looked up by this path (the merge gate does the same)
    root = workspace.resolve()
    review_id = review_id.removeprefix("gr:")
    if not name.strip() or not argv:
        raise check_records.CheckRefused("check_name_and_command_required")
    try:
        verified = grip.verify_review_commit(workspace, review_id)
        if verified.get("tree_matches") is not True:
            raise check_records.CheckRefused("bind_verification_failed: stored tree does not match")
        view = grip.show_review_commit(workspace, review_id)
    except (grip.GripInitError, grip.GripCorruptError, grip.GripReviewRefused, RuntimeError, OSError, ValueError,
            KeyError, subprocess.SubprocessError) as exc:
        raise check_records.CheckRefused(f"bind_unreadable: {type(exc).__name__}: {exc}") from exc
    review, members = view["id"], view["members"]
    if not members:
        raise check_records.CheckRefused("review_has_no_members")
    keys = [m["key"] for m in members]
    for key in keys:
        if not _KEY.fullmatch(key) or key.endswith(".git"):
            raise check_records.CheckRefused(f"member_key_unsafe: {key!r} cannot name a directory in the lane")
    repos: dict[str, Path] = {}
    for m in members:
        if grip.url_has_credentials(m["remote"]):  # every message below may name the remote, so refuse by key first
            raise check_records.CheckRefused(
                f"remote_credentials: {m['key']}: the recorded remote cannot be parsed or carries URL userinfo; "
                "pin again from a remote without it and use a credential helper")
        repo = (workspace / m["path"]).resolve()
        if not repo.is_relative_to(root):
            raise check_records.CheckRefused(f"member_path_outside_workspace: {m['path']}")
        if gitops.run(repo, "cat-file", "-e", f"{m['head']}^{{commit}}").returncode != 0:
            # Named with its next step: a reviewer's clone lacks the head until it is fetched (`review receive`
            # fetches it from the member's own remote).
            raise check_records.CheckRefused(
                f"head_not_in_member_clone: {m['key']} {m['head']}; fetch it with "
                f"`{next_steps._head_fetch(str(repo), m['remote'], m['head'])}`")
        check_records._oid(repo, m["head"])  # a full commit id that this member's repo holds
        check_records._remote(m["remote"])
        repos[m["key"]] = repo
    sid = set_id(members)
    try:
        grip.require_reconstructable(workspace, review_id, keys)
    except grip.GripReviewRefused as exc:
        raise check_records.CheckRefused(f"set_not_reconstructable: {exc}") from exc

    with tempfile.TemporaryDirectory(prefix="gr2-set-") as directory:
        lane = Path(directory)
        before: dict[str, str] = {}
        reproduced: dict[str, str] = {}
        for key in keys:
            try:
                res = grip.reconstruct_review_lane(workspace, review_id, key, lane / key)
            except grip.GripReviewRefused as exc:
                raise check_records.CheckRefused(f"set_not_reconstructable: {key}: {exc}") from exc
            before[key] = res["reconstructed_head"]
            # A bind that carries committer data reproduces the pinned commit; one that does not reproduces its tree
            # (proven equal above). What merges is that tree, so both are an exact observation, and the record says which.
            reproduced[key] = "head" if res["reconstructed_head"] == res["bound_head"] else "tree"
        env = {**os.environ, "GR2_SET_ROOT": str(lane), "GR2_REVIEW_ID": review,
               "GR2_SET_MEMBERS": json.dumps({k: str(lane / k) for k in keys}, sort_keys=True)}
        execution = subprocess.run(list(argv), cwd=lane, env=env, capture_output=True)
        after = {m["key"]: check_records._git(lane / m["key"], "rev-parse", "HEAD").decode().strip() for m in members}
    for m in members:
        if after[m["key"]] != before[m["key"]]:
            raise check_records.CheckRefused(f"check_execution_head_changed: {m['key']}")

    result = "pass" if execution.returncode == 0 else "fail"
    listing = sorted(f"{m['key']}:{m['head']}" for m in members)
    published, failed = [], None
    for m in members:
        row = dict(v=1, head=m["head"], observed_head=m["head"], name=name, result=result,
                   exit_code=execution.returncode, review=review, set_id=sid, members=listing,
                   reproduced=reproduced[m["key"]])
        try:
            out = check_records.publish_observation(repos[m["key"]], m["remote"], row)
        except (check_records.CheckRefused, subprocess.SubprocessError, OSError) as exc:
            failed = (m["key"], str(exc))
            break
        published.append({"key": m["key"], "head": m["head"], "reproduced": reproduced[m["key"]], "record_id": out["record_id"],
                          "observation_id": out["observation"]["observation_id"]})
    if failed:
        raise check_records.CheckRefused(
            f"set_publication_incomplete: published to {[p['key'] for p in published]}, failed at {failed[0]}: {failed[1]}")
    return {"review": review, "set_id": sid, "name": name, "result": result, "exit_code": execution.returncode,
            "members": published, "output_tail": _tail(execution.stdout + execution.stderr)}


def run_declared_set_check(workspace: Path, set_name: str, root_commit: str, argv: Sequence[str]) -> dict:
    """The check for a DECLARED set (`[[sets]]` in the store's grip.toml): one command over every member of the set
    at the heads a root commit pins, then one record per member remote naming that root commit.

    The subject is the root commit, not a review: its gitlinks name the heads, so the record is one statement for
    the whole set and no per-review record can stand in for it. It lists the set's members as declared AT that
    commit, and refuses before running anything when the commit does not pin every one of them."""
    from . import declared_sets

    root = Path(workspace).resolve()
    if not argv:
        raise check_records.CheckRefused("check_name_and_command_required")
    resolved = gitops.run(root, "rev-parse", "--verify", "-q", f"{root_commit}^{{commit}}")
    if resolved.returncode or not resolved.stdout.strip():
        raise check_records.CheckRefused(f"root_commit_unknown: {root_commit!r} is not a commit of this workspace")
    commit = resolved.stdout.strip()
    try:
        declaration = declared_sets.read(root, commit)
    except declared_sets.DeclaredSetError as exc:
        raise check_records.CheckRefused(f"declared_sets_invalid: {exc}") from exc
    dset = declaration.set_named(set_name)
    if dset is None:
        raise check_records.CheckRefused(
            f"declared_set_unknown: {set_name!r} is not declared at {commit[:12]}; declared: {[s.name for s in declaration.sets]}")
    if (full := declared_sets.mode_refusal(declaration)):
        raise check_records.CheckRefused(full)
    pins, missing = declared_sets.gitlinks(root, commit, declaration, dset)
    if missing:
        raise check_records.CheckRefused(
            f"declared_set_member_unpinned: root commit {commit[:12]} does not pin {missing} of set {set_name!r}")
    repos: dict[str, Path] = {}
    for member, head in pins.items():
        repo = (root / declaration.members[member]["path"]).resolve()
        if not repo.is_relative_to(root):
            raise check_records.CheckRefused(f"member_path_outside_workspace: {declaration.members[member]['path']}")
        check_records._oid(repo, head)  # a full commit id that this member's repo holds
        check_records._remote(declaration.members[member]["remote"])
        repos[member] = repo
    with tempfile.TemporaryDirectory(prefix="gr2-declared-set-") as directory:
        lane = Path(directory)
        for member, head in pins.items():
            cloned = gitops.clone("--quiet", "--no-hardlinks", "--no-checkout", str(repos[member]), str(lane / member), binary=True)
            if cloned.returncode:
                gitops._raise(cloned, ("clone",), check_records.CheckRefused)
            check_records._git(lane / member, "checkout", "--quiet", "--detach", head)
        env = {**os.environ, "GR2_SET_ROOT": str(lane), "GR2_SET_NAME": set_name, "GR2_ROOT_COMMIT": commit,
               "GR2_SET_MEMBERS": json.dumps({m: str(lane / m) for m in pins}, sort_keys=True)}
        execution = subprocess.run(list(argv), cwd=lane, env=env, capture_output=True)
        after = {m: check_records._git(lane / m, "rev-parse", "HEAD").decode().strip() for m in pins}
    if after != pins:
        raise check_records.CheckRefused("check_execution_head_changed")
    result = "pass" if execution.returncode == 0 else "fail"
    listing = sorted(f"{m}:{h}" for m, h in pins.items())
    published, failed = [], None
    for member, head in pins.items():
        row = dict(v=1, head=head, observed_head=head, name=declared_sets.DECLARED_SET_CHECK, result=result,
                   exit_code=execution.returncode, set_name=set_name, root_commit=commit,
                   declared_members=list(dset.members), heads=listing)
        try:
            out = check_records.publish_observation(repos[member], declaration.members[member]["remote"], row)
        except (check_records.CheckRefused, subprocess.SubprocessError, OSError) as exc:
            failed = (member, str(exc))
            break
        published.append({"member": member, "head": head, "record_id": out["record_id"],
                          "observation_id": out["observation"]["observation_id"]})
    if failed:
        raise check_records.CheckRefused(
            f"set_publication_incomplete: published to {[p['member'] for p in published]}, failed at {failed[0]}: {failed[1]}")
    return {"set": set_name, "root_commit": commit, "name": declared_sets.DECLARED_SET_CHECK, "result": result,
            "exit_code": execution.returncode, "members": published, "output_tail": _tail(execution.stdout + execution.stderr)}


REMOTE_SET_CHECK_NAME = "remote_set"


def _fetch_remote_head(repo: Path, remote: str, branch: str) -> str:
    """The commit `branch` names on `remote` now, fetched into `repo`. One fetch, so the head named is the head run.

    The fetch lands on a transient staging ref (the pattern check records use) and the ref is removed after; the
    objects stay, and no branch, tag or FETCH_HEAD of the member moves."""
    staging = f"refs/dev.synapt.grip/__remote_head_transfers__/{uuid.uuid4().hex}"
    fetched = gitops.run(repo, "fetch", "--no-tags", "--no-write-fetch-head", remote, f"refs/heads/{branch}:{staging}")
    if fetched.returncode:
        raise check_records.CheckRefused(f"remote_branch_absent: {branch!r} could not be fetched from {remote}")
    head = gitops.run(repo, "rev-parse", "--verify", "-q", f"{staging}^{{commit}}").stdout.strip()
    gitops.run(repo, "update-ref", "-d", staging)
    if not head:
        raise check_records.CheckRefused(f"remote_branch_absent: {branch!r} on {remote} is not a commit")
    return head


def run_set_check_at_remote_heads(workspace: Path, review_id: str, branch: str, argv: Sequence[str]) -> dict:
    """One command over every member of a review at the head `branch` names on that member's remote NOW.

    For the set after a move: the review names WHICH members and where each lives, the remotes name the heads. The
    result is recorded on every member remote as `remote_set` with the heads it ran over. That name is read by
    neither merge gate, so it neither satisfies nor blocks a merge; to merge a moved set, record it (a new pin)."""
    workspace = Path(workspace)
    root = workspace.resolve()
    review_id = review_id.removeprefix("gr:")
    if not argv:
        raise check_records.CheckRefused("check_name_and_command_required")
    # `refs/heads/<b>`, not `--branch <b>`: `--branch` expands `@{-1}` to the root's previous branch and passes it.
    if branch.startswith("-") or gitops.run(root, "check-ref-format", f"refs/heads/{branch}").returncode:
        raise check_records.CheckRefused(f"branch_name_invalid: {branch!r}")
    try:
        view = grip.show_review_commit(workspace, review_id)
    except (grip.GripInitError, grip.GripCorruptError, grip.GripReviewRefused, RuntimeError, OSError, ValueError,
            KeyError, subprocess.SubprocessError) as exc:
        raise check_records.CheckRefused(f"bind_unreadable: {type(exc).__name__}: {exc}") from exc
    review, members = view["id"], view["members"]
    if not members:
        raise check_records.CheckRefused("review_has_no_members")
    repos: dict[str, Path] = {}
    for m in members:
        if not _KEY.fullmatch(m["key"]) or m["key"].endswith(".git"):
            raise check_records.CheckRefused(f"member_key_unsafe: {m['key']!r} cannot name a directory in the lane")
        repo = (workspace / m["path"]).resolve()
        if not repo.is_relative_to(root):
            raise check_records.CheckRefused(f"member_path_outside_workspace: {m['path']}")
        check_records._remote(m["remote"])
        repos[m["key"]] = repo
    heads = {m["key"]: _fetch_remote_head(repos[m["key"]], m["remote"], branch) for m in members}
    with tempfile.TemporaryDirectory(prefix="gr2-remote-set-") as directory:
        lane = Path(directory)
        for key, head in heads.items():
            # The staging ref is gone by now, so `head` is unreferenced in the member. It reaches the lane only because
            # a clone from a LOCAL PATH copies the whole object store; a `file://` url or a `--depth` would drop it.
            cloned = gitops.clone("--quiet", "--no-hardlinks", "--no-checkout", str(repos[key]), str(lane / key), binary=True)
            if cloned.returncode:
                gitops._raise(cloned, ("clone",), check_records.CheckRefused)
            check_records._git(lane / key, "checkout", "--quiet", "--detach", head)
        env = {**os.environ, "GR2_SET_ROOT": str(lane), "GR2_REVIEW_ID": review, "GR2_SET_BRANCH": branch,
               "GR2_SET_MEMBERS": json.dumps({k: str(lane / k) for k in heads}, sort_keys=True)}
        execution = subprocess.run(list(argv), cwd=lane, env=env, capture_output=True)
        after = {k: check_records._git(lane / k, "rev-parse", "HEAD").decode().strip() for k in heads}
    if after != heads:
        raise check_records.CheckRefused("check_execution_head_changed")
    result = "pass" if execution.returncode == 0 else "fail"
    listing = sorted(f"{k}:{h}" for k, h in heads.items())
    published, failed = [], None
    for m in members:
        key, head = m["key"], heads[m["key"]]
        row = dict(v=1, head=head, observed_head=head, name=REMOTE_SET_CHECK_NAME, result=result,
                   exit_code=execution.returncode, review=review, branch=branch, heads=listing)
        try:
            out = check_records.publish_observation(repos[key], m["remote"], row)
        except (check_records.CheckRefused, subprocess.SubprocessError, OSError) as exc:
            failed = (key, str(exc))
            break
        published.append({"key": key, "head": head, "pinned_head": m["head"], "moved": head != m["head"],
                          "record_id": out["record_id"], "observation_id": out["observation"]["observation_id"]})
    if failed:
        raise check_records.CheckRefused(
            f"set_publication_incomplete: published to {[p['key'] for p in published]}, failed at {failed[0]}: {failed[1]}")
    return {"review": review, "branch": branch, "name": REMOTE_SET_CHECK_NAME, "result": result,
            "exit_code": execution.returncode, "members": published, "output_tail": _tail(execution.stdout + execution.stderr)}
