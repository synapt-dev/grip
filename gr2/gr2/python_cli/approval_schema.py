"""Pinned approval package bytes for the exact-head approval writer.

Unsigned links need no signature or keyring. The first link's ``prev`` hashes
the bind's record; subsequent links hash the previous approval's record.
The chain writer owns the sorted-key JSON representation and its verification.
"""
from __future__ import annotations

import hashlib
import importlib.resources
from pathlib import Path

PACKAGE = "dev.synapt.grip.approval.v1alpha1"
SCHEMA_COMMIT = "102a5e0d55bbcc8beddabc9834fafbf1660b52b3"
SCHEMA_SOURCE = f"{PACKAGE}@{SCHEMA_COMMIT}"
PROTO_SHA256 = "6d81e75318e40f5834ad942eb6c36c9d392ca3bead667012a971dd8ec45fc375"
DESCRIPTOR_SHA256 = "a2500edaf8c246ff6c8650c00d01b1f4b49b50e0528f942b51ab01ba7b8865eb"


def _read(name: str, expected: str) -> bytes:
    try:
        raw = (importlib.resources.files("gr2") / "schemas" / PACKAGE / name).read_bytes()
    except AttributeError:
        # The test harness injects a package without a ModuleSpec. The sibling
        # resource is still the same pinned file, checked below.
        raw = (Path(__file__).resolve().parent.parent / "schemas" / PACKAGE / name).read_bytes()
    actual = hashlib.sha256(raw).hexdigest()
    if actual != expected:
        raise ValueError(f"{SCHEMA_SOURCE} {name} hash mismatch: expected {expected}, got {actual}")
    return raw


def proto_bytes() -> bytes:
    return _read("approval.proto", PROTO_SHA256)


def descriptor_bytes() -> bytes:
    return _read("descriptor_set.pb", DESCRIPTOR_SHA256)
