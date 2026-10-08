"""Members of the workspace that a review lane does not bind, brought into the lane at their PIN.

A review lane binds the repositories that changed. "Tested together, before merge" also needs the members that
depend on them, and the members those need installed, each at the commit the workspace records for it: never at
whatever its remote's default branch points at today. The pin is the ``pin`` field of the member's entry in the
workspace spec, read through ``pin_of`` (the one accessor, so a later move of pins into state refs changes one
function). Everything here refuses rather than fall through to the remote tip: a green over a tip the workspace
never declared is exactly the claim this exists to prevent.
"""
from __future__ import annotations

import re
import subprocess
import tempfile
from pathlib import Path
from typing import NamedTuple

from . import gitops

_SHA40 = re.compile(r"^[0-9a-f]{40}$")


class PinRefused(Exception):
    """An unchanged member cannot be put at its pin. ``reason`` is the one sentence; the member is named."""

    def __init__(self, member: str, reason: str) -> None:
        super().__init__(f"member {member!r}: {reason}")
        self.member, self.reason = member, reason


def pin_of(repo_spec: dict) -> str | None:
    """The commit the workspace records for this member, or None when there is no usable one (absent, empty, or
    not a full lowercase 40-hex sha, which is also what a branch name or a short sha looks like)."""
    pin = repo_spec.get("pin")
    if isinstance(pin, str) and _SHA40.match(pin):
        return pin
    return None


class PinConflict(Exception):
    """The workspace spec and grip.toml record different pins for one member. Neither wins silently."""

    def __init__(self, member: str, spec_pin: str, root_pin: str) -> None:
        super().__init__(
            f"member {member!r}: .grip/workspace_spec.toml records pin {spec_pin[:12]} and grip.toml records "
            f"{root_pin[:12]}; they must agree, so fix one of them"
        )
        self.member, self.spec_pin, self.root_pin = member, spec_pin, root_pin


class MembersUnreadable(Exception):
    """A file that records the members' pins cannot be read. Names the file; a lane never guesses around it."""

    def __init__(self, path: Path, reason: str) -> None:
        super().__init__(f"cannot read {path}: {reason}")
        self.path, self.reason = path, reason


def _read_toml(path: Path) -> dict:
    import tomllib

    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, OSError, UnicodeDecodeError) as exc:
        raise MembersUnreadable(path, f"{type(exc).__name__}: {exc}") from exc


def load_members(workspace_root: Path) -> list[dict] | None:
    """Every member the workspace declares, as plain ``{name, url, pin, path, ref}`` dicts, or None when the root carries
    neither source. Two sources exist: the workspace spec's ``[[repos]]`` (name, url, pin) and the root's
    ``grip.toml`` ``[[members]]`` (name, pin, and the url under ``[members.remotes] origin``). A root made by
    ``store init`` alone has only the second. Both are read into the SAME shape and every pin is judged by
    ``pin_of``, so no source can make a non-commit count as a pin.

    Both present: members are matched by name. Two usable pins that differ refuse (``PinConflict``); a usable pin
    in one file and none in the other is used; a member only in grip.toml is added. Path, ref and url use the
    spec value when present, otherwise the root value. Defaults are left to the caller. Nothing here guesses."""
    from .spec_apply import workspace_spec_path

    spec_path = workspace_spec_path(workspace_root)
    root_path = workspace_root / "grip.toml"
    spec_members: list[dict] | None = None
    root_members: list[dict] | None = None
    if spec_path.is_file():
        doc = _read_toml(spec_path)
        spec_members = [
            {"name": r.get("name"), "url": r.get("url"), "pin": r.get("pin"),
             "path": r.get("path"), "ref": r.get("ref") or r.get("default_branch")}
            for r in doc.get("repos", []) if isinstance(r, dict) and r.get("name")
        ]
    if root_path.is_file():
        doc = _read_toml(root_path)
        root_members = []
        for m in doc.get("members", []):
            if not (isinstance(m, dict) and m.get("name")):
                continue
            remotes = m.get("remotes") if isinstance(m.get("remotes"), dict) else {}
            root_members.append({"name": m["name"], "url": remotes.get("origin"), "pin": m.get("pin"),
                                 "path": m.get("path"), "ref": m.get("ref")})
    if spec_members is None and root_members is None:
        return None
    if spec_members is None:
        return root_members
    if root_members is None:
        return spec_members
    by_name = {m["name"]: m for m in root_members}
    merged: list[dict] = []
    for m in spec_members:
        other = by_name.pop(m["name"], None)
        if other is not None:
            mine, theirs = pin_of(m), pin_of(other)
            if mine and theirs and mine != theirs:
                raise PinConflict(str(m["name"]), mine, theirs)
            m = {**m, "pin": mine or theirs, "url": m.get("url") or other.get("url"),
                 "path": m.get("path") or other.get("path"), "ref": m.get("ref") or other.get("ref")}
        merged.append(m)
    return merged + list(by_name.values())


def unchanged_members(spec: dict, lane_keys: list[str]) -> list[dict]:
    """The workspace members the lane does not bind, in spec order. A lane key names a member by its spec name."""
    bound = set(lane_keys)
    return [r for r in spec.get("repos", []) if isinstance(r, dict) and r.get("name") and r["name"] not in bound]


def materialize_at_pin(repo_spec: dict, dest: Path, *, workspace_root: Path, source: str | None = None) -> str:
    """Clone one unchanged member into ``dest`` at its pin and return the pin. ONE path for every way this can
    fail: no usable pin and a pin the clone cannot reach both refuse as ``PinRefused``, and a clone that did not
    land on the pin is refused too, because ``clone_and_pin`` answers "was this the first materialization", not
    "is the member at the pin" (an existing clone is reported as done without being looked at).

    An empty pin is refused HERE, before any clone: ``clone_and_pin`` treats an empty pin as "stay on the
    default branch", which is the fall-through this refuses."""
    from .clone_exec import CloneExecutionError, clone_and_pin
    from .spec_apply import repo_cache_path

    name = str(repo_spec["name"])
    pin = pin_of(repo_spec)
    if pin is None:
        raise PinRefused(name, "the workspace records no usable pin for it (a full 40-hex commit is required)")
    url = repo_spec.get("url")
    if not isinstance(url, str) or not url:
        raise PinRefused(name, f"the workspace records a pin {pin[:12]} but no url to fetch it from")
    try:
        clone_and_pin(
            source or url,
            dest,
            pin=pin,
            member=name,
            reference_repo_root=repo_cache_path(workspace_root, name),
        )
    except (SystemExit, CloneExecutionError, OSError) as exc:  # checkout_declared_pin refuses with SystemExit
        raise PinRefused(name, f"its pin {pin[:12]} could not be materialized ({exc})") from exc
    head = gitops.git(dest, "rev-parse", "--verify", "HEAD")
    landed = head.stdout.strip() if head.returncode == 0 else ""
    if landed != pin:
        raise PinRefused(name, f"it is at {landed[:12] or 'no commit'} in the lane, not at its pin {pin[:12]}")
    return pin


#: A root-level file bigger than this is not copied into a probe: a probe exists so the ecosystem plugins can read
#: manifests, and a manifest is small. (A lockfile or a data file at the root is not what a plugin describes.)
_PROBE_FILE_CAP = 2 * 1024 * 1024


class Probe(NamedTuple):
    pin: str
    source: str  # "local checkout" | "cache" | "url"
    location: str  # the path or url the pin was found at: the full checkout, if one follows, reads from it too
    filtered: bool  # False when the source ignored the blobless filter (every local source does) and the fetch was depth-1


def pin_sources(repo_spec: dict, workspace_root: Path) -> list[tuple[str, str]]:
    """Where a member's pin may be found, cheapest first: the workspace's own checkout of the member (no network,
    and the only place a commit nobody has pushed lives), the repo cache, then the member's url. The checkout is
    looked for at ``<workspace>/<name>``; a member kept elsewhere falls through to the cache and the url."""
    from .spec_apply import repo_cache_path

    name = str(repo_spec["name"])
    found: list[tuple[str, str]] = []
    local = workspace_root / name
    if (local / ".git").exists():
        found.append(("local checkout", str(local)))
    cache = repo_cache_path(workspace_root, name)
    if cache.exists():
        found.append(("cache", str(cache)))
    url = repo_spec.get("url")
    if isinstance(url, str) and url:
        found.append(("url", url))
    return found


#: A local source does not offer a blobless fetch unless told to (measured: without it a depth-1 fetch of the pin
#: of a 1.1 GB repository took 16 s and wrote 836 MB, with it 0.1 s and 244 KB). Passed as the probe repository's
#: own ``remote.origin.uploadpack`` so the later blob reads use it too; a network remote decides for itself.
_LOCAL_UPLOAD_PACK = "git -c uploadpack.allowFilter=true -c uploadpack.allowAnySHA1InWant=true upload-pack"


def _is_local(location: str) -> bool:
    return location.startswith("file://") or Path(location).exists()


def _git_in(cwd: Path, *args: str, timeout: int = 600) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, timeout=timeout)


def probe_at_pin(repo_spec: dict, dest: Path, *, workspace_root: Path) -> Probe:
    """Put the ROOT-LEVEL FILES of a member's pinned commit into ``dest`` and nothing else, so the planner can read
    manifests without a clone. ``dest`` holds plain files: not a repository, no history, no tracked tree to check.
    The commit is fetched (depth 1, asking for no blobs when the source honours it) into a throwaway repository
    that is removed here, and the fetched commit must BE the pin before a byte is copied. The same refusals as a
    full clone: no usable pin or no url, or a pin none of the sources has."""
    name = str(repo_spec["name"])
    pin = pin_of(repo_spec)
    if pin is None:
        raise PinRefused(name, "the workspace records no usable pin for it (a full 40-hex commit is required)")
    url = repo_spec.get("url")
    if not isinstance(url, str) or not url:
        raise PinRefused(name, f"the workspace records a pin {pin[:12]} but no url to fetch it from")
    tried: list[str] = []
    for label, location in pin_sources(repo_spec, workspace_root):
        with tempfile.TemporaryDirectory(prefix="grip-probe-") as tmp:
            repo = Path(tmp)
            if _git_in(repo, "init", "-q").returncode != 0 or _git_in(repo, "remote", "add", "origin", location).returncode != 0:
                tried.append(f"{label}: could not start a probe repository")
                continue
            if _is_local(location):
                _git_in(repo, "config", "remote.origin.uploadpack", _LOCAL_UPLOAD_PACK)
            try:
                fetched = _git_in(repo, "fetch", "-q", "--depth", "1", "--filter=blob:none", "origin", pin)
            except (subprocess.TimeoutExpired, OSError) as exc:
                tried.append(f"{label}: fetch did not finish ({type(exc).__name__})")
                continue
            if fetched.returncode != 0:
                tried.append(f"{label}: {(fetched.stderr.decode(errors='replace').strip().splitlines() or ['fetch failed'])[-1]}")
                continue
            filtered = b"filtering not recognized" not in fetched.stderr
            head = _git_in(repo, "rev-parse", "--verify", "-q", "FETCH_HEAD^{commit}")
            if head.returncode != 0 or head.stdout.decode().strip() != pin:
                tried.append(f"{label}: fetched {head.stdout.decode().strip()[:12] or 'nothing'}, not the pin")
                continue
            listing = _git_in(repo, "ls-tree", "-z", "FETCH_HEAD")
            if listing.returncode != 0:
                tried.append(f"{label}: the pin's tree could not be listed")
                continue
            dest.mkdir(parents=True, exist_ok=True)
            for entry in listing.stdout.split(b"\0"):
                if not entry:
                    continue
                meta, _, raw = entry.partition(b"\t")
                mode, kind, obj = meta.decode().split()[:3]
                if kind != "blob" or mode not in ("100644", "100755"):
                    continue
                body = _git_in(repo, "cat-file", "blob", obj)
                if body.returncode != 0 or len(body.stdout) > _PROBE_FILE_CAP:
                    continue
                (dest / raw.decode()).write_bytes(body.stdout)
            return Probe(pin, label, location, filtered)
    raise PinRefused(name, f"its pin {pin[:12]} was not found in any source ({'; '.join(tried) or 'none to try'})")


def tree_at(dest: Path, pin: str) -> str:
    """The tree id of the pinned commit: what a pinned member's tracked content must equal for the whole run."""
    out = gitops.git(dest, "rev-parse", "--verify", f"{pin}^{{tree}}")
    if out.returncode != 0 or not out.stdout.strip():
        raise PinRefused(dest.name, f"its pin {pin[:12]} has no tree in the lane clone")
    return out.stdout.strip()
