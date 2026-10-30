"""Two grip.toml members whose paths a case- and normalization-insensitive filesystem opens as one directory are
refused at the read, in the working document and in a selected commit: `.github` beside `.GITHUB`, `.github.`, or an
NFD spelling of an NFC name would clone two working copies into one directory on Windows or macOS."""
from __future__ import annotations

import subprocess
import unicodedata

import pytest

from gr2.python_cli import grip_cli

PIN = "1" * 40
CAFE_NFC = unicodedata.normalize("NFC", "café")
CAFE_NFD = unicodedata.normalize("NFD", "café")


def _member(name: str, path: str) -> str:
    return (f'[[members]]\nname = "{name}"\npath = "{path}"\npin = "{PIN}"\n'
            f'[members.remotes]\norigin = "https://example.invalid/x/{name}"\n')


def _read(tmp_path, monkeypatch, branch: str, *paths: str) -> list[str]:
    document = "".join(_member(f"m{i}", path) for i, path in enumerate(paths))
    if branch == "working":
        (tmp_path / "grip.toml").write_text(document)
        return [m["path"] for m in grip_cli._native_members(tmp_path)]

    def store_git(_root, *args):
        out = document if args[0] == "show" else f"160000 commit {PIN}\t{args[-1]}\n"
        return subprocess.CompletedProcess(list(args), 0, out, "")

    monkeypatch.setattr(grip_cli, "_store_git", store_git)
    return [m["path"] for m in grip_cli._native_members(tmp_path, "HEAD")]


BRANCHES = ["working", "revision"]
MESSAGE = {"working": "grip.toml has duplicate member paths",
           "revision": "selected workspace commit has duplicate member names or paths"}


def _refused(tmp_path, monkeypatch, branch, first, second):
    with pytest.raises(grip_cli.NativeStoreRefusal) as refusal:
        _read(tmp_path, monkeypatch, branch, first, second)
    text = str(refusal.value)
    assert MESSAGE[branch] in text
    assert refusal.value.code == 4
    return text


@pytest.mark.parametrize("branch", BRANCHES)
def test_r1_a_case_pair_is_refused_naming_both_paths(tmp_path, monkeypatch, branch):
    text = _refused(tmp_path, monkeypatch, branch, ".github", ".GITHUB")
    assert "'.github'" in text and "'.GITHUB'" in text


@pytest.mark.parametrize("branch", BRANCHES)
@pytest.mark.parametrize("twin", [".github.", ".github "])
def test_r2_a_trailing_dot_or_space_pair_is_refused(tmp_path, monkeypatch, branch, twin):
    """The `.github ` revision case is a PIN: base already refuses it, since _normalise_member_path strips the
    space. The `.github.` revision case is the witness."""
    _refused(tmp_path, monkeypatch, branch, ".github", twin)


@pytest.mark.parametrize("branch", BRANCHES)
def test_r3_an_nfd_spelling_beside_its_nfc_form_is_refused(tmp_path, monkeypatch, branch):
    assert CAFE_NFC != CAFE_NFD
    _refused(tmp_path, monkeypatch, branch, CAFE_NFC, CAFE_NFD)


@pytest.mark.parametrize("branch", BRANCHES)
def test_r4_an_nfd_spelling_with_a_trailing_dot_beside_its_nfc_form_is_refused(tmp_path, monkeypatch, branch):
    # Needs both folds: the alias alone keeps NFD apart from NFC, the Unicode fold alone keeps the trailing dot.
    _refused(tmp_path, monkeypatch, branch, CAFE_NFC, CAFE_NFD + ".")


@pytest.mark.parametrize("branch", BRANCHES)
@pytest.mark.parametrize("pair", [(".github", "github"), (".github", ".github2")])
def test_c1_c2_other_names_are_other_directories(tmp_path, monkeypatch, branch, pair):
    assert _read(tmp_path, monkeypatch, branch, *pair) == list(pair)


def test_c3_dot_slash_alpha_beside_alpha_is_still_refused(tmp_path, monkeypatch):
    _refused(tmp_path, monkeypatch, "working", "./alpha", "alpha")


@pytest.mark.parametrize("branch", BRANCHES)
@pytest.mark.parametrize("pair", [("a/x", "a./x"), ("a/x", "A/x")])
def test_r5_a_fold_on_a_leading_part_is_refused(tmp_path, monkeypatch, branch, pair):
    # Each part folds on its own: a trailing dot on a leading part names the same parent directory.
    _refused(tmp_path, monkeypatch, branch, *pair)
