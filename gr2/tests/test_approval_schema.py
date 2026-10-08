"""Offline schema copies match the ratified upstream package commits."""
from __future__ import annotations

from pathlib import Path

import pytest

from gr2.python_cli import approval_schema as approval


def test_approval_package_reports_the_upstream_pin():
    assert approval.SCHEMA_COMMIT == "102a5e0d55bbcc8beddabc9834fafbf1660b52b3"
    assert approval.SCHEMA_SOURCE == f"{approval.PACKAGE}@{approval.SCHEMA_COMMIT}"


@pytest.mark.parametrize("name,call", [
    ("approval.proto", approval.proto_bytes),
    ("descriptor_set.pb", approval.descriptor_bytes),
])
def test_changed_approval_resource_is_refused(monkeypatch, name, call):
    root = Path(approval.__file__).resolve().parent.parent / "schemas" / approval.PACKAGE
    raw = (root / name).read_bytes()

    class Changed:
        def __truediv__(self, item):
            return self

        def read_bytes(self):
            return raw + b"changed"

    monkeypatch.setattr(approval.importlib.resources, "files", lambda _: Changed())
    with pytest.raises(ValueError, match="hash mismatch"):
        call()
