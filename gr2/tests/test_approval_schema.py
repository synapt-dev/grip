"""Offline schema copies match the ratified upstream package commits."""
from __future__ import annotations

import hashlib
import importlib.resources

import pytest

from gr2.python_cli import approval_schema as approval
from gr2.python_cli import review_field_tree as review


def test_review_descriptor_and_proto_match_the_upstream_pin():
    root = importlib.resources.files("gr2") / "schemas" / review.PACKAGE
    assert hashlib.sha256((root / "review.proto").read_bytes()).hexdigest() == (
        "94e75f8ff703a20a2ab2cecd5226c24d25e9f8c1a1d295cdaf224ab0a2e73296")
    assert hashlib.sha256((root / "descriptor_set.pb").read_bytes()).hexdigest() == (
        "2be436c341c510e53f50ad53cd6b245f0f2d572dcfca595a1f6424f62c863390")


def test_approval_package_and_descriptor_match_the_upstream_pin():
    assert approval.SCHEMA_COMMIT == "102a5e0d55bbcc8beddabc9834fafbf1660b52b3"
    assert approval.SCHEMA_SOURCE == f"{approval.PACKAGE}@{approval.SCHEMA_COMMIT}"
    assert hashlib.sha256(approval.proto_bytes()).hexdigest() == (
        "6d81e75318e40f5834ad942eb6c36c9d392ca3bead667012a971dd8ec45fc375")
    assert hashlib.sha256(approval.descriptor_bytes()).hexdigest() == (
        "a2500edaf8c246ff6c8650c00d01b1f4b49b50e0528f942b51ab01ba7b8865eb")


@pytest.mark.parametrize("name,call", [
    ("approval.proto", approval.proto_bytes),
    ("descriptor_set.pb", approval.descriptor_bytes),
])
def test_changed_approval_resource_is_refused(monkeypatch, name, call):
    root = importlib.resources.files("gr2") / "schemas" / approval.PACKAGE
    raw = (root / name).read_bytes()

    class Changed:
        def __truediv__(self, item):
            return self

        def read_bytes(self):
            return raw + b"changed"

    monkeypatch.setattr(approval.importlib.resources, "files", lambda _: Changed())
    with pytest.raises(ValueError, match="hash mismatch"):
        call()
