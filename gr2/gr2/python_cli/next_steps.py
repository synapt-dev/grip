"""What a review refusal needs next, said by gr2 itself.

An operator who meets a refusal should not have to read gr2's source to learn what comes next. Each line here
names the next command for the state the refusal measured, and only the legitimate path: a different approver,
a new pin, a publish, a check. No line suggests changing who Git says you are, and no line suggests deleting a
remote branch; the tests check every line for both.
"""
from __future__ import annotations

import re
import shlex


def _escaped(value: str) -> str:
    """A value as it may appear in printed text: every non-printable character escaped (\\xNN, \\uNNNN)."""
    # Not only C0, DEL and C1: anything Python does not call printable (bidi overrides such as U+202E, line and
    # paragraph separators, zero-width spaces) can reorder or split a printed line, so it is escaped too.
    return "".join(c if c.isprintable() else (f"\\x{ord(c):02x}" if ord(c) < 256 else f"\\u{ord(c):04x}")
                   for c in str(value))


def _shown(value: str) -> str:
    """A value as it may appear in a printed command: escaped (see _escaped) so a name cannot colour the terminal or
    start a forged line, and shell-quoted so the line runs as printed."""
    return shlex.quote(_escaped(value))

REVIEW_ORDER = """\b
A review goes through five steps, in this order:
  1. gr2 review pin      pin the head before pushing it (needs gr2 store init)
  2. gr2 review publish  push every member's pinned head to its own remote,
                         then put the pinned review there
  3. gr2 review stamp    an approver other than the author stamps it; in their
                         own workspace, after gr2 review receive
  4. gr2 check run       record the check at the exact head on the remote; a
                         review over several members needs only gr2 check set,
                         one command over every member: merge accepts on the
                         set alone
  5. gr2 review merge    merges only if heads still match the pin and check
Each refusal along the way names the next command."""


def self_approval(workspace: str, review: str) -> str:
    return (f"next: a different approver stamps it from their own workspace, after receiving it there: "
            f"gr2 review receive <their-workspace> {_shown(review)} --remote <member-remote>, then "
            f"gr2 review stamp <their-workspace> {_shown(review)}")


def head_not_fetched(repo: str, remote: str, head: str, workspace: str, review: str) -> str:
    # stamp has already asked the member remote for the exact head, so a hand fetch would fail the same way: the head
    # has to reach that remote first.
    return (f"next: the member remote {_shown(remote)} does not serve the reviewed head {_shown(head)}; once the "
            f"author has pushed it there, stamp again: gr2 review stamp {_shown(workspace)} {_shown(review)}")


def _nothing_to_merge() -> str:
    # Private, so the skill's refusal table keeps one row for head_already_on_target. True whether the target reached
    # the head by a fast-forward or through a merge commit.
    return ("next: nothing to merge; the reviewed head is already in the target, so the reviewed work is there and no "
            "gr2 command lands it again. Confirm the target's tip is what you meant to ship; for new work, commit a "
            "new head and pin it")


def _stamp_base_moved(branch: str, review: str) -> str:
    # Private, so the skill's refusal table keeps one row per printed code (base_moved is merge_row's).
    # A merge needs the target at the reviewed base, and a pinned review cannot move to a new base, so a stamp here
    # could never count; the author has to update and pin again. The stamper is not the author, so the line names
    # the author's verb and the review it applies to.
    return (f"next: no stamp was written; {_shown(branch)} moved after the pin. Ask the author to run "
            f"gr2 review update <their workspace> {_shown(review)} and ask for a stamp on the review it prints")


def _update_lines(workspace: str, receipt: dict) -> list[str]:
    # Private: `review update` prints its refusal inside its JSON receipt, not as a `refused: <code>` line.
    lines, rid, new = [], receipt.get("review") or "<review>", receipt.get("new_review")
    if receipt.get("refused"):
        lines.append(f"refused: {receipt['refused']}")
        moved = receipt.get("merged") or []
        if moved:
            lines.append(f"partial: merged in the member clones (not pushed, not pinned): {', '.join(moved)}")
        if receipt.get("restored"):
            lines.append(f"restored: the update's merge was undone in {', '.join(receipt['restored'])}")
        if receipt["refused"].startswith("unresolved_merge"):
            for m in receipt.get("members", []):
                clone = f"{workspace.rstrip('/')}/{m['path']}"
                lines.append(f"next: resolve in {_shown(clone)}, git add, git commit (or git merge --abort), then run "
                             f"gr2 review update {_shown(workspace)} {_shown(rid)}")
        return lines
    for m in receipt.get("members", []):
        lines.append(f"{m['key']}: {m['state']}")
        for path in m.get("conflicts", []):
            lines.append(f"  {_shown(path)}")
    for m in receipt.get("members", []):
        if m["state"] == "conflict":
            clone = f"{workspace.rstrip('/')}/{m['path']}"
            lines.append(f"next: resolve in {_shown(clone)}, git add, git commit, then run "
                         f"gr2 review update {_shown(workspace)} {_shown(rid)}")
    if new:
        # One line: a bare publish pushes every member's pinned head to its own remote, then the review.
        lines.append(f"next: gr2 review publish {_shown(workspace)} {_shown(new)}")
        lines.append(f"stamps and checks on {rid} do not carry; ask for stamps and run checks on {new}")
    elif not receipt.get("refused") and not any(m["state"] == "conflict" for m in receipt.get("members", [])):
        lines.append(f"{rid} is current: every member is at its target's tip; nothing to pin")
    return lines


def _head_fetch(repo: str, remote: str, head: str) -> str:
    # Private, so the skill's refusal table keeps one row per printed code. The command a refusal prints for a clone
    # that lacks the pinned head; shell-quoted, so a path with a space runs as printed.
    return f"git -C {_shown(repo)} fetch -- {_shown(remote)} {_shown(head)}"


def _open_from_range(workspace: str, review: str) -> str:
    # Private, so the skill's refusal table keeps one row per printed code. receive's line for a head on another
    # remote: a lane opened from the range the review carries needs no fetch of the head. No --lane-dir placeholder,
    # so the line runs as printed in a shell; open picks its default lane directory.
    return f"gr2 review open {_shown(workspace)} {_shown(review)}"


def _unpushed_pin(path: str, sha: str, remote: str, branch: str | None, grip_toml: str, compat: bool) -> str:
    """Private, so the skill's refusal table keeps one row per printed code: this is `store commit`'s and
    `store check`'s coverage refusal, which prints no `refused: <code>`.

    The next steps for a pin no remote branch holds. ``branch`` is the member's branch when it contains the pin,
    else None (detached, or a branch that does not hold it). The full-mode line is compat-only: a full-mode store
    carries such a pin as a layer and never prints this."""
    if branch:
        steps = [f"push it: git -C {_shown(path)} push -u {_shown(remote)} {_shown(branch)}"]
    else:
        steps = [f"push it: push a branch holding {sha} to {_shown(remote)}, then commit again"]
    if compat:
        steps.append(f'or keep it unpushed: add mode = "full" as the first line of {_shown(grip_toml)} '
                     "(a full-mode record carries it as a layer), then commit again")
    return "\n".join(steps)


def not_received(workspace: str, review: str) -> str:
    return (f"next: receive the published review into this workspace, then stamp it: "
            f"gr2 review receive {_shown(workspace)} {_shown(review)} --remote <member-remote>, then "
            f"gr2 review stamp {_shown(workspace)} {_shown(review)}")


def head_already_on_remote() -> str:
    return ("next: pin a head before you push it. For this head, either commit a new head and pin it before "
            "pushing, or pin this one with --ratified gr:<id>, naming an earlier pin of this same head that was stamped")


def at_base_not_ancestor(observed: str) -> str:
    return (f"check: the range's base is not in the history of the ref's tip {_shown(observed)}; re-freeze the range "
            f"on {_shown(observed)}, or pin without --at-base")


def at_base_unreachable() -> str:
    return ("check: the range's base is not on the remote; push the base the range was cut on, re-freeze the range "
            "on the live tip, or pin without --at-base")


def at_base_tip_unfetchable() -> str:
    return ("check: the ref's tip could not be fetched from the remote, so its history was not read; the ref moved "
            "after it was read, or the fetch failed in transport or auth; pin again on the live tip, or pin without --at-base")


def head_behind_remote_branch(remote_tip: str) -> str:
    return (f"next: the remote branch already holds commits past this head; pin what it holds: "
            f"gr2 review pin <workspace> --head {_shown(remote_tip)} with your other flags unchanged")


def remote_branch_diverged() -> str:
    return ("next: fetch the remote branch, integrate it into your branch locally (merge or rebase) as a new "
            "head, then pin that head before you push it")


def merge_row(refused: str, *, workspace: str, review: str, remote: str, path: str, head: str,
              checks: tuple[str, ...], siblings: dict | None = None) -> str | None:
    """The next command for one member's merge refusal, or None when no single command answers it. ``siblings`` maps
    a declared-set member's name to what the gate found for it: the newest review of it at its pin in this workspace
    (or None), its remote, its clone and its target's live head."""
    if refused.startswith("ancestry_unmeasurable"):
        clone = re.search(r"; clone: (.+)$", refused)
        return (f"next: git -C {_shown(clone[1] if clone else path)} fetch --unshallow, then run gr2 review merge "
                f"{_shown(workspace)} {_shown(review)} again; nothing was pushed for this member")
    if refused.startswith("review_not_on_member_remote"):
        return f"next: gr2 review publish {_shown(workspace)} {_shown(review)} --remote {_shown(remote)}"
    if refused.startswith("declared_set_pin_unreviewed"):
        # The tie reads the sibling's own review on the sibling's own remote, so the answer is to publish a review of
        # the sibling at that pin there: the one this workspace holds, with its real id and remote. With none here, the
        # line pins one (the publish is the next line after it); with no target head to pin against, a note says so.
        tied = re.match(r"declared_set_pin_unreviewed: (.+) is pinned at ([0-9a-f]+), ", refused)
        found = (siblings or {}).get(tied[1]) if tied else None
        if found is None:
            return f"next: gr2 review merge {_shown(workspace)} {_shown(review)}"
        if found["review"]:
            return (f"next: gr2 review publish {_shown(workspace)} {_shown(found['review'])} "
                    f"--remote {_shown(found['remote'])}")
        if found.get("base"):  # pin the sibling at its pin against its target's live head; its publish comes next
            return (f"next: gr2 review pin {_shown(workspace)} --repo {_shown(tied[1])} --remote {_shown(found['remote'])} "
                    f"--base {_shown(found['base'])} --head {_shown(found['pin'])} --ref {_shown(found['ref'])} "
                    f"--path {_shown(found['member_path'])} --source {_shown(found['path'])}")
        # No live head of the target to pin against, so no pin command would run as printed: say so, not as a next.
        return (f"note: no review of {_shown(tied[1])} at {tied[2]} is pinned in {_shown(workspace)}, and its remote "
                f"{_shown(found['remote'])} serves no {_shown(found['ref'])} to pin it against; gr2 review pin needs that "
                f"head as --base")
    if refused.startswith("check_set_fail"):
        # A recorded failure at these heads stays (a pass beside it does not erase it), so a rerun cannot clear it.
        return ("next: the set check failed at these heads and that failure is recorded; a rerun does not clear it. "
                "Fix the member, commit a new head, then pin, stamp, run gr2 check set on that head and publish it")
    if refused.startswith("check_set_"):
        # The one safe next command; a set is checked together, never member by member.
        return f"next: gr2 check set {_shown(workspace)} {_shown(review)} -- <your combined test command>"
    if refused.startswith("check_absent"):
        if "set" in checks:
            # Several members: a member tested alone cannot pass when it imports its siblings, so name the run that
            # can, the members together at the pinned heads, recorded under the missing name.
            return "\n".join(f"next: gr2 check set {_shown(workspace)} {_shown(review)} --name {_shown(n)} -- "
                             f"<your combined {_shown(n)} command>" for n in checks if n != "set")
        # `check run --name` takes ONE name (a repeated flag keeps the last), so each required check gets its own line.
        return "\n".join(f"next: gr2 check run {_shown(path)} --remote {_shown(remote)} --head {_shown(head)}"
                         f"{'' if n == 'test' else f' --name {_shown(n)}'} -- <your {_shown(n)} command>" for n in checks
                         if n != "set")  # `set` is never recorded by `check run`; its refusals are check_set_*
    if refused.startswith("approvals_unset"):
        return (f"next: say how many approvals this merge needs: rerun with --approvals N (--approvals 0 merges "
                f"without approvals), or set [approvals] required = N in {_shown(workspace)}/grip.toml")
    if refused.startswith("approvals_incomplete"):
        # A stamp writes the chain to each member remote in turn, so a read in that window finds it on some only.
        return (f"next: an approval stamp is still writing the chain to the members it names; retry the merge once "
                f"it finishes. If a member stays without it, any approver's gr2 review stamp {_shown(workspace)} "
                f"{_shown(review)} republishes the chain to the members that lack it")
    if refused.startswith("approvals_insufficient"):
        return (f"next: more approvers are required; each one stamps from their own checkout: "
                f"gr2 review stamp {_shown(workspace)} {_shown(review)}")
    if refused.startswith("check_fail"):
        together = ""
        if "set" in checks and any(n != "set" for n in checks):
            # Several members, so the member's own check may be red only because it imports its siblings.
            together = (f"; if it fails only because this member needs the others, check them together with "
                        f"gr2 check set {_shown(workspace)} {_shown(review)} -- <your combined test command> and merge "
                        f"with --check set; if the member itself is broken")
        return (f"next: the check failed at this head{together or ';'} "
                "fix it, commit a new head, then pin, stamp, check and publish that head")
    if refused.startswith("feature_moved"):
        # Name the check over the moved set, then the two real ways to merge it. "Pin the new head" alone pointed
        # at head_already_on_remote, which refuses a head already pushed, so the two refusals pointed at each other.
        moved = re.match(r"feature_moved: remote (\S+) is ", refused)
        branch = moved.group(1).removeprefix("refs/heads/") if moved else "<branch>"
        return (f"next: the feature branch moved after the pin; to check the set as it is now: gr2 check set "
                f"{_shown(workspace)} {_shown(review)} --at-remote-heads {_shown(branch)} -- <your combined test command>. "
                "To merge it, commit a new head and pin it before you push, or pin this head with "
                "--ratified gr:<id>, naming an earlier pin of this same head that was stamped")
    if refused.startswith("base_moved"):
        # Name the target the merge measured, so update merges THAT branch and not the remote's default.
        moved = re.match(r"base_moved: remote (\S+) is ", refused)
        into = moved.group(1).removeprefix("refs/heads/") if moved else "<branch>"
        return (f"next: the target moved after the pin; bring every member up to it and pin again: "
                f"gr2 review update {_shown(workspace)} {_shown(review)} --into {_shown(into)}")
    if refused.startswith("head_already_on_target"):
        # Not base_moved's answer: update would pin this same head again and refuse head_already_on_remote.
        return _nothing_to_merge()
    if refused.startswith("target_moved_at_push"):
        # The gate passed, then the target moved before the push landed: the same answer as base_moved.
        moved = re.match(r"target_moved_at_push: remote (\S+) is ", refused)
        into = moved.group(1).removeprefix("refs/heads/") if moved else "<branch>"
        return (f"next: the target moved after the gate read it, so nothing was pushed; bring every member up to it "
                f"and pin again: gr2 review update {_shown(workspace)} {_shown(review)} --into {_shown(into)}")
    if refused.startswith("target_deleted_at_push"):
        gone = re.match(r"target_deleted_at_push: remote (\S+) is gone", refused)
        into = gone.group(1).removeprefix("refs/heads/") if gone else "<branch>"
        return (f"next: the target {_shown(into)} was deleted on the remote after the gate read it, so nothing was pushed "
                f"for this member or any after it; restore it at the base the review was pinned on, then run the same "
                f"gr2 review merge again")
    if refused.startswith("push_rejected"):
        return ("next: the remote refused the push (the reason is git's message in the refusal: a hook or a protected "
                "branch) and the target is still at the base the gate read, so nothing was pushed for this member or "
                "any after it; clear the rejection on the remote, then run the same gr2 review merge again")
    if refused.startswith("push_failed"):
        return ("next: the push did not reach the remote (git's message says why: auth, network, a local hook) and the "
                "target is still at the base the gate read, so nothing was pushed for this member or any after it; fix "
                "that, then run the same gr2 review merge again")
    if refused.startswith("push_unconfirmed"):
        return (f"next: the push did not finish (a timeout or a failed start), so whether this member landed is unknown "
                f"and no member after it was pushed; when the remote answers, read it with gr2 review merged "
                f"{_shown(workspace)} {_shown(review)}, then run the same gr2 review merge again (a landed member "
                f"reads merged and is not pushed twice)")
    if refused.startswith("target_unread_at_push"):
        return (f"next: the push failed and the target could not be read after it, so whether this member landed is "
                f"unknown and no member after it was pushed; when the remote answers, read it with gr2 review merged "
                f"{_shown(workspace)} {_shown(review)}, then run the same gr2 review merge again (a landed member "
                f"reads merged and is not pushed twice)")
    if refused.startswith("platform_head_moved_after_merge"):
        late = re.search(r"head is (\S+);", refused)
        return (f"next: this member landed at the pinned head, and the later commit {late.group(1) if late else '<sha>'} "
                "is on the PR's branch only: review it as a new change (pin it on top of the merged target), or "
                f"remove it from the branch; members after this one were not landed, so run gr2 review merge "
                f"{_shown(workspace)} {_shown(review)} again once it is resolved")
    if refused.startswith("platform_head_unmeasured_after_merge"):
        return ("next: this member landed at the pinned head; read the PR's commits on the platform to see whether one "
                "was pushed during the land (review any such commit as a new change); members after this one were not "
                f"landed, so run gr2 review merge {_shown(workspace)} {_shown(review)} again once that is known")
    if refused.startswith("platform_pr_not_ready"):
        return "next: mark the PR ready (or reopen it) on the platform, then run the merge again; nothing was merged"
    if refused.startswith("platform_unmeasurable"):
        return ("next: install the GitHub CLI (gh) and authenticate it (gh auth login), then run the merge again; "
                "nothing was merged")
    if refused.startswith("platform_pr_ambiguous"):
        return "next: close the extra PRs so one open PR has this head, then run the merge again; nothing was merged"
    return None



def _partial_moved_row(*, workspace: str, key: str, member_path: str, path: str, remote: str, base: str, into: str,
                      feature: str, landed: list[str], approvals: int | None, waiting: list[dict] = (), moved: str = "feature") -> str:
    """The refused member's next on a PARTIAL merge whose feature branch moved (or, with moved="target", whose target
    moved, base being the observed target): the old pin cannot merge it (the
    check over the moved set does not move the pin), and a pin over every member refuses on the merged ones' old
    base, so the recovery is a new pin of this member alone, merged as its own review. Members the run left unpushed
    (`waiting`: key/path/member_path/remote/base/head) follow, each pinned alone at its pinned head, which is already
    its feature tip. One line, in the order it runs; `$R` carries the id each pin prints, and only the operator's
    commit and test command are placeholders."""
    def segment(k, mpath, p, rem, b, head, push):
        merge = f'gr2 review merge {_shown(workspace)} "$R" --from {_shown(feature)} --into {_shown(into)}'
        if approvals is not None:
            merge += f" --approvals {approvals}"
        pushed = f" && git -C {_shown(p)} push {_shown(rem)} {_shown(feature)}" if push else ""
        return (f"R=$(gr2 review pin {_shown(workspace)} --repo {_shown(k)} --path {_shown(mpath)} --remote "
                f"{_shown(rem)} --base {_shown(b)} --head {_shown(head)} --ref {_shown('refs/heads/' + into)} --source "
                f"{_shown(p)}){pushed} && gr2 review publish {_shown(workspace)} \"$R\" --remote {_shown(rem)} && "
                f"gr2 check run {_shown(p)} --remote {_shown(rem)} --head {_shown(head)} -- <your test command> && {merge}")

    steps = [segment(key, member_path, path, remote, base, "<your new commit>", True)]
    steps += [segment(w["key"], w["member_path"], w["path"], w["remote"], w["base"], w["head"], False)
              for w in waiting]
    then = (f" (then {', '.join(_shown(w['key']) for w in waiting)}, not pushed after the refusal, each pinned alone "
            "at its pinned head)") if waiting else ""
    if moved == "target":
        # The target moved, not the feature: `review update` pins every member and refuses on the merged ones, so the
        # member merges the observed target in (committed, not pushed) and the new pin's base is that target.
        return (f"next: {', '.join(_shown(k) for k in landed)} already merged and {_shown(into)} moved on "
                f"{_shown(key)}'s remote, so this review cannot merge {_shown(key)}; pin {_shown(key)} alone on a new "
                f"head{then}. First merge the target into the member and commit it, without pushing: git -C "
                f"{_shown(path)} fetch {_shown(remote)} {_shown('refs/heads/' + into)} && git -C {_shown(path)} merge "
                f"--no-edit {_shown(base)}; then, with <your new commit> its sha, run: {' && '.join(steps)}")
    return (f"next: {', '.join(_shown(k) for k in landed)} already merged and {_shown(key)}'s feature branch moved, so "
            f"this review cannot merge {_shown(key)}; pin {_shown(key)} alone on a new head{then}. First bring the "
            f"branch up and commit your change: git -C {_shown(path)} pull --ff-only {_shown(remote)} "
            f"{_shown(feature)}; then, with <your new commit> its sha, run: {' && '.join(steps)}")
