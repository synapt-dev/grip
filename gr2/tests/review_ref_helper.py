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

    rows = row if isinstance(row, list) else [row]
    repos, observed, texts, objects, evidence = [], [], [], [], []
    for item in rows:
        key = item["key"]
        member = tree([(name, "blob", blob(item[value])) for name, value in (
            ("remote", "remote"), ("path", "path"), ("commit", "head"), ("base", "base"))])
        repos.append((key, "tree", member))
        observed.append((key, "tree", tree([("remote-head", "blob", blob(item.get("remote_head", item["base"])))])))
        texts.append((key, "tree", tree([(name, "blob", blob(item.get(name, "").rstrip("\n")))
                                       for name in ("title", "body")])))
        if item.get("objects"):
            objects.append((key, "tree", tree([(name, "blob", blob(value))
                                               for name, value in item["objects"].items()])))
        if item.get("evidence"):
            fields = [("commands", "blob", blob(item["evidence"]))]
            if item.get("resolution"):
                fields.append(("resolution", "blob", blob(item["resolution"])))
            evidence.append((key, "tree", tree(fields)))
    meta = tree([(name, "blob", blob(value)) for name, value in (
        ("schema", "gr2-review-bind/v2"), ("kind", "review"),
        ("policy", rows[0].get("policy", "no-policy")))])
    entries = [(".grip", "tree", meta), ("repos", "tree", tree(repos)),
               ("observed", "tree", tree(observed)), ("texts", "tree", tree(texts))]
    if objects:
        entries.append(("objects", "tree", tree(objects)))
    if evidence:
        entries.append(("evidence", "tree", tree(evidence)))
    return tree(entries)
