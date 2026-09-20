#!/usr/bin/env python3
"""Prototype lane metadata, execution planning, and shared scratchpads for gr2.

This prototype does not mutate git state. It explores three UX questions:

1. are lane records legible enough to guide multi-repo work?
2. can lightweight shared scratchpads fill the collaboration gap without
   violating private-workspace rules?
3. can the tool tell the user what to do next instead of forcing them to infer
   the workflow?
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import fcntl
import json
import os
import re
import shlex
import sys
import tempfile
import time
import tomllib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import tomli_w
from gr2.prototypes.jsonl_store import (
    JsonlRead,
    append_jsonl,
    read_jsonl,
    warn_unreadable,
)
from gr2.python_cli import gitops
from gr2.python_cli import review as _review
from gr2.python_cli import push as _push

LANE_SCHEMA_VERSION = 1
SCRATCHPAD_SCHEMA_VERSION = 1
MAX_PORTABLE_COMPONENT_BYTES = 255


def serialize_toml(document: dict[str, object]) -> str:
    """Serialize structured data without interpolating values into TOML source."""
    return tomli_w.dumps(document)


def validate_lane_path_component(value: str, field: str) -> None:
    """Require one portable path component while allowing TOML-significant text."""
    try:
        encoded_length = len(value.encode("utf-8"))
    except UnicodeEncodeError:
        encoded_length = MAX_PORTABLE_COMPONENT_BYTES + 1
    invalid = (
        not value
        or len(value) > 128
        or encoded_length > MAX_PORTABLE_COMPONENT_BYTES
        or value != value.strip()
        or value in {".", ".."}
        or "/" in value
        or "\\" in value
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    )
    if invalid:
        raise SystemExit(
            f"invalid {field}: expected 1-128 characters and at most "
            f"{MAX_PORTABLE_COMPONENT_BYTES} UTF-8 bytes forming one path component; "
            "no separators, control characters, dot components, or outer whitespace"
        )


@dataclasses.dataclass
class LaneMetadata:
    schema_version: int
    lane_name: str
    owner_unit: str
    agent_id: str | None
    lane_type: str
    repos: list[str]
    branch_map: dict[str, str]
    pr_associations: list[str]
    shared_context_roots: list[str]
    private_context_roots: list[str]
    exec_defaults: dict[str, object]
    creation_source: str
    shared_with: list[str]
    handoff_source: dict[str, str] | None
    # By design, lane_kind is required on every
    # lane document so a reader never infers the reconstruction guarantee. A
    # "materialized" lane holds an isolated clone pinned at a head; a "bound"
    # lane is a label on the author's own existing worktree (single-repo only),
    # honest under the clean-tree/HEAD guard bind re-checks. bound_worktree is
    # the resolved worktree path, present only for bound lanes. bound_head is the
    # worktree HEAD recorded at create time — the drift baseline a review bind on a
    # bound lane re-checks against (a moved HEAD or a dirty tree refuses).
    lane_kind: str = "materialized"
    bound_worktree: str | None = None
    bound_head: str | None = None
    # The fork base: the per-repo coordinate the lane
    # forked from on its integration branch, recorded ONCE at create and never
    # recomputed. Each entry is {branch: <integration branch, e.g. dev>, sha: <its
    # tip when the lane was cut>}. The reproduction paths (workspace snapshot,
    # review bind) read `base` from here; a lane with no entry reads base as
    # unknown (explicit), never HEAD^. Recorded, not derived: the caller that knows
    # the integration tip (materialization) supplies it; create_lane only stores it.
    fork_base: dict[str, dict[str, str]] = dataclasses.field(default_factory=dict)

    def as_toml(self) -> str:
        document: dict[str, object] = {
            "schema_version": self.schema_version,
            "lane_name": self.lane_name,
            "owner_unit": self.owner_unit,
            "agent_id": self.agent_id or "",
            "lane_type": self.lane_type,
            "lane_kind": self.lane_kind,
            "creation_source": self.creation_source,
            "repos": self.repos,
            "shared_with": self.shared_with,
            "branch_map": dict(sorted(self.branch_map.items())),
            "context": {
                "shared_roots": self.shared_context_roots,
                "private_roots": self.private_context_roots,
            },
            "exec_defaults": self.exec_defaults,
        }
        if self.bound_worktree is not None:
            document["bound_worktree"] = self.bound_worktree
        if self.bound_head is not None:
            document["bound_head"] = self.bound_head
        if self.fork_base:
            document["fork_base"] = {
                repo: {"branch": entry["branch"], "sha": entry["sha"]}
                for repo, entry in sorted(self.fork_base.items())
            }
        if self.pr_associations:
            document["pr_associations"] = [{"ref": ref} for ref in self.pr_associations]
        if self.handoff_source:
            document["handoff"] = self.handoff_source
        return serialize_toml(document)


@dataclasses.dataclass
class SharedScratchpad:
    schema_version: int
    name: str
    kind: str
    purpose: str
    participants: list[str]
    linked_refs: list[str]
    lifecycle: str
    creation_source: str
    docs_root: str
    notes_root: str
    context_root: str
    created_at: str
    updated_at: str

    def as_toml(self) -> str:
        return serialize_toml(
            {
                "schema_version": self.schema_version,
                "name": self.name,
                "kind": self.kind,
                "purpose": self.purpose,
                "lifecycle": self.lifecycle,
                "creation_source": self.creation_source,
                "created_at": self.created_at,
                "updated_at": self.updated_at,
                "participants": self.participants,
                "linked_refs": self.linked_refs,
                "paths": {
                    "docs_root": self.docs_root,
                    "notes_root": self.notes_root,
                    "context_root": self.context_root,
                },
            }
        )


@dataclasses.dataclass(frozen=True)
class LaneTransitionOutcome:
    """One authoritative result for a lane state transition.

    The prototype CLI and the Typer CLI both render this result.  Neither is
    allowed to infer success from a path print while the state writer reports a
    different outcome.
    """

    action: str
    owner_unit: str
    previous_lane: str | None
    current_lane: str | None
    state_path: Path
    status: str = "ok"

    @property
    def exit_code(self) -> int:
        return 0 if self.status == "ok" else 1

    def as_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "action": self.action,
            "owner_unit": self.owner_unit,
            "previous_lane": self.previous_lane,
            "current_lane": self.current_lane,
            "state_path": str(self.state_path),
        }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Prototype gr2 lanes + shared scratchpads")
    sub = parser.add_subparsers(dest="command", required=True)

    create = sub.add_parser("create-lane")
    create.add_argument("workspace_root", type=Path)
    create.add_argument("owner_unit")
    create.add_argument("lane_name")
    create.add_argument("--type", default="feature")
    create.add_argument("--repos", required=True, help="comma-separated repo names")
    create.add_argument(
        "--branch",
        required=True,
        help="default branch or repo=branch mappings separated by commas",
    )
    create.add_argument("--source", default="manual")
    create.add_argument(
        "--command",
        dest="default_commands",
        action="append",
        default=[],
        help="default lane command",
    )

    review = sub.add_parser("create-review-lane")
    review.add_argument("workspace_root", type=Path)
    review.add_argument("owner_unit")
    review.add_argument("repo")
    review.add_argument("pr_number", type=int)
    review.add_argument("--lane-name")
    review.add_argument("--branch")

    share_lane = sub.add_parser("share-lane")
    share_lane.add_argument("workspace_root", type=Path)
    share_lane.add_argument("owner_unit")
    share_lane.add_argument("lane_name")
    share_lane.add_argument("target_unit")

    continuation = sub.add_parser("create-continuation-lane")
    continuation.add_argument("workspace_root", type=Path)
    continuation.add_argument("source_owner_unit")
    continuation.add_argument("source_lane_name")
    continuation.add_argument("target_unit")
    continuation.add_argument("target_lane_name")

    handoff = sub.add_parser("plan-handoff")
    handoff.add_argument("workspace_root", type=Path)
    handoff.add_argument("source_owner_unit")
    handoff.add_argument("source_lane_name")
    handoff.add_argument("target_unit")
    handoff.add_argument("--mode", choices=["shared", "continuation"], required=True)
    handoff.add_argument("--target-lane-name")
    handoff.add_argument("--json", action="store_true")

    show = sub.add_parser("show-lane")
    show.add_argument("workspace_root", type=Path)
    show.add_argument("owner_unit")
    show.add_argument("lane_name")

    lane_list = sub.add_parser("list-lanes")
    lane_list.add_argument("workspace_root", type=Path)
    lane_list.add_argument("--owner-unit")

    next_step = sub.add_parser("next-step")
    next_step.add_argument("workspace_root", type=Path)
    next_step.add_argument("owner_unit")
    next_step.add_argument("lane_name")

    plan = sub.add_parser("plan-exec")
    plan.add_argument("workspace_root", type=Path)
    plan.add_argument("owner_unit")
    plan.add_argument("lane_name")
    plan.add_argument("command_text")
    plan.add_argument("--repos", help="optional comma-separated repo subset")
    plan.add_argument("--json", action="store_true")

    enter = sub.add_parser("enter-lane")
    enter.add_argument("workspace_root", type=Path)
    enter.add_argument("owner_unit")
    enter.add_argument("lane_name")
    enter.add_argument(
        "--actor", required=True, help="actor label, e.g. human:layne or agent:atlas"
    )
    enter.add_argument("--notify-channel", action="store_true")
    enter.add_argument("--recall", action="store_true")

    exit_lane = sub.add_parser("exit-lane")
    exit_lane.add_argument("workspace_root", type=Path)
    exit_lane.add_argument("owner_unit")
    exit_lane.add_argument("--actor", required=True)
    exit_lane.add_argument("--notify-channel", action="store_true")
    exit_lane.add_argument("--recall", action="store_true")

    current = sub.add_parser("current-lane")
    current.add_argument("workspace_root", type=Path)
    current.add_argument("owner_unit")
    current.add_argument("--json", action="store_true")

    history = sub.add_parser("lane-history")
    history.add_argument("workspace_root", type=Path)
    history.add_argument("owner_unit")
    history.add_argument("--json", action="store_true")

    lease = sub.add_parser("acquire-lane-lease")
    lease.add_argument("workspace_root", type=Path)
    lease.add_argument("owner_unit")
    lease.add_argument("lane_name")
    lease.add_argument("--actor", required=True)
    lease.add_argument("--mode", choices=["edit", "exec", "review"], required=True)
    lease.add_argument(
        "--ttl-seconds",
        type=int,
        default=900,
        help="lease TTL in seconds before it is considered stale",
    )
    lease.add_argument(
        "--force",
        action="store_true",
        help="break conflicting stale leases with a warning",
    )

    release = sub.add_parser("release-lane-lease")
    release.add_argument("workspace_root", type=Path)
    release.add_argument("owner_unit")
    release.add_argument("lane_name")
    release.add_argument("--actor", required=True)

    show_leases = sub.add_parser("show-lane-leases")
    show_leases.add_argument("workspace_root", type=Path)
    show_leases.add_argument("owner_unit")
    show_leases.add_argument("lane_name")
    show_leases.add_argument("--json", action="store_true")

    scratch = sub.add_parser("create-shared-scratchpad")
    scratch.add_argument("workspace_root", type=Path)
    scratch.add_argument("name")
    scratch.add_argument("--kind", default="doc")
    scratch.add_argument("--purpose", required=True)
    scratch.add_argument("--participant", action="append", default=[])
    scratch.add_argument("--ref", action="append", default=[])
    scratch.add_argument("--source", default="manual")

    scratch_show = sub.add_parser("show-shared-scratchpad")
    scratch_show.add_argument("workspace_root", type=Path)
    scratch_show.add_argument("name")

    scratch_list = sub.add_parser("list-shared-scratchpads")
    scratch_list.add_argument("workspace_root", type=Path)

    scratch_audit = sub.add_parser("audit-shared-scratchpads")
    scratch_audit.add_argument("workspace_root", type=Path)
    scratch_audit.add_argument(
        "--stale-days",
        type=int,
        default=7,
        help="mark scratchpads as stale when untouched for this many days",
    )

    promote = sub.add_parser("plan-promote-scratchpad")
    promote.add_argument("workspace_root", type=Path)
    promote.add_argument("name")
    promote.add_argument("--target-repo", required=True)
    promote.add_argument("--target-path", required=True)
    promote.add_argument("--owner-unit", required=True)
    promote.add_argument("--lane", help="optional lane that should carry the promotion")

    recommend = sub.add_parser("recommend-surface")
    recommend.add_argument("--kind", choices=["code", "doc", "review", "planning"], required=True)
    recommend.add_argument("--collaborative", action="store_true")
    recommend.add_argument("--formal-review", action="store_true")
    recommend.add_argument("--repos", type=int, default=1)
    recommend.add_argument("--shared-draft", action="store_true")

    review_check = sub.add_parser("check-review-requirements")
    review_check.add_argument("workspace_root", type=Path)
    review_check.add_argument("repo")
    review_check.add_argument("pr_number", type=int)
    review_check.add_argument("--json", action="store_true")

    return parser.parse_args()


def lane_state_root(workspace_root: Path) -> Path:
    """The one home for workspace lane state.

    Lane state is grip plumbing: it lives beside the rest of the lane control
    plane under ``.grip/state/`` -- the same tree that already holds
    ``current_lane``, ``lane_transitions``, ``lane_creation`` and the lease
    locks. The old ``workspace_root/agents/`` root was pollution at the
    workspace top level; every path that reaches into lane state funnels
    through here so the layout has a single point of truth.

    Deliberately NOT ``.grip/lanes``:
    the boundary test (``test_oss_gr2_has_no_external_lane_envelope_reader``)
    reserves the ``.grip/lanes`` fragment; records go beside the control plane
    at ``.grip/state/lanes`` instead.
    """
    return workspace_root / ".grip" / "state" / "lanes"


def lane_dir(workspace_root: Path, owner_unit: str, lane_name: str) -> Path:
    return lane_state_root(workspace_root) / owner_unit / lane_name


def lane_file(workspace_root: Path, owner_unit: str, lane_name: str) -> Path:
    return lane_dir(workspace_root, owner_unit, lane_name) / "lane.toml"


def shared_scratchpad_dir(workspace_root: Path, name: str) -> Path:
    return workspace_root / "shared" / "scratchpads" / name


def shared_scratchpad_file(workspace_root: Path, name: str) -> Path:
    return shared_scratchpad_dir(workspace_root, name) / "scratchpad.toml"


def current_lane_file(workspace_root: Path, owner_unit: str) -> Path:
    return workspace_root / ".grip" / "state" / "current_lane" / f"{owner_unit}.json"


def lane_transition_lock_file(workspace_root: Path, owner_unit: str) -> Path:
    """One unit-scoped lock covers complete enter/exit metadata transitions."""
    return workspace_root / ".grip" / "state" / "lane_transitions" / f"{owner_unit}.lock"


def lane_creation_lock_file(workspace_root: Path, owner_unit: str, lane_name: str) -> Path:
    return workspace_root / ".grip" / "state" / "lane_creation" / owner_unit / f"{lane_name}.lock"


def scratchpad_creation_lock_file(workspace_root: Path, name: str) -> Path:
    return workspace_root / ".grip" / "state" / "scratchpad_creation" / f"{name}.lock"


@contextlib.contextmanager
def exclusive_lock(path: Path):
    """Lock a complete read/modify/write scope, releasing on every path."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def atomic_replace_text(path: Path, content: str) -> None:
    """Publish complete metadata bytes or retain the preceding file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def lane_leases_file(workspace_root: Path, owner_unit: str, lane_name: str) -> Path:
    return lane_dir(workspace_root, owner_unit, lane_name) / "leases.json"


def lane_leases_lock_file(workspace_root: Path, owner_unit: str, lane_name: str) -> Path:
    return lane_dir(workspace_root, owner_unit, lane_name) / "leases.lock"


def workspace_edit_leases_lock_file(workspace_root: Path) -> Path:
    return workspace_root / ".grip" / "state" / "workspace_edit_leases.lock"


def shared_lane_access_file(workspace_root: Path, owner_unit: str, lane_name: str) -> Path:
    return (
        workspace_root / ".grip" / "state" / "shared_lane_access" / owner_unit / f"{lane_name}.json"
    )


def events_dir(workspace_root: Path) -> Path:
    return workspace_root / ".grip" / "events"


def lane_events_file(workspace_root: Path) -> Path:
    return events_dir(workspace_root) / "lane_events.jsonl"


def recall_lane_events_file(workspace_root: Path) -> Path:
    return events_dir(workspace_root) / "recall_lane_history.jsonl"


def load_workspace_spec(workspace_root: Path) -> dict:
    with (workspace_root / ".grip" / "workspace_spec.toml").open("rb") as fh:
        return tomllib.load(fh)


def find_unit_spec(workspace_root: Path, owner_unit: str) -> dict:
    spec = load_workspace_spec(workspace_root)
    for unit in spec.get("units", []):
        if unit.get("name") == owner_unit:
            return unit
    raise SystemExit(f"unit not found in workspace spec: {owner_unit}")


def load_lane_doc(workspace_root: Path, owner_unit: str, lane_name: str) -> dict:
    path = lane_file(workspace_root, owner_unit, lane_name)
    if not path.exists():
        raise SystemExit(f"lane not found: {owner_unit}/{lane_name}")
    return tomllib.loads(path.read_text())


def record_fork_base(
    workspace_root: Path, owner_unit: str, lane_name: str, fork_base: dict[str, dict[str, str]]
) -> None:
    """Record the fork base (the materialization point) into an existing lane doc.

    ``create_lane`` records ONLY the fork base the caller supplies
    (record, do not derive). The CLI `lane create` clones each repo at its
    branch AFTER the doc is written, so the materialization point — the branch the
    lane forked from and the sha it started at — is not known until then; this writes
    it back. Without it a CLI-created lane has no fork base and `review create-project`
    refuses — the multi-repo review producer failing its own fruit test for a stranger.

    ``fork_base`` maps repo -> {"branch": <name>, "sha": <40-hex>}. Each repo must be in
    the lane; a non-hex sha or empty branch is refused, matching create_lane's checks.
    """
    path = lane_file(workspace_root, owner_unit, lane_name)
    document = load_lane_doc(workspace_root, owner_unit, lane_name)
    repos_in_lane = set(document.get("repos", []))
    validated: dict[str, dict[str, str]] = {}
    for repo, entry in fork_base.items():
        if repo not in repos_in_lane:
            raise SystemExit(f"fork_base references repo outside lane: {repo}")
        branch = str(entry.get("branch", "")).strip()
        sha = str(entry.get("sha", "")).strip()
        if not branch:
            raise SystemExit(f"fork_base for {repo} has no branch")
        if not re.fullmatch(r"[0-9a-f]{40}", sha):
            raise SystemExit(f"fork_base sha for {repo} must be a 40-hex commit, got: {sha!r}")
        validated[repo] = {"branch": branch, "sha": sha}
    document["fork_base"] = {repo: validated[repo] for repo in sorted(validated)}
    atomic_replace_text(path, serialize_toml(document))


def load_shared_scratchpad_doc(workspace_root: Path, name: str) -> dict:
    path = shared_scratchpad_file(workspace_root, name)
    if not path.exists():
        raise SystemExit(f"shared scratchpad not found: {name}")
    return tomllib.loads(path.read_text())


def load_current_lane_doc(workspace_root: Path, owner_unit: str) -> dict:
    path = current_lane_file(workspace_root, owner_unit)
    if not path.exists():
        raise SystemExit(f"no current lane recorded for unit: {owner_unit}")
    return json.loads(path.read_text())


def require_current_lane(workspace_root: Path, owner_unit: str) -> dict:
    """The current lane record, refusing when there is not one.

    ``load_current_lane_doc`` raises only when the FILE is absent.  After a
    ``lane exit`` the file is present and holds ``{"current": null}``, so every
    caller that went straight to ``doc["current"]["lane_name"]`` raised
    ``TypeError: 'NoneType' object is not subscriptable`` and put a traceback in
    front of the user -- for a state gr2 writes itself, by the most ordinary
    sequence there is: enter, then exit.

    Two absent-shapes, one of them guarded, was the whole defect.  This exists
    so callers cannot read the field without the check: three separate call
    sites had each written the unguarded read, and a fourth copy of the guard
    would only postpone the fifth.
    """
    doc = load_current_lane_doc(workspace_root, owner_unit)
    current = doc.get("current")
    if not current:
        raise SystemExit(f"no current lane recorded for unit: {owner_unit}")
    return current


def emit_lane_event(workspace_root: Path, payload: dict) -> None:
    append_jsonl(lane_events_file(workspace_root), payload)


def emit_recall_lane_event(workspace_root: Path, payload: dict) -> None:
    append_jsonl(recall_lane_events_file(workspace_root), payload)


def iter_lane_events(workspace_root: Path) -> JsonlRead:
    """Rows AND the lines that could not be read. See ``jsonl_store.JsonlRead``."""

    return read_jsonl(lane_events_file(workspace_root))


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n")


def load_lane_leases(workspace_root: Path, owner_unit: str, lane_name: str) -> list[dict]:
    path = lane_leases_file(workspace_root, owner_unit, lane_name)
    if not path.exists():
        return []
    return json.loads(path.read_text())


def workspace_constraints(workspace_root: Path) -> dict:
    spec = load_workspace_spec(workspace_root)
    return spec.get("workspace_constraints", {})


def max_global_edit_leases(workspace_root: Path) -> int | None:
    value = workspace_constraints(workspace_root).get("max_concurrent_edit_leases_global")
    if value is None:
        return None
    return int(value)


def active_workspace_edit_leases(workspace_root: Path) -> list[dict]:
    rows: list[dict] = []
    for path in iter_lane_files(workspace_root):
        lane_doc = tomllib.loads(path.read_text())
        owner_unit = lane_doc["owner_unit"]
        lane_name = lane_doc["lane_name"]
        for lease in load_lane_leases(workspace_root, owner_unit, lane_name):
            if lease["mode"] != "edit" or is_stale_lease(lease):
                continue
            rows.append(
                {
                    "owner_unit": owner_unit,
                    "lane_name": lane_name,
                    **lease,
                }
            )
    return rows


def lease_locking_enabled() -> bool:
    return os.environ.get("GR2_DISABLE_LEASE_LOCKING") != "1"


def write_lane_leases(
    workspace_root: Path,
    owner_unit: str,
    lane_name: str,
    leases: list[dict],
    *,
    lock_fh=None,
) -> None:
    path = lane_leases_file(workspace_root, owner_unit, lane_name)
    path.parent.mkdir(parents=True, exist_ok=True)

    if lock_fh is None:
        lock_path = lane_leases_lock_file(workspace_root, owner_unit, lane_name)
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+", encoding="utf-8") as owned_lock_fh:
            if lease_locking_enabled():
                fcntl.flock(owned_lock_fh.fileno(), fcntl.LOCK_EX)
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(leases, indent=2) + "\n")
            os.replace(tmp, path)
            if lease_locking_enabled():
                fcntl.flock(owned_lock_fh.fileno(), fcntl.LOCK_UN)
        return

    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(leases, indent=2) + "\n")
    os.replace(tmp, path)


def mutate_lane_leases(
    workspace_root: Path,
    owner_unit: str,
    lane_name: str,
    mutator,
):
    lock_path = lane_leases_lock_file(workspace_root, owner_unit, lane_name)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as lock_fh:
        if lease_locking_enabled():
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
        path = lane_leases_file(workspace_root, owner_unit, lane_name)
        if path.exists():
            leases = json.loads(path.read_text())
        else:
            leases = []
        if not lease_locking_enabled():
            delay = float(os.environ.get("GR2_LEASE_TEST_DELAY", "0"))
            if delay > 0:
                time.sleep(delay)
        result = mutator(leases)
        if result.get("write"):
            write_lane_leases(
                workspace_root,
                owner_unit,
                lane_name,
                result["leases"],
                lock_fh=lock_fh,
            )
        if lease_locking_enabled():
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)
        return result


def mutate_workspace_edit_lease(
    workspace_root: Path,
    owner_unit: str,
    lane_name: str,
    actor: str,
    mutator,
):
    """Check the workspace cap and mutate one lane in a single critical section."""
    lock_path = workspace_edit_leases_lock_file(workspace_root)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as lock_fh:
        if lease_locking_enabled():
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)

        cap = max_global_edit_leases(workspace_root)
        if cap is not None:
            active_edits = [
                lease
                for lease in active_workspace_edit_leases(workspace_root)
                if not (
                    lease["owner_unit"] == owner_unit
                    and lease["lane_name"] == lane_name
                    and lease["actor"] == actor
                )
            ]
            if len(active_edits) >= cap:
                return {
                    "status": "blocked",
                    "payload": {
                        "status": "blocked",
                        "reason": "workspace-edit-lease-cap",
                        "requested": {
                            "owner_unit": owner_unit,
                            "lane_name": lane_name,
                            "actor": actor,
                            "mode": "edit",
                        },
                        "active_edit_leases": active_edits,
                        "max_concurrent_edit_leases_global": cap,
                    },
                    "write": False,
                }

        delay = float(os.environ.get("GR2_WORKSPACE_CAP_TEST_DELAY", "0"))
        if delay > 0:
            time.sleep(delay)
        return mutate_lane_leases(workspace_root, owner_unit, lane_name, mutator)


def iter_lane_files(workspace_root: Path, owner_unit: str | None = None) -> list[Path]:
    state_root = lane_state_root(workspace_root)
    if not state_root.exists():
        return []
    if owner_unit:
        unit_roots = [state_root / owner_unit]
    else:
        unit_roots = [path for path in state_root.iterdir() if path.is_dir()]

    files: list[Path] = []
    for root in unit_roots:
        if not root.exists():
            continue
        files.extend(sorted(root.glob("*/lane.toml")))
    return files


def iter_shared_scratchpad_files(workspace_root: Path) -> list[Path]:
    root = workspace_root / "shared" / "scratchpads"
    if not root.exists():
        return []
    return sorted(root.glob("*/scratchpad.toml"))


def now_utc() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def parse_utc(raw: str) -> datetime:
    return datetime.fromisoformat(raw)


def is_stale_lease(lease: dict) -> bool:
    expires_at = lease.get("expires_at")
    if not expires_at:
        return False
    return parse_utc(expires_at) <= datetime.now(UTC)


def build_lease(actor: str, mode: str, ttl_seconds: int) -> dict:
    acquired = datetime.now(UTC).replace(microsecond=0)
    expires = acquired + timedelta(seconds=ttl_seconds)
    return {
        "actor": actor,
        "mode": mode,
        "ttl_seconds": ttl_seconds,
        "acquired_at": acquired.isoformat(),
        "expires_at": expires.isoformat(),
    }


def lease_conflicts(existing_mode: str, requested_mode: str) -> bool:
    matrix = {
        "edit": {"edit", "exec", "review"},
        "exec": {"edit", "review"},
        "review": {"edit", "exec", "review"},
    }
    return requested_mode in matrix.get(existing_mode, set())


def conflicting_leases(
    leases: list[dict], actor: str, requested_mode: str
) -> tuple[list[dict], list[dict]]:
    active: list[dict] = []
    stale: list[dict] = []
    for lease in leases:
        if lease["actor"] == actor:
            continue
        if not lease_conflicts(lease["mode"], requested_mode):
            continue
        if is_stale_lease(lease):
            stale.append(lease)
        else:
            active.append(lease)
    return active, stale


def age_days(path: Path) -> int:
    modified = datetime.fromtimestamp(path.stat().st_mtime, UTC)
    return max(0, int((datetime.now(UTC) - modified).total_seconds() // 86400))


def parse_repo_list(raw: str) -> list[str]:
    return [repo.strip() for repo in raw.split(",") if repo.strip()]


def parse_branch_arg(raw: str, repos: list[str]) -> dict[str, str]:
    if "=" not in raw:
        return {repo: raw for repo in repos}

    branch_map: dict[str, str] = {}
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        repo, branch = item.split("=", 1)
        repo = repo.strip()
        branch = branch.strip()
        if repo not in repos:
            raise SystemExit(f"branch mapping references repo outside lane: {repo}")
        if not branch:
            raise SystemExit(f"empty branch in mapping: {item}")
        branch_map[repo] = branch

    missing = [repo for repo in repos if repo not in branch_map]
    if missing:
        raise SystemExit("missing branch mapping for repos: " + ", ".join(missing))

    return branch_map


def _validate_bound_worktree(bind_path: Path, workspace_root: Path) -> tuple[str, str]:
    """Validate a worktree an author wants a bound lane to LABEL, returning
    ``(head_sha, branch)``.

    A bound lane derives its reviewed bytes from this worktree instead of a
    pinned clone, so the reconstruction guarantee holds only if the tree is a
    real, clean, non-detached git checkout — the same discipline the
    freeze-public-range gate applies before it trusts a range. Any failure is a
    hard refusal (``SystemExit``); a bound lane is never created over a tree that
    could make its recorded head fail to reconstruct the reviewed bytes.

    Containment: the resolved worktree must live UNDER ``workspace_root`` (a plain
    filesystem containment, same shape as ``close_review_lane``'s strict-descendant
    gate). This is the "path outside the author's own gripspace"
    refusal, implemented as a path check rather than via identity resolution — so
    it needs no owner-unit -> gripspace mapping and stays clear of the premium
    boundary."""
    resolved = bind_path.resolve()
    root = workspace_root.resolve()
    if resolved != root and root not in resolved.parents:
        raise SystemExit(
            f"--bind path {resolved} is not under the workspace root {root}: a bound lane may "
            "only label a worktree inside the author's own workspace"
        )
    if not resolved.is_dir():
        raise SystemExit(f"--bind path is not a directory: {resolved}")
    if not gitops.is_git_repo(resolved):
        raise SystemExit(f"--bind path is not a git work tree: {resolved}")
    branch = gitops.current_branch(resolved)
    if not branch:
        raise SystemExit(
            f"--bind refuses a detached HEAD at {resolved}: a bound lane's head must be a "
            "named branch so pr create has something to push"
        )
    if gitops.repo_dirty(resolved):
        raise SystemExit(
            f"--bind refuses a dirty tree at {resolved}: a bound lane's receipt records the "
            "worktree HEAD, and uncommitted changes (tracked OR untracked) would make the "
            "recorded head fail to reconstruct the reviewed bytes. Commit or stash first."
        )
    head = gitops.current_head_sha(resolved)
    if not head:
        raise SystemExit(f"--bind could not read HEAD at {resolved}")
    return head, branch


def create_lane(args: argparse.Namespace) -> int:
    workspace_root = args.workspace_root.resolve()
    validate_lane_path_component(args.owner_unit, "owner_unit")
    validate_lane_path_component(args.lane_name, "lane_name")
    spec = load_workspace_spec(workspace_root)
    unit_spec = find_unit_spec(workspace_root, args.owner_unit)
    repo_names = [item["name"] for item in spec.get("repos", [])]
    repos = parse_repo_list(args.repos)

    missing = [repo for repo in repos if repo not in repo_names]
    if missing:
        raise SystemExit(f"unknown repos for lane: {', '.join(missing)}")

    # By design, --bind makes the lane a LABEL on
    # an existing worktree instead of a fresh materialization. A bound lane is
    # single-repo only, and its head/branch come from the worktree under the
    # clean-tree/HEAD guard; no clone is materialized (no repos/ subdir).
    bind = getattr(args, "bind", None)
    lane_kind = "materialized"
    bound_worktree: str | None = None
    bound_head: str | None = None
    if bind is not None:
        # Single-repo check comes BEFORE branch parsing: a bound lane's branch is
        # derived from the worktree, not the --branch arg, so parse_branch_arg
        # must not run (and must not mask the single-repo refusal with a
        # missing-mapping error for a repo a bound lane would never carry).
        if len(repos) != 1:
            raise SystemExit(
                "a bound lane is single-repo only: pass exactly "
                f"one repo to --repos, got {repos or '[]'}"
            )
        head, branch = _validate_bound_worktree(Path(bind), workspace_root)
        lane_kind = "bound"
        bound_worktree = str(Path(bind).resolve())
        # Record the worktree HEAD at create: the drift baseline a review bind on
        # this bound lane re-checks against (moved HEAD or dirty tree refuses).
        bound_head = head
        branch_map = {repos[0]: branch}
    else:
        branch_map = parse_branch_arg(args.branch, repos)

    # Fork base: record what the caller supplies, do not derive.
    # Each entry names a repo IN the lane and carries a 40-hex integration-branch tip.
    raw_fork_base = getattr(args, "fork_base", None) or {}
    fork_base: dict[str, dict[str, str]] = {}
    for repo, entry in raw_fork_base.items():
        if repo not in repos:
            raise SystemExit(f"fork_base references repo outside lane: {repo}")
        branch = str(entry.get("branch", "")).strip()
        sha = str(entry.get("sha", "")).strip()
        if not branch:
            raise SystemExit(f"fork_base for {repo} has no branch")
        if not re.fullmatch(r"[0-9a-f]{40}", sha):
            raise SystemExit(f"fork_base sha for {repo} must be a 40-hex commit, got: {sha!r}")
        fork_base[repo] = {"branch": branch, "sha": sha}

    metadata = LaneMetadata(
        schema_version=LANE_SCHEMA_VERSION,
        lane_name=args.lane_name,
        owner_unit=args.owner_unit,
        agent_id=unit_spec.get("agent_id"),
        lane_type=args.type,
        repos=repos,
        branch_map=branch_map,
        pr_associations=list(getattr(args, "pr_associations", [])),
        shared_context_roots=["config", ".grip/context/shared"],
        private_context_roots=[
            f"agents/{args.owner_unit}/home/context",
            str(
                lane_dir(workspace_root, args.owner_unit, args.lane_name).relative_to(
                    workspace_root
                )
                / "context"
            ),
        ],
        exec_defaults={
            "parallelism": "workspace-default",
            "fail_fast": True,
            "default_command_family": ["build", "test"],
            "commands": args.default_commands,
        },
        creation_source=args.source,
        shared_with=[],
        handoff_source=None,
        lane_kind=lane_kind,
        bound_worktree=bound_worktree,
        bound_head=bound_head,
        fork_base=fork_base,
    )
    lane_root = lane_dir(workspace_root, args.owner_unit, args.lane_name)
    metadata_path = lane_file(workspace_root, args.owner_unit, args.lane_name)
    expected = metadata.as_toml()
    # Refuse the ordinary existing-target case before even publishing lock
    # scaffolding. The locked recheck below still protects a racing creator.
    if metadata_path.exists():
        if metadata_path.read_text() == expected:
            print(f"lane already exists unchanged: {metadata_path}")
            return 0
        raise SystemExit(f"refusing to replace existing lane: {metadata_path}")
    if lane_root.exists():
        raise SystemExit(f"refusing to create lane over existing path: {lane_root}")
    with exclusive_lock(lane_creation_lock_file(workspace_root, args.owner_unit, args.lane_name)):
        if metadata_path.exists():
            if metadata_path.read_text() == expected:
                print(f"lane already exists unchanged: {metadata_path}")
                return 0
            raise SystemExit(f"refusing to replace existing lane: {metadata_path}")
        if lane_root.exists():
            raise SystemExit(f"refusing to create lane over existing path: {lane_root}")
        lane_root.mkdir(parents=True)
        # A bound lane owns no clone, so it has no repos/ subdir — the absence is
        # itself a signal to materialization that there is nothing to clone.
        if lane_kind != "bound":
            (lane_root / "repos").mkdir()
        (lane_root / "context").mkdir()
        atomic_replace_text(metadata_path, expected)
    print(metadata_path)
    return 0


def bind_bound_lane(
    workspace_root: Path, owner_unit: str, lane_name: str, *, base: str | None = None, allow_local: bool = False
) -> "_review.ReviewRecord":
    """Bind a review receipt for a BOUND lane, sourced LIVE from the author's
    worktree.

    A bound lane has no materialized clone and no carried range: its reviewed
    bytes are the author's own worktree, so reconstruction is "read the local
    tree". This re-checks, at bind time, the same invariants create imposed —
    the worktree is a clean, non-detached git checkout — AND that HEAD has not
    DRIFTED from the head recorded at create (``bound_head``). A moved HEAD or a
    dirty tree (tracked OR untracked) is a hard refusal: the receipt promises the
    recorded head reconstructs the reviewed bytes, and drift breaks that promise.

    On success it writes the ``(repo, base, head, lane_kind="bound")`` receipt to
    the worktree's own ``.git/grip-review.json`` (the same path helper a
    materialized lane uses) and returns the record.

    ``base`` is the pin the reviewed range is measured from. It MUST be a full
    40-hex commit that is an ANCESTOR of the worktree head — a non-hex string, a
    well-formed-but-nonexistent sha (``ffff…``), or a commit head does not descend
    from is refused, so a nonsense base can never reach the receipt. Deriving base
    automatically from the lane branch's upstream is a follow-up (the open
    receipt-contract question flagged to review); the drift mechanism does not
    depend on how base is chosen.

    ``allow_local`` gates a non-portable ``local:<path>`` identity for a worktree
    with no GitHub origin. It defaults False (a review identity must be a portable
    GitHub source); pass it only for a local test worktree."""
    lane_doc = load_lane_doc(workspace_root.resolve(), owner_unit, lane_name)
    if lane_doc.get("lane_kind") != "bound":
        raise SystemExit(
            f"bind_bound_lane: lane {owner_unit}/{lane_name} is not a bound lane "
            f"(lane_kind={lane_doc.get('lane_kind')!r}); use the materialized review path"
        )
    # Base resolution: an explicit base wins; when omitted, read
    # the recorded fork base for this bound lane's single repo. A lane with neither
    # is refused with the unknown message — the review base is never HEAD^.
    if base is None:
        fork_base = lane_doc.get("fork_base", {})
        repos = lane_doc.get("repos", [])
        entry = fork_base.get(repos[0]) if repos else None
        recorded = (entry or {}).get("sha")
        if not recorded:
            raise SystemExit(
                f"bind refuses: lane {owner_unit}/{lane_name} has no recorded fork base and no "
                "--base was given; the review base is unknown (it is never HEAD^). Recreate the "
                "lane with a fork base, or pass --base explicitly."
            )
        base = recorded
    worktree = Path(lane_doc.get("bound_worktree") or "")
    recorded_head = lane_doc.get("bound_head")
    if not worktree or not recorded_head:
        raise SystemExit(
            f"bind_bound_lane: bound lane {owner_unit}/{lane_name} is missing "
            "bound_worktree or bound_head; it was not created by lane create --bind"
        )
    resolved = worktree.resolve()
    if not gitops.is_git_repo(resolved):
        raise SystemExit(f"bind refuses: bound worktree {resolved} is no longer a git work tree")
    if not gitops.current_branch(resolved):
        raise SystemExit(f"bind refuses: bound worktree {resolved} is in detached HEAD")
    if gitops.repo_dirty(resolved):
        raise SystemExit(
            f"bind refuses (DRIFT): bound worktree {resolved} is dirty (tracked OR untracked "
            "changes); the recorded head would not reconstruct the reviewed bytes. Commit or "
            "stash, then re-bind."
        )
    current_head = gitops.current_head_sha(resolved)
    if current_head != recorded_head:
        raise SystemExit(
            f"bind refuses (DRIFT): bound worktree {resolved} HEAD is {current_head}, not the "
            f"head recorded at create ({recorded_head}); the lane has moved. Re-create the bound "
            "lane at the new head, or reset the worktree to the recorded head."
        )
    # base must be a real ancestor of head, not just a well-formed sha: a 40-hex
    # string that is not a commit (ffff...), or a commit head does not descend
    # from, would put a base into the receipt that does not bound the reviewed
    # range. The hex check refuses non-sha strings before git sees them; the
    # merge-base --is-ancestor check refuses a well-formed-but-wrong base.
    if len(base) != 40 or any(c not in "0123456789abcdef" for c in base):
        raise SystemExit(
            f"bind refuses: base {base!r} is not a full 40-hex commit sha"
        )
    if gitops.git(resolved, "merge-base", "--is-ancestor", base, current_head).returncode != 0:
        raise SystemExit(
            f"bind refuses: base {base} is not an ancestor of the worktree head {current_head} "
            "(not a commit, or head does not descend from it); it cannot bound the reviewed range"
        )
    origin = gitops.remote_origin_url(resolved)
    try:
        repo_identity = _review.canonical_source_identity(origin or str(resolved), allow_local=allow_local)
    except _review.ReviewError as exc:
        raise SystemExit(
            f"bind refuses: bound worktree {resolved} has no portable GitHub origin "
            f"({exc}); pass --allow-local to bind a non-portable local: identity"
        ) from exc
    record = _review.ReviewRecord(
        repo=repo_identity, base=base, head=current_head, lane_kind="bound"
    )
    receipt_path = _review.review_record_path(resolved)
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    receipt_path.write_text(json.dumps(record.to_dict(), indent=2) + "\n")
    return record


def pr_create_bound_lane(
    workspace_root: Path, owner_unit: str, lane_name: str, *, remote: str | None = None,
    set_upstream: bool = True,
) -> "_push.PushReceipt":
    """`pr create` for a BOUND lane: push the reviewed head from the author's own
    worktree.

    A bound lane's PR is opened FROM the worktree, not from a materialized clone,
    so this reuses the ordinary push seam (``push_current_branch``, which verifies
    the remote ref equals HEAD). It is gated on the bind receipt:

    * the lane must be bound and must have a bind receipt (``gr2 lane bind`` first);
    * the reviewed range ``base..head`` must be NON-EMPTY — ``base == head`` is
      refused HERE, at pr create, not at bind: bind records whatever head the
      author is on, but there is nothing to open a PR for when the range is empty;
    * the worktree HEAD must still equal the receipt head — if it drifted since
      bind, the push would carry an unreviewed head, so re-bind first.

    The smallest end-to-end proof pushes to a local bare remote and opens nothing;
    the platform (gh) path sits behind this same push seam."""
    lane_doc = load_lane_doc(workspace_root.resolve(), owner_unit, lane_name)
    if lane_doc.get("lane_kind") != "bound":
        raise SystemExit(
            f"pr_create_bound_lane: lane {owner_unit}/{lane_name} is not a bound lane "
            f"(lane_kind={lane_doc.get('lane_kind')!r}); use the group/adapter pr path"
        )
    worktree = Path(lane_doc.get("bound_worktree") or "")
    if not worktree:
        raise SystemExit(f"pr create refuses: bound lane {owner_unit}/{lane_name} has no bound_worktree")
    resolved = worktree.resolve()
    receipt_path = _review.review_record_path(resolved)
    if not receipt_path.is_file():
        raise SystemExit(
            f"pr create refuses: bound lane {owner_unit}/{lane_name} has no review receipt at "
            f"{receipt_path}; run `gr2 lane bind` first"
        )
    try:
        receipt = json.loads(receipt_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"pr create refuses: review receipt is unreadable or malformed ({exc})")
    base, head = receipt.get("base"), receipt.get("head")
    if base == head:
        raise SystemExit(
            f"pr create refuses: the reviewed range is EMPTY (base == head == {head}); there is "
            "nothing to open a PR for. Commit work, re-bind, then pr create."
        )
    current_head = gitops.current_head_sha(resolved)
    if current_head != head:
        raise SystemExit(
            f"pr create refuses: bound worktree HEAD {current_head} no longer equals the reviewed "
            f"head {head} recorded in the bind receipt; the worktree moved since bind. Re-bind, "
            "then pr create."
        )
    try:
        return _push.push_current_branch(resolved, remote=remote, set_upstream=set_upstream)
    except _push.PushError as exc:
        raise SystemExit(f"pr create refuses: push from the bound worktree failed ({exc})")


def enter_lane(args: argparse.Namespace) -> LaneTransitionOutcome:
    workspace_root = args.workspace_root.resolve()
    lane_doc = load_lane_doc(workspace_root, args.owner_unit, args.lane_name)
    unit_spec = find_unit_spec(workspace_root, args.owner_unit)
    path = current_lane_file(workspace_root, args.owner_unit)
    with exclusive_lock(lane_transition_lock_file(workspace_root, args.owner_unit)):
        previous: list[dict] = []
        previous_lane: str | None = None
        if path.exists():
            old = json.loads(path.read_text())
            previous = old.get("recent", [])
            current = old.get("current")
            if current:
                previous_lane = current.get("lane_name")
                previous.insert(0, current)

        deduped: list[dict] = []
        seen: set[tuple[str, str]] = set()
        for item in previous:
            key = (item["owner_unit"], item["lane_name"])
            if key in seen or key == (args.owner_unit, args.lane_name):
                continue
            seen.add(key)
            deduped.append(item)
        doc = {
            "current": {
                "owner_unit": args.owner_unit,
                "agent_id": unit_spec.get("agent_id"),
                "lane_name": args.lane_name,
                "lane_type": lane_doc["lane_type"],
                "repos": lane_doc.get("repos", []),
                "actor": args.actor,
                "entered_at": now_utc(),
            },
            "recent": deduped[:5],
        }
        atomic_replace_text(path, json.dumps(doc, indent=2) + "\n")
        event = {
            "type": "lane_enter", "agent": args.actor, "agent_id": unit_spec.get("agent_id"),
            "owner_unit": args.owner_unit, "lane": args.lane_name,
            "lane_type": lane_doc["lane_type"], "repos": lane_doc.get("repos", []),
            "timestamp": now_utc(),
        }
        emit_lane_event(workspace_root, event)
    if args.notify_channel:
        event["channel_message"] = (
            f"{args.actor} entered {args.owner_unit}/{args.lane_name} "
            f"[{lane_doc['lane_type']}] repos={','.join(lane_doc.get('repos', []))}"
        )
    if args.recall:
        emit_recall_lane_event(
            workspace_root,
            {
                "kind": "lane_transition",
                "action": "enter",
                "owner_unit": args.owner_unit,
                "agent_id": unit_spec.get("agent_id"),
                "actor": args.actor,
                "lane": args.lane_name,
                "lane_type": lane_doc["lane_type"],
                "repos": lane_doc.get("repos", []),
                "timestamp": event["timestamp"],
            },
        )
    return LaneTransitionOutcome("enter", args.owner_unit, previous_lane, args.lane_name, path)


def exit_lane(args: argparse.Namespace) -> LaneTransitionOutcome:
    workspace_root = args.workspace_root.resolve()
    path = current_lane_file(workspace_root, args.owner_unit)
    with exclusive_lock(lane_transition_lock_file(workspace_root, args.owner_unit)):
        doc = load_current_lane_doc(workspace_root, args.owner_unit)
        current_doc = doc.get("current")
        if not current_doc:
            raise SystemExit(f"no current lane to exit for unit: {args.owner_unit}")
        event = {
            "type": "lane_exit", "agent": args.actor, "agent_id": current_doc.get("agent_id"),
            "owner_unit": args.owner_unit, "lane": current_doc["lane_name"],
            "lane_type": current_doc["lane_type"], "repos": current_doc.get("repos", []),
            "timestamp": now_utc(),
        }
        recent = doc.get("recent", [])
        next_current = recent[0] if recent else None
        updated = {"current": next_current, "recent": recent[1:] if next_current else []}
        atomic_replace_text(path, json.dumps(updated, indent=2) + "\n")
        emit_lane_event(workspace_root, event)
    if args.notify_channel:
        event["channel_message"] = (
            f"{args.actor} exited {args.owner_unit}/{current_doc['lane_name']} "
            f"[{current_doc['lane_type']}]"
        )
    if args.recall:
        emit_recall_lane_event(
            workspace_root,
            {
                "kind": "lane_transition",
                "action": "exit",
                "owner_unit": args.owner_unit,
                "agent_id": current_doc.get("agent_id"),
                "actor": args.actor,
                "lane": current_doc["lane_name"],
                "lane_type": current_doc["lane_type"],
                "repos": current_doc.get("repos", []),
                "timestamp": event["timestamp"],
            },
        )

    return LaneTransitionOutcome("exit", args.owner_unit, current_doc["lane_name"], next_current.get("lane_name") if next_current else None, path)


def current_lane(args: argparse.Namespace) -> int:
    doc = load_current_lane_doc(args.workspace_root.resolve(), args.owner_unit)
    if args.json:
        print(json.dumps(doc, indent=2))
        return 0
    current_doc = doc.get("current")
    if not current_doc:
        raise SystemExit(f"no current lane recorded for unit: {args.owner_unit}")
    print("gr2 prototype current-lane")
    print(
        f"owner={current_doc['owner_unit']} lane={current_doc['lane_name']} type={current_doc['lane_type']} actor={current_doc['actor']}"
    )
    print(f"entered_at={current_doc['entered_at']}")
    recent = doc.get("recent", [])
    if recent:
        print("recent:")
        for item in recent:
            print(f"  - {item['owner_unit']}/{item['lane_name']} ({item['lane_type']})")
    return 0


def lane_history(args: argparse.Namespace) -> int:
    read = iter_lane_events(args.workspace_root.resolve())
    warn_unreadable(read, "the lane event log")
    rows = [event for event in read.rows if event.get("owner_unit") == args.owner_unit]
    if args.json:
        print(json.dumps(rows, indent=2))
        return 0
    print("TIMESTAMP\tTYPE\tACTOR\tAGENT_ID\tLANE\tREPOS")
    for row in rows:
        print(
            f"{row.get('timestamp', '-')}\t{row.get('type', '-')}\t{row.get('agent', '-')}\t{row.get('agent_id', '-')}\t{row.get('lane', '-')}\t{','.join(row.get('repos', []))}"
        )
    return 0


def acquire_lane_lease(args: argparse.Namespace) -> int:
    workspace_root = args.workspace_root.resolve()
    load_lane_doc(workspace_root, args.owner_unit, args.lane_name)
    mutator = lambda leases: _acquire_lane_lease_mutation(leases, args)
    if args.mode == "edit":
        result = mutate_workspace_edit_lease(
            workspace_root,
            args.owner_unit,
            args.lane_name,
            args.actor,
            mutator,
        )
    else:
        result = mutate_lane_leases(
            workspace_root,
            args.owner_unit,
            args.lane_name,
            mutator,
        )
    if result["status"] == "blocked":
        print(json.dumps(result["payload"], indent=2))
        return 1
    if result["status"] == "warning":
        print(json.dumps(result["warning"], indent=2))
    lane_doc = load_lane_doc(workspace_root, args.owner_unit, args.lane_name)
    unit_spec = find_unit_spec(workspace_root, args.owner_unit)
    emit_lane_event(
        workspace_root,
        {
            "type": "lease_acquire",
            "agent": args.actor,
            "agent_id": unit_spec.get("agent_id"),
            "owner_unit": args.owner_unit,
            "lane": args.lane_name,
            "lane_type": lane_doc["lane_type"],
            "lease_mode": args.mode,
            "ttl_seconds": args.ttl_seconds,
            "repos": lane_doc.get("repos", []),
            "timestamp": now_utc(),
        },
    )
    print(lane_leases_file(workspace_root, args.owner_unit, args.lane_name))
    return 0


def _acquire_lane_lease_mutation(leases: list[dict], args: argparse.Namespace) -> dict:
    retained = [lease for lease in leases if lease["actor"] != args.actor]
    active_conflicts, stale_conflicts = conflicting_leases(retained, args.actor, args.mode)

    if active_conflicts:
        return {
            "status": "blocked",
            "payload": {
                "status": "blocked",
                "reason": "conflicting-active-lease",
                "lane": args.lane_name,
                "owner_unit": args.owner_unit,
                "requested": {"actor": args.actor, "mode": args.mode},
                "conflicting_leases": active_conflicts,
            },
            "write": False,
        }

    if stale_conflicts and not args.force:
        return {
            "status": "blocked",
            "payload": {
                "status": "blocked",
                "reason": "stale-conflicting-lease",
                "lane": args.lane_name,
                "owner_unit": args.owner_unit,
                "requested": {"actor": args.actor, "mode": args.mode},
                "conflicting_leases": stale_conflicts,
                "hint": "rerun with --force to break stale conflicting leases",
            },
            "write": False,
        }

    warning = None
    if stale_conflicts and args.force:
        warning = {
            "status": "warning",
            "reason": "breaking-stale-conflicting-leases",
            "broken_leases": stale_conflicts,
        }
        stale_actors = {lease["actor"] for lease in stale_conflicts}
        retained = [lease for lease in retained if lease["actor"] not in stale_actors]

    retained.append(build_lease(args.actor, args.mode, args.ttl_seconds))
    return {
        "status": "warning" if warning else "ok",
        "warning": warning,
        "leases": retained,
        "write": True,
    }


def release_lane_lease(args: argparse.Namespace) -> int:
    workspace_root = args.workspace_root.resolve()
    lane_doc = load_lane_doc(workspace_root, args.owner_unit, args.lane_name)
    unit_spec = find_unit_spec(workspace_root, args.owner_unit)
    mutate_lane_leases(
        workspace_root,
        args.owner_unit,
        args.lane_name,
        lambda leases: {
            "status": "ok",
            "leases": [lease for lease in leases if lease["actor"] != args.actor],
            "write": True,
        },
    )
    emit_lane_event(
        workspace_root,
        {
            "type": "lease_release",
            "agent": args.actor,
            "agent_id": unit_spec.get("agent_id"),
            "owner_unit": args.owner_unit,
            "lane": args.lane_name,
            "lane_type": lane_doc["lane_type"],
            "repos": lane_doc.get("repos", []),
            "timestamp": now_utc(),
        },
    )
    print(lane_leases_file(workspace_root, args.owner_unit, args.lane_name))
    return 0


def show_lane_leases(args: argparse.Namespace) -> int:
    leases = load_lane_leases(args.workspace_root.resolve(), args.owner_unit, args.lane_name)
    if args.json:
        print(json.dumps(leases, indent=2))
        return 0
    print("ACTOR\tMODE\tTTL\tACQUIRED_AT\tEXPIRES_AT\tSTATE")
    for lease in leases:
        state = "stale" if is_stale_lease(lease) else "active"
        print(
            f"{lease['actor']}\t{lease['mode']}\t{lease.get('ttl_seconds', '-')}\t{lease['acquired_at']}\t{lease.get('expires_at', '-')}\t{state}"
        )
    return 0


def create_review_lane(args: argparse.Namespace) -> int:
    lane_name = args.lane_name or f"review-{args.pr_number}"
    branch = args.branch or f"pr/{args.pr_number}"
    workspace_root = args.workspace_root.resolve()
    validate_lane_path_component(args.owner_unit, "owner_unit")
    validate_lane_path_component(lane_name, "lane_name")
    lane_path = lane_file(workspace_root, args.owner_unit, lane_name)
    association = f"{args.repo}#{args.pr_number}"
    if lane_path.exists():
        with exclusive_lock(lane_creation_lock_file(workspace_root, args.owner_unit, lane_name)):
            document = load_lane_doc(workspace_root, args.owner_unit, lane_name)
            if document.get("lane_type") != "review" or args.repo not in document.get("repos", []):
                raise SystemExit(
                    f"existing lane is not a review lane for repo {args.repo}: "
                    f"{args.owner_unit}/{lane_name}"
                )
            associations = document.setdefault("pr_associations", [])
            if association not in [item.get("ref") for item in associations]:
                associations.append({"ref": association})
            atomic_replace_text(lane_path, serialize_toml(document))
        print(f"created review lane {args.owner_unit}/{lane_name} for {association}")
        return 0

    create_args = argparse.Namespace(
        workspace_root=workspace_root,
        owner_unit=args.owner_unit,
        lane_name=lane_name,
        type="review",
        repos=args.repo,
        branch=f"{args.repo}={branch}",
        source="pull-request",
        default_commands=[],
        pr_associations=[association],
    )
    create_lane(create_args)
    print(f"created review lane {args.owner_unit}/{lane_name} for {association}")
    return 0


def check_review_requirements(args: argparse.Namespace) -> int:
    workspace_root = args.workspace_root.resolve()
    ref = f"{args.repo}#{args.pr_number}"
    required = int(
        workspace_constraints(workspace_root).get("required_reviewers", {}).get(args.repo, 0)
    )
    matching: list[dict] = []
    for path in iter_lane_files(workspace_root):
        doc = tomllib.loads(path.read_text())
        if doc.get("lane_type") != "review":
            continue
        refs = [item["ref"] for item in doc.get("pr_associations", [])]
        if ref not in refs:
            continue
        matching.append(
            {
                "owner_unit": doc["owner_unit"],
                "lane_name": doc["lane_name"],
                "repos": doc.get("repos", []),
            }
        )
    reviewer_units = sorted({row["owner_unit"] for row in matching})
    payload = {
        "repo": args.repo,
        "pr_number": args.pr_number,
        "required_reviewers": required,
        "actual_reviewers": len(reviewer_units),
        "satisfied": len(reviewer_units) >= required,
        "review_lanes": matching,
    }
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print(json.dumps(payload, indent=2))
    return 0


def share_lane(args: argparse.Namespace) -> int:
    workspace_root = args.workspace_root.resolve()
    load_lane_doc(workspace_root, args.owner_unit, args.lane_name)
    find_unit_spec(workspace_root, args.target_unit)
    access_path = shared_lane_access_file(workspace_root, args.owner_unit, args.lane_name)
    with exclusive_lock(lane_creation_lock_file(workspace_root, args.owner_unit, args.lane_name)):
        if access_path.exists():
            doc = json.loads(access_path.read_text())
        else:
            doc = {
                "owner_unit": args.owner_unit,
                "lane_name": args.lane_name,
                "shared_with": [],
            }
        if args.target_unit not in doc["shared_with"]:
            doc["shared_with"].append(args.target_unit)
        atomic_replace_text(access_path, json.dumps(doc, indent=2) + "\n")
    print(access_path)
    return 0


def create_continuation_lane(args: argparse.Namespace) -> int:
    workspace_root = args.workspace_root.resolve()
    validate_lane_path_component(args.target_unit, "target_unit")
    validate_lane_path_component(args.target_lane_name, "target_lane_name")
    source = load_lane_doc(workspace_root, args.source_owner_unit, args.source_lane_name)
    unit_spec = find_unit_spec(workspace_root, args.target_unit)
    metadata = LaneMetadata(
        schema_version=LANE_SCHEMA_VERSION,
        lane_name=args.target_lane_name,
        owner_unit=args.target_unit,
        agent_id=unit_spec.get("agent_id"),
        lane_type=source["lane_type"],
        repos=source.get("repos", []),
        branch_map=source.get("branch_map", {}),
        pr_associations=[item["ref"] for item in source.get("pr_associations", [])],
        shared_context_roots=source.get("context", {}).get("shared_roots", []),
        private_context_roots=[
            f"agents/{args.target_unit}/home/context",
            str(
                lane_dir(
                    workspace_root, args.target_unit, args.target_lane_name
                ).relative_to(workspace_root)
                / "context"
            ),
        ],
        exec_defaults=source.get("exec_defaults", {}),
        creation_source="lane-handoff",
        shared_with=[],
        handoff_source={
            "kind": "continuation",
            "source_owner_unit": args.source_owner_unit,
            "source_lane": args.source_lane_name,
        },
    )
    lane_root = lane_dir(workspace_root, args.target_unit, args.target_lane_name)
    metadata_path = lane_file(workspace_root, args.target_unit, args.target_lane_name)
    expected = metadata.as_toml()
    if metadata_path.exists():
        if metadata_path.read_text() == expected:
            print(f"lane already exists unchanged: {metadata_path}")
            return 0
        raise SystemExit(f"refusing to replace existing lane: {metadata_path}")
    if lane_root.exists():
        raise SystemExit(f"refusing to create lane over existing path: {lane_root}")
    with exclusive_lock(lane_creation_lock_file(workspace_root, args.target_unit, args.target_lane_name)):
        if metadata_path.exists():
            if metadata_path.read_text() == expected:
                print(f"lane already exists unchanged: {metadata_path}")
                return 0
            raise SystemExit(f"refusing to replace existing lane: {metadata_path}")
        if lane_root.exists():
            raise SystemExit(f"refusing to create lane over existing path: {lane_root}")
        lane_root.mkdir(parents=True)
        (lane_root / "repos").mkdir()
        (lane_root / "context").mkdir()
        atomic_replace_text(metadata_path, expected)
    print(metadata_path)
    return 0


def plan_handoff(args: argparse.Namespace) -> int:
    workspace_root = args.workspace_root.resolve()
    source = load_lane_doc(workspace_root, args.source_owner_unit, args.source_lane_name)
    find_unit_spec(workspace_root, args.target_unit)
    if args.mode == "shared":
        access_path = shared_lane_access_file(
            workspace_root, args.source_owner_unit, args.source_lane_name
        )
        access = json.loads(access_path.read_text()) if access_path.exists() else None
        payload = {
            "mode": "shared",
            "source_owner_unit": args.source_owner_unit,
            "source_lane_name": args.source_lane_name,
            "target_unit": args.target_unit,
            "shared_access_present": bool(
                access and args.target_unit in access.get("shared_with", [])
            ),
            "exec_rows": [
                {
                    "acting_unit": args.target_unit,
                    "owner_unit": args.source_owner_unit,
                    "lane_name": args.source_lane_name,
                    "repo": repo,
                    "cwd": str(
                        lane_dir(
                            workspace_root,
                            args.source_owner_unit,
                            args.source_lane_name,
                        )
                        / "repos"
                        / repo
                    ),
                    "lease_scope": f"{args.source_owner_unit}/{args.source_lane_name}",
                }
                for repo in source.get("repos", [])
            ],
            "invariant_assessment": {
                "unit_scoped": False,
                "reason": "target unit must execute inside another unit's lane root and lease scope",
            },
        }
    else:
        target_lane_name = args.target_lane_name or f"{args.source_lane_name}-relay"
        payload = {
            "mode": "continuation",
            "source_owner_unit": args.source_owner_unit,
            "source_lane_name": args.source_lane_name,
            "target_unit": args.target_unit,
            "target_lane_name": target_lane_name,
            "exec_rows": [
                {
                    "acting_unit": args.target_unit,
                    "owner_unit": args.target_unit,
                    "lane_name": target_lane_name,
                    "repo": repo,
                    "cwd": str(
                        lane_dir(
                            workspace_root,
                            args.target_unit,
                            target_lane_name,
                        )
                        / "repos"
                        / repo
                    ),
                    "lease_scope": f"{args.target_unit}/{target_lane_name}",
                }
                for repo in source.get("repos", [])
            ],
            "handoff_source": {
                "source_owner_unit": args.source_owner_unit,
                "source_lane_name": args.source_lane_name,
            },
            "invariant_assessment": {
                "unit_scoped": True,
                "reason": "target unit gets its own lane root, lease scope, and current-lane state while keeping source linkage",
            },
        }

    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print(json.dumps(payload, indent=2))
    return 0


def create_shared_scratchpad(args: argparse.Namespace) -> int:
    workspace_root = args.workspace_root.resolve()
    validate_lane_path_component(args.name, "name")
    scratchpad = SharedScratchpad(
        schema_version=SCRATCHPAD_SCHEMA_VERSION,
        name=args.name,
        kind=args.kind,
        purpose=args.purpose,
        participants=sorted(set(args.participant)),
        linked_refs=args.ref,
        lifecycle="draft",
        creation_source=args.source,
        docs_root=f"shared/scratchpads/{args.name}/docs",
        notes_root=f"shared/scratchpads/{args.name}/notes",
        context_root=f"shared/scratchpads/{args.name}/context",
        created_at=now_utc(),
        updated_at=now_utc(),
    )
    root = shared_scratchpad_dir(workspace_root, args.name)
    metadata_path = shared_scratchpad_file(workspace_root, args.name)
    expected = scratchpad.as_toml()
    if metadata_path.exists():
        if metadata_path.read_text() == expected:
            print(f"scratchpad already exists unchanged: {metadata_path}")
            return 0
        raise SystemExit(f"refusing to replace existing scratchpad: {metadata_path}")
    if root.exists():
        raise SystemExit(f"refusing to create scratchpad over existing path: {root}")
    with exclusive_lock(scratchpad_creation_lock_file(workspace_root, args.name)):
        if metadata_path.exists():
            if metadata_path.read_text() == expected:
                print(f"scratchpad already exists unchanged: {metadata_path}")
                return 0
            raise SystemExit(f"refusing to replace existing scratchpad: {metadata_path}")
        if root.exists():
            raise SystemExit(f"refusing to create scratchpad over existing path: {root}")
        root.mkdir(parents=True)
        (root / "docs").mkdir()
        (root / "notes").mkdir()
        (root / "context").mkdir()
        atomic_replace_text(metadata_path, expected)
        atomic_replace_text(root / "docs" / "README.md",
            f"# {args.name}\n\nPurpose: {args.purpose}\n\nParticipants: "
            + (", ".join(scratchpad.participants) if scratchpad.participants else "unassigned")
            + "\n"
        )
    print(metadata_path)
    return 0


def list_lanes(args: argparse.Namespace) -> int:
    workspace_root = args.workspace_root.resolve()
    print("OWNER\tLANE\tTYPE\tREPOS\tPRS")
    for path in iter_lane_files(workspace_root, args.owner_unit):
        doc = tomllib.loads(path.read_text())
        refs = ",".join(item["ref"] for item in doc.get("pr_associations", [])) or "-"
        print(
            f"{doc['owner_unit']}\t{doc['lane_name']}\t{doc['lane_type']}\t{len(doc.get('repos', []))}\t{refs}"
        )
    return 0


def show_lane(args: argparse.Namespace) -> int:
    print(lane_file(args.workspace_root.resolve(), args.owner_unit, args.lane_name).read_text())
    return 0


def show_shared_scratchpad(args: argparse.Namespace) -> int:
    print(shared_scratchpad_file(args.workspace_root.resolve(), args.name).read_text())
    return 0


def list_shared_scratchpads(args: argparse.Namespace) -> int:
    workspace_root = args.workspace_root.resolve()
    print("NAME\tKIND\tLIFECYCLE\tAGE_DAYS\tPARTICIPANTS\tPURPOSE")
    for path in iter_shared_scratchpad_files(workspace_root):
        doc = tomllib.loads(path.read_text())
        participants = ",".join(doc.get("participants", [])) or "-"
        print(
            f"{doc['name']}\t{doc['kind']}\t{doc['lifecycle']}\t{age_days(path)}\t{participants}\t{doc['purpose']}"
        )
    return 0


def audit_shared_scratchpads(args: argparse.Namespace) -> int:
    workspace_root = args.workspace_root.resolve()
    print("NAME\tSTATUS\tAGE_DAYS\tISSUES")
    for path in iter_shared_scratchpad_files(workspace_root):
        doc = tomllib.loads(path.read_text())
        root = path.parent
        issues: list[str] = []
        days = age_days(path)
        docs_root = root / "docs"
        notes_root = root / "notes"
        context_root = root / "context"

        if days >= args.stale_days and doc.get("lifecycle") not in {"done", "paused"}:
            issues.append("stale-active")
        if not doc.get("participants"):
            issues.append("no-participants")
        if not doc.get("linked_refs"):
            issues.append("no-refs")
        if not docs_root.exists():
            issues.append("missing-docs-root")
        if not notes_root.exists():
            issues.append("missing-notes-root")
        if not context_root.exists():
            issues.append("missing-context-root")
        if doc.get("kind") == "doc" and docs_root.exists() and not any(docs_root.iterdir()):
            issues.append("empty-docs")

        status = "ok" if not issues else "needs-attention"
        print(f"{doc['name']}\t{status}\t{days}\t{','.join(issues) or '-'}")
    return 0


def plan_promote_scratchpad(args: argparse.Namespace) -> int:
    workspace_root = args.workspace_root.resolve()
    doc = load_shared_scratchpad_doc(workspace_root, args.name)
    lane_name = args.lane or f"promote-{args.name}"
    print("gr2 prototype scratchpad-promotion plan")
    print(f"scratchpad: {doc['name']}")
    print(f"kind: {doc['kind']}")
    print(f"lifecycle: {doc['lifecycle']}")
    print(f"target repo: {args.target_repo}")
    print(f"target path: {args.target_path}")
    print(f"owner unit: {args.owner_unit}")
    print(f"suggested lane: {lane_name}")
    print("recommended:")
    print(f"  1. create or reuse a feature lane for {args.target_repo} under {args.owner_unit}")
    print(
        f"  2. copy content from shared/scratchpads/{doc['name']}/docs into {args.target_repo}:{args.target_path}"
    )
    print(f"  3. branch and commit in lane {lane_name}")
    print("  4. open a PR once the artifact is ready for formal review")
    if not doc.get("linked_refs"):
        print(
            "warning: scratchpad has no linked refs; traceability should be added before promotion"
        )
    return 0


def recommend_surface(args: argparse.Namespace) -> int:
    recommendation = "feature-lane"
    rationale: list[str] = []

    if args.kind == "review" or args.formal_review:
        recommendation = "review-lane"
        rationale.append("formal review or PR inspection should stay isolated")
    elif args.kind in {"doc", "planning"} and args.collaborative:
        recommendation = "shared-scratchpad"
        rationale.append("shared drafting is lighter than a PR and should not invade private lanes")
    elif args.shared_draft:
        recommendation = "shared-scratchpad"
        rationale.append("explicit shared draft requested")
    elif args.kind == "code" and args.repos > 1:
        recommendation = "feature-lane"
        rationale.append("cross-repo implementation needs one named task context")
    elif args.kind == "code":
        recommendation = "feature-lane"
        rationale.append("private implementation should start in an isolated lane")
    else:
        recommendation = "feature-lane"
        rationale.append("default safe choice is an isolated lane")

    print("gr2 prototype surface recommendation")
    print(f"recommended: {recommendation}")
    print(f"why: {'; '.join(rationale)}")
    print("rules:")
    print("  - use a review lane for formal PR inspection")
    print("  - use a shared scratchpad for collaborative drafting")
    print("  - use a feature lane for implementation work")
    return 0


def next_step(args: argparse.Namespace) -> int:
    workspace_root = args.workspace_root.resolve()
    lane_doc = load_lane_doc(workspace_root, args.owner_unit, args.lane_name)
    print("gr2 prototype next-step")
    print(f"lane: {args.owner_unit}/{lane_doc['lane_name']}")
    print(f"type: {lane_doc['lane_type']}")
    print(f"repos: {', '.join(lane_doc['repos'])}")
    if lane_doc.get("pr_associations"):
        print("mode: review")
        print("recommended:")
        print(
            f"  python3 gr2/prototypes/lane_workspace_prototype.py plan-exec {workspace_root} {args.owner_unit} {args.lane_name} 'cargo test'"
        )
        print("  inspect the review lane, then return to your feature or home lane")
    elif lane_doc["lane_type"] == "feature":
        print("mode: feature")
        print("recommended:")
        print(
            f"  python3 gr2/prototypes/lane_workspace_prototype.py plan-exec {workspace_root} {args.owner_unit} {args.lane_name} 'cargo test'"
        )
        print(
            f"  python3 gr2/prototypes/lane_workspace_prototype.py list-shared-scratchpads {workspace_root}"
        )
    else:
        print("mode: general")
        print("recommended:")
        print(
            f"  python3 gr2/prototypes/lane_workspace_prototype.py show-lane {workspace_root} {args.owner_unit} {args.lane_name}"
        )
    return 0


def plan_exec(args: argparse.Namespace) -> int:
    workspace_root = args.workspace_root.resolve()
    lane_doc = load_lane_doc(workspace_root, args.owner_unit, args.lane_name)
    leases = load_lane_leases(workspace_root, args.owner_unit, args.lane_name)
    active_conflicts, stale_conflicts = conflicting_leases(leases, "agent:exec-planner", "exec")
    if active_conflicts:
        payload = {
            "status": "blocked",
            "reason": "conflicting-active-lease",
            "lane": lane_doc["lane_name"],
            "owner_unit": lane_doc["owner_unit"],
            "requested_mode": "exec",
            "conflicting_leases": active_conflicts,
        }
        if args.json:
            print(json.dumps(payload, indent=2))
        else:
            print("gr2 lane-exec prototype")
            print("status=blocked reason=conflicting-active-lease")
            for lease in active_conflicts:
                print(
                    f"conflict: actor={lease['actor']} mode={lease['mode']} acquired_at={lease['acquired_at']}"
                )
        return 1
    if stale_conflicts:
        payload = {
            "status": "blocked",
            "reason": "stale-conflicting-lease",
            "lane": lane_doc["lane_name"],
            "owner_unit": lane_doc["owner_unit"],
            "requested_mode": "exec",
            "conflicting_leases": stale_conflicts,
            "hint": "break stale leases with acquire-lane-lease --force or clean them up first",
        }
        if args.json:
            print(json.dumps(payload, indent=2))
        else:
            print("gr2 lane-exec prototype")
            print("status=blocked reason=stale-conflicting-lease")
            for lease in stale_conflicts:
                print(
                    f"stale-conflict: actor={lease['actor']} mode={lease['mode']} expires_at={lease.get('expires_at', '-')}"
                )
        return 1

    selected_repos = lane_doc["repos"]
    if args.repos:
        requested = parse_repo_list(args.repos)
        selected_repos = [repo for repo in selected_repos if repo in requested]

    command_argv = shlex.split(args.command_text)
    rows = []
    for repo in selected_repos:
        rows.append(
            {
                "lane": lane_doc["lane_name"],
                "owner_unit": lane_doc["owner_unit"],
                "repo": repo,
                "branch": lane_doc["branch_map"].get(repo),
                "cwd": str(
                    lane_dir(workspace_root, args.owner_unit, args.lane_name)
                    / "repos"
                    / repo
                ),
                "command": command_argv,
                "shared_context_roots": lane_doc.get("context", {}).get("shared_roots", []),
                "private_context_roots": lane_doc.get("context", {}).get("private_roots", []),
                "fail_fast": lane_doc["exec_defaults"]["fail_fast"],
            }
        )

    if args.json:
        print(json.dumps(rows, indent=2))
    else:
        print("gr2 lane-exec prototype")
        print(
            f"owner={lane_doc['owner_unit']} lane={lane_doc['lane_name']} type={lane_doc['lane_type']} fail_fast={lane_doc['exec_defaults']['fail_fast']}"
        )
        print("LANE\tREPO\tBRANCH\tCWD\tCOMMAND")
        for row in rows:
            print(
                f"{row['lane']}\t{row['repo']}\t{row['branch']}\t{row['cwd']}\t{' '.join(row['command'])}"
            )
    return 0


def main() -> int:
    args = parse_args()
    if args.command == "create-lane":
        return create_lane(args)
    if args.command == "enter-lane":
        outcome = enter_lane(args)
        print(json.dumps(outcome.as_dict(), indent=2))
        return outcome.exit_code
    if args.command == "exit-lane":
        outcome = exit_lane(args)
        print(json.dumps(outcome.as_dict(), indent=2))
        return outcome.exit_code
    if args.command == "current-lane":
        return current_lane(args)
    if args.command == "lane-history":
        return lane_history(args)
    if args.command == "create-review-lane":
        return create_review_lane(args)
    if args.command == "share-lane":
        return share_lane(args)
    if args.command == "create-continuation-lane":
        return create_continuation_lane(args)
    if args.command == "plan-handoff":
        return plan_handoff(args)
    if args.command == "show-lane":
        return show_lane(args)
    if args.command == "list-lanes":
        return list_lanes(args)
    if args.command == "next-step":
        return next_step(args)
    if args.command == "plan-exec":
        return plan_exec(args)
    if args.command == "acquire-lane-lease":
        return acquire_lane_lease(args)
    if args.command == "release-lane-lease":
        return release_lane_lease(args)
    if args.command == "show-lane-leases":
        return show_lane_leases(args)
    if args.command == "create-shared-scratchpad":
        return create_shared_scratchpad(args)
    if args.command == "show-shared-scratchpad":
        return show_shared_scratchpad(args)
    if args.command == "list-shared-scratchpads":
        return list_shared_scratchpads(args)
    if args.command == "audit-shared-scratchpads":
        return audit_shared_scratchpads(args)
    if args.command == "plan-promote-scratchpad":
        return plan_promote_scratchpad(args)
    if args.command == "recommend-surface":
        return recommend_surface(args)
    if args.command == "check-review-requirements":
        return check_review_requirements(args)
    raise SystemExit(f"unknown command: {args.command}")


if __name__ == "__main__":
    sys.exit(main())
