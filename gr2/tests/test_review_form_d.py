"""Form D review records: the build witnesses that need no ref writer. Each test
names the rule it holds."""
from __future__ import annotations

import subprocess
import zlib
from pathlib import Path

import pytest

from gr2.python_cli import review_form_d as fd


def git(repo: Path, *args: str, data: bytes | None = None) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], input=data, capture_output=True,
                          check=True).stdout.decode().strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "store.git"
    subprocess.run(["git", "init", "-q", "--bare", str(r)], check=True)
    return r


def member(key: str, **extra) -> dict:
    m = {"key": key, "path": key, "remote": f"https://example.invalid/{key}.git",
         "base": "1" * 40, "commit": "2" * 40, "remote_head": "1" * 40,
         "title": f"title {key}", "body": f"body {key}"}
    m.update(extra)
    return m


RECORD = {"schema": "grip-review-bind-v2", "kind": "review", "policy": "clean",
          "members": [member("beta", range_patch=b"From 2222 patch\n"), member("alpha")]}


def names(repo: Path, tree: str) -> list[str]:
    return [line.split("\t")[1] for line in git(repo, "ls-tree", "-r", "-t", tree).splitlines()]


def objects(repo: Path, tree: str) -> set[str]:
    return set(git(repo, "rev-list", "--objects", "--no-object-names", tree).split())


# --- the schema copy -------------------------------------------------------------

def test_vendored_schema_is_the_pinned_schemas_commit():
    s = fd.schema()
    assert {n: f.name for n, f in s["ReviewBind"].items()} == {1: "schema", 2: "kind", 3: "policy", 4: "members"}
    assert s["Member"][10].name == "title" and s["Member"][13].type == "Evidence"
    assert fd.SCHEMA_COMMIT == "eb877b7d56f6a4d039446c17a8a08c34d92cb891"
    assert fd.SCHEMA_SOURCE == f"{fd.PACKAGE}@{fd.SCHEMA_COMMIT}"


def test_schema_bytes_other_than_the_pin_are_refused(monkeypatch):
    pinned = fd._read_proto_bytes()
    monkeypatch.setattr(fd, "_schema", None)
    monkeypatch.setattr(fd, "_read_proto_bytes", lambda: pinned + b"\n")
    with pytest.raises(fd.ReviewRecordError, match="hash mismatch"):
        fd.schema()


def test_field_number_above_999_is_refused_in_the_schema_and_at_write(repo):
    with pytest.raises(fd.ReviewRecordError, match="1..999"):
        fd.parse_proto('message M {\n  string wide = 1000;\n}\n')
    assert fd.parse_proto('message M {\n  string ok = 999;\n}\n')["M"][999].name == "ok"  # control
    with pytest.raises(fd.ReviewRecordError, match="outside 1..999"):
        fd.write_tree(repo, fd._emit(1000, 2, b"x"))


# --- form D encode, decode, identity ------------------------------------------------

def test_record_round_trips_and_derives_canonical_protobuf(repo):
    tree = fd.write_record(repo, RECORD)
    assert fd.to_protobuf(repo, tree) == fd.encode(RECORD)
    back = fd.read_record(repo, tree)
    assert [m["key"] for m in back["members"]] == ["alpha", "beta"]
    assert back["members"][1]["range_patch"] == b"From 2222 patch\n"
    assert back["policy"] == "clean"


def test_entry_names_are_number_wire_type_repeat_and_writer_name(repo):
    tree = fd.write_record(repo, RECORD)
    got = names(repo, tree)
    assert got[:4] == ["001.2_schema", "002.2_kind", "003.2_policy", "004.2r_members"]
    assert "004.2r_members/0000002/008.2_range_patch" in got
    assert "004.2r_members/0000001/008.2_range_patch" not in got  # proto3: empty bytes are omitted


def test_same_record_gives_same_tree_whatever_the_member_order(repo):
    one = fd.write_record(repo, RECORD)
    flipped = {**RECORD, "members": list(reversed(RECORD["members"]))}
    assert fd.write_record(repo, flipped) == one
    changed = {**RECORD, "policy": "flagged"}
    assert fd.write_record(repo, changed) != one  # control: a value change moves the id


def test_padding_keeps_field_ten_after_field_two(repo):
    tree = fd.write_record(repo, RECORD)
    first = [n.split("/")[-1] for n in names(repo, tree) if n.startswith("004.2r_members/0000001/")]
    numbers = [int(n[:3]) for n in first]
    assert numbers == sorted(numbers) and 2 in numbers and 10 in numbers
    blob = git(repo, "hash-object", "-w", "--stdin", data=b"x")
    unpadded = git(repo, "mktree", data=f"100644 blob {blob}\t2.2_a\n100644 blob {blob}\t10.2_b\n".encode())
    assert [n.split("\t")[1] for n in git(repo, "ls-tree", unpadded).splitlines()] == ["10.2_b", "2.2_a"]  # why


# --- readers decode by number; unknown entries survive ----------------------------------

def _with_unknown_member_field(repo: Path) -> str:
    base = fd.encode(RECORD)
    out = b""
    for number, wt, payload in fd.fields(base):
        if number == 4:
            payload += fd._emit(99, 2, b"a newer writer's field")
        out += fd._emit(number, wt, payload)
    return fd.write_tree(repo, out)


def test_unknown_entry_is_kept_through_a_one_entry_edit(repo):
    newer = _with_unknown_member_field(repo)
    assert "004.2r_members/0000001/099.2" in names(repo, newer)
    assert fd.read_record(repo, newer) == fd.decode(fd.encode(RECORD))  # known fields unaffected
    before = objects(repo, newer)
    edited = fd.replace_entry(repo, newer, ["004.2", "0000001", "010.2"], b"an edited title")
    assert "004.2r_members/0000001/099.2" in names(repo, edited)
    assert b"a newer writer's field" in fd.to_protobuf(repo, edited)
    assert fd.read_record(repo, edited)["members"][0]["title"] == "an edited title"
    assert len(objects(repo, edited) - before) == 4  # blob, member, members, root


def test_a_record_written_under_other_names_reads_and_verifies_unchanged(repo):
    tree = fd.write_record(repo, RECORD)
    renamed = _rename_entry(repo, tree, "004.2r_members", "0000001", "004.2_base", "004.2_base_commit")
    assert renamed != tree
    assert fd.read_record(repo, renamed) == fd.read_record(repo, tree)
    fd.verify_tree(repo, renamed)
    # A verifier that regenerated names from the current schema would reject it:
    assert fd.write_tree(repo, fd.to_protobuf(repo, renamed)) != renamed


def _rename_entry(repo: Path, tree: str, members: str, occ: str, old: str, new: str) -> str:
    def rows(t):
        return [line.split("\t") for line in git(repo, "ls-tree", t).splitlines()]

    def mk(entries):
        return git(repo, "mktree", data="".join(f"{m}\t{n}\n" for m, n in entries).encode())

    top = []
    for meta, name in rows(tree):
        if name == members:
            occs = []
            for ometa, oname in rows(meta.split()[2]):
                if oname == occ:
                    inner = [(m, new if n == old else n) for m, n in rows(ometa.split()[2])]
                    ometa = f"040000 tree {mk(inner)}"
                occs.append((ometa, oname))
            meta = f"040000 tree {mk(occs)}"
        top.append((meta, name))
    return mk(top)


# --- verify ---------------------------------------------------------------------------

def test_verify_refuses_a_tampered_blob(repo):
    tree = fd.write_record(repo, RECORD)
    fd.verify_tree(repo, tree)  # control
    blob = git(repo, "rev-parse", f"{tree}:002.2_kind")
    path = repo / "objects" / blob[:2] / blob[2:]
    path.chmod(0o644)
    path.write_bytes(zlib.compress(b"blob 6\0review"[:-1] + b"X"))
    with pytest.raises(fd.ReviewRecordError, match="fsck"):
        fd.verify_tree(repo, tree)


def test_a_non_form_d_entry_name_is_refused(repo):
    blob = git(repo, "hash-object", "-w", "--stdin", data=b"x")
    tree = git(repo, "mktree", data=f"100644 blob {blob}\tschema\n".encode())
    with pytest.raises(fd.ReviewRecordError, match="not a form D entry name"):
        fd.read_record(repo, tree)


# --- the schema reader refuses what it cannot read ---------------------------------------

@pytest.mark.parametrize("form", [
    "  optional string a = 1;",
    "  string a = 1 [deprecated = true];",
    "  string a = 1; string b = 2;",
    "  string a = 0x1;",
    "  map<string, string> a = 1;",
    "  string a =\n    1;",
    "  message Inner {\n  }",
    "  string a = 1000;",
])
def test_every_unread_schema_form_is_refused(form):
    with pytest.raises(fd.ReviewRecordError):
        fd.parse_proto('syntax = "proto3";\npackage p.v1;\nmessage M {\n' + form + '\n}\n')
    assert fd.parse_proto('syntax = "proto3";\npackage p.v1;\nmessage M {\n  string a = 1;\n}\n')["M"][1].name == "a"


def test_a_schema_that_ends_inside_a_message_is_refused():
    with pytest.raises(fd.ReviewRecordError, match="ends inside a message"):
        fd.parse_proto("message M {\n  string a = 1;\n")


def test_a_one_line_message_is_refused():
    with pytest.raises(fd.ReviewRecordError):
        fd.parse_proto("message M { string a = 1; }\n")


# --- verify refuses every tree that is not form D ------------------------------------------

def _mk(repo: Path, rows: list[tuple[str, str, str, str]]) -> str:
    data = b"".join(f"{m} {k} {o}\t".encode() + n.encode() + b"\0" for m, k, o, n in rows)
    return subprocess.run(["git", "-C", str(repo), "mktree", "-z"], input=data, capture_output=True,
                          check=True).stdout.decode().strip()


def _blobrow(repo: Path, name: str, payload: bytes = b"x", mode: str = "100644"):
    return (mode, "blob", git(repo, "hash-object", "-w", "--stdin", data=payload), name)


def _tree(repo: Path, name: str, rows) -> tuple[str, str, str, str]:
    return ("040000", "tree", _mk(repo, rows), name)


def _depths(repo: Path):
    """The same rows at three depths: the record itself, inside a Member occurrence, and
    inside that member's Evidence. A defect must be refused wherever it sits."""
    return {
        "top level": lambda rows: _mk(repo, rows),
        "inside a Member": lambda rows: _mk(repo, [_tree(repo, "004.2r_members", [_tree(repo, "0000001", rows)])]),
        "inside Evidence": lambda rows: _mk(repo, [_tree(repo, "004.2r_members", [
            _tree(repo, "0000001", [_tree(repo, "013.2_evidence", rows)])])]),
    }


def _bad_rows(repo: Path) -> dict[str, list]:
    """Malformed classes that do not depend on which message holds them: every field
    number here is one no review message declares."""
    ok = _blobrow(repo, "050.2_ok")
    occ = lambda *names: _tree(repo, "099.2r", [_blobrow(repo, n) for n in names])
    return {
        "field 000": [ok, _blobrow(repo, "000.2_zero")],
        "unicode digits": [ok, _blobrow(repo, "\u0661\u0662\u0663.2_x")],
        "trailing newline": [ok, _blobrow(repo, "051.2_x\n")],
        "two entries for one number": [ok, _blobrow(repo, "051.2_a"), _blobrow(repo, "051.2_b")],
        "ordinal gap": [ok, occ("0000001", "0000005")],
        "ordinal newline": [ok, occ("0000001\n")],
        "executable mode": [ok, _blobrow(repo, "051.2_x", mode="100755")],
        "symlink mode": [ok, _blobrow(repo, "051.2_x", mode="120000")],
        # The tree derives to exactly four bytes, so only the "a message is wire type 2"
        # check refuses it; a tree under wire type 0 is refused by the scalar check too.
        "a tree under wire type 5": [ok, _tree(repo, "099.5_x", [ok])],
        "a tree under wire type 0": [ok, _tree(repo, "099.0_x", [ok])],
        # Scalar payloads that are not exactly one value, each beside a next field it
        # would swallow when protobuf is derived:
        "unterminated varint": [_blobrow(repo, "060.0_a", b"\x80"), _blobrow(repo, "061.0_b", b"")],
        "empty varint": [ok, _blobrow(repo, "060.0_a", b"")],
        "eleven-byte varint": [ok, _blobrow(repo, "060.0_a", b"\xff" * 10 + b"\x01")],
        "varint overflowing 64 bits": [ok, _blobrow(repo, "060.0_a", b"\xff" * 9 + b"\x02")],
        "varint with bytes after its end": [ok, _blobrow(repo, "060.0_a", b"\x01\x01")],
        "short fixed32": [_blobrow(repo, "060.5_a", b""), _blobrow(repo, "061.2_b", b"x")],
        "short fixed64": [_blobrow(repo, "060.1_a", b""), _blobrow(repo, "061.2_b", b"abcde")],
    }


def _schema_rows(repo: Path) -> dict[tuple[str, str], list]:
    """Malformed classes that break what the schema says about a known field."""
    kind = _blobrow(repo, "002.2_kind", b"review")
    single = lambda name: _tree(repo, name, [_blobrow(repo, "0000001", b"x")])
    return {
        ("top level", "r on a known single field"): [single("002.2r_kind")],
        ("top level", "known field, wrong wire type"): [_blobrow(repo, "002.0_kind", b"\x01")],
        ("top level", "known message stored as blob"): [kind, _tree(repo, "004.2r_members", [
            _blobrow(repo, "0000001", fd.encode(member("a"), "Member"))])],
        ("inside a Member", "r on a known single field"): [single("002.2r_path")],
        ("inside a Member", "known field, wrong wire type"): [_blobrow(repo, "002.0_path", b"\x01")],
        ("inside a Member", "known message stored as blob"): [
            _blobrow(repo, "013.2_evidence", fd.encode({"commands": b"make test"}, "Evidence"))],
        ("inside Evidence", "known field, wrong wire type"): [_blobrow(repo, "001.0_commands", b"\x01")],
    }


def test_verify_accepts_the_written_record_and_refuses_each_malformed_class(repo):
    fd.verify_tree(repo, fd.write_record(repo, RECORD))  # control
    fd.verify_tree(repo, _with_unknown_member_field(repo))  # control: unknown entries are form D
    depths = _depths(repo)
    ok = [_blobrow(repo, "050.2_ok")]
    cases = [(d, label, rows) for d in depths for label, rows in _bad_rows(repo).items()]
    cases += [(d, label, rows) for (d, label), rows in _schema_rows(repo).items()]
    for depth, wrap in depths.items():
        fd.verify_tree(repo, wrap(ok))  # control at this depth
    for depth, label, rows in cases:
        with pytest.raises(fd.ReviewRecordError):
            fd.verify_tree(repo, depths[depth](rows))
            pytest.fail(f"verify passed: {label}, {depth}")


def test_well_formed_scalars_of_every_wire_type_verify(repo):
    rows = [_blobrow(repo, "060.0_a", b"\x96\x01"), _blobrow(repo, "061.0_b", b"\xff" * 9 + b"\x01"),
            _blobrow(repo, "062.1_c", b"12345678"), _blobrow(repo, "063.5_d", b"1234"),
            _blobrow(repo, "064.2_e", b"")]
    tree = _mk(repo, rows)
    fd.verify_tree(repo, tree)
    assert [n for n, _, _ in fd.fields(fd.to_protobuf(repo, tree))] == [60, 61, 62, 63, 64]


def test_the_writer_refuses_a_varint_longer_than_ten_bytes(repo):
    with pytest.raises(fd.ReviewRecordError, match="not one wire type 0 value"):
        fd.write_tree(repo, fd._tag(60, 0) + b"\xff" * 10 + b"\x01")
    assert "060.0" in names(repo, fd.write_tree(repo, fd._tag(60, 0) + b"\xff" * 9 + b"\x01"))  # control


# --- edits keep what they do not touch ---------------------------------------------------------

def test_an_edit_keeps_a_siblings_mode(repo):
    tree = _mk(repo, [_blobrow(repo, "002.2_kind", b"review"), _blobrow(repo, "005.2_x", mode="100755")])
    edited = fd.replace_entry(repo, tree, ["002.2"], b"changed")
    assert "100755 blob" in [l for l in git(repo, "ls-tree", edited).splitlines() if l.endswith("005.2_x")][0]


def test_an_edit_beside_a_non_utf8_name_keeps_it(repo):
    blob = git(repo, "hash-object", "-w", "--stdin", data=b"x")
    data = f"100644 blob {blob}\t002.2_kind".encode() + b"\0" + f"100644 blob {blob}\t".encode() + b"051.2_\xff\0"
    tree = subprocess.run(["git", "-C", str(repo), "mktree", "-z"], input=data, capture_output=True,
                          check=True).stdout.decode().strip()
    edited = fd.replace_entry(repo, tree, ["002.2"], b"changed")
    raw = subprocess.run(["git", "-C", str(repo), "ls-tree", "-z", "--name-only", edited], capture_output=True,
                         check=True).stdout
    assert b"051.2_\xff" in raw.split(b"\0")


def test_an_edit_that_matches_more_than_one_entry_is_refused(repo):
    tree = _mk(repo, [_blobrow(repo, "002.2_kind", b"a"), _blobrow(repo, "002.2_kind_again", b"b")])
    with pytest.raises(fd.ReviewRecordError, match="matches 2 entries"):
        fd.replace_entry(repo, tree, ["002.2"], b"changed")


def test_rewriting_from_the_decoded_view_loses_unknown_entries(repo):
    """Why replace_entry is the edit path: the decoded dict is a view of known fields."""
    newer = _with_unknown_member_field(repo)
    rewritten = fd.write_record(repo, fd.read_record(repo, newer))
    assert not any(n.endswith("/099.2") for n in names(repo, rewritten))
    assert any(n.endswith("/099.2") for n in names(repo, newer))


# --- every wire type round-trips by number ----------------------------------------------------

def test_varint_and_fixed_width_unknown_fields_round_trip(repo):
    extra = fd._emit(20, 0, fd._varint(300)) + fd._emit(21, 1, (7).to_bytes(8, "little")) \
        + fd._emit(22, 5, (9).to_bytes(4, "little"))
    buf = fd.encode(RECORD) + extra
    tree = fd.write_tree(repo, buf)
    assert {"020.0", "021.1", "022.5"} <= set(names(repo, tree))
    assert fd.to_protobuf(repo, tree) == buf
    fd.verify_tree(repo, tree)
