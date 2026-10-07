"""Review-ref spellings used by tests, independent of the production writer.

Legacy writers remain unchanged during the reader-only slice; the
namespace-wide prefix is only for observing that no extra review ref appeared.
"""
REVIEW_REF_ROOT = "refs/dev.synapt.grip/__reviews__/"
REVIEW_REF_PREFIX = REVIEW_REF_ROOT


def review_ref(commit: str, version: str = "v1") -> str:
    return REVIEW_REF_ROOT + (version + "/" if version else "") + commit


def legacy_review_ref(commit: str) -> str:
    return review_ref(commit, version="")


def review_ref_glob(version: str = "") -> str:
    return review_ref("*", version=version)
