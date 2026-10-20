"""Delete a record: its STOOK and its SHADOW in one act, never while another record pins it.

``delete`` removes ``.../STOOK`` and, when there is one, ``.../SHADOW`` from the remote in ONE atomic push, each
leased on the tracking tree; the STOOK never goes alone. It is refused, with nothing sent:
- while any SHADOW on the remote pins the record as a member (named by the pinning record), since its history
  must stay reachable through the shadows that recorded it; drop the member and cut a new shadow first;
- when the tracking tree is not current for the record's own refs or for any SHADOW on the remote, since a
  shadow pushed after the last fetch could pin it unseen; fetch first.
After the push, git removes the tracking refs through the fetch line, and the local refs are removed here.
A history that was pinned before survives under the arbons that pinned it."""
from __future__ import annotations

from pathlib import Path

from . import gitops
from . import leased_push
from . import shadow as sh
from . import tracking


class StillAMember(RuntimeError):
    def __init__(self, record: str, pinned_by: list[str]):
        super().__init__(f"{record} is pinned as a member by {', '.join(pinned_by)}; drop it there and cut a new shadow first")
        self.record, self.pinned_by = record, pinned_by


def delete(repo: Path, stook: str, *, org: str, remote: str = "origin") -> list[str]:
    """Delete ``stook`` and its SHADOW from the remote and here; returns the refs deleted."""
    shadow = sh.shadow_of(stook)
    path = sh._member_path(stook, org)                    # under refs/<org>/, a STOOK
    gitops.plain_remote(remote, lambda why: leased_push.PushRefused(stook, why))
    track_root = f"refs/{org}/{tracking.REMOTES}/{remote}/{org}/"
    proc = gitops.run(repo, "ls-remote", "--", remote, f"refs/{org}/*")
    if proc.returncode:
        raise leased_push.PushRefused(stook, f"ls-remote {remote} failed: {proc.stderr.strip()[:200]}")
    now = {}
    for line in proc.stdout.split("\n"):
        if not line:
            continue
        sha, name = line.split("\t", 1)
        if not name.endswith("^{}"):
            now[name] = sha
    shadows_now = {n for n in now if n.endswith("/SHADOW")}
    tracked_shadows = {f"refs/{org}/" + r[len(track_root):] for r in gitops.for_each_ref_rows(gitops.run(
        repo, "for-each-ref", "--format=%(refname)", track_root).stdout) if r.endswith("/SHADOW")}
    for name in sorted(shadows_now | tracked_shadows | {stook}):
        if now.get(name) != tracking.tracked(repo, name, org=org, remote=remote):
            raise leased_push.PushRefused(name, f"stale tracking: the remote has {now.get(name) or 'nothing'}, the "
                                                f"last fetch recorded {tracking.tracked(repo, name, org=org, remote=remote) or 'nothing'}; fetch first")
    pinned_by = []
    for name in sorted(shadows_now - {shadow}):
        if path in sh._pins_of(repo, now[name]):
            pinned_by.append(sh._member_path(name[: -len("SHADOW")] + "STOOK", org))
    if pinned_by:
        raise StillAMember(path, pinned_by)
    if stook not in now:
        raise leased_push.PushRefused(stook, f"no such record on {remote}")
    refs = [stook] + ([shadow] if shadow in now else [])
    args = ["push", "--porcelain", "--atomic"] + [f"--force-with-lease={r}:{now[r]}" for r in refs] + ["--", remote] + [f":{r}" for r in refs]
    proc = gitops.run(repo, *args)
    if proc.returncode:
        raise leased_push.PushRejected(f"delete turned away by {remote}: {(proc.stdout + proc.stderr).strip()[-400:]}")
    for r in refs:
        gitops.run(repo, "update-ref", "-d", r)
    return refs
