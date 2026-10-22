"""gr2 review bind: a rows-json row carrying a key bind does not read is refused, naming it."""

from __future__ import annotations

import json
import re

try:
    from typer._click.utils import strip_ansi
except ImportError:  # older Typer
    from click.utils import strip_ansi

from tests.test_review_bind_range_patch_cap import _bind, _member, _objects
from tests.test_review_publish_all import git, REVIEW_REF_PREFIX

_ALLOWED = "['base', 'body', 'head', 'key', 'path', 'range', 'ref', 'remote', 'source', 'title', 'visibility']"


def _with(tmp_path, **extra):
    author, _patch = _member(tmp_path)
    row = json.loads((tmp_path / "rows.json").read_text())[0]
    row.update(extra)
    (tmp_path / "rows.json").write_text(json.dumps([row]))
    return author, row


def _refused(tmp_path, monkeypatch, author):
    before = _objects(author)
    out = _bind(tmp_path, monkeypatch, author)
    assert out.exit_code == 2, out.output
    assert git(author, "for-each-ref", "--format=%(refname)", REVIEW_REF_PREFIX) == ""
    assert _objects(author) == before
    return out.output


def test_range_patch_in_place_of_range_is_refused_naming_both(tmp_path, monkeypatch):
    author, _row = _with(tmp_path, range_patch="From x\n")
    out = _refused(tmp_path, monkeypatch, author)
    assert f"rows-json entry 'm0': unknown field(s) ['range_patch']; allowed: {_ALLOWED}" in out, out


def test_two_unknown_keys_are_both_named_sorted(tmp_path, monkeypatch):
    author, _row = _with(tmp_path, zeta="1", titel="typo of title")
    out = _refused(tmp_path, monkeypatch, author)
    assert "rows-json entry 'm0': unknown field(s) ['titel', 'zeta']; allowed: " in out, out


def test_row_with_every_allowed_key_still_binds(tmp_path, monkeypatch):
    author, _row = _with(tmp_path, title="t", body="b", range="", visibility="private")
    out = _bind(tmp_path, monkeypatch, author)
    assert out.exit_code == 0, out.output
    assert git(author, "for-each-ref", "--format=%(refname)", REVIEW_REF_PREFIX) != ""


def test_repo_in_place_of_key_keeps_the_named_by_key_hint(tmp_path, monkeypatch):
    author, row = _with(tmp_path)
    row["repo"] = row.pop("key")
    (tmp_path / "rows.json").write_text(json.dumps([row]))
    out = _bind(tmp_path, monkeypatch, author)
    assert out.exit_code == 2, out.output
    # the hint rides a Rich panel that wraps, and under CI (GITHUB_ACTIONS) rich colours it: read it as plain text
    flat = " ".join(re.sub("[\u2500-\u257f]", " ", strip_ansi(out.output)).split())
    assert 'the member is named by "key", not "repo"' in flat, out.output
    assert "unknown field" not in out.output, out.output


def test_unknown_key_message_is_one_plain_line_at_80_columns(tmp_path, monkeypatch):
    monkeypatch.setenv("COLUMNS", "80")
    author, _row = _with(tmp_path, range_patch="From x\n")
    out = _refused(tmp_path, monkeypatch, author)
    lines = [ln for ln in out.splitlines() if "unknown field" in ln]
    assert lines == [f"rows-json entry 'm0': unknown field(s) ['range_patch']; allowed: {_ALLOWED}"], out
