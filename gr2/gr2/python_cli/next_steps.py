"""What a review refusal needs next, said by gr2 itself.

An operator who meets a refusal should not have to read gr2's source to learn what comes next. Each line here
names the next command for the state the refusal measured, and only the legitimate path: a different approver,
a new pin, a publish, a check. No line suggests changing who Git says you are, and no line suggests deleting a
remote branch; the tests check every line for both.
"""
from __future__ import annotations

REVIEW_ORDER = """\b\n\
A review goes through six steps, in this order:
  1. gr2 review pin      pin the head before pushing it (needs gr2 store init)
  2. push the head       gr2 push, or git push
  3. gr2 review stamp    an approver other than the author stamps it
  4. gr2 check run       record the check at the exact head on the remote
  5. gr2 review publish  put the pinned review on each member remote
  6. gr2 review merge    merges only if heads still match the pin and check
Each refusal along the way names the next command."""


def self_approval(workspace: str, review: str) -> str:
    return (f"next: a different approver stamps this review from their own checkout: "
            f"gr2 review stamp <their workspace> {review}")


def head_already_on_remote() -> str:
    return ("next: pin a head before you push it. For this head, either commit a new head and pin it before "
            "pushing, or pin this one with --ratified <receipt id> naming the ratify that already covers it")


def merge_row(refused: str, *, workspace: str, review: str, remote: str, path: str, head: str,
              checks: tuple[str, ...]) -> str | None:
    """The next command for one member's merge refusal, or None when no single command answers it."""
    if refused.startswith("review_not_on_member_remote"):
        return f"next: gr2 review publish {workspace} {review} --remote {remote}"
    if refused.startswith("check_absent"):
        # `check run --name` takes ONE name (a repeated flag keeps the last), so each required check gets its own line.
        return "\n".join(f"next: gr2 check run {path} --remote {remote} --head {head}"
                         f"{'' if n == 'test' else f' --name {n}'} -- <your {n} command>" for n in checks)
    if refused.startswith("approvals_insufficient"):
        return (f"next: more approvers are required; each one stamps from their own checkout: "
                f"gr2 review stamp {workspace} {review}")
    if refused.startswith("check_fail"):
        return ("next: the check failed at this head; fix it, commit a new head, then pin, stamp, check and "
                "publish that head")
    if refused.startswith("feature_moved"):
        return ("next: the feature branch moved after the pin; pin the new head with gr2 review pin, then "
                "stamp, check and publish it")
    if refused.startswith("base_moved"):
        return "next: the target moved after the pin; pin again on the new base with gr2 review pin"
    return None
