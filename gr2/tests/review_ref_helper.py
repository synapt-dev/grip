"""Review-ref spellings used by tests, independent of the production writer.

New writes use v1. Legacy spellings are explicit compatibility fixtures; the
namespace-wide prefix is only for observing that no extra review ref appeared.
"""
REVIEW_REF_ROOT = "refs/dev.synapt.grip/__reviews__/"
REVIEW_REF_PREFIX = REVIEW_REF_ROOT + "v1/"


def review_ref(commit: str, version: str = "v1") -> str:
    return REVIEW_REF_ROOT + (version + "/" if version else "") + commit


def legacy_review_ref(commit: str) -> str:
    return review_ref(commit, version="")


def review_ref_glob(version: str = "v1") -> str:
    return review_ref("*", version=version)


def legacy_bind_tree(repo, row: dict[str, str]) -> str:
    """Build the old v2 tree without calling today's bind writer.

    This fixture intentionally has no carried objects, the supported SHA-only
    legacy record. Migration must retain its commit/tree instead of upgrading
    the record in place when the new writer changes its own representation.
    """
    import subprocess

    def git(*args: str, input: str = "") -> str:
        p = subprocess.run(["git", "-C", str(repo), *args], input=input,
                           text=True, capture_output=True, check=True)
        return p.stdout.strip()

    def blob(text: str) -> str:
        return git("hash-object", "-w", "--stdin", input=text)

    def tree(entries: list[tuple[str, str, str]]) -> str:
        return git("mktree", input="".join(
            f"{'040000' if kind == 'tree' else '100644'} {kind} {oid}\t{name}\n"
            for name, kind, oid in entries))

    key = row["key"]
    member = tree([(name, "blob", blob(row[value])) for name, value in (
        ("remote", "remote"), ("path", "path"), ("commit", "head"), ("base", "base"))])
    observed = tree([("remote-head", "blob", blob(row["base"]))])
    texts = tree([(name, "blob", blob(row.get(name, "").rstrip("\n"))) for name in ("title", "body")])
    meta = tree([(name, "blob", blob(value)) for name, value in (
        ("schema", "gr2-review-bind/v2"), ("kind", "review"), ("policy", "no-policy"))])
    return tree([(".grip", "tree", meta),
                 ("repos", "tree", tree([(key, "tree", member)])),
                 ("observed", "tree", tree([(key, "tree", observed)])),
                 ("texts", "tree", tree([(key, "tree", texts)]))])
