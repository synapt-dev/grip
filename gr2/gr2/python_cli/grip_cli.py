"""CLI commands for grip object model and config overlay.

Separate module so tests can import without pulling in all of app.py's
dependencies (gr2.prototypes, lane_workspace_prototype, etc.).
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tomllib
from pathlib import Path, PurePosixPath
from typing import Optional

import typer

from . import config as config_mod
from . import gitops
from . import grip as grip_mod
from .gitops import git, repo_dirty
from .layout import MOVED_MARKER, grip_dir
from .root_option import ROOT_OPTION, RootOptionalCommand
from .spec_apply import unit_member_path, validate_grip_toml
from .workspace_guidance import missing_gr2_workspace_guidance

grip_app = typer.Typer(
    help="Grip object model: workspace snapshots and history. Exit codes: 0 ok, "
    "2 usage, 3 refused on coverage or cleanliness, 4 refused as inconsistent or "
    "beta, 5 cannot measure (a verb that could not complete also reports 5 and "
    'prefixes its message with "cannot complete this store verb:")'
)
config_cli_app = typer.Typer(help="Config base+overlay management")


def _resolve_repos(workspace: Path, repos_csv: str) -> dict[str, Path]:
    result: dict[str, Path] = {}
    for name in repos_csv.split(","):
        name = name.strip()
        if not name:
            continue
        result[name] = workspace / name
    return result


def _discover_repos(workspace: Path) -> dict[str, Path]:
    """Auto-discover repos from workspace_spec.toml."""
    spec_path = workspace / ".grip" / "workspace_spec.toml"
    if not spec_path.exists():
        typer.echo(
            f"No .grip/workspace_spec.toml at {workspace}. Use --repos or create a workspace spec."
        )
        raise typer.Exit(code=1)
    with spec_path.open("rb") as fh:
        spec = tomllib.load(fh)
    result: dict[str, Path] = {}
    for repo in spec.get("repos", []):
        name = repo.get("name", "")
        path = repo.get("path", name)
        if name:
            result[name] = workspace / path
    return result


def _member_working_root(workspace_root: Path, name: str, declared_path: Path) -> Path:
    """The path a store verb reads and writes for one member.

    The state helper answers what the declared path holds, because asking the
    path directly let git answer for the ENCLOSING root: on a workspace
    adopted from a superproject the declared path is the EMPTY placeholder,
    git answered every question there for the ENCLOSING root, `store snapshot`
    recorded the ROOT's HEAD as every member's head (rc 0), and the dirty
    check refused with the ROOT's untracked files as the members' dirt. A
    missing member keeps the old shape (skipped by the dirty check, recorded
    empty) -- a missing member is nothing to read, not a conflict. A present
    path that is a repo root is used as-is; an empty placeholder falls to the
    unit's materialized copy of that member, which is the clone `workspace
    materialize` placed at its pin; a present path that is neither refuses
    with the verb that fixes it.
    """
    if not declared_path.exists():
        return declared_path
    state = gitops.repo_path_state(declared_path)
    if state == "repo_root":
        return declared_path
    if state == "empty_placeholder":
        try:
            spec = tomllib.loads((workspace_root / ".grip" / "workspace_spec.toml").read_text())
        except (OSError, tomllib.TOMLDecodeError):
            raise SystemExit(
                f"run gr2 workspace materialize first: {declared_path} is an empty placeholder "
                "and the workspace spec cannot be read to find its materialized copy"
            )
        for unit in spec.get("units", []):
            if name not in [str(item) for item in unit.get("repos", [])]:
                continue
            candidate = unit_member_path(workspace_root, spec, unit, name)
            if gitops.repo_path_state(candidate) == "repo_root":
                return candidate
        raise SystemExit(
            f"run gr2 workspace materialize first: {name} is not materialized in any unit "
            f"(the declared path {declared_path} is an empty placeholder)"
        )
    raise SystemExit(f"run gr2 workspace materialize first: {declared_path} is not a repository")


def _member_map(workspace_root: Path, repos_csv: str) -> dict[str, Path]:
    """The store verbs' member map, resolved to working roots.

    The declared paths are what the spec says; what a store verb READS is the
    working root of each member, which on an adopted superproject is the
    unit's materialized copy, not the placeholder at the root.
    """
    if repos_csv:
        declared = _resolve_repos(workspace_root, repos_csv)
    else:
        declared = _discover_repos(workspace_root)
    return {name: _member_working_root(workspace_root, name, path) for name, path in declared.items()}


def _validate_grip_dir(workspace: Path) -> None:
    """Check .grip/ directory exists."""
    grip_dir = workspace / ".grip"
    if not grip_dir.exists():
        typer.echo(
            f"No .grip/ directory at {workspace}. "
            f"{missing_gr2_workspace_guidance(workspace, 'Run `gr2 workspace init .` first.')}"
        )
        raise typer.Exit(code=1)


def _check_dirty_repos(repos: dict[str, Path]) -> list[str]:
    """Return list of dirty repo names."""
    dirty = []
    for name, path in sorted(repos.items()):
        if path.is_dir() and repo_dirty(path):
            dirty.append(name)
    return dirty


def _repo_head_state(repo_path: Path) -> dict[str, object]:
    """Get head state for a single repo."""
    if not repo_path.exists():
        # a missing path is nothing to read, not a conflict: record it empty
        # without invoking git, which would raise before the rc check below
        # could answer (the declared path can hold no placeholder and no repo)
        return {"head": None, "is_empty": True, "head_state": "empty"}
    head_proc = git(repo_path, "rev-parse", "HEAD")
    if head_proc.returncode != 0:
        return {"head": None, "is_empty": True, "head_state": "empty"}

    head_sha = head_proc.stdout.strip()
    branch = git(repo_path, "branch", "--show-current")
    branch_name = branch.stdout.strip() if branch.returncode == 0 else ""

    if branch_name:
        return {
            "head": head_sha,
            "is_empty": False,
            "head_state": "attached",
            "branch": branch_name,
        }
    return {"head": head_sha, "is_empty": False, "head_state": "detached"}


# ---------------------------------------------------------------------------
# THE ALPHA OBJECT MODEL'S READERS, NOW UNREFERENCED BY THE CLI (2026-09-28).
#
# Porting `store log` off `_read_snapshot_index` removed the LAST live caller of this group.
# The functions below are the alpha `.grip` snapshot object model -- snapshot index, its
# writes, snapshot-id resolution, the member map, the dirty check, the grip-dir guard. They
# are KEPT rather than deleted for ONE reason now: `store migrate` (design section 6a) READS
# the alpha store to convert it, so the readers stay until that step either adopts them or
# replaces them. Removing log's alpha branch, which was still live, made
# this group dead. `tests/test_grip_object_model.py` used to import three of them and is now RETIRED,
# so it is no longer a reason to keep anything -- corrected here because the first version of
# this comment named it as one.
# ---------------------------------------------------------------------------


def _read_snapshot_index(workspace: Path) -> list[dict[str, object]]:
    index_path = workspace / ".grip" / "snapshots" / "index.json"
    if not index_path.exists():
        return []
    return json.loads(index_path.read_text())


def _write_snapshot_index(workspace: Path, index: list[dict[str, object]]) -> None:
    snapshots_dir = workspace / ".grip" / "snapshots"
    snapshots_dir.mkdir(parents=True, exist_ok=True)
    index_path = snapshots_dir / "index.json"
    index_path.write_text(json.dumps(index, indent=2) + "\n")


class AmbiguousSnapshotId(Exception):
    """A prefix matching more than one snapshot. Never resolved by guessing."""

    def __init__(self, prefix: str, matches: list[str]) -> None:
        self.prefix = prefix
        self.matches = matches
        super().__init__(prefix)


def _find_snapshot_by_id(
    index: list[dict[str, object]],
    snapshot_id: str,
) -> dict[str, object] | None:
    """Resolve a snapshot by full id or by unique prefix.

    The store holds 40-character ids and ``grip log`` prints the first 12, so
    an exact-match-only lookup made the ONLY id a user can obtain from the CLI
    the one that does not work: ``grip checkout`` and ``grip diff`` answered
    ``Snapshot not found`` for an id ``grip log`` had just printed, while the
    working id existed solely inside ``.grip/snapshots/index.json``.

    Prefix resolution follows git's rule, including the part that matters most:
    an ambiguous prefix RAISES rather than picking the first match.  Silently
    resolving to one of several would make ``checkout`` land on an arbitrary
    snapshot, which is worse than the refusal this replaces.
    """
    if not snapshot_id:
        return None
    for entry in index:
        if entry.get("id") == snapshot_id:
            return entry
    matches = [e for e in index if str(e.get("id", "")).startswith(snapshot_id)]
    if len(matches) > 1:
        raise AmbiguousSnapshotId(snapshot_id, [str(e.get("id", "")) for e in matches])
    return matches[0] if matches else None


# ---------------------------------------------------------------------------
# gr grip
# ---------------------------------------------------------------------------


def _resolve_snapshot_or_exit(
    index: list[dict[str, object]],
    snapshot_id: str,
) -> dict[str, object] | None:
    """Resolve for a CLI verb, turning ambiguity into a refusal.

    ``_find_snapshot_by_id`` RAISES on an ambiguous prefix, which is right for
    a library: picking one of several arbitrarily is the thing we are trying
    not to do.  But every verb called it bare, so the raise reached the user as
    a traceback -- the exact class this whole range exists to remove,
    reintroduced by the fix for it.

    The verbs go through here rather than each carrying its own ``except``,
    for the same reason the current-lane fix added a required accessor: there
    were three call sites, and a per-site handler makes the fourth one's
    omission silent.  ``None`` (not found) still returns, because each verb
    words that message differently.
    """
    try:
        return _find_snapshot_by_id(index, snapshot_id)
    except AmbiguousSnapshotId as exc:
        typer.echo(
            f"Ambiguous snapshot id: {exc.prefix!r} matches {len(exc.matches)} snapshots:"
        )
        for match in exc.matches:
            typer.echo(f"  {match}")
        typer.echo("Use more characters to disambiguate.")
        raise typer.Exit(code=1)


def _store_git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(["git", "-C", str(root), *args], text=True, capture_output=True, check=False)
    if check and result.returncode:
        raise RuntimeError(result.stderr.strip() or "git command failed")
    return result


class NativeStoreRefusal(RuntimeError):
    """A native-store refusal with a stable CLI status."""

    def __init__(self, message: str, code: int) -> None:
        super().__init__(message)
        self.code = code


#: A moved workspace tracks `MOVED_MARKER` at its root, or holds its state under this ref: either
#: one means a newer gr2 owns this workspace and this one must not touch it.
MOVED_STATE_REF = "refs/dev.synapt.grip/__state__/v1"


def _refuse_moved_workspace(root: Path) -> None:
    """Refuse, in one sentence, a workspace whose state has moved out of the files this gr2 reads.

    Without this, an older gr2 meeting a moved workspace says "run store init" (its root has no
    `grip.toml` and no gitlinks), and following that advice starts a second, divergent workspace
    beside the real one. It reads only the root's git objects, never the working tree: the marker
    must be in `HEAD`, so a stray untracked copy does not count."""
    if not (root / ".git").exists():
        return
    in_head = _store_git(root, "ls-tree", "--name-only", "HEAD", "--", MOVED_MARKER, check=False)
    has_ref = _store_git(root, "rev-parse", "--verify", "--quiet", MOVED_STATE_REF, check=False)
    if (in_head.returncode == 0 and in_head.stdout.strip()) or (has_ref.returncode == 0 and has_ref.stdout.strip()):
        raise NativeStoreRefusal(
            "this workspace's state has moved to a format this gr2 cannot read; "
            "use a newer gr2 (gr2 --version shows this one)",
            4,
        )


def _refuses_moved_workspace(fn):
    """Run `_refuse_moved_workspace` on the root (the first argument) before the verb does anything."""
    import functools

    @functools.wraps(fn)
    def wrapper(root: Path, *args, **kwargs):
        _refuse_moved_workspace(root)
        return fn(root, *args, **kwargs)

    return wrapper



# The beta list of design section 4. These fields are PARSED and REFUSED, never silently
# ignored: a later schema version must not discover that alpha users wrote them and nothing
# happened.
BETA_GRIP_KEYS = ("staged", "overlay", "nested", "files", "hooks", "target", "detached", "units")


def _grip_document(root: Path) -> dict:
    path = root / "grip.toml"
    if path.exists():
        with path.open("rb") as handle:
            return tomllib.load(handle)
    # A root remote need not carry member objects.  Read the canonical spec
    # directly so materialize can populate those members before checkout.
    # ⚠ AND A ROOT WITH NEITHER IS A REFUSAL, NOT AN EXIT 1 (measured 2026-09-28). Asking
    # git for `HEAD:grip.toml` where there is no HEAD and no document raised the generic
    # RuntimeError, so `store status` and `store check` printed git's raw "fatal: invalid
    # object name 'HEAD'" and exited 1 -- a named refusal on an internal code. The group's
    # table has a code for this: 5, cannot measure.
    spec = _store_git(root, "show", "HEAD:grip.toml", check=False)
    if spec.returncode:
        raise NativeStoreRefusal(
            f"no grip.toml at {root} and no committed spec at HEAD; this root is not a "
            f"store these verbs can measure -- run store init here",
            5,
        )
    return tomllib.loads(spec.stdout)


def _beta_fields(node: object) -> list[str]:
    """Every beta key name appearing ANYWHERE in the document, at any depth.

    Depth matters and is not a nicety: TOML keeps the last table header active to the end
    of the file, so a user who appends `staged = true` to a `grip.toml` written by
    `store init` lands it INSIDE `[members.remotes]`, not at the top level. A top-level-only
    scan reads that as an unknown sub-key of `remotes`, drops it, and reports success --
    which is the silent-ignore section 4 forbids (measured on break_11's fixture, which
    appends exactly that way).
    """
    found: list[str] = []

    def walk(value: object) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                if key in BETA_GRIP_KEYS:
                    found.append(key)
                walk(child)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(node)
    return found


def _refuse_beta(root: Path) -> None:
    """Exit 4 naming the beta field. A beta key accepted in silence is a contract the
    reader cannot see (design section 4)."""
    document = _grip_document(root)
    for key in _beta_fields(document):
        raise NativeStoreRefusal(f"{key} is beta and is not implemented in this slice", 4)
    for member in document.get("members", []):
        if not isinstance(member, dict):
            continue
        mode = str(member.get("mode", "full"))
        if mode != "full":
            raise NativeStoreRefusal(
                f"{member.get('name', 'member')} mode {mode!r} is beta; link mode is beta", 4
            )


def _refuse_symlinked_members(root: Path, members: list[dict[str, str]]) -> None:
    """Exit 4 naming the member. A gitlink needs a real checkout at its path, and
    `update-index --cacheinfo 160000` over a symlink leaves the root reporting a type
    change forever (design section 11.2)."""
    for member in members:
        if (root / member["path"]).is_symlink():
            raise NativeStoreRefusal(
                f"{member['name']} path {member['path']} is a symlink; link mode is beta "
                f"-- a gitlink needs a real checkout at its path",
                4,
            )


def _native_members(root: Path, revision: str | None = None) -> list[dict[str, str]]:
    # A selected workspace commit owns the whole document, not only its pins.
    data = (_grip_document(root) if revision is None else
            tomllib.loads(_store_git(root, "show", f"{revision}:grip.toml").stdout))
    members = data.get("members", [])
    if not isinstance(members, list) or not members:
        raise NativeStoreRefusal("grip.toml has no members; run store init against a root that has them", 4)
    result: list[dict[str, str]] = []
    for member in members:
        item = dict(member)
        remotes = item.pop("remotes", {})
        if not isinstance(remotes, dict) or not isinstance(remotes.get("origin"), str):
            raise NativeStoreRefusal(f"{item.get('name', 'member')} has no origin remote", 4)
        item["remote"] = remotes["origin"]
        if revision is not None:
            item["path"] = _normalise_member_path(root, str(item["path"]))
            if any(previous["name"] == item["name"] or previous["path"] == item["path"] for previous in result):
                raise NativeStoreRefusal("selected workspace commit has duplicate member names or paths", 4)
            link = _store_git(root, "ls-tree", revision, "--", item["path"]).stdout.split()
            if len(link) < 3 or link[0] != "160000" or link[2] != item["pin"]:
                raise NativeStoreRefusal(f"{item['name']} selected document pin disagrees with its gitlink", 4)
        result.append(item)
    return result


def _write_native_members(root: Path, members: list[dict[str, str]]) -> None:
    lines = ["schema_version = 1", f"workspace_name = {json.dumps(root.name)}", ""]
    for member in members:
        lines.extend([
            "[[members]]",
            f"name = {json.dumps(member['name'])}",
            f"path = {json.dumps(member['path'])}",
            f"upstream = {json.dumps(member.get('upstream', 'origin/main'))}",
            f"ref = {json.dumps(member.get('ref', 'main'))}",
            f"pin = {json.dumps(member['pin'])}",
            'mode = "full"',
            "[members.remotes]",
            f"origin = {json.dumps(member['remote'])}",
            "",
        ])
    (root / "grip.toml").write_text("\n".join(lines))


def _url_has_credentials(url: str) -> bool:
    """Refuse URL userinfo except an SSH login, never by printing the URL (owner: grip.url_has_credentials)."""
    return grip_mod.url_has_credentials(url)


def _credential_refusal(name: str) -> NativeStoreRefusal:
    return NativeStoreRefusal(
        f"{name} origin contains credentials; remove URL userinfo and use a credential helper", 4
    )


def _infer_member_branch(path: Path) -> tuple[str, str]:
    """(ref, upstream) for the member checkout at `path`: the branch it is ON, not a literal.

    This used to be the fixed pair ("main", "origin/main") for every member, so a member on
    `core/main` had its pin checked against `origin/main` and the first `store commit` of a
    correct workspace was refused. The rule, with each edge decided:

      * attached to branch B: ref = B, and upstream = B's tracking ref when that ref's remote
        is `origin`, else `origin/B`. ORIGIN ONLY, because `grip.toml` carries the member's
        origin url and nothing else: a recorded upstream on any other remote (a fork whose
        branch tracks `upstream/main`) names a remote that a fresh clone and materialize does
        not have, and `store check` there exits 5 "cannot fetch upstream" where the original
        root passed. A branch tracking a LOCAL branch (its remote is ".") is the same case:
        its `@{upstream}` has no remote part, or one that is a local branch name, so it falls
        back too.
      * a branch with no upstream is NOT refused here: init is not where that is said, and the
        coverage check at commit then names the branch that is really missing ("not on
        origin/B; push it first") instead of naming main.
      * detached HEAD (or no readable branch): today's defaults, no refusal. A materialized
        workspace is detached at its pins, so refusing would break init there, and an
        uncovered pin is already refused at commit.

    One reader, so every caller that records a member agrees on what its branch is.
    """
    current = _store_git(path, "branch", "--show-current", check=False)
    branch = current.stdout.strip() if current.returncode == 0 else ""
    if not branch:
        return "main", "origin/main"
    tracking = _store_git(path, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}", check=False)
    upstream = tracking.stdout.strip() if tracking.returncode == 0 else ""
    remote = _store_git(path, "config", "--get", f"branch.{branch}.remote", check=False).stdout.strip()
    if remote != "origin" or not upstream:
        upstream = f"origin/{branch}"
    return branch, upstream


def _member_from_path(root: Path, rel: str, name: str) -> dict[str, str]:
    """The member record for one checkout, wherever its path came from.

    One reader for all three sources (a declared spec, an explicit `--member`, direct-child
    discovery), so the credential refusal, the origin requirement and the pin read cannot
    drift between them.
    """
    path = root / rel
    remote = _store_git(path, "remote", "get-url", "origin", check=False)
    if remote.returncode:
        raise NativeStoreRefusal(
            f"{rel} has no origin remote; store init cannot pin a member it cannot fetch", 4
        )
    url = remote.stdout.strip()
    if _url_has_credentials(url):
        raise _credential_refusal(rel)
    ref, upstream = _infer_member_branch(path)
    return {
        "name": name,
        "path": rel,
        "remote": url,
        "upstream": upstream,
        "ref": ref,
        "pin": _store_git(path, "rev-parse", "HEAD").stdout.strip(),
    }


def _declared_member_paths(root: Path) -> list[tuple[str, str]]:
    """(name, path) for each member the root's OWN spec declares, or [] when it declares none.

    The alpha spec (`.grip/workspace_spec.toml`, `[[repos]]`) is read because it is what a real
    workspace carries BEFORE it is adopted: a real workspace declares its members' nested
    paths (`core/config`, `team-b/config`) there. Section 3a's rule that init never edits an adopted root's own
    files is untouched -- this only READS the declaration.
    """
    spec = root / ".grip" / "workspace_spec.toml"
    if not spec.is_file():
        return []
    try:
        doc = tomllib.loads(spec.read_text())
    except tomllib.TOMLDecodeError as exc:
        raise NativeStoreRefusal(f"the workspace spec at {spec} is not readable TOML: {exc}", 4)
    declared: list[tuple[str, str]] = []
    for entry in doc.get("repos", []):
        rel = str(entry.get("path", "")).strip()
        if not rel:
            continue
        declared.append((str(entry.get("name", "")).strip() or _name_for_path(rel), rel))
    return declared


def _name_for_path(rel: str) -> str:
    """`core/config` -> `core-config`: the name the alpha spec in this workspace already uses."""
    return rel.strip("/").replace("/", "-")


def _normalise_member_path(root: Path, rel: str) -> str:
    """The member path the store will record, or a refusal at 4 that names it.

    A member path becomes a gitlink path, so it has to be one git can write: relative, inside
    the root, and free of a `..` part. Measured 2026-09-28 -- `--member ../outside` inited rc 0
    and then failed at `store commit` rc 5 with `update-index: --cacheinfo cannot add
    ../outside`, i.e. the verb built a store that could never commit.
    """
    posix = PurePosixPath(rel.strip())
    normalised = str(posix)
    if posix.is_absolute() or normalised in ("", ".") or ".." in posix.parts:
        raise NativeStoreRefusal(
            f"member path {rel!r} is not usable: it must be a relative path inside the root",
            4,
        )
    target = (root / normalised).resolve()
    root_resolved = root.resolve()
    if target != root_resolved and root_resolved not in target.parents:
        raise NativeStoreRefusal(
            f"member path {rel!r} resolves outside the root ({target})",
            4,
        )
    return normalised


def _is_member_checkout(root: Path, rel: str) -> bool:
    path = root / rel
    if not path.is_dir():
        return False
    top = _store_git(path, "rev-parse", "--show-toplevel", check=False)
    return top.returncode == 0 and Path(top.stdout.strip()).resolve() == path.resolve()


# The one-release NAME fallback on the NATIVE STORE path, the sibling of
# `spec_apply._note_alpha_checkout`. gr2 alpha placed members by NAME, so a root whose
# member name differs from its declared path may hold the checkout at `<root>/<name>`
# with nothing at the path. Keyed on the two absolute paths, so a verb that resolves a
# member twice in one process still prints ONE line for one user action.
_NAME_PLACED_NOTED: set[tuple[str, str]] = set()


def _note_name_placed(member: dict[str, str], declared: Path, at_name: Path) -> None:
    """The one-release note for the native store: EXACTLY ONE line, naming BOTH coordinates.

    The line has to name where the checkout IS and the declaration that disagrees with it.
    A message naming only the path is the refusal this replaces, and one naming only the
    name leaves the reader unable to find the manifest line that disagrees with it.
    """
    key = (str(declared), str(at_name))
    if key in _NAME_PLACED_NOTED:
        return
    _NAME_PLACED_NOTED.add(key)
    typer.echo(
        f"note: '{member['name']}' is checked out at {at_name}, the name gr2 placed members "
        f"by until this release, while grip.toml declares its path as {member['path']!r} "
        f"({declared}). Reading the existing checkout where it is; nothing is moved and "
        "nothing is cloned over it.",
        err=True,
    )


def _claimed_member_paths(
    root: Path, members: list[dict[str, str]], exclude: dict[str, str] | None = None
) -> set[Path]:
    """Every OTHER member's TWO coordinates -- declared path AND name -- resolved.

    BOTH COORDINATES, and that is the whole point. The first cut of this built the set from
    DECLARED PATHS alone, and a SECOND reader broke it: a member whose checkout sits at its NAME
    coordinate (its declared path has nothing at it -- exactly the alpha-era placement this
    fallback exists for) contributed its declared path to the set and NOT the directory its
    checkout actually occupies. Another member whose name then landed on that directory found it
    UNCLAIMED and read it -- and pinned it -- as surely as the rename case, with no rename
    anywhere. Measured: `beta` declaring `beta-declared` while its checkout sits at `beta`, and
    `alpha` renamed to `beta`, put beta's HEAD on BOTH rows.

    `exclude` IS LOAD-BEARING, not tidiness: a member's OWN name coordinate must never be in its
    own claim set, or the fallback would refuse the very directory it exists to find.

    NAMED `_claimed_member_paths`, not `_declared_member_paths`: this module ALREADY defines
    `_declared_member_paths(root)` with a DIFFERENT signature, and a second definition under that
    name shadows the first silently -- it parses, and the existing one-argument caller fails only
    at RUNTIME with a TypeError. Found by grepping the name after writing it; no syntax check can
    see a redefinition at all.
    """
    claimed: set[Path] = set()
    for member in members:
        if member is exclude:
            continue
        claimed.add((root / member["path"]).resolve())
        name = str(member.get("name", ""))
        if name and name != member["path"]:
            try:
                claimed.add((root / _normalise_member_path(root, name)).resolve())
            except NativeStoreRefusal:
                # A sibling whose name cannot be normalised claims nothing; its own reads are
                # refused elsewhere and it must not take the fallback away from anyone.
                continue
    return claimed


def _native_member_working_root(
    root: Path, member: dict[str, str], claimed: set[Path]
) -> Path:
    """Where this member's checkout actually IS: the PATH first, then the NAME.

    THE PATH WINS WHENEVER IT HOLDS A CHECKOUT, and the NAME is consulted only when it
    does not -- so every root whose two coordinates agree resolves exactly as before, and
    this cannot change which member a verb reads.

    When nothing is at the path and a checkout sits at the name, the checkout is read
    WHERE IT IS and both coordinates are named. Nothing is moved; nothing is cloned over
    it. The alternative was a refusal, and it is the wrong one here: the member is present
    and healthy, one directory over, and "cannot measure" tells the user nothing about
    where it went. Exit 5 stays for a member that really is unreadable.

    `claimed` IS EVERY OTHER MEMBER'S TWO COORDINATES, RESOLVED (`_claimed_member_paths`, called
    with `exclude=<this member>`), and the fallback REFUSES a name coordinate that lands **on OR
    INSIDE** one -- a subtree test, not an equality test. Landing on a different member's directory
    is what let a renamed member be measured, and PINNED, from its neighbour; landing *inside* one
    is the same wrong pin one level out, and equality alone cannot see it.

    IT IS REQUIRED, deliberately, on a reader's suggestion. It was typed `= None` with an
    `in (claimed or ())` test, so a caller that forgot the argument turned the guard OFF silently
    -- a guard failing in the one direction that has no symptom and no error. Every caller passes
    it today, so requiring it costs nothing and turns the omission into a TypeError.
    """
    declared = root / member["path"]
    if _is_member_checkout(root, member["path"]):
        return declared
    name = str(member.get("name", ""))
    if name and name != member["path"]:
        try:
            # THE NAME IS A PATH COORDINATE, so it goes through the SAME normaliser the declared
            # path does. Without this the fallback was a second door into the escape that
            # normaliser exists to close: a name that is ABSOLUTE or carries `..` resolved
            # outside the root, `store status` reported the foreign checkout's HEAD, and
            # `store commit` ran git (fetch) inside it. Refused here exactly as a path is.
            at_name_rel = _normalise_member_path(root, name)
        except NativeStoreRefusal:
            # Falling back to `declared` keeps the refusal the parent already gives this shape:
            # the member stays unmeasurable, which is what a containing store should say.
            return declared
        if _is_member_checkout(root, at_name_rel):
            # A NAME THAT LANDS ON ANOTHER MEMBER'S DECLARED PATH IS NOT THIS MEMBER'S CHECKOUT.
            # Found by Apollo's r2 on this lane (2026-09-30): member `alpha` RENAMED to `beta` while
            # a member named `beta` exists, and alpha's declared path empty. The fallback resolved
            # alpha's name onto beta's checkout, so `store status` reported beta's HEAD on alpha's
            # row AND `store commit` REWROTE alpha's gitlink pin to beta's sha at rc 0. The parent
            # refuses this shape, so the fallback INTRODUCED it; a wrong READ is a bad answer, a
            # wrong PIN is a bad byte in the root snapshot.
            #
            # CONTAINMENT, NOT A CONFIG GATE: refusing a duplicate member NAME once at discovery is
            # a smaller diff and would close THIS example, but it refuses the whole WORKSPACE for a
            # collision that only the fallback can act on, and it misses the wider class -- a name
            # that lands on a path another member declares under a DIFFERENT name, where no
            # duplicate name exists at all. This closes the class that can write.
            # A SUBTREE TEST, not an equality test, and the difference is a whole class of shape.
            # Exact membership closes "the name lands ON another member's coordinate" and CANNOT
            # close "the name lands INSIDE one" -- measured by a reader (2026-09-30) on
            # `alpha.name = "beta/nested"` with `beta/nested` a real checkout carrying its own
            # fetchable origin: alpha's gitlink pin was rewritten to the NESTED checkout's HEAD at
            # rc 0, which is the same wrong-pin severity as the direct collision.
            #
            # ⚠ AND THAT SHAPE ALMOST READ AS CLOSED ANYWAY. With NO remote on the nested checkout
            # the read WAS stopped -- by §5a's coverage fetch (`cannot fetch origin`), not by this
            # guard. A refusal arriving from a DIFFERENT guard looks exactly like this predicate
            # working, so the row that witnesses it must give the nested checkout a real origin.
            target = (root / at_name_rel).resolve()
            if any(p == target or p in target.parents for p in claimed):
                return declared
            at_name = root / at_name_rel
            _note_name_placed(member, declared, at_name)
            return at_name
    return declared


def _discover_members(root: Path, declared: list[str] | None = None) -> list[dict[str, str]]:
    """The member checkouts `store init` records, in the order the sources are trusted.

    ⚠ PATHS THE ROOT DECLARES COME BEFORE ANY GUESS (measured 2026-09-28 on a root whose
    members sit one level down). This used to iterate `root.iterdir()` and nothing else, so a
    root whose members sit one level down (`core/config`, `team-b/config`) refused with "no
    sibling git repositories found to store" while its own spec named both paths. The depth-1/depth-2 control that found
    it: the SAME member clone moved to depth 1 inited rc 0.

    Order: explicit `--member` paths, then the root's declared spec, then direct children. A
    source that names members is never silently answered with "nothing found" -- if a named
    path is not a checkout, the refusal NAMES it, because that message is the only thing a
    stranger has to go on.
    """
    if declared:
        # ⚠ A NAMED PATH GETS THE SAME CHECKS AS A DECLARED ONE (found by two readers, 2026-09-28).
        # Without the checkout check, `--member plain` on a plain folder exited 0 and recorded the
        # ROOT's origin and HEAD as member "plain": `rev-parse --show-toplevel` inside a folder
        # that is not a checkout resolves to the ENCLOSING root, so the answer looked like a
        # member with an origin. And without the path checks, `--member ../outside` exited 0 and
        # built a store that could never commit -- `update-index --cacheinfo cannot add
        # ../outside` at rc 5, in git's own words. Every refusal here happens BEFORE a record is
        # built, and every one NAMES the path: "no origin remote" or a raw git fatal describes a
        # symptom the caller cannot act on.
        entries = [(None, rel) for rel in declared]
    else:
        spec_members = _declared_member_paths(root)
        if not spec_members:
            members: list[dict[str, str]] = []
            for path in sorted(root.iterdir()):
                if not path.is_dir() or path.name.startswith("."):
                    continue
                if not _is_member_checkout(root, path.name):
                    continue
                members.append(_member_from_path(root, path.name, path.name))
            return members
        entries = [(name, rel) for name, rel in spec_members]

    resolved: list[tuple[str, str]] = []
    seen_paths: set[str] = set()
    seen_names: dict[str, str] = {}
    seen_targets: dict[Path, str] = {}
    problems: list[str] = []
    root_resolved = root.resolve()
    for given_name, rel in entries:
        try:
            normalised = _normalise_member_path(root, rel)
        except NativeStoreRefusal as exc:
            problems.append(str(exc))
            continue
        # ⚠ THE NAME COMES FROM THE NORMALISED PATH, not the caller's spelling: `./core/config`
        # used to name a member `.-core-config`. A declared spec's own name wins where it gave one.
        name = given_name or _name_for_path(normalised)
        if normalised in seen_paths:
            # The SAME path named twice is one member, not a refusal: the caller has said the
            # same thing twice, which is a de-duplication and not a mistake to bounce.
            continue
        if name in seen_names:
            problems.append(
                f"{seen_names[name]!r} and {normalised!r} both resolve to the member name {name!r}"
            )
            continue
        target = (root / normalised).resolve()
        # ⚠ A MEMBER PATH MUST NOT BE THE ROOT OR AN ALIAS OF ANOTHER MEMBER (found by the reads
        # on the range that added the path checks; both cases are the base's behaviour too).
        # `self -> .` resolved to the root and was recorded as a member carrying the ROOT's
        # origin and HEAD; `alias -> core/config` is one checkout claiming two names. Both left
        # init exiting 0 and `store commit` refusing them at 4 afterwards.
        if target == root_resolved:
            problems.append(f"member path {normalised!r} resolves to the ROOT itself, not a member")
            continue
        if target in seen_targets:
            problems.append(
                f"member path {normalised!r} resolves to the same checkout as "
                f"{seen_targets[target]!r}: one checkout cannot be two members"
            )
            continue
        if not _is_member_checkout(root, normalised):
            problems.append(
                f"member path {normalised!r} is not a git checkout: a member is a checkout with "
                f"its own origin, not a folder"
            )
            continue
        seen_paths.add(normalised)
        seen_names[name] = normalised
        seen_targets[target] = normalised
        resolved.append((name, normalised))
    # ONE refusal naming EVERY problem: a caller fixing paths one refusal at a time is the
    # failure mode this replaces, and the paths are the only thing they can act on.
    if problems:
        raise NativeStoreRefusal("cannot use these member paths: " + "; ".join(problems), 4)
    return [_member_from_path(root, rel, name) for name, rel in resolved]


def _refuse_init_disagreement(root: Path, members: list[dict[str, str]] | None = None) -> None:
    """Exit 4 when an existing `grip.toml` names a different root than this one is.

    `store init` is idempotent, and this is what makes the word mean something: a second
    init against a spec the root no longer matches is a disagreement, not a no-op
    (design section 5). Before this, a root carrying another root's `grip.toml` returned 0
    and said nothing (measured 2026-09-28).

    Identity only -- name, path, origin. PINS ARE NOT COMPARED ON PURPOSE: the pin belongs
    to `store commit`, so comparing it would make a re-init after any commit refuse, which
    is not what idempotent means. And an empty discovery is not a disagreement: a root
    whose members live at nested paths (the witness shape, `core/config`) is derived from
    `.grip/workspace_spec.toml`, not from siblings, and the document is the authority there.
    """
    if members is None:
        members = _discover_members(root)
    if not members:
        return
    document = _grip_document(root)

    def identity(entry: dict) -> str:
        return f"path={entry.get('path')} origin={(entry.get('remotes') or {}).get('origin')}"

    recorded = {
        str(member.get("name")): identity(member)
        for member in document.get("members", [])
        if isinstance(member, dict)
    }
    actual = {member["name"]: f"path={member['path']} origin={member['remote']}" for member in members}
    if recorded == actual:
        return
    for name in sorted(set(recorded) | set(actual)):
        if recorded.get(name) != actual.get(name):
            raise NativeStoreRefusal(
                f"grip.toml disagrees with this root: {name} is recorded as "
                f"{recorded.get(name, '<absent>')} but the root has {actual.get(name, '<absent>')}",
                4,
            )


STORE_INCOMPLETE_PREFIX = "cannot complete this store verb: "
STORE_COMMIT_UNCHANGED = "Nothing to record: every pin already matches the root's last commit."
"""Section 5's exit table has no room for an internal error.

For the whole `store` group the table is 0 ok, 2 usage, 3 refused on coverage or cleanliness,
4 refused as inconsistent or beta, 5 cannot measure. A verb that could not COMPLETE -- an
unexpected git failure, an unreadable root -- has no code of its own in that set, so it
reports 5 and prefixes its message with this string. THE PREFIX IS PART OF THE SURFACE, not a
style choice: it is what tells a measured "cannot measure" apart from a wrapped exception, and
a caller reading the code alone cannot. Named as a constant so the ten handlers share one
spelling, and named in the change description so a reader can hold the contract.
"""

def _compile_workspace_gitignore(declaration: str) -> str:
    from .gitinclude import compile_gitignore

    generated, notices = compile_gitignore(declaration)
    if notices:
        detail = "; ".join(f"{n.line}: {n.reason}" for n in notices)
        raise NativeStoreRefusal(f".gitinclude cannot be compiled: {detail}", 4)
    return generated


def _regenerate_workspace_gitignore(root: Path) -> bool:
    declaration = root / ".gitinclude"
    if declaration.is_symlink():
        raise NativeStoreRefusal(".gitinclude must be a regular file, not a symlink", 4)
    if declaration.exists() and not declaration.is_file():
        raise NativeStoreRefusal(".gitinclude must be a regular file", 4)
    if not declaration.is_file():
        return False  # Historical adopted roots keep their owner's file.
    _validate_workspace_ignore_paths(root)
    generated = _compile_workspace_gitignore(declaration.read_text(encoding="utf-8"))
    _publish_workspace_gitignore(root, generated)
    return True


def _validate_workspace_ignore_paths(root: Path) -> None:
    for name in (".gitinclude", ".gitignore"):
        path = root / name
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise NativeStoreRefusal(f"{name} must be a regular file, not a symlink or directory", 4)
    tracked = _store_git(root, "ls-files", "--", ".gitignore").stdout
    if tracked:
        raise NativeStoreRefusal(".gitignore is tracked; preserve the owner's file and resolve the declaration conflict before generation", 4)


def _publish_workspace_gitignore(root: Path, generated: str) -> None:
    """Replace generated output without opening an existing owner path."""
    import os
    import stat
    import tempfile

    _validate_workspace_ignore_paths(root)
    target = root / ".gitignore"
    mode = stat.S_IMODE(target.stat().st_mode) if target.exists() else 0o644
    fd, temporary = tempfile.mkstemp(prefix=".gitignore-", dir=root)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(generated)
        os.chmod(temporary, mode)
        os.replace(temporary, target)
    finally:
        try:
            os.unlink(temporary)
        except OSError:
            pass  # Cleanup cannot replace a primary write/replace failure.


def _write_gitignore(root: Path, members: list[dict[str, str]]) -> None:
    """Seed the inclusion declaration, then use the existing compiler."""
    declaration = root / ".gitinclude"
    _validate_workspace_ignore_paths(root)
    if not declaration.exists():
        declaration.write_text("grip.toml\n" + "".join(f"{m['path']}\n" for m in members), encoding="utf-8")
    _regenerate_workspace_gitignore(root)


@_refuses_moved_workspace
def _native_store_init(root: Path, member_paths: list[str] | None = None) -> None:
    if (root / ".git").exists() and (root / "grip.toml").exists():
        _refuse_init_disagreement(root)
        _regenerate_workspace_gitignore(root)
        return
    # ⚠ A STORE ROOT MUST BE ITS OWN REPO. Measured 2026-09-28: before this check, `store
    # init` inside another repo's worktree returned 0 and created a NESTED repo, so the
    # root's commits lived inside a parent that would track them as ordinary files. Design
    # section 5 names this refusal (exit 4). `rev-parse --show-toplevel` succeeding on a root
    # that has no `.git` of its own is the measurement: the answer comes back as the
    # enclosing repo, which is never the root.
    enclosing = _store_git(root, "rev-parse", "--show-toplevel", check=False)
    if enclosing.returncode == 0:
        top = Path(enclosing.stdout.strip()).resolve()
        if top != root.resolve():
            raise NativeStoreRefusal(
                f"{root} is inside another repository's worktree ({top}); a store root is "
                f"its own repo -- clone or move the workspace out before store init",
                4,
            )
    members = _discover_members(root, declared=member_paths)
    if not members:
        raise NativeStoreRefusal("no sibling git repositories found to store", 4)
    # New roots record .gitinclude. Adopted roots without that declaration
    # retain their owner's ignore file and historical index.
    created_repo = not (root / ".git").exists()
    # ⚠ PIN THE ROOT BRANCH. A bare `git init` takes its initial branch from the ambient
    # git configuration, so the same workspace produced `main` on a host whose system
    # config sets init.defaultBranch and `master` on one that does not. That is not only a
    # test-environment artifact: `store push` publishes "the ROOT branch", and `store
    # commit` commits on "the root's current branch", so two machines would publish and
    # record under different branch names for identical input. Found by break_10
    # (test_store_break_attempts.py), which could never run: the root came out on `master`
    # and its own `git checkout -q main` died in SETUP with "pathspec 'main' did not match
    # any file(s) known to git", so the row had never once exercised the verb. The suite's
    # `_isolated_git_config` fixture is what exposes it, by setting GIT_CONFIG_NOSYSTEM=1
    # and pointing GIT_CONFIG_GLOBAL at an empty file -- exactly the blank-config host a
    # stranger has. `-b` makes the branch a property of the verb, not of the machine.
    _store_git(root, "init", "-b", "main")
    _write_native_members(root, members)
    if created_repo:
        _write_gitignore(root, members)
    else:
        _regenerate_workspace_gitignore(root)
    # gr2's per-desk state lives under `.grip/` and is never tracked. An adopted root keeps its
    # owner's `.gitignore`, so the root's own `.git/info/exclude` carries the line (local to the clone).
    grip_mod.exclude_grip_state(root)


def _git_detail(proc: subprocess.CompletedProcess[str]) -> str:
    """git's own stderr as one line.

    A resolution refusal that DISCARDS its diagnostic leaves the user with a bare "not a root
    commit" and no way to proceed. For an ambiguous short sha git's stderr is exactly what
    names the candidates ("hint: The candidates are:"), which is the half of the refusal a
    user needs. Carried into the message rather than dropped (Atlas's sweep flagged the two
    prefix-resolution rows on these verbs).
    """
    lines = [line.strip() for line in (proc.stderr or "").splitlines() if line.strip()]
    return " | ".join(lines)


def _require_root_commit(root: Path) -> None:
    """Refuse at 5 when the root has no commit yet.

    LIFTED OUT OF `status` because the
    same unborn-HEAD read was in three more verbs: `check` ran `ls-tree HEAD`, `log` ran
    `rev-list HEAD`, and `push` inherits it from check. Each surfaced git's own "fatal: invalid
    object name 'HEAD'" -- or "ambiguous argument" -- across the exit surface, which section
    5's table has no room for. One guard, three callers, so the three cannot drift apart the
    way commit and check did on section 5a.

    It is placed AFTER the document read at every call site, so a root that is not a store at
    all still gets its own message rather than this one.
    """
    if _store_git(root, "rev-parse", "--verify", "HEAD", check=False).returncode:
        raise NativeStoreRefusal("no root commit yet; run store commit", 5)


def _member_coverage(
    root: Path, member: dict[str, str], sha: str, claimed: set[Path]
) -> None:
    """Section 5a, in ONE place, so two verbs cannot answer it with different refs.

    §5a: run `git fetch <upstream-remote>` and fail with exit 5 if it fails, then
    `git merge-base --is-ancestor <sha> <upstream>`; the check is against `upstream`, never
    against the literal name `origin`.

    THIS HELPER EXISTS BECAUSE OF A MEASURED DIVERGENCE (measured
    against base acb74890): `store commit` compared against the literal `origin/main` and
    never fetched, while `store check` fetched and compared against `member["upstream"]`.
    The two disagree only when a member sets `upstream` to anything else --
    `_write_native_members` writes `upstream = member.get("upstream", "origin/main")`, so the
    DEFAULT EQUALS THE HARDCODE and every member on today's data agreed with itself. The
    fault was invisible by construction and would appear exactly when the field starts
    being meaningful. Fathom's handoff (57f24fb) closed the ref half; this closes the fetch
    half and makes the two verbs share the implementation rather than the behaviour.

    The comparison takes the sha it is asked about, because the callers ask different
    questions and §5a's input follows the verb: `store commit` asks whether the HEAD it is
    about to record is covered; `store check` asks whether the PIN ALREADY RECORDED is
    covered (the recorded pin, not the member's current HEAD -- a member whose HEAD moved is
    `unpinned`, a status observation, and its pin may still be perfectly covered).
    """
    # THE WORKING ROOT, not the declared path: §5a asks about the member that is HERE, and a
    # root placed by name one directory over keeps its checkout at the name. The gitlink KEY
    # stays the declared path at every call site that writes one -- that key belongs to the
    # root snapshot, not to wherever the working copy happens to be.
    path = _native_member_working_root(root, member, claimed)
    upstream = member["upstream"]
    remote, separator, branch = upstream.partition("/")
    if not separator or not remote or not branch:
        raise NativeStoreRefusal(f"{member['name']} has invalid upstream {upstream!r}", 4)
    if _store_git(path, "fetch", remote, check=False).returncode:
        raise NativeStoreRefusal(f"{member['name']} cannot fetch {remote}", 5)
    if _store_git(path, "merge-base", "--is-ancestor", sha, upstream, check=False).returncode:
        raise NativeStoreRefusal(
            f"{member['name']} pin {sha} is not on {upstream}; push it first", 3
        )


@_refuses_moved_workspace
def _native_store_commit(root: Path, message: str) -> bool:
    """Record the pins; True when a root commit was made, False when there was nothing to record."""
    _refuse_beta(root)
    members = _native_members(root)
    _refuse_symlinked_members(root, members)
    changed: list[dict[str, str]] = []
    for member in members:
        if _url_has_credentials(member["remote"]):
            raise _credential_refusal(member["name"])
        # PER MEMBER, and `exclude=member` is load-bearing: a member's OWN name coordinate must not
        # be in its own claim set, or the fallback would refuse the directory it exists to find.
        claimed = _claimed_member_paths(root, members, exclude=member)
        path = _native_member_working_root(root, member, claimed)
        # A member path that is not a checkout cannot be measured at all. Named here rather
        # than left to the residual RuntimeError backstop: the backstop's code (5) is right
        # but its message is git's, and a refusal a reader meets first should say what to run.
        top = _store_git(path, "rev-parse", "--show-toplevel", check=False)
        if top.returncode or Path(top.stdout.strip()).resolve() != path.resolve():
            raise NativeStoreRefusal(
                f"{member['name']} path {member['path']} is not a checkout; materialize it first",
                5,
            )
        # Section 5's commit row refuses a dirty member at exit 3, and it is checked BEFORE
        # coverage so the refusal names the thing the author has to fix first. Before this,
        # no verb in the group refused a dirty member, though the row names it.
        if _store_git(path, "status", "--porcelain").stdout.strip():
            raise NativeStoreRefusal(
                f"{member['name']} is dirty; commit or stash changes first", 3
            )
        head = _store_git(path, "rev-parse", "HEAD").stdout.strip()
        _member_coverage(root, member, head, claimed)
        changed.append({**member, "pin": head})
    has_declaration = _regenerate_workspace_gitignore(root)
    if has_declaration:
        from .gitinclude import path_is_included
        declaration = (root / ".gitinclude").read_text(encoding="utf-8")
        # Ignore rules do not constrain already staged files or cacheinfo writes.
        # Refuse excluded index entries without clearing the author's index.
        indexed = _store_git(root, "ls-files", "-z").stdout.split("\0")
        head = _store_git(root, "rev-parse", "--verify", "HEAD", check=False)
        committed = set(_store_git(root, "ls-tree", "-r", "--name-only", "-z", "HEAD").stdout.split("\0")) if head.returncode == 0 else set()
        for path in dict.fromkeys(["grip.toml", *[m["path"] for m in changed], *filter(None, indexed)]):
            if not path_is_included(declaration, path):
                raise NativeStoreRefusal(f"{path} is not included by .gitinclude; update the declaration before store commit", 4)
            ignored = _store_git(root, "check-ignore", "--no-index", "--", path, check=False)
            if ignored.returncode == 0 and path not in committed:
                raise NativeStoreRefusal(f"{path} is ignored by Git; resolve its ignore rule before store commit", 4)
            if ignored.returncode not in (0, 1):
                raise NativeStoreRefusal(f"cannot measure .gitinclude coverage for {path}: {_git_detail(ignored)}", 5)
    _write_native_members(root, changed)
    _store_git(root, "add", "grip.toml")
    if has_declaration:
        _store_git(root, "add", ".gitinclude")
    for member in changed:
        _store_git(root, "update-index", "--add", "--cacheinfo", f"160000,{member['pin']},{member['path']}")
    # NOTHING STAGED IS A NO-OP, NOT A FAILURE. When every pin already equals the
    # gitlink the root last committed, `git commit` exits 1 with "nothing to commit" on STDOUT
    # and an empty stderr, which `_store_git` renders as the bare "git command failed" and the
    # handler maps to exit 5. `diff --cached --quiet` is the question that distinguishes the
    # two: 0 means nothing staged, 1 means something is, anything else is a real git failure.
    staged = _store_git(root, "diff", "--cached", "--quiet", check=False)
    if staged.returncode == 0:
        return False
    if staged.returncode != 1:
        raise RuntimeError(staged.stderr.strip() or "git diff --cached failed")
    _store_git(root, "-c", "user.name=gr2", "-c", "user.email=gr2@example.invalid", "commit", "-m", message)
    return True


@_refuses_moved_workspace
def _native_store_check(root: Path) -> list[dict[str, str]]:
    """Verify the committed root pins against each live member upstream."""
    _refuse_beta(root)
    issues = validate_grip_toml(root)
    if issues:
        raise NativeStoreRefusal(f"grip.toml schema invalid: {issues[0].message}", 4)
    _require_root_commit(root)
    members = _native_members(root)
    checked: list[dict[str, str]] = []
    for member in members:
        claimed = _claimed_member_paths(root, members, exclude=member)
        # ORDER MATTERS AND THE DESIGN STATES IT: §5's check row reads "schema valid, gitlink
        # equals pin for every member, every pin covered against LIVE upstream", so the
        # consistency comparison runs BEFORE coverage. The other order changes which refusal
        # a malformed root meets first: with coverage first, break_07's hand-edited pin (all
        # zeros) fails as UNCOVERED (3) before the verb ever compares it to the gitlink, and
        # the row that exists to pin "every verb that reads the pin exits 4 and names BOTH
        # values" goes red -- measured 2026-09-28 when this helper was introduced.
        tree = _store_git(root, "ls-tree", "HEAD", "--", member["path"]).stdout.strip().split()
        gitlink = tree[2] if len(tree) >= 3 else ""
        if gitlink != member["pin"]:
            raise NativeStoreRefusal(
                f"{member['name']} gitlink {gitlink or '<missing>'} disagrees with pin {member['pin']}", 4
            )
        # §5a's input is the RECORDED PIN, not the member's current HEAD: this verb asks
        # whether the pin the root publishes is still covered, and a member whose HEAD has
        # moved is `unpinned` (a status observation) whose recorded pin may be fine.
        _member_coverage(root, member, member["pin"], claimed)
        checked.append({"name": member["name"], "pin": member["pin"], "upstream": member["upstream"], "state": "upstream"})
    return checked


@_refuses_moved_workspace
def _native_store_push(root: Path) -> dict[str, object]:
    """Push only the checked root record. Member publication is a prior verb."""
    members = _native_store_check(root)
    branch = _store_git(root, "symbolic-ref", "--short", "HEAD").stdout.strip()
    _store_git(root, "push", "origin", branch)
    return {
        "status": "pushed",
        "root_branch": branch,
        "root_commit": _store_git(root, "rev-parse", "HEAD").stdout.strip(),
        "members": members,
    }


def _native_members_at(root: Path, revision: str) -> dict[str, str]:
    text = _store_git(root, "show", f"{revision}:grip.toml").stdout
    document = tomllib.loads(text)
    return {str(member["name"]): str(member["pin"]) for member in document.get("members", [])}


@_refuses_moved_workspace
def _native_store_log(root: Path, max_count: int) -> list[dict[str, object]]:
    # THE DOCUMENT IS READ FIRST, so a root that is not a store gets the NAMED refusal rather
    # than git's own "not a git repository" wrapped in an exit code. Found by rewriting the
    # gr1 guidance row after the log port: that row's workspace is not a git repo at all, and
    # `rev-list` was the first thing the verb touched. Same defect class as the
    # raw git message status once returned, one verb over.
    _grip_document(root)
    _require_root_commit(root)
    commits = _store_git(root, "rev-list", "--reverse", f"--max-count={max_count}", "HEAD").stdout.splitlines()
    prior: dict[str, str] = {}
    entries: list[dict[str, object]] = []
    for commit in commits:
        # THE ADOPTION BOUNDARY (2026-09-28). A root that carried its own history
        # BEFORE it became a store -- an ADOPTED root -- has commits that predate grip.toml.
        # Reading the document at one of them is `fatal: path 'grip.toml' exists on disk, but
        # not in '<sha>'`, and it took the WHOLE verb down: exit 5 with no rows, on a root whose
        # store was otherwise working. The walk now STARTS AT THE BOUNDARY: a commit without the
        # document is outside the store's scope and is skipped, and every commit after it is
        # shown. `log` never fails whole on a pre-adoption commit again.
        #
        # ⚠ THIS COMMENT ONCE CLAIMED MORE THAN IT MEASURED. An earlier version said the same
        # fatal also "poisoned the store forward -- the next `commit` exited 5 as well". That
        # causal link is FALSE. Measured on the base arm with this guard disabled and a
        # LEGITIMATE second commit (the member advanced AND pushed to its origin first):
        # commit1 exit 0, commit2 exit 0, and only `log` exits 5. The second exit 5 I had seen
        # was the no-change commit, which fails identically on an UNADOPTED root -- so it was
        # never this boundary's doing, and `commit` was never blocked by it.
        #
        # AND THE SKIP IS NOT A STOP: a document DELETED mid-history and restored later is
        # hidden rather than reported, because every commit without it is skipped. A gap is not
        # a boundary. Named as a follow-on rather than fixed here.
        if _store_git(root, "cat-file", "-e", f"{commit}:grip.toml", check=False).returncode:
            continue
        current = _native_members_at(root, commit)
        changes = [{"name": name, "before": prior.get(name), "after": pin} for name, pin in current.items() if prior.get(name) != pin]
        entries.append({"commit": commit, "message": _store_git(root, "show", "-s", "--format=%s", commit).stdout.strip(), "pins": changes})
        prior = current
    return entries


@_refuses_moved_workspace
def _native_store_diff(root: Path, ref_a: str, ref_b: str) -> list[dict[str, object]]:
    # Both refs are resolved FIRST and by name, so an unresolvable one is a NAMED refusal at
    # 5 rather than a wrapped git message from whichever read happened to run first. Same
    # judgement as checkout's unresolvable ref, and the same code for the same reason: the
    # verb could not MEASURE the thing the caller named.
    _grip_document(root)  # the named refusal for a root that is not a store, first
    for ref in (ref_a, ref_b):
        resolved = _store_git(root, "rev-parse", "--verify", f"{ref}^{{commit}}", check=False)
        if resolved.returncode:
            raise NativeStoreRefusal(
                f"{ref} is not a root commit this store can resolve; git says: {_git_detail(resolved)}", 5
            )
    before = _native_members_at(root, ref_a)
    after = _native_members_at(root, ref_b)
    return [{"name": name, "old_pin": before.get(name), "new_pin": after.get(name), "changed": before.get(name) != after.get(name)} for name in sorted(set(before) | set(after))]


def _member_working_root_or_refuse(
    root: Path, members: list[dict[str, str]], member: dict[str, str]
) -> Path:
    """The member's WORKING ROOT, resolved exactly as `status`/`commit`/`check` resolve it,
    and REFUSED when that root is not a checkout.

    ONE RESOLVER FOR ALL FIVE VERBS. `checkout` and `materialize` used to build
    ``root / member["path"]`` directly, so a member whose checkout sits at its NAME
    coordinate -- the alpha-era placement the fallback exists for -- was acted on at a
    path that holds nothing, while three other verbs in the same group acted on the
    checkout that is really there. Two spellings of the member path in one verb group is
    how `checkout` came to leave a name-placed member silently unrestored, and how
    `materialize` came to clone a SECOND working copy beside the real one.

    THE `.git` CHECK IS THE POINT OF THIS HELPER, and it closes a class neither verb could
    see. When nothing is at the declared path and nothing is at the name, the resolver
    returns the DECLARED path -- correctly, it has nothing better to offer. But a plain
    directory inside a repository has no toplevel of its own, so `git status` and
    `git checkout` run there WALK UP and operate on an ancestor: the store root and its
    other members. A verb that reports one member while git acts on the whole store is the
    same wrong-subject defect as a name that resolves onto a neighbour. Refusing names the
    state; a ceiling on git's search would only change what git reads, not what we say.

    A `.git` FILE counts: a separate git dir, a linked worktree and a submodule all carry
    one, and all three are ordinary member checkouts.

    Exit 5 rather than 3: the group already answers "is not a checkout; materialize it
    first" with 5 at the `store commit` site, and this is that same state reached by a
    different verb. A dirty member is 3, and it is checked AFTER this -- there is no point
    asking whether a directory is dirty before knowing it is the member's directory.
    """
    claimed = _claimed_member_paths(root, members, exclude=member)
    path = _native_member_working_root(root, member, claimed)
    if not (path / ".git").exists():
        raise NativeStoreRefusal(
            f"{member['name']} path {path} is not a checkout (no .git); "
            f"materialize it first, or point grip.toml at where the checkout is",
            5,
        )
    return path


@_refuses_moved_workspace
def _native_store_checkout(root: Path, revision: str) -> tuple[str, list[dict[str, str]]]:
    """Materialize the members at a root commit (section 5: `checkout` is materialize-at-a-commit).

    ⚠ THE REVISION IS RESOLVED TO A SHA ONCE, BEFORE ANYTHING MOVES HEAD (measured
    2026-09-28, found by the new witness row). The first version ran
    `git checkout --detach <revision>` and then resolved the SAME NAME again to read the
    pins -- but detaching makes HEAD BE the revision, so the second use means a different
    commit, or none: with a two-commit history, `HEAD~1` after detaching at `HEAD~1` is the
    commit before that, which does not exist, and the verb died with git's own
    "fatal: invalid object name 'HEAD~1'". A name that means one thing and then another is
    the same class as a control that answers a different question than the one asked.
    """
    target = _store_git(root, "rev-parse", "--verify", f"{revision}^{{commit}}", check=False)
    if target.returncode:
        raise NativeStoreRefusal(
            f"{revision} is not a root commit this store can resolve; git says: {_git_detail(target)}", 5
        )
    sha = target.stdout.strip()
    members = _native_members(root, sha)
    planned: list[tuple[dict[str, str], Path, bool]] = []
    for member in members:
        if _url_has_credentials(member["remote"]):
            raise _credential_refusal(member["name"])
        # Selected C owns the physical path. Legacy PATH-then-NAME fallback
        # remains for ambient readers, but must not redirect this operation.
        path = root / member["path"]
        exists = (path / ".git").exists()
        if exists:
            top = _store_git(path, "rev-parse", "--show-toplevel", check=False)
            if top.returncode or Path(top.stdout.strip()).resolve() != path.resolve():
                raise NativeStoreRefusal(f"{member['name']} selected path {path} is not its own checkout", 5)
            if _store_git(path, "status", "--porcelain").stdout.strip():
                raise NativeStoreRefusal(f"{member['name']} is dirty; commit or stash changes first", 3)
        elif path.exists() and any(path.iterdir()):
            raise NativeStoreRefusal(f"{member['name']} path {path} is not an empty checkout placeholder", 3)
        planned.append((member, path, exists))

    include = _store_git(root, "show", f"{sha}:.gitinclude", check=False)
    generated_ignore = _compile_workspace_gitignore(include.stdout) if include.returncode == 0 else None
    if generated_ignore is not None:
        _validate_workspace_ignore_paths(root)
        declaration_entry = _store_git(root, "ls-tree", sha, "--", ".gitinclude").stdout
        if not declaration_entry.startswith(("100644 blob ", "100755 blob ")):
            raise NativeStoreRefusal("selected .gitinclude must be a regular blob", 4)
        if _store_git(root, "ls-tree", sha, "--", ".gitignore").stdout:
            raise NativeStoreRefusal("selected commit tracks generated .gitignore; resolve the declaration conflict before checkout", 4)
    # Member planning precedes root movement. Later materialization failures may
    # leave a partially applied selected C, so preserve every existing directory.
    _store_git(root, "checkout", "--detach", sha)
    if generated_ignore is not None:
        try:
            _publish_workspace_gitignore(root, generated_ignore)
        except Exception as primary:
            primary.gr2_workspace_part_applied = sha
            try:
                primary.add_note(f"THIS WORKSPACE IS PART-APPLIED -- ignore generation failed; root HEAD: {sha}")
            except Exception:
                pass
            raise
    restored: list[dict[str, str]] = []
    moved: list[str] = []
    for member, path, exists in planned:
        name, pin = member["name"], member["pin"]
        stage = "member checkout"
        try:
            if not exists:
                from .clone_exec import materialize_lane_clone

                cache = grip_dir(root) / "cache" / "repos" / f"{name}.git"
                stage = "cache seeding"
                gitops.ensure_repo_cache(member["remote"], cache)
                # Keep the existing cache and ordinary reference-clone owners.
                stage = "clone materialization"
                if path.exists():
                    path.rmdir()  # planning proved this is an empty placeholder
                materialize_lane_clone(source_repo_root=cache, dest=path,
                    branch=member.get("ref", "main"), seed_commit=pin,
                    workspace_root=root, cache_root=cache)
            stage = "member checkout"
            step = _store_git(path, "checkout", "--detach", pin, check=False)
            if step.returncode:
                raise NativeStoreRefusal(
                    f"{name} could not be checked out at {pin[:12]}; git says: {_git_detail(step)}", 5,
                )
            moved.append(name)
            stage = "member HEAD read"
            restored.append({"name": name, "pin": pin, "head": _store_git(path, "rev-parse", "HEAD").stdout.strip()})
        except Exception as primary:
            primary.gr2_workspace_part_applied = sha
            remaining = [m["name"] for m, _, _ in planned if m["name"] not in moved]
            detail = (
                f"THIS WORKSPACE IS PART-APPLIED -- {name}: {stage} failed; "
                f"moved: {moved or 'nothing'}; did not move: {remaining}; "
                f"root HEAD: {sha}"
            )
            # Reporting is secondary. Preserve the failure and usable partial
            # materialization even if stderr or exception annotation fails.
            try:
                primary.add_note(detail)
            except Exception:
                pass
            try:
                typer.echo(detail, err=True)
            except Exception:
                pass
            raise
    # THE RESOLVED SHA IS RETURNED, not the string the caller typed.
    # `--json` reported `root_commit: "HEAD~1"`, which is not a commit: it names
    # one only relative to a HEAD the call itself just moved, so a caller could not tell
    # which commit it got, and the value is meaningless to any later read.
    return sha, restored


@_refuses_moved_workspace
def _native_store_status(root: Path) -> tuple[list[dict[str, str | None]], dict[str, object]]:
    """Render every member's working state without stopping at the first bad row.

    Each row carries the COMMITTED gitlink beside the pin, and the command exits 4 when any
    row shows the two disagreeing -- both values are in that member's row, so a caller can
    name them without a second read. The disagreement is read from HEAD's tree, not the
    working index: the root snapshot is what a clone materializes from, so an index-only
    edit is not a snapshot defect (measured 2026-09-28).
    """
    members = _native_members(root)
    # A ROOT WITH NO COMMIT YET IS A NAMED STATE, not a git error (now
    # shared with check and log through _require_root_commit).
    _require_root_commit(root)
    rows: list[dict[str, str | None]] = []
    for member in members:
        claimed = _claimed_member_paths(root, members, exclude=member)
        # THE WORKING ROOT: the declared path unless nothing is there and a checkout sits
        # at the member's NAME. The `ls-tree` below keeps the DECLARED path -- that is the
        # gitlink key in the root snapshot, and it must not move with where the working
        # copy happens to be.
        path = _native_member_working_root(root, member, claimed)
        tree = _store_git(root, "ls-tree", "HEAD", "--", member["path"]).stdout.strip().split()
        gitlink = tree[2] if len(tree) >= 3 else None
        top = _store_git(path, "rev-parse", "--show-toplevel", check=False)
        is_checkout = top.returncode == 0 and Path(top.stdout.strip()).resolve() == path.resolve()
        if not is_checkout:
            rows.append({"name": member["name"], "pin": member["pin"], "gitlink": gitlink, "head": None, "state": "cannot-measure"})
            continue
        head = _store_git(path, "rev-parse", "HEAD", check=False)
        if head.returncode:
            rows.append({"name": member["name"], "pin": member["pin"], "gitlink": gitlink, "head": None, "state": "cannot-measure"})
            continue
        head_sha = head.stdout.strip()
        remote, separator, branch = member["upstream"].partition("/")
        fetched = _store_git(path, "fetch", remote, check=False) if separator else None
        if not separator or not remote or not branch or fetched is None or fetched.returncode:
            state = "cannot-measure"
        elif _store_git(path, "merge-base", "--is-ancestor", member["pin"], member["upstream"], check=False).returncode:
            state = "missing"
        elif head_sha != member["pin"]:
            state = "unpinned"
        elif _store_git(path, "rev-parse", member["upstream"], check=False).stdout.strip() != member["pin"]:
            state = "stale"
        else:
            state = "upstream"
        rows.append({"name": member["name"], "pin": member["pin"], "gitlink": gitlink, "head": head_sha, "state": state})
    porcelain = _store_git(root, "status", "--porcelain").stdout.splitlines()
    return rows, {"state": "dirty" if porcelain else "clean", "porcelain": porcelain}


@_refuses_moved_workspace
def _native_store_materialize(root: Path) -> list[dict[str, str]]:
    materialized: list[dict[str, str]] = []
    members = _native_members(root)
    for member in members:
        if _url_has_credentials(member["remote"]):
            raise _credential_refusal(member["name"])
        # THE WORKING ROOT, resolved the way the other four verbs resolve it -- but NOT
        # through `_member_working_root_or_refuse`, which refuses a root with no `.git`.
        # Refusing is right for the four verbs that MOVE or READ an existing checkout; it
        # is exactly wrong here, because "no checkout" is the state this verb exists to
        # fix. So the resolver's answer is used and the existing `is_checkout` test decides
        # between ADOPTING what is there and cloning.
        #
        # WITHOUT THIS, a member placed at its NAME coordinates as `is_checkout` false --
        # the test looked at `root / member["path"]`, which holds nothing -- and the verb
        # CLONED A SECOND WORKING COPY at the declared path while the member's real
        # checkout sat one directory over, untouched. `Materialized N` at rc 0, and nothing
        # in the message to say which of the two copies a later `commit` would pin.
        claimed = _claimed_member_paths(root, members, exclude=member)
        path = _native_member_working_root(root, member, claimed)
        top = _store_git(path, "rev-parse", "--show-toplevel", check=False)
        is_checkout = top.returncode == 0 and Path(top.stdout.strip()).resolve() == path.resolve()
        if not is_checkout:
            if path.exists():
                try:
                    path.rmdir()
                except OSError as exc:
                    raise NativeStoreRefusal(
                        f"{member['name']} path {path} is not an empty checkout placeholder; "
                        f"materialize will not clear it -- move or remove what is there",
                        3,
                    ) from exc
            result = subprocess.run(["git", "clone", member["remote"], str(path)], text=True, capture_output=True, check=False)
            if result.returncode:
                done = ", ".join(item["name"] for item in materialized) or "none"
                raise NativeStoreRefusal(
                    f"{member['name']} pin {member['pin']} cannot be served by origin; done: {done}", 3
                )
        checkout = _store_git(path, "checkout", "--detach", member["pin"], check=False)
        if checkout.returncode:
            done = ", ".join(item["name"] for item in materialized) or "none"
            raise NativeStoreRefusal(
                f"{member['name']} pin {member['pin']} cannot be served by origin; done: {done}", 3
            )
        materialized.append(
            {"name": member["name"], "pin": member["pin"], "head": _store_git(path, "rev-parse", "HEAD").stdout.strip()}
        )
    return materialized


@_refuses_moved_workspace
def _native_store_migrate(root: Path, *, dry_run: bool = False) -> dict[str, object]:
    """Move the alpha ``.grip/.git`` HEAD into one native root commit.

    The alpha history is deliberately retained, not replayed: a migration is a
    boundary between two store formats, and a copied history would claim that
    its old tree objects were native workspace commits.  Read the alpha HEAD
    before creating the root repository, then rename that store only after the
    native commit is durable.
    """
    legacy = root / ".grip" / ".git"
    if not legacy.is_dir():
        raise NativeStoreRefusal("no alpha .grip/.git store to migrate", 4)
    if (root / ".git").exists() or (root / "grip.toml").exists():
        raise NativeStoreRefusal("native store already exists; refusing to overwrite it", 4)

    alpha_head = _store_git(legacy.parent, "rev-parse", "HEAD", check=False)
    if alpha_head.returncode:
        raise NativeStoreRefusal("alpha store has no HEAD snapshot", 4)
    alpha_sha = alpha_head.stdout.strip()
    states = grip_mod._read_repo_state(root, alpha_sha)
    if not states:
        raise NativeStoreRefusal("alpha HEAD has no member pins", 4)

    planned: dict[str, str] = {}
    for name, state in sorted(states.items()):
        pin = state.get("commit", "")
        path = root / name
        if not isinstance(pin, str) or len(pin) != 40:
            raise NativeStoreRefusal(f"{name} alpha snapshot has no valid pin", 4)
        if repo_dirty(path):
            raise NativeStoreRefusal(f"{name} is dirty; commit or stash changes first", 3)
        remote = _store_git(path, "remote", "get-url", "origin", check=False)
        if remote.returncode:
            raise NativeStoreRefusal(f"{name} has no origin remote", 4)
        if _url_has_credentials(remote.stdout.strip()):
            raise _credential_refusal(name)
        fetched = _store_git(path, "fetch", "origin", check=False)
        if fetched.returncode:
            raise NativeStoreRefusal(f"{name} cannot fetch origin", 5)
        # The branch the member is ON, read by the same helper init records it with, so the pin
        # is checked against the upstream init would write (`origin/core/main`, not a literal).
        _ref, upstream = _infer_member_branch(path)
        if _store_git(path, "merge-base", "--is-ancestor", pin, upstream, check=False).returncode:
            raise NativeStoreRefusal(f"{name} pin {pin} is not on {upstream}; push it first", 3)
        planned[name] = pin

    if dry_run:
        return {"status": "dry-run", "alpha_head": alpha_sha, "members": planned}

    _native_store_init(root)
    members = _native_members(root)
    by_name = {member["name"]: member for member in members}
    missing = sorted(set(planned) - set(by_name))
    if missing:
        raise NativeStoreRefusal(f"alpha member(s) not found at root: {', '.join(missing)}", 4)
    migrated = [{**member, "pin": planned[member["name"]]} for member in members if member["name"] in planned]
    _write_native_members(root, migrated)
    _store_git(root, "add", "grip.toml")
    for member in migrated:
        _store_git(root, "update-index", "--add", "--cacheinfo", f"160000,{member['pin']},{member['path']}")
    _store_git(root, "-c", "user.name=gr2", "-c", "user.email=gr2@example.invalid", "commit", "-m", f"grip: migrated from alpha store {alpha_sha}")
    shutil.move(str(legacy), str(root / ".grip" / "legacy-store.git"))
    return {
        "status": "migrated",
        "alpha_head": alpha_sha,
        "root_commit": _store_git(root, "rev-parse", "HEAD").stdout.strip(),
        "members": planned,
    }


# THE JSON SHAPES TABLE -- design section 7: "JSON keys come from a
# ``JSON_SHAPES`` table beside each command's renderer". These are the keys the
# store renderers below actually emit, read off each `json.dumps` site.
#
# ONE ENTRY PER KEY PATH, in payload order. A path is dotted from the payload
# root, and `[]` marks an array whose elements share the path's tail --
# `.members[].state` is the `state` key of every element of `members`.
#
# AN ANNOTATION AFTER TWO SPACES CLOSES THE VALUE DOMAIN where this module
# closes it: `enum(...)` for a set of literals chosen here, `map<str,str>` for
# an object keyed by member name. A path with NO annotation is a value whose
# domain is open (a sha, a path, a commit message); no annotation is an honest
# "not a closed set", never an oversight -- so a reader can tell "open" from
# "nobody looked".
#
# THE MARKER IS NOT DUPLICATED HERE. A json item's marker is its VERB's marker
# (dump_api._json_marker), because a promise about a verb's keys cannot be
# stronger than the promise about the verb: while `store status` is may-change,
# so are the keys it emits. An ALIAS MOUNT resolves to its canonical twin for
# that lookup (dump_api._canonical_verb), so the same key path cannot publish two
# different markers depending on which mount a caller reads it through.
#
# ⚠ `store migrate`'s `.members` IS AN OBJECT (name -> pin), not the list the
# other members are -- hence its `map<str,str>` annotation. That is the product's
# own inconsistency, reported rather than smoothed over; the dump says what the
# payload is.
#
# KEYED BY THE VERB UNDER THE GROUP, because a payload does not depend on the
# mount: `app.add_typer(grip_app, name="store")` and
# `app.add_typer(grip_app, name="grip", hidden=True)` are the same callbacks.
# `JSON_SHAPES` below expands this table to every mount, so the hidden alias
# family carries the same shapes instead of a gap a reader has to explain.
_STORE_JSON_SHAPES: dict[str, tuple[str, ...]] = {
    "init": (
        ".status  enum(initialized)",
        ".path",
        ".store  enum(native)",
    ),
    "commit": (
        ".status  enum(committed,unchanged)",
        ".root_commit",
    ),
    # The hidden alias of `store commit` (section 5), so it emits commit's shape.
    "snapshot": (
        ".status  enum(committed,unchanged)",
        ".root_commit",
    ),
    "check": (
        ".status  enum(checked)",
        ".members[].name",
        ".members[].pin",
        ".members[].upstream",
        ".members[].state  enum(upstream)",
    ),
    "push": (
        ".status  enum(pushed)",
        ".root_branch",
        ".root_commit",
        ".members[].name",
        ".members[].pin",
        ".members[].upstream",
        ".members[].state  enum(upstream)",
    ),
    "status": (
        ".status  enum(status)",
        ".members[].name",
        ".members[].pin",
        ".members[].gitlink",
        ".members[].head",
        ".members[].state  enum(upstream,stale,missing,unpinned,cannot-measure)",
        ".root.state  enum(clean,dirty)",
        ".root.porcelain",
    ),
    "materialize": (
        ".status  enum(materialized)",
        ".members[].name",
        ".members[].pin",
        ".members[].head",
    ),
    "migrate": (
        ".status  enum(dry-run,migrated)",
        ".alpha_head",
        ".root_commit",
        ".members  map<str,str>",
    ),
    "migrate-reviews": (
        ".receipt",
        ".rows[].old_id",
        ".rows[].new_id",
        ".rows[].ref",
        ".rows[].status  enum(created,present)",
    ),
    "log": (
        ".entries[].commit",
        ".entries[].message",
        ".entries[].pins[].name",
        ".entries[].pins[].before",
        ".entries[].pins[].after",
    ),
    "diff": (
        ".ref_a",
        ".ref_b",
        ".members[].name",
        ".members[].old_pin",
        ".members[].new_pin",
        ".members[].changed",
    ),
    "checkout": (
        ".status  enum(checked-out)",
        ".root_commit",
        ".members[].name",
        ".members[].pin",
        ".members[].head",
    ),
}

#: THE MOUNTS, NAMED ONCE. The FIRST is canonical; the rest are aliases of the
#: same callbacks (see the note above). Both the expansion below and the dump's
#: marker lookup read this tuple, so a second mount cannot be added to one and
#: missed by the other.
STORE_MOUNTS: tuple[str, ...] = ("store", "grip")

#: verb path -> key paths, for EVERY mount of this group (see the note above).
JSON_SHAPES: dict[str, tuple[str, ...]] = {
    f"{mount} {verb}": paths
    for mount in STORE_MOUNTS
    for verb, paths in _STORE_JSON_SHAPES.items()
}


@grip_app.command("init")
def grip_init_cmd(
    workspace_root: Path | None = typer.Argument(None),
    member_paths: list[str] | None = typer.Option(
        None,
        "--member",
        help=(
            "Declare a member checkout by its path relative to the root (repeatable). Named "
            "paths win over the root's own spec and over sibling discovery; a named path that "
            "is not a checkout refuses the verb."
        ),
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Initialize a native store at cwd or the supplied root."""
    root = Path.cwd() if workspace_root is None else workspace_root.resolve()
    try:
        _native_store_init(root, member_paths=member_paths)
    except NativeStoreRefusal as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=exc.code)
    except RuntimeError as exc:
        # Section 5's exit table has no 1: 0 ok, 2 usage, 3 coverage-or-cleanliness,
        # 4 inconsistent-or-beta, 5 cannot measure. A residual RuntimeError is a verb that
        # could not COMPLETE, which is the 5 row, and it is prefixed so a raw git message
        # cannot read as a contract refusal (an unaudited backstop is the one that will
        # fire).
        typer.echo(f"{STORE_INCOMPLETE_PREFIX}{exc}", err=True)
        raise typer.Exit(code=5)
    if json_output:
        typer.echo(json.dumps({"status": "initialized", "path": str(root), "store": "native"}))
    else:
        typer.echo(f"Initialized git-native store at {root}")
    return


@grip_app.command("commit")
def grip_commit_cmd(
    message: str = typer.Option(..., "--message", "-m"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Record origin-covered member pins in the root git tree."""
    try:
        committed = _native_store_commit(Path.cwd(), message)
    except NativeStoreRefusal as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=exc.code)
    except RuntimeError as exc:
        # Section 5's exit table has no 1: 0 ok, 2 usage, 3 coverage-or-cleanliness,
        # 4 inconsistent-or-beta, 5 cannot measure. A residual RuntimeError is a verb that
        # could not COMPLETE, which is the 5 row, and it is prefixed so a raw git message
        # cannot read as a contract refusal (an unaudited backstop is the one that will
        # fire).
        typer.echo(f"{STORE_INCOMPLETE_PREFIX}{exc}", err=True)
        raise typer.Exit(code=5)
    if json_output:
        root_commit = _store_git(Path.cwd(), "rev-parse", "HEAD").stdout.strip()
        typer.echo(json.dumps({"status": "committed" if committed else "unchanged", "root_commit": root_commit}))
    elif not committed:
        typer.echo(STORE_COMMIT_UNCHANGED)


@grip_app.command("check")
def grip_check_cmd(
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Verify root gitlinks and member coverage against live upstreams."""
    try:
        members = _native_store_check(Path.cwd())
    except NativeStoreRefusal as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=exc.code)
    except RuntimeError as exc:
        # Section 5's exit table has no 1: 0 ok, 2 usage, 3 coverage-or-cleanliness,
        # 4 inconsistent-or-beta, 5 cannot measure. A residual RuntimeError is a verb that
        # could not COMPLETE, which is the 5 row, and it is prefixed so a raw git message
        # cannot read as a contract refusal (an unaudited backstop is the one that will
        # fire).
        typer.echo(f"{STORE_INCOMPLETE_PREFIX}{exc}", err=True)
        raise typer.Exit(code=5)
    if json_output:
        typer.echo(json.dumps({"status": "checked", "members": members}))
    else:
        typer.echo(f"Checked {len(members)} member(s)")


@grip_app.command("push")
def grip_push_cmd(
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Check then push only the root branch, never member branches."""
    try:
        payload = _native_store_push(Path.cwd())
    except NativeStoreRefusal as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=exc.code)
    except RuntimeError as exc:
        # Section 5's exit table has no 1: 0 ok, 2 usage, 3 coverage-or-cleanliness,
        # 4 inconsistent-or-beta, 5 cannot measure. A residual RuntimeError is a verb that
        # could not COMPLETE, which is the 5 row, and it is prefixed so a raw git message
        # cannot read as a contract refusal (an unaudited backstop is the one that will
        # fire).
        typer.echo(f"{STORE_INCOMPLETE_PREFIX}{exc}", err=True)
        raise typer.Exit(code=5)
    if json_output:
        typer.echo(json.dumps(payload))
    else:
        typer.echo(f"Pushed root {payload['root_branch']} at {payload['root_commit']}")


@grip_app.command("status")
def grip_status_cmd(
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Show every member's pin and working HEAD."""
    try:
        members, root_status = _native_store_status(Path.cwd())
    except NativeStoreRefusal as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=exc.code)
    except RuntimeError as exc:
        # Section 5's exit table has no 1: 0 ok, 2 usage, 3 coverage-or-cleanliness,
        # 4 inconsistent-or-beta, 5 cannot measure. A residual RuntimeError is a verb that
        # could not COMPLETE, which is the 5 row, and it is prefixed so a raw git message
        # cannot read as a contract refusal (an unaudited backstop is the one that will
        # fire).
        typer.echo(f"{STORE_INCOMPLETE_PREFIX}{exc}", err=True)
        raise typer.Exit(code=5)
    if json_output:
        typer.echo(json.dumps({"status": "status", "members": members, "root": root_status}))
    else:
        for member in members:
            typer.echo(f"{member['name']} {member['state']} pin={member['pin']} head={member['head']}")
    # THE TABLE PRINTS IN EVERY CASE, and the code follows it. A diagnostic that exits 0 on
    # an inconsistency is the silent-success class: the root snapshot a clone would
    # materialize disagrees with its own pin, and a caller that only reads the exit code
    # would take that snapshot as good. 4 outranks 5, and neither outranks the table.
    disagreed = [member for member in members if member["gitlink"] != member["pin"]]
    if disagreed:
        for member in disagreed:
            typer.echo(
                f"{member['name']} gitlink {member['gitlink'] or '<missing>'} disagrees with pin {member['pin']}",
                err=True,
            )
        raise typer.Exit(code=4)
    if any(member["state"] == "cannot-measure" for member in members):
        raise typer.Exit(code=5)


@grip_app.command("materialize")
def grip_materialize_cmd(
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Materialize each canonical pin from its declared origin."""
    try:
        members = _native_store_materialize(Path.cwd())
    except NativeStoreRefusal as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=exc.code)
    except RuntimeError as exc:
        # Section 5's exit table has no 1: 0 ok, 2 usage, 3 coverage-or-cleanliness,
        # 4 inconsistent-or-beta, 5 cannot measure. A residual RuntimeError is a verb that
        # could not COMPLETE, which is the 5 row, and it is prefixed so a raw git message
        # cannot read as a contract refusal (an unaudited backstop is the one that will
        # fire).
        typer.echo(f"{STORE_INCOMPLETE_PREFIX}{exc}", err=True)
        raise typer.Exit(code=5)
    if json_output:
        typer.echo(json.dumps({"status": "materialized", "members": members}))
    else:
        typer.echo(f"Materialized {len(members)} member(s)")


@grip_app.command("migrate")
def grip_migrate_cmd(
    dry_run: bool = typer.Option(False, "--dry-run", help="Print the alpha-to-native plan without writing"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Convert alpha .grip/.git HEAD into one native root commit."""
    try:
        payload = _native_store_migrate(Path.cwd(), dry_run=dry_run)
    except NativeStoreRefusal as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=exc.code)
    except RuntimeError as exc:
        # Section 5's exit table has no 1: 0 ok, 2 usage, 3 coverage-or-cleanliness,
        # 4 inconsistent-or-beta, 5 cannot measure. A residual RuntimeError is a verb that
        # could not COMPLETE, which is the 5 row, and it is prefixed so a raw git message
        # cannot read as a contract refusal (an unaudited backstop is the one that will
        # fire).
        typer.echo(f"{STORE_INCOMPLETE_PREFIX}{exc}", err=True)
        raise typer.Exit(code=5)
    if json_output:
        typer.echo(json.dumps(payload))
    elif dry_run:
        typer.echo(f"Would migrate alpha {payload['alpha_head']} with {len(payload['members'])} member(s)")
    else:
        typer.echo(f"Migrated alpha {payload['alpha_head']} to root {payload['root_commit']}")


@grip_app.command("migrate-reviews")
def grip_migrate_reviews_cmd(
    root: Path = typer.Argument(..., help="The native store root whose review binds to migrate"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Give every legacy review bind a field tree twin at the v1 ref; legacy refs stay as they are."""
    try:
        receipt, rows = grip_mod.migrate_review_binds(root.resolve())
    except grip_mod.GripCorruptError as exc:
        typer.echo(f"refused: {exc}", err=True)
        raise typer.Exit(code=4)
    if json_output:
        typer.echo(json.dumps({"receipt": str(receipt), "rows": rows}))
    else:
        for row in rows:
            typer.echo(f"{row['status']}: {row['old_id']} -> {row['new_id']}")
        typer.echo(f"receipt: {receipt}")


@grip_app.command("snapshot", hidden=True)
def grip_snapshot_cmd(
    message: str = typer.Option(..., "--message", "-m", help="Snapshot message"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Hidden alias of `store commit` (design section 5), removed at beta.

    ⚠ THIS IS A BEHAVIOUR CHANGE, NOT A RENAME (ported 2026-09-28). The verb used to be a
    separate command with four flags commit does not have (--repos, --type, --sprint,
    --overlay-dir), an optional message where commit requires one, and a DIFFERENT
    IMPLEMENTATION: it wrote the alpha `.grip` store through grip_mod.grip_init /
    grip_snapshot, which is the format section 6's `store migrate` exists to convert away
    from. So it was a live writer of the retired format, and an alias that acquires its own
    semantics is how a `may-change` row becomes permanent by accident.

    Section 5 line 100 makes it an alias of `store commit`, so it now makes the native root
    commit and carries only commit's flags. This UNWIRES the verb; it does not delete the
    alpha writer, because `store migrate` (section 6a) reads that store and is a later step.
    """
    try:
        committed = _native_store_commit(Path.cwd(), message)
    except NativeStoreRefusal as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=exc.code)
    except RuntimeError as exc:
        typer.echo(f"{STORE_INCOMPLETE_PREFIX}{exc}", err=True)
        raise typer.Exit(code=5)
    root_commit = _store_git(Path.cwd(), "rev-parse", "HEAD").stdout.strip()
    if json_output:
        typer.echo(json.dumps({"status": "committed" if committed else "unchanged", "root_commit": root_commit}))
    elif not committed:
        typer.echo(STORE_COMMIT_UNCHANGED)
    else:
        typer.echo(f"grip snapshot {root_commit[:12]}")


@grip_app.command("log")
def grip_log_cmd(
    max_count: int = typer.Option(10, "--max-count", "-n", help="Max entries to show"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """The root's commit history, with the pins each commit recorded (design section 5).

    Section 5 line 99 puts `log` in the same row as diff and checkout -- "`log` is the root's
    `git log` with pins per commit" -- so it acts on the cwd like every other verb in the
    group, and its entries are native: a root `commit`, its `message`, and the `pins` that
    changed.

    ⚠ IT CARRIED A LIVE ALPHA READER UNTIL 2026-09-28.
    A positional `workspace_root` sent the verb to `_read_snapshot_index`
    (`.grip/snapshots/index.json`) and printed rows keyed `id`, the alpha shape, while the
    no-argument branch was already native. `diff` had been ported off that same index in the
    SAME RANGE, so this was the same defect surviving in the one verb the port did not reach: a
    verb with two branches that answer different questions presents as ported whenever a
    caller happens to use the branch that was ported.
    """
    try:
        entries = _native_store_log(Path.cwd(), max_count)
    except NativeStoreRefusal as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=exc.code)
    except RuntimeError as exc:
        typer.echo(f"{STORE_INCOMPLETE_PREFIX}{exc}", err=True)
        raise typer.Exit(code=5)
    if json_output:
        typer.echo(json.dumps({"entries": entries}))
        return
    for entry in entries:
        typer.echo(f"{entry['commit']} {entry['message']}")


@grip_app.command("diff")
def grip_diff_cmd(
    ref_a: str = typer.Argument(None, help="First root commit (default: HEAD~1)"),
    ref_b: str = typer.Argument(None, help="Second root commit (default: HEAD)"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Pin changes between two root commits (design section 5).

    Section 5 line 99: "`diff` is pin changes between two root commits", and the row's
    stability is "as `materialize`". It acts on the cwd, like every other verb in the group:
    the old signature took a positional `workspace_root` and read the alpha snapshot index,
    so calling it the way the row describes failed on a missing argument before reaching any
    of this (measured 2026-09-28: `store diff HEAD~1 HEAD --json` on the base exited 2 with
    "Missing argument 'ref_b'").
    """
    root = Path.cwd()
    a = ref_a or "HEAD~1"
    b = ref_b or "HEAD"
    try:
        members = _native_store_diff(root, a, b)
    except NativeStoreRefusal as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=exc.code)
    except RuntimeError as exc:
        typer.echo(f"{STORE_INCOMPLETE_PREFIX}{exc}", err=True)
        raise typer.Exit(code=5)
    if json_output:
        typer.echo(json.dumps({"ref_a": a, "ref_b": b, "members": members}))
    else:
        for member in members:
            marker = "*" if member["changed"] else " "
            typer.echo(f"{marker} {member['name']} {member['old_pin']} -> {member['new_pin']}")


@grip_app.command("checkout")
def grip_checkout_cmd(
    ref: str = typer.Argument(..., help="Root commit to materialize the members at"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Materialize the members at a root commit (design section 5).

    Section 5 line 99: "`checkout` is `materialize` at a commit", and its stability is "as
    `materialize`" -- so it INHERITS that verb's guarantees (a member whose origin cannot
    serve the pin refuses at 3 naming the member and the pin; earlier members report as done
    rather than leaving a silent partial tree) and defines no second materialization path. It
    delegates to the same helper materialize uses for exactly that reason.
    """
    root = Path.cwd()
    try:
        resolved, members = _native_store_checkout(root, ref)
    except NativeStoreRefusal as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=exc.code)
    except RuntimeError as exc:
        typer.echo(f"{STORE_INCOMPLETE_PREFIX}{exc}", err=True)
        raise typer.Exit(code=5)
    if json_output:
        typer.echo(json.dumps({"status": "checked-out", "root_commit": resolved, "members": members}))
    else:
        for member in members:
            typer.echo(f"{member['name']} -> {member['pin']}")


# ---------------------------------------------------------------------------
# gr config
# ---------------------------------------------------------------------------


@config_cli_app.command("apply")
def config_apply_cmd(
    base_path: Path = typer.Argument(..., help="Path to base TOML config file"),
    overlay_dir: str = typer.Option(
        "",
        "--overlay-dir",
        help="Overlay directory (default: sibling overlay/)",
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Materialize TOML base into JSON runtime overlay."""
    base_path = base_path.resolve()
    if not base_path.is_file():
        typer.echo(f"base config file not found: {base_path}", err=True)
        raise typer.Exit(code=1)
    overlay = Path(overlay_dir).resolve() if overlay_dir else base_path.parent / "overlay"
    result = config_mod.config_apply(base_path, overlay)
    if json_output:
        typer.echo(json.dumps(result))
    else:
        typer.echo(f"Applied {base_path.name} -> {overlay / (base_path.stem + '.json')}")


@config_cli_app.command("show")
def config_show_cmd(
    base_path: Path = typer.Argument(..., help="Path to base TOML config file"),
    overlay_dir: str = typer.Option("", "--overlay-dir", help="Overlay directory"),
    key: str = typer.Option("", "--key", "-k", help="Dotted key path (e.g. agents.opus.model)"),
    strict: bool = typer.Option(False, "--strict", help="Fail if overlay is stale"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Show merged config (overlay-first, base-fallback)."""
    base_path = base_path.resolve()
    if not base_path.is_file():
        typer.echo(f"base config file not found: {base_path}", err=True)
        raise typer.Exit(code=1)
    overlay = Path(overlay_dir).resolve() if overlay_dir else base_path.parent / "overlay"
    try:
        result = config_mod.config_show(
            base_path,
            overlay,
            key=key or None,
            strict=strict,
        )
    except config_mod.BaseStaleError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1)
    if json_output:
        if key:
            typer.echo(json.dumps({"key": key, "value": result}))
        else:
            typer.echo(json.dumps(result))
    else:
        typer.echo(json.dumps(result, indent=2))


@config_cli_app.command("restore", cls=RootOptionalCommand)
def config_restore_cmd(
    workspace_root: Path,
    ref: str = typer.Argument(..., help="Grip commit ref to restore config from"),
    overlay_dir: str = typer.Option("", "--overlay-dir", help="Overlay directory to restore into"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
    root: Optional[Path] = ROOT_OPTION,
) -> None:
    """Restore config overlay from a grip commit snapshot."""
    workspace_root = workspace_root.resolve()
    overlay = Path(overlay_dir).resolve() if overlay_dir else workspace_root / "config" / "overlay"
    result = config_mod.config_restore(workspace_root, ref, overlay)
    if json_output:
        typer.echo(json.dumps({"restored": result}))
    else:
        typer.echo(f"Restored {len(result)} file(s) from {ref[:12]}")
