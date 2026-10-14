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
A review goes through six steps, in this order:
  1. gr2 review pin      pin the head before pushing it (needs gr2 store init)
  2. push the head       gr2 push, or git push
  3. gr2 review publish  put the pinned review on each member remote
  4. gr2 review stamp    an approver other than the author stamps it; in their
                         own workspace, after gr2 review receive
  5. gr2 check run       record the check at the exact head on the remote, for
                         every member; a review over several members is also
                         checked together with gr2 check set, one command over
                         every member
  6. gr2 review merge    merges only if heads still match the pin and check
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


def not_received(workspace: str, review: str) -> str:
    return (f"next: receive the published review into this workspace, then stamp it: "
            f"gr2 review receive {_shown(workspace)} {_shown(review)} --remote <member-remote>, then "
            f"gr2 review stamp {_shown(workspace)} {_shown(review)}")


def head_already_on_remote() -> str:
    return ("next: pin a head before you push it. For this head, either commit a new head and pin it before "
            "pushing, or pin this one with --ratified <receipt id> naming the ratify that already covers it")


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
        # `check run --name` takes ONE name (a repeated flag keeps the last), so each required check gets its own line.
        return "\n".join(f"next: gr2 check run {_shown(path)} --remote {_shown(remote)} --head {_shown(head)}"
                         f"{'' if n == 'test' else f' --name {_shown(n)}'} -- <your {_shown(n)} command>" for n in checks
                         if n != "set")  # `set` is never recorded by `check run`; its refusals are check_set_*
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
                "--ratified <receipt id> naming the ratify that covers it")
    if refused.startswith("base_moved"):
        return "next: the target moved after the pin; pin again on the new base with gr2 review pin"
    if refused.startswith("platform_head_moved_after_merge"):
        late = re.search(r"head is (\S+);", refused)
        return (f"next: this member landed at the pinned head, and the later commit {late.group(1) if late else '<sha>'} "
                "is on the PR's branch only: review it as a new change (pin it on top of the merged target), or "
                f"remove it from the branch; members after this one were not landed, so run gr2 review merge "
                f"{_shown(workspace)} {_shown(review)} again once it is resolved")
    if refused.startswith("platform_pr_not_ready"):
        return "next: mark the PR ready (or reopen it) on the platform, then run the merge again; nothing was merged"
    if refused.startswith("platform_pr_ambiguous"):
        return "next: close the extra PRs so one open PR has this head, then run the merge again; nothing was merged"
    return None
