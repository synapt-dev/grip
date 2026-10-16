"""Unsigned, exact-head approval links in ordinary Git refs.

Names are self-declared Git identities, not authenticated identities. Unsigned
links do not accept signature fields. Each link names the bind and repeats its member pins.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
import subprocess
import tomllib
import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import approval_schema, gitops, grip

PREFIX = "refs/dev.synapt.grip/__approvals__/v1/"
SCHEMA = approval_schema.PACKAGE


class ApprovalRefused(RuntimeError):
    """A named refusal. `next_step`, when set, is the printed next line for the operator."""

    def __init__(self, message: str, next_step: str | None = None) -> None:
        super().__init__(message)
        self.next_step = next_step


def _git(repo: Path, *args: str, data: str | None = None, raw: bool = False) -> str:
    try:
        p = gitops.run(repo, *args, input=data, timeout=60, raise_timeout=True)
    except (OSError, subprocess.SubprocessError, UnicodeError) as exc:
        raise ApprovalRefused(f"approval_unmeasurable: {type(exc).__name__}") from exc
    if p.returncode:
        raise ApprovalRefused(f"approval_unmeasurable: {p.stderr.strip()}")
    return p.stdout if raw else p.stdout.strip()


def canonical(record: dict) -> str:
    return json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def digest(record: dict) -> str:
    return hashlib.sha256(canonical(record).encode("utf-8")).hexdigest()


def _bind_record(workspace: Path, rid: str) -> dict:
    """Lossless canonical JSON view of the full bind, not its decoded pin view.

    Field occurrences retain numeric/wire identity and order. Raw payloads use
    base64, including nested message bytes and unknown fields at every depth.
    Writer field labels are not identities. The verified field tree supplies
    its protobuf bytes without decoding away fields unknown to this version.
    """
    from . import review_field_tree as fd
    tree = _git(workspace, "rev-parse", rid + "^{tree}")
    try:
        wire = fd.to_protobuf(workspace, tree)
        occurrences = [{"number": n, "wire_type": wt,
                        "payload": base64.b64encode(payload).decode("ascii")}
                       for n, wt, payload in fd.fields(wire)]
    except (fd.ReviewRecordError, UnicodeError, ValueError) as exc:
        raise ApprovalRefused("approval_bind_record_unmeasurable") from exc
    return {"format": "protobuf-fields-v1", "schema": fd.PACKAGE, "fields": occurrences}


def current_review(workspace: Path) -> str:
    """Use the shared exact-current-head resolver for the default approval bind."""
    from . import defaults
    root = workspace.resolve()
    refs = _git(workspace, "for-each-ref", "--format=%(objectname)", "refs/dev.synapt.grip/__reviews__/")

    def members_of(commit):
        view = grip.show_review_commit(workspace, commit)
        members = [(root / m["path"], m["head"]) for m in view["members"]]
        if any(not path.resolve().is_relative_to(root) for path, _ in members):
            raise ApprovalRefused("approval_member_repo_mismatch")
        return members

    try:
        rid, _ = defaults.review(sorted(set(refs.splitlines())), members_of, defaults.current_head)
        return rid
    except defaults.Unresolved as exc:
        raise ApprovalRefused(f"approval_bind_not_unique: {exc}") from exc


def approval_policy(workspace: Path, given: int | None = None) -> dict:
    """The approval count a merge requires, and where it came from.

    grip.toml `[approvals] required` is a floor; `--approvals N` can raise it. When NEITHER is given the merge
    refuses with `approvals_unset` rather than merging on zero approvals: zero is a choice someone makes
    (`required = 0`, or `--approvals 0`), never a default nobody saw."""
    try:
        policy = tomllib.loads((workspace / "grip.toml").read_text()).get("approvals", {})
    except FileNotFoundError:
        policy = {}
    except (OSError, ValueError) as exc:
        raise ApprovalRefused("approval_policy_unreadable") from exc
    if not isinstance(policy, dict):
        raise ApprovalRefused("approval_policy_invalid")
    if "require_signed" in policy and type(policy["require_signed"]) is not bool:
        raise ApprovalRefused("approval_policy_invalid: require_signed must be a boolean")
    if policy.get("require_signed"):
        raise ApprovalRefused("approval_signed_unsupported")
    configured = "required" in policy
    required = policy.get("required", 0)
    if type(required) is not int or required < 0:
        raise ApprovalRefused("approval_policy_invalid: required must be a nonnegative integer")
    if given is not None and (type(given) is not int or given < 0):
        raise ApprovalRefused("approval_policy_invalid: requested count must be a nonnegative integer")
    if not configured and given is None:
        raise ApprovalRefused("approvals_unset: grip.toml has no [approvals] required and no --approvals was given")
    source = [name for name, present in (("grip.toml", configured), ("--approvals", given is not None)) if present]
    return {"required": max(required, given or 0), "source": " and ".join(source)}


def required_approvals(workspace: Path, given: int | None = None) -> int:
    """The required count alone; refuses `approvals_unset` as `approval_policy` does."""
    return approval_policy(workspace, given)["required"]


def review_open_lane(lane: Path) -> tuple[Path, str, dict[str, Path]] | None:
    """A lane `review open` made: (the store workspace it was opened from, the review id, each member's checkout).

    A reviewer in another gripspace has no member clone in their own workspace; the lane is the only checkout of
    the reviewed repos, so stamp reads the members there and the store from the workspace the marker names.
    A member directory of a multi-member lane (`<lane>/<key>`) resolves to its lane. None when neither holds a
    review-open marker."""
    from . import review_run
    lane = Path(lane).resolve()
    path = review_run.find_marker(lane)
    if path is None:
        parent = review_run.find_marker(lane.parent)
        if parent is None:
            return None
        found = review_open_lane(lane.parent)
        return found if found is not None and found[2].get(lane.name) == lane else None
    try:
        marker = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise ApprovalRefused(f"approval_review_record_unreadable: {path}") from exc
    rows = marker.get("repos") if isinstance(marker, dict) else None
    if (not review_run.marker_kind_ok(marker) or not isinstance(marker.get("gr_commit"), str)
            or not isinstance(rows, list) or not rows
            or not all(isinstance(r, dict) and isinstance(r.get("key"), str) for r in rows)):
        raise ApprovalRefused(f"approval_review_record_unreadable: {path}")
    if not isinstance(marker.get("workspace_root"), str):
        raise ApprovalRefused(f"approval_lane_names_no_workspace: {lane} was opened by an older gr2 and does not name "
                              "the workspace it was opened from",
                              next_step=f"next: open the review again, then stamp the new lane: "
                                        f"gr2 review open <your-workspace> gr:{marker['gr_commit']} --lane-dir <new-lane>, "
                                        "then gr2 review stamp <new-lane>")
    if not Path(marker["workspace_root"]).is_absolute():
        raise ApprovalRefused(f"approval_lane_workspace_not_absolute: {lane} names the workspace "
                              f"{marker['workspace_root']!r}, which is not absolute and would resolve against the "
                              "current directory; review open writes it absolute, so this marker was edited by hand or written elsewhere",
                              next_step=f"next: open the review again, then stamp the new lane: "
                                        f"gr2 review open <your-workspace> gr:{marker['gr_commit']} --lane-dir <new-lane>, "
                                        "then gr2 review stamp <new-lane>")
    try:
        members = ({rows[0]["key"]: lane} if len(rows) == 1
                   else {r["key"]: review_run._member_dir(lane, r["key"]) for r in rows})
    except review_run.ReviewRunRefused as exc:
        raise ApprovalRefused(f"approval_review_record_unreadable: {exc.detail}") from exc
    return Path(marker["workspace_root"]).resolve(), marker["gr_commit"], members


def _context(workspace: Path, review_id: str, member_dirs: dict[str, Path] | None = None) -> tuple[dict, set[str]]:
    """`member_dirs` (key -> checkout) reads the members from a review lane instead of the workspace's own clones."""
    from . import merge_gate
    workspace = workspace.resolve()
    if merge_gate._toplevel(workspace) != workspace or not merge_gate._store_inside(workspace, workspace):
        raise ApprovalRefused("approval_workspace_store_outside_workspace")
    rid = review_id.removeprefix("gr:")
    verified = grip.verify_review_commit(workspace, rid)
    if verified.get("tree_matches") is not True:
        raise ApprovalRefused("approval_bind_unverified")
    view = grip.show_review_commit(workspace, rid)
    measured = {m["key"]: m for m in verified["rows"]}
    # Older binds have no author field; their immutable Git author is the fallback.
    author = view.get("author") or _git(workspace, "show", "-s", "--format=%an", rid)
    authors = {author}
    members = []
    root = workspace.resolve()
    for m in view["members"]:
        if member_dirs is not None:
            repo = member_dirs.get(m["key"])
            if repo is None:
                keys = [r["key"] for r in view["members"]]
                raise ApprovalRefused(
                    f"approval_lane_partial: this lane holds {', '.join(sorted(member_dirs))}; the review has "
                    f"{', '.join(keys)}",
                    next_step=f"next: open the whole review and stamp that lane: gr2 review open {workspace} "
                              f"gr:{rid} --lane-dir <new-lane>, then gr2 review stamp <new-lane>")
            if merge_gate._toplevel(repo) != repo.resolve():
                raise ApprovalRefused(f"approval_member_repo_mismatch: {m['key']}")
            repo = repo.resolve()
        else:
            repo = (root / m["path"]).resolve()
            if not repo.is_relative_to(root) or merge_gate._toplevel(repo) != repo or not merge_gate._store_inside(repo, root):
                raise ApprovalRefused(f"approval_member_repo_mismatch: {m['key']}")
        # A reviewer's clone made before the head was pushed does not hold it, so the exact reviewed head is
        # fetched from the member remote the review names (the remote the approval chain is read from below).
        # Only when that remote cannot serve it: a named refusal with the next step, not a raw `unknown revision`.
        if gitops.run(repo, "cat-file", "-e", f"{m['head']}^{{commit}}").returncode:
            gitops.run(repo, "fetch", "-q", "--no-tags", "--no-write-fetch-head", "--", m["remote"], m["head"],
                       timeout=60)
        try:
            _git(repo, "cat-file", "-t", f"{m['head']}^{{commit}}")
        except ApprovalRefused:
            from . import next_steps
            raise ApprovalRefused(f"head_not_fetched: {m['key']}",
                                  next_steps.head_not_fetched(str(repo), m["remote"], m["head"], str(workspace),
                                                              "gr:" + rid)) from None
        actual_tree = _git(repo, "rev-parse", f"{m['head']}^{{tree}}")
        tree = measured[m["key"]].get("head_tree", actual_tree)
        if actual_tree != tree:
            raise ApprovalRefused(f"approval_head_tree_unmeasurable: {m['key']}")
        authors.update(_git(repo, "log", "--format=%an", f"{m['base']}..{m['head']}").splitlines())
        members.append({"key": m["key"], "remote": m["remote"], "head_commit": m["head"], "head_tree": tree})
    # Every local refusal comes first; then one remote read per member, before any approval is pushed.
    for m in view["members"]:
        _refuse_moved_base(workspace, m["key"], m["remote"], m["base"], "gr:" + rid)
    return {"review_id": "gr:" + rid, "author": author, "members": members}, authors


def _refuse_moved_base(workspace: Path, key: str, remote: str, base: str, review: str) -> None:
    """Refuse a stamp the merge could never count: a merge needs its target at the reviewed base, so when no branch
    on the member's remote still sits at that base, the target has moved and the review needs a new pin. Read
    before any approval is pushed. Any branch at the base passes (a review may target a branch other than the
    default), so a stale branch left at the base lets the stamp through; the merge still refuses that case."""
    if remote.startswith("-") or any(ord(c) < 32 or ord(c) == 127 for c in remote):
        raise ApprovalRefused("approval_remote_invalid")
    default, at_base, tips = None, False, {}
    for line in _git(workspace, "ls-remote", "--symref", "--", remote, "HEAD", "refs/heads/*").splitlines():
        left, _, ref = line.partition("\t")
        if left.startswith("ref: refs/heads/") and ref == "HEAD":
            default = left.removeprefix("ref: refs/heads/")
        elif ref.startswith("refs/heads/"):
            tips[ref.removeprefix("refs/heads/")] = left
            at_base = at_base or left == base
    if at_base:
        return
    from . import next_steps
    branch = default or "<target branch>"
    now = f" ({branch} is now {tips[branch][:12]})" if branch in tips else ""
    raise ApprovalRefused(f"base_moved: {key}: no branch on its remote is at the reviewed base {base[:12]}{now}",
                          next_steps._stamp_base_moved(branch, review))


def _remote_tips(workspace: Path, root: dict) -> dict[str, str | None]:
    ref = PREFIX + root["review_id"].removeprefix("gr:")
    tips = {}
    for m in root["members"]:
        remote = m["remote"]
        if remote.startswith("-") or any(ord(c) < 32 or ord(c) == 127 for c in remote):
            raise ApprovalRefused("approval_remote_invalid")
        lines = _git(workspace, "ls-remote", "--", remote, ref).splitlines()
        hits = [line.split("\t")[0] for line in lines if line.split("\t")[-1] == ref]
        if len(hits) > 1:
            raise ApprovalRefused("approval_chain_unmeasurable")
        tip = hits[0] if hits else None
        tips[remote] = tip
        if tip:
            temporary = "refs/dev.synapt.grip/__approval_transfers__/" + uuid.uuid4().hex
            try:
                _git(workspace, "fetch", "--no-tags", "--refmap=", "--no-write-fetch-head", "--", remote, f"{ref}:{temporary}")
                if _git(workspace, "rev-parse", temporary) != tip:
                    raise ApprovalRefused("approval_chain_moved")
            finally:
                _git(workspace, "update-ref", "-d", temporary)
    return tips


def _unique_pairs(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise ApprovalRefused("approval_chain_broken: duplicate JSON key")
        out[key] = value
    return out


def _read_link(workspace: Path, commit: str) -> dict:
    entries = _git(workspace, "ls-tree", commit).splitlines()
    if len(entries) != 1 or not re.fullmatch(r"100644 blob [0-9a-f]{40}\tapproval\.json", entries[0]):
        raise ApprovalRefused("approval_chain_broken: expected only plain approval.json")
    text = _git(workspace, "show", f"{commit}:approval.json", raw=True)
    try:
        record = json.loads(text, object_pairs_hook=_unique_pairs)
        if not isinstance(record, dict) or text != canonical(record):
            raise ApprovalRefused("approval_chain_broken: not canonical")
    except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
        raise ApprovalRefused("approval_chain_broken: invalid JSON") from exc
    return record


def _validate_link(record: dict, root: dict, authors: set[str]) -> str:
    required = {"schema", "review_id", "members", "approver", "author", "verdict", "prev", "created_at"}
    if not required.issubset(record) or set(record) - required - {"sig", "keyring_tip"}:
        raise ApprovalRefused("approval_chain_broken: missing or unknown fields")
    if record["schema"] != SCHEMA or record["verdict"] != "approve":
        raise ApprovalRefused("approval_chain_broken: schema or verdict")
    if any(record[k] != root[k] for k in ("review_id", "author", "members")):
        raise ApprovalRefused("approval_chain_broken: bind or head pins")
    identity = record["approver"]
    if not isinstance(identity, dict) or set(identity) != {"name", "key_id"}:
        raise ApprovalRefused("approval_chain_broken: approver fields")
    name = identity["name"]
    if not isinstance(name, str) or not name or name != name.strip() or any(ord(c) < 32 or ord(c) == 127 for c in name):
        raise ApprovalRefused("approval_chain_broken: invalid approver name")
    if name in authors:
        raise ApprovalRefused("self_approval")
    if "sig" in record or identity["key_id"] != "" or record.get("keyring_tip", "") != "":
        raise ApprovalRefused("approval_signed_unsupported")
    if not isinstance(record["prev"], str) or not re.fullmatch(r"[0-9a-f]{64}", record["prev"]):
        raise ApprovalRefused("approval_chain_broken: invalid prev digest")
    stamp = record["created_at"]
    if not isinstance(stamp, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]+)?Z", stamp):
        raise ApprovalRefused("approval_chain_broken: invalid created_at")
    try:
        datetime.fromisoformat(stamp.removesuffix("Z") + "+00:00")
    except ValueError as exc:
        raise ApprovalRefused("approval_chain_broken: invalid created_at") from exc
    return name


def _walk(workspace: Path, root: dict, authors: set[str], tip: str | None) -> list[dict]:
    rid = root["review_id"].removeprefix("gr:")
    if tip == rid:
        raise ApprovalRefused("approval_chain_broken: approval ref points at bind")
    links = []
    seen = set()
    while tip and tip != rid:
        if len(seen) >= 10000:
            raise ApprovalRefused("approval_chain_broken: excessive length")
        seen.add(tip)
        parents = _git(workspace, "rev-list", "--parents", "-n", "1", tip).split()[1:]
        if len(parents) != 1:
            raise ApprovalRefused("approval_chain_broken: expected one parent")
        record = _read_link(workspace, tip)
        name = _validate_link(record, root, authors)
        previous = _bind_record(workspace, rid) if parents[0] == rid else _read_link(workspace, parents[0])
        if record.get("prev") != digest(previous):
            raise ApprovalRefused("approval_chain_broken: prev")
        links.append({"approver": name, "key_id": "", "record_hash": digest(record), "commit": tip, "signed": False})
        tip = parents[0]
    return list(reversed(links))


def _reconcile(workspace: Path, root: dict, authors: set[str], tips: dict[str, str | None]) -> str | None:
    """Republish a linear descendant to lagging remotes; never pick a side of a fork."""
    candidates = set(tips.values()) - {None}
    for tip in candidates:
        _walk(workspace, root, authors, tip)
    if not candidates:
        return None
    longest = []
    for candidate in candidates:
        ancestors = set(_git(workspace, "rev-list", candidate).splitlines())
        if all(tip is None or tip in ancestors for tip in tips.values()):
            longest.append(candidate)
    if len(longest) != 1:
        raise ApprovalRefused("approval_chain_divergent: " + canonical(tips))
    tip = longest[0]
    ref = PREFIX + root["review_id"].removeprefix("gr:")
    for remote, old in tips.items():
        if old != tip:
            _git(workspace, "push", f"--force-with-lease={ref}:{old or ''}", "--", remote, f"{tip}:{ref}")
    measured = _remote_tips(workspace, root)
    if set(measured.values()) != {tip}:
        raise ApprovalRefused("approval_chain_divergent: " + canonical(measured))
    return tip


def count_approvals(workspace: Path, review_id: str, member_dirs: dict[str, Path] | None = None) -> dict:
    root, authors = _context(workspace, review_id, member_dirs)
    tips = _remote_tips(workspace, root)
    if len(set(tips.values())) != 1:
        raise ApprovalRefused("approval_chain_divergent: " + canonical(tips))
    tip = next(iter(tips.values()))
    links = _walk(workspace, root, authors, tip)
    current = _remote_tips(workspace, root)
    if current != tips:
        raise ApprovalRefused("approval_chain_divergent: " + canonical(current) if len(set(current.values())) != 1
                              else "approval_chain_moved: retry the measurement")
    distinct = {link["approver"]: link for link in links}
    return {"review_id": root["review_id"], "tip": tip, "count": len(distinct), "links": list(distinct.values()), "signed": False}


def approve(workspace: Path, review_id: str | None = None, member_dirs: dict[str, Path] | None = None) -> dict:
    workspace = workspace.resolve()
    rid = review_id.removeprefix("gr:") if review_id else current_review(workspace)
    root, authors = _context(workspace, rid, member_dirs)
    name = _git(workspace, "config", "user.name")
    if not name.strip():
        raise ApprovalRefused("approval_identity_missing")
    if name in authors:
        raise ApprovalRefused("self_approval")
    tips = _remote_tips(workspace, root)
    tip = _reconcile(workspace, root, authors, tips)
    _walk(workspace, root, authors, tip)
    previous = _bind_record(workspace, rid) if tip is None else _read_link(workspace, tip)
    record = {**root, "schema": SCHEMA, "approver": {"name": name, "key_id": ""}, "keyring_tip": "",
              "verdict": "approve", "prev": digest(previous),
              "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")}
    _validate_link(record, root, authors)
    blob = _git(workspace, "hash-object", "-w", "--stdin", data=canonical(record))
    tree = _git(workspace, "mktree", data=f"100644 blob {blob}\tapproval.json\n")
    commit = _git(workspace, "commit-tree", tree, "-p", tip or rid, "-m", "Approve " + root["review_id"])
    ref = PREFIX + rid
    for remote in tips:
        old = tip
        _git(workspace, "push", f"--force-with-lease={ref}:{old or ''}", "--", remote, f"{commit}:{ref}")
    _git(workspace, "update-ref", ref, commit)
    return count_approvals(workspace, rid, member_dirs)


# ---- a PR-head review: the lane `review checkout-pr` (or `review open`) made, stamped by its (repo, head) record
#
# The approval is a commit chain under PREFIX + "pr/<head>" in the WORKSPACE'S OWN CLONE of the reviewed repo (the
# member checkout the PR was fetched into): local, never pushed, and it outlives the lane, which `review close`
# drops. A workspace a checkout-pr lane lives in need not have a root store, so the member clone is the store.
# As for bind approvals, the approver is the self-declared Git user.name; who wrote the PR is not checked here.

PR_LANE_SCHEMA = "gr2.pr-lane-approval.v1"
PR_PREFIX = PREFIX + "pr/"


def pr_lane_record(lane: Path) -> dict | None:
    """The (repo, base, head, lane_kind) record of the review lane at `lane`, or None when `lane` is not one."""
    from .review_records import ReviewRecordLocationError, worktree_git_path
    if not (Path(lane) / ".git").exists():
        return None
    try:
        path = worktree_git_path(lane, "grip-review.json")
    except ReviewRecordLocationError:
        return None
    if not path.is_file():
        return None
    try:
        record = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise ApprovalRefused(f"approval_review_record_unreadable: {path}") from exc
    if not isinstance(record, dict) or not all(isinstance(record.get(k), str) for k in ("repo", "base", "head")):
        raise ApprovalRefused(f"approval_review_record_unreadable: {path}")
    return record


def pr_lane_store(lane: Path) -> Path:
    """The workspace member clone a review lane was made from: the nearest enclosing workspace's repo named as
    the lane's repo directory. Refused, naming the lane, when there is none."""
    from ..prototypes import lane_workspace_prototype as lane_proto
    lane = Path(lane).resolve()
    from .layout import grip_dir
    ws = next((p for p in lane.parents if (grip_dir(p) / "workspace_spec.toml").is_file()), None)
    if ws is None:
        raise ApprovalRefused(f"approval_no_workspace: no .grip/workspace_spec.toml encloses {lane}")
    for repo in lane_proto.load_workspace_spec(ws).get("repos", []):
        if repo.get("name") == lane.name:
            source = Path(str(repo.get("path", "")))
            source = (source if source.is_absolute() else ws / source).resolve()
            if source == lane or not (source / ".git").exists():
                break
            return source
    raise ApprovalRefused(f"approval_no_member_clone: the workspace {ws} has no clone of repo {lane.name!r} to hold the approval")


def pr_head_approvals(store: Path, repo: str, head: str) -> dict:
    """The approvals of (repo, head) held in `store`, the workspace member clone. Readable after the lane is gone."""
    ref = PR_PREFIX + head
    tip = gitops.git(Path(store), "rev-parse", "--verify", "-q", ref + "^{commit}").stdout.strip()
    links = []
    if tip:
        for commit in _git(store, "rev-list", tip).splitlines():
            try:
                link = json.loads(_git(store, "show", f"{commit}:approval.json"))
            except ValueError as exc:
                raise ApprovalRefused(f"approval_record_unreadable: {ref} at {commit}") from exc
            if link.get("repo") == repo and link.get("head") == head:
                links.append(link)
    links.reverse()
    return {"repo": repo, "head": head, "tip": tip or None,
            "count": len({link["approver"]["name"] for link in links}), "links": links, "signed": False}


def pr_lane_approvals(lane: Path) -> dict:
    """The approvals bound to the lane's review record (repo, head)."""
    lane = Path(lane).resolve()
    record = pr_lane_record(lane)
    if record is None:
        raise ApprovalRefused(f"approval_not_a_review_lane: {lane} holds no review record")
    return {**pr_head_approvals(pr_lane_store(lane), record["repo"], record["head"]), "base": record["base"]}


def approve_pr_lane(lane: Path) -> dict:
    """Stamp a PR-head review: an unsigned approval as git user.name, bound to the lane record's (repo, head).
    A lane whose HEAD is not the recorded head is refused, so an approval never lands on bytes nobody read."""
    lane = Path(lane).resolve()
    record = pr_lane_record(lane)
    if record is None:
        raise ApprovalRefused(f"approval_not_a_review_lane: {lane} holds no review record")
    head = gitops.git(lane, "rev-parse", "HEAD")
    actual = head.stdout.strip() if head.returncode == 0 else ""
    if actual != record["head"]:
        raise ApprovalRefused(
            f"approval_head_moved: lane {lane} is at {actual or '(unreadable)'!s}, the review record's head is "
            f"{record['head']}; nothing was stamped",
            next_step="close this lane with `gr2 review close` and check the PR out again, then stamp the new head")
    name = gitops.git(lane, "config", "user.name").stdout.strip()
    if not name:
        raise ApprovalRefused("approval_identity_missing")
    store = pr_lane_store(lane)
    current = pr_head_approvals(store, record["repo"], record["head"])
    if name not in {link["approver"]["name"] for link in current["links"]}:
        link = {"schema": PR_LANE_SCHEMA, "repo": record["repo"], "base": record["base"], "head": record["head"],
                "approver": {"name": name, "key_id": ""}, "verdict": "approve",
                "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")}
        blob = _git(store, "hash-object", "-w", "--stdin", data=canonical(link))
        tree = _git(store, "mktree", data=f"100644 blob {blob}\tapproval.json\n")
        parent = ["-p", current["tip"]] if current["tip"] else []
        commit = _git(store, "-c", f"user.name={name}", "-c", "user.email=gr2@localhost",
                      "commit-tree", tree, *parent, "-m", f"Approve {record['repo']} {record['head']}")
        # compare-and-swap on the tip read above, so two stamps at once cannot drop one
        _git(store, "update-ref", PR_PREFIX + record["head"], commit, current["tip"] or "0" * 40)
    return pr_lane_approvals(lane)
