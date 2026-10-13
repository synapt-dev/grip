"""ARBON and the close: a SHADOW commit given a name at a close, and the one atomic leased push that carries it.

A STOOK moves when its owner writes, a SHADOW moves when a snapshot is cut, an ARBON never moves. The arbon of
``.../<thing>/STOOK`` for close ``<rheon>`` is ``.../<thing>/__arbons__/<rheon>``: an annotated tag object on the
SHADOW commit. Making one where one exists is refused, with nothing written.

``close`` cuts the SHADOW (``snapshot``), checks its members (``recurse``), makes the arbon, and pushes the
thing's STOOK, its SHADOW and the arbon in ONE atomic push leased on the tracking tree. If the push is turned
away, nothing moved on the remote and the local arbon is removed again, so the close can be retried.

``recurse`` (the members, by membership):
- ``check`` (the default): every pinned member must still be on the remote, its current tip there equal to or
  descending from the pin, checked by a local walk; a member that moved backwards or sideways is refused by
  name and both shas, and a commit not held even after a fetch is refused as unverifiable;
- ``on-demand``: the members' STOOKs are pushed in the same atomic push, for a clone that holds them; a member
  this clone has no value for is refused by name;
- ``none``: the thing's own refs only."""
from __future__ import annotations

from pathlib import Path
from typing import NamedTuple

from . import gitops
from . import leased_push
from . import shadow as sh
from .agent_record import _leaf


class ArbonExists(RuntimeError):
    """An arbon for this close exists; arbons never move. Nothing was written."""


class MemberNotCurrent(RuntimeError):
    def __init__(self, member: str, pin: str, now: str | None):
        super().__init__(f"{member}: the shadow pins {pin}, the remote now has {now or 'nothing'}, "
                         f"which does not descend from it")
        self.member, self.pin, self.now = member, pin, now


class Closed(NamedTuple):
    shadow: str
    arbon: str           # the tag object id
    pushed: dict[str, str]


def arbon_ref(stook: str, rheon: str) -> str:
    sh.shadow_of(stook)                                     # it is a STOOK ref
    return f"{stook[: -len('/STOOK')]}/__arbons__/{_leaf(rheon, 'rheon')}"


def _value(repo: Path, ref: str) -> str | None:
    proc = gitops.run(repo, "rev-parse", "-q", "--verify", ref)
    return proc.stdout.strip() if proc.returncode == 0 else None


def arbon(repo: Path, stook: str, rheon: str, *, message: str | None = None) -> str:
    """Tag the current SHADOW beside ``stook`` as the arbon of ``rheon``; returns the tag object id."""
    ref = arbon_ref(stook, rheon)
    if _value(repo, ref):
        raise ArbonExists(f"{ref} exists; an arbon never moves")
    target = _value(repo, f"{sh.shadow_of(stook)}^{{commit}}")
    if not target:
        raise ValueError(f"no SHADOW beside {stook}; cut one first")
    ident = gitops.run(repo, "var", "GIT_COMMITTER_IDENT").stdout.strip()
    body = f"object {target}\ntype commit\ntag {rheon}\ntagger {ident}\n\n{message or f'close {rheon}'}\n"
    proc = gitops.run(repo, "mktag", input=body)
    if proc.returncode:
        raise RuntimeError(f"git mktag failed: {proc.stderr.strip()[:200]}")
    tag = proc.stdout.strip()
    if gitops.run(repo, "update-ref", ref, tag, "0" * 40).returncode:
        raise ArbonExists(f"{ref} appeared during the close; an arbon never moves")
    return tag


def check_members(repo: Path, shadow_commit: str, *, org: str, remote: str = "origin") -> None:
    """``recurse=check``: every member the shadow pins is still on the remote at or past its pin."""
    pins = sh._pins_of(repo, shadow_commit)
    if not pins:
        return
    names = {path: (f"refs/{org}/{path}/SHADOW", f"refs/{org}/{path}/STOOK") for path in pins}
    now = leased_push._remote_values(repo, remote, [r for pair in names.values() for r in pair])
    for path, pin in pins.items():
        tip = now.get(names[path][0]) or now.get(names[path][1])
        if tip is None:
            raise MemberNotCurrent(path, pin, None)
        if tip != pin:
            tip_commit = gitops.run(repo, "rev-parse", "-q", "--verify", f"{tip}^{{commit}}").stdout.strip() or tip
            if sh._walk(repo, path, pin, tip_commit, remote) not in ("same", "ff"):
                raise MemberNotCurrent(path, pin, tip)


def close(repo: Path, stook: str, rheon: str, members: list[str], *, org: str, remote: str = "origin",
          snapshot: bool = True, recurse: str = "check", allow_rewind: bool = False,
          message: str | None = None) -> Closed:
    if recurse not in ("check", "on-demand", "none"):
        raise ValueError(f"recurse is check, on-demand or none, not {recurse!r}")
    ref = arbon_ref(stook, rheon)                          # names first: a bad rheon writes nothing
    if _value(repo, ref):
        raise ArbonExists(f"{ref} exists; an arbon never moves")
    member_refs = []
    if recurse == "on-demand":
        for m in members:
            if _value(repo, m) is None:
                raise leased_push.PushRefused(m, "on-demand: this clone holds no value for the member")
        member_refs = list(members)
    if snapshot:
        sh.cut(repo, stook, members, org=org, remote=remote, allow_rewind=allow_rewind)
    shadow_ref = sh.shadow_of(stook)
    shadow_commit = _value(repo, f"{shadow_ref}^{{commit}}")
    if not shadow_commit:
        raise ValueError(f"no SHADOW beside {stook}; close with snapshot=True or cut one first")
    if recurse == "check":
        check_members(repo, shadow_commit, org=org, remote=remote)
    tag = arbon(repo, stook, rheon, message=message)
    try:
        pushed = leased_push.push(repo, [stook, shadow_ref, ref] + member_refs, org=org, remote=remote,
                                  atomic=True, allow_rewind=allow_rewind)
    except Exception:
        gitops.run(repo, "update-ref", "-d", ref, tag)     # nothing reached the remote; the close can be retried
        raise
    return Closed(shadow_commit, tag, pushed)
