"""the consent gate: member-hook consent records and projection confinement.

The property the gate guards: consent may NOT live
inside the object it consents to. A hooks table is authored by whoever wrote
the member repo; a consent section inside it arrives pre-consented and the
gate opens itself. Consent is a LOCAL record on the host:

    <workspace_root>/.grip/consent/<member-path>.json

    {"hooks_sha": <sha256 of the member's .gr2/hooks.toml>,
     "granted_by": <host user, never an agent identity>,
     "granted_at": <ISO-8601>}

keyed by the content hash of the member's hooks table; consent lapses when
the hook text changes, because the hash changes. Binding is verb-only
(`gr2 hooks trust`); the record file carries a .json suffix because nested
member paths would collide otherwise.

Confinement: consent answers whether this repo may act;
confinement answers where. Every projection dest RESOLVES (symlinks
followed): inside the workspace root (the root itself and {unit_root} are
designed surfaces), never under any .git component anywhere, never under
gr2's own state directory (.grip — a forged consent record there binds
another member, an overwritten workspace_spec.toml repoints every
member's url), never inside ANOTHER member's tree, and for
[[files.link]] the link's TARGET must resolve inside the member's own
tree. A projection SOURCE also resolves inside the member's own tree. A
violating row is REFUSED and REPORTED even when consent is GRANTED.
"""
from __future__ import annotations

import getpass
import hashlib
import unicodedata
import json
import tomllib
from datetime import datetime, timezone
from pathlib import Path


def hooks_file(repo_root: Path) -> Path:
    return repo_root / ".gr2" / "hooks.toml"


def hooks_sha(repo_root: Path) -> str:
    path = hooks_file(repo_root)
    if not path.exists():
        return ""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def consent_path(workspace_root: Path, member_key: str) -> Path:
    # The .json suffix is load-bearing: nested member paths would otherwise
    # collide with the parent's record.
    rel = member_key.strip("/")
    if not rel or rel in (".", ".."):
        raise ValueError(f"invalid member key: {member_key!r}")
    return workspace_root / ".grip" / "consent" / (rel + ".json")


def workspace_spec_path(workspace_root: Path) -> Path:
    return workspace_root / ".grip" / "workspace_spec.toml"


def member_key(workspace_root: Path, repo_root: Path, repo_name: str) -> str:
    """The consent key for a member: the spec's declared path for this repo
    NAME when the spec names one (stable across lane copies of the member);
    otherwise the workspace-relative path of repo_root."""
    spec = workspace_spec_path(workspace_root)
    if spec.exists():
        try:
            doc = tomllib.loads(spec.read_text())
        except tomllib.TOMLDecodeError:
            doc = {}
        for repo in doc.get("repos", []):
            if str(repo.get("name", "")) == repo_name and repo.get("path"):
                return str(repo["path"]).strip("/")
    try:
        return str(repo_root.resolve().relative_to(workspace_root.resolve()))
    except ValueError:
        return repo_name


def load_consent(workspace_root: Path, member_key: str) -> dict | None:
    path = consent_path(workspace_root, member_key)
    if not path.exists():
        return None
    return json.loads(path.read_text())


def consent_state(
    workspace_root: Path, member_key: str, repo_root: Path
) -> tuple[str, dict | None]:
    """("unbound" | "changed" | "bound", record). "changed" means a record
    exists whose hooks_sha no longer matches the member's hook text."""
    record = load_consent(workspace_root, member_key)
    if record is None:
        return ("unbound", None)
    if record.get("hooks_sha") != hooks_sha(repo_root):
        return ("changed", record)
    return ("bound", record)


def bind_ctx(ctx) -> dict:
    """Bind the member a HookContext points at, through the SAME key
    derivation the gate uses. This is the fixture-side twin of the gate:
    tests that exercise hook RUNTIME semantics (when/on_failure/projections)
    bind first; only the consent tests exercise the unbound path."""
    return write_consent(
        Path(ctx.workspace_root),
        member_key(Path(ctx.workspace_root), Path(ctx.repo_root), ctx.repo_name),
        Path(ctx.repo_root),
    )


def write_consent(
    workspace_root: Path, member_key: str, repo_root: Path, granted_by: str | None = None
) -> dict:
    record = {
        "hooks_sha": hooks_sha(repo_root),
        "granted_by": granted_by or getpass.getuser(),
        "granted_at": datetime.now(timezone.utc).isoformat(),
    }
    path = consent_path(workspace_root, member_key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, indent=2) + "\n")
    return record


def remove_consent(workspace_root: Path, member_key: str) -> bool:
    path = consent_path(workspace_root, member_key)
    if not path.exists():
        return False
    path.unlink()
    return True


def declared_member_roots(workspace_root: Path, exclude: Path | None = None) -> list[Path]:
    """The spec's [[repos]] paths (resolved), minus the caller's own member —
    the sibling-member boundary: a member may not write into another
    member's tree, even with consent."""
    spec = workspace_spec_path(workspace_root)
    if not spec.exists():
        return []
    try:
        doc = tomllib.loads(spec.read_text())
    except tomllib.TOMLDecodeError:
        return []
    roots = []
    exclude_resolved = exclude.resolve() if exclude is not None else None
    for repo in doc.get("repos", []):
        rel = str(repo.get("path", "")).strip("/")
        if not rel or rel in (".", ".."):
            continue
        root = (workspace_root / rel).resolve()
        if exclude_resolved is not None and root == exclude_resolved:
            continue
        roots.append(root)
    return roots


def norm_text(s: str) -> str:
    """The ONE comparison form for every boundary check: NFC normalization
    then casefold, because macOS volumes are
    case-insensitive and case-ambiguous — .GRIP, .Grip and .grip name the
    same directory, and a boundary that compares strings lets a case
    variant walk straight through it."""
    return unicodedata.normalize("NFC", s).casefold()


def _is_under(child: Path, root: Path) -> bool:
    """Prefix check in the normalized form, on BOTH sides."""
    c = norm_text(str(child))
    r = norm_text(str(root))
    return c == r or c.startswith(r + "/")


def confinement_violations(
    dest_template: str,
    repo_root: Path,
    workspace_root: Path,
    *,
    rendered: str | None = None,
    sibling_member_roots: list[Path] | None = None,
    kind: str | None = None,
    link_target: str | None = None,
) -> list[str]:
    """Every rule violation for one projection destination, or [] if the dest
    is confined. The runtime gate raises on a non-empty result; the trust
    verb prints the same flags. `rendered` is the dest after template
    rendering when the caller has the context (the runtime gate); at trust
    time the caller renders what it can and passes the same text.

    The boundary, RESOLVED with symlinks
    followed:
    - inside the workspace root (the root itself is inside — projecting to
      the root is the designed purpose; {unit_root} is gr2-declared and
      inside too);
    - AND not under any `.git` component, anywhere;
    - AND not inside ANOTHER member's path (a stranger member must not
      rewrite a trusted member's files);
    - and for [[files.link]], the link's TARGET (not only its location)
      must resolve inside the member's OWN tree — a consented link pointing
      at an absolute outside path exposes that path to every reader that
      follows it.

    There is no blanket absolute-dest refusal: an absolute dest that
    resolves inside the boundary is allowed; resolution already refuses
    every absolute path outside it.
    """
    flags: list[str] = []
    if not dest_template.strip():
        return ["empty destination"]
    text = rendered
    if text is None:
        # bind-time best effort: substitute the workspace-level tokens the
        # caller has; {lane_*}/{unit_root} templates stay unrendered and are
        # classified at runtime (the gate always passes the true rendered
        # value).
        text = dest_template.replace("{workspace_root}", str(workspace_root)).replace(
            "{repo_root}", str(repo_root)
        )
    if Path(text).is_absolute():
        target = Path(text)
    else:
        target = repo_root / text
    try:
        resolved = target.resolve()
    except OSError:
        resolved = target
    if not _is_under(resolved, workspace_root):
        flags.append("destination resolves outside the workspace root")
    for part in resolved.parts:
        if norm_text(part) == norm_text(".git"):
            flags.append("destination resolves under a .git directory")
            break
    grip_root = workspace_root / ".grip"
    if _is_under(resolved, grip_root):
        # a bound member must not write into
        # gr2's own state area — a forged consent record there binds another
        # member, and an overwritten workspace_spec.toml repoints every
        # member's url. .grip is off-limits to member projections entirely.
        flags.append("destination resolves under gr2's state directory (.grip)")
    for sibling in sibling_member_roots or []:
        try:
            sibling_resolved = sibling.resolve()
        except OSError:
            sibling_resolved = sibling
        if _is_under(resolved, sibling_resolved):
            flags.append("destination resolves inside another member's tree")
            break
    if kind == "link" and link_target is not None:
        try:
            link_resolved = Path(link_target).resolve()
        except OSError:
            link_resolved = Path(link_target)
        if not _is_under(link_resolved, repo_root):
            flags.append("link target resolves outside the member's own tree")
    return flags


def describe_member(
    workspace_root: Path,
    repo_root: Path,
    key: str,
    hooks,  # RepoHooks; imported lazily to avoid a circular import
) -> list[str]:
    """The `gr2 hooks trust` SHOW block: what is being consented to. Every
    lifecycle command, every projection with its resolved destination and
    confinement flags (a row with flags is an ESCAPE row, counted once no
    matter how many flags it carries), and any consent-shaped section found
    in the table. Returns (lines, escaped_rows)."""
    lines: list[str] = []
    escaped_rows: list[str] = []
    sha = hooks_sha(repo_root)
    lines.append(f"member {key} ({repo_root})")
    lines.append(f"hooks_sha {sha} ({sha[:12]})")
    if hooks.ignored_consent_keys:
        lines.append(
            "ignored consent-shaped section(s) in the hooks table: "
            + ", ".join(hooks.ignored_consent_keys)
            + " — consent is a local record on the host, never inside the hooks table"
        )
    for stage in ("on_materialize", "on_enter", "on_exit"):
        for hook in getattr(hooks, stage):
            lines.append(f"lifecycle {stage} · {hook.name} [{hook.when}]: {hook.command}")
    for item in [*hooks.file_links, *hooks.file_copies]:
        rendered = item.dest
        link_target: str | None = None
        if item.kind == "link":
            # the link's TARGET is what the dest will point at: render src
            # the same way the runtime gate does.
            lt = item.src
            if "{" in lt and "}" in lt:
                try:
                    from .hooks import render_text as _rt

                    lt = _rt(lt, _trust_ctx(workspace_root, repo_root, hooks, key))
                except ValueError:
                    lt = None
            if lt is not None:
                p = Path(lt)
                link_target = str(p if p.is_absolute() else repo_root / p)
        if "{" in item.dest and "}" in item.dest:
            try:
                from .hooks import HookContext, render_text

                rendered = render_text(item.dest, _trust_ctx(workspace_root, repo_root, hooks, key))
            except ValueError:
                lines.append(
                    f"projection {item.kind} dest {item.dest} (resolves per lane/unit at runtime)"
                )
                continue
        flags = confinement_violations(
            item.dest,
            repo_root,
            workspace_root,
            rendered=rendered,
            sibling_member_roots=declared_member_roots(workspace_root, exclude=repo_root),
            kind=item.kind,
            link_target=link_target,
        )
        # the SOURCE carries the same confinement as the runtime's first
        # raise: a resolved src outside the member's own tree is refused
        # when the hook block runs, so the screen flags it here — the
        # warning's count must cover what the runtime refuses.
        if "{" in item.src and "}" in item.src:
            try:
                from .hooks import render_text as _rt

                src_rendered = _rt(item.src, _trust_ctx(workspace_root, repo_root, hooks, key))
            except ValueError:
                src_rendered = None
            else:
                src_rendered = src_rendered  # noqa: F841 - name set for clarity
        else:
            src_rendered = item.src
        if src_rendered is not None:
            src_path = Path(src_rendered)
            src_abs = str(src_path if src_path.is_absolute() else repo_root / src_path)
            if not Path(src_abs).resolve().is_relative_to(repo_root.resolve()):
                flags = [*flags, "the projection source resolves outside the member's own tree"]
        marker = " ESCAPE: " + "; ".join(flags) if flags else ""
        lines.append(f"projection {item.kind} dest {rendered}{marker}")
        if flags:
            escaped_rows.append(item.kind)
    return lines, escaped_rows


LANE_DEPENDENT_TOKENS = (
    "{unit_root}",
    "{lane_root}",
    "{lane_name}",
    "{lane_owner}",
    "{lane_subject}",
)


def trust_refusal_rows(
    hooks, workspace_root: Path, repo_root: Path, key: str
) -> list[str]:
    """A consented root write is allowed
    only because the trust screen shows the RESOLVED destination before
    binding; a row whose destination cannot be resolved at trust time (an
    unset variable, or a template that renders differently later — the
    lane-dependent tokens) is REFUSED at trust time, not deferred. The
    consent record binds on the rows the screen showed, and it binds the
    whole table by hash, so a member carrying an unresolvable row is not
    bound at all until the table renders honestly."""
    from .hooks import render_text

    ctx = _trust_ctx_for_key(workspace_root, repo_root, str(hooks.repo_name or key))
    refusals: list[str] = []
    for item in [*hooks.file_links, *hooks.file_copies]:
        templates = [item.dest]
        if item.kind == "link":
            templates.append(item.src)
        for template in templates:
            reason = None
            if any(token in template for token in LANE_DEPENDENT_TOKENS):
                reason = "renders differently at runtime (lane/unit-dependent template)"
            else:
                try:
                    render_text(template, ctx)
                except ValueError as exc:
                    reason = f"unresolvable at trust time ({exc})"
            if reason is not None:
                refusals.append(f"projection {item.kind} dest {item.dest}: {reason}")
                break
    return refusals


def _trust_ctx_for_key(workspace_root: Path, repo_root: Path, key: str):
    from .hooks import HookContext

    return HookContext(
        workspace_root=workspace_root,
        unit_root=workspace_root,
        lane_root=repo_root,
        repo_root=repo_root,
        repo_name=key.rsplit("/", 1)[-1],
        lane_owner="workspace",
        lane_subject=key.rsplit("/", 1)[-1],
        lane_name="workspace",
    )


def _trust_ctx(workspace_root: Path, repo_root: Path, hooks, key: str):
    return _trust_ctx_for_key(workspace_root, repo_root, str(hooks.repo_name or key))

# ---------------------------------------------------------------------------
# Pending first-materialize markers: a member's hooks table
# arrives WITH the clone, so the first materialization of an unbound member
# skips and reports. The skip writes a pending marker; the NEXT materialize
# where the member is BOUND runs the member's hooks once (first-materialize
# semantics preserved) and clears the marker — never rm -rf.
# ---------------------------------------------------------------------------

PENDING_DIR = Path(".grip") / "state" / "hooks_pending"


def pending_marker_path(workspace_root: Path, member_key: str) -> Path:
    rel = member_key.strip("/")
    if not rel or rel in (".", ".."):
        raise ValueError(f"invalid member key: {member_key!r}")
    return workspace_root / PENDING_DIR / (rel + ".json")


def write_pending_marker(workspace_root: Path, member_key: str) -> bool:
    """Record that this member's hooks were skipped while unbound. Idempotent:
    an existing marker is left untouched (its first-seen time is the truth)."""
    from datetime import datetime, timezone

    path = pending_marker_path(workspace_root, member_key)
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"member": member_key, "pending_since": datetime.now(timezone.utc).isoformat()})
        + "\n"
    )
    return True


def take_pending_marker(workspace_root: Path, member_key: str) -> bool:
    """True when a pending marker existed (and is now consumed)."""
    path = pending_marker_path(workspace_root, member_key)
    if not path.exists():
        return False
    path.unlink()
    return True


def pending_members(workspace_root: Path) -> list[str]:
    """Every member key carrying an unconsumed pending marker."""
    root = workspace_root / PENDING_DIR
    if not root.exists():
        return []
    keys = []
    for path in sorted(root.glob("**/*.json")):
        keys.append(path.relative_to(root).with_suffix("").as_posix())
    return keys
