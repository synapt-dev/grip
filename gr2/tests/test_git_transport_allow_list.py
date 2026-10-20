"""A member url cannot reach a `git-remote-<name>` helper, by two layers.

Layer 1, the read: `grip.url_transport_refusal` names any transport outside https, http, ssh, git and file, and
both readers refuse with it before any git call (a full-mode record's url at `member_field_tree._validate`, a
compat grip.toml remote at `_native_members`). Layer 2, git itself: a transport allow-list of https, http, ssh,
git always, and file as git's own `user` default.

Git runs any installed `git-remote-<name>` helper for a url shaped `<name>::<address>`, so a member url
in grip.toml reached a program on the reader's PATH through `store materialize`'s clone. The allow-list
lives in `gitops.NO_AUTO`, which every gr2 git spawn prepends. It is written as config pairs rather than
GIT_ALLOW_PROTOCOL because that variable makes each listed protocol `always`, which loosens git's
`file=user` default for submodule recursion; the config form keeps it.

Each refusal row puts a stand-in helper on PATH that leaves a marker file when git runs it. The marker
is the witness, not the exit code: the helper exits 1, so the clone fails with or without the
allow-list, and only the marker says whether the helper ran.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.test_store_break_attempts import (
    _assert_init_ran,
    _cli,
    _set_member_remote,
    two_member_ws,
)


def _stand_in_helper(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str = "pwn") -> Path:
    """`git-remote-<name>` on PATH, leaving a marker when run. Returns the marker's path."""
    bindir = tmp_path / "helper-bin"
    bindir.mkdir()
    marker = tmp_path / "helper-ran"
    helper = bindir / f"git-remote-{name}"
    helper.write_text(f'#!/bin/sh\ntouch "{marker}"\nexit 1\n')
    helper.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.delenv("GIT_ALLOW_PROTOCOL", raising=False)
    return marker


def _remove_checkout(root: Path, member: str) -> None:
    shutil.rmtree(root / member)
    assert not (root / member).exists()


def test_row1_compat_materialize_refuses_a_remote_helper_url(
    two_member_ws: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Row 1. A `pwn::` origin in grip.toml (compat mode): materialize refuses and the helper never runs."""
    root = two_member_ws
    assert _cli("store", "init", str(root))[0] == 0
    _assert_init_ran(root)
    assert _cli("store", "commit", "-m", "record")[0] == 0
    marker = _stand_in_helper(tmp_path, monkeypatch)
    manifest = root / "grip.toml"
    manifest.write_text(_set_member_remote(manifest.read_text(), "alpha", "origin", "pwn::x"))
    assert 'origin = "pwn::x"' in manifest.read_text()
    _remove_checkout(root, "alpha")

    rc, out = _cli("store", "materialize")

    assert not marker.exists(), (
        "git ran the stand-in git-remote-pwn for a pwn:: url in grip.toml; the transport "
        f"allow-list did not hold. rc={rc} out={out[:300]!r}"
    )
    assert rc != 0, f"materialize should refuse a member it could not clone. out={out[:300]!r}"
    assert not (root / "alpha" / ".git").exists()


def test_row3_recursive_clone_of_a_file_path_submodule_stays_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Row 3. gr2's clone keeps git's own `file=user` default: a file-path submodule is not fetched on recursion.

    GIT_ALLOW_PROTOCOL=https:ssh:git:file refuses the helper of row 1 just as well, and makes `file` always,
    so this clone then populates the submodule. This row is the difference between the two forms.
    """
    from gr2.python_cli import gitops

    monkeypatch.delenv("GIT_ALLOW_PROTOCOL", raising=False)
    ident = ("-c", "user.email=t@e.invalid", "-c", "user.name=t")
    sub, sup = tmp_path / "sub", tmp_path / "sup"
    for repo in (sub, sup):
        repo.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True, capture_output=True)
    subprocess.run(["git", *ident, "-C", str(sub), "commit", "-q", "--allow-empty", "-m", "s"], check=True,
                   capture_output=True)
    subprocess.run(["git", *ident, "-C", str(sup), "-c", "protocol.file.allow=always", "submodule", "add", "-q",
                    str(sub), "s"], check=True, capture_output=True)
    subprocess.run(["git", *ident, "-C", str(sup), "commit", "-q", "-m", "sup"], check=True, capture_output=True)
    dest = tmp_path / "dest"

    proc = gitops.clone("--quiet", "--recurse-submodules", f"file://{sup}", str(dest))

    populated = sorted(p.name for p in (dest / "s").iterdir()) if (dest / "s").is_dir() else []
    assert not populated, (
        "gr2's clone fetched a file-path submodule on recursion; git's default refuses it (file=user). "
        f"rc={proc.returncode} populated={populated} stderr={proc.stderr[-300:]!r}"
    )


def test_row1_control_compat_materialize_still_clones_a_file_url(
    two_member_ws: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Control for row 1. The same shape with the member's own file:// origin clones under the allow-list."""
    root = two_member_ws
    assert _cli("store", "init", str(root))[0] == 0
    _assert_init_ran(root)
    assert _cli("store", "commit", "-m", "record")[0] == 0
    marker = _stand_in_helper(tmp_path, monkeypatch)
    _remove_checkout(root, "alpha")

    rc, out = _cli("store", "materialize")

    assert rc == 0, f"materialize should clone a file:// member under the allow-list. out={out[:300]!r}"
    assert (root / "alpha" / ".git").exists()
    assert not marker.exists()


# --- Layer 1: the read refuses the url by name, before any git call ------------------------------------------------
#
# Layer 2 (above) is git's own refusal. Each layer-1 row below also DISABLES layer 2, the way a caller can: a
# GIT_ALLOW_PROTOCOL in the environment overrides the config list, so with `pwn` in it git would run the helper.
# The control row proves that override really disables layer 2; without it, a layer-1 row could pass on layer 2.

_LAYER2_OFF = "pwn:https:http:ssh:git:file"


def test_layer2_off_control_git_itself_runs_the_helper(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Control for every layer-1 row: with the override, a gr2 git call DOES run the helper."""
    from gr2.python_cli import gitops

    marker = _stand_in_helper(tmp_path, monkeypatch)
    monkeypatch.setenv("GIT_ALLOW_PROTOCOL", _LAYER2_OFF)
    gitops.clone("--quiet", "pwn::x", str(tmp_path / "dest"))
    assert marker.exists(), "the override did not disable layer 2, so the layer-1 rows below would prove nothing"


@pytest.mark.parametrize("url,refused", [
    ("https://example.invalid/r.git", None), ("http://example.invalid/r", None), ("ssh://git@example.invalid/r", None),
    ("git://example.invalid/r", None), ("file:///abs/r.git", None), ("git+ssh://example.invalid/r", None),
    ("ssh+git://example.invalid/r", None), ("git@github.com:o/r.git", None), ("host.example:o/r.git", None),
    ("/abs/r.git", None), ("../r.git", None), ("C:/repos/r.git", None), ("r.git", None), ("https::x", None),
    ("pwn::x", "pwn"), ("PWN::x", "PWN"), ("pwn://x", "pwn"), ("ext::sh -c x", "ext"), ("ftp://example.invalid/r", "ftp"),
    # Git matches the name exactly: FILE:// runs a helper named git-remote-FILE, not the file transport.
    ("FILE:///abs/r.git", "FILE"), ("HTTPS://example.invalid/r", "HTTPS"),
    # Git lets a helper name start with a digit: `1pwn::x` runs git-remote-1pwn.
    ("1pwn::x", "1pwn"), ("1pwn://x", "1pwn"),
])
def test_the_transport_rule(url: str, refused: str | None) -> None:
    from gr2.python_cli import grip

    assert grip.url_transport_refusal(url) == refused


def _record_root(tmp_path: Path, url: str) -> Path:
    """A full-mode root whose alpha record carries `url`, in a root commit as any pusher could write it."""
    from gr2.python_cli import member_field_tree as mft
    from tests.test_store_full_mode_carrier import _full_store, _member_head
    from tests.test_store_git_native_smoke import git

    root = _full_store(tmp_path)
    record = mft.commit(root, dict(kind=mft.KIND_REPO, pin=_member_head(root, "alpha"), url=url, ref="main",
                                   covered_by="refs/heads/main", covered_test=mft.COVER_REACHABLE), "record")
    git(root, "update-index", "--add", "--cacheinfo", f"160000,{record},alpha")
    tree = git(root, "write-tree").stdout.strip()
    head = git(root, "rev-parse", "HEAD").stdout.strip()
    made = git(root, "-c", "user.name=x", "-c", "user.email=x@example.invalid", "commit-tree", tree,
               "-p", head, "-p", record, "-m", "carry the record").stdout.strip()
    git(root, "update-ref", "HEAD", made)
    git(tmp_path, "clone", "-q", str(root), "victim")
    return tmp_path / "victim"


@pytest.mark.parametrize("layer2", ["on", "off"])
def test_row2_a_forged_legacy_record_in_a_sidecar_gitlink_is_never_read_as_a_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, layer2: str
) -> None:
    """Row 2. A legacy record with url `pwn::x` as alpha's gitlink: materialize fails, the helper never runs, and
    alpha's clone holds nothing from the forged commit, with layer 2 on and off (the read alone).
    In sidecar mode grip.toml is the url's only home, so the record's url is never read (row 2b covers grip.toml)."""
    from tests.test_store_git_native_smoke import git, gr2

    victim = _record_root(tmp_path, "pwn::x")
    forged = git(victim, "rev-parse", "HEAD:alpha").stdout.strip()
    marker = _stand_in_helper(tmp_path, monkeypatch)
    if layer2 == "off":
        monkeypatch.setenv("GIT_ALLOW_PROTOCOL", _LAYER2_OFF)

    made = gr2(victim, "materialize", check=False)

    out = f"{made.stdout}\n{made.stderr}"
    assert not marker.exists(), f"git ran git-remote-pwn for a record url (layer 2 {layer2}). {out[-300:]}"
    assert made.returncode != 0, out
    assert "Traceback" not in out, out
    alpha = victim / "alpha"
    assert (alpha / ".git").exists(), f"alpha's clone is missing (layer 2 {layer2}), so the checks below would see nothing"
    has = subprocess.run(["git", "-C", str(alpha), "cat-file", "-e", forged], capture_output=True)
    assert has.returncode != 0, f"alpha's clone holds the forged commit {forged}"
    refs = git(alpha, "for-each-ref", "--points-at", forged).stdout.strip()
    assert refs == "", f"alpha's clone has refs at the forged commit: {refs}"


@pytest.mark.parametrize("layer2", ["on", "off"])
def test_row2b_full_mode_grip_toml_url_is_refused_at_the_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, layer2: str
) -> None:
    """Row 2b. A `pwn::x` origin for alpha in a full-mode root's grip.toml: materialize refuses by name and the
    helper never runs, with layer 2 on, and with layer 2 off (the read alone)."""
    from tests.test_store_full_mode_carrier import _full_store
    from tests.test_store_git_native_smoke import git, gr2

    root = _full_store(tmp_path)
    manifest = root / "grip.toml"
    manifest.write_text(_set_member_remote(manifest.read_text(), "alpha", "origin", "pwn::x"))
    assert 'origin = "pwn::x"' in manifest.read_text()
    git(root, "-c", "user.name=x", "-c", "user.email=x@example.invalid", "commit", "-q", "-am", "pwn origin")
    git(tmp_path, "clone", "-q", str(root), "victim")
    victim = tmp_path / "victim"
    marker = _stand_in_helper(tmp_path, monkeypatch)
    if layer2 == "off":
        monkeypatch.setenv("GIT_ALLOW_PROTOCOL", _LAYER2_OFF)

    made = gr2(victim, "materialize", check=False)

    out = f"{made.stdout}\n{made.stderr}"
    assert not marker.exists(), f"git ran git-remote-pwn for a grip.toml url (layer 2 {layer2}). {out[-300:]}"
    assert made.returncode == 4, out
    assert "uses the 'pwn' transport" in out, out
    assert "Traceback" not in out, out


@pytest.mark.parametrize("name", ["pwn", "1pwn"])
def test_row4_compat_grip_toml_url_is_refused_at_the_read_with_layer2_off(
    two_member_ws: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    """Row 4. A `<name>::` origin in grip.toml (compat), layer 2 off: the read refuses by name and the helper never runs.
    `1pwn` is a real helper name to git, which allows a digit first."""
    root = two_member_ws
    assert _cli("store", "init", str(root))[0] == 0
    _assert_init_ran(root)
    assert _cli("store", "commit", "-m", "record")[0] == 0
    marker = _stand_in_helper(tmp_path, monkeypatch, name)
    monkeypatch.setenv("GIT_ALLOW_PROTOCOL", f"{name}:{_LAYER2_OFF}")
    manifest = root / "grip.toml"
    manifest.write_text(_set_member_remote(manifest.read_text(), "alpha", "origin", f"{name}::x"))
    _remove_checkout(root, "alpha")

    rc, out = _cli("store", "materialize")

    assert not marker.exists(), f"git ran git-remote-{name} for a grip.toml url with layer 2 off. rc={rc} out={out[:300]!r}"
    assert rc == 4, f"rc={rc} out={out[:300]!r}"
    assert f"remote origin uses the '{name}' transport" in out, out


def test_row5_compat_non_origin_remote_is_refused_at_the_read_with_layer2_off(
    two_member_ws: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Row 5. Every remote is read, not only origin: alpha keeps its file:// origin and gains `upstream = "pwn::x"`."""
    root = two_member_ws
    assert _cli("store", "init", str(root))[0] == 0
    _assert_init_ran(root)
    assert _cli("store", "commit", "-m", "record")[0] == 0
    marker = _stand_in_helper(tmp_path, monkeypatch)
    monkeypatch.setenv("GIT_ALLOW_PROTOCOL", _LAYER2_OFF)
    manifest = root / "grip.toml"
    blocks = manifest.read_text().split("[[members]]")
    hits = [i for i, block in enumerate(blocks) if i and 'path = "alpha"' in block]
    assert len(hits) == 1, hits
    blocks[hits[0]], n = re.subn(r'(?m)^(\s*)origin\s*=\s*"file://[^"]*"$', r'\g<0>\n\g<1>upstream = "pwn::x"',
                                 blocks[hits[0]])
    assert n == 1, blocks[hits[0]]
    manifest.write_text("[[members]]".join(blocks))
    assert 'upstream = "pwn::x"' in manifest.read_text()
    _remove_checkout(root, "alpha")

    rc, out = _cli("store", "materialize")

    assert not marker.exists(), f"git ran git-remote-pwn for a non-origin remote. rc={rc} out={out[:300]!r}"
    assert rc == 4, f"rc={rc} out={out[:300]!r}"
    assert "remote upstream uses the 'pwn' transport" in out, out
