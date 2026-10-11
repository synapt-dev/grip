"""gr2 runs git with a transport allow-list: https, http, ssh, git always; file as git's own `user` default.

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


def _stand_in_helper(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """`git-remote-pwn` on PATH, leaving a marker when run. Returns the marker's path."""
    bindir = tmp_path / "helper-bin"
    bindir.mkdir()
    marker = tmp_path / "helper-ran"
    helper = bindir / "git-remote-pwn"
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
