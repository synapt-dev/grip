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


def required_approvals(workspace: Path, given: int | None = None) -> int:
    """Workspace policy is a floor, default zero; a CLI count can raise it."""
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
    required = policy.get("required", 0)
    if type(required) is not int or required < 0:
        raise ApprovalRefused("approval_policy_invalid: required must be a nonnegative integer")
    if given is not None and (type(given) is not int or given < 0):
        raise ApprovalRefused("approval_policy_invalid: requested count must be a nonnegative integer")
    return max(required, given or 0)


def _context(workspace: Path, review_id: str) -> tuple[dict, set[str]]:
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
        repo = (root / m["path"]).resolve()
        if not repo.is_relative_to(root) or merge_gate._toplevel(repo) != repo or not merge_gate._store_inside(repo, root):
            raise ApprovalRefused(f"approval_member_repo_mismatch: {m['key']}")
        # A reviewer's clone made before the head was pushed does not hold it: a named refusal with the
        # fetch that fixes it, not a raw `unknown revision` from the rev-parse below.
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


def count_approvals(workspace: Path, review_id: str) -> dict:
    root, authors = _context(workspace, review_id)
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


def approve(workspace: Path, review_id: str | None = None) -> dict:
    workspace = workspace.resolve()
    rid = review_id.removeprefix("gr:") if review_id else current_review(workspace)
    root, authors = _context(workspace, rid)
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
    return count_approvals(workspace, rid)
