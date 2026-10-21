"""host_size_refusal: name the file, its size and every member over the limit."""
from __future__ import annotations

import subprocess
from pathlib import Path

from gr2.python_cli.grip_cli import host_size_refusal

# The stderr names a limit of 0.01 MB (10485.76 bytes read as MiB), so a
# 20000-byte blob is over it and a 100-byte blob is under it.
BIG = "b" * 20000
SMALL = "s" * 100


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True,
        text=True, stdin=subprocess.DEVNULL,
    ).stdout.strip()


def _member(tmp_path: Path, name: str, files: dict[str, str]) -> tuple[str, str, str]:
    repo = tmp_path / name
    repo.mkdir()
    _git(repo, "init", "-q")
    for rel, body in files.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body)
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "c")
    return (name, str(repo), _git(repo, "rev-parse", "HEAD"))


def _size_line(path: str) -> str:
    return (
        "remote: error: Trace: 0123abcd\n"
        "remote: error: See https://gh.io/lfs for more information.\n"
        f"remote: error: File {path} is 0.02 MB; this exceeds GitHub's "
        "file size limit of 0.01 MB\n"
        "remote: error: GH001: Large files detected.\n"
        " ! [remote rejected] main -> main (pre-receive hook declined)\n"
    )


def test_a_second_member_holds_path(tmp_path):
    pushed = [
        _member(tmp_path, "app", {"src/main.py": "x"}),
        _member(tmp_path, "data", {"assets/big.bin": BIG}),
    ]
    got = host_size_refusal(_size_line("assets/big.bin"), pushed)
    assert got == ("assets/big.bin", "0.02 MB", ("data",))


def test_b_no_pushed_commit_holds_path(tmp_path):
    pushed = [
        _member(tmp_path, "app", {"src/main.py": "x"}),
        _member(tmp_path, "data", {"assets/small.bin": BIG}),
    ]
    got = host_size_refusal(_size_line("assets/big.bin"), pushed)
    assert got == ("assets/big.bin", "0.02 MB", ())


def test_c_non_size_refusal(tmp_path):
    pushed = [_member(tmp_path, "app", {"assets/big.bin": BIG})]
    stderr = (
        "To github.com:o/app.git\n"
        " ! [rejected]        main -> main (fetch first)\n"
        "error: failed to push some refs to 'github.com:o/app.git'\n"
    )
    assert host_size_refusal(stderr, pushed) is None


def test_d_path_with_space(tmp_path):
    pushed = [
        _member(tmp_path, "app", {"src/main.py": "x"}),
        _member(tmp_path, "data", {"raw data/big file.bin": BIG}),
    ]
    got = host_size_refusal(_size_line("raw data/big file.bin"), pushed)
    assert got == ("raw data/big file.bin", "0.02 MB", ("data",))


def test_e1_two_hold_path_only_one_over(tmp_path):
    pushed = [
        _member(tmp_path, "app", {"assets/big.bin": SMALL}),
        _member(tmp_path, "data", {"assets/big.bin": BIG}),
    ]
    got = host_size_refusal(_size_line("assets/big.bin"), pushed)
    assert got == ("assets/big.bin", "0.02 MB", ("data",))


def test_e2_both_over_in_push_order(tmp_path):
    pushed = [
        _member(tmp_path, "web", {"assets/big.bin": BIG}),
        _member(tmp_path, "api", {"assets/big.bin": BIG + "x"}),
    ]
    got = host_size_refusal(_size_line("assets/big.bin"), pushed)
    assert got == ("assets/big.bin", "0.02 MB", ("web", "api"))


def test_e3_neither_over_member_not_determined(tmp_path):
    pushed = [
        _member(tmp_path, "app", {"assets/big.bin": SMALL}),
        _member(tmp_path, "data", {"assets/big.bin": SMALL + "y"}),
    ]
    got = host_size_refusal(_size_line("assets/big.bin"), pushed)
    assert got == ("assets/big.bin", "0.02 MB", ())


def test_f_replace_ref_to_big_blob_does_not_make_member_over(tmp_path):
    pushed = [_member(tmp_path, "m", {"assets/big.bin": SMALL})]
    repo = Path(pushed[0][1])
    (repo / "assets/big.bin").write_text(BIG)
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qam", "big")
    _git(repo, "update-ref", f"refs/replace/{pushed[0][2]}", _git(repo, "rev-parse", "HEAD"))
    got = host_size_refusal(_size_line("assets/big.bin"), pushed)
    assert got == ("assets/big.bin", "0.02 MB", ())
