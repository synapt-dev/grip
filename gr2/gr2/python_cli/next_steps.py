"""What a review refusal needs next, said by gr2 itself.

An operator who meets a refusal should not have to read gr2's source to learn what comes next. Each line here
names the next command for the state the refusal measured, and only the legitimate path: a different approver,
a new pin, a publish, a check. No line suggests changing who Git says you are, and no line suggests deleting a
remote branch; the tests check every line for both.
"""
from __future__ import annotations

import re
import shlex


def _shown(value: str) -> str:
    """A value as it may appear in a printed command: every non-printable character escaped (\\xNN, \\uNNNN) so
    a name cannot colour the terminal or start a forged line, and shell-quoted so the line runs as printed."""
    # Not only C0, DEL and C1: anything Python does not call printable (bidi overrides such as U+202E, line and
    # paragraph separators, zero-width spaces) can reorder or split a printed line, so it is escaped too.
    safe = "".join(c if c.isprintable() else (f"\\x{ord(c):02x}" if ord(c) < 256 else f"\\u{ord(c):04x}")
                   for c in str(value))
    return shlex.quote(safe)

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
              checks: tuple[str, ...]) -> str | None:
    """The next command for one member's merge refusal, or None when no single command answers it."""
    if refused.startswith("review_not_on_member_remote"):
        return f"next: gr2 review publish {_shown(workspace)} {_shown(review)} --remote {_shown(remote)}"
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
