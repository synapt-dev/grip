"""`gr2 review update`: bring every member of a pinned review up to its moved target, then pin the result again.

When the target moves after a pin, `review merge` refuses base_moved and a pinned review cannot move to a new base.
Update merges the live tip of the target, fetched from each ROW's recorded remote (the remote the pin checks, not
the clone's origin), into each member clone's feature branch, and, when every member is clean, pins the merged
heads as a new review whose base is that tip. It never pushes, never resolves a conflict, and never carries stamps:
a new head is a new reading.

A merge, not a rebase: the reviewed head stays an ancestor of the new head, so the push is a fast-forward and the
land (a two-parent merge of base and head) takes the shape it already expects.

Exit codes match `review merge` where they overlap: 0 done (pinned again, or every member already up to date and
the pin current), 2 refused before anything moved (unresolved_merge, member_dirty, member_untracked_in_the_way, fetch_failed, an
unreadable or unresolved review, an unresolved target), 3 a conflict in at least one member (nothing pinned), 4 partial (a member
was merged before a later step failed in a way other than a conflict; the receipt names what moved).
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from . import defaults, gitops, grip

EXIT_DONE, EXIT_REFUSED, EXIT_CONFLICT, EXIT_PARTIAL = 0, 2, 3, 4


def _paths(repo: Path, *args: str) -> list[str]:
    return [p for p in gitops.run(repo, *args, "-z").stdout.split("\0") if p]


def _conflicts(repo: Path) -> list[str]:
    return _paths(repo, "diff", "--name-only", "--diff-filter=U")


def _head(repo: Path) -> str:
    return gitops.run(repo, "rev-parse", "HEAD").stdout.strip()


def _in_the_way(repo: Path, tip: str) -> list[str]:
    """Untracked paths the merge of `tip` would overwrite: what the target changed since the merge base, against
    `ls-files --others` (ignored files excluded, as git's own merge treats them). A file where the target adds a
    directory, or the reverse, counts too. Untracked files anywhere else stay allowed."""
    untracked = _paths(repo, "ls-files", "--others", "--exclude-standard")
    if not untracked:
        return []
    base = gitops.run(repo, "merge-base", "HEAD", tip).stdout.strip()
    changed = set(_paths(repo, "diff", "--name-only", base, tip) if base
                  else _paths(repo, "ls-tree", "-r", "--name-only", tip))
    return [u for u in untracked
            if u in changed or any(c.startswith(u + "/") or u.startswith(c + "/") for c in changed)]


def merge_in_progress(repo: Path) -> bool:
    return gitops.run(repo, "rev-parse", "-q", "--verify", "MERGE_HEAD").returncode == 0


def update(workspace: Path, review_id: str | None = None, into: str | None = None) -> tuple[int, dict]:
    ws = workspace.resolve()
    receipt: dict = {"review": review_id, "into": into, "members": [], "new_review": None, "exit": EXIT_REFUSED,
                     "refused": None}

    def done(code: int, refused: str | None = None) -> tuple[int, dict]:
        receipt.update(exit=code, refused=refused)
        return code, receipt

    if not review_id:
        # The same default as `review stamp` and `review merge`: THE pinned review at the members' current heads.
        def members_of(bind: str) -> list[tuple[Path, str]]:
            return [((ws / m["path"]).resolve(), m["head"]) for m in grip.show_review_commit(ws, bind)["members"]]
        try:
            review_id, _ = defaults.review([b for b, _ in grip.list_review_binds(ws)], members_of, defaults.current_head)
        except (defaults.Unresolved, grip.GripInitError, grip.GripCorruptError) as exc:
            return done(EXIT_REFUSED, f"review_unresolved: {exc}")
    try:
        view = grip.show_review_commit(ws, review_id.removeprefix("gr:"))
    except (grip.GripInitError, grip.GripCorruptError, RuntimeError, OSError, ValueError, KeyError,
            subprocess.SubprocessError) as exc:
        return done(EXIT_REFUSED, f"review_unreadable: {exc}")
    receipt["review"] = view["id"]
    # The checks `review publish` makes too (grip._member_preflight, one copy): a credential in a recorded remote, or
    # a member clone already mid-merge (`unresolved_merge`; resolve and commit, or abort, then run update again).
    try:
        grip._member_preflight(ws, view["id"].removeprefix("gr:"), for_push=False)
    except grip.GripReviewRefused as exc:
        if exc.refusal == "unresolved_merge":
            # The member that blocked, so the next line can name its clone (the receipt is all next_steps reads).
            receipt["members"] = [{"key": m["key"], "path": m["path"], "state": "unresolved_merge"}
                                  for m in view["members"] if m["key"] == exc.expected]
        return done(EXIT_REFUSED, f"{exc.refusal}: {exc.expected}: {exc.observed}")
    members = [dict(m, repo=(ws / m["path"]).resolve()) for m in view["members"]]

    # Every refusal that moves nothing comes before the first merge, across ALL members.
    dirty, branches = [], {}
    for m in members:
        repo = m["repo"]
        if gitops.run(repo, "rev-parse", "--git-dir").returncode:
            return done(EXIT_REFUSED, f"member_absent: {m['key']}: no git checkout at {repo}")
        branch = gitops.run(repo, "symbolic-ref", "-q", "--short", "HEAD").stdout.strip()
        if not branch:
            return done(EXIT_REFUSED, f"detached_head: {m['key']}: check out the feature branch first")
        branches[m["key"]] = branch
        # Untracked files are not dirty; the ones a merge would overwrite are refused below, once the tip is fetched.
        status = gitops.run(repo, "status", "--porcelain", "--untracked-files=no").stdout
        changed = [line[3:] for line in status.splitlines()]
        if changed:
            dirty.append(f"{m['key']}: {', '.join(changed)}")
    if dirty:
        return done(EXIT_REFUSED, f"member_dirty: {'; '.join(dirty)}; commit or stash, then run update again")

    # The target is the one each member was pinned against. A review pinned before that was recorded names no
    # target, and the remote's default is not one: after a promotion the default holds the old base too, so it
    # would merge the wrong branch into the work. Such a review asks for --into.
    targets = {}
    for m in members:
        branch = into or m.get("target", "").removeprefix("refs/heads/")
        if not branch:
            return done(EXIT_REFUSED, f"into_unresolved: {m['key']}: this review was pinned before its target was "
                                      "recorded; pass --into <the branch it was pinned against>")
        targets[m["key"]] = branch
    receipt["into"] = into or (next(iter(set(targets.values()))) if len(set(targets.values())) == 1 else None)

    # Phase one, still before any merge: fetch every member's tip, then refuse untracked files in the way.
    tips, in_the_way = {}, []
    for m in members:
        repo, key = m["repo"], m["key"]
        # The ROW's remote: the pin checks the base against that remote's live head, whatever the clone's origin is.
        fetched = gitops.run(repo, "fetch", "-q", "--", m["remote"], f"refs/heads/{targets[key]}")
        tip = gitops.run(repo, "rev-parse", "-q", "--verify", "FETCH_HEAD^{commit}").stdout.strip() \
            if not fetched.returncode else ""
        if not tip:
            return done(EXIT_REFUSED, f"fetch_failed: {key}: {m['remote']} {targets[key]}: {fetched.stderr.strip()}")
        tips[key] = tip
        paths = _in_the_way(repo, tip)
        if paths:
            in_the_way.append(f"{key}: {', '.join(paths)}")
    if in_the_way:
        return done(EXIT_REFUSED, f"member_untracked_in_the_way: {'; '.join(in_the_way)}; move or remove them, "
                                  "then run update again")

    # Phase two: merge.
    merged, conflicted = [], False
    receipt["merged"] = merged  # filled as members merge, so a partial names what moved
    for m in members:
        repo, key = m["repo"], m["key"]
        row = {"key": key, "path": m["path"], "remote": m["remote"], "into": targets[key], "conflicts": []}
        receipt["members"].append(row)
        tip = row["base"] = tips[key]
        if gitops.run(repo, "merge-base", "--is-ancestor", tip, "HEAD").returncode == 0:
            row.update(state="up_to_date", head=_head(repo))
            continue
        proc = gitops.run(repo, "merge", "--no-edit", "--no-ff", "-m",
                    f"Merge {targets[key]} ({tip[:12]}) into {branches[key]}", tip)
        if proc.returncode == 0:
            row.update(state="merged", head=_head(repo))
            merged.append(key)
        elif merge_in_progress(repo):
            row.update(state="conflict", conflicts=_conflicts(repo), head=_head(repo))
            conflicted = True
        else:
            why = f"merge_failed: {key}: {(proc.stderr or proc.stdout).strip()}"
            return done(EXIT_PARTIAL if merged else EXIT_REFUSED, why)
    if conflicted:
        return done(EXIT_CONFLICT)

    current = all(r["state"] == "up_to_date" and r["head"] == m["head"] and r["base"] == m["base"]
                  for r, m in zip(receipt["members"], members))
    if current:
        return done(EXIT_DONE)
    rows = [{"key": m["key"], "remote": m["remote"], "path": m["path"], "base": r["base"], "head": r["head"],
             "ref": f"refs/heads/{r['into']}", "title": m.get("title", ""), "body": m.get("body", ""),
             "source": str(m["repo"])} for r, m in zip(receipt["members"], members)]
    try:
        receipt["new_review"] = "gr:" + grip.create_review_bind_commit(ws, rows)
    except (grip.GripReviewRefused, grip.GripCorruptError, grip.GripInitError) as exc:
        return done(EXIT_PARTIAL if merged else EXIT_REFUSED, f"pin_refused: {exc}")
    return done(EXIT_DONE)
