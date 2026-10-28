"""A store root's own composed files: `[[compose]]` tables in grip.toml, each part a "<member>:<path>".

gr1's agent-codelab built a root AGENTS.md from several repos; a member's [[files.compose]] cannot, since its
parts are confined to that member. A root compose reads one part from each named member's checkout and writes
one dest at the root. It runs at the end of `gr2 checkout` on a store root. The dest is derived, so it is never
tracked by the root: the store root ignores by default (its .gitignore is compiled from .gitinclude), and a
dest the root would track refuses.

Everything is read and checked before the dest is touched, so a refusal writes nothing.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from . import gitops, layout

GITIGNORE = ".gitignore"
#: The root's own control files: a compose dest is never one of them (it would replace the manifest or the ignore rules).
ROOT_CONTROL = (layout.LAYOUT[0].spelling, GITIGNORE, ".gitinclude", ".gitmodules")
#: Under the root's git dir: {dest: sha256 of the bytes gr2 last wrote there}. A dest that exists is rewritten only
#: when it still holds those bytes, so a file gr2 did not write (or one edited since) is never overwritten.
RECORD = "gr2-root-compose.json"


class RootComposeRefusal(RuntimeError):
    def __init__(self, message: str, code: int) -> None:
        super().__init__(message)
        self.code = code


def _declarations(document: dict) -> list[dict]:
    items = document.get("compose", [])
    if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
        raise RootComposeRefusal("grip.toml compose must be [[compose]] tables; nothing was composed", 4)
    return items


def _on_disk(path: Path) -> Path:
    """``path`` with each existing component spelled as the filesystem holds it. On a case-insensitive filesystem
    "readme.md" names the README.md that is there, so every check below sees the file a write would replace; on a
    case-sensitive one a component that does not exist as typed is a different, absent file and is left as typed."""
    parts = path.parts
    out = Path(parts[0])
    for i, part in enumerate(parts[1:], 1):
        nxt = out / part
        if os.path.lexists(nxt) and out.is_dir():
            try:
                names = os.listdir(out)
            except OSError:
                return Path(out, *parts[i:])
            if part not in names:
                same = [n for n in names if n.casefold() == part.casefold() and os.path.samefile(out / n, nxt)]
                if same:
                    nxt = out / same[0]
        out = nxt
    return out


def _inside(path: Path, top: Path) -> bool:
    return path == top or path.is_relative_to(top)


def apply(root: Path, document: dict, members: list[dict]) -> list[str]:
    """Write every root compose; return one line per dest written. ``members`` are the materialized rows."""
    lines: list[str] = []
    for dest_text, dest, body, count in plan(root, document, members):
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(f".{dest.name}.gr2-compose.tmp")
        # Bytes, not text: text mode writes each "\n" as os.linesep, so on Windows the file would never hold the
        # bytes recorded below and the next checkout would read it as hand-edited.
        data = body.encode("utf-8")
        tmp.write_bytes(data)
        os.replace(tmp, dest)
        _write_record(root, dest.relative_to(root).as_posix(), _sha(data))
        lines.append(f"composed {dest_text} from {count} part(s)")
    return lines


def drift(root: Path, document: dict, members: list[dict]) -> list[str]:
    """For `gr2 status`: one line per root compose whose dest is missing or differs from its parts now. Reads only."""
    try:
        plans = plan(root, document, members)
    except RootComposeRefusal as exc:
        return [f"compose: cannot compose ({exc})"]
    lines = []
    for dest_text, dest, body, _count in plans:
        if not dest.is_file():
            lines.append(f"compose: {dest_text} is missing; gr2 checkout writes it")
        elif dest.read_bytes() != body.encode("utf-8"):
            lines.append(f"compose: {dest_text} is stale (its parts changed); gr2 checkout rewrites it")
    return lines


def plan(root: Path, document: dict, members: list[dict]) -> list[tuple[str, Path, str, int]]:
    """Read and check every root compose; return (dest text, dest, body, part count) for each. Writes nothing."""
    items = _declarations(document)
    if not items:
        return []
    root = _on_disk(root.resolve())
    checkouts = {str(m["name"]): _on_disk(Path(m["path"]).resolve()) for m in members}
    written = _read_record(root)
    seen: set[str] = set()
    plans: list[tuple[str, Path, str, int]] = []
    for item in items:
        dest_text = item.get("dest")
        parts = item.get("parts")
        separator = item.get("separator", "\n\n")
        if not isinstance(dest_text, str) or not dest_text.strip():
            raise RootComposeRefusal("a [[compose]] table needs a dest (a path at the root); nothing was composed", 4)
        if not isinstance(parts, list) or not parts or not all(isinstance(p, str) for p in parts):
            raise RootComposeRefusal(
                f'compose {dest_text}: parts must be a list of "<member>:<path>" strings; nothing was composed', 4)
        if not isinstance(separator, str):
            raise RootComposeRefusal(f"compose {dest_text}: separator must be a string; nothing was composed", 4)
        dest = _on_disk((root / dest_text).resolve())
        if not _inside(dest, root) or dest == root or _inside(dest, root / ".git") or _inside(dest, layout.grip_dir(root)):
            raise RootComposeRefusal(
                f"compose {dest_text}: the dest must be a file at the root, outside .git and .grip; "
                "nothing was composed", 4)
        rel_dest = dest.relative_to(root).as_posix()
        if rel_dest.casefold() in {c.casefold() for c in ROOT_CONTROL}:
            raise RootComposeRefusal(
                f"compose {dest_text}: the dest is the root's own {rel_dest}, which the store owns; "
                "nothing was composed", 4)
        if gitops.run(root, "--literal-pathspecs", "ls-files", "--error-unmatch", "--", rel_dest).returncode == 0:
            raise RootComposeRefusal(
                f"compose {dest_text}: the root tracks {rel_dest}, so a composed file there would enter a root "
                "commit; nothing was composed", 4)
        for name, checkout in checkouts.items():
            if _inside(dest, checkout):
                raise RootComposeRefusal(
                    f"compose {dest_text}: the dest is inside member {name}; a member's own files are composed by "
                    "its [[files.compose]]; nothing was composed", 4)
        # The ignore rules alone, on the literal name: check-ignore refuses the literal pathspec magic, and without
        # --no-index it glob-matches the name against tracked files ("R*.md" reads as not ignored beside a tracked
        # README.md). A tracked dest already refused above.
        ignored = gitops.run(root, "check-ignore", "-q", "--no-index", "--", rel_dest).returncode
        if ignored != 0:
            why = "by its .gitinclude" if ignored == 1 else "(git check-ignore could not say it is ignored)"
            raise RootComposeRefusal(
                f"compose {dest_text}: the root tracks {rel_dest} {why}; nothing was composed", 4)
        if rel_dest.casefold() in seen:
            raise RootComposeRefusal(
                f"compose {dest_text}: two [[compose]] tables name {rel_dest}; nothing was composed", 4)
        seen.add(rel_dest.casefold())
        for ancestor in dest.parents:
            if ancestor == root:
                break
            if os.path.lexists(ancestor) and not ancestor.is_dir():
                raise RootComposeRefusal(
                    f"compose {dest_text}: {ancestor.relative_to(root).as_posix()} exists and is not a directory; "
                    "nothing was composed", 4)
        if os.path.islink(root / dest_text) or (os.path.lexists(dest) and not dest.is_file()):
            raise RootComposeRefusal(
                f"compose {dest_text}: {dest_text} exists and is not a regular file; move or remove it, then rerun "
                "gr2 checkout; nothing was composed", 4)
        if os.path.lexists(dest):
            recorded = written.get(rel_dest)
            if recorded is None:
                raise RootComposeRefusal(
                    f"compose {dest_text}: {dest_text} already exists and gr2 did not compose it; move or remove "
                    "it, then rerun gr2 checkout; nothing was composed", 4)
            if _sha(dest.read_bytes()) != recorded:
                raise RootComposeRefusal(
                    f"compose {dest_text}: {dest_text} was edited since gr2 composed it; move or remove it, then "
                    "rerun gr2 checkout; nothing was composed", 4)
        texts: list[str] = []
        for part in parts:
            member, sep, rel = part.partition(":")
            if not sep or not member or not rel:
                raise RootComposeRefusal(
                    f'compose {dest_text}: part {part!r} is not "<member>:<path>"; nothing was composed', 4)
            if member not in checkouts:
                raise RootComposeRefusal(
                    f"compose {dest_text}: part {part!r} names {member}, which is not a checked-out member of this "
                    "root; nothing was composed", 4)
            source = _on_disk((checkouts[member] / rel).resolve())
            if not _inside(source, checkouts[member]):
                raise RootComposeRefusal(
                    f"compose {dest_text}: part {part!r} resolves outside member {member}'s own tree; "
                    "nothing was composed", 4)
            if not source.is_file():
                raise RootComposeRefusal(
                    f"compose {dest_text}: part {part!r} does not exist in member {member}; nothing was composed", 3)
            try:
                texts.append(source.read_text(encoding="utf-8"))
            except UnicodeDecodeError:
                raise RootComposeRefusal(
                    f"compose {dest_text}: part {part!r} is not valid UTF-8 text; nothing was composed", 4) from None
        plans.append((dest_text, dest, separator.join(texts), len(texts)))
    return plans


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _record_path(root: Path) -> Path:
    result = gitops.run(root, "rev-parse", "--absolute-git-dir")
    if result.returncode != 0:
        raise RootComposeRefusal("the root is not a git repository, so gr2 cannot tell which files it composed; "
                                 "nothing was composed", 4)
    return Path(result.stdout.strip()) / RECORD


def _read_record(root: Path) -> dict[str, str]:
    """What gr2 wrote. An unreadable or malformed record reads as empty, so every existing dest refuses."""
    try:
        data = json.loads(_record_path(root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, str)}


def _write_record(root: Path, rel_dest: str, sha: str) -> None:
    path = _record_path(root)
    data = _read_record(root)
    data[rel_dest] = sha
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(data, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)
