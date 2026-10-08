"""Offline schema resources equal the pinned upstream package bytes."""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from gr2.python_cli import check_records

# Package revisions identify the upstream copy; no new JSON encoding or runtime
# descriptor validator is introduced by making these offline resources available.
COPIES = (
    (
        "dev.synapt.grip.review.v1alpha1",
        "88babf4a0c55110a1e1badc53f07a6f0f8912f86",
        "review.proto",
        "94e75f8ff703a20a2ab2cecd5226c24d25e9f8c1a1d295cdaf224ab0a2e73296",
    ),
    (
        "dev.synapt.grip.review.v1alpha1",
        "88babf4a0c55110a1e1badc53f07a6f0f8912f86",
        "descriptor_set.pb",
        "2be436c341c510e53f50ad53cd6b245f0f2d572dcfca595a1f6424f62c863390",
    ),
    (
        "dev.synapt.grip.approval.v1alpha1",
        "102a5e0d55bbcc8beddabc9834fafbf1660b52b3",
        "approval.proto",
        "6d81e75318e40f5834ad942eb6c36c9d392ca3bead667012a971dd8ec45fc375",
    ),
    (
        "dev.synapt.grip.approval.v1alpha1",
        "102a5e0d55bbcc8beddabc9834fafbf1660b52b3",
        "descriptor_set.pb",
        "a2500edaf8c246ff6c8650c00d01b1f4b49b50e0528f942b51ab01ba7b8865eb",
    ),
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
        "73f69f513c19211561a8209c46198739a30501ec",
        "merge.proto",
        "748f129d8df97cb817261af3d9a15e875736043212f7ccf77721979579f8f8f5",
    ),
    (
        "dev.synapt.grip.merge.v1alpha1",
        "73f69f513c19211561a8209c46198739a30501ec",
        "descriptor_set.pb",
        "8a36f79b29b7e0d97ff25354de70ab821b517fb156edc44f49162c27568288dc",
    ),
)


@pytest.mark.parametrize("package,revision,name,expected", COPIES)
def test_record_schema_copy_matches_upstream(package, revision, name, expected):
    root = Path(check_records.__file__).resolve().parent.parent / "schemas"
    actual = hashlib.sha256((root / package / name).read_bytes()).hexdigest()
    assert actual == expected, f"{package}@{revision}/{name}: {actual} != {expected}"
