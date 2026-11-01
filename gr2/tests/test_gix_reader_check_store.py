"""The optional gr2_gix reader answers the check store's reads exactly as git does.

Rows (a), (b4) and (c1)-(c5) need the extension and skip without it; rows (b1)-(b3) and (b5)-(b7), the fallback, run everywhere."""
import importlib
import json
import os
import subprocess
import sys
import types
from pathlib import Path

import pytest

from gr2.python_cli import check_records, review_field_tree
from gr2.python_cli.review_field_tree import BatchReader, GixReader, ReviewRecordError


def _git(repo, *args, data=None):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, input=data).stdout


@pytest.fixture
def store_repos(tmp_path):
    """A bare remote holding one check snapshot for one head, and a reader clone that has not fetched it."""
    remote, writer, reader = tmp_path / "remote.git", tmp_path / "writer", tmp_path / "reader"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    subprocess.run(["git", "init", "-q", str(writer)], check=True)
    _git(writer, "config", "user.name", "Gix rows")
    _git(writer, "config", "user.email", "gix-rows@example.invalid")
    (writer / "input").write_text("H\n")
    (writer / "sub").mkdir()
    (writer / "sub" / "deep").write_text("D\n")
    _git(writer, "add", "-A")
    # a name that is not UTF-8: ls-tree must carry its bytes unchanged. It goes into the index by plumbing, never
    # the filesystem, since APFS refuses a name that is not UTF-8 (EILSEQ).
    blob = subprocess.run(["git", "-C", str(writer), "hash-object", "-w", "--stdin"], input=b"latin-1 name\n",
                          check=True, capture_output=True).stdout.decode().strip()
    subprocess.run([b"git", b"-C", os.fsencode(writer), b"update-index", b"--add", b"--cacheinfo",
                    b"100644," + blob.encode() + b",caf\xe9"], check=True)
    _git(writer, "commit", "-q", "-m", "fixture")
    head = _git(writer, "rev-parse", "HEAD").decode().strip()
    _git(writer, "push", "-q", str(remote), "HEAD:refs/heads/main")
    check_records.run_check(writer, str(remote), head, "test", [sys.executable, "-c", "pass"])
    subprocess.run(["git", "clone", "-q", "--no-checkout", "--branch", "main", str(remote), str(reader)], check=True)
    return dict(remote=remote, writer=writer, reader=reader, head=head)


def _read(repos, gix: str, monkeypatch):
    monkeypatch.setenv("GR2_GIX", gix)
    member = {"path": repos["reader"], "key": "repo", "remote": str(repos["remote"])}
    return check_records.read_remote_check(str(repos["remote"]), member, repos["head"])


def _gix_or_skip():
    return pytest.importorskip("gr2_gix", reason="the optional gr2_gix extension is not installed")


def test_a_read_remote_check_is_byte_equal_under_both_readers(store_repos, monkeypatch):
    _gix_or_skip()
    monkeypatch.setenv("GR2_GIX", "1")
    with check_records._Store(store_repos["reader"]) as store:
        assert isinstance(store._reader, GixReader)
    by_gix = json.dumps(_read(store_repos, "1", monkeypatch), sort_keys=True).encode()
    by_git = json.dumps(_read(store_repos, "0", monkeypatch), sort_keys=True).encode()
    assert by_gix == by_git
    assert json.loads(by_gix)["status"] == "pass"


def test_a_check_show_cli_bytes_equal_under_both_readers(store_repos):
    _gix_or_skip()
    argv = [sys.executable, "-m", "gr2.python_cli.app", "check", "show", str(store_repos["reader"]),
            "--remote", str(store_repos["remote"]), "--head", store_repos["head"], "--json"]
    runs = [subprocess.run(argv, capture_output=True, env={**os.environ, "GR2_GIX": gix}) for gix in ("1", "0")]
    assert [(r.returncode, r.stdout, r.stderr) for r in runs[:1]] == [(r.returncode, r.stdout, r.stderr) for r in runs[1:]]
    assert runs[0].returncode == 0


LINE = "gr2: fast git reader unavailable; using plain git ("


def _lines(capsys):
    err = capsys.readouterr().err
    return [line for line in err.splitlines() if line.startswith(LINE)], err


def _absent(monkeypatch):
    monkeypatch.setitem(sys.modules, "gr2_gix", None)  # `import gr2_gix` now raises ModuleNotFoundError
    monkeypatch.setattr(review_field_tree, "_GIX_NOTED", [])


def test_b1_not_installed_is_silent_and_answers_the_same(store_repos, monkeypatch, capsys):
    _absent(monkeypatch)
    monkeypatch.delenv("GR2_GIX", raising=False)
    member = {"path": store_repos["reader"], "key": "repo", "remote": str(store_repos["remote"])}
    first = check_records.read_remote_check(str(store_repos["remote"]), member, store_repos["head"])
    lines, err = _lines(capsys)
    assert lines == [], err
    assert first == _read(store_repos, "0", monkeypatch)
    assert first["status"] == "pass"


def test_b2_asked_for_but_not_installed_says_one_line_once(store_repos, monkeypatch, capsys):
    _absent(monkeypatch)
    first = _read(store_repos, "1", monkeypatch)
    second = _read(store_repos, "1", monkeypatch)
    lines, err = _lines(capsys)
    assert len(lines) == 1, err
    assert first == second == _read(store_repos, "0", monkeypatch)


def test_b3_installed_but_broken_says_one_line(store_repos, monkeypatch, capsys, tmp_path):
    broken = tmp_path / "broken" / "gr2_gix"
    broken.mkdir(parents=True)
    (broken / "__init__.py").write_text("raise ImportError('gr2_gix built for another interpreter')\n")
    monkeypatch.syspath_prepend(str(broken.parent))
    monkeypatch.delitem(sys.modules, "gr2_gix", raising=False)
    monkeypatch.setattr(review_field_tree, "_GIX_NOTED", [])
    monkeypatch.delenv("GR2_GIX", raising=False)
    assert type(review_field_tree.reader_for(store_repos["reader"])) is BatchReader
    lines, err = _lines(capsys)
    assert len(lines) == 1 and "another interpreter" in lines[0], err


def test_b4_installed_but_cannot_open_says_one_line(monkeypatch, capsys, tmp_path):
    _gix_or_skip()
    monkeypatch.setattr(review_field_tree, "_GIX_NOTED", [])
    monkeypatch.delenv("GR2_GIX", raising=False)
    assert type(review_field_tree.reader_for(tmp_path / "not-a-repo")) is BatchReader
    lines, err = _lines(capsys)
    assert len(lines) == 1 and "cannot open" in lines[0], err


class _UnreadableStore:
    """A stand-in extension whose store reads fail as an unreadable objects directory would (OSError)."""

    def kind(self, oid):
        raise OSError("objects directory unreadable")

    def rev_parse_ref(self, name):
        raise OSError("objects directory unreadable")


def test_b5_a_store_read_error_asks_git_instead_of_raising(store_repos):
    repo = store_repos["reader"]
    r = GixReader(repo, _UnreadableStore())
    head = store_repos["head"]
    assert r.kind(head) is None  # None means "ask git", never "absent"
    assert r.rev_parse_ref("refs/heads/main") is None
    r.close()


class PanicException(BaseException):
    """Stands in for pyo3_runtime.PanicException, the type a Rust panic inside the extension reaches Python as."""


PanicException.__module__ = "pyo3_runtime"


class _PanickingStore:
    """A stand-in extension whose every read raises the given exception type."""

    def __init__(self, exc):
        self._exc = exc

    def object_format(self):
        raise self._exc("object_format")

    def kind(self, oid):
        raise self._exc("kind")

    def rev_parse_ref(self, name):
        raise self._exc("rev_parse_ref")

    def ls_tree(self, root):
        raise self._exc("ls_tree")

    def header(self, oid):
        raise self._exc("header")


def test_b6_a_panic_inside_the_extension_says_one_line_and_asks_git(store_repos, monkeypatch, capsys):
    monkeypatch.setattr(review_field_tree, "_GIX_NOTED", [])
    repo, head = store_repos["reader"], store_repos["head"]
    blob = _git(repo, "rev-parse", "HEAD:input").decode().strip()
    by_git = BatchReader(repo)
    want_tree, want_blob = by_git.ls_tree(head), by_git.blob(blob)
    by_git.close()
    r = GixReader(repo, _PanickingStore(PanicException))
    assert r.ls_tree(head) == want_tree
    assert r.blob(blob) == want_blob
    assert r.kind(head) is None and r.rev_parse_ref("refs/heads/main") is None
    r.close()
    monkeypatch.setattr(check_records, "reader_for", lambda repo: GixReader(repo, _PanickingStore(PanicException)))
    with check_records._Store(repo) as store:
        assert store.size() == 40  # object_format panicked: the size comes from git
    lines, err = _lines(capsys)
    assert len(lines) == 1 and "panicked" in lines[0], err
    # anything that is not the extension's panic passes through: an interrupt never becomes a fallback
    other = GixReader(repo, _PanickingStore(KeyboardInterrupt))
    with pytest.raises(KeyboardInterrupt):
        other.kind(head)
    other.close()


def test_b7_a_panic_while_opening_says_one_line_and_asks_git(store_repos, monkeypatch, capsys):
    def opening(exc_type):
        def reader(path):
            raise exc_type("opening panicked")
        return types.SimpleNamespace(Reader=reader)

    monkeypatch.setattr(review_field_tree, "_GIX_NOTED", [])
    monkeypatch.delenv("GR2_GIX", raising=False)
    monkeypatch.setitem(sys.modules, "gr2_gix", opening(PanicException))
    assert type(review_field_tree.reader_for(store_repos["reader"])) is BatchReader
    lines, err = _lines(capsys)
    assert len(lines) == 1 and "panicked" in lines[0], err
    # an interrupt while opening is never turned into a fallback
    monkeypatch.setitem(sys.modules, "gr2_gix", opening(KeyboardInterrupt))
    with pytest.raises(KeyboardInterrupt):
        review_field_tree.reader_for(store_repos["reader"])


def test_c1_object_format_equal(store_repos, tmp_path):
    gix = _gix_or_skip()
    assert GixReader(store_repos["reader"], gix.Reader(str(store_repos["reader"]))).object_format() == \
        _git(store_repos["reader"], "rev-parse", "--show-object-format").decode().strip()
    sha256 = tmp_path / "sha256"
    subprocess.run(["git", "init", "-q", "--object-format=sha256", str(sha256)], check=True)
    # a sha256 repository: gix either answers sha256 or cannot open it, and then the store reads with git
    with check_records._Store(sha256) as store:
        assert store.size() == 64


def test_c2_kind_equal(store_repos):
    gix = _gix_or_skip()
    repo = store_repos["reader"]
    r = GixReader(repo, gix.Reader(str(repo)))
    tree = _git(repo, "rev-parse", "HEAD^{tree}").decode().strip()
    blob = _git(repo, "rev-parse", "HEAD:input").decode().strip()
    for oid in (store_repos["head"], tree, blob):
        assert r.kind(oid) == _git(repo, "cat-file", "-t", oid).decode().strip()
    assert r.kind("0" * 39 + "1") is None
    assert r.kind("abc") is None


def test_c3_rev_parse_ref_equal(store_repos):
    gix = _gix_or_skip()
    repo = store_repos["reader"]
    r = GixReader(repo, gix.Reader(str(repo)))
    assert r.rev_parse_ref("refs/heads/main") == _git(repo, "rev-parse", "--verify", "refs/heads/main").decode().strip()
    assert r.rev_parse_ref("refs/heads/absent") is None
    assert r.rev_parse_ref("HEAD") is None  # not a full ref name: the caller asks git


def test_c3b_a_ref_at_an_annotated_tag_answers_the_tag_unpeeled(store_repos):
    gix = _gix_or_skip()
    repo = store_repos["reader"]
    _git(repo, "-c", "user.name=Gix rows", "-c", "user.email=gix-rows@example.invalid", "tag", "-a", "-m", "t", "T")
    _git(repo, "symbolic-ref", "refs/heads/sym", "refs/heads/main")
    r = GixReader(repo, gix.Reader(str(repo)))
    tag = _git(repo, "rev-parse", "--verify", "refs/tags/T").decode().strip()
    assert _git(repo, "cat-file", "-t", tag).strip() == b"tag"
    assert r.rev_parse_ref("refs/tags/T") == tag
    assert r.rev_parse_ref("refs/heads/sym") is None  # a symbolic ref: the caller asks git


def test_c3c_a_check_ref_at_an_annotated_tag_is_refused_under_both_readers(store_repos, monkeypatch):
    _gix_or_skip()
    writer, remote, reader = store_repos["writer"], store_repos["remote"], store_repos["reader"]
    _git(writer, "tag", "-a", "-m", "not a commit", "checktag", store_repos["head"])
    _git(writer, "push", "-q", "-f", str(remote), "refs/tags/checktag:" + check_records.CHECK_REF)
    for gix in ("1", "0"):
        monkeypatch.setenv("GR2_GIX", gix)
        with check_records._Store(reader) as store:
            assert isinstance(store._reader, GixReader) == (gix == "1")
            with pytest.raises(check_records.CheckRefused, match="^head_must_be_commit$"):
                check_records._snapshot(reader, str(remote), store)
        assert _git(reader, "for-each-ref", "refs/dev.synapt.grip/__check_transfers__/") == b""


def test_c4_ls_tree_equal_including_a_non_utf8_name(store_repos):
    gix = _gix_or_skip()
    repo = store_repos["writer"]
    r = GixReader(repo, gix.Reader(str(repo)))
    tree = _git(repo, "rev-parse", "HEAD^{tree}").decode().strip()
    by_git = _git(repo, "ls-tree", "-r", "-t", "-z", tree)
    assert b"caf\xe9" in by_git
    assert r.gix_ls_tree(tree) == by_git
    snapshot = _git(repo, "rev-parse", "--verify", check_records.CHECK_REF).decode().strip()
    snap_tree = _git(repo, "rev-parse", snapshot + "^{tree}").decode().strip()
    assert r.gix_ls_tree(snap_tree) == _git(repo, "ls-tree", "-r", "-t", "-z", snap_tree)
    # the check store passes the snapshot COMMIT: gix itself must answer it (None would mean git answered)
    by_gix = r.gix_ls_tree(snapshot)
    assert by_gix is not None and by_gix == _git(repo, "ls-tree", "-r", "-t", "-z", snapshot)
    with check_records._Store(repo) as store:
        assert store.ls_tree(snapshot) == _git(repo, "ls-tree", "-r", "-t", "-z", snapshot)


def _refusal(reader, oid):
    try:
        reader.blob(oid)
    except ReviewRecordError as exc:
        return str(exc)
    finally:
        reader.close()
    return None


def test_c5_blob_equal_and_refusals_equal(store_repos, monkeypatch):
    gix = _gix_or_skip()
    repo = store_repos["writer"]
    blob = _git(repo, "rev-parse", "HEAD:input").decode().strip()
    tree = _git(repo, "rev-parse", "HEAD^{tree}").decode().strip()
    new = lambda: GixReader(repo, gix.Reader(str(repo)))
    with new() as r, BatchReader(repo) as b:
        assert r.blob(blob) == b.blob(blob) == b"H\n"
    for oid in (tree, blob[:12], "0" * 39 + "1"):
        assert _refusal(new(), oid) == _refusal(BatchReader(repo), oid)
        assert _refusal(new(), oid) is not None
    monkeypatch.setattr(review_field_tree, "MAX_BLOB_BYTES", 1)
    assert _refusal(new(), blob) == _refusal(BatchReader(repo), blob)
    assert "over the 1 byte limit" in _refusal(new(), blob)
