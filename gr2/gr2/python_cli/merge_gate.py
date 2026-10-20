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

import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Mapping, Sequence

from . import check_records, declared_sets, gitops, grip, keys, merge_receipts, next_steps, set_check

EXIT_MERGED, EXIT_REFUSED, EXIT_PARTIAL = 0, 3, 4


def _git(repo: Path, *args: str, check: bool = True, raw: bool = False, history: bool = False) -> str:
    # `history` reads the real history (gitops.real_history_run): for a merge-base or a rev-list the gate acts on.
    runner = gitops.real_history_run if history else gitops.run
    proc = runner(repo, *args, timeout=60, raise_timeout=True)  # an expiry raises, as it always did here
    if check:
        gitops._raise(proc, args, RuntimeError)
    return proc.stdout if raw else proc.stdout.strip()


def _ls_remote(repo: Path, remote: str, *patterns: str) -> list[tuple[str, str]]:
    """(oid, name) per advertised ref, split on "\n" from the raw output: str.splitlines and strip would cut a
    name at U+2028 or U+0085."""
    try:
        return gitops.ls_remote_rows(_git(repo, "ls-remote", remote, *patterns, raw=True))
    except gitops.LsRemoteMalformed as exc:
        raise RuntimeError(f"ls-remote {remote} {' '.join(patterns)}: {exc}") from None


_plain_remote = gitops.plain_remote


def _text(raw: bytes | str | None) -> str:
    """A binary git run's output as text; bytes that are not UTF-8 read as \\xNN, never a crash."""
    return raw.decode("utf-8", "backslashreplace") if isinstance(raw, bytes) else (raw or "")


def _pre_push_refused(trace: Path) -> bool | None:
    """Whether the push's own pre-push hook exited nonzero, read from git's trace2 event file; None if unreadable.

    Only the top-level process counts, named by the sid of the first "version" event (under a git alias that sid
    has a "/" too, from the inherited GIT_TRACE2_PARENT_SID): a git run inside the hook logs under a child sid.
    """
    try:
        events = [json.loads(ln) for ln in trace.read_text(encoding="utf-8", errors="replace").splitlines() if ln]
    except (OSError, ValueError):
        return None
    top = next((e.get("sid") for e in events if isinstance(e, dict) and e.get("event") == "version"), None)
    if not isinstance(top, str):
        return None
    mine = [e for e in events if isinstance(e, dict) and e.get("sid") == top]
    hooks = {e.get("child_id") for e in mine if e.get("event") == "child_start"
             and e.get("child_class") == "hook" and e.get("hook_name") == "pre-push"}
    return any(e.get("event") == "child_exit" and e.get("child_id") in hooks and e.get("code") != 0 for e in mine)


def _push_verdict(out: list[str], into: str, url: str | None = None) -> list[str]:
    """Git's own porcelain "!" line for refs/heads/<into>, read only in the LAST "To " block, up to "Done".

    Git prints the URL rewritten (insteadOf, pushInsteadOf, scp form) and with any credential dropped, so the
    block is found by place, never by matching the remote's URL; a pre-push hook that said no is read apart, from
    trace2, before this is asked. With no trace2 to read, `url` keeps the older reading: only the block after
    "To <url>" exactly, so a hook's echoed "!" line is not read as the remote's.
    """
    block: list[str] | None = None
    last: list[str] | None = None
    for ln in out:
        if ln.startswith("To "):
            block = [] if url is None or ln == f"To {url}" else None
        elif block is not None and ln == "Done":
            if url is not None:
                break
            last, block = block, None
        elif block is not None:
            block.append(ln)
    if block is None and url is None:
        block = last
    return [ln.replace("\t", " ") for ln in block or ()
            if ln.startswith("!") and ln.split("\t")[1:2] and ln.split("\t")[1].endswith(f":refs/heads/{into}")]


def _capped(text: str, cap: int = 300) -> str:
    """Printable text, escaped FIRST and then cut to its head, so the cap counts what is printed."""
    shown = next_steps._escaped(text)
    return shown if len(shown) <= cap else f"{shown[:cap]} [{len(shown) - cap} chars cut]"


def _advertised(repo: Path, remote: str, ref: str) -> str | None:
    """The oid the remote advertises for EXACTLY ref; ls-remote patterns also match nested refs by tail."""
    _plain_remote(remote)
    hits = _ls_remote(repo, remote, ref)
    exact = [oid for oid, name in hits if name == ref]
    if len(exact) > 1:
        raise RuntimeError(f"ls-remote: {ref} advertised more than once")
    return exact[0] if exact else None


def feature_moves(workspace: Path, members: list[dict], feature: str | None) -> dict[str, dict]:
    """Per member: does the remote feature branch still name the pinned head? The advertisement read `_remote_tip`
    makes first (ls-remote: no fetch, no ref written). moved_since_pin is None when it cannot be read, never False. feature names the branch
    compared, so a reviewer whose checkouts sit on main sees that main is what moved."""
    out = {}
    for m in members:
        row = {"moved_since_pin": None, "branch_head": None, "feature": feature}
        if not feature:
            row["unmeasured"] = "feature_unresolved: members are not on one branch"
        elif grip.url_has_credentials(m["remote"]):  # a git error names its argv, so refuse by key before any call
            row["unmeasured"] = "remote_credentials: the recorded remote cannot be parsed or carries URL userinfo"
        else:
            try:
                tip = _advertised((Path(workspace) / m["path"]).resolve(), m["remote"], f"refs/heads/{feature}")
                row.update(moved_since_pin=tip != m["head"], branch_head=tip)
            except (RuntimeError, subprocess.SubprocessError, OSError, ValueError) as exc:
                # Never the exception text: a git error names its argv, the remote URL included.
                row["unmeasured"] = f"{type(exc).__name__}: the remote's advertisement of the feature branch could not be read"
        out[m["key"]] = row
    return out


def _remote_tips(repo: Path, remote: str, branches: tuple[str, ...], listed: tuple[str, ...] = (),
                 scan: str = "") -> dict[str, str | None]:
    """One read of a remote for several exact refs: each branch's commit (its objects fetched by id) and each listed
    ref's advertised oid, None when absent; with `scan`, also every ref advertised under that prefix, by its oid and
    not fetched. One ls-remote and one fetch per remote, and no private ref to make or delete."""
    _plain_remote(remote)
    refs = tuple(f"refs/heads/{b}" for b in branches) + listed
    hits = _ls_remote(repo, remote, *refs, *((f"{scan}*",) if scan else ()))
    scanned = tuple(dict.fromkeys(name for _, name in hits if scan and name.startswith(scan) and name not in refs))
    tips: dict[str, str | None] = {}
    for ref in refs + scanned:  # ls-remote patterns also match nested refs by tail
        exact = {oid for oid, name in hits if name == ref}
        if len([name for _, name in hits if name == ref]) > 1:
            raise RuntimeError(f"ls-remote: {ref} advertised more than once")
        tips[ref] = exact.pop() if exact else None
    wanted = sorted({tips[f"refs/heads/{b}"] for b in branches} - {None})
    if wanted:
        _git(repo, "fetch", "--no-tags", "--no-write-fetch-head", "--refmap=", remote, *wanted)
    for b in branches:
        if tips[f"refs/heads/{b}"]:
            tips[f"refs/heads/{b}"] = _git(repo, "rev-parse", "--verify", f"{tips[f'refs/heads/{b}']}^{{commit}}")
    return tips


def _remote_tip(repo: Path, remote: str, branch: str) -> str | None:
    """One remote branch's commit, None when absent."""
    return _remote_tips(repo, remote, (branch,))[f"refs/heads/{branch}"]


def _merged_at(repo: Path, target: str, head: str) -> str | None:
    """The commit on target's first-parent chain whose SECOND parent is head, if any."""
    for line in _git(repo, "rev-list", "--first-parent", "--parents", target, history=True).splitlines():
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


_url_key = keys.remote_url_key  # the one remote-URL key; the alias keeps existing callers


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
        shown = next_steps._shown  # every value quoted whole: a refname may hold ';', so the line runs as printed
        return {"command": f"git -C {shown(repo)} fetch --no-tags --refmap= -- {shown(gitops._shown_arg(remote))} "
                           f"{shown(f'+refs/heads/{into}:refs/remotes/{target}/{into}')}",
                "reason": f"no remote of this clone names {gitops._shown_arg(remote)}"}
    spec = f"+refs/heads/{into}:refs/remotes/{name}/{into}"
    proc = gitops.run(repo, "fetch", "--no-tags", "-q", "--refmap=", name, spec, timeout=120)
    shown = next_steps._shown
    return None if proc.returncode == 0 else {"command": f"git -C {shown(repo)} fetch --no-tags --refmap= -- {shown(name)} "
                                                         f"{shown(spec)}",
                                              "reason": "the fetch failed"}


_REVIEW_REF_PREFIX = "refs/dev.synapt.grip/__reviews__/"
_REVIEW_SCAN_CAP = 200  # reviews read per member remote; over it is a named refusal, never a silent partial read
_OID_NOT_SERVED = ("not our ref", "no such remote ref")  # git's words when a remote will not serve one wanted oid


def _first_error(exc: Exception) -> str:
    """git's first `fatal:` or `error:` line from a failed _git call (a warning may come first), else its first line."""
    command, _, said = str(exc).partition(": ")
    lines = said.split("\n")
    return f"{command}: " + next((line for line in lines if line.startswith(("fatal:", "error:"))), lines[0])


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
            tips = _remote_tips(repo, remote, (into,), scan=_REVIEW_REF_PREFIX)  # the target and every review, one read
            tip = tips.pop(f"refs/heads/{into}")
            merged = gitops.is_ancestor(repo, pin, tip) if tip else False
            if merged is True:
                return None
            # None (a shallow clone, or the target's commit not in this clone) is no answer: a review may still tie the
            # pin below, but the refusal at the end must not say "not merged".
            unknown = merged is None
            if len(tips) > _REVIEW_SCAN_CAP:
                return (f"declared_set_pin_unmeasured: {name}'s remote advertises {len(tips)} reviews, over the "
                        f"{_REVIEW_SCAN_CAP} this gate reads; its pin {short} cannot be tied to a reviewed head")
            failed: dict[str, str] = {}  # oid -> git's first line, for reviews the remote advertises but cannot serve
            if tips:  # every review's objects in one fetch by id, read by id: no private ref to make or delete
                try:
                    _git(repo, "fetch", "--no-tags", "--no-write-fetch-head", "--refmap=", remote, *sorted(set(tips.values())))
                except RuntimeError as exc:  # one oid the remote no longer has fails the whole fetch: read the rest one by one
                    if not any(said in str(exc) for said in _OID_NOT_SERVED):
                        raise  # any other failure is the remote's or this clone's, not one review's: refuse with it
                    for oid in sorted(set(tips.values())):
                        try:
                            _git(repo, "fetch", "--no-tags", "--no-write-fetch-head", "--refmap=", remote, oid)
                        except RuntimeError as exc:
                            failed[oid] = _first_error(exc)
            states: dict[str, dict] = {}
            retracted = ""

            def names(oid: str) -> bool:
                if oid not in states:
                    states[oid] = grip._read_repo_state(repo, oid, bind=True)
                return any(fields.get("commit") == pin and declaration.member_name(key, fields.get("path", "")) == name
                           for key, fields in states[oid].items())

            named = [ref for ref in sorted(tips) if tips[ref] not in failed and names(tips[ref])]  # by the oid scanned
            if named:  # a review counts only while its ref still names the pin: deleted, or moved off it, is retracted
                now = _remote_tips(repo, remote, (), tuple(named))
                for ref in [ref for ref in named if now[ref]]:  # any one still naming it ties; re-published counts
                    try:
                        if now[ref] != tips[ref]:  # moved: read the new oid, which may hold the same review
                            _git(repo, "fetch", "--no-tags", "--no-write-fetch-head", "--refmap=", remote, now[ref])
                        if names(now[ref]):
                            return None
                    except (RuntimeError, grip.GripCorruptError):
                        continue  # the new oid does not read as a review: moved off the pin
                retracted = ", ".join(next_steps._shown(ref) for ref in named)
            if failed:  # no review that read names the pin, and one that did not read might: unmeasured, by ref
                lost = ", ".join(next_steps._shown(ref) for ref in sorted(tips) if tips[ref] in failed)
                said = "".join(c if c.isprintable() else ascii(c)[1:-1] for c in next(iter(failed.values())))
                return (f"declared_set_pin_unmeasured: {name}'s pin {short} is named by no review its remote serves, "
                        f"and these reviews could not be read from it: {lost}: {said}")
        except (RuntimeError, subprocess.SubprocessError, OSError, grip.GripCorruptError) as exc:
            return f"declared_set_pin_unmeasured: {name}'s pin {short} could not be read from its remote: {exc}"
        tie.siblings[name] = {"review": _local_review_of(root, declaration, name, pin), "remote": remote, "path": str(repo),
                             "base": tip, "ref": f"refs/heads/{into}", "member_path": info["path"], "pin": pin}
        if unknown:
            return (f"ancestry_unmeasurable: {name}'s pin {short} is named by no review its remote serves, and this "
                    f"clone cannot tell whether it is in {into} at {tip[:12]}; clone: {repo}")
        if retracted:
            return (f"declared_set_pin_unreviewed: {name} is pinned at {short}, which is not merged into {into}, and "
                    f"the reviews that named it were retracted from its remote (ref deleted or moved): {retracted}")
        return (f"declared_set_pin_unreviewed: {name} is pinned at {short}, which is neither merged into {into} nor "
                f"named by a review published on its remote")
    tie.siblings = {}  # name -> its local review at the pin (or None), remote, clone and target head, for the next line
    return tie


def _local_review_of(root: Path, declaration: declared_sets.Declaration, name: str, pin: str) -> str | None:
    """The newest review pinned in this workspace that names member `name` at exactly `pin`, as gr:<sha>; None when
    there is none, or the store cannot be read (the next line then names what is missing)."""
    try:
        binds = grip.list_review_binds(root)
    except (grip.GripCorruptError, grip.GripInitError, grip.ReviewStoreAbsent, OSError, subprocess.SubprocessError):
        return None
    for commit, _ in reversed(binds):
        try:
            state = grip._read_repo_state(root, commit, bind=True)
        except (grip.GripCorruptError, OSError, subprocess.SubprocessError):
            continue
        if any(f.get("commit") == pin and declaration.member_name(k, f.get("path", "")) == name for k, f in state.items()):
            return f"gr:{commit}"
    return None


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


_PLATFORM_CAPABILITIES = ("pr_for_head", "pr_for_branch", "pr_head", "merge_pr_async")


def _github_repo(remote: str) -> str | None:
    """'<owner>/<repo>' for every remote gr2 reads as GitHub (review.canonical_source_identity: https, http, with a
    user or a token, scp-style and ssh), else None. One reader for the whole of gr2, so a spelling the review record
    calls GitHub is never a plain-Git member here."""
    from .review import ReviewError, canonical_source_identity
    try:
        return canonical_source_identity(remote or "").removeprefix("https://github.com/")
    except ReviewError:
        return None


class PlatformUnavailable(RuntimeError):
    """A member's remote is a GitHub URL, but no adapter here can read its PRs (gh absent, or an adapter that cannot
    land a stack). Merging it by push could pass an open PR stack, so the member refuses instead."""


def platform_for_remote(remote: str):  # noqa: ANN201
    """(adapter, repo) when the member's remote is on a hosting platform whose adapter can land a PR stack, else None.

    A member is a platform member only if, in addition, the platform reports exactly one open PR whose head is the
    pinned head; that is asked in the preflight, not here. A GitHub URL with no usable adapter raises
    PlatformUnavailable: whether a member's PRs are honoured must not depend on what is installed on this machine."""
    from . import platform as platform_ops
    on_github = _github_repo(remote)
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
        except Exception as exc:  # noqa: BLE001  (a URL the adapter does not read is a plain-Git member, unless on GitHub)
            if on_github:
                # Named by repository, never by the remote as spelled: it may carry a token, and so may the
                # adapter's own error text, so only the error's type is printed.
                raise PlatformUnavailable(f"the GitHub adapter on this machine cannot read the remote of {on_github} "
                                          f"({type(exc).__name__})") from exc
            return None
        return (adapter, f"{target.org}/{target.repo}") if target.org and target.repo else None
    return (adapter, on_github) if on_github else None  # a version 1 adapter: GitHub URLs, read by gr2's own reader


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
    single-member review requires `test` by default and `set` only when asked, and `no_set` changes nothing for it.
    A DEFAULT `test` that is absent at a member's head yields to a passing `set` record for this review there; a red
    one never does (review_merge)."""
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
                 no_set: bool = False, dry_run: bool = False) -> tuple[int, dict]:
    """dry_run (`review blockers`) stops after the approval count: no merge build, no push, no receipt, so the
    remote refs are unchanged; it returns every refusal the real merge would have stopped on.

    required_checks names what each member's remote must hold at its reviewed head (see required_check_names for
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
    credentialed = sorted(m["key"] for m in view["members"] if grip.url_has_credentials(m["remote"]))
    if credentialed:  # every row, advice line and next step prints the remote, so refuse before any is built
        return EXIT_REFUSED, {"id": f"gr:{review_id}", "into": into, "feature": feature, "exit": EXIT_REFUSED,
                              "refused": f"remote_credentials: {', '.join(credentialed)}: the recorded remote cannot "
                                         "be parsed or carries URL userinfo; pin again from a remote without it and "
                                         "use a credential helper", "members": []}
    rows: list[dict] = []
    siblings: dict[str, dict] = {}  # member key -> its declared set's tie findings, for the next lines
    root = Path(workspace).resolve()
    try:
        declaration = declared_sets.read(root)  # the committed grip.toml: a set is declared once, for every workspace
    except declared_sets.DeclaredSetError as exc:
        return _unmeasured(review_id, into, feature, f"declared_sets_invalid: {exc}")
    test_by_default = required_checks is None  # a `test` the merger NAMED is never decided by a set record
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
            review_ref = f"refs/dev.synapt.grip/__reviews__/v1/{review_id}"
            tips = _remote_tips(repo, m["remote"], (into, feature), (review_ref,))
            target, feat, published = tips[f"refs/heads/{into}"], tips[f"refs/heads/{feature}"], tips[review_ref]
            merged = _merged_at(repo, target, m["head"]) if target else None
            ancestor = _git(repo, "merge-base", m["head"], target, history=True) if target else ""
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
        elif target != m["base"] and ancestor == m["head"]:
            # The target moved to the reviewed head or past it: the work is already in.
            row["refused"] = f"head_already_on_target: the reviewed head {m['head']} is already in {into} at {target}"
            row["_heads"] = (m["base"], target)
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
            if (not fault and test_by_default and len(view["members"]) == 1 and plain_checks == (MEMBER_CHECK_NAME,)
                    and check["status"] == "absent"):
                # One member: `check set` over it is that member's test at that head, so a passing `set` record for
                # THIS review and set identity decides `test` (one for another review stays stale and never counts).
                verdict, _why = set_check.judge_set_records(check["records"], view["id"], expected_set_id)
                if verdict == "pass":
                    check = dict(check, status="pass", reason="required_checks_pass")
                    row["check"].update(status="pass", reason="required_checks_pass", source={MEMBER_CHECK_NAME: "set"})
            if not fault and plain_checks and len(view["members"]) >= 2:
                # Set evidence for X decides X on a multi-member review; the receipt says which source decided it.
                judged = {n: set_check.judge_plain_check(check["records"], n, view["id"], expected_set_id)
                          for n in plain_checks}
                if test_by_default and judged.get(MEMBER_CHECK_NAME, ("",))[0] in ("absent", "stale") and \
                        set_check.judge_set_records(check["records"], view["id"], expected_set_id)[0] == "pass":
                    # No `test` of this member's own at this head (members that import each other cannot pass one
                    # alone), and the members passed together for this review: the set decides. A red `test` record
                    # is evidence and still refuses below; no record is not.
                    judged[MEMBER_CHECK_NAME] = ("pass", "required_checks_pass", "set")
                row["check"]["source"] = {n: j[2] for n, j in judged.items()}
                worst = next((j for want in ("fail", "stale", "absent") for j in judged.values() if j[0] == want), None)
                if worst is not None:
                    status, reason = worst[0], worst[1]
                    stale = next((n for n, j in judged.items() if j is worst), None) if status == "stale" else None
                    row["refused"] = f"check_{stale}_stale: {reason}" if stale else f"check_{status}: {reason}"
                    row["check"].update(status=status, reason=reason)
                else:
                    row["check"].update(status="pass", reason="required_checks_pass")
                check = dict(check, status=row["check"]["status"])
            if row["refused"]:
                pass
            elif fault or (plain_checks and check["status"] != "pass"):
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
                    tie = _pin_tie(root, declaration, into)
                    why = declared_sets.judge(root, declaration, dset, member_name, m["head"], declared["records"], tie)
                    siblings[m["key"]] = tie.siblings
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
            policy = approvals.approval_policy(root, required_approvals)
            required = policy["required"]
            approval_receipt = dict(policy)
            if required:
                approval_receipt = approvals.count_approvals(root, view["id"])
                approval_receipt.update(policy)
                if approval_receipt["count"] < required:
                    raise approvals.ApprovalRefused(f"approvals_insufficient: {approval_receipt['count']} of {required}")
            else:
                approval_receipt["skipped"] = True
        except (approvals.ApprovalRefused, grip.GripInitError, grip.GripCorruptError,
                OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
            heads = (getattr(exc, "expected", None), getattr(exc, "observed", None))
            for r in rows:
                if r["state"] == "ready":
                    r["refused"] = str(exc)
                    if any(heads):
                        r["_heads"] = heads

    if dry_run:
        for r, m in zip(rows, view["members"]):
            if r["refused"]:
                step = next_steps.merge_row(r["refused"], workspace=str(root), review="gr:" + view["id"].removeprefix("gr:"),
                                            remote=m["remote"], path=str((workspace / m["path"]).resolve()),
                                            head=m["head"], checks=tuple(required_checks),
                                            siblings=siblings.get(m["key"], {}))
                if step:
                    r["next"] = step
            expected, observed = r.pop("_heads", (None, None))
            r.setdefault("records", None)
            r["refusal"] = None if not r["refused"] else {
                "code": r["refused"].split(":", 1)[0], "member": r["key"], "expected": expected, "observed": observed}
        blockers = [r["refusal"] for r in rows if r["refusal"]]
        return (EXIT_REFUSED if blockers else 0), {
            "id": view["id"], "into": into, "feature": feature, "dry_run": True, "ready": not blockers,
            "blockers": blockers, "members": rows, "approvals": approval_receipt}

    if not any(r["refused"] for r in rows):
        # Build every merge before any push.
        builds = {}
        for r, m in zip(rows, view["members"]):
            if r["state"] != "ready":
                continue
            repo = (workspace / m["path"]).resolve()
            try:
                # The merge base comes from the real history (real_history_run): a replace ref or graft would
                # otherwise give merge-tree parents the head does not have.
                proc = gitops.real_history_run(repo, "merge-tree", "--write-tree", m["base"], m["head"], timeout=60,
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
                        now, read_error = adapter.pr_head(plan["repo"], plan["pr"]).get("head"), None
                    except Exception as exc:  # noqa: BLE001
                        now, read_error = None, f"{type(exc).__name__}: {exc}"
                    if state != "merged":
                        moved = f"; PR #{plan['pr']} head is now {now}" if now and now != m["head"] else ""
                        r["refused"] = f"platform_merge_not_confirmed: {result.get('status')}: {result.get('message')}{moved}"
                        break
                    if not now:
                        # The merge holds the pinned head, but whether a commit was pushed during the land is unknown:
                        # never reported as stranding a commit that was not measured, and never as a clean merge.
                        r["refused"] = (f"platform_head_unmeasured_after_merge: PR #{plan['pr']} merged at the pinned head "
                                        f"{m['head']}, but its head could not be read after the merge "
                                        f"({read_error or 'no head returned'}); whether a later commit was left out is unknown")
                        r["_unmeasured"] = True
                        break  # land no further member after one whose outcome is not known
                    if now != m["head"]:
                        r["refused"] = (f"platform_head_moved_after_merge: PR #{plan['pr']} head is {now}; the merge holds "
                                        f"the pinned head {m['head']}, so {now} was pushed during the land and is not in it")
                        r["_heads"], r["stranded"] = (m["head"], now), now
                        break  # land no further member after a stranded one
                    continue
                remote_said = unconfirmed = False
                try:
                    # --porcelain marks a refusal the remote made ("[remote rejected]") apart from a push that never
                    # reached it; binary, because a hook may print bytes that are not UTF-8.
                    # trace2 tells a refusing pre-push hook apart from the remote, whatever the hook prints (this
                    # one push overrides a user's own GIT_TRACE2_EVENT); git < 2.27 writes no file: read stdout alone.
                    with tempfile.TemporaryDirectory(prefix="gr2-push-") as tmp:
                        trace = Path(tmp) / "trace2.json"
                        proc = gitops.run(repo, "push", "--porcelain",
                                          f"--force-with-lease=refs/heads/{into}:{m['base']}", m["remote"],
                                          f"{builds[r['key']]}:refs/heads/{into}", binary=True, timeout=120,
                                          env={**os.environ, "GIT_TRACE2_EVENT": str(trace)})
                        hook_said = _pre_push_refused(trace)
                    out = _text(proc.stdout).split("\n")
                    err = gitops._shown_stderr(proc.args, _text(proc.stderr))
                    secrets = gitops.credential_values(proc.args, repo)
                    err = gitops._masked(err, secrets).split("\n")
                    status = [] if hook_said else _push_verdict(out, into, None if hook_said is False else m["remote"])
                    remote_said = any("[remote rejected]" in ln for ln in status)
                    # the verdict line carries the server's reason text: masked after the test, so a short
                    # credential cannot erase the marker
                    status = [gitops._masked(ln, secrets) for ln in status]
                    if proc.returncode == 124:  # gitops' expiry: the push may still land after it
                        push_error, unconfirmed = f"push_unconfirmed: {_capped(chr(10).join(err).strip())}", True
                    elif remote_said:  # the remote's own words and git's verdict line, never the URL it repeats
                        push_error = _capped("\n".join([ln for ln in err if ln.startswith("remote:")] + status))
                    elif proc.returncode:
                        why = [ln for ln in err if ln.strip() and not ln.startswith(("To ", "error: failed to push"))]
                        push_error = _capped("\n".join(why + status) or f"git push exit {next_steps._shown(str(proc.returncode))}")
                    else:
                        push_error = None
                except (subprocess.SubprocessError, OSError) as exc:  # a timeout is not evidence nothing moved
                    push_error, unconfirmed = f"push_unconfirmed: {_capped(f'{type(exc).__name__}: {exc}')}", True
                state, merged = _state(repo, m["remote"], into, m["head"])
                by = "this_run" if merged == builds[r["key"]] else ("another_writer" if merged else None)
                r.update(state=state, merged=merged, merged_by=by)
                if push_error and by != "this_run":
                    r["push_error"] = push_error  # e.g. our lease lost to a writer who merged the same head
                if push_error and state != "merged":
                    # The push failed and the member did not land: name why, never a bare unmerged or unknown, and
                    # push no further member (the set lands on the base it was checked on, or not at all).
                    if unconfirmed:  # the push may still land: no read now tells this member's outcome
                        r["refused"], r["_unmeasured"] = push_error, True
                        break
                    try:
                        now, unread = _remote_tip(repo, m["remote"], into), None
                        # One more git call, on this failure path only: is the reviewed head already in the moved target?
                        landed = gitops.is_ancestor(repo, m["head"], now) if now and now != m["base"] else False
                    except (RuntimeError, subprocess.SubprocessError, OSError) as exc:
                        now, unread, landed = None, f"{type(exc).__name__}: {exc}", False
                    if unread:
                        r["refused"] = f"target_unread_at_push: {_capped(unread)}"
                    elif landed is None:  # a shallow clone cannot tell landed from moved: never read it as moved
                        r["refused"] = (f"ancestry_unmeasurable: remote {into} moved to {now} at the push, and this "
                                        f"clone cannot tell whether the reviewed head {m['head']} is in it; clone: {repo}")
                        r["_heads"] = (m["base"], now)
                    elif now is None:
                        r["refused"] = f"target_deleted_at_push: remote {into} is gone, the gate read {m['base']}"
                        r["_heads"] = (m["base"], None)
                    elif landed:
                        r["refused"] = (f"head_already_on_target: the reviewed head {m['head']} is already in {into} "
                                        f"at {now}")
                        r["_heads"] = (m["base"], now)
                    elif now != m["base"]:
                        r["refused"] = f"target_moved_at_push: remote {into} is {now}, the gate read {m['base']}"
                        r["_heads"] = (m["base"], now)
                    elif remote_said:  # still at the base the gate read: a hook or a protected branch said no
                        r["refused"] = f"push_rejected: {push_error}"
                    else:  # still at the base, and the remote never answered: auth, network or a local hook
                        r["refused"] = f"push_failed: {push_error}"
                    break
                if state == "unknown":
                    break  # push no further members after an unresolved one

    # Name the next command for each refusal that has one, so the operator need not read this file.
    for r, m in zip(rows, view["members"]):
        if r["refused"]:
            step = next_steps.merge_row(r["refused"], workspace=str(root), review="gr:" + view["id"].removeprefix("gr:"),
                                        remote=m["remote"], path=str((workspace / m["path"]).resolve()),
                                        head=m["head"], checks=tuple(required_checks),
                                        siblings=siblings.get(m["key"], {}))
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
    # A partial whose refused member's feature or target moved: its old pin cannot merge it, and `review update` pins
    # every member, so its next is the new pin alone (on the observed target when the target moved). The members the
    # run left unpushed ride on the first such next only, so no two printed lines pin them twice.
    landed = [r["key"] for r in rows if r.get("final") == "merged"]
    waiting_given = False
    for r, m in zip(rows, view["members"]):
        refused = r["refused"] or ""
        # A base_moved with no measured heads has no new target to pin.
        target_moved = refused.startswith(("base_moved", "target_moved_at_push")) and r.get("_heads") is not None
        if landed and (refused.startswith("feature_moved") or target_moved) and r.get("final") == "unmerged":
            r["next"] = next_steps._partial_moved_row(
                workspace=str(root), key=m["key"], member_path=m["path"], path=str((workspace / m["path"]).resolve()),
                remote=m["remote"], base=r["_heads"][1] if target_moved else m["base"], into=into, feature=feature,
                landed=landed, approvals=required_approvals, moved="target" if target_moved else "feature",
                waiting=[{"key": w["key"], "member_path": w["path"], "path": str((workspace / w["path"]).resolve()),
                          "remote": w["remote"], "base": w["base"], "head": w["head"]}
                         for o, w in zip(rows, view["members"])
                         if not waiting_given and o.get("final") == "unmerged" and not o["refused"]])
            waiting_given = True
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
            spec = next_steps._shown(f"+refs/heads/{into}:refs/remotes/<remote>/{into}")
            failed = {"command": f"git -C {next_steps._shown(repo)} fetch --no-tags <remote> {spec}",
                      "reason": f"the clone could not be read: {exc}"}
        if failed:
            tracking_stale.append({"member": m["key"], **failed})
    if any(r.get("stranded") or r.get("_unmeasured") for r in rows):
        code = EXIT_PARTIAL  # the PR reads merged, and a commit on it is not in the target, or that is not known
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
        r.pop("_unmeasured", None)
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


def review_merged(workspace: Path, review_id: str, *, into: str = "main") -> tuple[int, dict]:
    """Read-only (`review merged`): for each member, the merge of its reviewed head on the REMOTE target, with the
    merge's committer date, and whether this clone holds a `review merge` receipt for the review. The remote is the
    answer, so a clone that never ran the merge still finds it. Exit 0 all merged, 3 none, 4 some or unmeasured."""
    review_id = review_id.removeprefix("gr:")
    try:
        view = grip.show_review_commit(workspace, review_id)
    except (grip.GripInitError, grip.GripCorruptError, RuntimeError, OSError, ValueError, KeyError,
            subprocess.SubprocessError) as exc:
        return _unmeasured(review_id, into, "", f"bind_unreadable: {type(exc).__name__}: {exc}")
    root = Path(workspace).resolve()
    try:
        receipts = merge_receipts.runs(root, view["id"])
    except (RuntimeError, OSError, ValueError, subprocess.SubprocessError):
        receipts = None  # an unreadable local receipt never changes the remote answer; it reads as null
    rows, states = [], []
    for m in view["members"]:
        repo = (workspace / m["path"]).resolve()
        row = {"key": m["key"], "head": m["head"], "merged": None, "merged_at": None, "refused": None}
        rows.append(row)
        try:
            target = _remote_tip(repo, m["remote"], into)
            row["merged"] = _merged_at(repo, target, m["head"]) if target else None
            if row["merged"]:
                row["merged_at"] = _git(repo, "show", "-s", "--format=%cI", row["merged"])
        except (RuntimeError, subprocess.SubprocessError, OSError) as exc:
            row["refused"] = f"remote_unmeasurable: {exc}"
        states.append("unknown" if row["refused"] else ("merged" if row["merged"] else "unmerged"))
    code = (EXIT_MERGED if all(s == "merged" for s in states) else
            EXIT_REFUSED if all(s == "unmerged" for s in states) else EXIT_PARTIAL)
    return code, {"id": view["id"], "into": into, "exit": code, "members": rows, "receipt_here": None if receipts is None else bool(receipts)}
