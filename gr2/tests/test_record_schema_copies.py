"""Offline check and merge descriptors equal the pinned upstream package bytes."""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from gr2.python_cli import check_records

# Package revisions identify the upstream copy; no new JSON encoding or runtime
# descriptor validator is introduced by making these offline resources available.
COPIES = (
    (
        "dev.synapt.grip.check.v1alpha1",
        "fda5ce8bf75cc0d2dd6770b34e250634cbf5d5bd",
        "check.proto",
        "74c50d191359243cbef192d004e88f624c75faf67f4e0b06b7bcee6811fd6559",
    ),
    (
        "dev.synapt.grip.check.v1alpha1",
        "fda5ce8bf75cc0d2dd6770b34e250634cbf5d5bd",
        "descriptor_set.pb",
        "6c1442db18c84e3fe9d8a04b58a5a0b4849b556b346377cc66e6629ee73aefed",
    ),
    (
        "dev.synapt.grip.merge.v1alpha1",
        "4e8c5a00505352c97514be0633e6e1ed8f043807",
        "merge.proto",
        "fd06a2fefaeb030623a13997f7d1e2a652ce1b3458c90465aa07f530cedd37b0",
    ),
    (
        "dev.synapt.grip.merge.v1alpha1",
        "4e8c5a00505352c97514be0633e6e1ed8f043807",
        "descriptor_set.pb",
        "5b9c7aedaa658cfb8f4afdb83abc5a1f0a92d3bc084ce1951935f7050efb2878",
    ),
)


@pytest.mark.parametrize("package,revision,name,expected", COPIES)
def test_record_schema_copy_matches_upstream(package, revision, name, expected):
    root = Path(check_records.__file__).resolve().parent.parent / "schemas"
    actual = hashlib.sha256((root / package / name).read_bytes()).hexdigest()
    assert actual == expected, f"{package}@{revision}/{name}: {actual} != {expected}"
