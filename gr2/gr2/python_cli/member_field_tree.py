"""A full-mode member's record as a field tree (package ``dev.synapt.grip.field.v1alpha1``).

The record is a Git tree with one entry per protobuf field, named ``NNN.W_name`` exactly as the review records are
(see ``review_field_tree``): the field number zero-padded to three digits, the wire type, the field name from the
writer's schema. Scalars are stored as their raw protobuf payload bytes, and a field at its proto3 default is absent.

A FIELD-COMMIT is a commit whose tree is that record. The grip tree's entry for the member is a gitlink to it, and the
grip commit carries it as a parent, so every plain clone and pull holds the record while git materializes nothing at the
member's path.

Field numbers are the field schema's (package v1alpha1, named above). This module
writes and reads the fields a covered pin needs: schema (1), kind (2), pin (3), url (4), ref (5), covered_by (6),
ref_defaulted (12) and covered_test (13). Layers, hooks, records and config are not written yet; a reader that meets
an entry it does not know ignores it, and refuses a tree whose schema entry names another package.
"""
from __future__ import annotations

import re
from pathlib import Path

from . import review_field_tree as rft

PACKAGE = "dev.synapt.grip.field.v1alpha1"

KIND_REPO, KIND_GRIP = 1, 2
COVER_REACHABLE, COVER_LANDED_BY_PATCH = 1, 2

# number -> (name, wire type, record key). Wire type 2 is a string, 0 a varint.
_FIELDS = {
    1: ("schema", 2, "schema"),
    2: ("kind", 0, "kind"),
    3: ("pin", 2, "pin"),
    4: ("url", 2, "url"),
    5: ("ref", 2, "ref"),
    6: ("covered_by", 2, "covered_by"),
    12: ("ref_defaulted", 0, "ref_defaulted"),
    13: ("covered_test", 0, "covered_test"),
}


class FieldTreeError(ValueError):
    """A member record that cannot be written or read as a field tree."""


def write(repo: Path, record: dict) -> str:
    """The tree id of ``record`` (keys: kind, pin, url, ref, covered_by, ref_defaulted, covered_test; the schema entry
    is always this package). A field at its default is not written."""
    values = {**record, "schema": PACKAGE}
    entries: list[rft.Entry] = []
    for number, (name, wire, key) in sorted(_FIELDS.items()):
        value = values.get(key)
        if value in (None, "", 0, False):
            continue
        if wire == 2:
            payload = str(value).encode("utf-8")
        else:
            payload = rft._varint(int(value))
        field = rft.Field(number, name, "string" if wire == 2 else "uint64", False)
        entries.append(("100644", "blob", rft._blob(repo, payload), rft.entry_name(field, number, wire, False)))
    return rft._mktree(repo, entries)


def read(repo: Path, tree: str) -> dict:
    """The record in ``tree``, decoded by field NUMBER (the name is a hint). Refuses a tree for another package."""
    out: dict = dict(schema="", kind=0, pin="", url="", ref="", covered_by="", ref_defaulted=False, covered_test=0)
    try:
        entries = rft._ls(repo, tree)   # a tree object this clone does not hold is a git failure, not a record
    except rft.ReviewRecordError as exc:
        raise FieldTreeError(str(exc)) from exc
    for mode, kind, oid, name in entries:
        try:
            number, wire, repeated = rft.parse_entry(name)
        except rft.ReviewRecordError as exc:
            raise FieldTreeError(str(exc)) from exc
        spec = _FIELDS.get(number)
        if spec is None:
            continue
        if kind != "blob" or repeated or wire != spec[1]:
            raise FieldTreeError(f"entry {name!r} is not the {spec[0]} field this schema defines")
        payload = rft._git(repo, "cat-file", "blob", oid)
        if wire == 2:
            out[spec[2]] = payload.decode("utf-8")
        else:
            value, end = rft._read_varint(payload, 0)
            if end != len(payload):
                raise FieldTreeError(f"entry {name!r} carries bytes after its varint")
            out[spec[2]] = bool(value) if spec[2] == "ref_defaulted" else value
    if out["schema"] != PACKAGE:
        raise FieldTreeError(f"schema entry names {out['schema']!r}, not {PACKAGE!r}")
    _validate(repo, out)
    return out


_HEX_PIN = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")


def _validate(repo: Path, record: dict) -> None:
    """Refuse a record whose strings git would act on as something other than data.

    A record arrives in a commit anyone with push or PR access can write, and `materialize` hands its ref and url to
    `git fetch` and `git clone`. A ref that starts with a dash is an option (`--upload-pack=...` runs a command), so
    each field is checked here, at the read, and every reader gets the same answer."""
    from . import grip as grip_mod

    if record["pin"] and not _HEX_PIN.fullmatch(record["pin"]):
        raise FieldTreeError(f"pin {record['pin'][:80]!r} is not a full hex commit id")
    for key in ("covered_by", "ref", "url"):
        value = record[key]
        if value and (value.startswith("-") or any(ch.isspace() or ord(ch) < 32 for ch in value)):
            raise FieldTreeError(f"{key} {value[:80]!r} is shaped like an option or carries whitespace or control bytes")
    covered = record["covered_by"]
    if covered:
        try:
            rft._git(repo, "check-ref-format", covered)
            well_formed = True
        except rft.ReviewRecordError:
            well_formed = False
        if not covered.startswith("refs/heads/") or not well_formed:
            raise FieldTreeError(f"covered_by {covered[:80]!r} is not a refs/heads/ ref name")
    url = record["url"]
    if url and (url.lower().startswith("ext::") or grip_mod.url_has_credentials(url)):
        raise FieldTreeError("url uses the ext transport or carries credentials; remove them and use a credential helper")


def root_commit(repo: Path, message: str = "grip root") -> str:
    """An empty commit with no parents. The first grip commit has no previous grip commit, and git makes a commit's
    first parent whatever is listed first, so without this the first carried field-commit would be the first parent and
    `git log --first-parent` would walk into a record. This commit holds that place, so the first-parent chain is grip
    commits down to a real root."""
    tree = rft._mktree(repo, [])
    made = rft._git(repo, "-c", "user.name=gr2", "-c", "user.email=gr2@example.invalid", "commit-tree", tree, "-m", message)
    return made.decode().strip()


def commit(repo: Path, record: dict, message: str, previous: str | None = None) -> str:
    """A field-commit for ``record``. When ``previous`` (the field-commit the grip tree already holds) has this very
    tree, it is returned unchanged, so a member whose record did not move keeps the same field-commit id."""
    tree = write(repo, record)
    if previous:
        try:
            if rft._git(repo, "rev-parse", f"{previous}^{{tree}}").decode().strip() == tree:
                return previous
        except rft.ReviewRecordError:
            pass
    made = rft._git(repo, "-c", "user.name=gr2", "-c", "user.email=gr2@example.invalid", "commit-tree", tree, "-m", message)
    return made.decode().strip()
