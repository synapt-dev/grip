"""Full mode on the griplink root: a STOOK and a SHADOW.

Step 1: commit, push and checkout. Step 2: status, record, pin and follow. Functions over gitops.run; no verb is
wired yet, the top-level names come with the CLI surface. Ported from the prototype that carried the design, whose
rows ran on real git.

The shape, one level:
  refs/heads/main                             the SHADOW: a standard commit
  refs/dev.synapt.grip/SHADOW                 the same commit
  refs/dev.synapt.grip/STOOK                  the same root tree with names in it; its own chain
  refs/dev.synapt.grip/__members__/<m>/STOOK  <m>'s workspace changes, a chain of GripCommits
  .gitmodules  [submodule "<m>"] path = <m>.griplink/001.2_pin  url = <remote>  ignore = dirty
  <m>                       120000 -> <m>.griplink/001.2_pin
  <m>.griplink/001.2_pin    160000 gitlink: always a sha, the member's real commit (the base)
  <m>.griplink/002.2_head   in the STOOK "ref: refs/dev.synapt.grip/__members__/<m>/STOOK" (a branch) or a sha;
                            in the SHADOW the sha the branch pointed at when the shadow was cut; absent while empty
  <m>.griplink/005.2r_records/<id>/002.2_head   the same, for a record: "ref: refs/dev.synapt.grip/__records__/<id>"
                            or a sha; the record's chain lives at that ref

A member's change stays in the workspace (its GripCommit chain) until it is promoted; the member's own git is
never written here. The shadow's parents are the previous shadow, then every sha its heads resolved to, so a plain
clone of main carries every workspace change.
"""
from __future__ import annotations

import json
import os
import re
import unicodedata
from pathlib import Path

from . import gitops

NS = "refs/dev.synapt.grip"
STOOK = NS + "/STOOK"
SHADOW = NS + "/SHADOW"
MAIN = "refs/heads/main"
HEADF, RECS = "002.2_head", "005.2r_records"
REFP = "ref: "
PIN = "001.2_pin"
GC_TREE, GC_PARENTS = "001.2_tree.griptree", "002.2r_parents"
GC_AUTHOR, GC_COMMITTER, GC_ADATE, GC_CDATE, GC_MSG = (
    "003.2_author", "004.2_committer", "005.2_author_date", "006.2_commit_date", "007.2_message")
FT_SCHEMA, FT_KIND, FT_REF, FT_OVERLAY, FT_DELETED = (
    "001.2_schema", "002.0_kind", "005.2_ref", "014.2_overlay", "015.2_deleted")
FIELD_SCHEMA = "dev.synapt.grip.field.v1alpha1"
EMPTY_BLOB = "e69de29bb2d1d6434b8b29ae775ad8c2e48c5391"
ONE_BRANCH = "full mode carries one root branch for now; more than one comes later"
REC_KIND, REC_ID, REC_TEXT, REC_COMMIT = "001.2_kind", "002.2_id", "003.2_record", "005.2_commit"


class GriplinkRefusal(RuntimeError):
    """A refusal with a stable exit status: 1 git's own "nothing to commit", 3 a named refusal, 5 a push git refused."""

    def __init__(self, message: str, code: int) -> None:
        super().__init__(message)
        self.code = code


# ---- git plumbing, through gitops ---------------------------------------------------------------------------------
def _git(repo: Path, *args: str, input: str | bytes | None = None, env: dict | None = None) -> str:
    return gitops.out(repo, *args, input=input, env=env)


def _try(repo: Path, *args: str) -> str | None:
    proc = gitops.run(repo, *args)
    return proc.stdout.strip() if proc.returncode == 0 and proc.stdout.strip() else None


def _blob(repo: Path, data: str | bytes) -> str:
    raw = data.encode() if isinstance(data, str) else data
    return gitops.out_bytes(repo, "hash-object", "-w", "--stdin", input=raw).decode().strip()


def _mktree(repo: Path, entries) -> str:
    """entries: (mode, type, sha, name)"""
    return _git(repo, "mktree", input="".join("%s %s %s\t%s\n" % e for e in entries))


def _ls_tree(repo: Path, tree: str | None, recursive: bool = False) -> dict[str, tuple[str, str, str]]:
    out: dict[str, tuple[str, str, str]] = {}
    if not tree:
        return out
    raw = gitops.out_bytes(repo, "ls-tree", "-z", *(["-r"] if recursive else []), tree)
    for line in raw.decode("utf-8", "surrogateescape").split("\0"):
        if not line:
            continue
        meta, name = line.split("\t", 1)
        mode, typ, sha = meta.split(" ")
        out[name] = (mode, typ, sha)
    return out


def _build_tree(repo: Path, files: dict[str, tuple[str, str, str]]) -> str:
    top, subs = {}, {}
    for p, v in files.items():
        head, _, rest = p.partition("/")
        if rest:
            subs.setdefault(head, {})[rest] = v
        else:
            top[head] = v
    entries = [(m, t, s, n) for n, (m, t, s) in top.items()]
    entries += [("040000", "tree", _build_tree(repo, sub), d) for d, sub in subs.items()]
    return _mktree(repo, entries)


def _has(repo: Path, sha: str) -> bool:
    return gitops.run(repo, "cat-file", "-e", sha).returncode == 0


def _copy_blob(src: Path, dst: Path, sha: str) -> None:
    if not _has(dst, sha):
        got = _blob(dst, gitops.out_bytes(src, "cat-file", "blob", sha))
        if got != sha:
            raise GriplinkRefusal(f"copying blob {sha} gave {got}", 3)


def _cur(repo: Path, ref: str) -> str | None:
    return _try(repo, "rev-parse", "-q", "--verify", ref)


def _anc(repo: Path, a: str, b: str) -> bool | None:
    """a is an ancestor of (or equal to) b: True, False, or None when git cannot tell (gitops.is_ancestor)."""
    return gitops.is_ancestor(repo, a, b)


def _tree_of(repo: Path, rev: str | None) -> str | None:
    return _try(repo, "rev-parse", "-q", "--verify", rev + "^{tree}") if rev else None


def _git_dir(root: Path) -> Path:
    return (root / _git(root, "rev-parse", "--git-dir")).resolve()


# ---- the names ---------------------------------------------------------------------------------------------------
def member_ref(m: str) -> str:
    return f"{NS}/__members__/{m}/STOOK"


def record_ref(rid: str) -> str:
    return f"{NS}/__records__/{rid}"


def rewound_ref(tip: str) -> str:
    return f"{NS}/rewound/{tip}"


_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_MODES = ("100644", "100755", "120000")


def _check_name(m: str) -> None:
    """A member name reaches paths, config keys and ref names, so it is a plain name or it is refused."""
    if not _NAME.fullmatch(m) or ".." in m:
        raise GriplinkRefusal(f"{m!r}: not a member name gr2 takes (letters, digits, '.', '_' and '-', starting with a "
                              "letter or digit, no '..'); nothing was changed", 3)


def _check_url(m: str, url: str) -> None:
    """A url read from .gitmodules never reaches git as an option."""
    if not url or url.startswith("-") or gitops.unsafe_in_identifier(url):
        raise GriplinkRefusal(f"{m}: the url {url!r} in .gitmodules is not one gr2 passes to git; nothing was changed", 3)


def _check_path(m: str, p: str) -> None:
    """A path from a workspace change (the root's remote wrote it) stays inside the member and out of its .git."""
    parts = p.split("/")
    if p.startswith("/") or any(c in ("", ".", "..") for c in parts) or any(
            c.rstrip(". ").lower() in (".git", "git~1") for c in parts):
        raise GriplinkRefusal(f"{m}: the workspace change names a path gr2 will not write ({p!r}); nothing was "
                              "checked out", 3)


def member_dir(root: Path, m: str) -> Path:
    return root / f"{m}.griplink" / PIN


def _links(root: Path, tree: str) -> dict[str, str]:
    return {n[: -len(".griplink")]: s for n, (_, _, s) in _ls_tree(root, tree).items() if n.endswith(".griplink")}


def _is_field(path: str) -> bool:
    """A griplink's head, or a record's head under it (one level only)."""
    parts = path.split("/")
    return parts[-1] == HEADF and parts[0].endswith(".griplink") and (
        len(parts) == 2 or (len(parts) == 4 and parts[1] == RECS))


def _show(path: str) -> str:
    """How a field is named in a message: its member, and its record under the member."""
    parts = path.split("/")
    return parts[0][: -len(".griplink")] + (f"/{parts[2]}" if len(parts) == 4 else "")


def _fields(root: Path, tree: str | None) -> dict[str, str]:
    out = {}
    for path, (_, _, sha) in _ls_tree(root, tree, recursive=True).items():
        if _is_field(path):
            out[path] = _git(root, "cat-file", "blob", sha)
    return out


def _pending_file(root: Path) -> Path:
    return _git_dir(root) / "grip-pending.json"


def _pending(root: Path) -> dict[str, str]:
    p = _pending_file(root)
    return json.loads(p.read_text()) if p.exists() else {}


def _stook_fields(root: Path) -> dict[str, str]:
    n = _fields(root, _tree_of(root, STOOK))
    n.update(_pending(root))
    return n


def _follow(root: Path, v: str) -> str | None:
    return _cur(root, v[len(REFP):]) if v.startswith(REFP) else v


def _with_fields(root: Path, tree: str, fmap: dict[str, str | None]) -> str:
    """tree with each field set (or removed for None), in a scratch index so the workspace index is untouched"""
    idx = _git_dir(root) / "grip-scratch-index"
    env = dict(os.environ, GIT_INDEX_FILE=str(idx))
    _git(root, "read-tree", tree, env=env)
    for path, v in sorted(fmap.items()):
        if v is None:
            _git(root, "update-index", "--force-remove", path, env=env)
        else:
            _git(root, "update-index", "--add", "--cacheinfo", f"100644,{_blob(root, v + chr(10))},{path}", env=env)
    tree = _git(root, "write-tree", env=env)
    idx.unlink()
    return tree


def _set_field(root: Path, path: str, value: str) -> None:
    _git(root, "update-index", "--add", "--cacheinfo", f"100644,{_blob(root, value + chr(10))},{path}")
    _git(root, "checkout-index", "-f", "--", path)
    p = _pending(root)
    p[path] = value
    _pending_file(root).write_text(json.dumps(p, indent=1, sort_keys=True))


def _one_branch(root: Path) -> None:
    """Full mode carries one root branch for now: any root branch but main is refused by name."""
    branch = _try(root, "symbolic-ref", "--short", "-q", "HEAD")
    if branch != "main":
        raise GriplinkRefusal(f"{branch or 'a detached HEAD'}: {ONE_BRANCH}", 3)


def _cut(root: Path, msg: str, parents: list[str]) -> tuple[str, str]:
    """Write the STOOK (the names, its own chain) and the SHADOW (every branch followed to its sha, the shas as
    parents) from the index, which is staged against the shadow."""
    base = _git(root, "write-tree")
    stook_tree = _with_fields(root, base, dict(_stook_fields(root)))
    args = ["commit-tree", stook_tree]
    for p in dict.fromkeys(x for x in [_cur(root, STOOK)] if x):
        args += ["-p", p]
    stook = _git(root, *args, input=msg + "\n")
    resolved = {p: _follow(root, v) for p, v in _fields(root, stook_tree).items()}
    shadow_tree = _with_fields(root, stook_tree, resolved)
    got = [resolved[p] for p in sorted(resolved) if resolved[p]]
    args = ["commit-tree", shadow_tree]
    for p in dict.fromkeys(x for x in [*parents, *got] if x):
        args += ["-p", p]
    shadow = _git(root, *args, input=msg + "\n")
    _git(root, "update-ref", STOOK, stook)
    _git(root, "update-ref", MAIN, shadow)
    _git(root, "update-ref", SHADOW, shadow)
    _git(root, "reset", "-q", "--soft", shadow)
    _git(root, "read-tree", shadow)
    _git(root, "checkout-index", "-a", "-f")
    for p, v in resolved.items():  # a branch with nothing behind it yet has no field in the shadow
        if v is None and (root / p).exists():
            (root / p).unlink()
    if _pending_file(root).exists():
        _pending_file(root).unlink()
    return stook, shadow


# ---- the store ---------------------------------------------------------------------------------------------------
def init(root: Path) -> str:
    """A new full-mode root: main (the shadow) and an empty STOOK."""
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q", "-b", "main", ".")
    _git(root, "config", "fetch.recurseSubmodules", "false")
    (root / "README.md").write_text(f"# {root.name}\n")
    _git(root, "add", "README.md")
    _git(root, "commit", "-q", "--no-verify", "-m", "workspace")
    _git(root, "update-ref", STOOK, _git(root, "commit-tree", _git(root, "rev-parse", "HEAD^{tree}"), input="stook\n"))
    return _git(root, "rev-parse", "HEAD")


def add(root: Path, m: str, url: str) -> str:
    """Add a member at its remote's HEAD: the gitlink pin, the symlink, .gitmodules (ignore = dirty), and a head that
    follows the member's own STOOK. Returns the new shadow."""
    _one_branch(root)
    _check_name(m)
    _check_url(m, url)
    mrepo = member_dir(root, m)
    mrepo.parent.mkdir(parents=True, exist_ok=True)
    gitops.out(root, "clone", "-q", "--", url, str(mrepo))
    pin = _git(mrepo, "rev-parse", "HEAD")
    gm = str(root / ".gitmodules")
    _git(root, "config", "-f", gm, f"submodule.{m}.path", f"{m}.griplink/{PIN}")
    _git(root, "config", "-f", gm, f"submodule.{m}.url", url)
    _git(root, "config", "-f", gm, f"submodule.{m}.ignore", "dirty")
    link = root / m
    if not link.is_symlink() and not link.exists():
        link.symlink_to(f"{m}.griplink/{PIN}")
    _git(root, "add", ".gitmodules", m)
    _git(root, "update-index", "--add", "--cacheinfo", f"160000,{pin},{m}.griplink/{PIN}")
    _set_field(root, f"{m}.griplink/{HEADF}", REFP + member_ref(m))
    return _cut(root, f"add {m} at {pin[:7]}", [_git(root, "rev-parse", "HEAD")])[1]


def _staged_change(root: Path, m: str, pin: str) -> tuple[dict, dict]:
    mrepo = member_dir(root, m)
    raw = gitops.out_bytes(mrepo, "diff-index", "--cached", "-z", "--no-renames", pin).decode(errors="replace")
    toks, overlay, deleted, i = raw.split("\0"), {}, {}, 0
    while i + 1 < len(toks) and toks[i]:
        f = toks[i][1:].split(" ")
        if f[4][0] == "D":
            deleted[toks[i + 1]] = ("100644", "blob", EMPTY_BLOB)
        else:
            _copy_blob(mrepo, root, f[3])
            overlay[toks[i + 1]] = (f[1], "blob", f[3])
        i += 2
    if deleted:
        _blob(root, b"")
    return overlay, deleted


def _field_tree(root: Path, overlay: dict, deleted: dict) -> str:
    ents = [("100644", "blob", _blob(root, FIELD_SCHEMA), FT_SCHEMA), ("100644", "blob", _blob(root, "1"), FT_KIND),
            ("100644", "blob", _blob(root, "main"), FT_REF)]
    if overlay:
        ents.append(("040000", "tree", _build_tree(root, overlay), FT_OVERLAY))
    if deleted:
        ents.append(("040000", "tree", _build_tree(root, deleted), FT_DELETED))
    return _mktree(root, ents)


def _gripcommit_tree(root: Path, pin: str, overlay: dict, deleted: dict, msg: str) -> str:
    def split(ident: str) -> tuple[str, str]:
        parts = ident.rsplit(" ", 2)
        return parts[0], " ".join(parts[1:])

    (a_id, a_date), (c_id, c_date) = split(_git(root, "var", "GIT_AUTHOR_IDENT")), split(_git(root, "var", "GIT_COMMITTER_IDENT"))
    return _mktree(root, [
        ("040000", "tree", _field_tree(root, overlay, deleted), GC_TREE),
        ("040000", "tree", _mktree(root, [("160000", "commit", pin, "0000001")]), GC_PARENTS),
        ("100644", "blob", _blob(root, a_id), GC_AUTHOR), ("100644", "blob", _blob(root, c_id), GC_COMMITTER),
        ("100644", "blob", _blob(root, a_date), GC_ADATE), ("100644", "blob", _blob(root, c_date), GC_CDATE),
        ("100644", "blob", _blob(root, msg + "\n"), GC_MSG)])


def unstaged(root: Path) -> dict[str, list[str]]:
    """{member: tracked paths changed in its work tree but not staged}, as git status names them"""
    out = {}
    for m in _links(root, _git(root, "write-tree")):
        mrepo = member_dir(root, m)
        if (mrepo / ".git").exists():
            paths = [p for p in _git(mrepo, "diff", "--name-only").splitlines() if p]
            if paths:
                out[m] = paths
    return out


def commit(root: Path, message: str, stage_all: bool = False) -> dict:
    """Each member's STAGED change becomes the next GripCommit on its STOOK (the member's git untouched); then the
    STOOK and the SHADOW are cut together. `all` first stages each member's tracked changes, as git commit -a does.
    Nothing new refuses with git's own words and exit 1. Never pushes."""
    _one_branch(root)
    if stage_all:
        for m in _links(root, _git(root, "write-tree")):
            _git(member_dir(root, m), "add", "-u")
    moved = []
    for m, s in _links(root, _git(root, "write-tree")).items():
        pin = _ls_tree(root, s)[PIN][2]
        overlay, deleted = _staged_change(root, m, pin)
        if not overlay and not deleted:
            continue
        gt = _gripcommit_tree(root, pin, overlay, deleted, message)
        old = _cur(root, member_ref(m))
        if old and _git(root, "rev-parse", f"{old}:{GC_TREE}") == _ls_tree(root, gt)[GC_TREE][2]:
            continue
        _git(root, "update-ref", member_ref(m),
             _git(root, "commit-tree", gt, *(["-p", old] if old else []), input=message + "\n"))
        moved.append(m)
    if not moved and not _pending(root):
        now = {p: _follow(root, v) for p, v in _stook_fields(root).items()}
        then = _fields(root, _tree_of(root, "HEAD"))
        if all(now.get(p) == then.get(p) for p in set(now) | set(then)):
            un = unstaged(root)
            lines = [f"{m}: changes not staged for commit: {' '.join(ps)}" for m, ps in un.items()]
            lines.append('no changes added to commit (use "git add" inside a member, or "gr2 commit -a")' if un
                         else "nothing to commit, working tree clean")
            raise GriplinkRefusal("\n".join(lines), 1)
    stook, shadow = _cut(root, message, [_git(root, "rev-parse", "HEAD")])
    return {"moved": moved, "stook": stook, "shadow": shadow}


def status(root: Path) -> dict:
    """What a commit would cut, and what it would not take. {"moved": entries whose name now resolves to another sha
    than the shadow holds ("api", or "api/r1" for a record), "unstaged": {member: tracked paths not staged}}."""
    now = {p: _follow(root, v) for p, v in _stook_fields(root).items()}
    then = _fields(root, _tree_of(root, "HEAD"))
    moved = sorted(_show(p) for p in set(now) | set(then) if now.get(p) != then.get(p))
    return {"moved": moved, "unstaged": unstaged(root)}


def status_lines(found: dict) -> list[str]:
    """status as a reader sees it: "shadow = stook" or "shadow BEHIND the stook: <entries>", then each member's
    unstaged tracked changes, as git status names them."""
    lines = ["status: " + ("shadow BEHIND the stook: " + ", ".join(found["moved"]) if found["moved"] else
                           "shadow = stook")]
    lines += [f'{m}: changes not staged for commit: {" ".join(ps)} (use "git add" inside {m}, or "gr2 commit -a")'
              for m, ps in found["unstaged"].items()]
    return lines


def record(root: Path, m: str, rid: str, text: str = "", kind: str = "review") -> str:
    """Append to record <rid>'s chain under member <m>, like a commit: the record names the member's pin. A new entry
    follows the record's branch; a pinned one stays pinned (moving a branch never re-attaches a detached head).
    Returns the record's new tip."""
    _check_name(m)
    _check_name(rid)
    links = _links(root, _git(root, "write-tree"))
    if m not in links:
        raise GriplinkRefusal(f"{m}: not a member of this workspace; nothing was recorded", 3)
    for path in _stook_fields(root):  # the chain is __records__/<id>, so one id under two members would share it
        parts = path.split("/")
        if len(parts) == 4 and parts[1] == RECS and parts[2] == rid and parts[0] != f"{m}.griplink":
            raise GriplinkRefusal(f"{rid}: a record of {parts[0][: -len('.griplink')]}; one record id belongs to one "
                                  "member; nothing was recorded", 3)
    pin = _ls_tree(root, links[m])[PIN][2]
    tree = _mktree(root, [("100644", "blob", _blob(root, kind), REC_KIND), ("100644", "blob", _blob(root, rid), REC_ID),
                          ("100644", "blob", _blob(root, text), REC_TEXT), ("100644", "blob", _blob(root, pin), REC_COMMIT)])
    old = _cur(root, record_ref(rid))
    tip = _git(root, "commit-tree", tree, *(["-p", old] if old else []), input="record\n")
    _git(root, "update-ref", record_ref(rid), tip, *([old] if old else []))
    path = f"{m}.griplink/{RECS}/{rid}/{HEADF}"
    if path not in _stook_fields(root):
        _set_field(root, path, REFP + record_ref(rid))
    return tip


def _entry(root: Path, m: str, rid: str | None) -> tuple[str, str]:
    """The field path and the branch an entry follows: member <m>'s head, or record <rid>'s under it."""
    _check_name(m)
    if rid is not None:
        _check_name(rid)
    if m not in _links(root, _git(root, "write-tree")):
        raise GriplinkRefusal(f"{m}: not a member of this workspace; nothing was changed", 3)
    if rid is None:
        return f"{m}.griplink/{HEADF}", member_ref(m)
    path = f"{m}.griplink/{RECS}/{rid}/{HEADF}"
    if path not in _stook_fields(root):
        raise GriplinkRefusal(f"{m}/{rid}: no such record under {m}; nothing was changed", 3)
    return path, record_ref(rid)


def pin(root: Path, m: str, rid: str | None = None) -> str:
    """The entry's head becomes the sha its branch points at now (detached: it stops following). Returns the sha."""
    path, branch = _entry(root, m, rid)
    sha = _cur(root, branch)
    if not sha:
        raise GriplinkRefusal(f"{_show(path)}: its branch holds nothing yet, so there is no sha to pin; nothing was "
                              "changed", 3)
    _set_field(root, path, sha)
    return sha


def follow(root: Path, m: str, rid: str | None = None) -> str:
    """The entry's head follows its branch again. Returns the branch."""
    path, branch = _entry(root, m, rid)
    _set_field(root, path, REFP + branch)
    return branch


def _is_the_change(mrepo: Path, path: str, overlay: list[tuple[str, str, str]], deleted: list[str]) -> bool:
    """True when a member's uncommitted state at `path` is exactly what the workspace change applies there (staged and
    in the work tree), as after gr2 commit, which leaves the member's own git alone: checking out again loses nothing."""
    blobs = {p: b for p, _, b in overlay}
    staged = gitops.run(mrepo, "ls-files", "-s", "-z", "--", f":(literal){path}", binary=True).stdout
    staged = staged.decode("utf-8", "surrogateescape").split("\0")[0]
    if path in blobs:
        on_disk = gitops.run(mrepo, "hash-object", "--no-filters", "--", path)
        return (staged.split(" ")[1:2] == [blobs[path]] and on_disk.returncode == 0
                and on_disk.stdout.strip() == blobs[path])
    if path in deleted:
        return not staged and not os.path.lexists(mrepo / path)
    return False


def checkout(root: Path) -> dict[str, int]:
    """Each member at its pin, its workspace change applied and staged, not committed. Returns {member: paths}.

    Everything is read and checked before any member moves: each name, each url, each path and mode of each workspace
    change (the root's remote wrote them), and each member checkout that already exists, which must hold no
    uncommitted work. Then git writes every file: update-index takes the path first (git's own path check), then
    checkout-index writes it; a deletion goes through git rm. Python never writes inside a member."""
    gm = str(root / ".gitmodules")
    plan = []
    for m, s in _links(root, _git(root, "rev-parse", "HEAD^{tree}")).items():
        _check_name(m)
        url = _git(root, "config", "-f", gm, f"submodule.{m}.url")
        _check_url(m, url)
        e = _ls_tree(root, s)
        overlay: list[tuple[str, str, str]] = []
        deleted: list[str] = []
        head = _git(root, "cat-file", "blob", e[HEADF][2]) if HEADF in e else ""
        if head and not head.startswith(REFP):  # in a shadow the head is always a sha
            ft = _ls_tree(root, _git(root, "rev-parse", f"{head}:{GC_TREE}"))
            if FT_OVERLAY in ft:
                overlay = [(p, mode, b) for p, (mode, _, b) in _ls_tree(root, ft[FT_OVERLAY][2], recursive=True).items()]
            if FT_DELETED in ft:
                deleted = list(_ls_tree(root, ft[FT_DELETED][2], recursive=True))
        for p, mode, _ in overlay:
            _check_path(m, p)
            if mode not in _MODES:
                raise GriplinkRefusal(f"{m}: the workspace change gives {p!r} mode {mode}, which gr2 will not write; "
                                      "nothing was checked out", 3)
        for p in deleted:
            _check_path(m, p)
        mrepo = member_dir(root, m)
        if (mrepo / ".git").exists():
            raw = gitops.out_bytes(mrepo, "status", "--porcelain=v1", "-z", "--untracked-files=all")
            entries = [e for e in raw.decode("utf-8", "surrogateescape").split("\0") if len(e) > 3 and e[2] == " "]
            dirty = [e[3:] for e in entries if not e.startswith("??") and not _is_the_change(mrepo, e[3:], overlay, deleted)]
            if dirty:
                raise GriplinkRefusal(f"{m}: uncommitted work in {', '.join(dirty)}; nothing was checked out. next: "
                                      "commit it into the workspace with gr2 commit, or discard it yourself", 3)
            # An untracked file where the pin or the change puts a file (or needs a directory) would be overwritten,
            # which plain git checkout refuses too.
            listed = gitops.run(mrepo, "ls-tree", "-r", "-z", "--name-only", e[PIN][2], binary=True)
            if listed.returncode:
                raise GriplinkRefusal(f"{m}: its checkout does not hold the pin {e[PIN][2][:12]}; nothing was checked "
                                      "out. next: fetch in the member, then check out again", 3)
            # Where the member's filesystem folds case or Unicode normalization (git sets core.ignorecase and
            # core.precomposeunicode at clone), NEW.txt and new.txt, or the NFC and NFD spellings of café.txt, are one
            # file, so the paths are compared folded.
            def flag(k: str) -> bool:
                return gitops.run(mrepo, "config", "--bool", k).stdout.strip() == "true"
            ic, nf = flag("core.ignorecase"), flag("core.precomposeunicode")
            def fold(s: str) -> str:
                if nf or ic:
                    s = unicodedata.normalize("NFC", s)
                return s.casefold() if ic else s
            wanted = set(listed.stdout.decode("utf-8", "surrogateescape").split("\0")) - {""}
            wanted = {fold(w) for w in wanted | {p for p, _, _ in overlay}}
            hit = [u[3:] for u in entries if u.startswith("??") and (fold(u[3:]) in wanted or any(
                w.startswith(fold(u[3:]) + "/") or fold(u[3:]).startswith(w + "/") for w in wanted))]
            if hit:
                raise GriplinkRefusal(f"{m}: untracked files would be overwritten: {', '.join(hit)}; nothing was "
                                      "checked out. next: move them aside, or add them in the member and commit "
                                      "them into the workspace with gr2 commit", 3)
        plan.append((m, url, e[PIN][2], overlay, deleted))
    applied = {}
    for m, url, pin, overlay, deleted in plan:
        mrepo = member_dir(root, m)
        if not (mrepo / ".git").exists():
            mrepo.parent.mkdir(parents=True, exist_ok=True)
            gitops.out(root, "clone", "-q", "--no-checkout", "--", url, str(mrepo))
        link = root / m
        if not link.is_symlink() and not link.exists():
            link.symlink_to(f"{m}.griplink/{PIN}")
        _git(mrepo, "checkout", "-q", "--detach", "-f", pin)
        for p, mode, b in overlay:
            _copy_blob(root, mrepo, b)
            for args in (("update-index", "--add", "--cacheinfo", f"{mode},{b},{p}"), ("checkout-index", "-f", "--", p)):
                done = gitops.run(mrepo, *args)
                if done.returncode:
                    raise GriplinkRefusal(f"{m}: the path {p!r} was refused: {(done.stderr or '').strip()}", 3)
        for p in deleted:
            done = gitops.run(mrepo, "rm", "-q", "-f", "--ignore-unmatch", "--", f":(literal){p}")
            if done.returncode:
                raise GriplinkRefusal(f"{m}: the path {p!r} was refused: {(done.stderr or '').strip()}", 3)
        applied[m] = len(overlay) + len(deleted)
    return applied


def size_sentence(found: tuple[str, str, tuple[str, ...]]) -> str:
    from .grip_cli import host_size_line
    return host_size_line(found[0], ", ".join(found[2]) or None, found[1], found[3])


def push(root: Path, remote: str = "origin", allow_rewind: bool = False) -> dict:
    """One atomic push of main, SHADOW, STOOK and every member and record ref that is ahead; never forced. Refused, with nothing
    pushed: (1) "fetch first" when the remote's shadow is not an ancestor of ours after a fresh fetch; (2) an entry
    that would move back from what the remote's shadow recorded, by entry (--allow-rewind is the recorded
    exception). A remote whose shadow cannot be read (ls-remote or the fetch fails) refuses at exit 5. When git
    refuses the push, its own words are printed, after the size sentence for a size refusal."""
    _one_branch(root)
    theirs = None
    listed = gitops.run(root, "ls-remote", remote, MAIN)
    if listed.returncode:
        raise GriplinkRefusal(f"cannot read the remote shadow from {remote}: {(listed.stderr or '').strip()}; nothing "
                              "was pushed", 5)
    if listed.stdout.strip():  # absent: the first push
        fetched = gitops.run(root, "fetch", "-q", "--no-recurse-submodules", remote,
                             f"+{MAIN}:refs/remotes/{remote}/main")
        if fetched.returncode:
            raise GriplinkRefusal(f"cannot read the remote shadow from {remote}: {(fetched.stderr or '').strip()}; "
                                  "nothing was pushed", 5)
        theirs = _cur(root, f"refs/remotes/{remote}/main")
    mine = _git(root, "rev-parse", "HEAD")
    if theirs:
        ahead = _anc(root, theirs, mine)
        if ahead is None:
            raise GriplinkRefusal(f"cannot tell whether the remote shadow {theirs[:12]} is an ancestor of "
                                  f"{mine[:12]}; nothing was pushed", 5)
        if not ahead:
            raise GriplinkRefusal(
                f"fetch first: the remote shadow moved (remote main is {theirs[:12]}, not an ancestor of "
                f"{mine[:12]}); nothing was pushed", 3)
    rewound: list[tuple[str, str, str]] = []
    if theirs:
        old, new = _fields(root, _tree_of(root, theirs)), _fields(root, _tree_of(root, "HEAD"))
        for path, sha in sorted(new.items()):
            if path not in old or old[path] == sha:
                continue
            forward = _anc(root, old[path], sha)
            if forward is None:
                raise GriplinkRefusal(f"{_show(path)}: cannot tell whether {sha[:12]} descends from {old[path][:12]}, "
                                      "which the remote shadow recorded; nothing was pushed", 5)
            if not forward and allow_rewind:
                rewound.append((_show(path), old[path], sha))
            elif not forward:
                raise GriplinkRefusal(
                    f"{_show(path)}: {sha[:12]} does not descend from {old[path][:12]}, which the remote shadow "
                    "recorded (a rewind; --allow-rewind records one); nothing was pushed", 3)
    listed = gitops.run(root, "ls-remote", remote, NS + "/*")
    rem = dict(reversed(line.split("\t")) for line in (listed.stdout or "").splitlines() if "\t" in line)
    subs = [r for prefix in ("/__members__/", "/__records__/") for r in gitops.for_each_ref_rows(
        _git(root, "for-each-ref", "--format=%(refname)", NS + prefix))]
    send = []
    for r in subs:
        here, there = _git(root, "rev-parse", r), rem.get(r)
        if there == here or (there and _anc(root, here, there) is True):
            continue
        send.append(r)
    specs = [f"{MAIN}:{MAIN}", f"{SHADOW}:{SHADOW}", f"{STOOK}:{STOOK}"] + \
        [f"{'+' if allow_rewind else ''}{r}:{r}" for r in send]
    if rewound:
        # The recorded exception (10-12 design sec 5, today's push lease 2): one commit at rewound/<tip>, parent and
        # tree the tip's, its message naming each member and both shas; it rides the same atomic push.
        lines = "".join(f"{m}: rewind: {o} -> {n}\n" for m, o, n in rewound)
        record = _git(root, "commit-tree", f"{mine}^{{tree}}", "-p", mine,
                      input=f"push: rewound under --allow-rewind at {mine}\n\n{lines}")
        _git(root, "update-ref", rewound_ref(mine), record)
        specs.append(f"{rewound_ref(mine)}:{rewound_ref(mine)}")
    proc = gitops.run(root, "push", "--atomic", "--porcelain", remote, *specs)
    if proc.returncode:
        said = (proc.stderr or proc.stdout or "").strip()
        from .grip_cli import host_size_refusal  # the parser lives with today's push; step 1 is its first caller
        # only member refs go to the size parser: a record is not a member, and a wrong member is worse than none
        found = host_size_refusal(proc.stderr or "", [(r.split("/")[-2], root, _git(root, "rev-parse", r))
                                                      for r in send if r.startswith(NS + "/__members__/")])
        raise GriplinkRefusal((size_sentence(found) + "\n" if found else "") + said, 5)
    _git(root, "update-ref", f"refs/remotes/{remote}/main", _git(root, "rev-parse", MAIN))
    return {"shadow": _git(root, "rev-parse", MAIN),
            "members": [r.split("/")[-2] for r in send if r.startswith(NS + "/__members__/")],
            "records": [r.split("/")[-1] for r in send if r.startswith(NS + "/__records__/")]}
