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
from pathlib import Path
from typing import Mapping, Sequence

from . import check_records, grip

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
        repo = (workspace / m["path"]).resolve()
        if not repo.is_relative_to(root):
            raise check_records.CheckRefused(f"member_path_outside_workspace: {m['path']}")
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
