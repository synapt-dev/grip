"""Which engine a top-level root verb runs: found by the shape of the nearest root above the current directory.

A griplink root (full mode, section 13) is a git root holding refs/dev.synapt.grip/STOOK, or one whose HEAD tree holds
a <m>.griplink/001.2_pin gitlink: a plain `git clone` of a griplink root has main and no STOOK, and `gr2 checkout` is
the step after it. Any other workspace root (a store root, compat or today's full mode) runs the store engine. A root
is one shape or the other, never both; nothing here reads the shape from grip.toml.

A command typed inside a member checkout is refused by default, naming git; a store's grip.toml can say
`inside_member = "root"` to run it on the root above instead.
"""
from __future__ import annotations

import shlex
import tomllib
from dataclasses import dataclass
from pathlib import Path

from . import gitops, griplink

GRIPLINK, STORE = "griplink", "store"
INSIDE_MEMBER_VALUES = ("refuse", "root")


class RootVerbRefusal(RuntimeError):
    def __init__(self, message: str, code: int) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Root:
    path: Path
    shape: str          # GRIPLINK or STORE
    has_stook: bool     # False for a griplink root found by its gitlink alone (a plain clone)
    member: str | None  # the member checkout the command was typed in, if any


def _is_git_root(d: Path) -> bool:
    return (d / ".git").exists()


def _has_stook(d: Path) -> bool:
    return gitops.run(d, "rev-parse", "-q", "--verify", griplink.STOOK).returncode == 0


def _shape_rev(d: Path) -> str:
    """HEAD, or origin's main when HEAD is unborn (a plain clone of a remote whose HEAD names a missing branch)."""
    if gitops.run(d, "rev-parse", "-q", "--verify", "HEAD").returncode == 0:
        return "HEAD"
    return ORIGIN_MAIN


ORIGIN_MAIN = "refs/remotes/origin/main"


def _has_griplink_gitlink(d: Path) -> bool:
    """HEAD (or origin's main, HEAD unborn) holds a top-level <m>.griplink tree whose 001.2_pin is a gitlink. Reads
    the top level only, so an ordinary repo of any size costs one short listing."""
    rev = _shape_rev(d)
    listed = gitops.run(d, "ls-tree", "-z", rev)
    if listed.returncode:
        return False
    for entry in listed.stdout.split("\0"):
        meta, _, name = entry.partition("\t")
        if meta.split(" ")[1:2] == ["tree"] and name.endswith(".griplink"):
            pin = gitops.run(d, "ls-tree", rev, "--", f"{name}/{griplink.PIN}")
            if pin.returncode == 0 and pin.stdout.startswith("160000 "):
                return True
    return False


def _is_full_mode_shape(d: Path) -> bool:
    """The shape `store init --mode full` makes: a git root whose own git carries a stook, or whose HEAD holds a
    `<m>.griplink/001.2_pin` gitlink. Either alone is enough: a fresh full root has the stook and no member yet, and a
    clone of one has the gitlinks and no stook (a default clone does not fetch the stook's ref namespace). The git-root
    test comes first because `rev-parse` run in a plain subdirectory reads the enclosing repo's stook."""
    return _is_git_root(d) and (_has_stook(d) or _has_griplink_gitlink(d))


def _is_store_root(d: Path) -> bool:
    """A native store root (grip.toml beside a root .git, what `store init` makes). A spec workspace
    (.grip/workspace_spec.toml, the lanes' workspace) is not a root these verbs act on."""
    from .grip import _is_native_workspace

    return _is_native_workspace(d)


def _member_of(root: Path, start: Path) -> str | None:
    """The member whose checkout `start` is in, if any: a griplink member lives at <m>.griplink/001.2_pin, a store
    member at the path its root declares; for a store root any git checkout below the root that is not the root is
    taken as a member."""
    top = gitops.run(start, "rev-parse", "--show-toplevel")
    if top.returncode:
        return None
    here = Path(top.stdout.strip()).resolve()
    if here == root:
        return None
    try:
        rel = here.relative_to(root)
    except ValueError:
        return None
    parts = rel.parts
    if len(parts) == 2 and parts[0].endswith(".griplink") and parts[1] == griplink.PIN:
        return parts[0][: -len(".griplink")]
    return rel.as_posix()


def find(start: Path | None = None) -> Root | None:
    """The nearest root at or above `start` (default: the current directory), with its shape; None when there is
    none."""
    start = (start or Path.cwd()).resolve()
    for d in (start, *start.parents):
        if not _is_git_root(d):
            continue
        stook = _has_stook(d)
        if stook or _has_griplink_gitlink(d):
            return Root(d, GRIPLINK, stook, _member_of(d, start))
        if _is_store_root(d):
            if _in_spec_workspace(d):
                return None  # a lane's root (lanes are held): the lane and one-repo forms run unchanged
            return Root(d, STORE, False, _member_of(d, start))
    return None


def _in_spec_workspace(d: Path) -> bool:
    """d is, or sits inside, a spec workspace (.grip/workspace_spec.toml): where lanes live."""
    from .layout import grip_dir

    return any((grip_dir(p) / "workspace_spec.toml").is_file() for p in (d, *d.parents))


def inside_member(root: Path) -> str:
    """The store's `inside_member` setting: "refuse" (absent) or "root". Any other value refuses at exit 2."""
    doc_path = root / "grip.toml"
    if not doc_path.is_file():
        return "refuse"
    try:
        value = tomllib.loads(doc_path.read_text(encoding="utf-8")).get("inside_member", "refuse")
    except tomllib.TOMLDecodeError as exc:
        raise RootVerbRefusal(f"grip.toml at {root} cannot be read: {exc}", 2) from exc
    if value not in INSIDE_MEMBER_VALUES:
        raise RootVerbRefusal(
            f'grip.toml inside_member must be "refuse" or "root", got {value!r}; nothing was run', 2)
    return value


REFUSE_IN_MEMBER = ("add", "commit", "push")  # the verbs git also has inside a member (cut 2, issue A)


def resolve(verb: str, git_word: str, start: Path | None = None) -> Root | None:
    """The root a top-level verb acts on, or None when no root is above (the caller keeps its one-repo form). Inside
    a member checkout, add, commit and push refuse unless the store says inside_member = "root"; every other root
    verb acts on the root above."""
    root = find(start)
    if root is None:
        return None
    setting = inside_member(root.path)  # read first, so a typo refuses everywhere
    if root.member is not None and setting == "refuse" and verb in REFUSE_IN_MEMBER:
        raise RootVerbRefusal(
            f"this is a member checkout ({root.member}): use {git_word} here; gr2 {verb} acts on the workspace "
            f"root at {root.path}", 2)
    return root


def require_stook(root: Root, verb: str) -> None:
    """A verb that reads or cuts the STOOK refuses on a root found by its gitlink alone, naming gr2 checkout (which
    fetches the names); a local verb never fetches."""
    if root.shape == GRIPLINK and not root.has_stook:
        raise RootVerbRefusal(
            f"gr2 {verb}: this workspace's names were not fetched (a plain git clone has main only); run "
            f"gr2 checkout (or gr2 pull), then gr2 {verb} again", 4)


def fetch_names(root: Root, verb: str, remote: str, lenient: bool = False) -> Root:
    """A root found by its gitlink alone fetches its gr2 names once from `remote`, taking only the names it does not
    hold, so a local name is never overwritten. Refused at exit 4 when the root has no such remote or the remote
    holds no STOOK, and at exit 5 when the names cannot be listed or fetched. `lenient` (checkout, push) returns the
    root unchanged in each of those cases instead, so the verb runs as it did before names were fetched, with no remote
    reachable included. A root that has its STOOK is returned as it is, with no network."""
    if root.shape != GRIPLINK or root.has_stook:
        return root
    if remote.startswith("-") or gitops.unsafe_in_identifier(remote) or gitops.run(
            root.path, "remote", "get-url", remote).returncode:
        if lenient:
            return root
        raise RootVerbRefusal(
            f"gr2 {verb}: this workspace's names were not fetched and {shlex.quote(remote)} is not a remote of this root; "
            "run "
            f"git remote add {shlex.quote(remote)} <url>, then gr2 {verb} again", 4)
    listed = gitops.run(root.path, "ls-remote", "--refs", remote, f"{griplink.NS}/*")
    if listed.returncode:
        if lenient:
            return root
        raise RootVerbRefusal(f"gr2 {verb}: cannot list the gr2 names on {shlex.quote(remote)}: "
                              f"{(listed.stderr or '').strip()}; nothing was fetched", 5)
    try:
        rows = gitops.ls_remote_rows(listed.stdout)
    except gitops.LsRemoteMalformed as exc:
        if lenient:
            return root
        raise RootVerbRefusal(f"gr2 {verb}: cannot list the gr2 names on {shlex.quote(remote)}: "
                              "a row was malformed; nothing was "
                              "fetched", 5) from exc
    names = [n for _, n in rows if n.startswith(griplink.NS + "/")]
    missing = [n for n in names if gitops.run(root.path, "rev-parse", "-q", "--verify", n).returncode]
    if griplink.STOOK in missing:
        fetched = gitops.run(root.path, "fetch", "-q", "--no-tags", "--no-recurse-submodules", "--no-write-fetch-head",
                             "--refmap=", remote, *[f"{n}:{n}" for n in missing])
        if fetched.returncode and not lenient:
            raise RootVerbRefusal(f"gr2 {verb}: cannot fetch the gr2 names from {shlex.quote(remote)}: "
                                  f"{(fetched.stderr or '').strip()}; nothing past that step was written", 5)
    if not _has_stook(root.path):
        if lenient:
            return root
        raise RootVerbRefusal(
            f"gr2 {verb}: the remote {shlex.quote(remote)} has no gr2 names yet (no {griplink.STOOK}); nothing was "
            "fetched", 4)
    return Root(root.path, root.shape, True, root.member)
