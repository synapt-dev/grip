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
import re
import subprocess
import uuid
from pathlib import Path
from typing import Mapping, Sequence

from . import check_records, declared_sets, gitops, grip, merge_receipts, next_steps, set_check

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


def _url_key(url: str, base: Path) -> tuple[str, str]:
    """One repository, however its URL is spelled: (host, path) for a network URL, ("", realpath) for a local one.

    A `file://` scheme, a trailing '/', and a trailing '.git' on a network path do not change the repository, and
    neither does the user or port in front of the host, so an SSH spelling (`git@host:org/x.git`, `ssh://...`) and
    an HTTPS one (`https://host/org/x`) of the same repository give the same key. A relative local path resolves
    against `base`, the clone, which is where git resolves it."""
    u = url.strip()
    if u.startswith("file://"):
        u = u[len("file://"):]
    if "://" in u:
        hostpart, _, path = u.split("://", 1)[1].partition("/")
        return hostpart.rsplit("@", 1)[-1].split(":", 1)[0].lower(), path.strip("/").removesuffix(".git")
    head, sep, tail = u.partition(":")
    if sep and head and "/" not in head:  # git's scp-like form: a colon before any slash
        return head.rsplit("@", 1)[-1].lower(), tail.strip("/").removesuffix(".git")
    return "", os.path.realpath(u if os.path.isabs(u) else base / u)


def _refresh_tracking(repo: Path, remote: str, into: str) -> dict | None:
    """Bring the clone's own remote-tracking ref for `into` up to the remote after a merge; None, or the miss as
    {command, reason}. The merge was pushed by URL, so the clone's tracking ref did not move, and a `gr2 branch <into>
    --base <name>/<into>` followed by `store commit` recorded the PRE-merge pin with exit 0. The clone's remote is
    found by URL compared as one repository (`_url_key`), not as one string, and fetched by name with an explicit
    refspec (a narrowed clone has no refspec for it), never by sha. Only a clone with no remotes at all has no
    tracking ref to leave behind; one whose remotes all name other URLs is listed, never passed over."""
    names = _git(repo, "remote", check=False).split()
    if not names:
        return None
    want = _url_key(remote, repo)
    urls = {n: _git(repo, "remote", "get-url", n, check=False) for n in names}
    name = next((n for n in names if urls[n] and _url_key(urls[n], repo) == want), None)
    if name is None:
        target = "origin" if "origin" in names else names[0]
        return {"command": f"git -C {repo} fetch --no-tags {remote} +refs/heads/{into}:refs/remotes/{target}/{into}",
                "reason": f"no remote of this clone names {remote}"}
    spec = f"+refs/heads/{into}:refs/remotes/{name}/{into}"
    proc = gitops.run(repo, "fetch", "--no-tags", "-q", name, spec, timeout=120)
    return None if proc.returncode == 0 else {"command": f"git -C {repo} fetch --no-tags {name} {spec}",
                                              "reason": "the fetch failed"}


_REVIEW_REF_PREFIX = "refs/dev.synapt.grip/__reviews__/"
_REVIEW_SCAN_CAP = 200  # reviews read per member remote; over it is a named refusal, never a silent partial read


def _pin_tie(root: Path, declaration: declared_sets.Declaration, into: str):
    """The tie for the OTHER members of a declared set: `tie(member, pin)` returns None when that pin is merged into
    the member's target on its remote, or when a review published on that remote names the member at exactly that
    head; otherwise the named refusal.

    The set record vouches for the pins a root commit records, but in a compat workspace that commit holds a gitlink,
    not the member's content, so a pin that exists only in a clone, or was pushed and never read, passes the record
    without anyone having read it. Reviewed means a published review names this head, not that the commit exists."""
    def tie(name: str, pin: str) -> str | None:
        info = declaration.members[name]
        repo, remote, short = (root / info["path"]).resolve(), info["remote"], pin[:12]
        if not remote or not repo.is_relative_to(root) or _toplevel(repo) != repo or not _store_inside(repo, root):
            return (f"declared_set_pin_unmeasured: {name} has no readable repository and remote here, so its pin "
                    f"{short} cannot be tied to a merged or reviewed head")
        try:
            tip = _remote_tip(repo, remote, into)
            if tip and gitops.run(repo, "merge-base", "--is-ancestor", pin, tip).returncode == 0:
                return None
            refs = [line.split("\t")[1] for line in _git(repo, "ls-remote", remote, f"{_REVIEW_REF_PREFIX}*").splitlines()
                    if "\t" in line and line.split("\t")[1].startswith(_REVIEW_REF_PREFIX)]
            if len(refs) > _REVIEW_SCAN_CAP:
                return (f"declared_set_pin_unmeasured: {name}'s remote advertises {len(refs)} reviews, over the "
                        f"{_REVIEW_SCAN_CAP} this gate reads; its pin {short} cannot be tied to a reviewed head")
            for ref in refs:
                staging = f"refs/dev.synapt.grip/__merge_transfers__/{uuid.uuid4().hex}"
                try:
                    _git(repo, "fetch", "--no-tags", "--no-write-fetch-head", remote, f"{ref}:{staging}")
                    state = grip._read_repo_state(repo, staging, bind=True)
                finally:
                    _git(repo, "update-ref", "-d", staging, check=False)
                for key, fields in state.items():
                    if fields.get("commit") == pin and declaration.member_name(key, fields.get("path", "")) == name:
                        return None
        except (RuntimeError, subprocess.SubprocessError, OSError, grip.GripCorruptError) as exc:
            return f"declared_set_pin_unmeasured: {name}'s pin {short} could not be read from its remote: {exc}"
        return (f"declared_set_pin_unreviewed: {name} is pinned at {short}, which is neither merged into {into} nor "
                f"named by a review published on its remote")
    return tie


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


MEMBER_CHECK_NAME = "test"  # the per-member check a review over several members may require beside `set`


class RequirementRefused(ValueError):
    """The merge asked to lower what the review's own bind requires; refused before any remote is read."""


_GITHUB_REMOTE = re.compile(r"^(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)([^/]+/[^/]+?)(?:\.git)?/?$")
_PLATFORM_CAPABILITIES = ("pr_for_head", "pr_for_branch", "pr_head", "merge_pr_async")


class PlatformUnavailable(RuntimeError):
    """A member's remote is a GitHub URL, but no adapter here can read its PRs (gh absent, or an adapter that cannot
    land a stack). Merging it by push could pass an open PR stack, so the member refuses instead."""


def platform_for_remote(remote: str):  # noqa: ANN201
    """(adapter, repo) when the member's remote is on a hosting platform whose adapter can land a PR stack, else None.

    A member is a platform member only if, in addition, the platform reports exactly one open PR whose head is the
    pinned head; that is asked in the preflight, not here. A GitHub URL with no usable adapter raises
    PlatformUnavailable: whether a member's PRs are honoured must not depend on what is installed on this machine."""
    from . import platform as platform_ops
    on_github = _GITHUB_REMOTE.match(remote or "")
    try:
        adapter = platform_ops.get_platform_adapter("github")
    except Exception as exc:  # noqa: BLE001  (no adapter: a plain-Git member, unless the remote is on GitHub)
        if on_github:
            raise PlatformUnavailable(f"no GitHub adapter on this machine ({exc})") from exc
        return None
    if not all(callable(getattr(adapter, name, None)) for name in _PLATFORM_CAPABILITIES):
        if on_github:
            raise PlatformUnavailable("the GitHub adapter on this machine cannot land a PR stack")
        return None
    if platform_ops.adapter_api_version(adapter) >= 2:
        # A version 2 adapter is the only reader of its URLs.
        try:
            target = adapter.resolve_target(remote)
        except Exception:  # noqa: BLE001  (a URL the adapter does not read is a plain-Git member)
            return None
        return (adapter, f"{target.org}/{target.repo}") if target.org and target.repo else None
    found = _GITHUB_REMOTE.match(remote or "")  # a version 1 adapter: read as the v1 path reads a GitHub URL
    return (adapter, found.group(1)) if found else None


def _platform_preflight(adapter, repo: str, head: str, into: str) -> tuple[dict | None, str | None]:  # noqa: ANN001
    """The member's PR stack, top first, when exactly one open PR has the pinned head; (None, None) for none.

    Every PR of the stack must be open and not a draft; the stack is followed down through each PR's base branch
    until the base is the review's target."""
    tops = adapter.pr_for_head(repo, head)
    if not tops:
        return None, None
    if len(tops) > 1:
        return None, "platform_pr_ambiguous: " + ", ".join(f"#{p['number']}" for p in tops) + f" all have head {head}"
    stack = [tops[0]]
    while stack[-1]["base"] != into:
        below = adapter.pr_for_branch(repo, stack[-1]["base"])
        if len(below) != 1 or len(stack) > 50:
            return None, (f"platform_stack_unresolved: PR #{stack[-1]['number']} targets {stack[-1]['base']}, and "
                          f"{len(below)} open PRs have that branch as their head")
        stack.append(below[0])
    for pr in stack:
        if pr.get("state") != "open" or pr.get("draft"):
            why = "a draft" if pr.get("draft") else f"state {pr.get('state')}"
            return None, f"platform_pr_not_ready: PR #{pr['number']} is {why}"
    return {"repo": repo, "pr": stack[0]["number"], "stack": [p["number"] for p in reversed(stack)]}, None


def required_check_names(required: Sequence[str] | None, members: int, no_set: bool) -> tuple[str, ...]:
    """What each member must hold. The requirement comes from the BIND, not from the person merging: a review over
    several members always requires `set` (one command over all of them, at the pinned heads), and requires `test`
    (each member's own recorded check) unless only `set` is named. `no_set`, or a check name other than `test` or
    `set`, is refused there (RequirementRefused), because either would let the merging author choose the gate. A
    single-member review requires `test` by default and `set` only when asked, and `no_set` changes nothing for it."""
    if members <= 1:
        return tuple(required) if required is not None else (MEMBER_CHECK_NAME,)
    if no_set:
        raise RequirementRefused(
            f"set_required: this review pins {members} members, so it merges only on a passing `set` record (every "
            "member tested together at the pinned heads); --no-set is refused. Record it with "
            "`gr2 check set <workspace> <review> -- <command>`")
    names = tuple(required) if required is not None else (MEMBER_CHECK_NAME, set_check.SET_CHECK_NAME)
    foreign = [n for n in names if n not in (MEMBER_CHECK_NAME, set_check.SET_CHECK_NAME)]
    if foreign:
        raise RequirementRefused(
            f"check_not_configured: {', '.join(foreign)}: a review over {members} members takes only "
            f"`{MEMBER_CHECK_NAME}` and `{set_check.SET_CHECK_NAME}`, so a check recorded under a new name cannot stand "
            "in for them (a failed record stays recorded; fix the change and record the configured check again)")
    if set_check.SET_CHECK_NAME not in names:
        names += (set_check.SET_CHECK_NAME,)
    return names


def review_merge(workspace: Path, review_id: str, *, into: str = "main", feature: str,
                 required_checks: Sequence[str] | None = None, required_approvals: int | None = None,
                 no_set: bool = False) -> tuple[int, dict]:
    """required_checks names what each member's remote must hold at its reviewed head (see required_check_names for
    the default). The name `set` is not judged by name: it must be a record `check set` wrote for THIS review and
    THIS set of heads (set_check.judge_set_records)."""
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
    try:
        declaration = declared_sets.read(root)  # the committed grip.toml: a set is declared once, for every workspace
    except declared_sets.DeclaredSetError as exc:
        return _unmeasured(review_id, into, feature, f"declared_sets_invalid: {exc}")
    try:
        required_checks = required_check_names(required_checks, len(view["members"]), no_set)
    except RequirementRefused as exc:  # nothing read from a remote, nothing transferred
        return EXIT_REFUSED, {"id": f"gr:{review_id}", "into": into, "feature": feature, "exit": EXIT_REFUSED,
                              "refused": str(exc), "members": []}
    want_set = set_check.SET_CHECK_NAME in required_checks
    plain_checks = tuple(n for n in required_checks if n != set_check.SET_CHECK_NAME)
    expected_set_id = set_check.set_id(view["members"])
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
        row["records"] = {"review": f"refs/dev.synapt.grip/__reviews__/v1/{review_id}" if published == review_id else None,
                          "checks": check_records.CHECK_REF}
        if published != review_id:
            row["refused"] = "review_not_on_member_remote: the remote does not hold this exact review"
            continue
        if merged:
            row.update(state="already_merged", merged=merged)
            continue
        if feat != m["head"]:
            row["refused"] = f"feature_moved: remote {feature} is {feat}, reviewed {m['head']}"
            row["_heads"] = (m["head"], feat)
        elif target != m["base"]:
            row["refused"] = f"base_moved: remote {into} is {target}, reviewed base {m['base']}"
            row["_heads"] = (m["base"], target)
        elif ancestor != target:
            row["refused"] = "base_not_ancestor_of_head"
        else:
            check = check_records.read_remote_check(
                m["remote"], {"path": str(repo), "key": m["key"], "remote": m["remote"]},
                m["head"], plain_checks or (set_check.SET_CHECK_NAME,))
            row["check"] = {k: check.get(k) for k in ("status", "record_id", "reason")}
            # A `fail` whose reason is not a verdict on a record is a transport or format fault, never a content answer.
            fault = check["status"] == "fail" and check["reason"] != "required_check_failed"
            if fault or (plain_checks and check["status"] != "pass"):
                row["refused"] = f"check_{check['status']}: {check['reason']}"
            elif want_set:
                verdict, why = set_check.judge_set_records(check["records"], view["id"], expected_set_id)
                row["set_check"] = {"status": verdict, "reason": why}
                if verdict != "pass":
                    row["refused"] = f"check_set_{verdict}: {why}"
                else:
                    row["state"] = "ready"
            else:
                row["state"] = "ready"
            member_name = declaration.member_name(m["key"], m["path"])
            dset = declaration.set_of(member_name) if member_name else None
            if dset is not None and not row["refused"]:
                # A member of a declared set merges only with the set's own record, even as the only member of
                # its review: five single-member reviews of one change are not five independent changes.
                declared = check_records.read_remote_check(
                    m["remote"], {"path": str(repo), "key": m["key"], "remote": m["remote"]}, m["head"],
                    (declared_sets.DECLARED_SET_CHECK,))
                if declared["status"] == "fail" and declared["reason"] != "required_check_failed":
                    why = f"declared_set_unmeasured: {declared['reason']}"
                else:
                    why = declared_sets.judge(root, declaration, dset, member_name, m["head"], declared["records"],
                                              _pin_tie(root, declaration, into))
                row["declared_set"] = {"set": dset.name, "refused": why}
                if why:
                    row["refused"], row["state"] = why, None

    # A member whose remote is on a hosting platform AND has exactly one open PR at the pinned head lands through the
    # platform (its PR stack); every other member merges by push, as before. Decided for every member before any merge.
    platforms: dict[str, tuple] = {}
    for r, m in zip(rows, view["members"]):
        if r["state"] != "ready":
            continue
        try:
            found = platform_for_remote(m["remote"])
        except PlatformUnavailable as exc:
            r["refused"], r["state"] = f"platform_unmeasurable: {exc}", None
            continue
        if found is None:
            continue
        adapter, repo_name = found
        try:
            plan, why = _platform_preflight(adapter, repo_name, m["head"], into)
        except Exception as exc:  # noqa: BLE001  (a platform read that fails is a named refusal, never a plain merge)
            plan, why = None, f"platform_unmeasurable: {type(exc).__name__}: {exc}"
        if why:
            r["refused"], r["state"] = why, None
        elif plan:
            r["platform"] = plan
            platforms[m["key"]] = (adapter, plan)

    approval_receipt = None
    if not any(r["refused"] for r in rows):
        from . import approvals
        try:
            required = approvals.required_approvals(root, required_approvals)
            approval_receipt = {"required": required}
            if required:
                approval_receipt = approvals.count_approvals(root, view["id"])
                approval_receipt["required"] = required
                if approval_receipt["count"] < required:
                    raise approvals.ApprovalRefused(f"approvals_insufficient: {approval_receipt['count']} of {required}")
            else:
                approval_receipt["skipped"] = True
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
                    r["_heads"] = (m["head"], still)
                    break  # narrows the head race; no further member is pushed
                if m["key"] in platforms:
                    adapter, plan = platforms[m["key"]]
                    try:
                        result = adapter.merge_pr_async(plan["repo"], plan["pr"], m["head"], "merge")
                    except Exception as exc:  # noqa: BLE001
                        result = {"status": "unknown", "message": f"{type(exc).__name__}: {exc}"}
                    state, merged = _state(repo, m["remote"], into, m["head"])
                    r.update(state=state, merged=merged, merged_by="platform" if merged else None,
                             platform_result={k: result.get(k) for k in ("status", "sha", "message")})
                    # After the platform says merged, the PR's head must still be the pinned head: a commit pushed while
                    # the land ran is left out of the merge under a PR that reads merged (measured on GitHub stacks).
                    try:
                        now = adapter.pr_head(plan["repo"], plan["pr"]).get("head")
                    except Exception:  # noqa: BLE001
                        now = None
                    if state != "merged":
                        moved = f"; PR #{plan['pr']} head is now {now}" if now != m["head"] else ""
                        r["refused"] = f"platform_merge_not_confirmed: {result.get('status')}: {result.get('message')}{moved}"
                        break
                    if now != m["head"]:
                        r["refused"] = (f"platform_head_moved_after_merge: PR #{plan['pr']} head is {now}; the merge holds "
                                        f"the pinned head {m['head']}, so {now} was pushed during the land and is not in it")
                        r["_heads"], r["stranded"] = (m["head"], now), now
                        break  # land no further member after a stranded one
                    continue
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

    # Name the next command for each refusal that has one, so the operator need not read this file.
    for r, m in zip(rows, view["members"]):
        if r["refused"]:
            step = next_steps.merge_row(r["refused"], workspace=str(root), review="gr:" + view["id"].removeprefix("gr:"),
                                        remote=m["remote"], path=str((workspace / m["path"]).resolve()),
                                        head=m["head"], checks=tuple(required_checks))
            if step:
                r["next"] = step

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
    # The remote holds each merged member's merge; the clone must know it too, or the next `store commit` records the
    # pre-merge pin. A fetch that fails here never changes the exit (the merge stands), and is never silent.
    tracking_stale = []
    for r, m in zip(rows, view["members"]):
        if r.get("final") != "merged":
            continue
        repo = (workspace / m["path"]).resolve()
        try:
            failed = _refresh_tracking(repo, m["remote"], into)
        except (RuntimeError, subprocess.SubprocessError, OSError) as exc:
            failed = {"command": f"git -C {repo} fetch --no-tags <remote> +refs/heads/{into}:refs/remotes/<remote>/{into}",
                      "reason": f"the clone could not be read: {exc}"}
        if failed:
            tracking_stale.append({"member": m["key"], **failed})
    if any(r.get("stranded") for r in rows):
        code = EXIT_PARTIAL  # the PR reads merged, and a commit on it is not in the target
    elif all(s == "merged" for s in final):
        code = EXIT_MERGED
    elif all(s == "unmerged" for s in final):
        code = EXIT_REFUSED
    else:
        code = EXIT_PARTIAL
    # Beside the `refused` string, the same refusal as fields: the code, the member, and both heads when the refusal
    # is a moved ref (expected is what was reviewed, observed is what the remote holds now).
    for r in rows:
        expected, observed = r.pop("_heads", (None, None))
        r.setdefault("records", None)
        r["refusal"] = None if not r["refused"] else {
            "code": r["refused"].split(":", 1)[0], "member": r["key"], "expected": expected, "observed": observed}
    receipt = {"id": view["id"], "into": into, "feature": feature, "exit": code, "members": rows,
               "approvals": approval_receipt, "tracking_stale": tracking_stale}
    try:
        # Kept for `review show`, so the receipt outlives this run's stdout; a failed write never changes the exit.
        merge_receipts.record(root, view["id"], receipt)
    except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
        receipt["stored"] = f"receipt_not_stored: {exc}"
    return code, receipt
