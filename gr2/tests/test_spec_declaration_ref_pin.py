"""The declaration keeps what the member state actually is: the branch of
record, the pin, and whether the head is detached.

``workspace init`` wrote name/path/url and nothing else, so a converted
superproject's spec said nothing about what the root PINS or which branch a
member's work would land on. The 09-21 ruling says the detached case is recorded
as what it is -- "the snapshot says detached at <sha>" -- and that the branch of
record defaults to the remote's default when a member is detached.

Every field is resolvable from local refs, which is what these tests pin:

    member state   symbolic-ref -q HEAD     rev-parse HEAD   refs/remotes/origin/HEAD
    attached       refs/heads/main          the commit       refs/remotes/origin/main
    detached       fails (detached)         the commit       refs/remotes/origin/main

The last column is why no network call is needed: a detached member still knows
the remote's default, so the branch of record can be written without inventing
one or reaching for ``ls-remote``.

Backward compatibility is a claim in this file and not an assumption: a spec
written before these keys existed must read unchanged, which is asserted against
a hand-written spec rather than reasoned about from the reader's source.
"""

from __future__ import annotations

import posixpath
import subprocess

import pytest
from pathlib import Path

from gr2.prototypes import repo_maintenance_prototype as repo_proto
from gr2.python_cli import migration
from gr2.python_cli.app import app

from tests.conftest import make_cli_runner


def _run(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=False, capture_output=True, text=True
    )


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "dev@layne.pro"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Layne Penney"], cwd=path, check=True)
    (path / "README.md").write_text("a\n")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=path, check=True)


def _spec_text(workspace_root: Path) -> str:
    return (workspace_root / ".grip" / "workspace_spec.toml").read_text()


def test_init_records_the_branch_the_pin_and_the_attached_state(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    repo = root / "repo"
    _init_repo(repo)
    head = _run("rev-parse", "HEAD", cwd=repo).stdout.strip()

    result = make_cli_runner().invoke(app, ["workspace", "init", str(root)])
    assert result.exit_code == 0, result.stdout

    spec = repo_proto.read_workspace_spec(root / ".grip" / "workspace_spec.toml")
    written = spec.repos[0]
    assert written.name == "repo"
    assert written.url == "", "fixture check: a locally created repo has no origin"
    assert written.ref == "main", "the branch of record is the branch it is on"
    assert written.pin == head, "the pin is the commit the root would pin"
    assert written.detached is False, "an attached head says so rather than saying nothing"


def test_init_records_a_detached_member_at_the_remote_default(tmp_path: Path) -> None:
    """The case the ruling names: a detached member records the detached state,
    and its branch of record is the REMOTE's default rather than an invented
    branch name."""
    src = tmp_path / "member-src"
    _init_repo(src)
    root = tmp_path / "super"
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "master"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "dev@layne.pro"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "Layne Penney"], cwd=root, check=True)
    added = subprocess.run(
        ["git", "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(src), "member1"],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )
    assert added.returncode == 0, added.stderr
    subprocess.run(["git", "commit", "-qm", "add submodule"], cwd=root, check=True)

    member = root / "member1"
    got = _run("checkout", "--detach", cwd=member)
    assert got.returncode == 0, got.stderr
    member_head = _run("rev-parse", "HEAD", cwd=member).stdout.strip()
    gitlink = _run("-C", str(root), "ls-files", "-s", "member1", cwd=tmp_path).stdout.split()
    assert len(gitlink) >= 2, "fixture check: the root records a gitlink for the member"

    result = make_cli_runner().invoke(app, ["workspace", "init", str(root)])
    assert result.exit_code == 0, result.stdout

    written = repo_proto.read_workspace_spec(root / ".grip" / "workspace_spec.toml").repos[0]
    assert written.detached is True
    assert written.ref == "main", "the remote's default, not an invented branch"
    assert written.pin == member_head
    assert written.pin == gitlink[1], "the pin is the SHA the root's gitlink actually pins"


def _superproject(tmp_path: Path, *, submodule_branch: str | None = None) -> tuple[Path, Path]:
    """A real superproject with one member, optionally tracking a branch.

    ``submodule_branch`` writes the ``branch`` key into ``.gitmodules``, which
    is a declaration the root makes about the member and outranks anything the
    member's own remote refs say.
    """
    src = tmp_path / "member-src"
    _init_repo(src)
    second = src / "second.txt"
    second.write_text("second\n")
    subprocess.run(["git", "add", "."], cwd=src, check=True)
    subprocess.run(["git", "commit", "-qm", "second"], cwd=src, check=True)
    if submodule_branch:
        subprocess.run(["git", "branch", submodule_branch], cwd=src, check=True)

    root = tmp_path / "super"
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "master"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "dev@layne.pro"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "Layne Penney"], cwd=root, check=True)
    add = ["git", "-c", "protocol.file.allow=always", "submodule", "add", "-q"]
    if submodule_branch:
        add += ["-b", submodule_branch]
    add += [str(src), "member1"]
    added = subprocess.run(add, cwd=root, check=False, capture_output=True, text=True)
    assert added.returncode == 0, added.stderr
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "add submodule"], cwd=root, check=True)
    return root, root / "member1"


def _gitlink(root: Path, path: str, tmp_path: Path) -> str:
    out = _run("-C", str(root), "ls-tree", "HEAD", path, cwd=tmp_path).stdout.split()
    assert len(out) >= 3, f"fixture check: no gitlink for {path}: {out}"
    return out[2]


def test_the_pin_comes_from_the_root_gitlink_not_the_member_head(tmp_path: Path) -> None:
    """A member moved WITHOUT a root commit must not have its drift recorded as
    the pin. The pin is the root's declaration -- the gitlink -- and the moved
    head is a fact about the member, reported as drift rather than written into
    the declaration as though the root had said it."""
    root, member = _superproject(tmp_path)
    pinned = _gitlink(root, "member1", tmp_path)

    # Move the member off the pinned commit without touching the root: the
    # classic drift. Backward, not to `origin/main`, because main IS the pinned
    # commit here and checking it out would move nothing -- the fixture check
    # below is what caught that the first time.
    got = _run("checkout", "-q", "HEAD~1", cwd=member)
    assert got.returncode == 0, got.stderr
    moved = _run("rev-parse", "HEAD", cwd=member).stdout.strip()
    assert moved != pinned, "fixture check: the member really moved"

    result = make_cli_runner().invoke(app, ["workspace", "init", str(root)])
    assert result.exit_code == 0, result.stdout

    written = repo_proto.read_workspace_spec(root / ".grip" / "workspace_spec.toml").repos[0]
    assert written.pin == pinned, "the pin is what the ROOT pins, not where the member is"
    assert written.pin != moved


def test_status_reports_drift_when_the_member_moved(tmp_path: Path) -> None:
    """The observation belongs in the status, where it is dated and visibly a
    remark about a moment, and not in the declaration, which holds declared
    state only."""
    root, member = _superproject(tmp_path)
    assert make_cli_runner().invoke(app, ["workspace", "init", str(root)]).exit_code == 0

    clean = migration.render_status(migration.workspace_status(root))
    # Matched on the rendered marker, not the bare word: the workspace path in
    # the same output is named after this test and contains "drift".
    assert "-- drift" not in clean

    got = _run("checkout", "-q", "HEAD~1", cwd=member)
    assert got.returncode == 0, got.stderr
    moved = _run("rev-parse", "HEAD", cwd=member).stdout.strip()

    drifted = migration.render_status(migration.workspace_status(root))
    assert "-- drift" in drifted
    assert moved in drifted, "the drift names where the member actually is"


def test_the_gitmodules_branch_outranks_the_members_origin_head(tmp_path: Path) -> None:
    """The precedence the ruling names: ``.gitmodules``, then ``origin/HEAD``,
    then absent. A member declared to track ``dev`` must not be recorded as
    ``main`` just because its own remote refs happen to name main."""
    root, member = _superproject(tmp_path, submodule_branch="dev")
    # Detach the member, so its CURRENT branch cannot be the source of the
    # answer: a member left on the declared branch would satisfy this test by
    # reading `symbolic-ref HEAD`, which is not the precedence under test.
    got = _run("checkout", "-q", "--detach", cwd=member)
    assert got.returncode == 0, got.stderr
    origin_head = _run(
        "symbolic-ref", "-q", "refs/remotes/origin/HEAD", cwd=member
    ).stdout.strip()
    assert origin_head == "refs/remotes/origin/main", "fixture check: origin/HEAD says main"

    result = make_cli_runner().invoke(app, ["workspace", "init", str(root)])
    assert result.exit_code == 0, result.stdout

    written = repo_proto.read_workspace_spec(root / ".grip" / "workspace_spec.toml").repos[0]
    assert written.ref == "dev", "the declared branch of record outranks origin/HEAD"


def test_root_gitlink_pins_lists_gitlinks_and_not_blobs(tmp_path: Path) -> None:
    """The contract, stated against a tree that contains both: only mode 160000
    entries are pins. A blob in the same tree is not one, and a reader that
    dropped the mode test would call `.gitmodules` a pin -- harmlessly today
    only because no repo path can equal a blob's path, which is an argument
    about the callers rather than a property of the instrument."""
    root, _member = _superproject(tmp_path)
    pins = repo_proto.root_gitlink_pins(root)
    assert "member1" in pins, "the member IS pinned"
    assert ".gitmodules" not in pins, "a blob is not a pin, whatever its mode test says"
    assert set(pins) == {"member1"}


def test_the_submodule_branch_is_keyed_by_path_when_the_name_differs(
    tmp_path: Path,
) -> None:
    """A submodule's NAME and its PATH are different strings and only the path
    is what a workspace scans. A fixture where they coincide cannot tell a
    path-keyed reader from a name-keyed one, so this one renames the submodule."""
    src = tmp_path / "member-src"
    _init_repo(src)
    (src / "second.txt").write_text("second\n")
    subprocess.run(["git", "add", "."], cwd=src, check=True)
    subprocess.run(["git", "commit", "-qm", "second"], cwd=src, check=True)
    subprocess.run(["git", "branch", "dev"], cwd=src, check=True)

    root = tmp_path / "super"
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "master"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "dev@layne.pro"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "Layne Penney"], cwd=root, check=True)
    added = subprocess.run(
        ["git", "-c", "protocol.file.allow=always", "submodule", "add", "-q",
         "-b", "dev", "--name", "renamed", str(src), "member1"],
        cwd=root, check=False, capture_output=True, text=True,
    )
    assert added.returncode == 0, added.stderr
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "add renamed submodule"], cwd=root, check=True)

    declared = repo_proto.root_submodule_branches(root)
    assert declared == {"member1": "dev"}, "keyed by PATH, not by submodule name"

    member = root / "member1"
    got = _run("checkout", "-q", "--detach", cwd=member)
    assert got.returncode == 0, got.stderr
    assert make_cli_runner().invoke(app, ["workspace", "init", str(root)]).exit_code == 0
    written = repo_proto.read_workspace_spec(root / ".grip" / "workspace_spec.toml").repos[0]
    assert written.ref == "dev"


def test_the_gitmodules_branch_is_read_per_path_not_per_name(tmp_path: Path) -> None:
    """Control for the precedence: with no ``branch`` key in ``.gitmodules``,
    the fallback still applies. Without this, a reader that always answered
    from ``.gitmodules`` (or always answered ``dev``) would pass the test
    above."""
    root, member = _superproject(tmp_path)
    assert (
        _run("config", "-f", str(root / ".gitmodules"), "--get", "submodule.member1.branch", cwd=tmp_path)
        .stdout.strip()
        == ""
    ), "fixture check: no branch key is declared"
    got = _run("checkout", "-q", "--detach", cwd=member)
    assert got.returncode == 0, got.stderr

    assert make_cli_runner().invoke(app, ["workspace", "init", str(root)]).exit_code == 0
    written = repo_proto.read_workspace_spec(root / ".grip" / "workspace_spec.toml").repos[0]
    assert written.ref == "main", "the fallback is origin/HEAD's branch"


def test_a_spec_written_before_these_fields_still_reads(tmp_path: Path) -> None:
    """Control: every spec written before this change lacks the new keys, and
    must read with them absent rather than raising or inventing values."""
    root = tmp_path / "ws"
    grip = root / ".grip"
    grip.mkdir(parents=True)
    (grip / "workspace_spec.toml").write_text(
        'workspace_name = "older"\n\n'
        "[[repos]]\n"
        'name = "recall"\n'
        'path = "recall"\n'
        'url = "https://example.invalid/recall.git"\n\n'
        "[[units]]\n"
        'name = "default"\n'
        'path = "agents/default/home"\n'
        'repos = ["recall"]\n'
    )
    spec = repo_proto.read_workspace_spec(grip / "workspace_spec.toml")
    assert spec.workspace_name == "older"
    assert spec.repos[0].name == "recall"
    assert spec.repos[0].ref is None
    assert spec.repos[0].pin is None
    assert spec.repos[0].detached is None
    # and the whole workspace still reads
    assert migration.workspace_status(root)["gr2_repo_count"] == 1


def test_status_declares_the_pin_and_the_branch_of_record(tmp_path: Path) -> None:
    """The consumer: a declared pin nothing reads would drift, so the status
    reports it. This is the fruit of the change -- the root's declaration is
    readable without re-deriving it from the tree."""
    root = tmp_path / "ws"
    repo = root / "repo"
    _init_repo(repo)
    assert make_cli_runner().invoke(app, ["workspace", "init", str(root)]).exit_code == 0
    pin = _run("rev-parse", "HEAD", cwd=repo).stdout.strip()

    rendered = migration.render_status(migration.workspace_status(root))
    assert pin in rendered, "the declared pin is reported"
    assert "main" in rendered, "the branch of record is reported beside it"


def test_status_names_the_detached_case(tmp_path: Path) -> None:
    """A detached member must not read as an attached one: the two print
    differently, or the report would be claiming a branch the member is not on."""
    root = tmp_path / "ws"
    repo = root / "repo"
    _init_repo(repo)
    assert make_cli_runner().invoke(app, ["workspace", "init", str(root)]).exit_code == 0
    detached_before = migration.render_status(migration.workspace_status(root))

    _run("checkout", "--detach", cwd=repo)
    assert make_cli_runner().invoke(app, ["workspace", "init", str(root)]).exit_code == 0
    detached_after = migration.render_status(migration.workspace_status(root))

    assert detached_before != detached_after, "detaching must change what is printed"
    # Matched on the rendered form, not on the bare word: the workspace path in
    # the same output is named after this test, so a substring search for
    # "detached" matches the directory and passes without the state being read.
    assert "(detached)" in detached_after
    assert "(detached)" not in detached_before


def _nested_superproject(tmp_path: Path) -> tuple[Path, str]:
    """A superproject whose single member sits TWO levels down, at ``libs/c``.

    The depth matters: a scan that walks one level sees ``libs`` -- a plain
    directory which git answers for from the ENCLOSING repo -- and never sees
    the member at all.
    """
    src = tmp_path / "c"
    _init_repo(src)
    root = tmp_path / "super"
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "master"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "dev@layne.pro"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "Layne Penney"], cwd=root, check=True)
    (root / "libs").mkdir()
    added = subprocess.run(
        ["git", "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(src), "libs/c"],
        cwd=root, check=False, capture_output=True, text=True,
    )
    assert added.returncode == 0, added.stderr
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "add nested submodule"], cwd=root, check=True)
    return root, "libs/c"


def test_a_nested_member_is_scanned_as_itself(tmp_path: Path) -> None:
    """The member at ``libs/c`` is the thing the root pins, so it is what the
    spec must carry -- and NOT a row named ``libs``, which is a plain directory
    that git answers for from the enclosing superproject, so it would carry the
    superproject's own branch and its own head as though they were a member's."""
    root, member_path = _nested_superproject(tmp_path)
    pinned = _gitlink(root, member_path, tmp_path)

    result = make_cli_runner().invoke(app, ["workspace", "init", str(root)])
    assert result.exit_code == 0, result.stdout

    spec = repo_proto.read_workspace_spec(root / ".grip" / "workspace_spec.toml")
    written = {repo.path: repo for repo in spec.repos}
    assert set(written) == {member_path}, f"the member set is the pinned set: {sorted(written)}"
    assert written[member_path].pin == pinned
    assert written[member_path].url, "the member's url comes from .gitmodules"

    # The directory that is not a repo must not be reported, and nothing should
    # advise adding a remote to it.
    assert not (root / "libs" / ".git").exists()
    assert "libs\t" not in result.stdout
    rendered = migration.render_status(migration.workspace_status(root))
    assert "super/libs" not in rendered or "libs/c" in rendered


def test_a_plain_directory_is_not_a_repo_when_the_root_is_one(tmp_path: Path) -> None:
    """The same defect one layer down, with no superproject involved: when the
    workspace root is a git repo, every plain directory under it answers
    ``--is-inside-work-tree`` true and would be scanned as a repo."""
    root = tmp_path / "ws"
    _init_repo(root)
    (root / "plain").mkdir()
    (root / "plain" / "notes.txt").write_text("not a repo\n")
    clone = root / "clone"
    subprocess.run(["git", "clone", "-q", str(root), str(clone)], check=True, capture_output=True)

    result = make_cli_runner().invoke(app, ["workspace", "init", str(root)])
    assert result.exit_code == 0, result.stdout

    spec = repo_proto.read_workspace_spec(root / ".grip" / "workspace_spec.toml")
    assert {repo.path for repo in spec.repos} == {"clone"}, (
        "only a repo that is its own toplevel counts"
    )


def _plain_clone_of_superproject(
    tmp_path: Path, *, submodule_branch: str | None = None
) -> tuple[Path, str]:
    """A plain ``git clone`` of a superproject, WITHOUT ``--recurse-submodules``.

    The member paths are then empty directories, which is the state this whole
    fixture exists for: git answers every question asked from inside one about
    the ENCLOSING superproject, so a scan that trusts those answers records the
    superproject's own url, branch and head as the member's.
    """
    src = tmp_path / "c"
    _init_repo(src)
    if submodule_branch:
        subprocess.run(["git", "branch", submodule_branch], cwd=src, check=True)

    super_src = tmp_path / "super-src"
    super_src.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "master"], cwd=super_src, check=True)
    subprocess.run(["git", "config", "user.email", "dev@layne.pro"], cwd=super_src, check=True)
    subprocess.run(["git", "config", "user.name", "Layne Penney"], cwd=super_src, check=True)
    (super_src / "libs").mkdir()
    add = ["git", "-c", "protocol.file.allow=always", "submodule", "add", "-q"]
    if submodule_branch:
        add += ["-b", submodule_branch]
    # A RELATIVE url, which is the case under test: `git submodule add` writes
    # an absolute one when it is handed an absolute path, so a fixture built
    # that way never exercises the resolution at all and its assertion passes
    # trivially.
    add += ["../c", "libs/c"]
    added = subprocess.run(add, cwd=super_src, check=False, capture_output=True, text=True)
    assert added.returncode == 0, added.stderr
    subprocess.run(["git", "add", "."], cwd=super_src, check=True)
    subprocess.run(["git", "commit", "-qm", "add submodule"], cwd=super_src, check=True)

    declared = subprocess.run(
        ["git", "config", "-f", str(super_src / ".gitmodules"), "--get", "submodule.libs/c.url"],
        capture_output=True, text=True,
    ).stdout.strip()
    assert declared == "../c", f"fixture check: the url is relative, not {declared!r}"

    clone = tmp_path / "plainclone"
    subprocess.run(
        ["git", "clone", "-q", str(super_src), str(clone)], check=True, capture_output=True
    )
    assert not (clone / "libs" / "c" / ".git").exists(), "fixture check: member is NOT materialized"
    assert (clone / "libs" / "c").is_dir(), "fixture check: the member directory exists"
    return clone, "libs/c"


def test_an_unmaterialized_member_is_described_not_read_through(tmp_path: Path) -> None:
    """Inside an empty member directory git answers about the ENCLOSING
    superproject, so reading it records the superproject's url, branch and head
    as the member's -- and a materialize from that spec would clone the
    superproject into the member path. The member is still declared by the root,
    so it is described from ``.gitmodules`` and the gitlink instead."""
    root, member_path = _plain_clone_of_superproject(tmp_path)
    pinned = _gitlink(root, member_path, tmp_path)

    result = make_cli_runner().invoke(app, ["workspace", "init", str(root)])
    assert result.exit_code == 0, result.stdout

    written = repo_proto.read_workspace_spec(root / ".grip" / "workspace_spec.toml").repos[0]
    assert written.path == member_path
    assert written.pin == pinned, "the pin is still the gitlink"
    assert written.url != str(tmp_path / "super-src"), (
        "the member's url is NOT the superproject's own origin"
    )
    assert Path(written.url).resolve() == (tmp_path / "c").resolve(), (
        "a relative .gitmodules url resolves against the root's origin"
    )
    assert written.ref is None, "no branch is declared, so none is invented"
    assert written.detached is None, (
        "the member's state is unknown here and is left UNSAID, not answered from"
        " the enclosing repo"
    )


def test_an_unmaterialized_member_keeps_the_branch_gitmodules_declares(
    tmp_path: Path,
) -> None:
    """The same case with a declared branch: it comes from ``.gitmodules``,
    since the member itself has nothing to say."""
    root, member_path = _plain_clone_of_superproject(tmp_path, submodule_branch="dev")

    result = make_cli_runner().invoke(app, ["workspace", "init", str(root)])
    assert result.exit_code == 0, result.stdout

    written = repo_proto.read_workspace_spec(root / ".grip" / "workspace_spec.toml").repos[0]
    assert written.path == member_path
    assert written.ref == "dev", "the declared branch, not the superproject's `master`"
    assert written.detached is None


@pytest.mark.parametrize(
    "url",
    [
        "c.git",
        "org/c.git",
        "example.com:org/c.git",
        "me@example.invalid:org/c.git",
        "/srv/git/c.git",
        "https://example.invalid/org/c.git",
    ],
)
def test_only_a_dot_slash_url_is_relative(tmp_path: Path, url: str) -> None:
    """git's rule, from gitmodules(5): a url is relative ONLY when it begins
    with ``./`` or ``../``. Everything else is used as written.

    A resolver that treats "anything not obviously absolute" as relative
    rewrites `me@host:org/c.git`, `example.com:org/c.git` and even a bare
    `c.git` onto the root's origin -- each a url that names somewhere else, or
    nowhere, afterwards.
    """
    # The root must HAVE an origin, or the no-origin path returns everything
    # unchanged for a reason unrelated to the rule under test.
    root, _member = _plain_clone_of_superproject(tmp_path)
    assert repo_proto.run_git(root, "remote", "get-url", "origin").returncode == 0, (
        "fixture check: the root has an origin to resolve against"
    )
    assert repo_proto.resolve_member_url(url, root) == url


@pytest.mark.parametrize("url", ["./c.git", "../c.git", "../../elsewhere/c.git"])
def test_a_dot_slash_url_resolves_against_the_origin(tmp_path: Path, url: str) -> None:
    """The other side of the same rule: these ARE relative, so they resolve
    onto the root's origin as a directory."""
    root, _member = _plain_clone_of_superproject(tmp_path)
    origin = repo_proto.run_git(root, "remote", "get-url", "origin").stdout.strip()
    resolved = repo_proto.resolve_member_url(url, root)
    assert resolved != url, "a relative url is rewritten"
    assert resolved == posixpath.normpath(f"{origin}/{url}"), resolved


def test_a_relative_url_with_no_origin_resolves_against_the_root(tmp_path: Path) -> None:
    """git's answer, measured by letting it resolve one: a root with no origin
    and a url of ``../c`` fetched the member from the root's own parent. Leaving
    the url as written makes materialize exit 1, because there is nothing to
    resolve it against later."""
    root, _member = _superproject(tmp_path)
    assert repo_proto.run_git(root, "remote", "get-url", "origin").returncode != 0, (
        "fixture check: this root has NO origin"
    )
    resolved = repo_proto.resolve_member_url("../c", root)
    assert resolved != "../c", "the url is resolved, not left as written"
    assert resolved == posixpath.normpath(f"{root}/../c"), resolved
    assert Path(resolved).resolve() == (tmp_path / "c").resolve()


def test_an_absolute_declaration_url_is_returned_unchanged(tmp_path: Path) -> None:
    """The resolver joins only a url that is RELATIVE. An absolute path, or one
    carrying a scheme, already names where the member lives; joining it onto the
    root's origin produces a path under the origin that names nothing, which is
    the failure mode of resolving unconditionally."""
    # The root must HAVE an origin, or the resolver takes its no-origin path
    # and returns every url unchanged for a reason unrelated to the guard under
    # test -- which is exactly how a first form of this test could not fail.
    root, _member = _plain_clone_of_superproject(tmp_path)
    assert repo_proto.run_git(root, "remote", "get-url", "origin").returncode == 0, (
        "fixture check: the root has an origin to resolve against"
    )
    for absolute in (
        "/srv/git/c.git",
        "https://example.invalid/org/c.git",
        "git@example.invalid:org/c.git",
    ):
        assert repo_proto.resolve_member_url(absolute, root) == absolute, absolute


def test_a_member_with_no_origin_takes_its_url_from_the_declaration(
    tmp_path: Path,
) -> None:
    """The other direction from the unmaterialized case: a member that IS
    materialized but carries no origin of its own still has a url, because the
    root declares one. Without this the fallback is a branch nothing exercises,
    and init would advise a `remote add` for a url it already had."""
    root, member = _superproject(tmp_path)
    removed = _run("remote", "remove", "origin", cwd=member)
    assert removed.returncode == 0, removed.stderr
    assert _run("remote", "get-url", "origin", cwd=member).returncode != 0, (
        "fixture check: the member has no origin"
    )

    result = make_cli_runner().invoke(app, ["workspace", "init", str(root)])
    assert result.exit_code == 0, result.stdout

    written = repo_proto.read_workspace_spec(root / ".grip" / "workspace_spec.toml").repos[0]
    assert written.url, "the declaration supplies the url the member lacks"
    assert "member-src" in written.url, f"it is the member's source: {written.url!r}"
    assert "remote add origin" not in result.stdout


def test_no_drift_is_reported_for_an_unmaterialized_member(tmp_path: Path) -> None:
    """The same read-through, on the status arm. With the member directory empty,
    reading HEAD from inside it returns the PLAIN CLONE'S OWN commit, so the
    status told the user their member was at a commit -- the root's -- and
    called it drift. Only a repo that IS its own toplevel can be at a commit."""
    root, member_path = _plain_clone_of_superproject(tmp_path)
    assert make_cli_runner().invoke(app, ["workspace", "init", str(root)]).exit_code == 0

    clone_head = _run("rev-parse", "HEAD", cwd=root).stdout.strip()
    rendered = migration.render_status(migration.workspace_status(root))

    assert "-- drift" not in rendered, rendered
    assert clone_head not in rendered, "the ROOT's own commit must not appear as a member's"
    assert member_path in rendered, "the member is still declared"
    # And the state is NAMED rather than left as a silence: a declared member
    # that is not there is not the same as a clean one.
    assert "not materialized" in rendered


def test_a_materialized_member_still_reads_its_own_state(tmp_path: Path) -> None:
    """Control for the two above: a member that IS materialized is its own
    toplevel and must keep reading its own head, or the fix would have replaced
    one wrong answer with another."""
    root, member = _superproject(tmp_path)
    pinned = _gitlink(root, "member1", tmp_path)
    got = _run("checkout", "-q", "--detach", cwd=member)
    assert got.returncode == 0, got.stderr
    head = _run("rev-parse", "HEAD", cwd=member).stdout.strip()

    result = make_cli_runner().invoke(app, ["workspace", "init", str(root)])
    assert result.exit_code == 0, result.stdout

    written = repo_proto.read_workspace_spec(root / ".grip" / "workspace_spec.toml").repos[0]
    assert written.pin == pinned
    assert written.detached is True, "a materialized detached member says so"
    assert head == pinned, "fixture check: detached AT the pinned commit"
