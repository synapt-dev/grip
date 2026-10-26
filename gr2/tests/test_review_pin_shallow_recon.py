"""`review pin --from-range` derives the head-tree in a throwaway repo. It needs the base commit and the blobs the
checkout and `git am` touch, never the remote's whole history, so the plain path fetches the base alone (depth 1, no
blobs) and keeps today's full clone everywhere else: `--at-base` asks an ancestry question that needs history, and a
remote that will not serve the base by sha, or a base that is not a full sha, falls back to it. In every case the
pinned result is the same bytes.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from gr2.python_cli import gitops, grip


def _git(cwd: Path, *a: str) -> str:
    return subprocess.run(["git", *a], cwd=cwd, text=True, capture_output=True, check=True).stdout.strip()


def _remote_and_range(tmp_path: Path) -> tuple[str, str, str, str, str, str]:
    """A bare origin whose `main` carries history up to BASE, plus a two-commit range over it that exists only as
    patch bytes. Returns (remote, base, head, head_tree, range_patch, committers_tsv)."""
    origin = tmp_path / "alpha.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    work = tmp_path / "work"
    _git(tmp_path, "clone", "-q", str(origin), str(work))
    _git(work, "config", "user.email", "a@e.invalid")
    _git(work, "config", "user.name", "a")
    for i in range(3):
        (work / f"f{i}.txt").write_text(f"v{i}\n" * 20)
        _git(work, "add", ".")
        _git(work, "commit", "-q", "-m", f"history {i}")
    _git(work, "push", "-q", "origin", "main")
    base = _git(work, "rev-parse", "HEAD")
    (work / "f1.txt").write_text("changed\n")
    (work / "new.txt").write_text("added by the review\n")
    _git(work, "add", ".")
    _git(work, "commit", "-q", "-m", "review one")
    (work / "f2.txt").write_text("changed too\n")
    _git(work, "commit", "-q", "-am", "review two")
    head = _git(work, "rev-parse", "HEAD")
    head_tree = _git(work, "rev-parse", "HEAD^{tree}")
    range_patch = subprocess.run(["git", "format-patch", f"{base}..{head}", "--stdout"],
                                 cwd=work, text=True, capture_output=True, check=True).stdout
    committers = _git(work, "log", "--reverse", "--format=%cn%x09%ce%x09%cI", f"{base}..{head}") + "\n"
    return str(origin), base, head, head_tree, range_patch, committers


def _record(monkeypatch) -> list[tuple[str, tuple[str, ...]]]:
    """Record every clone and every network fetch the reconstruction makes, passing each through."""
    calls: list[tuple[str, tuple[str, ...]]] = []
    real_clone, real_run = gitops.clone, gitops.run

    def clone(*args, **kw):
        calls.append(("clone", args))
        return real_clone(*args, **kw)

    def run(cwd, *args, **kw):
        if args and args[0] == "fetch":
            calls.append(("fetch", args))
        return real_run(cwd, *args, **kw)

    monkeypatch.setattr(gitops, "clone", clone)
    monkeypatch.setattr(gitops, "run", run)
    return calls


def test_the_plain_path_fetches_the_base_alone_and_never_clones(tmp_path, monkeypatch):
    remote, base, head, head_tree, rng, committers = _remote_and_range(tmp_path)
    calls = _record(monkeypatch)
    obj = grip._carry_objects_from_range(tmp_path, remote, base, rng, committers=committers, expected_head=head)
    assert obj["head-tree"] == head_tree
    assert [c for c in calls if c[0] == "clone"] == []
    fetches = [c[1] for c in calls if c[0] == "fetch"]
    assert len(fetches) == 1
    assert "--depth" in fetches[0] and fetches[0][fetches[0].index("--depth") + 1] == "1"
    assert "--filter=blob:none" in fetches[0] and fetches[0][-1] == base


def test_the_shallow_pin_is_the_same_bytes_as_the_clone_pin(tmp_path, monkeypatch):
    remote, base, head, head_tree, rng, committers = _remote_and_range(tmp_path)
    shallow = grip._carry_objects_from_range(tmp_path, remote, base, rng, committers=committers, expected_head=head)
    monkeypatch.setattr(grip, "_shallow_base_into", lambda lane, remote, base: False)
    cloned = grip._carry_objects_from_range(tmp_path, remote, base, rng, committers=committers, expected_head=head)
    # committer-faithful, so the derived commits are the pinned head and every carried byte must match
    assert shallow == cloned
    assert shallow["head-tree"] == head_tree


def test_a_remote_that_will_not_serve_the_base_by_sha_falls_back_to_a_clone_and_prints_nothing(
        tmp_path, monkeypatch, capsys):
    remote, base, head, head_tree, rng, committers = _remote_and_range(tmp_path)
    calls: list[tuple[str, tuple[str, ...]]] = []
    real_clone, real_run = gitops.clone, gitops.run

    def run(cwd, *args, **kw):
        if args and args[0] == "fetch" and "--depth" in args:
            calls.append(("refused-fetch", args))
            return subprocess.CompletedProcess(args, 128, "", "fatal: server does not allow request for unadvertised object")
        return real_run(cwd, *args, **kw)

    def clone(*args, **kw):
        calls.append(("clone", args))
        return real_clone(*args, **kw)

    monkeypatch.setattr(gitops, "run", run)
    monkeypatch.setattr(gitops, "clone", clone)
    obj = grip._carry_objects_from_range(tmp_path, remote, base, rng, committers=committers, expected_head=head)
    assert [c[0] for c in calls] == ["refused-fetch", "clone"]
    # today's clone exactly: a remote that refuses a fetch by sha may refuse a partial clone's lazy blob fetches too
    assert not any(a.startswith("--filter") or a.startswith("--depth") for a in calls[1][1])
    assert obj["head-tree"] == head_tree
    out = capsys.readouterr()
    assert out.out == "" and out.err == ""


def test_at_base_keeps_todays_full_clone(tmp_path, monkeypatch):
    remote, base, head, head_tree, rng, committers = _remote_and_range(tmp_path)
    calls = _record(monkeypatch)
    obj = grip._carry_objects_from_range(tmp_path, remote, base, rng, at_base_of="main")
    assert obj["head-tree"] == head_tree
    clones = [c[1] for c in calls if c[0] == "clone"]
    assert len(clones) == 1 and not any(a.startswith("--filter") or a.startswith("--depth") for a in clones[0])
    assert not any("--depth" in c[1] for c in calls if c[0] == "fetch")


def test_a_base_that_is_not_a_full_sha_takes_the_clone(tmp_path, monkeypatch):
    remote, base, head, head_tree, rng, committers = _remote_and_range(tmp_path)
    calls = _record(monkeypatch)
    obj = grip._carry_objects_from_range(tmp_path, remote, "main", rng)
    assert obj["head-tree"] == head_tree
    assert [c[0] for c in calls] == ["clone"]


# --- the shallow base is checked out sparse, to the paths the range touches ---

def _awkward_remote_and_range(tmp_path: Path) -> tuple[str, str, str, str, str, str, str]:
    """A base with a large blob the range never touches, and a range that renames a file, adds paths that gitignore
    syntax would misread (a space, a leading #, a trailing space, a glob character), changes only a mode, adds a
    binary file and touches one file twice. Returns (remote, base, head, head_tree, range_patch, committers,
    big_blob_sha)."""
    import os
    origin = tmp_path / "beta.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    work = tmp_path / "bwork"
    _git(tmp_path, "clone", "-q", str(origin), str(work))
    _git(work, "config", "user.email", "a@e.invalid")
    _git(work, "config", "user.name", "a")
    (work / "big.bin").write_bytes(os.urandom(5 * 1024 * 1024))
    (work / "old name.txt").write_text("renamed below\n" * 30)
    (work / "run.sh").write_text("echo hi\n")
    (work / "keep.txt").write_text("one\n")
    (work / "caf\u00e9.txt").write_text("a name git quotes\n" * 30)
    _git(work, "add", ".")
    _git(work, "commit", "-q", "-m", "base")
    _git(work, "push", "-q", "origin", "main")
    base = _git(work, "rev-parse", "HEAD")
    big = _git(work, "rev-parse", "HEAD:big.bin")
    _git(work, "mv", "old name.txt", "new name.txt")
    _git(work, "mv", "caf\u00e9.txt", "cafe2.txt")
    (work / "#lead.md").write_text("hash first\n")
    (work / "trail ").write_text("trailing space\n")
    (work / "star*[1].txt").write_text("globby\n")
    (work / "keep.txt").write_text("one\ntwo\n")
    _git(work, "add", ".")
    _git(work, "commit", "-q", "-m", "awkward one")
    os.chmod(work / "run.sh", 0o755)
    (work / "img.bin").write_bytes(bytes(range(256)) * 8)
    (work / "keep.txt").write_text("one\ntwo\nthree\n")
    _git(work, "add", ".")
    _git(work, "commit", "-q", "-m", "awkward two")
    head = _git(work, "rev-parse", "HEAD")
    head_tree = _git(work, "rev-parse", "HEAD^{tree}")
    range_patch = subprocess.run(["git", "format-patch", "--binary", f"{base}..{head}", "--stdout"],
                                 cwd=work, text=True, capture_output=True, check=True).stdout
    committers = _git(work, "log", "--reverse", "--format=%cn%x09%ce%x09%cI", f"{base}..{head}") + "\n"
    # a file:// remote that serves filtered fetches, as GitHub does; a plain path ignores the filter
    _git(origin, "config", "uploadpack.allowFilter", "true")
    _git(origin, "config", "uploadpack.allowAnySHA1InWant", "true")
    return "file://" + str(origin), base, head, head_tree, range_patch, committers, big


def test_a_sparse_pin_of_awkward_paths_is_the_same_bytes_as_the_full_pin(tmp_path, monkeypatch):
    remote, base, head, head_tree, rng, committers, _ = _awkward_remote_and_range(tmp_path)
    sparse = grip._carry_objects_from_range(tmp_path, remote, base, rng, committers=committers, expected_head=head)
    monkeypatch.setattr(grip, "_shallow_base_into", lambda lane, remote, base: False)
    full = grip._carry_objects_from_range(tmp_path, remote, base, rng, committers=committers, expected_head=head)
    assert sparse == full
    assert sparse["head-tree"] == head_tree


def test_the_range_paths_are_read_exactly(tmp_path):
    remote, base, head, head_tree, rng, committers, _ = _awkward_remote_and_range(tmp_path)
    lane = tmp_path / "lane"
    _git(tmp_path, "init", "-q", str(lane))
    assert 'rename from "caf\\303\\251.txt"' in rng  # git C-quotes this source; the reader must unquote it
    assert grip._range_paths(lane, rng) == sorted(
        ["#lead.md", "caf\u00e9.txt", "cafe2.txt", "img.bin", "keep.txt", "new name.txt", "old name.txt", "run.sh",
         "star*[1].txt", "trail "])


def test_a_blob_the_range_never_touches_is_never_fetched(tmp_path, monkeypatch):
    remote, base, head, head_tree, rng, committers, big = _awkward_remote_and_range(tmp_path)
    seen = {}
    real = grip._apply_range_in_lane

    def apply(lane, range_patch, committers):
        real(lane, range_patch, committers)
        probe = subprocess.run(["git", "-C", str(lane), "cat-file", "-e", big], capture_output=True,
                               env={**__import__("os").environ, "GIT_NO_LAZY_FETCH": "1"})
        seen["big_present"] = probe.returncode == 0

    monkeypatch.setattr(grip, "_apply_range_in_lane", apply)
    obj = grip._carry_objects_from_range(tmp_path, remote, base, rng, committers=committers, expected_head=head)
    assert obj["head-tree"] == head_tree
    assert seen == {"big_present": False}


def test_a_range_git_cannot_list_takes_the_whole_tree_and_the_same_bytes(tmp_path, monkeypatch):
    remote, base, head, head_tree, rng, committers, _ = _awkward_remote_and_range(tmp_path)
    monkeypatch.setattr(grip, "_range_paths", lambda lane, range_patch: None)
    obj = grip._carry_objects_from_range(tmp_path, remote, base, rng, committers=committers, expected_head=head)
    assert obj["head-tree"] == head_tree


def test_a_sparse_set_that_misses_a_path_still_pins_the_same_bytes(tmp_path, monkeypatch):
    # git am in a sparse tree applies a path outside the set too, so this one never needs the redo below
    remote, base, head, head_tree, rng, committers, _ = _awkward_remote_and_range(tmp_path)
    monkeypatch.setattr(grip, "_range_paths", lambda lane, range_patch: ["#lead.md"])
    obj = grip._carry_objects_from_range(tmp_path, remote, base, rng, committers=committers, expected_head=head)
    assert obj["head-tree"] == head_tree


def test_an_apply_that_fails_in_the_sparse_tree_is_redone_on_the_whole_tree(tmp_path, monkeypatch):
    remote, base, head, head_tree, rng, committers, _ = _awkward_remote_and_range(tmp_path)
    real, calls = grip._apply_range_in_lane, []

    def apply(lane, range_patch, committers):
        calls.append(subprocess.run(["git", "-C", str(lane), "config", "--bool", "core.sparseCheckout"],
                                    capture_output=True, text=True).stdout.strip())
        if len(calls) == 1:
            raise grip._RangeApplyError("am", "forced failure in the sparse tree")
        real(lane, range_patch, committers)

    monkeypatch.setattr(grip, "_apply_range_in_lane", apply)
    obj = grip._carry_objects_from_range(tmp_path, remote, base, rng, committers=committers, expected_head=head)
    # the first try ran sparse, the redo ran on the whole tree, and the result is the pinned head's bytes
    assert calls == ["true", "false"]
    assert obj["head-tree"] == head_tree


def test_a_sparse_pattern_matches_only_its_own_path():
    assert grip._sparse_pattern("a/b.txt") == "/a/b.txt"
    assert grip._sparse_pattern("#lead.md") == "/\\#lead.md"
    assert grip._sparse_pattern("!neg") == "/\\!neg"
    assert grip._sparse_pattern("star*[1].txt") == "/star\\*\\[1\\].txt"
    assert grip._sparse_pattern("back\\slash") == "/back\\\\slash"
    assert grip._sparse_pattern("trail  ") == "/trail\\ \\ "


def test_git_unquote_reads_what_git_writes():
    assert grip._git_unquote("plain name.txt") == "plain name.txt"
    assert grip._git_unquote('"caf\\303\\251.txt"') == "caf\u00e9.txt"
    assert grip._git_unquote('"tab\\there"') == "tab\there"
    assert grip._git_unquote('"q\\"uote"') == 'q"uote'
    assert grip._git_unquote('"unterminated') is None
    assert grip._git_unquote('"bad\\qescape"') is None
