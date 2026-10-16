"""A pinned review member's change, read from the member's OBJECTS: `git diff <base> <head>` in the member clone.

Never from the pinned range.patch text: format-patch text is ambiguous (an empty commit has no separator of its own,
so a message carrying a whole stanza reads as diff, and `git am --empty=keep` creates that file too). A member clone
that lacks the base or head refuses by name; there is no text fallback. A range with merges shows its net change.

The change is read from the merge-base of the pinned base and the head (`git diff base...head`), as a PR page reads
it: pin holds the base at the target's live tip, which may have moved past where the head branched, and a plain
`base head` diff would show what the target gained since as the member undoing it.
"""
from __future__ import annotations

from pathlib import Path

from . import gitops
from .grip import GripReviewRefused

# A shallow clone cuts the shared history even when both commits are present; else the head was re-cut or replaced.
NO_MERGE_BASE_NEXT = ("next: if the clone is shallow (git rev-parse --is-shallow-repository), deepen it "
                      "(git fetch --unshallow) and show again; if not, the head shares no commit with the target "
                      "(a re-cut or replaced member branch): rebase it onto the target and pin again")

# A reviewer's diff config must not swap in another program's output or recolour it.
_PLAIN = ("--no-ext-diff", "--no-textconv", "--no-color")


def _git(repo: Path, *args: str) -> str:
    proc = gitops.run(repo, *args, binary=True)
    if proc.returncode != 0:
        raise GripReviewRefused("member_diff_failed", " ".join(args), proc.stderr.decode("utf-8", "replace").strip())
    return proc.stdout.decode("utf-8", "replace")


def change(workspace: Path, member: dict[str, object], *, with_diff: bool) -> dict[str, object]:
    """{"files", "diffstat", and with_diff "diff"} for one member, from `git diff base...head` in its clone at
    <workspace>/<path>. Files come from `--numstat -z`, so a name git would quote stays one plain name."""
    repo, base, head = workspace / str(member["path"]), str(member["base"]), str(member["head"])
    for sha in (base, head):
        if gitops.run(repo, "cat-file", "-e", f"{sha}^{{commit}}").returncode != 0:
            exc = GripReviewRefused("member_objects_missing", f"{member['key']} {sha}", str(repo))
            exc.add_note("next: fetch the member's head into its clone (git fetch <remote> <head>), then show again")
            raise exc
    fork = gitops.run(repo, "merge-base", base, head)
    if fork.returncode != 0:  # no common history: nothing the head adds to the target can be named
        exc = GripReviewRefused("member_no_merge_base", f"{member['key']} {base}", head)
        exc.add_note(NO_MERGE_BASE_NEXT)
        raise exc
    fork_sha = fork.stdout.strip()
    fields = _git(repo, "diff", *_PLAIN, "--numstat", "-z", fork_sha, head).split("\0")
    files: list[str] = []
    added = removed = 0
    i = 0
    while i < len(fields) and fields[i]:
        a, r, name = fields[i].split("\t", 2)
        if name == "":  # a rename or copy: the old and new names follow as their own fields
            name, i = fields[i + 2], i + 3
        else:
            i += 1
        files.append(name)
        added += int(a) if a != "-" else 0
        removed += int(r) if r != "-" else 0
    commits = int(_git(repo, "rev-list", "--count", f"{base}..{head}").strip())
    out: dict[str, object] = {"files": files, "diffstat": {"files": len(files), "commits": commits,
                                                           "added": added, "removed": removed}}
    if with_diff:
        out["diff"] = _git(repo, "diff", *_PLAIN, fork_sha, head)
    return out


def printable(line: str) -> str:
    """A diff line as it may reach a terminal: the next_steps._shown character rule (every non-printable escaped,
    \\xNN or \\uNNNN), but not shell-quoted, and a tab kept as a tab so the hunks still read and apply."""
    return "".join(c if c.isprintable() or c == "\t" else (f"\\x{ord(c):02x}" if ord(c) < 256 else f"\\u{ord(c):04x}")
                   for c in line)


def summary(s: dict[str, int]) -> str:
    files = f"{s['files']} file{'s' if s['files'] != 1 else ''}"
    commits = f", {s['commits']} commits" if s["commits"] > 1 else ""
    return f"diff: {files}{commits}, +{s['added']} -{s['removed']}"
