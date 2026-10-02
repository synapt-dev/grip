"""A minimal NATIVE root for tests that exercise the review engine, not `store init`.

A native root is a `grip.toml` beside the root's own `.git` (`grip._is_native_workspace`). Review binds
live in that `.git` under `refs/dev.synapt.grip/__reviews__/`, so a bind test needs exactly this and nothing from the
alpha `.grip/.git` store. Tests of `store init` itself use the verb."""

from __future__ import annotations

import subprocess
from pathlib import Path


def native_root(ws: Path) -> Path:
    ws.mkdir(parents=True, exist_ok=True)
    if not (ws / ".git").exists():
        subprocess.run(["git", "init", "-q", "-b", "main", str(ws)], check=True, capture_output=True)
    (ws / "grip.toml").write_text("")
    return ws


def workspace_kind_commit(ws: Path, repos: list[dict[str, str]]) -> str:
    """A `gr2-workspace/v1`-kind commit published under `refs/dev.synapt.grip/__reviews__/` in a native root: the
    WRONG-KIND fixture for the refusal rows (a commit that IS bound but is not a review or a project
    review). It mirrors the shape `grip.create_workspace_commit` writes to the alpha store."""

    def git(*args: str, stdin: str = "") -> str:
        return subprocess.run(
            ["git", "-C", str(ws), "-c", "user.name=t", "-c", "user.email=t@e.invalid", *args],
            input=stdin, capture_output=True, text=True, check=True,
        ).stdout.strip()

    def blob(text: str) -> str:
        return git("hash-object", "-w", "--stdin", stdin=text)

    entries = []
    for repo in sorted(repos, key=lambda r: r["key"]):
        fields = "".join(f"100644 blob {blob(repo[n])}\t{n}\n" for n in ("remote", "path", "commit", "base"))
        entries.append(f"040000 tree {git('mktree', stdin=fields)}\t{repo['key']}\n")
    repos_tree = git("mktree", stdin="".join(entries))
    meta = git("mktree", stdin=f"100644 blob {blob('gr2-workspace/v1')}\tschema\n100644 blob {blob('workspace')}\tkind\n")
    root = git("mktree", stdin=f"040000 tree {meta}\t.grip\n040000 tree {repos_tree}\trepos\n")
    commit = git("commit-tree", root, "-m", "grip workspace snapshot")
    git("update-ref", f"refs/dev.synapt.grip/__reviews__/{commit}", commit)
    return commit
