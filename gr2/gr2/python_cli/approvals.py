"""Unsigned, exact-head approval links in ordinary Git refs.

Names are self-declared Git identities, not authenticated identities. Phase one
does not accept signatures. Each link names the bind and repeats its member pins.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import tomllib
import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import grip

PREFIX = "refs/dev.synapt.grip/__approvals__/v1/"
SCHEMA = "dev.synapt.grip.approval.v1alpha1"


class ApprovalRefused(RuntimeError):
    pass


def _git(repo: Path, *args: str, data: str | None = None) -> str:
    p = subprocess.run(["git", "-c", "maintenance.auto=false", "-c", "gc.auto=0", "-C", str(repo), *args],
                       input=data, capture_output=True, text=True, timeout=60)
    if p.returncode:
        raise ApprovalRefused(f"approval_unmeasurable: {p.stderr.strip()}")
    return p.stdout.strip()


def canonical(record: dict) -> str:
    return json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def digest(record: dict) -> str:
    return hashlib.sha256(canonical(record).encode("utf-8")).hexdigest()


def current_review(workspace: Path) -> str:
    """Temporary exact-current-head resolver; never pick the most recent bind."""
    refs = _git(workspace, "for-each-ref", "--format=%(objectname)", "refs/dev.synapt.grip/__reviews__/")
    hits = []
    for commit in sorted(set(refs.splitlines())):
        view = grip.show_review_commit(workspace, commit)
        from . import merge_gate
        root = workspace.resolve()
        if all((repo := (root / m["path"]).resolve()).is_relative_to(root)
               and merge_gate._toplevel(repo) == repo
               and _git(repo, "rev-parse", "HEAD") == m["head"] for m in view["members"]):
            hits.append(commit)
    if len(hits) != 1:
        raise ApprovalRefused(f"approval_bind_not_unique: {len(hits)} binds match the current member heads")
    return hits[0]


def required_approvals(workspace: Path, given: int | None = None) -> int:
    """Workspace grip.toml policy, default zero; an explicit CLI count wins."""
    try:
        policy = tomllib.loads((workspace / "grip.toml").read_text()).get("approvals", {})
    except (OSError, ValueError) as exc:
        raise ApprovalRefused("approval_policy_unreadable") from exc
    if not isinstance(policy, dict):
        raise ApprovalRefused("approval_policy_invalid")
    if policy.get("require_signed"):
        raise ApprovalRefused("approval_signed_unsupported")
    required = given if given is not None else policy.get("required", 0)
    if type(required) is not int or required < 0:
        raise ApprovalRefused("approval_policy_invalid: required must be a nonnegative integer")
    return required


def current_branch(workspace: Path, review_id: str) -> str:
    """Temporary branch resolver until the shared defaults module lands."""
    from . import merge_gate
    view = grip.show_review_commit(workspace, review_id.removeprefix("gr:"))
    names = set()
    root = workspace.resolve()
    for m in view["members"]:
        repo = (root / m["path"]).resolve()
        if not repo.is_relative_to(root) or merge_gate._toplevel(repo) != repo:
            raise ApprovalRefused("approval_member_repo_mismatch")
        name = _git(repo, "symbolic-ref", "--quiet", "--short", "HEAD")
        names.add(name)
    if len(names) != 1:
        raise ApprovalRefused("feature_unresolved: pass --from when members are on different branches")
    return names.pop()


def _context(workspace: Path, review_id: str) -> tuple[dict, set[str]]:
    from . import merge_gate
    rid = review_id.removeprefix("gr:")
    verified = grip.verify_review_commit(workspace, rid)
    if verified.get("tree_matches") is not True:
        raise ApprovalRefused("approval_bind_unverified")
    view = grip.show_review_commit(workspace, rid)
    measured = {m["key"]: m for m in verified["rows"]}
    # Until the review schema adds author, use the bind's own immutable Git author.
    author = view.get("author") or _git(workspace, "show", "-s", "--format=%an", rid)
    authors = {author}
    members = []
    root = workspace.resolve()
    for m in view["members"]:
        repo = (root / m["path"]).resolve()
        if not repo.is_relative_to(root) or merge_gate._toplevel(repo) != repo or not merge_gate._store_inside(repo, root):
            raise ApprovalRefused(f"approval_member_repo_mismatch: {m['key']}")
        tree = measured[m["key"]].get("head_tree")
        if not tree or _git(repo, "rev-parse", f"{m['head']}^{{tree}}") != tree:
            raise ApprovalRefused(f"approval_head_tree_unmeasurable: {m['key']}")
        authors.update(_git(repo, "log", "--format=%an", f"{m['base']}..{m['head']}").splitlines())
        members.append({"key": m["key"], "remote": m["remote"], "head_commit": m["head"], "head_tree": tree})
    return {"review_id": "gr:" + rid, "author": author, "members": members}, authors


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
                _git(workspace, "fetch", "--no-tags", "--no-write-fetch-head", "--", remote, f"{ref}:{temporary}")
                if _git(workspace, "rev-parse", temporary) != tip:
                    raise ApprovalRefused("approval_chain_moved")
            finally:
                _git(workspace, "update-ref", "-d", temporary)
    return tips


def _walk(workspace: Path, root: dict, authors: set[str], tip: str | None) -> list[dict]:
    rid = root["review_id"].removeprefix("gr:")
    links = []
    seen = set()
    while tip and tip != rid:
        if tip in seen or len(seen) >= 10000:
            raise ApprovalRefused("approval_chain_broken: cycle or excessive length")
        seen.add(tip)
        parents = _git(workspace, "rev-list", "--parents", "-n", "1", tip).split()[1:]
        if len(parents) != 1:
            raise ApprovalRefused("approval_chain_broken: expected one parent")
        text = _git(workspace, "show", f"{tip}:approval.json")
        try:
            record = json.loads(text)
        except ValueError as exc:
            raise ApprovalRefused("approval_chain_broken: invalid JSON") from exc
        if not isinstance(record, dict) or text != canonical(record):
            raise ApprovalRefused("approval_chain_broken: not canonical")
        if record.get("schema") != SCHEMA or record.get("verdict") != "approve":
            raise ApprovalRefused("approval_chain_broken: schema or verdict")
        if any(record.get(k) != root[k] for k in ("review_id", "author", "members")):
            raise ApprovalRefused("approval_chain_broken: bind or head pins")
        identity = record.get("approver", {})
        name = identity.get("name") if isinstance(identity, dict) else None
        if not isinstance(name, str) or not name.strip():
            raise ApprovalRefused("approval_chain_broken: missing approver")
        if name in authors:
            raise ApprovalRefused("self_approval")
        if record.get("sig") or identity.get("key_id") or record.get("keyring_tip"):
            raise ApprovalRefused("approval_signed_unsupported")
        previous = root if parents[0] == rid else json.loads(_git(workspace, "show", f"{parents[0]}:approval.json"))
        if record.get("prev") != digest(previous):
            raise ApprovalRefused("approval_chain_broken: prev")
        links.append({"approver": name, "key_id": "", "record_hash": digest(record), "commit": tip, "signed": False})
        tip = parents[0]
    if links and tip != rid:
        raise ApprovalRefused("approval_chain_broken: not rooted at bind")
    return list(reversed(links))


def count_approvals(workspace: Path, review_id: str) -> dict:
    root, authors = _context(workspace, review_id)
    tips = _remote_tips(workspace, root)
    if len(set(tips.values())) != 1:
        raise ApprovalRefused("approval_chain_divergent: " + canonical(tips))
    tip = next(iter(tips.values()))
    links = _walk(workspace, root, authors, tip)
    distinct = {link["approver"]: link for link in links}
    return {"review_id": root["review_id"], "tip": tip, "count": len(distinct), "links": list(distinct.values()), "signed": False}


def approve(workspace: Path, review_id: str | None = None) -> dict:
    workspace = workspace.resolve()
    rid = review_id.removeprefix("gr:") if review_id else current_review(workspace)
    root, authors = _context(workspace, rid)
    name = _git(Path.cwd(), "config", "user.name")
    if not name.strip():
        raise ApprovalRefused("approval_identity_missing")
    if name in authors:
        raise ApprovalRefused("self_approval")
    tips = _remote_tips(workspace, root)
    if len(set(tips.values())) != 1:
        raise ApprovalRefused("approval_chain_divergent: " + canonical(tips))
    tip = next(iter(tips.values()))
    _walk(workspace, root, authors, tip)
    previous = root if tip is None else json.loads(_git(workspace, "show", f"{tip}:approval.json"))
    record = {**root, "schema": SCHEMA, "approver": {"name": name, "key_id": ""}, "keyring_tip": "",
              "verdict": "approve", "prev": digest(previous),
              "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")}
    blob = _git(workspace, "hash-object", "-w", "--stdin", data=canonical(record))
    tree = _git(workspace, "mktree", data=f"100644 blob {blob}\tapproval.json\n")
    commit = _git(workspace, "commit-tree", tree, "-p", tip or rid, "-m", "Approve " + root["review_id"])
    ref = PREFIX + rid
    for remote, old in tips.items():
        _git(workspace, "push", f"--force-with-lease={ref}:{old or ''}", "--", remote, f"{commit}:{ref}")
    _git(workspace, "update-ref", ref, commit)
    return count_approvals(workspace, rid)
