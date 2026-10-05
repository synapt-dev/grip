"""Workspace-owned disposal authority, distinct from a lane's discovery marker."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import tomllib
from pathlib import Path

from .gitops import git
from .layout import grip_dir


class ReviewAllocationError(ValueError):
    pass


def allocation_path(workspace: Path, target: Path) -> Path:
    key = hashlib.sha256(str(target.resolve()).encode()).hexdigest()
    return grip_dir(workspace.resolve()) / "state" / "review-allocations" / f"{key}.json"


def _physical(path: Path) -> list[int]:
    stat = path.stat()
    return [stat.st_dev, stat.st_ino]


def save_allocation(workspace: Path, target: Path, doc: dict) -> None:
    path = allocation_path(workspace, target)
    if path.is_symlink():
        raise ReviewAllocationError("allocation path is a symlink")
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".allocation-", delete=False) as stream:
        temporary = Path(stream.name)
        stream.write((json.dumps(doc, indent=2) + "\n").encode())
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def record_created_allocation(workspace: Path, target: Path, owner_unit: str,
                              lane_name: str, members: list[Path], *, disposable: bool) -> dict:
    """Called by successful owning creation, never by the marker writer."""
    workspace, target = workspace.resolve(), target.resolve()
    if target == workspace or target in workspace.parents or target in allocation_path(workspace, target).parents:
        raise ReviewAllocationError("review target cannot contain its workspace")
    path = allocation_path(workspace, target)
    if path.exists():
        return require_allocation(workspace, target, owner_unit=owner_unit)
    observed = []
    for member in members:
        member = member.resolve()
        if member != target and target not in member.parents:
            raise ReviewAllocationError("review member escapes allocation")
        gd = member / ".git"
        if not gd.is_dir() or gd.is_symlink():
            raise ReviewAllocationError("allocation requires owned ordinary Git members")
        head = git(member, "rev-parse", "HEAD")
        if head.returncode:
            raise ReviewAllocationError("cannot identify allocated member HEAD")
        observed.append({"path": str(member.relative_to(target)), "identity": _physical(member),
                         "git_identity": _physical(gd), "head": head.stdout.strip()})
    doc = {"version": 1, "workspace": str(workspace), "target": str(target),
           "identity": _physical(target), "owner_unit": owner_unit, "lane_name": lane_name,
           "disposable": disposable, "members": observed, "state": "open"}
    save_allocation(workspace, target, doc)
    return doc


def require_allocation(workspace: Path, target: Path, *, owner_unit: str | None = None) -> dict:
    if target.is_symlink():
        raise ReviewAllocationError("review target is a symlink")
    workspace, target = workspace.resolve(), target.resolve()
    path = allocation_path(workspace, target)
    if target == workspace or target in workspace.parents or target in path.parents:
        raise ReviewAllocationError("refusing workspace/root or allocation-containing target")
    try:
        if path.is_symlink():
            raise ValueError("allocation is a symlink")
        doc = json.loads(path.read_text())
        if doc["version"] != 1 or doc["workspace"] != str(workspace) or doc["target"] != str(target):
            raise ValueError("allocation coordinates differ")
        if owner_unit is not None and doc["owner_unit"] != owner_unit:
            raise ValueError("allocation unit differs")
        if type(doc["disposable"]) is not bool or doc["state"] not in {"open", "closing"} or not isinstance(doc["members"], list) or not doc["members"]:
            raise ValueError("invalid allocation state or members")
        if target.exists():
            if _physical(target) != doc["identity"]:
                raise ValueError("allocated target physical identity changed")
            for item in doc["members"]:
                relative = Path(item["path"])
                if relative.is_absolute() or ".." in relative.parts:
                    raise ValueError("allocated member escapes target")
                member = target / relative
                if member.is_symlink() or (member != target and target not in member.resolve().parents):
                    raise ValueError("allocated member changed location")
                gd = member / ".git"
                if not gd.is_dir() or gd.is_symlink() or _physical(member) != item["identity"] or _physical(gd) != item["git_identity"]:
                    raise ValueError("allocated Git identity unavailable or changed; operator recovery required")
                head = git(member, "rev-parse", "HEAD")
                if head.returncode or head.stdout.strip() != item["head"]:
                    raise ValueError("allocated member HEAD changed")
        elif doc["state"] != "closing":
            raise ValueError("open allocation target is absent")
        return doc
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ReviewAllocationError(
            f"no valid owning allocation for {target}: {exc}; marker alone cannot authorize deletion. "
            "For an existing managed project review use explicit legacy adoption. Otherwise save work/run "
            "artifacts and reopen into a newly allocated lane; unavailable identity requires operator recovery."
        ) from exc



def _legacy_transport_source(member: Path, expected_repo: str) -> str:
    """Resolve only the existing materializer's expected local mirror transport.

    Workspace/source receipts remain the authority. A cache origin is not an
    arbitrary redirect and this local provenance check is not publisher auth.
    """
    from urllib.parse import unquote, urlsplit
    from .review import canonical_source_identity
    from .open_gr_review import review_cache_root, _pin_transport_location, _mirror_basename

    def location(value: str) -> str:
        if value.startswith("file:"):
            parsed = urlsplit(value)
            if parsed.scheme != "file" or parsed.netloc not in {"", "localhost"} or parsed.query or parsed.fragment:
                raise ValueError("unrecognized local file transport")
            return unquote(parsed.path)
        return value.removeprefix("local:")

    origin = git(member, "remote", "get-url", "origin")
    if origin.returncode:
        raise ValueError("legacy member source origin unavailable")
    transport = location(origin.stdout.strip())
    direct = canonical_source_identity(transport, allow_local=True)
    if direct == expected_repo:
        return direct
    expected_location = _pin_transport_location(expected_repo)
    cache = review_cache_root().resolve()
    mirror = cache / f"{_mirror_basename(expected_location)}.git"
    actual = Path(transport)
    if not actual.is_absolute() or actual != mirror or actual.is_symlink() or actual.resolve() != mirror or mirror.resolve() != mirror or mirror.is_symlink():
        raise ValueError("legacy member transport is not the owning materializer's expected mirror")
    bare = git(mirror, "rev-parse", "--is-bare-repository")
    source = git(mirror, "remote", "get-url", "origin")
    if bare.returncode or bare.stdout.strip() != "true" or source.returncode:
        raise ValueError("legacy mirror source provenance unavailable")
    identity = canonical_source_identity(location(source.stdout.strip()), allow_local=True)
    if identity != expected_repo:
        raise ValueError("legacy mirror source identity differs from workspace")
    return identity


def adopt_legacy_project_allocation(workspace: Path, owner_unit: str, lane_name: str) -> dict:
    """Deliberate compatibility operation based on independent workspace state.

    The marker/project receipt does not choose the target or grant ownership.
    Standalone lanes without this managed state require operator recovery.
    """
    from gr2.prototypes import lane_workspace_prototype as lanes
    from .review import canonical_source_identity
    from .review_records import review_record_paths, read_review_record_at

    workspace = workspace.resolve()
    lanes.validate_lane_path_component(owner_unit, "owner_unit")
    lanes.validate_lane_path_component(lane_name, "lane_name")
    target = workspace / "reviews" / owner_unit / lane_name
    managed = workspace / "reviews" / owner_unit
    if target.resolve() != target or target.is_symlink() or target.resolve().parent != managed.resolve() or managed.resolve() not in target.resolve().parents:
        raise ReviewAllocationError("legacy project target is outside its managed workspace boundary")
    try:
        definition = tomllib.loads(lanes.lane_file(workspace, owner_unit, lane_name).read_text())
        if definition.get("owner_unit") != owner_unit or definition.get("lane_name") != lane_name or definition.get("lane_type") != "review" or definition.get("creation_source") != "project-review":
            raise ValueError("no independent owning project-review lane definition")
        kind = definition.get("lane_kind", "materialized")
        if kind not in {"materialized", "review-ephemeral"}:
            raise ValueError("legacy lane is not an owned project review")
        unit = lanes.find_unit_spec(workspace, owner_unit)
        spec = lanes.load_workspace_spec(workspace)
        allowed = {r["name"]: r["url"] for r in spec.get("repos", [])}
        members = definition.get("repos", [])
        if not members or len(set(members)) != len(members) or not set(members) <= set(unit.get("repos", [])):
            raise ValueError("legacy review membership is not independently authorized")
        actual = []
        for name in members:
            lanes.validate_lane_path_component(name, "member")
            member = target / "repos" / name
            if member.is_symlink() or target.resolve() not in member.resolve().parents:
                raise ValueError("legacy member target escapes owned root")
            paths = review_record_paths(workspace, owner_unit, lane_name, name, member)
            selected = read_review_record_at(paths, notice=lambda _s: None)
            # Allocation is recovered from workspace-owned canonical evidence, not
            # a member-local legacy receipt that could be planted with the marker.
            if selected is None or selected[1] != paths.current:
                raise ValueError("independent canonical safety evidence unavailable")
            record = selected[0]
            if set(record) != {"repo", "base", "head", "lane_kind"} or record["lane_kind"] != kind:
                raise ValueError("legacy workspace receipt schema/kind mismatch")
            expected_repo = canonical_source_identity(allowed[name], allow_local=True)
            if record["repo"] != expected_repo or _legacy_transport_source(member, expected_repo) != expected_repo:
                raise ValueError("legacy member source identity differs from workspace")
            head = git(member, "rev-parse", "HEAD")
            base = git(member, "merge-base", "--is-ancestor", record["base"], record["head"])
            if head.returncode or head.stdout.strip() != record["head"] or base.returncode:
                raise ValueError("legacy member HEAD/base differs from workspace safety evidence")
            actual.append(member)
        doc = record_created_allocation(workspace, target, owner_unit, lane_name, actual,
                                        disposable=kind == "review-ephemeral")
        doc["origin"] = "explicit-legacy-workspace-adoption"
        save_allocation(workspace, target, doc)
        return doc
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ReviewAllocationError(f"legacy adoption refused: {exc}; preserve evidence for operator recovery") from exc
