"""The adapter protocol's changelog ships in the package and names everything an adapter implements or receives.

A new protocol method, keyword or field with no entry fails here, so the changelog cannot fall behind the code
the way the absent one did (an out-of-tree adapter author found the protocol had changed with no record).
"""
from __future__ import annotations

import dataclasses
import inspect
import re
import tomllib
from pathlib import Path

import pytest

from gr2.python_cli import platform

CHANGELOG = "PLATFORM_ADAPTER_CHANGELOG.md"
# What an adapter implements (the protocol) or receives and returns (these types).
TYPES = (platform.CreatePRRequest, platform.PRRef, platform.PRStatus, platform.PRDetail, platform.PRReview,
         platform.PRCheck, platform.MergeReceipt)


def _text() -> str:
    # The file beside the gr2 package source. (This suite's conftest replaces `gr2` with a module that has no
    # import spec, so importlib.resources cannot anchor on it here; the installed read is proved by a built wheel.)
    return (Path(platform.__file__).resolve().parents[1] / CHANGELOG).read_text()


def _entries() -> list[str]:
    """Each bullet, or each plain paragraph, as one entry: a name counts only where its owner is named."""
    entries, current = [], []
    for line in _text().splitlines():
        if line.startswith("- ") or not line.strip() or line.startswith("#"):
            if current:
                entries.append(" ".join(current))
            current = [line] if line.startswith("- ") else []
        else:
            current.append(line)
    if current:
        entries.append(" ".join(current))
    return entries


def _owned(owner: str, name: str) -> bool:
    """`owner.name` anywhere, or `owner` and `name` in code spans of the same entry (prose words never count)."""
    def in_code(word, entry):
        return any(re.search(rf"\b{word}\b", span) for span in re.findall(r"`([^`]+)`", entry))
    return f"`{owner}.{name}`" in _text() or any(in_code(owner, e) and in_code(name, e) for e in _entries())


def _protocol_names():
    """Each method, and each keyword-only parameter as (method, keyword)."""
    for name, member in vars(platform.PlatformAdapter).items():
        if callable(member) and not name.startswith("_"):
            yield name, None
            for p in list(inspect.signature(member).parameters.values())[1:]:
                if p.kind is p.KEYWORD_ONLY:
                    yield name, p.name


def test_the_changelog_is_package_data():
    data = tomllib.loads((Path(platform.__file__).resolve().parents[2] / "pyproject.toml").read_text())
    assert CHANGELOG in data["tool"]["setuptools"]["package-data"]["gr2"]
    assert _text().startswith("# gr2 platform adapter protocol")


@pytest.mark.parametrize("method,keyword", sorted(set(_protocol_names()), key=str))
def test_every_protocol_method_and_keyword_is_named(method, keyword):
    """A method by name; a keyword only in an entry that also names its own method."""
    if keyword is None:
        assert re.search(rf"\b{method}\b", _text()), method
    else:
        assert _owned(method, keyword), f"{method}({keyword}=...)"


# The fields as first published in 2.0.0a1 (measured from that tag). Any field added since needs an entry.
FIRST_PUBLISHED = {
    "CreatePRRequest.base_branch", "CreatePRRequest.body", "CreatePRRequest.draft", "CreatePRRequest.head_branch",
    "CreatePRRequest.repo", "CreatePRRequest.title", "MergeReceipt.commit_sha", "MergeReceipt.observed",
    "MergeReceipt.requested", "MergeReceipt.requested_method", "PRCheck.conclusion", "PRCheck.details_url",
    "PRCheck.name", "PRCheck.status", "PRRef.base_branch", "PRRef.head_branch", "PRRef.number", "PRRef.repo",
    "PRRef.title", "PRRef.url", "PRStatus.checks", "PRStatus.mergeable", "PRStatus.ref", "PRStatus.state",
}
ADDED = sorted({f"{t.__name__}.{f.name}" for t in TYPES for f in dataclasses.fields(t)} - FIRST_PUBLISHED)


@pytest.mark.parametrize("field", ADDED)
def test_every_field_added_since_the_first_alpha_is_named(field):
    """Named as `Type.field`, or as `field` in an entry that names its own type (not another type's)."""
    type_name, name = field.split(".")
    assert _owned(type_name, name), field


def test_the_api_version_in_the_changelog_is_the_code_s():
    assert f"`PLATFORM_ADAPTER_API_VERSION = {platform.PLATFORM_ADAPTER_API_VERSION}`" in _text()


def test_a_field_named_only_under_another_type_does_not_count(monkeypatch):
    """PRReview.state must not keep PRDetail.state's row green (both types have a `state`)."""
    before = "`PRDetail` carries `ref`, `state`,"
    text = _text()
    assert text.count(before) == 1 and "`PRReview`**, one review on a PR: `user` and `state`" in text
    changed = text.replace(before, "`PRDetail` carries `ref`,")
    monkeypatch.setitem(globals(), "_text", lambda: changed)
    assert not _owned("PRDetail", "state") and _owned("PRReview", "state")


def test_a_keyword_named_only_under_another_method_does_not_count(monkeypatch):
    """`head_branch` is a CreatePRRequest field too; list_prs's keyword must be named in list_prs's own entry."""
    text = _text()
    changed = text.replace("`list_prs(repo, *, head_branch=None)`", "`list_prs(repo)`")
    assert changed != text
    monkeypatch.setitem(globals(), "_text", lambda: changed)
    assert not _owned("list_prs", "head_branch")
