"""CLI commands for grip object model and config overlay.

Separate module so tests can import without pulling in all of app.py's
dependencies (gr2.prototypes, lane_workspace_prototype, etc.).
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tomllib
from pathlib import Path
from urllib.parse import urlsplit

import typer

from . import config as config_mod
from . import gitops
from . import grip as grip_mod
from .gitops import git, repo_dirty
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
            candidate = unit_member_path(workspace_root, unit, name)
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
# replaces them. Atlas's r2 BLOCK (m_ce3627a7) made this group dead by removing log's alpha
# branch. `tests/test_grip_object_model.py` used to import three of them and is now RETIRED,
# so it is no longer a reason to keep anything -- corrected here because the first version of
# this comment named it as one (Apollo, v2 r1).
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


def _native_members(root: Path) -> list[dict[str, str]]:
    data = _grip_document(root)
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
    """Refuse URL userinfo except an SSH login, never by printing the URL."""
    head = url.split("://", 1)[0]
    address = url.split("::", 1)[1] if "::" in head else url
    parsed = urlsplit(address)
    if parsed.password is not None:
        return True
    return parsed.username is not None and parsed.scheme.lower() not in {"ssh", "git+ssh", "ssh+git"}


def _credential_refusal(name: str) -> NativeStoreRefusal:
    return NativeStoreRefusal(
        f"{name} origin contains credentials; remove URL userinfo and use a credential helper", 4
    )


def _discover_members(root: Path) -> list[dict[str, str]]:
    """The sibling clones `store init` derives its members from, gr1 layout."""
    members: list[dict[str, str]] = []
    for path in sorted(root.iterdir()):
        if not path.is_dir() or path.name.startswith("."):
            continue
        top = _store_git(path, "rev-parse", "--show-toplevel", check=False)
        if top.returncode or Path(top.stdout.strip()).resolve() != path.resolve():
            continue
        remote = _store_git(path, "remote", "get-url", "origin", check=False)
        if remote.returncode:
            raise NativeStoreRefusal(
                f"{path.name} has no origin remote; store init cannot pin a member it cannot fetch", 4
            )
        url = remote.stdout.strip()
        if _url_has_credentials(url):
            raise _credential_refusal(path.name)
        members.append({"name": path.name, "path": path.name, "remote": url, "upstream": "origin/main", "ref": "main", "pin": _store_git(path, "rev-parse", "HEAD").stdout.strip()})
    return members


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


# Written as the first line of an allow-list THIS VERB generates, so `store commit` can tell
# a gr2-generated `.gitignore` from an adopted root's own file without guessing by content.
# `store init` never edits an adopted root's `.gitignore` (section 3a), and commit must not
# stage a file the owner wrote.
STORE_INCOMPLETE_PREFIX = "cannot complete this store verb: "
"""Section 5's exit table has no room for an internal error.

For the whole `store` group the table is 0 ok, 2 usage, 3 refused on coverage or cleanliness,
4 refused as inconsistent or beta, 5 cannot measure. A verb that could not COMPLETE -- an
unexpected git failure, an unreadable root -- has no code of its own in that set, so it
reports 5 and prefixes its message with this string. THE PREFIX IS PART OF THE SURFACE, not a
style choice: it is what tells a measured "cannot measure" apart from a wrapped exception, and
a caller reading the code alone cannot. Named as a constant so the ten handlers share one
spelling, and named in the freeze post so the readers can hold the contract (Atlas, r2
m_ce3627a7).
"""

GITIGNORE_MARKER = "# gr2 store allow-list (written by store init; an adopted root keeps its own)"


def _write_gitignore(root: Path, members: list[dict[str, str]]) -> None:
    """Section 3a's allow-list, written ONLY when `store init` creates the root repo.

    A gr1 root holds venvs, scratch and logs beside its members, so without this the root's
    `git status` is noise and someone eventually commits a venv (measured 2026-09-28: a
    fresh init left `?? alpha/` for the whole member checkout). An ADOPTED root is never
    touched -- the caller decides, not this function.

    The shape is `/*` (ignore everything), then the two files the root exists to carry, then
    one un-ignore per member. A nested member path also needs each ancestor directory
    un-ignored and its other contents re-ignored, or `/*` swallows the way down:

        !/reference/
        /reference/*
        !/reference/mem0
    """
    lines = [GITIGNORE_MARKER, "/*", "!/.gitignore", "!/grip.toml"]
    for member in members:
        parts = [part for part in member["path"].split("/") if part]
        for depth in range(1, len(parts)):
            prefix = "/" + "/".join(parts[:depth]) + "/"
            lines.append(f"!{prefix}")
            lines.append(f"{prefix}*")
        lines.append("!/" + "/".join(parts))
    root.joinpath(".gitignore").write_text("\n".join(lines) + "\n")


def _native_store_init(root: Path) -> None:
    if (root / ".git").exists() and (root / "grip.toml").exists():
        _refuse_init_disagreement(root)
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
    members = _discover_members(root)
    if not members:
        raise NativeStoreRefusal("no sibling git repositories found to store", 4)
    # Section 3a: the allow-list is written ONLY when init CREATES the root repo. An adopted
    # root keeps whatever rule its owner wrote (break_12 asserts byte-identical).
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

    LIFTED OUT OF `status` AFTER APOLLO'S v2 r1 BLOCK (finding B's class), which found the
    same unborn-HEAD read in three more verbs: `check` ran `ls-tree HEAD`, `log` ran
    `rev-list HEAD`, and `push` inherits it from check. Each surfaced git's own "fatal: invalid
    object name 'HEAD'" -- or "ambiguous argument" -- across the exit surface, which section
    5's table has no room for. One guard, three callers, so the three cannot drift apart the
    way commit and check did on section 5a.

    It is placed AFTER the document read at every call site, so a root that is not a store at
    all still gets its own message rather than this one.
    """
    if _store_git(root, "rev-parse", "--verify", "HEAD", check=False).returncode:
        raise NativeStoreRefusal("no root commit yet; run store commit", 5)


def _member_coverage(root: Path, member: dict[str, str], sha: str) -> None:
    """Section 5a, in ONE place, so two verbs cannot answer it with different refs.

    §5a: run `git fetch <upstream-remote>` and fail with exit 5 if it fails, then
    `git merge-base --is-ancestor <sha> <upstream>`; the check is against `upstream`, never
    against the literal name `origin`.

    THIS HELPER EXISTS BECAUSE OF A MEASURED DIVERGENCE (Atlas, pre-read m_d416e648 F1, read
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
    path = root / member["path"]
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


def _native_store_commit(root: Path, message: str) -> None:
    _refuse_beta(root)
    members = _native_members(root)
    _refuse_symlinked_members(root, members)
    changed: list[dict[str, str]] = []
    for member in members:
        if _url_has_credentials(member["remote"]):
            raise _credential_refusal(member["name"])
        path = root / member["path"]
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
        # coverage so the refusal names the thing the author has to fix first. Atlas's
        # pre-read F2: no verb in the group refused a dirty member, though the row names it.
        if _store_git(path, "status", "--porcelain").stdout.strip():
            raise NativeStoreRefusal(
                f"{member['name']} is dirty; commit or stash changes first", 3
            )
        head = _store_git(path, "rev-parse", "HEAD").stdout.strip()
        _member_coverage(root, member, head)
        changed.append({**member, "pin": head})
    _write_native_members(root, changed)
    _store_git(root, "add", "grip.toml")
    # The generated allow-list is TRACKED (section 3: ".gitignore | tracked, only when init
    # created the repo"). Staged only when it is ours, proved by the marker line.
    allow_list = root / ".gitignore"
    if allow_list.is_file() and allow_list.read_text().splitlines()[:1] == [GITIGNORE_MARKER]:
        _store_git(root, "add", ".gitignore")
    for member in changed:
        _store_git(root, "update-index", "--add", "--cacheinfo", f"160000,{member['pin']},{member['path']}")
    _store_git(root, "-c", "user.name=gr2", "-c", "user.email=gr2@example.invalid", "commit", "-m", message)


def _native_store_check(root: Path) -> list[dict[str, str]]:
    """Verify the committed root pins against each live member upstream."""
    _refuse_beta(root)
    issues = validate_grip_toml(root)
    if issues:
        raise NativeStoreRefusal(f"grip.toml schema invalid: {issues[0].message}", 4)
    _require_root_commit(root)
    checked: list[dict[str, str]] = []
    for member in _native_members(root):
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
        _member_coverage(root, member, member["pin"])
        checked.append({"name": member["name"], "pin": member["pin"], "upstream": member["upstream"], "state": "upstream"})
    return checked


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


def _native_store_log(root: Path, max_count: int) -> list[dict[str, object]]:
    # THE DOCUMENT IS READ FIRST, so a root that is not a store gets the NAMED refusal rather
    # than git's own "not a git repository" wrapped in an exit code. Found by rewriting the
    # gr1 guidance row after the log port: that row's workspace is not a git repo at all, and
    # `rev-list` was the first thing the verb touched. Same defect class as Apollo's r1
    # finding B on status, one verb over.
    _grip_document(root)
    _require_root_commit(root)
    commits = _store_git(root, "rev-list", "--reverse", f"--max-count={max_count}", "HEAD").stdout.splitlines()
    prior: dict[str, str] = {}
    entries: list[dict[str, object]] = []
    for commit in commits:
        current = _native_members_at(root, commit)
        changes = [{"name": name, "before": prior.get(name), "after": pin} for name, pin in current.items() if prior.get(name) != pin]
        entries.append({"commit": commit, "message": _store_git(root, "show", "-s", "--format=%s", commit).stdout.strip(), "pins": changes})
        prior = current
    return entries


def _native_store_diff(root: Path, ref_a: str, ref_b: str) -> list[dict[str, object]]:
    # Both refs are resolved FIRST and by name, so an unresolvable one is a NAMED refusal at
    # 5 rather than a wrapped git message from whichever read happened to run first. Same
    # judgement as checkout's unresolvable ref, and the same code for the same reason: the
    # verb could not MEASURE the thing the caller named (Apollo, r1 ruling).
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
    members = _native_members(root)
    for member in members:
        path = root / member["path"]
        if _store_git(path, "status", "--porcelain").stdout.strip():
            raise NativeStoreRefusal(f"{member['name']} is dirty; commit or stash changes first", 3)
    # Exit 5 rather than 2 for an unresolvable name: 2 is the argument-parser's code in this
    # group, and this is the verb failing to MEASURE the thing the caller named. Named here
    # because it is a judgement call rather than a row the design states -- the readers get
    # to attack it.
    target = _store_git(root, "rev-parse", f"{revision}^{{commit}}", check=False)
    if target.returncode:
        raise NativeStoreRefusal(
            f"{revision} is not a root commit this store can resolve; git says: {_git_detail(target)}", 5
        )
    sha = target.stdout.strip()
    _store_git(root, "checkout", "--detach", sha)
    pins = _native_members_at(root, sha)
    restored: list[dict[str, str]] = []
    for member in members:
        path = root / member["path"]
        pin = pins[member["name"]]
        _store_git(path, "checkout", "--detach", pin)
        restored.append({"name": member["name"], "pin": pin, "head": _store_git(path, "rev-parse", "HEAD").stdout.strip()})
    # THE RESOLVED SHA IS RETURNED, not the string the caller typed (Apollo, r1 m_227344fd
    # finding A). `--json` reported `root_commit: "HEAD~1"`, which is not a commit: it names
    # one only relative to a HEAD the call itself just moved, so a caller could not tell
    # which commit it got, and the value is meaningless to any later read.
    return sha, restored


def _native_store_status(root: Path) -> tuple[list[dict[str, str | None]], dict[str, object]]:
    """Render every member's working state without stopping at the first bad row.

    Each row carries the COMMITTED gitlink beside the pin, and the command exits 4 when any
    row shows the two disagreeing -- both values are in that member's row, so a caller can
    name them without a second read. The disagreement is read from HEAD's tree, not the
    working index: the root snapshot is what a clone materializes from, so an index-only
    edit is not a snapshot defect (measured 2026-09-28).
    """
    members = _native_members(root)
    # A ROOT WITH NO COMMIT YET IS A NAMED STATE, not a git error (Apollo, r1 m_227344fd
    # finding B, now shared with check and log through _require_root_commit).
    _require_root_commit(root)
    rows: list[dict[str, str | None]] = []
    for member in members:
        path = root / member["path"]
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


def _native_store_materialize(root: Path) -> list[dict[str, str]]:
    materialized: list[dict[str, str]] = []
    for member in _native_members(root):
        if _url_has_credentials(member["remote"]):
            raise _credential_refusal(member["name"])
        path = root / member["path"]
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
        if _store_git(path, "merge-base", "--is-ancestor", pin, "origin/main", check=False).returncode:
            raise NativeStoreRefusal(f"{name} pin {pin} is not on origin/main; push it first", 3)
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


@grip_app.command("init")
def grip_init_cmd(
    workspace_root: Path | None = typer.Argument(None),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Initialize a native store at cwd or the supplied root."""
    root = Path.cwd() if workspace_root is None else workspace_root.resolve()
    try:
        _native_store_init(root)
    except NativeStoreRefusal as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=exc.code)
    except RuntimeError as exc:
        # Section 5's exit table has no 1: 0 ok, 2 usage, 3 coverage-or-cleanliness,
        # 4 inconsistent-or-beta, 5 cannot measure. A residual RuntimeError is a verb that
        # could not COMPLETE, which is the 5 row, and it is prefixed so a raw git message
        # cannot read as a contract refusal (Atlas, pre-read F4: an unaudited backstop is
        # the one that will fire).
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
        _native_store_commit(Path.cwd(), message)
    except NativeStoreRefusal as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=exc.code)
    except RuntimeError as exc:
        # Section 5's exit table has no 1: 0 ok, 2 usage, 3 coverage-or-cleanliness,
        # 4 inconsistent-or-beta, 5 cannot measure. A residual RuntimeError is a verb that
        # could not COMPLETE, which is the 5 row, and it is prefixed so a raw git message
        # cannot read as a contract refusal (Atlas, pre-read F4: an unaudited backstop is
        # the one that will fire).
        typer.echo(f"{STORE_INCOMPLETE_PREFIX}{exc}", err=True)
        raise typer.Exit(code=5)
    if json_output:
        typer.echo(json.dumps({"status": "committed", "root_commit": _store_git(Path.cwd(), "rev-parse", "HEAD").stdout.strip()}))


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
        # cannot read as a contract refusal (Atlas, pre-read F4: an unaudited backstop is
        # the one that will fire).
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
        # cannot read as a contract refusal (Atlas, pre-read F4: an unaudited backstop is
        # the one that will fire).
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
        # cannot read as a contract refusal (Atlas, pre-read F4: an unaudited backstop is
        # the one that will fire).
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
        # cannot read as a contract refusal (Atlas, pre-read F4: an unaudited backstop is
        # the one that will fire).
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
        # cannot read as a contract refusal (Atlas, pre-read F4: an unaudited backstop is
        # the one that will fire).
        typer.echo(f"{STORE_INCOMPLETE_PREFIX}{exc}", err=True)
        raise typer.Exit(code=5)
    if json_output:
        typer.echo(json.dumps(payload))
    elif dry_run:
        typer.echo(f"Would migrate alpha {payload['alpha_head']} with {len(payload['members'])} member(s)")
    else:
        typer.echo(f"Migrated alpha {payload['alpha_head']} to root {payload['root_commit']}")


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
    semantics is how a `may-change` row becomes permanent by accident (Atlas, pre-read
    m_d416e648 F3).

    Section 5 line 100 makes it an alias of `store commit`, so it now makes the native root
    commit and carries only commit's flags. This UNWIRES the verb; it does not delete the
    alpha writer, because `store migrate` (section 6a) reads that store and is a later step.
    """
    try:
        _native_store_commit(Path.cwd(), message)
    except NativeStoreRefusal as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=exc.code)
    except RuntimeError as exc:
        typer.echo(f"{STORE_INCOMPLETE_PREFIX}{exc}", err=True)
        raise typer.Exit(code=5)
    root_commit = _store_git(Path.cwd(), "rev-parse", "HEAD").stdout.strip()
    if json_output:
        typer.echo(json.dumps({"status": "committed", "root_commit": root_commit}))
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

    ⚠ IT CARRIED A LIVE ALPHA READER UNTIL 2026-09-28, and Atlas's r2 BLOCK (m_ce3627a7)
    caught it. A positional `workspace_root` sent the verb to `_read_snapshot_index`
    (`.grip/snapshots/index.json`) and printed rows keyed `id`, the alpha shape, while the
    no-argument branch was already native. `diff` had been ported off that same index in the
    SAME RANGE, so this was F3's shape surviving in the one verb the port did not reach: a
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


@config_cli_app.command("restore")
def config_restore_cmd(
    workspace_root: Path,
    ref: str = typer.Argument(..., help="Grip commit ref to restore config from"),
    overlay_dir: str = typer.Option("", "--overlay-dir", help="Overlay directory to restore into"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Restore config overlay from a grip commit snapshot."""
    workspace_root = workspace_root.resolve()
    overlay = Path(overlay_dir).resolve() if overlay_dir else workspace_root / "config" / "overlay"
    result = config_mod.config_restore(workspace_root, ref, overlay)
    if json_output:
        typer.echo(json.dumps({"restored": result}))
    else:
        typer.echo(f"Restored {len(result)} file(s) from {ref[:12]}")
